// ---------------------------------------------------------------- closed captions (CEA-608)
// Broadcast TV carries its captions inside the picture: ATSC A/53 "GA94" cc_data in the H.264 / HEVC SEI.
// The server already keeps them (-c:v copy passes them on, and libx264 writes them back when it converts
// MPEG-2 or HEVC), and mpegts.js keeps every NAL unit in the fMP4 it gives Media Source Extensions, but
// nothing draws them. So the video's segments are read here as they're appended, the CC1 bytes queued by
// presentation time and decoded (pop-on, roll-up, paint-on) as the picture reaches them. Safari's own HLS
// player decodes them itself: its caption track is just turned on. Menu › Closed captions, or C, per screen.
// `early`: seconds to show live-typed (roll-up / paint-on) captions ahead of their picture. A stenographer
// captions live sports a few seconds behind the voices; the video ahead of the picture is already read, so
// those lines can be drawn sooner, by at most as much as is buffered. Pop-on captions, timed beforehand, stay put.
const CC = { on: false, early: 0, url: new Map(), cur: null };
const CC_EARLY = [0, 2, 3, 4, 5, 6, 8];
try { CC.on = localStorage.getItem("tb.cc") === "1"; CC.early = Math.max(0, +localStorage.getItem("tb.ccEarly") || 0); } catch (_) {}

// iPhone Safari has only ManagedMediaSource (and ManagedSourceBuffer); elsewhere it, if present, extends MediaSource
const CC_MS = window.MediaSource || window.ManagedMediaSource;
const CC_SB = window.SourceBuffer || window.ManagedSourceBuffer;
if (CC_MS && CC_SB) {
  const add = CC_MS.prototype.addSourceBuffer;
  CC_MS.prototype.addSourceBuffer = function (type) {
    const sb = add.call(this, type);
    const m = /^video\/.*codecs="?\s*(avc[13]|hvc1|hev1)/i.exec(type || "");
    if (m) this._tbcc = sb._tbcc = { hevc: /^h/i.test(m[1]), scale: 1000, q: [], dec: null, t: -1, any: false, played: 0, warned: false };
    return sb;
  };
  const app = CC_SB.prototype.appendBuffer;
  CC_SB.prototype.appendBuffer = function (data) {
    // the init segment's timescale is read even with captions off, so turning them on later times them right
    if (this._tbcc) { try { ccSegment(this._tbcc, data, this.timestampOffset || 0, CC.on); } catch (e) { /* never stop the picture */ } }
    return app.call(this, data);
  };
  const mk = URL.createObjectURL, rv = URL.revokeObjectURL;
  URL.createObjectURL = function (o) { const u = mk.call(URL, o); if (o instanceof CC_MS) CC.url.set(u, o); return u; };
  URL.revokeObjectURL = function (u) { CC.url.delete(u); return rv.call(URL, u); };
}

// ---- reading fMP4: moov (the timescale), then moof + mdat (each sample's time and NAL units)
const u32 = (u, i) => ((u[i] << 24) | (u[i + 1] << 16) | (u[i + 2] << 8) | u[i + 3]) >>> 0;
const u64 = (u, i) => u32(u, i) * 4294967296 + u32(u, i + 4);
function* ccBoxes(u, s, e) {
  while (s + 8 <= e) {
    let size = u32(u, s), body = s + 8;
    if (size === 1) { size = u64(u, s + 8); body = s + 16; } else if (size === 0) size = e - s;
    if (size < 8 || s + size > e) return;
    yield [String.fromCharCode(u[s + 4], u[s + 5], u[s + 6], u[s + 7]), s, body, s + size];
    s += size;
  }
}
const ccChild = (u, s, e, path) => {
  for (const name of path) {
    let hit = null;
    for (const b of ccBoxes(u, s, e)) if (b[0] === name) { hit = b; break; }
    if (!hit) return null;
    [, , s, e] = hit;
  }
  return [s, e];
};
function ccSegment(st, data, offset, on) {
  const u = data instanceof ArrayBuffer ? new Uint8Array(data) : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  let moof = null;
  for (const [type, s, body, e] of ccBoxes(u, 0, u.length)) {
    if (type === "moov") {
      const m = ccChild(u, body, e, ["trak", "mdia", "mdhd"]);
      if (m) st.scale = u32(u, m[0] + (u[m[0]] === 1 ? 20 : 12)) || 1000;
    } else if (!on) continue;
    else if (type === "moof") moof = [s, body, e];
    else if (type === "mdat" && moof) { ccSamples(st, u, moof, body, e, offset); moof = null; }
  }
}
function ccSamples(st, u, [ms, mb, me], db, de, offset) {
  for (const [type, , tb, te] of ccBoxes(u, mb, me)) {
    if (type !== "traf") continue;
    let defDur = 0, defSize = 0, dts = 0, base = ms, next = null;
    const h = ccChild(u, tb, te, ["tfhd"]);
    if (h) {
      const f = u32(u, h[0]) & 0xffffff;
      let i = h[0] + 8;
      if (f & 1) { base = u64(u, i); i += 8; }                  // base_data_offset (into what was appended)
      if (f & 2) i += 4;
      if (f & 8) { defDur = u32(u, i); i += 4; }
      if (f & 0x10) defSize = u32(u, i);
    }
    const d = ccChild(u, tb, te, ["tfdt"]);
    if (d) dts = u[d[0]] === 1 ? u64(u, d[0] + 4) : u32(u, d[0] + 4);
    for (const [rt, , rb] of ccBoxes(u, tb, te)) {
      if (rt !== "trun") continue;
      const f = u32(u, rb) & 0xffffff, n = u32(u, rb + 4);
      // without a data_offset a run carries on where the last one ended (the first starts in the mdat)
      let i = rb + 8, pos = next === null ? db : next;
      if (f & 1) { pos = base + (u32(u, i) | 0); i += 4; }
      if (f & 4) i += 4;
      for (let k = 0; k < n; k++) {
        let dur = defDur, size = defSize, cts = 0;
        if (f & 0x100) { dur = u32(u, i); i += 4; }
        if (f & 0x200) { size = u32(u, i); i += 4; }
        if (f & 0x400) i += 4;
        if (f & 0x800) { cts = u32(u, i) | 0; i += 4; }
        if (pos + size > de) return;
        const t = (dts + cts) / st.scale + offset;
        for (let p = pos; p + 4 < pos + size;) {             // length-prefixed NAL units
          const len = u32(u, p), nal = p + 4;
          if (!len || nal + len > pos + size) break;
          const sei = st.hevc ? ((u[nal] >> 1) & 0x3f) === 39 : (u[nal] & 0x1f) === 6;
          if (sei) ccSei(st, u.subarray(nal + (st.hevc ? 2 : 1), nal + len), t);
          p = nal + len;
        }
        pos += size; dts += dur;
      }
      next = pos;
    }
  }
}
// SEI messages: user_data_registered_itu_t_t35 (4) from ATSC (B5 0031 "GA94"), user data type 3 = cc_data
function ccSei(st, raw, t) {
  const r = [];
  for (let i = 0, z = 0; i < raw.length; i++) {          // drop emulation-prevention bytes (00 00 03)
    if (z >= 2 && raw[i] === 3) { z = 0; continue; }
    z = raw[i] ? 0 : z + 1; r.push(raw[i]);
  }
  for (let i = 0; i + 2 < r.length && r[i] !== 0x80;) {
    let type = 0, size = 0;
    while (r[i] === 0xff) { type += 255; i++; } type += r[i++];
    while (r[i] === 0xff) { size += 255; i++; } size += r[i++];
    if (type === 4 && r[i] === 0xb5 && r[i + 1] === 0 && r[i + 2] === 0x31 && r[i + 3] === 0x47 && r[i + 4] === 0x41
        && r[i + 5] === 0x39 && r[i + 6] === 0x34 && r[i + 7] === 3 && r[i + 8] & 0x40) {
      const n = r[i + 8] & 0x1f;
      for (let k = 0, j = i + 10; k < n && j + 2 < i + size; k++, j += 3) {
        if ((r[j] & 7) !== 4) continue;                      // cc_valid, NTSC field 1 (CC1 / CC2)
        const a = r[j + 1] & 0x7f, b = r[j + 2] & 0x7f;
        if (!a && !b) continue;                              // padding
        let at = st.q.length;                                // by presentation time (B-frames come out of order)
        while (at && st.q[at - 1][0] > t) at--;
        st.q.splice(at, 0, [t, a, b]);
        st.any = true;
      }
    }
    i += size;
  }
  if (st.q.length > 20000) st.q.splice(0, st.q.length - 20000);
}

// ---- CEA-608: 15 rows of 32 characters, a displayed and a hidden page
const CC_CHARS = { 0x2a: "á", 0x5c: "é", 0x5e: "í", 0x5f: "ó", 0x60: "ú", 0x7b: "ç", 0x7c: "÷", 0x7d: "Ñ", 0x7e: "ñ", 0x7f: "█" };
const CC_SPECIAL = Array.from("®°½¿™¢£♪à èâêîôû");
const CC_EXT = { 0x12: Array.from("ÁÉÓÚÜü‘¡*’—©℠•“”ÀÂÇÈÊËëÎÏïÔÙùÛ«»"), 0x13: Array.from("ÃãÍÌìÒòÕõ{}\\^_|~ÄäÖöß¥¤¦ÅåØø┌┐└┘") };
const CC_PAC = { 0x11: [0, 1], 0x12: [2, 3], 0x15: [4, 5], 0x16: [6, 7], 0x17: [8, 9], 0x10: [10, 10], 0x13: [11, 12], 0x14: [13, 14] };
function ccDecoder() {
  const row = () => new Array(32).fill(" "), page = () => Array.from({ length: 15 }, row);
  const d = { mode: "pop", disp: page(), back: page(), row: 14, col: 0, roll: 2, chan: 1, last: -1, html: null };
  const mem = () => (d.mode === "pop" ? d.back : d.disp);
  const put = (c) => { if (d.mode === "text") return; mem()[d.row][Math.min(d.col, 31)] = c; d.col = Math.min(d.col + 1, 32); d.html = null; };
  const cr = () => {
    const top = d.row - d.roll + 1;
    for (let r = Math.max(0, top - 1); r < d.row; r++) d.disp[r] = r < top ? row() : d.disp[r + 1];
    d.disp[d.row] = row(); d.col = 0; d.html = null;
  };
  function misc(b) {
    switch (b) {
      case 0x20: d.mode = "pop"; break;                                          // resume caption loading
      case 0x21: if (d.col > 0) mem()[d.row][--d.col] = " "; break;              // backspace
      case 0x24: mem()[d.row].fill(" ", d.col); break;                           // delete to end of row
      case 0x25: case 0x26: case 0x27:                                           // roll-up, 2-4 rows
        if (d.mode !== "roll") { d.disp = page(); d.back = page(); d.row = 14; d.col = 0; }
        d.mode = "roll"; d.roll = b - 0x23; d.row = Math.max(d.row, d.roll - 1); break;
      case 0x29: d.mode = "paint"; break;                                        // resume direct captioning
      case 0x2a: case 0x2b: d.mode = "text"; break;                              // text service: not captions
      case 0x2c: d.disp = page(); break;                                         // erase displayed
      case 0x2d: if (d.mode === "roll") cr(); else { d.row = Math.min(14, d.row + 1); d.col = 0; } break;
      case 0x2e: d.back = page(); break;                                         // erase hidden
      case 0x2f: [d.disp, d.back] = [d.back, d.disp]; d.mode = "pop"; break;     // end of caption: flip
    }
    d.html = null;
  }
  function pac(c, b) {
    const r = CC_PAC[c][b & 0x20 ? 1 : 0];
    if (d.mode === "roll" && r !== d.row) {                                      // the roll-up window moves
      const to = Math.max(r, d.roll - 1), rows = [];
      for (let k = 0; k < d.roll; k++) rows.push(d.disp[d.row - k] || row());
      d.disp = page();
      rows.forEach((x, k) => { if (to - k >= 0) d.disp[to - k] = x; });
      d.row = to;
    } else if (d.mode !== "roll") d.row = r;
    d.col = b & 0x10 ? ((b >> 1) & 7) * 4 : 0;
    d.html = null;
  }
  d.pair = (a, b) => {
    if (a >= 0x10 && a <= 0x1f) {
      const key = (a << 8) | b;
      if (key === d.last) { d.last = -1; return; }                               // control codes are sent twice
      d.last = key; d.chan = a & 8 ? 2 : 1;
      if (d.chan !== 1) return;
      const c = a & 0xf7;
      if ((c === 0x14 || c === 0x15) && b >= 0x20 && b <= 0x2f) misc(b);
      else if (c === 0x17 && b >= 0x21 && b <= 0x23) d.col = Math.min(31, d.col + b - 0x20);
      else if (c === 0x11 && b >= 0x20 && b <= 0x2f) put(" ");                   // mid-row style change
      else if (c === 0x11 && b >= 0x30 && b <= 0x3f) put(CC_SPECIAL[b - 0x30]);
      else if ((c === 0x12 || c === 0x13) && b >= 0x20 && b <= 0x3f) { if (d.col > 0) d.col--; put(CC_EXT[c][b - 0x20]); }
      else if (b >= 0x40 && CC_PAC[c]) pac(c, b);
      return;
    }
    d.last = -1;
    if (d.chan !== 1 || (a && a < 0x20)) return;
    if (a >= 0x20) put(CC_CHARS[a] || String.fromCharCode(a));
    if (b >= 0x20) put(CC_CHARS[b] || String.fromCharCode(b));
  };
  d.clear = () => { d.disp = page(); d.html = null; };
  // the rows on screen, top to bottom, drawn like On Demand subtitles: centred lines in one box. Captions the
  // captioner put in the top half (out of the way of a score or a name on screen) stay at the top.
  d.render = () => {
    if (d.html !== null) return d.html;
    const lines = [];
    let last = -1;
    d.disp.forEach((cells, r) => { const text = cells.join("").trim(); if (text) { lines.push(esc(text)); last = r; } });
    d.top = last >= 0 && last < 7;
    return (d.html = lines.length ? `<span>${lines.join("<br>")}</span>` : "");
  };
  return d;
}

// ---- on screen
function ccSource() {
  const o = video.srcObject;
  const ms = o && o._tbcc ? o : video.src ? CC.url.get(video.src) : null;
  return ms && ms._tbcc;
}
function ccTick() {
  const el = $("vcc");
  let html = "";
  const st = CC.on && !S.mv.tiles.length && !document.hidden ? ccSource() : null;
  if (st) {
    if (CC.cur !== st) { CC.cur = st; st.dec = ccDecoder(); st.t = -1; }
    const t = video.currentTime;
    if (t < st.t - 1) st.dec = ccDecoder();                                     // went back: start clean
    st.t = t;
    let i = 0, last = st.lastAt || 0;
    const live = () => st.dec.mode === "roll" || st.dec.mode === "paint";
    while (i < st.q.length && st.q[i][0] <= t + 0.03 + (CC.early && live() ? CC.early : 0)) {
      st.dec.pair(st.q[i][1], st.q[i][2]); last = st.q[i][0]; i++;
    }
    if (i) { st.q.splice(0, i); st.lastAt = last; }
    if (t - (st.lastAt || t) > 30 + CC.early) st.dec.clear();                              // nothing new for a while
    // On Demand subtitles win, between their lines too
    html = !(S.mode === "vod" && S.vod && S.vod.sub) && $("vsubs").classList.contains("hidden") ? st.dec.render() : "";
    // tell them once when the channel (or title) has none
    if (!st.any && !st.warned && !video.paused && video.readyState >= 3 && (st.played += 0.1) > 20) {
      st.warned = true; toast("No closed captions on this channel right now", 4000);
    }
  }
  if (el.dataset.h !== html) { el.dataset.h = html; el.innerHTML = html; }
  el.classList.toggle("hidden", !html);
  el.classList.toggle("top", !!(html && st && st.dec.top));
  // above the channel banner while it shows (its height changes with the description)
  let lift = "";
  const banner = $("banner");    // CSS can hide it without .hidden (the on-screen live remote), so ask what's drawn
  if (html && !el.classList.contains("top") && !banner.classList.contains("hidden") && banner.getClientRects().length) {
    const chan = banner.querySelector(".b-chan"), box = el.parentElement.getBoundingClientRect();
    if (chan && chan.getClientRects().length) lift = `${Math.max(0, Math.round(box.bottom - chan.getBoundingClientRect().top + 12))}px`;
  }
  if (el.style.bottom !== lift) el.style.bottom = lift;
}
setInterval(ccTick, 100);
// Safari's own HLS player: it reads the captions itself and lists them as text tracks (CC1, CC3 …). Show one,
// CC1 / English if it's there, and leave subtitle tracks to whatever picked them.
function ccNative() {
  const caps = Array.from(video.textTracks || []).filter((t) => t.kind === "captions");
  const pick = CC.on && (caps.find((t) => /cc1/i.test(t.label || "") || /cc1/i.test(t.id || ""))
    || caps.find((t) => /^en/i.test(t.language || "")) || caps[0]);
  for (const t of caps) { const m = t === pick ? "showing" : "disabled"; if (t.mode !== m) t.mode = m; }
}
if (video.textTracks) video.textTracks.addEventListener("addtrack", ccNative);
function toggleCaptions() {
  CC.on = !CC.on;
  try { localStorage.setItem("tb.cc", CC.on ? "1" : "0"); } catch (_) {}
  if (!CC.on) {
    for (const ms of CC.url.values()) if (ms._tbcc) ms._tbcc.q = [];
    const o = video.srcObject; if (o && o._tbcc) o._tbcc.q = [];
    CC.cur = null;
  }
  ccNative(); ccTick();
  toast(`Closed captions: ${CC.on ? "On (they start in a few seconds)" : "Off"}`, 2500);
}
const ccEarlyText = () => (CC.early ? `${CC.early} s early` : "Off");
function ccEarlyDialog() {
  openDialog("Live captions early", "Live shows and sports are captioned by a person typing as they listen, so the words come a few "
    + "seconds after the voices. This shows those scrolling captions sooner. Captions made ahead of time aren't moved. "
    + "It can only go as far ahead as the video already loaded; a Stream cache gives it more room.",
  CC_EARLY.map((s) => ({ label: `${s ? s + " seconds" : "Off: as the channel sends them"}${CC.early === s ? "  ✓" : ""}`,
    act: () => {
      CC.early = s;
      try { localStorage.setItem("tb.ccEarly", String(s)); } catch (_) {}
      closeDialog(); toast(`Live captions early: ${ccEarlyText()}`, 2500);
    } })));
}
