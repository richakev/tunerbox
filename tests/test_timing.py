"""The log lines that say where the time went while playlists are read (to find what's slow on an install)."""
import logging

import httpx
import pytest

from tunerbox import db, sources, timing, vod


def test_steps_say_how_long_each_part_took():
    s = timing.Steps()
    assert str(s).endswith(" s")
    s.lap("download")
    s.lap("saving")
    assert str(s).startswith(f"{s.total():.1f} s (download ") and ", saving 0.0 s)" in str(s)


def test_machine_line_names_the_database_and_its_settings():
    line = timing.machine()
    assert "SQLite" in line and "tunerbox.db" in line and "journal wal" in line and "opened in" in line


def test_machine_line_warns_about_a_synced_folder(monkeypatch, tmp_path):
    synced = tmp_path / "OneDrive" / "TunerBox"
    synced.mkdir(parents=True)
    monkeypatch.setattr(timing.config, "DATA", synced)
    monkeypatch.setattr(timing.db, "conn", lambda: (_ for _ in ()).throw(OSError("not here")))
    line = timing.machine()
    assert "inside a OneDrive folder" in line and "not here" in line


def _src():
    sid = db.run("INSERT INTO sources(name, type, url, username, password) VALUES ('P', 'xtream', 'http://p:80', 'u', 'pw')")
    return db.one("SELECT * FROM sources WHERE id=?", (sid,))


def _answer(monkeypatch, response):
    monkeypatch.setattr(sources, "client", lambda timeout=60: httpx.Client(transport=httpx.MockTransport(lambda req: response)))


def test_a_slow_xtream_call_is_logged_with_where_its_time_went(monkeypatch, caplog):
    _answer(monkeypatch, httpx.Response(200, json=[{"stream_id": 1}], headers={"content-encoding": "identity"}))
    monkeypatch.setattr(sources, "SLOW", 0)
    with caplog.at_level(logging.INFO, logger="tunerbox.sources"):
        assert sources.xtream_api(_src(), "get_vod_streams") == [{"stream_id": 1}]
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Xtream get_vod_streams from P"))
    assert "provider answered" in line and "MB (identity, HTTP/1.1)" in line and "reading the JSON" in line


def test_a_quick_xtream_call_logs_nothing(monkeypatch, caplog):
    _answer(monkeypatch, httpx.Response(200, json={}))
    with caplog.at_level(logging.INFO, logger="tunerbox.sources"):
        sources.xtream_api(_src())
    assert not [r for r in caplog.records if r.getMessage().startswith("Xtream")]


def test_a_refused_xtream_call_still_raises(monkeypatch):
    _answer(monkeypatch, httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        sources.xtream_api(_src(), "get_live_streams")


def test_reading_a_playlist_logs_each_step(monkeypatch, caplog, tmp_path):
    m3u = tmp_path / "list.m3u"
    m3u.write_text('#EXTM3U\n#EXTINF:-1 group-title="News",News One\nhttp://x/1.ts\n'
                   '#EXTINF:-1 group-title="Films",A Film (2020)\nhttp://x/film.mp4\n')
    sid = db.run("INSERT INTO sources(name, type, url, vod_movies) VALUES ('Mine', 'm3u', ?, 1)", (str(m3u),))
    monkeypatch.setattr(sources, "SLOW", 0)
    with caplog.at_level(logging.INFO):
        assert sources.refresh_source(db.one("SELECT * FROM sources WHERE id=?", (sid,))) == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Playlist Mine: ") and "reading 2 entries" in m for m in msgs)
    assert any(m.startswith("On Demand Mine movie+series: saved 1 titles in ") and "batches" in m for m in msgs)
    assert any(m.startswith("Source Mine: 1 On Demand titles in ") and "saving 0 episodes" in m for m in msgs)
    line = next(m for m in msgs if m.startswith("Source Mine: 1 channels in "))
    assert all(step in line for step in ("fetching 2 entries", "On Demand files", "sorting", "saving"))


def test_on_demand_lists_log_each_step(monkeypatch, caplog):
    data = {"get_vod_categories": [{"category_id": "1", "category_name": "Action"}],
            "get_vod_streams": [{"stream_id": 7, "name": "Heat (1995)", "category_id": "1"}],
            "get_series_categories": [], "get_series": []}
    monkeypatch.setattr(sources, "xtream_api", lambda src, action=None, **p: data[action])
    s = _src()
    db.run("UPDATE sources SET vod_movies=1, vod_series=1 WHERE id=?", (s["id"],))
    with caplog.at_level(logging.INFO, logger="tunerbox.vod"):
        vod.refresh_xtream(db.one("SELECT * FROM sources WHERE id=?", (s["id"],)))
    msgs = [r.getMessage() for r in caplog.records]
    films = next(m for m in msgs if m.startswith("On Demand P films: "))
    assert all(step in films for step in ("1 categories", "downloading 1 films", "reading them", "saving"))
    assert any(m.startswith("On Demand P shows: ") and "downloading 0 shows" in m for m in msgs)
