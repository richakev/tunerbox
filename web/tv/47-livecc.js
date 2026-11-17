// ---------------------------------------------------------------- live TV captions
// Menu › Captions, the CC button (or C) on live TV. The server reads the broadcaster's closed captions out
// of the channel's stream (tunerbox/captions.py) and this draws them over the picture in step with it:
// each caption has the time of its frame, and the server says where this screen's stream started, so it
// shows at the right moment however far behind live the screen plays. A channel without captions gets
// speech-to-text instead, which needs a few seconds: that channel then plays ~10 seconds behind live.
// iPhone Safari (HLS) draws the broadcaster's captions itself.
S.cc = { on: localStorage.getItem("tb.cc") === "1", id: null, after: 0, cues: [], base: null, state: "",
         told: "", delayKey: "", polling: false, hlsTold: false, shifts: {} };
const ccKey = (ch) => (ch ? (ch.feed ? `f:${ch.feed}` : `${ch.number}`) : "");
// seconds of stream to hold before playing: the screen's cache, or more for speech captions on this channel
function playBuf(ch) {
  const b = bufSecs();
  return S.cc.on && S.cc.delayKey && S.cc.delayKey === ccKey(ch) ? Math.max(b, S.cc.delaySecs || 10) : b;
}
function ccToggle() {
  const c = S.cc;
  c.on = !c.on;
  try { localStorage.setItem("tb.cc", c.on ? "1" : "0"); } catch (_) {}
  toast(c.on ? "Captions on" : "Captions off", 2000);
  ccReset();
  ccHlsTracks();
  if (c.on) ccPoll();
  else ccDraw();
}
function ccReset() {
  const c = S.cc;
  c.id = null; c.after = 0; c.cues = []; c.base = null; c.state = ""; c.told = ""; c.hlsTold = false;
}
// Safari's own caption display (HLS): on or off with the toggle
function ccHlsTracks() {
  if (!useHls()) return;
  let any = false;
  for (const t of video.textTracks || []) {
    if (t.kind === "captions" || t.kind === "subtitles") { t.mode = S.cc.on && S.mode === "live" ? "showing" : "disabled"; any = true; }
  }
  return any;
}
if (video.textTracks && video.textTracks.addEventListener) video.textTracks.addEventListener("addtrack", ccHlsTracks);

function ccSay(state, msg, ms = 4000) {
  if (S.cc.told === state) return;
  S.cc.told = state;
  if (msg) toast(msg, ms);
}
async function ccRetune() {
  // the server keeps this screen's tuner while the stream restarts (no new provider connection, and it
  // starts from what the tuner kept, so there's no wait)
  try { await api(`/api/cc/hold?client=${encodeURIComponent(CID)}`, { method: "POST" }); } catch (_) {}
  const f = S.feedCh;
  if (f) playFeed({ key: f.feed, name: f.name, logo: f.logo, group: f.group, kind: f.badge === "PPV" ? "ppv" : "events" });
  else if (S.idx >= 0) tune(S.idx, S.alt);
}

async function ccPoll() {
  const c = S.cc;
  if (!c.on || c.polling || S.mode !== "live" || !S.player || !curCh()) return;
  if (useHls()) {
    const k = ccKey(curCh());
    if (c.hlsKey !== k) { c.hlsKey = k; c.hlsTold = false; }
    if (!c.hlsTold && video.readyState >= 3) {
      c.hlsTold = true;
      setTimeout(() => { if (S.cc.on && S.mode === "live" && !ccHlsTracks()) toast("This channel has no captions", 3000); }, 8000);
    }
    return;
  }
  const key = ccKey(curCh());
  c.polling = true;
  let r;
  try { r = await api(`/api/cc?client=${encodeURIComponent(CID)}&after=${c.id ? c.after : 0}`); } catch (_) { r = null; }
  c.polling = false;
  if (!r || !c.on || ccKey(curCh()) !== key) return;
  if (r.id !== c.id) { c.id = r.id; c.after = 0; c.cues = []; }      // another channel (another tuner)
  c.state = r.state; c.base = r.base ?? null;
  for (const q of r.cues || []) {        // kept in time order (a speech line can come after a later caption)
    let i = c.cues.length;
    while (i > 0 && c.cues[i - 1][1] > q[1]) i--;
    c.cues.splice(i, 0, q);
    c.after = Math.max(c.after, q[0]);
  }
  if (c.cues.length > 1500) c.cues.splice(0, c.cues.length - 1500);
  if (r.state === "none") ccSay("none", "This channel has no captions");
  else if (r.state === "ai-loading") ccSay("ai-loading", "No captions on this channel: getting speech-to-text ready…");
  else if (r.state === "ai") ccSay("ai", "");
  else if (r.state === "cc") ccSay("cc", "");
  // speech captions are a few seconds late: play this channel far enough behind live that they line up
  // live captions typed as people speak come out seconds after the words: they're shown that much earlier,
  // so the screen plays far enough behind live to have them in time (once per channel, or if it grows)
  c.shifts = r.shifts || {};
  const had = c.delayKey === key ? c.delaySecs : 0;
  if (r.delay && r.delay > Math.max(bufSecs(), had) + (had ? 1 : 0)) {
    c.delayKey = key; c.delaySecs = r.delay;
    toast(r.state === "cc"
      ? `Timing captions to the speech: playing ${r.delay} seconds behind live`
      : `No captions on this channel: captions from speech, playing ${r.delay} seconds behind live`, 5000);
    ccRetune();
  }
}
setInterval(ccPoll, 700);

// the caption on screen now: the newest one whose moment has come on this screen's video clock
function ccDraw() {
  const el = $("lcc"), c = S.cc;
  let html = "", top = false;
  if (c.on && S.mode === "live" && c.base !== null && c.cues.length && !useHls()) {
    const t = video.currentTime * 1000 + c.base;
    // when a cue is due on this clock: live-typed captions are moved earlier by how far they lag the speech
    const at = (x) => x[1] - (x[5] === "cc" ? c.shifts[x[6]] || 0 : 0);
    let cue = null;         // the latest one that's due (styles shift differently, so not always the last)
    for (let j = c.cues.length - 1; j >= 0; j--) {
      const x = c.cues[j], a = at(x);
      if (a <= t && (!cue || a > at(cue))) cue = x;
      if (x[1] < t - 60000) break;
    }
    if (cue && (cue[2] === null || t < cue[2]) && t - at(cue) < 30000) {
      html = cue[3].map(esc).join("<br>");
      top = cue[4];
    }
  }
  if (el.dataset.h !== html) { el.dataset.h = html; el.innerHTML = html ? `<span>${html}</span>` : ""; }
  el.classList.toggle("hidden", !html);
  el.classList.toggle("top", top);
}
video.addEventListener("timeupdate", ccDraw);
setInterval(() => { if (S.cc.on || !$("lcc").classList.contains("hidden")) ccDraw(); }, 200);
