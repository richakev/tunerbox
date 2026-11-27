"""Shared tuners, with a small Python script writing MPEG-TS standing in for ffmpeg."""
import asyncio
import sys
import time

import pytest

from tunerbox import streaming, tuner

# Fake ffmpeg: PAT, PMT, then "GOPs" of a video keyframe + 20 video packets + 5 audio packets (audio
# packets carry the random-access flag too, as ffmpeg's do: they must not count as keyframes).
# Every video packet's payload starts with its GOP number. Written in 1000-byte pieces, so reads
# never line up with packets.
FAKE = r'''
import sys, time
V, A, PMT = 0x100, 0x101, 0x1000
def pkt(pid, payload=b"", start=False, rai=False):
    b1 = (0x40 if start else 0) | (pid >> 8)
    if rai:
        return (bytes([0x47, b1, pid & 0xFF, 0x30, 1, 0x40]) + payload).ljust(188, b"\xff")
    return (bytes([0x47, b1, pid & 0xFF, 0x10]) + payload).ljust(188, b"\xff")
PAT = pkt(0, bytes([0, 0, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xF0, 0x00, 0, 0, 0, 0]), start=True)
PMTP = pkt(PMT, bytes([0, 2, 0xB0, 23, 0, 1, 0xC1, 0, 0, 0xE1, 0x00, 0xF0, 0,
                      0x1B, 0xE1, 0x00, 0xF0, 0, 0x0F, 0xE1, 0x01, 0xF0, 0, 0, 0, 0, 0]), start=True)
gops, delay = int(sys.argv[1]), float(sys.argv[2])
out = sys.stdout.buffer
for g in range(gops):
    data = (PAT + PMTP + pkt(V, g.to_bytes(4, "big"), start=True, rai=True)
            + b"".join(pkt(V, g.to_bytes(4, "big")) for _ in range(20))
            + b"".join(pkt(A, b"a", start=True, rai=True) for _ in range(5)))
    for i in range(0, len(data), 1000):
        out.write(data[i:i + 1000]); out.flush()
    time.sleep(delay)
time.sleep(float(sys.argv[3]) if len(sys.argv) > 3 else 0)
'''

PASS = r"""
import sys
i, o = sys.stdin.buffer, sys.stdout.buffer
while True:
    d = i.read1(65536)
    if not d:
        break
    o.write(d); o.flush()
"""


@pytest.fixture
def fake(monkeypatch):
    """Use the fake ffmpeg; returns the list of commands started."""
    started = []

    def cmd(url, mode=None, audio="aac", buffer=0):
        gops, delay, linger = url.split(":")[1:]
        started.append((url, mode, audio))
        return [sys.executable, "-c", FAKE, gops, delay, linger]
    monkeypatch.setattr(tuner, "live_cmd", cmd)

    def derive(mode, audio):                        # a converting tuner: passes its input straight on
        started.append(("derive", mode, audio))
        return [sys.executable, "-c", PASS]
    monkeypatch.setattr(tuner, "derive_cmd", derive)
    yield started
    for t in list(tuner._tuners):
        tuner.kill(t.proc)


def _read(sub, n_bytes, timeout=5):
    out = b""
    end = time.monotonic() + timeout
    while len(out) < n_bytes and time.monotonic() < end:
        try:
            chunk = sub.get(timeout=0.5)
        except Exception:
            continue
        if not chunk:
            break
        out += chunk
    return out


def _packets(data):
    assert len(data) % 188 == 0
    assert all(data[i] == 0x47 for i in range(0, len(data), 188))
    return [data[i:i + 188] for i in range(0, len(data), 188)]


def _pid(p):
    return ((p[1] & 0x1F) << 8) | p[2]


def test_two_viewers_share_one_connection(fake):
    a = tuner.join("fake:1000:0.05:0", "copy", name="tv1")
    b = tuner.join("fake:1000:0.05:0", "copy", name="tv2")
    assert len(fake) == 1 and a.tuner is b.tuner
    assert _read(a, 20000) and _read(b, 20000)
    proc = a.tuner.proc
    a.close()
    assert proc.poll() is None                      # tv2 is still watching
    b.close()
    assert proc.poll() is not None                  # last one out stops it
    assert not tuner._tuners


def test_a_screen_joining_late_starts_at_a_keyframe(fake):
    a = tuner.join("fake:1000:0.05:0", "copy")
    _read(a, 60000)                                 # a few GOPs in
    b = tuner.join("fake:1000:0.05:0", "copy")
    pk = _packets(_read(b, 188 * 3))[:3]
    assert [_pid(p) for p in pk] == [0, 0x1000, 0x100]          # PAT, PMT, then video...
    assert pk[2][1] & 0x40 and pk[2][5] & 0x40                  # ...that is a keyframe
    gop = int.from_bytes(pk[2][6:10], "big")
    assert gop >= 2                                             # the newest one, not the first
    a.close(); b.close()


def test_output_stays_packet_aligned_and_in_order(fake):
    a = tuner.join("fake:1000:0.01:0", "copy")
    seen = [int.from_bytes(p[4:8], "big") for p in _packets(_read(a, 188 * 600))
            if _pid(p) == 0x100 and not p[1] & 0x40]
    assert seen == sorted(seen) and seen[-1] > seen[0]
    a.close()


def test_stream_cache_joiner_gets_older_keyframe(fake):
    a = tuner.join("fake:1000:0.1:0", "copy", buffer=0)
    _read(a, 188 * 27 * 12)                         # about a second of GOPs
    b = tuner.join("fake:1000:0.1:0", "copy", buffer=0)
    c = tuner.join("fake:1000:0.1:0", "copy", buffer=5)
    gb = int.from_bytes(_packets(_read(b, 188 * 3))[2][6:10], "big")
    gc = int.from_bytes(_packets(_read(c, 188 * 3))[2][6:10], "big")
    assert gc < gb                                  # the 5-second cache starts further back
    for s in (a, b, c):
        s.close()


def test_modes_audio_and_the_dvr(fake):
    url = "fake:1000:0.05:0"
    copy = tuner.join(url, "copy")
    h264 = tuner.join(url, "h264")                  # a TV that needs re-encoding: its own tuner,
    assert copy.tuner is not h264.tuner and len(fake) == 2      # converting the copy one's stream
    assert h264.tuner.parent.tuner is copy.tuner and fake[1] == ("derive", "h264", "aac")
    dvr = tuner.join(url, "roku720", any_mode=True)  # the DVR takes any picture going
    assert dvr.tuner in (copy.tuner, h264.tuner) and len(fake) == 2
    plex = tuner.join(url, "copy", audio="copy")    # Plex takes AAC rather than a second connection
    assert plex.tuner is copy.tuner and len(fake) == 2
    aac = tuner.join("fake:1000:0.05:1", "copy", audio="copy")
    tv = tuner.join("fake:1000:0.05:1", "copy")     # but a TV can't play the original audio
    assert tv.tuner is not aac.tuner
    for s in (copy, h264, dvr, plex, aac, tv):
        s.close()


def test_feed_ending_ends_every_reader(fake):
    a = tuner.join("fake:3:0:0", "copy")
    b = tuner.join("fake:3:0:0", "copy")
    for s in (a, b):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if s.get(timeout=5) == b"":
                break
        else:
            pytest.fail("no end of stream")
    time.sleep(0.2)
    assert not tuner._tuners


def test_new_channel_on_the_same_screen_releases_the_old_one(fake):
    async def main():
        g1 = streaming.ffmpeg_stream("fake:1000:0.05:0", "screen1")
        assert await g1.__anext__()
        first = tuner._clients["screen1"].tuner
        g2 = streaming.ffmpeg_stream("fake:1000:0.05:1", "screen1")
        assert await g2.__anext__()
        assert first.proc.poll() is not None        # stopped before the new one started
        await g1.aclose(); await g2.aclose()
    asyncio.run(main())
    time.sleep(0.5)
    assert "screen1" not in tuner._clients


def test_a_reader_that_falls_behind_is_dropped(fake, monkeypatch):
    monkeypatch.setattr(tuner, "QUEUE_CHUNKS", 50)
    a = tuner.join("fake:1000:0.01:0", "copy", name="slow")
    b = tuner.join("fake:1000:0.01:0", "copy", name="fast")
    end = time.monotonic() + 5
    while a in a.tuner.subs and time.monotonic() < end:
        b.get(timeout=1)
    assert a not in a.tuner.subs and b in b.tuner.subs
    b.close()


def test_a_slow_reader_alone_on_its_tuner_is_waited_for(fake, monkeypatch):
    monkeypatch.setattr(tuner, "QUEUE_CHUNKS", 5)
    a = tuner.join("fake:1000:0.001:0", "copy", name="alone")
    time.sleep(1.5)                                 # far more than 5 chunks produced meanwhile
    assert a in a.tuner.subs                        # held the tuner back instead of being dropped
    assert _read(a, 188 * 300)
    a.close()


def test_recording_shares_the_tuner_a_tv_is_on(fake, tmp_path):
    from tunerbox import dvr
    tv = tuner.join("fake:1000:0.02:0", "copy", name="tv")
    _read(tv, 30000)
    rec = tuner.join("fake:1000:0.02:0", "copy", any_mode=True, name="dvr:1")
    assert rec.tuner is tv.tuner and len(fake) == 1
    ts = tmp_path / "Show.ts"
    dvr._capture(rec, ts, time.time() + 1)
    rec.close()
    pk = _packets(ts.read_bytes())
    assert len(pk) > 100 and _pid(pk[0]) == 0 and pk[2][5] & 0x40     # starts clean, on a keyframe
    assert tv.tuner.proc.poll() is None                                # the TV is still watching
    tv.close()


def test_hls_session_that_cant_start_leaves_no_folder(monkeypatch, tmp_path):
    from tunerbox import hls

    def fail(*a, **k):
        raise OSError("ffmpeg not found")
    monkeypatch.setattr(hls, "ROOT", tmp_path)
    monkeypatch.setattr(tuner, "join", fail)
    with pytest.raises(OSError):
        hls.start("http://x/1")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- connection limits (connections.py)
def _account(limit, kind="xtream", own=0):
    """A playlist allowing `limit` connections (Xtream's max_connections, or an HDHomeRun's tuners)."""
    from tunerbox import db
    sid = db.run("INSERT INTO sources(name, type, url, max_conn) VALUES ('Prov', ?, 'http://prov', ?)", (kind, own))
    db.set_meta(f"{'xtream_max' if kind == 'xtream' else 'hdhr_tuners'}_{sid}", limit)
    return sid


def test_full_account_refuses_a_second_channel(fake):
    from tunerbox.connections import Full
    sid = _account(1)
    a = tuner.join("fake:1000:0.05:0", "copy", name="tv1", source_id=sid)
    b = tuner.join("fake:1000:0.05:0", "copy", name="tv2", source_id=sid)     # same channel: shared, free
    with pytest.raises(Full, match="Prov allows 1 connection"):
        tuner.join("fake:1000:0.05:1", "copy", name="tv3", source_id=sid)
    assert len(fake) == 1
    tuner.join("fake:1000:0.05:2", "copy", name="tv3", source_id=None).close()  # another playlist: no limit
    a.close(); b.close()


def test_a_screens_own_channel_doesnt_count_against_it(fake):
    from tunerbox import connections
    sid = _account(1)
    a = tuner.join("fake:1000:0.05:0", "copy", name="tv1", source_id=sid)
    connections.check(sid, "fake:1000:0.05:1", "copy", client="tv1")            # tv1 gives it up when it tunes
    with pytest.raises(connections.Full):
        connections.check(sid, "fake:1000:0.05:1", "copy", client="tv2")
    a.close()


def test_own_limit_overrides_what_the_provider_says(fake):
    from tunerbox import connections, db
    sid = _account(1, own=3)
    assert connections.limit_of(db.one("SELECT * FROM sources WHERE id=?", (sid,))) == 3
    subs = [tuner.join(f"fake:1000:0.05:{i}", "copy", name=f"tv{i}", source_id=sid) for i in range(3)]
    with pytest.raises(connections.Full):
        tuner.join("fake:1000:0.05:9", "copy", name="tv9", source_id=sid)
    for s in subs:
        s.close()


def test_recording_takes_the_connection_from_live_tv(fake):
    sid = _account(1, "hdhomerun")
    tv = tuner.join("fake:1000:0.05:0", "copy", name="tv1", source_id=sid)
    _read(tv, 20000)
    rec = tuner.join("fake:1000:0.05:1", "copy", any_mode=True, name="dvr:7", source_id=sid, take=True)
    assert rec.tuner is not tv.tuner and tv.tuner.ended                      # live TV stopped
    try:                                            # ffmpeg is killed in the background (taskkill is slow on Windows)
        tv.tuner.proc.wait(5)
    except Exception:
        pytest.fail("live TV's ffmpeg still running")
    end = time.monotonic() + 5
    while time.monotonic() < end and tv.get(timeout=5) != b"":
        pass                                        # the screen sees the end of its stream
    assert [t["viewers"] for t in tuner.running()] == [["dvr:7"]]
    rec.close()


def test_recording_never_takes_another_recordings_connection(fake):
    from tunerbox.connections import Full
    sid = _account(1)
    first = tuner.join("fake:1000:0.05:0", "copy", any_mode=True, name="dvr:1", source_id=sid, take=True)
    with pytest.raises(Full):
        tuner.join("fake:1000:0.05:1", "copy", any_mode=True, name="dvr:2", source_id=sid, take=True)
    assert first.tuner.proc.poll() is None
    first.close()


def test_api_refuses_a_full_playlist_and_says_whats_using_it(fake):
    from fastapi.testclient import TestClient

    from tunerbox import api, db, guide
    sid = _account(1)
    for n, url in ((8, "fake:1000:0.05:0"), (9, "fake:1000:0.05:1")):
        db.run("INSERT INTO streams(key, source_id, name, url) VALUES (?, ?, ?, ?)", (f"k{n}", sid, f"Ch {n}", url))
        db.run("INSERT INTO lineup(number, name) VALUES (?, ?)", (n, f"Ch {n}"))
        db.run("INSERT INTO mappings(number, stream_key, mode) VALUES (?, ?, 'manual')", (n, f"k{n}"))
    guide.invalidate()
    rec = tuner.join("fake:1000:0.05:0", "copy", any_mode=True, name="dvr:5", source_id=sid)
    c = TestClient(api.app)
    r = c.get("/api/stream/9?client=tv2")
    assert r.status_code == 503 and r.headers["X-HDHomeRun-Error"].startswith("805")
    busy = c.get("/api/busy?number=9&client=tv2")
    full = busy.json()["full"]
    assert full[0]["source"] == "Prov" and full[0]["limit"] == 1
    assert full[0]["using"][0]["number"] == 8 and full[0]["using"][0]["viewers"] == [{"kind": "dvr", "name": "Recording"}]
    assert "fake:" not in busy.text                                     # never a provider address
    assert c.get("/api/busy?number=8&client=tv2").json()["full"] == []  # channel 8 is on: it can be watched
    rec.close()


def test_two_screens_tuning_at_once_cant_both_take_the_last_connection(fake, monkeypatch):
    import threading

    from tunerbox import connections
    sid = _account(1)
    real = connections.check
    gate = threading.Barrier(2, timeout=5)

    def slow_check(*a, **k):                    # both screens pass the check before either starts a tuner
        real(*a, **k)
        try:
            gate.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
    monkeypatch.setattr(connections, "check", slow_check)
    got, refused = [], []

    def tune(i):
        try:
            got.append(tuner.join(f"fake:1000:0.05:{i}", "copy", name=f"tv{i}", source_id=sid))
        except connections.Full:
            refused.append(i)
    ts = [threading.Thread(target=tune, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert len(got) == 1 and len(refused) == 1 and len(fake) == 1
    for s in got:
        s.close()


def test_screen_stopped_for_a_recording_cant_take_the_connection_back(fake, monkeypatch):
    import threading

    from tunerbox import connections
    sid = _account(1)
    tv = tuner.join("fake:1000:0.05:0", "copy", name="tv1", source_id=sid)
    real_make_room = connections.make_room
    retried = []

    def make_room(*a, **k):                     # the stopped screen retunes the moment its channel stops
        ok = real_make_room(*a, **k)
        t = threading.Thread(target=lambda: retried.append(_try_join(sid)))
        t.start()
        t.join(0.5)                             # it must wait for the recording to start its tuner
        return ok
    monkeypatch.setattr(connections, "make_room", make_room)
    rec = tuner.join("fake:1000:0.05:1", "copy", any_mode=True, name="dvr:7", source_id=sid, take=True)
    end = time.monotonic() + 5
    while not retried and time.monotonic() < end:
        time.sleep(0.05)
    assert retried == ["full"]
    assert [t["viewers"] for t in tuner.running()] == [["dvr:7"]]
    rec.close()
    tv.close()


def _try_join(sid):
    from tunerbox import connections
    try:
        tuner.join("fake:1000:0.05:2", "copy", name="tv1", source_id=sid).close()
        return "joined"
    except connections.Full:
        return "full"


def test_hls_channel_stops_the_screens_on_demand_stream(fake, monkeypatch, tmp_path):
    from tunerbox import connections, hls
    sid = _account(1)
    monkeypatch.setattr(hls, "ROOT", tmp_path)
    proc = streaming.start_cmd([sys.executable, "-c", "import time; time.sleep(30)"], "tv1", "http://prov/movie.mkv",
                               "A movie", sid)
    assert connections.used(sid) == 1
    monkeypatch.setattr(hls, "_cmd", lambda *a, **k: [sys.executable, "-c", "import sys; sys.stdin.read()"])
    s = hls.start("fake:1000:0.05:0", "tv1", "copy", 0, source_id=sid)
    assert streaming.playing() == [] and proc.wait(5) is not None   # the movie's connection was given up
    assert connections.used(sid) == 1                                 # only the channel's tuner
    hls._stop(s)


def test_busy_uses_the_mode_the_screen_asked_for(fake):
    from tunerbox import connections
    sid = _account(1)
    a = tuner.join("fake:1000:0.05:0", "copy", name="tv1", source_id=sid)
    row = {"url": "fake:1000:0.05:0", "source_id": sid, "video_mode": "copy"}
    assert connections.busy([row], "tv2") == []                           # copy is already on: free to watch
    assert connections.busy([row], "tv2", mode="roku720") == []           # 720p converts that one: free too
    full = connections.busy([{**row, "url": "fake:1000:0.05:1"}], "tv2")  # another channel needs its own
    assert full and full[0]["limit"] == 1
    a.close()


def test_joining_a_running_tuner_is_decided_and_done_in_one_step(fake, monkeypatch):
    from tunerbox import connections
    sid = _account(1)
    a = tuner.join("fake:1000:0.05:0", "copy", name="tv1", source_id=sid)
    real = connections.check

    def no_check(*a_, **k):     # sharing is settled under tuner._lock, not by a check that can go stale
        raise AssertionError("checked")
    monkeypatch.setattr(connections, "check", no_check)
    b = tuner.join("fake:1000:0.05:0", "copy", name="tv2", source_id=sid)
    assert b.tuner is a.tuner and len(fake) == 1
    monkeypatch.setattr(connections, "check", real)
    a.close()
    b.close()                   # the tuner stops: the next viewer needs a counted, new connection
    rec = tuner.join("fake:1000:0.05:1", "copy", any_mode=True, name="dvr:1", source_id=sid)
    with pytest.raises(connections.Full):
        tuner.join("fake:1000:0.05:0", "copy", name="tv3", source_id=sid)
    rec.close()


def test_full_playlist_doesnt_cost_the_screen_its_channel(fake):
    from tunerbox import connections
    sid = _account(1)
    rec = tuner.join("fake:1000:0.05:0", "copy", any_mode=True, name="dvr:1", source_id=sid)
    tv = tuner.open_for("fake:1000:0.05:1", "tv1", "copy", source_id=None)     # tv1 on another playlist
    with pytest.raises(connections.Full):
        tuner.open_for("fake:1000:0.05:2", "tv1", "copy", source_id=sid)
    assert tuner._clients["tv1"] is tv and not tv.tuner.ended                  # still watching
    tuner.stop_client("tv1")
    rec.close()


def test_stopping_a_tuner_for_a_recording_doesnt_wait_for_ffmpeg(fake, monkeypatch):
    sid = _account(1)
    tv = tuner.join("fake:1000:0.05:0", "copy", name="tv1", source_id=sid)
    slow = []
    monkeypatch.setattr(tuner, "kill", lambda p: (time.sleep(1), slow.append(p), p.kill()))
    t0 = time.monotonic()
    tv.tuner.stop()
    assert time.monotonic() - t0 < 0.5 and tuner.running() == []
    end = time.monotonic() + 3
    while not slow and time.monotonic() < end:
        time.sleep(0.05)
    assert slow                                         # ffmpeg still stopped, in the background


def _end_line(caplog, url):
    lines = [r for r in caplog.records if r.getMessage().startswith("Tuner end") and url in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    return lines[0]


def test_log_says_the_provider_ended_the_stream(fake, caplog):
    with caplog.at_level("INFO", logger="tunerbox.tuner"):
        a = tuner.join("fake:3:0:0.1", "copy", name="tv1")
        _read(a, 10 ** 9)
        time.sleep(0.5)
    r = _end_line(caplog, "fake:3:0:0.1")
    assert r.levelname == "WARNING" and "provider's stream ended" in r.getMessage()
    assert "ffmpeg exit 0" in r.getMessage()


def test_log_says_the_viewer_left(fake, caplog):
    with caplog.at_level("INFO", logger="tunerbox.tuner"):
        a = tuner.join("fake:1000:0.05:2", "copy", name="tv1")
        assert _read(a, 20000)
        a.close()
        time.sleep(0.5)
    r = _end_line(caplog, "fake:1000:0.05:2")
    assert r.levelname == "INFO" and "last viewer left: tv1 disconnected" in r.getMessage()


def test_log_says_a_recording_took_the_connection(fake, caplog):
    with caplog.at_level("INFO", logger="tunerbox.tuner"):
        a = tuner.join("fake:1000:0.05:3", "copy", name="tv1")
        assert _read(a, 20000)
        assert tuner.evict("fake:1000:0.05:3", "copy", "aac")
        _read(a, 10 ** 9)
        time.sleep(0.5)
    assert "for a recording" in _end_line(caplog, "fake:1000:0.05:3").getMessage()


def test_a_second_format_converts_the_open_stream_instead_of_a_new_connection(fake):
    sid = _account(1)
    tv = tuner.join("fake:1000:0.05:0", "copy", name="browser", source_id=sid)
    roku = tuner.join("fake:1000:0.05:0", "roku720", name="roku", source_id=sid)   # 1 connection: still fine
    assert [f[0] for f in fake] == ["fake:1000:0.05:0", "derive"]
    assert roku.tuner.parent.tuner is tv.tuner and roku.tuner.source_id is None
    assert _packets(_read(roku, 20000))             # the converted stream plays
    parent = tv.tuner
    tv.close()                                      # the browser leaves: the Roku keeps the stream
    assert parent.proc.poll() is None and _read(roku, 5000)
    roku.close()                                    # the Roku leaves too: both stop
    time.sleep(0.5)
    assert parent.proc.poll() is not None and not tuner._tuners


def test_ending_stream_ends_its_converted_copy(fake):
    tv = tuner.join("fake:30:0.01:0", "copy", name="browser")
    roku = tuner.join("fake:30:0.01:0", "roku720", name="roku")
    data, chunk = b"", None
    end = time.monotonic() + 5
    while time.monotonic() < end:
        chunk = roku.get(timeout=1)
        if not chunk:
            break
        data += chunk
    assert data and chunk == b""                    # all of it, then the end
    tv.close(); roku.close()


def test_from_a_converted_stream_only_when_no_connection_is_left(fake):
    from tunerbox.connections import Full
    roku = tuner.join("fake:1000:0.05:0", "roku720", name="roku")     # no limit: a new connection is better
    tv = tuner.join("fake:1000:0.05:0", "copy", name="browser")
    assert tv.tuner.parent is None and len(fake) == 2
    tv.close(); roku.close()
    sid = _account(1)
    roku = tuner.join("fake:1000:0.05:1", "roku720", name="roku", source_id=sid)
    tv = tuner.join("fake:1000:0.05:1", "copy", name="browser", source_id=sid)    # full: convert the 720p
    assert tv.tuner.parent.tuner is roku.tuner
    with pytest.raises(Full):                       # another channel still needs a connection of its own
        tuner.join("fake:1000:0.05:2", "copy", name="tv3", source_id=sid)
    tv.close(); roku.close()


def test_tuners_list_shows_a_converted_copy_as_viewers_of_the_original(fake):
    sid = _account(1)
    tv = tuner.join("fake:1000:0.05:0", "copy", name="browser", source_id=sid)
    roku = tuner.join("fake:1000:0.05:0", "roku720", name="roku", source_id=sid)
    run = tuner.running()
    assert len(run) == 1                                  # one connection, listed once
    assert run[0]["source_id"] == sid and sorted(run[0]["viewers"]) == ["browser", "roku"]
    from tunerbox import connections
    assert connections.used(sid) == 1
    assert connections.used(sid, "browser") == 1          # the browser leaving still leaves the Roku on it
    tv.close()
    assert tuner.running()[0]["viewers"] == ["roku"]
    roku.close()


# ------------------------------------------------------------ moving a channel to free a connection
CNN_A, CNN_B, ESPN_A = "fake:1000:0.05:0", "fake:1000:0.05:1", "fake:1000:0.05:2"


@pytest.fixture
def two_playlists(fake):
    """A and B each allow one connection. CNN is on both (A first); ESPN is only on A."""
    import json

    from tunerbox import db, guide
    a, b = _account(1), _account(1)
    for key, sid, url in (("cnn-a", a, CNN_A), ("cnn-b", b, CNN_B), ("espn-a", a, ESPN_A)):
        db.run("INSERT INTO streams(key, source_id, name, url) VALUES (?, ?, ?, ?)", (key, sid, key, url))
    for num, name, main, alts in ((1, "CNN", "cnn-a", ["cnn-b"]), (2, "ESPN", "espn-a", [])):
        db.run("INSERT INTO lineup(number, name) VALUES (?, ?)", (num, name))
        db.run("INSERT INTO mappings(number, stream_key, alt_keys, mode) VALUES (?, ?, ?, 'manual')",
               (num, main, json.dumps(alts)))
    guide.invalidate()
    return a, b


def test_full_playlist_moves_another_screens_channel_to_a_playlist_with_room(two_playlists, fake):
    a, b = two_playlists
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    assert _read(tv1, 20000)
    t, old = tv1.tuner, tv1.tuner.proc
    tv2 = tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)         # A is full: CNN moves to B
    assert t.url == CNN_A and t.playing == CNN_B and t.source_id == b and t.moves == 1 and not t.ended
    assert [u for u, *_ in fake] == [CNN_A, CNN_B, ESPN_A]
    old.wait(5)                                                     # A's copy is closed
    assert _read(tv1, 20000) and _read(tv2, 20000)                  # both screens play
    assert sorted((r["source_id"], r["viewers"]) for r in tuner.running()) == [(a, ["tv2"]), (b, ["tv1"])]
    tv1.close(); tv2.close()


def test_a_moved_channel_starts_joiners_on_the_new_copy(two_playlists, fake):
    a, b = two_playlists
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    assert _read(tv1, 20000)
    tv2 = tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)
    tv3 = tuner.open_for(CNN_B, "tv3", "copy", source_id=b)          # joins the moved tuner: no new connection
    assert tv3.tuner is tv1.tuner and len(fake) == 3
    pk = _packets(_read(tv3, 188 * 30))
    assert _pid(pk[0]) == 0                                         # PAT first, from the new copy
    for s in (tv1, tv2, tv3):
        s.close()


def test_recording_moves_live_tv_instead_of_stopping_it(two_playlists, fake):
    a, b = two_playlists
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    assert _read(tv1, 20000)
    rec = tuner.join(ESPN_A, "copy", any_mode=True, name="dvr:7", source_id=a, take=True)
    assert not tv1.tuner.ended and tv1.tuner.source_id == b and rec.tuner.source_id == a
    rec.close(); tv1.close()


def test_a_channel_a_recording_is_on_never_moves(two_playlists):
    from tunerbox.connections import Full
    a, b = two_playlists
    rec = tuner.join(CNN_A, "copy", any_mode=True, name="dvr:3", source_id=a, take=True)
    tv1 = tuner.join(CNN_A, "copy", name="tv1", source_id=a)
    with pytest.raises(Full):
        tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)
    assert rec.tuner.playing == CNN_A and rec.tuner.moves == 0
    rec.close(); tv1.close()


def test_no_move_when_the_other_playlist_is_full_too(two_playlists):
    from tunerbox.connections import Full
    a, b = two_playlists
    other = tuner.join("fake:1000:0.05:3", "copy", name="tv9", source_id=b)     # B's one connection
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    with pytest.raises(Full):
        tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)
    assert tv1.tuner.playing == CNN_A
    other.close(); tv1.close()


def test_no_move_when_turned_off(two_playlists):
    from tunerbox import config
    from tunerbox.connections import Full
    a, b = two_playlists
    config.update({"conn_auto_move": False})
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    with pytest.raises(Full):
        tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)
    assert tv1.tuner.playing == CNN_A
    tv1.close()


def test_a_copy_that_sends_nothing_leaves_the_channel_where_it_was(two_playlists, fake, monkeypatch):
    from tunerbox import db
    from tunerbox.connections import Full
    a, b = two_playlists
    monkeypatch.setattr(tuner, "MOVE_WAIT", 0.5)
    db.run("UPDATE streams SET url='fake:0:0:10' WHERE key='cnn-b'")          # B's CNN never sends anything
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    assert _read(tv1, 20000)
    with pytest.raises(Full):
        tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)
    t = tv1.tuner
    assert t.playing == CNN_A and t.source_id == a and t.moves == 0 and not t.ended
    assert _read(tv1, 20000)                                        # still playing A's copy
    tv1.close()


def test_reading_file_details_never_moves_a_channel(two_playlists):
    from tunerbox import connections
    a, b = two_playlists
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    with pytest.raises(connections.Full):
        connections.check(a, move=False)
    assert tv1.tuner.playing == CNN_A
    tv1.close()


def test_a_moved_channel_is_still_found_by_its_original_address(two_playlists, fake):
    """The guide (and a recording) still ask for A's address: they join the moved tuner, no new stream."""
    a, b = two_playlists
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    assert _read(tv1, 20000)
    tv2 = tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)         # CNN moves to B
    assert tuner.sharing(CNN_A, "copy") and tuner.sharing(CNN_B, "copy")
    tv3 = tuner.open_for(CNN_A, "tv3", "copy", source_id=a)          # A is full, but this joins CNN on B
    rec = tuner.join(CNN_A, "copy", any_mode=True, name="dvr:5", source_id=a, take=True)
    assert tv3.tuner is tv1.tuner and rec.tuner is tv1.tuner and len(fake) == 3
    assert [r["url"] for r in tuner.running() if r["source_id"] == b] == [CNN_A]     # Setup › Tuners › Stop holds A's
    for s in (tv1, tv2, tv3, rec):
        s.close()


def test_other_tunes_dont_wait_while_a_move_is_tried(two_playlists, fake, monkeypatch):
    """Trying another playlist's copy can take seconds: it's done without connections.admit held."""
    import threading

    from tunerbox import connections
    a, b = two_playlists
    gate, held = threading.Event(), []
    real = tuner.Tuner.move

    def slow(self, url, source_id, wait=None):
        held.append(connections._held())
        free = connections.admit.acquire(timeout=2)                 # another tune could take it
        if free:
            connections.admit.release()
        held.append(free)
        assert connections.used(b) == 1                             # B's room is held for this try
        gate.set()
        return real(self, url, source_id, wait)
    monkeypatch.setattr(tuner.Tuner, "move", slow)
    tv1 = tuner.open_for(CNN_A, "tv1", "copy", source_id=a)
    assert _read(tv1, 20000)
    tv2 = tuner.open_for(ESPN_A, "tv2", "copy", source_id=a)
    assert gate.is_set() and held == [False, True]
    assert connections._reserved == {} and connections._moving == set()
    assert connections.used(b) == 1 and tv1.tuner.source_id == b
    tv1.close(); tv2.close()


def test_the_old_copys_last_data_is_dropped_after_a_move(fake):
    """A chunk the old ffmpeg's reader had in hand when the move swapped: its PAT/PMT and video PID
    don't come back, and its end doesn't end the tuner."""
    tv1 = tuner.join("fake:1000:0.05:0", "copy", name="tv1")
    assert _read(tv1, 20000)
    t = tv1.tuner
    old = t.proc
    data = None
    while not data:
        with t.lock:
            data = next((c[2] for c in reversed(t.chunks) if c[2][1] & 0x40 and _pid(c[2]) == 0), None)
        time.sleep(0.05)
    assert t.move("fake:1000:0.05:1", None)
    with t.lock:
        t.psi, t.pmt_pids, t.video_pid = {}, set(), None             # as if the new copy sent no PAT yet
    assert t._feed(data[:188], old) is False
    assert t.psi == {} and t.video_pid is None
    old.wait(5)
    time.sleep(0.3)
    assert not t.ended and _read(tv1, 20000)
    tv1.close()
