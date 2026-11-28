"""Live HLS output for players without Media Source Extensions (iPad/iPhone Safari,
older smart TVs). ffmpeg writes a rolling playlist + 2-second segments into a temp
folder per viewer; sessions nobody asks for in a while are stopped. Channels and feeds are read
from a shared tuner (tuner.py), so this ffmpeg only cuts segments; On Demand titles read their own."""
import asyncio
import logging
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config
from .streaming import NO_WINDOW, buffer_seconds, ffmpeg_bin, kill, play_args, track

log = logging.getLogger("tunerbox.hls")
ROOT = Path(tempfile.gettempdir()) / "tunerbox-hls"
IDLE = 30                       # seconds without a request before a session is stopped
MAX_TIMESHIFT = 240             # most minutes of live TV a session keeps to pause and rewind in
TS_MB_PER_MIN = 40              # about what a minute of a 5 Mbps channel takes on disk
TS_SPARE_MB = 1024              # disk always left free by timeshift sessions
SAFE_FILE = re.compile(r"^(index\.m3u8|seg\d{5,}\.ts)$")     # seg%05d.ts grows a digit after 99999

_sessions = {}                  # sid -> dict(proc, dir, last, client)
_lock = threading.Lock()
_reaper = None


def ts_root():
    """Timeshift sessions keep many minutes of segments: under the data folder (the /data volume in Docker),
    not the temp folder, which is RAM on many Linux hosts and the container's own layer in Docker."""
    return config.DATA / "timeshift-hls"


def _ts_room(minutes):
    """`minutes`, cut to what the disk holding ts_root() has room for (other sessions' share already taken)."""
    if minutes <= 0:
        return 0
    try:
        root = ts_root()
        root.mkdir(parents=True, exist_ok=True)
        free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    except OSError:
        return 0
    with _lock:
        others = [(s.get("timeshift", 0), s["dir"]) for s in _sessions.values() if s.get("timeshift")]
    # what other sessions will still write (what they've written is already out of `free`)
    taken = sum(max(0, m * TS_MB_PER_MIN - _dir_mb(d)) for m, d in others)
    room = (free_mb - TS_SPARE_MB - taken) // TS_MB_PER_MIN
    if room < minutes:
        log.warning("timeshift: room for %s of %s minutes (%s MB free)", max(0, room), minutes, free_mb)
    return int(max(0, min(minutes, room)))


def _dir_mb(d):
    try:
        return sum(f.stat().st_size for f in Path(d).iterdir()) // (1024 * 1024)
    except OSError:
        return 0


def _segments(d):
    try:
        return sorted(f for f in Path(d).iterdir() if f.suffix == ".ts")
    except OSError:
        return []


def _trim_timeshift():
    """A channel can run well above the 40 MB a minute _ts_room budgets for, so while the data folder's
    disk is below TS_SPARE_MB free, drop the oldest minute of the biggest timeshift session (playlist()
    then leaves those segments out). The newest 30 s of each session are never dropped."""
    with _lock:
        dirs = [s["dir"] for s in _sessions.values() if s.get("timeshift")]
    for _ in range(20):
        if not dirs:
            return
        try:
            if shutil.disk_usage(ts_root()).free // (1024 * 1024) >= TS_SPARE_MB:
                return
        except OSError:
            return
        d = max(dirs, key=_dir_mb)
        segs = _segments(d)[:-15]
        if not segs:
            return
        log.warning("timeshift: disk low, dropping the oldest minute of %s", d.name)
        for f in segs[:30]:
            f.unlink(missing_ok=True)


def _hide_missing(text, d):
    """A timeshift playlist without the oldest segments _trim_timeshift dropped (its sequence moved on)."""
    lines = text.split("\n")
    try:
        first = next(i for i, ln in enumerate(lines) if ln.startswith("#EXTINF"))
    except StopIteration:
        return text
    head, body, dropped = lines[:first], lines[first:], 0
    while body:
        try:
            uri = next(i for i, ln in enumerate(body) if ln and not ln.startswith("#"))
        except StopIteration:
            break
        if (d / body[uri]).exists():
            break
        body, dropped = body[uri + 1:], dropped + 1
    if not dropped:
        return text
    for i, ln in enumerate(head):
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            head[i] = f"#EXT-X-MEDIA-SEQUENCE:{int(ln.split(':', 1)[1]) + dropped}"
    return "\n".join(head + body)


def timeshift_minutes(asked=None):
    """Minutes of live TV a session keeps for pausing and rewinding: what Setup allows (timeshift_minutes),
    capped at what the screen `asked` for (None: Setup's)."""
    try:
        most = max(0, min(int(config.get("timeshift_minutes", 0) or 0), MAX_TIMESHIFT))
    except (TypeError, ValueError):
        most = 0
    if asked is None:
        return most
    return max(0, min(int(asked), most))


def _cmd(raw_url, mode=None, buffer=0, pre=(), audio=None, timeshift=0):
    if raw_url is None:         # from a tuner on stdin: already in the right format
        src = ["-f", "mpegts", "-i", "pipe:0", "-c", "copy"]
    else:
        inp, vid, aud = play_args(raw_url, mode or config.get("video_mode", "copy"), buffer)
        maps = ["-map", "0:v:0?", "-map", f"0:a:{int(audio)}?"] if audio is not None else []    # a chosen audio track
        src = ["-nostdin", *pre, *inp, *maps, "-sn", "-dn", *vid, *aud]
    return [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", *src,
            "-f", "hls", "-hls_time", "2", "-hls_list_size", str(max(8 + buffer // 2, timeshift * 30)),
            "-hls_allow_cache", "0",
            "-hls_flags", "delete_segments+independent_segments+omit_endlist",
            "-hls_segment_filename", "seg%05d.ts", "index.m3u8"]


def _stop(sid):
    with _lock:
        s = _sessions.pop(sid, None)
    if s:
        if s.get("sub"):
            s["sub"].close()
        kill(s["proc"])
        shutil.rmtree(s["dir"], ignore_errors=True)


def stop_client(client):
    for sid in [k for k, s in list(_sessions.items()) if client and s["client"] == client]:
        _stop(sid)


def stop_all():
    for sid in list(_sessions):
        _stop(sid)


def _reap():
    while True:
        time.sleep(5)
        try:
            _trim_timeshift()
        except Exception:  # noqa: BLE001 - the reaper must keep running
            log.exception("timeshift trim")
        now = time.monotonic()
        for sid, s in list(_sessions.items()):
            if now - s["last"] > IDLE or s["proc"].poll() is not None and now - s["last"] > 5:
                log.info("HLS session %s ended", sid)
                _stop(sid)


def start(raw_url, client=None, mode=None, buffer=None, pre=(), label="", source_id=None, audio=None, timeshift=0):
    """`pre`: extra ffmpeg input options (On Demand: play at normal speed, from a start time).
    `timeshift`: minutes of the channel the playlist keeps, so the player can pause and rewind in it
    (0: the usual short rolling playlist).
    `label` names what it plays in Setup › Tuners; `source_id` is the playlist whose connection it
    uses. A channel on a full playlist raises connections.Full."""
    global _reaper
    from . import connections, streaming, tuner
    buffer = buffer_seconds(buffer)
    connections.after_stop(client, raw_url)
    # room first, so a full playlist doesn't cost the viewer what it was watching (checked again below)
    if pre:
        connections.check(source_id, client=client or "")
    else:
        connections.check(source_id, raw_url, mode or config.get("video_mode", "copy"), "aac", client or "")
    if client:                  # one channel or movie per viewer, whether it played over HLS or TS
        stop_client(client)
        streaming.stop_client(client)
    timeshift = _ts_room(timeshift)
    sid = secrets.token_hex(6)
    d = (ts_root() if timeshift else ROOT) / sid
    d.mkdir(parents=True, exist_ok=True)
    sub = None
    # checked and registered as one step, so two screens can't both take a playlist's last connection
    with connections.admit:
        try:
            if not pre:         # a channel or feed: share its tuner (tuner.join checks the limit)
                sub = tuner.join(raw_url, mode, "aac", buffer, name=f"hls:{client or sid}", source_id=source_id)
            elif source_id is not None:     # On Demand: a connection of its own
                connections.check(source_id, client=client or "")
            proc = subprocess.Popen(_cmd(None if sub else raw_url, mode, buffer, pre, audio, timeshift), cwd=d,
                                    stdin=subprocess.PIPE if sub else subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        except Exception:
            if sub:
                sub.close()
            shutil.rmtree(d, ignore_errors=True)
            raise
        track(proc)
        err = []
        with _lock:
            _sessions[sid] = {"proc": proc, "dir": d, "last": time.monotonic(), "client": client, "err": err,
                              "buffer": buffer, "sub": sub, "url": raw_url, "label": label, "since": int(time.time()),
                              "source_id": source_id, "timeshift": timeshift}
            if _reaper is None:
                _reaper = threading.Thread(target=_reap, daemon=True)
                _reaper.start()
    if sub:
        threading.Thread(target=_feed, args=(sub, proc), daemon=True).start()
    threading.Thread(target=lambda: err.extend(proc.stderr.read().decode("utf-8", "ignore").splitlines()[-5:]),
                     daemon=True).start()
    return sid


def playing():
    """HLS sessions with their own provider connection (On Demand on iPhone / iPad): [{url, label, client, since}].
    Channels share a tuner, so tuner.running() lists them. `url` holds the provider login."""
    with _lock:
        return [{"id": sid, "url": s["url"], "label": s["label"], "client": s["client"] or "", "since": s["since"],
                 "source_id": s.get("source_id")}
                for sid, s in _sessions.items() if not s["sub"] and s.get("url")]


def keep_alive(client):
    """A paused player asks for nothing, so a screen paused on live TV says it's still there (GET
    /api/timeshift?client=). Number of its timeshift sessions kept (only those: nothing else is held)."""
    n = 0
    now = time.monotonic()
    with _lock:
        for s in _sessions.values():
            if client and s["client"] == client and s.get("timeshift"):
                s["last"] = now
                n += 1
    return n


def stop_id(sid):
    """Force-stop one session (Setup › Tuners › Stop). True if it was running."""
    if sid not in _sessions:
        return False
    _stop(sid)
    return True


def _feed(sub, proc):
    """Tuner -> this session's ffmpeg. Ends (and ffmpeg with it) when the tuner does."""
    try:
        while True:
            chunk = sub.get()
            if not chunk:
                break
            proc.stdin.write(chunk)
    except (OSError, ValueError):   # ffmpeg stopped
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        sub.close()


async def wait_ready(sid, timeout=25.0):
    """True once the playlist lists enough segments: two to start smoothly, more with the extra cache."""
    s = _sessions.get(sid)
    want = 2 + (s["buffer"] // 2 if s else 0)
    end = time.monotonic() + timeout + (s["buffer"] if s else 0)
    while time.monotonic() < end:
        s = _sessions.get(sid)
        if not s:
            return False
        pl = s["dir"] / "index.m3u8"
        if pl.exists() and pl.read_text(errors="ignore").count(".ts") >= want:
            return True
        if s["proc"].poll() is not None:
            log.warning("HLS ffmpeg exited: %s", " | ".join(s["err"]))
            _stop(sid)
            return False
        s["last"] = time.monotonic()
        await asyncio.sleep(0.25)
    _stop(sid)
    return False


def playlist(sid, path):
    """The live playlist; with the extra cache on, Safari is told to start that far behind live."""
    text = path.read_text(errors="ignore")
    s = _sessions.get(sid)
    if s and s.get("timeshift"):
        text = _hide_missing(text, s["dir"])
    if s and s["buffer"] and "#EXT-X-START" not in text:
        text = text.replace("#EXTM3U", f"#EXTM3U\n#EXT-X-START:TIME-OFFSET=-{s['buffer'] + 4},PRECISE=NO", 1)
    return text


def master(sid):
    """A master playlist over this session's index.m3u8 that names the CEA-608 captions carried in the video
    (CC1), so players that list only the captions a playlist declares (Roku) find them. None: no such session."""
    s = _sessions.get(sid)
    if not s:
        return None
    s["last"] = time.monotonic()
    return ("#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n"
            '#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="English",LANGUAGE="en",INSTREAM-ID="CC1",'
            "DEFAULT=YES,AUTOSELECT=YES\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=8000000,CLOSED-CAPTIONS="cc"\n'
            "index.m3u8\n")


def file(sid, name):
    s = _sessions.get(sid)
    if not s or not SAFE_FILE.match(name):
        return None
    s["last"] = time.monotonic()
    p = s["dir"] / name
    return p if p.exists() else None


def cleanup_stale():
    """Remove folders left behind by a previous run."""
    for root in (ROOT, ts_root()):
        if root.exists():
            for d in root.iterdir():
                if d.name not in _sessions:
                    shutil.rmtree(d, ignore_errors=True)
