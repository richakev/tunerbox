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


@pytest.fixture
def gzip_server():
    """A real HTTP server sending a gzipped list: httpx's MockTransport doesn't count the bytes downloaded."""
    import gzip
    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    body = json.dumps([{"stream_id": i, "name": os.urandom(8).hex()} for i in range(20000)]).encode()
    zipped = gzip.compress(body)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(zipped)))
            self.end_headers()
            self.wfile.write(zipped)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", len(body), len(zipped)
    srv.shutdown()
    srv.server_close()


def test_a_compressed_list_logs_its_size_and_what_was_sent(gzip_server, monkeypatch, caplog):
    url, size, sent = gzip_server
    monkeypatch.setattr(sources, "SLOW", 0)
    sid = db.run("INSERT INTO sources(name, type, url, username, password) VALUES ('P', 'xtream', ?, 'u', 'pw')", (url,))
    with caplog.at_level(logging.INFO, logger="tunerbox.sources"):
        assert len(sources.xtream_api(db.one("SELECT * FROM sources WHERE id=?", (sid,)), "get_vod_streams")) == 20000
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Xtream get_vod_streams"))
    assert sent < size / 2                                  # what came over the network, not the unzipped list
    assert f"download {size / 1e6:.1f} MB (gzip, {sent / 1e6:.1f} MB sent, HTTP/1." in line


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
    assert any(m.startswith("Source Mine: 1 On Demand titles in ") and "saving 0 new or changed of 0 episodes" in m for m in msgs)
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


# ---------------------------------------------------------------- only what changed is written
def _films(*films):
    return {"get_vod_categories": [{"category_id": "1", "category_name": "Action"}, {"category_id": "2", "category_name": "Drama"}],
            "get_vod_streams": [{"stream_id": sid, "name": name, "category_id": cat} for sid, name, cat in films]}


def _refresh(monkeypatch, caplog, data):
    monkeypatch.setattr(sources, "xtream_api", lambda src, action=None, **p: data[action])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="tunerbox.vod"):
        vod.refresh_xtream(db.one("SELECT * FROM sources WHERE vod_movies=1"))
    return next(r.getMessage() for r in caplog.records if r.getMessage().startswith("On Demand P movie: saved"))


def test_a_refresh_only_writes_new_changed_and_gone_titles(monkeypatch, caplog):
    s = _src()
    db.run("UPDATE sources SET vod_movies=1 WHERE id=?", (s["id"],))
    films = [(1, "Heat (1995)", "1"), (2, "Ronin (1998)", "1"), (3, "Fargo (1996)", "2")]
    assert "saving 3 new or changed" in _refresh(monkeypatch, caplog, _films(*films))
    db.run("UPDATE vod_titles SET plot='A heist.', bitrate=4000 WHERE id=?", (f"{s['id']}:m1",))   # from the title's details
    first = {r["id"]: r["seen_at"] for r in db.rows("SELECT id, seen_at FROM vod_titles")}

    line = _refresh(monkeypatch, caplog, _films(*films))                     # the same list again: nothing written
    assert "saving 0 new or changed" in line and "removing 0 gone" in line
    assert {r["id"]: r["seen_at"] for r in db.rows("SELECT id, seen_at FROM vod_titles")} == first

    films = [(1, "Heat (1995)", "1"), (2, "Ronin: Director's Cut (1998)", "1"), (4, "Alien (1979)", "2")]
    line = _refresh(monkeypatch, caplog, _films(*films))                     # one renamed, one new, one gone
    assert "saving 2 new or changed" in line and "removing 1 gone" in line
    t = {r["id"]: r for r in db.rows("SELECT * FROM vod_titles")}
    assert sorted(t) == [f"{s['id']}:m1", f"{s['id']}:m2", f"{s['id']}:m4"]
    assert t[f"{s['id']}:m2"]["name"] == "Ronin: Director's Cut" and t[f"{s['id']}:m2"]["search"] == vod.fold("Ronin: Director's Cut")
    assert (t[f"{s['id']}:m1"]["plot"], t[f"{s['id']}:m1"]["bitrate"]) == ("A heist.", 4000)    # details kept


def test_a_hidden_category_stays_hidden_and_shows_again(monkeypatch, caplog):
    s = _src()
    db.run("UPDATE sources SET vod_movies=1 WHERE id=?", (s["id"],))
    data = _films((1, "Heat (1995)", "1"), (3, "Fargo (1996)", "2"))
    _refresh(monkeypatch, caplog, data)
    vod.set_hidden(s["id"], ["Drama"])
    assert "saving 0 new or changed" in _refresh(monkeypatch, caplog, data)
    assert db.one("SELECT hidden FROM vod_titles WHERE id=?", (f"{s['id']}:m3",))["hidden"] == 1
    vod.set_hidden(s["id"], [])
    _refresh(monkeypatch, caplog, data)
    assert db.one("SELECT hidden FROM vod_titles WHERE id=?", (f"{s['id']}:m3",))["hidden"] == 0


def test_m3u_episodes_only_change_when_the_playlist_does(tmp_path):
    m3u = tmp_path / "list.m3u"
    eps = ['#EXTINF:-1 group-title="Shows",Lost S01E01 Pilot\nhttp://x/lost101.mkv',
           '#EXTINF:-1 group-title="Shows",Lost S01E02 Tabula Rasa\nhttp://x/lost102.mkv']
    sid = db.run("INSERT INTO sources(name, type, url, vod_movies) VALUES ('Mine', 'm3u', ?, 1)", (str(m3u),))
    src = lambda: db.one("SELECT * FROM sources WHERE id=?", (sid,))        # noqa: E731
    m3u.write_text("#EXTM3U\n" + "\n".join(eps) + "\n")
    sources.refresh_source(src())
    db.run("UPDATE vod_episodes SET duration=2580 WHERE url='http://x/lost101.mkv'")      # learned when played
    sources.refresh_source(src())
    assert db.one("SELECT duration FROM vod_episodes WHERE url='http://x/lost101.mkv'")["duration"] == 2580

    m3u.write_text("#EXTM3U\n" + eps[0].replace("lost101.mkv", "lost101-hd.mkv") + "\n")   # moved, and E02 gone
    sources.refresh_source(src())
    assert [(r["episode"], r["url"]) for r in db.rows("SELECT episode, url FROM vod_episodes")] == [(1, "http://x/lost101-hd.mkv")]


def test_a_title_listed_twice_is_saved_once_and_not_rewritten(monkeypatch, caplog):
    s = _src()
    db.run("UPDATE sources SET vod_movies=1 WHERE id=?", (s["id"],))
    data = _films((1, "Heat (1995)", "1"), (2, "Ronin (1998)", "1"))
    data["get_vod_streams"].append({"stream_id": 1, "name": "Heat (1995)", "category_id": "1", "stream_icon": "b.jpg"})
    assert "saved 2 titles" in _refresh(monkeypatch, caplog, data)
    assert "saving 0 new or changed" in _refresh(monkeypatch, caplog, data)
    assert db.one("SELECT poster FROM vod_titles WHERE id=?", (f"{s['id']}:m1",))["poster"] == "b.jpg"   # the last one
    assert db.one("SELECT count FROM vod_cats WHERE source_id=? AND name='Action'", (s["id"],))["count"] == 2


def test_an_m3u_episode_listed_twice_is_saved_once_and_not_rewritten(tmp_path, caplog):
    m3u = tmp_path / "list.m3u"
    m3u.write_text('#EXTM3U\n#EXTINF:-1 group-title="Shows",Lost S01E01 Pilot\nhttp://x/lost101.mkv\n'
                   '#EXTINF:-1 group-title="Shows",Lost S01E01 Pilot\nhttp://x/lost101-4k.mkv\n'
                   '#EXTINF:-1 group-title="Shows",Lost S01E02 Tabula Rasa\nhttp://x/lost102.mkv\n')
    sid = db.run("INSERT INTO sources(name, type, url, vod_movies) VALUES ('Mine', 'm3u', ?, 1)", (str(m3u),))

    def refresh():
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="tunerbox.vod"):
            sources.refresh_source(db.one("SELECT * FROM sources WHERE id=?", (sid,)))
        return next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Source Mine: 1 On Demand titles"))
    assert "saving 2 new or changed of 2 episodes, removing 0 gone" in refresh()
    assert "saving 0 new or changed of 2 episodes, removing 0 gone" in refresh()
    assert db.one("SELECT url FROM vod_episodes WHERE episode=1")["url"] == "http://x/lost101-4k.mkv"     # the last one
    m3u.write_text(m3u.read_text().split("#EXTINF:-1 group-title=\"Shows\",Lost S01E02")[0])
    assert "saving 0 new or changed of 1 episodes, removing 1 gone" in refresh()
