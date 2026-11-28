"""Server side of pausing live TV (hls.py): off by default, kept under the data folder, cut to the disk's
room, and segment names past seg99999."""
from collections import namedtuple

from tunerbox import config, hls

Usage = namedtuple("Usage", "total used free")


def test_off_by_default():
    assert config.DEFAULTS["timeshift_minutes"] == 0


def test_segment_names_past_99999_are_served():
    assert hls.SAFE_FILE.match("seg99999.ts") and hls.SAFE_FILE.match("seg100000.ts")
    assert not hls.SAFE_FILE.match("seg1.ts") and not hls.SAFE_FILE.match("../seg00001.ts")


def test_timeshift_sessions_live_under_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    assert hls.ts_root() == tmp_path / "timeshift-hls"
    assert not str(hls.ts_root()).startswith(str(hls.ROOT))


def test_minutes_are_cut_to_the_disks_room(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    mb = 1024 * 1024
    monkeypatch.setattr(hls.shutil, "disk_usage", lambda p: Usage(0, 0, (hls.TS_SPARE_MB + 400) * mb))
    monkeypatch.setattr(hls, "_sessions", {})
    assert hls._ts_room(30) == 10                       # 400 MB spare: 10 minutes at 40 MB a minute
    assert hls._ts_room(5) == 5
    assert hls._ts_room(0) == 0
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(hls, "_sessions", {"a": {"timeshift": 8, "dir": other}})
    assert hls._ts_room(30) == 2                        # another screen's minutes still to write take the rest
    (other / "seg00000.ts").write_bytes(b"x" * 4 * hls.TS_MB_PER_MIN * mb)
    assert hls._ts_room(30) == 6                        # what it has written is already out of `free`
    monkeypatch.setattr(hls.shutil, "disk_usage", lambda p: Usage(0, 0, 100 * mb))
    assert hls._ts_room(30) == 0                        # nearly full: the usual short playlist


def test_cleanup_clears_both_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(hls, "ROOT", tmp_path / "tmp")
    monkeypatch.setattr(hls, "_sessions", {})
    for d in (hls.ROOT / "old1", hls.ts_root() / "old2"):
        d.mkdir(parents=True)
    hls.cleanup_stale()
    assert not any(hls.ROOT.iterdir()) and not any(hls.ts_root().iterdir())


PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n"
            "#EXTINF:2.000000,\nseg00007.ts\n#EXTINF:2.000000,\nseg00008.ts\n"
            "#EXT-X-DISCONTINUITY\n#EXTINF:2.000000,\nseg00009.ts\n")


def test_playlist_leaves_out_dropped_segments(tmp_path, monkeypatch):
    (tmp_path / "index.m3u8").write_text(PLAYLIST)
    for n in (8, 9):
        (tmp_path / f"seg{n:05d}.ts").write_bytes(b"x")
    monkeypatch.setattr(hls, "_sessions", {"s1": {"dir": tmp_path, "buffer": 0, "timeshift": 30}})
    text = hls.playlist("s1", tmp_path / "index.m3u8")
    assert "seg00007.ts" not in text and "#EXT-X-MEDIA-SEQUENCE:8" in text
    assert text.endswith("#EXT-X-DISCONTINUITY\n#EXTINF:2.000000,\nseg00009.ts\n")
    # without timeshift the playlist goes out as ffmpeg wrote it
    monkeypatch.setattr(hls, "_sessions", {"s1": {"dir": tmp_path, "buffer": 0, "timeshift": 0}})
    assert hls.playlist("s1", tmp_path / "index.m3u8") == PLAYLIST


def test_low_disk_drops_the_oldest_minute(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    d = hls.ts_root() / "s1"
    d.mkdir(parents=True)
    for n in range(60):
        (d / f"seg{n:05d}.ts").write_bytes(b"x")
    monkeypatch.setattr(hls, "_sessions", {"s1": {"dir": d, "timeshift": 30}})
    free = [0, hls.TS_SPARE_MB * 1024 * 1024]               # low once, then fine
    monkeypatch.setattr(hls.shutil, "disk_usage", lambda p: Usage(0, 0, free.pop(0) if len(free) > 1 else free[0]))
    hls._trim_timeshift()
    left = sorted(f.name for f in d.iterdir())
    assert len(left) == 30 and left[0] == "seg00030.ts"


def test_keep_alive_holds_only_timeshift_sessions(monkeypatch):
    monkeypatch.setattr(hls, "_sessions", {"a": {"last": 0, "client": "c1", "timeshift": 30},
                                           "b": {"last": 0, "client": "c1"}})
    assert hls.keep_alive("c1") == 1
    assert hls._sessions["a"]["last"] > 0 and hls._sessions["b"]["last"] == 0
