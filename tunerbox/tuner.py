"""Shared tuners: one ffmpeg, and so one provider connection, per channel, however many TVs, phones,
Plex and the DVR are on it. Xtream accounts often allow only 1-2 connections, so two TVs on the same
channel, or a TV and a recording, must not each open their own.

A tuner keeps the stream from its last video keyframe (further back with the stream cache on), so a
screen that joins a channel someone is already watching gets a picture at once and a cache fills
straight away. Each viewer reads through its own queue, so a slow one never holds up the others or
the provider connection; one that falls too far behind is dropped and reconnects."""
import itertools
import logging
import queue
import subprocess
import threading
import time
from collections import deque

from . import config
from .probe import split_url
from . import streaming
from .streaming import NO_WINDOW, _waker, buffer_seconds, kill, live_cmd, track

log = logging.getLogger("tunerbox.tuner")
TS = 188                                    # MPEG-TS packet size
MAX_BACKLOG = 64_000_000                    # most stream kept per tuner for viewers who join later
MIN_RETAIN = 10                             # seconds always kept, so a cache can fill at once
QUEUE_CHUNKS = 256                          # per reader: 16 MB and up, rides out Wi-Fi hiccups
ALONE_WAIT = 30                             # seconds a tuner waits for its only reader before dropping it
VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x42, 0xEA}   # MPEG-1/2, MPEG-4, H.264, HEVC, AVS, VC-1

_tuners = []                                # running tuners
_lock = threading.Lock()                    # guards _tuners; taken before a tuner's own lock
_clients = {}                               # client id -> its Subscriber (one channel per screen)
# client id -> (tuner, until): kept running a moment after that screen lets go, because it's coming
# straight back to the same channel (captions: re-opened to play further behind live)
_held = {}


class Subscriber:
    """One reader of a tuner: a screen, an HLS session or a recording."""

    def __init__(self, tuner, buffer, name, tap=False):
        self.tuner, self.name, self.tap = tuner, name, tap
        self.q = queue.Queue(maxsize=QUEUE_CHUNKS + buffer * 40)
        self.wake = None                                   # set by an async reader
        # the timestamp (ms) this reader's stream starts its player's clock from (captions.py)
        self.base = None
        self._bf = None if tap else _captions().BaseFinder()

    def _note(self, chunk):
        if self._bf is not None:
            b = self._bf.feed(chunk)
            if b is not None:
                self.base, self._bf = (b if b >= 0 else None), None

    def put(self, chunk, wait=0):
        """From the tuner's thread. False when this reader has fallen too far behind. `wait`: seconds
        to hold the tuner for it (only when it's alone on the tuner, so nobody else is held up)."""
        try:
            if wait:
                self.q.put(chunk, timeout=wait)
            else:
                self.q.put_nowait(chunk)
        except queue.Full:
            return False
        if self._bf is not None:
            self._note(chunk)
        if self.wake:
            self.wake()
        return True

    def end(self):
        """No more data: b"" tells the reader (making room for it if the queue is full)."""
        while True:
            try:
                self.q.put_nowait(b"")
                break
            except queue.Full:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
        if self.wake:
            self.wake()

    def get(self, timeout=None):
        """Next chunk (b"" at the end); raises queue.Empty after `timeout` seconds."""
        return self.q.get(timeout=timeout)

    def close(self, wait=True):
        """Leave the tuner; the last one out stops it. wait=False stops ffmpeg in the background."""
        self.tuner.leave(self, wait)


class Tuner:
    def __init__(self, url, mode, audio, buffer, source_id=None):
        self.url, self.mode, self.audio = url, mode, audio
        self.source_id = source_id          # the playlist whose connection this uses (connections.py)
        self.retain = max(buffer, MIN_RETAIN)   # seconds of stream kept for viewers who join later
        self.subs = []
        self.taps = []                      # readers that don't keep it running (captions.py)
        self.lock = threading.Lock()
        self.chunks = deque()               # (seq, arrival time, bytes); seqs are consecutive
        self.keys = deque()                 # (seq, time) of chunks that start at a video keyframe
        self.seq = 0
        self.size = 0
        self.psi = {}                       # pid -> latest PAT / PMT packet, sent first to a joiner
        self.pmt_pids = set()
        self.video_pid = None
        self.ended = False
        self.sent = 0
        self.started = time.time()
        self.err = []
        cmd = live_cmd(url, mode, audio, buffer)
        self.proc = track(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, bufsize=0))
        self._errt = threading.Thread(target=self._stderr, daemon=True)
        self._errt.start()
        threading.Thread(target=self._read, daemon=True).start()
        log.info("Tuner start (%s, %s): %s", mode, audio, split_url(url)[0][-60:])

    def error(self):
        return " | ".join(self.err)

    # ------------------------------------------------------------ viewers
    def add(self, buffer, name):
        """New reader (call with _lock held). It first gets the kept stream from a keyframe."""
        sub = Subscriber(self, buffer, name)
        with self.lock:
            self.retain = max(self.retain, buffer)
            seed = self._seed(buffer)
            if seed:
                sub.q.put_nowait(seed)
                sub._note(seed)
            self.subs.append(sub)
            n = len(self.subs)
        if n > 1:
            log.info("%s joined %s (%d on this tuner)", name or "viewer", split_url(self.url)[0][-40:], n)
        return sub

    def tap(self, name):
        """A reader of everything kept and everything to come that doesn't count as a viewer: the
        tuner stops when its last viewer leaves, whatever taps it has. One that falls behind misses data."""
        sub = Subscriber(self, 0, name, tap=True)
        with self.lock:
            seed = self._seed(self.retain)
            if seed:
                sub.q.put_nowait(seed)
            if self.ended:
                sub.end()
            else:
                self.taps.append(sub)
        return sub

    def stop(self):
        """Stop at once (a recording needs the connection): every reader gets the end of the stream."""
        with _lock:
            with self.lock:
                self.ended = True
            if self in _tuners:
                _tuners.remove(self)
        # the reader thread then ends each viewer (_end). In the background: this runs while a
        # recording holds connections.admit, and taskkill takes a moment, so no other tune waits for it
        threading.Thread(target=kill, args=(self.proc,), daemon=True).start()

    def leave(self, sub, wait=True):
        if sub.tap:
            with self.lock:
                if sub in self.taps:
                    self.taps.remove(sub)
            sub.end()
            return
        with _lock:
            with self.lock:
                if sub in self.subs:
                    self.subs.remove(sub)
                last = not self.subs and not self.ended
                h = _held.get(sub.name)
                if last and h and h[0] is self and h[1] > time.monotonic():
                    # its screen is coming straight back: stop only if it doesn't
                    threading.Timer(h[1] - time.monotonic(), self.let_go).start()
                    last = False
                elif last:                  # nobody can join a tuner that's stopping
                    self.ended = True
                    if self in _tuners:
                        _tuners.remove(self)
        sub.end()
        if last:
            if wait:
                kill(self.proc)
            else:                           # taskkill takes a moment: never make the server wait
                threading.Thread(target=kill, args=(self.proc,), daemon=True).start()

    def let_go(self):
        """Stop a held tuner (see hold) if nobody came back to it."""
        with _lock:
            with self.lock:
                if self.subs or self.ended:
                    return
                self.ended = True
                if self in _tuners:
                    _tuners.remove(self)
        threading.Thread(target=kill, args=(self.proc,), daemon=True).start()

    def _seed(self, buffer):
        """PAT/PMT, then the kept stream from the newest keyframe at least `buffer` seconds old."""
        if not self.chunks:
            return b""
        cutoff = time.monotonic() - buffer
        start = None
        for seq, t in reversed(self.keys):
            start = seq
            if t <= cutoff:
                break
        if start is None:                   # no keyframe seen (or no video): what there is
            start = self.chunks[0][0]
        first = self.chunks[0][0]
        body = [c[2] for c in itertools.islice(self.chunks, max(0, start - first), None)]
        psi = [self.psi[p] for p in (0, *sorted(self.pmt_pids)) if p in self.psi]
        return b"".join(psi + body)

    # ------------------------------------------------------------ ffmpeg output
    def _stderr(self):
        try:
            self.err.extend(self.proc.stderr.read().decode("utf-8", "ignore").splitlines()[-5:])
        except (OSError, ValueError):
            pass

    def _read(self):
        carry = b""
        try:
            while True:
                data = self.proc.stdout.read(65536)
                if not data:
                    break
                data = carry + data
                if data[0] != 0x47:
                    data = _resync(data)
                n = len(data) - len(data) % TS
                carry = data[n:]
                if n:
                    self._feed(data[:n])
        except (OSError, ValueError):       # pipe closed under us (ffmpeg killed)
            pass
        self._end()

    def _feed(self, buf):
        cuts = []
        vp = self.video_pid
        for off in range(0, len(buf), TS):
            b1 = buf[off + 1]
            pid = ((b1 & 0x1F) << 8) | buf[off + 2]
            if pid == vp:
                # start of a PES with the random-access flag: ffmpeg marks video keyframes this way
                if b1 & 0x40 and buf[off + 3] & 0x20 and buf[off + 4] and buf[off + 5] & 0x40:
                    cuts.append(off)
            elif pid == 0 or pid in self.pmt_pids:
                self._psi(pid, buf[off:off + TS])
                vp = self.video_pid
        now = time.monotonic()
        with self.lock:
            bounds = sorted({0, *cuts, len(buf)})
            for a, b in zip(bounds, bounds[1:]):
                if a in cuts:
                    self.keys.append((self.seq, now))
                self.chunks.append((self.seq, now, buf[a:b]))
                self.size += b - a
                self.seq += 1
            self._trim(now)
            subs = list(self.subs)
            taps = list(self.taps)
        self.sent += len(buf)
        # A viewer alone on its tuner slows the tuner down instead of being dropped, as before shared
        # tuners: at startup the guide refresh can starve the server just as the provider's first
        # burst arrives. Only a viewer holding up others is dropped.
        wait = ALONE_WAIT if len(subs) == 1 else 0
        for s in subs:
            if not s.put(buf, wait):
                log.warning("%s fell too far behind on %s: dropped", s.name or "viewer", split_url(self.url)[0][-40:])
                s.close()
        for s in taps:
            s.put(buf)

    def _trim(self, now):
        cutoff = now - self.retain
        while len(self.keys) >= 2 and self.keys[1][1] <= cutoff:
            self.keys.popleft()
        if self.keys:
            head = self.keys[0][0]
            while self.chunks and self.chunks[0][0] < head:
                self.size -= len(self.chunks.popleft()[2])
        else:                               # no keyframes (yet): keep about the last second
            while self.chunks and self.chunks[0][1] < cutoff - 1:
                self.size -= len(self.chunks.popleft()[2])
        while self.size > MAX_BACKLOG and len(self.chunks) > 1:
            self.size -= len(self.chunks.popleft()[2])
            while self.keys and self.keys[0][0] < self.chunks[0][0]:
                self.keys.popleft()

    def _psi(self, pid, pkt):
        """Remember PAT/PMT packets and find the video stream's PID from them."""
        if not pkt[1] & 0x40:               # only sections that start here (they fit in one packet)
            return
        self.psi[pid] = pkt
        p = 4
        if pkt[3] & 0x20:
            p += 1 + pkt[4]
        if p >= TS:
            return
        p += 1 + pkt[p]                     # pointer field
        if p + 12 > TS:
            return
        end = min(p + 3 + (((pkt[p + 1] & 0x0F) << 8) | pkt[p + 2]) - 4, TS)    # up to the CRC
        if pid == 0:
            pids = set()
            for i in range(p + 8, end - 3, 4):
                if (pkt[i] << 8) | pkt[i + 1]:          # program 0 is the network PID
                    pids.add(((pkt[i + 2] & 0x1F) << 8) | pkt[i + 3])
            if pids:
                self.pmt_pids = pids
        else:
            i = p + 12 + (((pkt[p + 10] & 0x0F) << 8) | pkt[p + 11])
            while i + 5 <= end:
                if pkt[i] in VIDEO_TYPES:
                    self.video_pid = ((pkt[i + 1] & 0x1F) << 8) | pkt[i + 2]
                    return
                i += 5 + (((pkt[i + 3] & 0x0F) << 8) | pkt[i + 4])

    def _end(self):
        with _lock:
            if self in _tuners:
                _tuners.remove(self)
            with self.lock:
                self.ended = True
                subs, self.subs = self.subs, []
                subs += self.taps
                self.taps = []
                self.chunks.clear()
                self.keys.clear()
        kill(self.proc)
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        self._errt.join(2)          # readers (the DVR) report ffmpeg's last words
        for s in subs:
            s.end()
        log.info("Tuner end after %.1f MB: %s", self.sent / 1e6, split_url(self.url)[0][-60:])
        if self.sent == 0:
            log.warning("No data from %s: %s", split_url(self.url)[0][:120], self.error())


def _resync(data):
    """Drop bytes up to the next pair of TS sync bytes."""
    for i in range(len(data)):
        if data[i] == 0x47 and (i + TS >= len(data) or data[i + TS] == 0x47):
            return data[i:]
    return b""


def _find(url, mode, audio, any_mode):
    """A running tuner this reader can use. Anyone takes AAC audio (Plex asks for the original, but
    sharing beats a second connection); `any_mode` (the DVR) takes whatever video mode is running."""
    best = None
    for t in _tuners:
        if t.url != url or t.ended or (t.audio != audio and t.audio != "aac"):
            continue
        if t.mode == mode and t.audio == audio:
            return t
        if t.mode == mode or any_mode:
            best = best or t
    return best


def join(url, mode=None, audio="aac", buffer=0, any_mode=False, name="", source_id=None, take=False):
    """A Subscriber on the tuner for `url`, starting one if none suits. Starting one when `source_id`'s
    playlist has no connection left raises connections.Full, unless `take` (a recording) and live TV
    or On Demand on that playlist can be stopped to make room."""
    from . import connections
    mode = mode or config.get("video_mode", "copy")
    buffer = buffer_seconds(buffer)
    client = name[4:] if name.startswith("hls:") else name
    # Check and start under connections.admit, so nobody else can take the room in between. That
    # includes a viewer that make_room stopped, whose screen tunes again at once.
    with connections.admit:
        with _lock:     # a running tuner: joining it is free. Decided and joined in one go, so it
            t = _find(url, mode, audio, any_mode)       # can't stop in between and leave a new,
            if t is not None:                           # uncounted connection to start instead
                return t.add(buffer, name)
        for attempt in range(3):
            try:    # outside _lock: counting what's in use takes it. url=None: a new connection
                connections.check(source_id, None, client=client)
                break
            except connections.Full:
                if not take or attempt == 2 or not connections.make_room(source_id, name):
                    raise
        with _lock:     # nothing else can start a tuner while admit is held; one may have stopped
            t = _find(url, mode, audio, any_mode)
            if t is None:
                t = Tuner(url, mode, audio, buffer, source_id)
                _tuners.append(t)
            return t.add(buffer, name)


def open_for(url, client_id=None, mode=None, audio="aac", buffer=None, source_id=None, name=""):
    """A screen tunes `url`: take it off whatever it was playing (channel, movie, TS or HLS), then join
    the tuner. Raises connections.Full when the playlist has no connection left, before any response
    is sent, so the screen gets a 503 instead of an empty stream. Checked before the old stream is
    stopped, so a full playlist doesn't cost the screen what it was watching (join checks again).
    `name`: who it is for Setup › Tuners when there's no `client_id` (connections.app_reader)."""
    from . import connections, hls
    h = _held.pop(client_id, None) if client_id else None
    if h and h[0].url != url:               # held for a screen that went to another channel instead
        h[0].let_go()
    connections.after_stop(client_id, url)
    connections.check(source_id, url, mode or config.get("video_mode", "copy"), audio, client_id or "")
    # the same channel again (playing further behind live for captions, a reconnect): join its running
    # tuner before letting go, so the provider connection - and what the tuner kept - isn't lost
    old = _clients.get(client_id) if client_id else None
    again = old is not None and old.tuner.url == url and not old.tuner.ended
    if client_id:
        if again:
            _clients.pop(client_id, None)
        streaming.stop_client(client_id)
        hls.stop_client(client_id)
    try:
        sub = join(url, mode, audio, buffer, name=client_id or name, source_id=source_id)
    finally:
        if again:
            old.close(wait=False)
    if client_id:
        _clients[client_id] = sub
    return sub


def sharing(url, mode=None, audio="aac", any_mode=False):
    """True when a reader of `url` would join a running tuner (no new connection)."""
    mode = mode or config.get("video_mode", "copy")
    with _lock:
        return _find(url, mode, audio, any_mode) is not None


def evict(url, mode, audio):
    """Stop the running tuner for `url` (to free its connection). True if there was one."""
    with _lock:
        ts = [t for t in _tuners if t.url == url and t.mode == mode and t.audio == audio and not t.ended]
    for t in ts:
        t.stop()
    return bool(ts)


def running():
    """The tuners running now and who is on each (Setup › Tuners). Holds the provider login in `url`:
    never send it to a page as is."""
    with _lock:
        ts = [t for t in _tuners if not t.ended]
    out = []
    for t in ts:
        with t.lock:
            names = [s.name for s in t.subs]
        out.append({"id": id(t), "url": t.url, "mode": t.mode, "audio": t.audio, "viewers": names, "source_id": t.source_id,
                    "since": int(t.started), "mb": round(t.sent / 1e6, 1)})
    return out


def stop_id(tid):
    """Force-stop one running tuner (Setup › Tuners › Stop): every viewer on it gets the end of the stream.
    `tid` is the id from running(). True if it was running."""
    with _lock:
        ts = [t for t in _tuners if id(t) == tid and not t.ended]
    for t in ts:
        t.stop()
    return bool(ts)


def hold(client_id, secs=8):
    """The screen is about to re-open its channel (to play further behind live): keep its tuner, and the
    provider connection, for `secs` after it lets go."""
    sub = _clients.get(client_id)
    if sub is not None and not sub.tuner.ended:
        _held[client_id] = (sub.tuner, time.monotonic() + secs)
        return True
    return False


def client_sub(client_id):
    """The tuner reader a screen's live TS stream comes from, or None."""
    sub = _clients.get(client_id)
    return sub if sub is not None and not sub.tuner.ended else None


def _captions():
    from . import captions
    return captions


def stop_client(client_id):
    """Take a screen off its channel (before it tunes another: Xtream allows few connections)."""
    sub = _clients.pop(client_id, None)
    if sub:
        sub.close()


async def watch(url, client_id=None, mode=None, audio="aac", request=None, buffer=None, source_id=None, sub=None):
    """Async generator of MPEG-TS bytes from a shared tuner. Waiting never holds a worker thread.
    `sub`: already joined (open_for, so the endpoint could refuse a full playlist with a 503). Without
    it, it joins here and ends at once (no data) if the playlist has no connection left."""
    import asyncio

    import anyio

    from .connections import Full
    if sub is None:
        try:
            sub = await anyio.to_thread.run_sync(lambda: open_for(url, client_id, mode, audio, buffer, source_id))
        except Full as e:
            log.info("Not started: %s", e)
            return
    ready = asyncio.Event()
    sub.wake = _waker(asyncio.get_running_loop(), ready)
    last_check = time.monotonic()
    try:
        while True:
            try:
                chunk = sub.q.get_nowait()
            except queue.Empty:
                ready.clear()
                try:                # re-check after clearing, so a chunk put just before isn't missed
                    chunk = sub.q.get_nowait()
                except queue.Empty:
                    try:
                        await asyncio.wait_for(ready.wait(), 2)
                    except asyncio.TimeoutError:
                        if request is not None and await request.is_disconnected():
                            break
                    continue
            if not chunk:
                break
            yield chunk
            if request is not None and time.monotonic() - last_check > 2:
                last_check = time.monotonic()
                if await request.is_disconnected():
                    break
    finally:
        if client_id and _clients.get(client_id) is sub:
            _clients.pop(client_id, None)
        sub.close(wait=False)
