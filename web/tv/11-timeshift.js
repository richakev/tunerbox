// ---------------------------------------------------------------- pause / rewind live TV (timeshift)
// The server keeps up to /api/timeshift's minutes of a live channel in its HLS playlist when asked
// (/api/hls/{n}?timeshift=, /api/feed/hls?key=..&timeshift=). With that on, this screen plays live channels
// over HLS instead of the plain stream: hls.js (vendor/hls.light.min.js) where there's Media Source
// Extensions (Chrome, Edge, Firefox), else the browser's own HLS player (Safari, iPhone, iPad). The video
// element then pauses and seeks in what the playlist keeps: Space pauses, Rewind / FF (or , and .) skip
// 15 s back / 30 s ahead, and the progress bar shows how far behind live it is (click or drag it to move).
// pasiegel's TunerBox has no /api/timeshift (a 404): TS.min stays 0 and live TV plays as before (mpegts.js).
// Multiview tiles keep the plain stream. A paused player may ask for nothing, so the screen tells the server
// it's still there (/api/timeshift?client=) every few seconds while paused.
// userPaused: the viewer paused (not a picture still loading); pausedStart: the kept stretch's start then
const TS = { min: 0, pauseWhenUp: false, target: null, ticks: 0, userPaused: false, pausedStart: 0 };
TS.ready = (async () => {
  try { const r = await api("/api/timeshift"); TS.min = Math.max(0, Math.floor(+r.minutes) || 0); } catch (_) { TS.min = 0; }
})();
const TS_BACK = 15, TS_AHEAD = 30, TS_EDGE = 3;     // seconds: Rewind, FF, "at live" slack

function tsOwnOff() { try { return localStorage.getItem("tb.timeshift") === "0"; } catch (_) { return false; } }
// the server can, this screen hasn't turned it off (Menu), and the browser can play HLS one way or the other
function tsWanted() {
  return TS.min > 0 && !tsOwnOff() && (useHls() || !!(window.Hls && Hls.isSupported()));
}
// what's playing on the main screen is pausable (S.ts is set by tsPlay and cleared with the player)
function tsActive() { return !!(S.ts && S.player && S.ts.p === S.player && S.mode === "live" && !mvOn()); }

// the video's pausable stretch: { start, edge } in its own clock (edge: where live plays)
function tsRange() {
  const s = video.seekable;
  if (!s || !s.length) return null;
  const start = s.start(0), end = s.end(s.length - 1);
  const sync = S.ts && S.ts.hls && S.ts.hls.liveSyncPosition;
  const edge = sync && isFinite(sync) ? Math.min(sync, end) : Math.max(start, end - 6);
  return { start, edge: Math.max(start, edge) };
}
function tsLag() { const r = tsRange(); return r ? Math.max(0, r.edge - video.currentTime) : 0; }

// a live channel on the main screen, pausable. `onFail` / `failed` / `again` / `badge` as in play().
function tsPlay(ch, alt, onFail, failed, again, badge) {
  const key = ch.feed ? `f:${ch.feed}` : `${ch.number}:${alt}`;
  const mode = h264Keys.has(key) ? "h264" : "";
  const url = `${streamUrl(ch, alt, true, mode)}&timeshift=${TS.min}`;
  const token = {};
  const pauseWhenUp = TS.pauseWhenUp;      // Space asked for this start to come up paused (tsKey)
  TS.pauseWhenUp = false;
  TS.userPaused = false;
  let played = false;
  video.addEventListener("playing", function up() {
    if (!S.ts || S.ts.token !== token) return;
    played = true; S.retries = 0;
    if (pauseWhenUp) { tsUserPause(); tsShowBar(); }
  }, { once: true });
  const start = () => {
    const pr = video.play();
    if (pr && pr.catch) pr.catch((e) => { if (e && e.name === "NotAllowedError" && S.ts && S.ts.token === token) showTapPlay(); });
  };
  // Safari / iPhone / iPad: its own HLS player
  if (useHls() || !window.Hls) {
    S.player = { destroy() {}, token };
    S.ts = { p: S.player, token, hls: null };
    video.onerror = () => { if (S.player && S.player.token === token) onFail(); };
    video.src = url;
    start();
    S.watchdog = setTimeout(() => { if (S.player && S.player.token === token && !played && video.readyState < 3) onFail(); }, 35000 + bufSecs() * 1000);
    return;
  }
  const h = new Hls({
    enableWorker: true, lowLatencyMode: false,
    liveSyncDurationCount: 3,          // play 3 segments (~6 s) behind the newest, as Safari does
    maxBufferLength: 30, backBufferLength: 90,
    enableCEA708Captions: false,       // 47-captions.js draws them from the picture itself
  });
  h.token = token;
  S.player = h;
  S.ts = { p: h, token, hls: h };
  const mine = () => S.player === h;
  let mediaRetry = false, reconnecting = false;
  h.on(Hls.Events.ERROR, (_e, d) => {
    if (!mine() || !d || !d.fatal) return;
    console.warn("timeshift player error", d.type, d.details);
    if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
      if (!mediaRetry) { mediaRetry = true; h.recoverMediaError(); return; }
      // a picture this browser can't decode (HEVC, MPEG-2): the server converts it, as codecFallback does
      if (!mode) { h264Keys.add(key); toast("Converting the picture for this screen…", 3000); setTimeout(again, 0); return; }
    }
    if (!played) { onFail(); return; }
    // the stream dropped mid-show: reconnect, as the plain stream does (unless Setup stopped it), once per drop
    if (reconnecting) return;
    reconnecting = true;
    stoppedFromSetup(badge).then((stopped) => {
      if (stopped || !mine()) return;
      S.retries = (S.retries || 0) + 1;
      if (S.retries <= 3) { toast("Reacquiring signal…"); setTimeout(() => mine() && again(), 1500); }
      else failed();
    });
  });
  // /api/hls/{n} answers once the playlist has a few segments, then redirects to it: hls.js is given the
  // playlist itself, so its reloads never start a second session
  fetch(url, { cache: "no-store" }).then((r) => {
    if (!mine()) return;
    if (!r.ok) { onFail(); return; }
    h.attachMedia(video);
    h.loadSource(r.url);
    h.once(Hls.Events.MANIFEST_PARSED, () => { if (mine()) start(); });
  }).catch(() => { if (mine()) onFail(); });
  S.watchdog = setTimeout(() => { if (mine() && !played && video.readyState < 3) onFail(); }, 40000 + bufSecs() * 1000);
}

function tsSeekTo(t) {
  const r = tsRange();
  if (!r) return;
  const at = Math.min(r.edge, Math.max(r.start + 4, t));
  TS.target = at;
  video.currentTime = at;                   // paused stays paused, as on a DVR
  tsShowBar();
}
function tsSkip(delta) {
  const r = tsRange();
  if (!r) return false;
  const from = TS.target !== null ? TS.target : video.currentTime;
  if (delta > 0 && r.edge - from < TS_EDGE) return false;        // at live already
  if (delta < 0 && from - r.start < 6) { toast("That's as far back as this channel goes"); tsShowBar(); return true; }
  tsSeekTo(from + delta);
  return true;
}
video.addEventListener("seeked", () => { TS.target = null; });

function tsUserPause() {
  const r = tsRange();
  TS.userPaused = true; TS.pausedStart = r ? r.start : 0;
  video.pause();
}
function tsPause() {
  if (video.paused) video.play().catch(() => {}); else tsUserPause();
  tsShowBar();
}

// Space / Rewind / FF on live TV. false: not handled (the key does what it did before)
function tsKey(k) {
  if (k === " ") {
    if (tsActive()) { tsPause(); return true; }
    // this channel started before the server answered (or with it off): start it again pausable, paused
    // (only a channel that's playing: not No Signal, nor one Setup stopped, where S.player is null)
    if (tsWanted() && S.mode === "live" && !mvOn() && S.player && !(S.ts && S.ts.p === S.player)
        && (S.feedCh || S.idx >= 0)) {
      TS.pauseWhenUp = true;
      toast("Getting live TV ready to pause…");
      tsRetune();
      TS.pauseWhenUp = false;               // tsPlay took it, or the retune didn't get that far
      return true;
    }
    return false;
  }
  if (!tsActive()) return false;
  if (k === "Rew") return tsSkip(-TS_BACK);
  if (k === "FF") return tsSkip(TS_AHEAD);
  return false;
}
function tsRetune() {
  if (S.feedCh) { const f = S.feedCh; playFeed({ key: f.feed, name: f.name, group: f.group, logo: f.logo, kind: f.badge === "PPV" ? "ppv" : "events" }, true); }
  else if (S.idx >= 0) tune(S.idx, S.alt, true);
}

// the progress bar (shared with recordings and On Demand): the kept stretch, where the picture is in it
function tsDrawBar() {
  const r = tsRange();
  if (!r) return;
  const span = Math.max(1, r.edge - r.start), lag = Math.max(0, r.edge - video.currentTime), ch = curCh();
  $("pb-title").textContent = ch ? [ch.number, ch.name].filter(Boolean).join(" ") : "";
  if (!pbScrub) $("pb-progress").style.width = `${Math.min(100, (video.currentTime - r.start) / span * 100)}%`;
  $("pb-time").textContent = `${video.paused ? "❚❚ Paused · " : ""}${lag < TS_EDGE ? "Live" : fmtDur(lag) + " behind live"}`
    + `   ·   Space pause · Rew −${TS_BACK}s · FF +${TS_AHEAD}s · up to ${fmtDur(TS.min * 60)} back`;
}
function tsShowBar() {
  if (!tsActive()) return;
  tsDrawBar();
  showPlaybackBar();
  if (video.paused) clearTimeout(pbTimer);        // stays up while paused
  tsRemoteSync();
}
video.addEventListener("timeupdate", () => { if (tsActive() && !$("playback").classList.contains("hidden")) tsDrawBar(); });
video.addEventListener("pause", () => { if (tsActive()) tsRemoteSync(); });
video.addEventListener("play", () => {
  TS.userPaused = false;
  if (!tsActive()) return;
  tsRemoteSync();
  if (!$("playback").classList.contains("hidden")) showPlaybackBar();     // hides in 4 s, not while it's held
});

// the on-screen controls' Rewind / Pause / FF buttons: only while what's on can pause
function tsRemoteSync() {
  const on = tsActive();
  document.querySelectorAll("#remote .rm-ts").forEach((b) => b.classList.toggle("hidden", !on));
  const p = $("rm-pause");
  if (p) {
    p.querySelector("use").setAttribute("href", on && video.paused ? "#i-play" : "#i-pause");
    p.title = on && video.paused ? "Play (Space)" : "Pause live TV (Space)";
  }
}

// while paused: keep the server's session, keep the bar's clock going, and play on before the paused spot
// drops out of what the playlist keeps
setInterval(() => {
  // multiview has no pause controls: a channel the viewer paused before opening it plays on (once: the
  // "play" event clears userPaused; a picture still loading isn't userPaused)
  if (TS.userPaused && S.ts && S.ts.p === S.player && S.player && mvOn() && video.paused && S.mode === "live") {
    TS.userPaused = false;
    toast("Playing live TV on: it can't stay paused in multiview", 4000);
    video.play().catch(() => {});
  }
  if (!tsActive() || !video.paused) { TS.ticks = 0; return; }
  if (++TS.ticks % 10 === 0) api(`/api/timeshift?client=${encodeURIComponent(CID)}`).catch(() => {});
  const r = tsRange();
  // only once the playlist has started dropping its oldest (its start moved on since the pause): a new
  // session holds only a few seconds, and pausing there is fine
  if (r && r.start > TS.pausedStart + 1 && video.currentTime - r.start < 8) { toast("Paused as long as TunerBox keeps live TV: playing on", 5000); video.play().catch(() => {}); }
  if (!$("playback").classList.contains("hidden")) tsDrawBar();
}, 1000);

// Menu › Pause live TV: this screen's choice
function tsToggleOwn() {
  try { localStorage.setItem("tb.timeshift", tsOwnOff() ? "1" : "0"); } catch (_) {}
  toast(`Pause live TV: ${tsOwnOff() ? "Off" : "On"} (this screen)`);
  // only a channel that's playing (not No Signal, nor one Setup stopped)
  if (S.mode === "live" && !mvOn() && S.player) tsRetune();
}
