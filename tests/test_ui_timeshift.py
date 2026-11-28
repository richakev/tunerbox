"""Pausing live TV in a real browser (web/tv/11-timeshift.js): with a server that keeps live TV
(/api/timeshift), a channel plays over HLS (hls.js) with timeshift= on its address, Space pauses and
Rewind / FF skip in what it keeps; without one (pasiegel's TunerBox: a 404), or with it off on this screen,
it plays the plain stream (mpegts.js) as before. hls.js, mpegts.js and the video element are stand-ins:
the server is mocked and nothing really plays.

Needs Playwright (see test_ui_gamestats.py); skipped when it isn't installed."""
import json
import mimetypes
from pathlib import Path

import pytest

from tunerbox import bundle

sync_api = pytest.importorskip("playwright.sync_api")

WEB = Path(__file__).resolve().parent.parent / "web"
BASE = "http://tunerbox.test"
CHANNELS = [
    {"number": 702, "name": "ESPN", "label": "ESPN", "mapped": True, "alts": 0, "epg": None, "logo": ""},
    {"number": 705, "name": "FS1", "label": "FS1", "mapped": True, "alts": 0, "epg": None, "logo": ""},
]

FAKE_MPEGTS = """
window.__streams = [];
window.mpegts = {
  Events: { ERROR: "error", MEDIA_INFO: "media_info", LOADING_COMPLETE: "loading_complete" },
  ErrorTypes: { MEDIA_ERROR: "MediaError" },
  getFeatureList: () => ({ mseLivePlayback: true }),
  createPlayer(cfg) {
    window.__streams.push(cfg.url.replace(location.origin, ""));
    return { on() {}, attachMediaElement() {}, load() {}, play() { return Promise.resolve(); },
             pause() {}, unload() {}, detachMediaElement() {}, destroy() {} };
  },
};
"""

# hls.js: remembers what it was given and says the playlist is parsed
FAKE_HLS = """
window.__hls = [];
window.Hls = class {
  static isSupported() { return true; }
  constructor(cfg) { this.cfg = cfg; this.h = {}; window.__hls.push(this); }
  on(e, f) { (this.h[e] = this.h[e] || []).push(f); }
  once(e, f) { this.on(e, f); }
  emit(e, ...a) { (this.h[e] || []).forEach((f) => f(...a)); }
  attachMedia(v) { this.v = v; }
  loadSource(u) { this.src = u.replace(location.origin, ""); setTimeout(() => this.emit("hlsManifestParsed"), 10); }
  recoverMediaError() {}
  destroy() { this.dead = true; }
  get liveSyncPosition() { return window.__end - 6; }
};
Hls.Events = { ERROR: "hlsError", MANIFEST_PARSED: "hlsManifestParsed" };
Hls.ErrorTypes = { MEDIA_ERROR: "mediaError", NETWORK_ERROR: "networkError" };
"""

# the <video>: plays and pauses at once, keeps a clock, and has 0..__end seconds to seek in
FAKE_VIDEO = """() => {
  const v = document.getElementById('video');
  let paused = true, t = 0;
  window.__end = 600;
  Object.defineProperty(v, 'paused', { get: () => paused });
  Object.defineProperty(v, 'readyState', { get: () => 4 });
  Object.defineProperty(v, 'currentTime', { get: () => t, set: (x) => { t = x; setTimeout(() => v.dispatchEvent(new Event('seeked')), 5); } });
  Object.defineProperty(v, 'seekable', { get: () => ({ length: 1, start: () => window.__start || 0, end: () => window.__end }) });
  v.play = () => { paused = false; t = t || window.__end - 6; v.dispatchEvent(new Event('play'));
                   setTimeout(() => v.dispatchEvent(new Event('playing')), 5); return Promise.resolve(); };
  v.pause = () => { paused = true; v.dispatchEvent(new Event('pause')); };
}"""


def _bundle():
    js = bundle.tv_js()
    end = js.rindex("})();")
    return js[:end] + ("window.__ts = { S, TS, tune, handleKey, tsActive, tsLag, get video() { return video; }, CID };\n") + js[end:]


class Server:
    def __init__(self, minutes):
        self.minutes = minutes      # None: no /api/timeshift (an older TunerBox)
        self.hls = []               # /api/hls/... addresses asked for
        self.alive = []

    def __call__(self, route):
        url = route.request.url[len(BASE):]
        path = url.split("?")[0]
        if path in ("/", "/index.html"):
            return route.fulfill(path=str(WEB / "index.html"), content_type="text/html")
        if path == "/static/tv.js":
            return route.fulfill(body=_bundle(), content_type="text/javascript")
        if path == "/static/vendor/mpegts.js":
            return route.fulfill(body=FAKE_MPEGTS, content_type="text/javascript")
        if path == "/static/vendor/hls.light.min.js":
            return route.fulfill(body=FAKE_HLS, content_type="text/javascript")
        if path.startswith("/static/"):
            f = WEB / path[len("/static/"):]
            if f.is_file():
                return route.fulfill(path=str(f), content_type=mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        if path == "/api/timeshift":
            if "client=" in url:
                self.alive.append(url)
            if self.minutes is None:
                return route.fulfill(status=404, body='{"detail": "Not Found"}', content_type="application/json")
            return route.fulfill(body=json.dumps({"minutes": self.minutes, "kept": 0}), content_type="application/json")
        if path.startswith("/api/hls/"):
            # the real server redirects to /api/hls/s/<sid>/index.m3u8; Playwright doesn't route a redirect's
            # second request, so the playlist comes straight back here
            self.hls.append(url)
            return route.fulfill(body="#EXTM3U\n", content_type="application/vnd.apple.mpegurl")
        if path == "/api/channels":
            return route.fulfill(body=json.dumps(CHANNELS), content_type="application/json")
        if path == "/api/mv/room":
            return route.fulfill(body="[]", content_type="application/json")
        if path.startswith("/api/now/"):
            return route.fulfill(body='{"now": null, "next": null}', content_type="application/json")
        if path.startswith("/api/"):
            return route.fulfill(body="{}", content_type="application/json")
        return route.fulfill(status=404, body="")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = None
        for kw in ({}, {"channel": "chrome"}):
            try:
                b = p.chromium.launch(**kw)
                break
            except Exception:  # noqa: BLE001
                continue
        if not b:
            pytest.skip("no browser for Playwright (python -m playwright install chromium)")
        yield b
        b.close()


def open_tv(browser, minutes, own=None):
    pg = browser.new_page(viewport={"width": 1280, "height": 720})
    srv = Server(minutes)
    pg.route("**/*", srv)
    if own is not None:
        pg.add_init_script(f"localStorage.setItem('tb.timeshift', '{own}')")
    pg.goto(BASE + "/")
    pg.wait_for_function("window.__ts")
    pg.evaluate(FAKE_VIDEO)
    pg.evaluate(f"""async () => {{
        const m = window.__ts;
        await new Promise((r) => setTimeout(r, 150));            // /api/timeshift answered
        document.querySelectorAll('#home,#dvr,#guide,#od,.screen,#welcome').forEach((e) => e.classList.add('hidden'));
        m.S.layer = [];
        m.S.channels = {json.dumps(CHANNELS)};
        m.S.byNum = new Map(m.S.channels.map((c, i) => [c.number, i]));
        window.__streams.length = 0;
        m.tune(0, 0, true);
        await new Promise((r) => setTimeout(r, 200));
    }}""")
    pg.srv = srv
    return pg


@pytest.fixture
def tv(browser):
    pg = open_tv(browser, 30)
    yield pg
    pg.close()


def key(pg, k):
    return pg.evaluate(f"window.__ts.handleKey({json.dumps(k)})")


def test_live_tv_plays_pausable_over_hls(tv):
    cid = tv.evaluate("window.__ts.CID")
    (asked,) = tv.srv.hls
    assert asked == f"/api/hls/702?alt=0&client={cid}&buf=0&timeshift=30"
    # hls.js gets the address the fetch ended on (the playlist the server redirects to)
    assert tv.evaluate("window.__hls.map((h) => h.src)") == [asked]
    assert tv.evaluate("window.__streams") == []                      # not the plain stream
    assert tv.evaluate("window.__ts.tsActive()") is True


def test_space_pauses_and_the_bar_says_so(tv):
    key(tv, " ")
    assert tv.evaluate("window.__ts.video.paused") is True
    assert not tv.evaluate("document.getElementById('playback').classList.contains('hidden')")
    assert "Paused" in tv.evaluate("document.getElementById('pb-time').textContent")
    assert not tv.evaluate("document.getElementById('rm-pause').classList.contains('hidden')")
    key(tv, " ")
    assert tv.evaluate("window.__ts.video.paused") is False


def test_rewind_and_ff_skip(tv):
    edge = tv.evaluate("window.__ts.video.currentTime")
    assert key(tv, "Rew") is True
    assert tv.evaluate("window.__ts.video.currentTime") == edge - 15
    tv.wait_for_timeout(30)
    key(tv, "Rew")
    tv.wait_for_timeout(30)
    assert round(tv.evaluate("window.__ts.tsLag()")) == 30
    assert "behind live" in tv.evaluate("document.getElementById('pb-time').textContent")
    key(tv, "FF")
    tv.wait_for_timeout(30)
    assert round(tv.evaluate("window.__ts.tsLag()")) == 0              # back at live
    # at live, FF isn't a skip: it does what it did before
    assert tv.evaluate("window.__ts.TS && window.__ts.tsActive()") is True
    before = tv.evaluate("window.__ts.video.currentTime")
    key(tv, "FF")
    assert tv.evaluate("window.__ts.video.currentTime") == before


def test_rewind_stops_at_the_oldest_kept(tv):
    tv.evaluate("window.__ts.video.currentTime = 3")
    tv.wait_for_timeout(30)
    key(tv, "Rew")
    assert tv.evaluate("window.__ts.video.currentTime") == 3
    assert "as far back" in tv.evaluate("document.getElementById('toast').textContent")


def test_older_server_plays_the_plain_stream(browser):
    pg = open_tv(browser, None)            # pasiegel's TunerBox: /api/timeshift is a 404
    try:
        cid = pg.evaluate("window.__ts.CID")
        assert pg.evaluate("window.__streams") == [f"/api/stream/702?alt=0&client={cid}&buf=0"]
        assert pg.srv.hls == [] and pg.evaluate("window.__ts.TS.min") == 0
        assert key(pg, " ") is False                                    # Space does nothing on live TV, as before
        assert pg.evaluate("document.getElementById('rm-pause').classList.contains('hidden')")
    finally:
        pg.close()


def test_setup_off_plays_the_plain_stream(browser):
    pg = open_tv(browser, 0)
    try:
        assert pg.srv.hls == [] and len(pg.evaluate("window.__streams")) == 1
    finally:
        pg.close()


def test_this_screen_can_turn_it_off(browser):
    pg = open_tv(browser, 30, own="0")
    try:
        assert pg.srv.hls == [] and len(pg.evaluate("window.__streams")) == 1
    finally:
        pg.close()


def test_skipping_while_paused_stays_paused(tv):
    key(tv, " ")
    assert tv.evaluate("window.__ts.video.paused") is True
    key(tv, "Rew")
    tv.wait_for_timeout(30)
    assert tv.evaluate("window.__ts.video.paused") is True              # as on a DVR
    assert round(tv.evaluate("window.__ts.tsLag()")) == 15


def test_space_on_no_signal_doesnt_retune(tv):
    # a channel that failed over to No Signal, or Setup stopped: no player, nothing to make pausable
    tv.evaluate("window.__ts.S.player = null")
    before = len(tv.srv.hls)
    assert key(tv, " ") is False
    tv.wait_for_timeout(50)
    assert len(tv.srv.hls) == before and tv.evaluate("window.__ts.TS.pauseWhenUp") is False


def test_space_on_a_plain_stream_comes_back_paused_once(browser):
    pg = open_tv(browser, 30, own="0")          # plays the plain stream; then this screen turns pausing on
    try:
        pg.evaluate("localStorage.setItem('tb.timeshift', '1')")
        assert key(pg, " ") is True
        pg.wait_for_timeout(100)
        assert len(pg.srv.hls) == 1 and pg.evaluate("window.__ts.video.paused") is True
        assert pg.evaluate("window.__ts.TS.pauseWhenUp") is False
        key(pg, "ChannelUp")                     # the next channel isn't left to start paused
        pg.wait_for_timeout(600)
        assert len(pg.srv.hls) == 2 and pg.evaluate("window.__ts.video.paused") is False
    finally:
        pg.close()


def test_multiview_plays_a_paused_channel_on(tv):
    key(tv, " ")
    assert tv.evaluate("window.__ts.video.paused") is True
    tv.evaluate("window.__ts.S.mv.tiles.push({})")          # multiview opened over it
    try:
        tv.wait_for_timeout(1200)
        assert tv.evaluate("window.__ts.video.paused") is False
    finally:
        tv.evaluate("window.__ts.S.mv.tiles.length = 0")


def test_a_pause_near_the_start_stays_paused(tv):
    # a new session keeps only a few seconds: pausing there isn't "paused as long as it keeps"
    tv.evaluate("window.__end = 10; window.__ts.video.currentTime = 4")
    key(tv, " ")
    tv.wait_for_timeout(1200)
    assert tv.evaluate("window.__ts.video.paused") is True
    # once the oldest starts dropping and catches up with the paused spot, it plays on
    tv.evaluate("window.__start = 2")
    tv.wait_for_timeout(1200)
    assert tv.evaluate("window.__ts.video.paused") is False


def test_multiview_leaves_a_loading_picture_alone(tv):
    tv.evaluate("window.__ts.video.pause()")                # paused, but not by the viewer
    tv.evaluate("window.__ts.S.mv.tiles.push({})")
    try:
        tv.wait_for_timeout(1200)
        assert tv.evaluate("window.__ts.video.paused") is True
        assert "multiview" not in tv.evaluate("document.getElementById('toast').textContent")
    finally:
        tv.evaluate("window.__ts.S.mv.tiles.length = 0")
