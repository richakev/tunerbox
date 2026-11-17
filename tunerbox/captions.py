"""Closed captions for live TV (Menu › Captions, or the CC / C key).

First choice: the broadcaster's own captions. US channels carry CEA-608 captions inside the video
(H.264 / HEVC SEI messages, MPEG-2 user data), and most providers pass them through. They're read
here straight from the shared tuner's stream (no decoding of the picture, so it costs next to
nothing) and turned into what a TV's caption decoder would show, frame by frame.

Fallback: a channel that has none gets speech-to-text from its audio (faster-whisper, when it's
installed: the Docker image has it). That runs a few seconds behind, so a screen using it plays the
channel about 10 seconds behind live and the words still line up with the picture.

Timing: every caption carries the presentation time (PTS) of the frame it belongs to. A screen gets
the PTS its own stream started from (tuner.Subscriber.base, what mpegts.js counts video.currentTime
from), so `pts - base` is the moment on the screen's own clock, however far behind live it plays.

One Captioner per running tuner, started when a screen on that tuner asks for captions and stopped
when nobody has asked for a while. It reads the tuner as a tap: it never keeps a tuner (and so a
provider connection) running on its own."""
import difflib
import heapq
import importlib.util
import logging
import os
import queue
import re
import threading
import time
from collections import deque

from . import config

log = logging.getLogger("tunerbox.captions")
TS = 188
IDLE = 20                       # seconds without a screen asking before a captioner stops
KEEP_MS = 150_000               # captions kept for screens that play behind live
NO_CC_AFTER_MS = 12_000         # this much stream with no broadcaster captions: speech-to-text instead
CC_GONE_MS = 20_000             # broadcaster captions this long silent: speech-to-text may take over
AI_DELAY = 10                   # seconds behind live a screen plays when its captions come from speech
REORDER = 10                    # frames held to put B-frames back in display order
SYNC_WINDOW = 25                # seconds of speech matched against the captions to time them
SYNC_SLACK = 12                 # ...once this much more has come (the captions for its last words)
SYNC_EVERY = 300                # seconds between timings of a channel's captions (they can drift)
MAX_SHIFT = 12_000              # captions are never moved earlier than this

_shifts = {}                    # channel URL -> (ms the captions run behind the speech, when measured)


# ---------------------------------------------------------------- PES timestamps
def _pes_ts(p, i):
    """(pts, dts) in 90 kHz ticks from a PES header starting at p[i] (00 00 01 sid ...), or None."""
    if len(p) < i + 14 or p[i + 6] & 0xC0 != 0x80:
        return None
    flags = p[i + 7] >> 6
    if not flags & 2:
        return None

    def ts(j):
        return ((p[j] & 0x0E) << 29) | (p[j + 1] << 22) | ((p[j + 2] & 0xFE) << 14) | (p[j + 3] << 7) | (p[j + 4] >> 1)
    pts = ts(i + 9)
    dts = ts(i + 14) if flags == 3 and len(p) >= i + 19 else pts
    return pts, dts


def _payload(pkt):
    """Offset of the payload in a TS packet, or None if it has none."""
    afc = (pkt[3] >> 4) & 3
    if not afc & 1:
        return None
    off = 4 + (1 + pkt[4] if afc & 2 else 0)
    return off if off < TS else None


class BaseFinder:
    """The timestamp a player starts its clock from: mpegts.js counts video.currentTime from the
    earliest first-sample DTS of the audio and video it was sent. Fed a viewer's first chunks."""

    def __init__(self):
        self.first = {}                     # pid -> dts of its first whole PES
        self.seen = 0

    def feed(self, data):
        """Returns the base in ms once known, else None."""
        for off in range(0, len(data) - TS + 1, TS):
            pkt = data[off:off + TS]
            if pkt[0] != 0x47 or not pkt[1] & 0x40:
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pid in self.first:
                continue
            o = _payload(pkt)
            if o is None or pkt[o:o + 3] != b"\x00\x00\x01":
                continue
            sid = pkt[o + 3]
            if not (0xC0 <= sid <= 0xEF or sid == 0xBD):
                continue
            t = _pes_ts(pkt, o)
            if t:
                self.first[pid] = t[1]
        self.seen += len(data)
        if len(self.first) >= 2 or (self.first and self.seen > 1_500_000) or self.seen > 4_000_000:
            return min(self.first.values()) // 90 if self.first else -1
        return None


# ---------------------------------------------------------------- CEA-608
_STD = {0x2A: "á", 0x5C: "é", 0x5E: "í", 0x5F: "ó", 0x60: "ú", 0x7B: "ç", 0x7C: "÷", 0x7D: "Ñ", 0x7E: "ñ", 0x7F: "█"}
_SPECIAL = "®°½¿™¢£♪à èâêîôû"
_EXT1 = "ÁÉÓÚÜü‘¡*'—©℠•“”ÀÂÇÈÊËëÎÏïÔÙùÛ«»"
_EXT2 = "ÃãÍÌìÒòÕõ{}\\^_|~ÄäÖöß¥¤│ÅåØø┌┐└┘"
_PAC_ROWS = {0x11: (0, 1), 0x12: (2, 3), 0x15: (4, 5), 0x16: (6, 7), 0x17: (8, 9), 0x10: (10, 10),
             0x13: (11, 12), 0x14: (13, 14)}
ROWS, COLS = 15, 32


class Cea608:
    """A CEA-608 caption decoder for CC1: pop-on, roll-up and paint-on, as a TV draws them."""

    def __init__(self):
        self.disp = self._blank()
        self.back = self._blank()           # pop-on captions are built here, then swapped in
        self.mode = None                    # "pop" | "roll" | "paint" | "text" (ignored)
        self.rollup = 2
        self.row, self.col = ROWS - 1, 0
        self.chan = 1
        self.last_ctrl = None
        self.big = False                    # something structural changed (show it at once)

    @staticmethod
    def _blank():
        return [[" "] * COLS for _ in range(ROWS)]

    def screen(self):
        """What's on screen: (lines, top). top: the captions sit in the upper half (moved out of the
        way of something on the picture)."""
        rows = [(i, "".join(r).strip()) for i, r in enumerate(self.disp)]
        rows = [(i, t) for i, t in rows if t]
        return [t for _, t in rows], bool(rows) and rows[-1][0] < 7

    def _mem(self):
        return self.back if self.mode == "pop" else self.disp

    def _put(self, ch):
        if self.mode in (None, "text") or self.chan != 1:
            return
        m = self._mem()
        m[self.row][self.col] = ch
        self.col = min(self.col + 1, COLS - 1)

    def _roll(self):
        """Carriage return in roll-up: the window moves up a row, the bottom row starts empty."""
        top = max(0, self.row - self.rollup + 1)
        for r in range(0, top):
            self.disp[r] = [" "] * COLS
        for r in range(top, self.row):
            self.disp[r] = self.disp[r + 1]
        self.disp[self.row] = [" "] * COLS
        self.col = 0

    def pair(self, b1, b2):
        """One byte pair (parity already stripped)."""
        if not b1 and not b2:
            return False
        if 0x10 <= b1 <= 0x1F:
            if (b1, b2) == self.last_ctrl:          # control codes are sent twice: act once
                self.last_ctrl = None
                return False
            self.last_ctrl = (b1, b2)
            self.chan = 2 if b1 & 0x08 else 1
            if self.chan == 1:
                self._control(b1 & 0xF7, b2)
            return True
        self.last_ctrl = None
        for b in (b1, b2):
            if b >= 0x20:
                self._put(_STD.get(b, chr(b)))
        return True

    def _control(self, b1, b2):
        if b1 in (0x14, 0x15) and 0x20 <= b2 <= 0x2F:
            self._misc(b2)
        elif b1 == 0x17 and 0x21 <= b2 <= 0x23:     # tab offsets
            self.col = min(self.col + (b2 - 0x20), COLS - 1)
        elif b1 == 0x11 and 0x30 <= b2 <= 0x3F:
            self._put(_SPECIAL[b2 - 0x30])
        elif b1 == 0x11 and 0x20 <= b2 <= 0x2F:     # mid-row style change: shows as a space
            self._put(" ")
        elif b1 in (0x12, 0x13) and 0x20 <= b2 <= 0x3F:
            # extended character: it replaces the plain one sent before it (for older decoders)
            if self.mode not in (None, "text") and self.col > 0:
                self.col -= 1
            self._put((_EXT1 if b1 == 0x12 else _EXT2)[b2 - 0x20])
        elif b1 in _PAC_ROWS and 0x40 <= b2 <= 0x7F:
            self._pac(b1, b2)

    def _pac(self, b1, b2):
        row = _PAC_ROWS[b1][1 if b2 & 0x20 else 0]
        if self.mode == "roll" and row != self.row:
            # the roll-up window moves: take its lines along
            n = self.rollup
            old = [self.disp[r] for r in range(max(0, self.row - n + 1), self.row + 1)]
            self.disp = self._blank()
            for k, line in enumerate(reversed(old)):
                if row - k >= 0:
                    self.disp[row - k] = line
            self.big = True
        self.row = row
        self.col = ((b2 & 0x0E) >> 1) * 4 if b2 & 0x10 else 0

    def _misc(self, c):
        if c == 0x20:                               # RCL: pop-on
            self.mode = "pop"
        elif c == 0x21:                             # BS
            if self.mode not in (None, "text") and self.col > 0:
                self.col -= 1
                self._mem()[self.row][self.col] = " "
        elif c == 0x24:                             # DER: delete to end of row
            m = self._mem()
            for k in range(self.col, COLS):
                m[self.row][k] = " "
        elif c in (0x25, 0x26, 0x27):               # RU2-4: roll-up
            if self.mode != "roll":
                self.disp, self.back = self._blank(), self._blank()
                self.row, self.col = ROWS - 1, 0
                self.big = True
            self.mode, self.rollup = "roll", c - 0x23
        elif c == 0x29:                             # RDC: paint-on
            self.mode = "paint"
        elif c in (0x2A, 0x2B):                     # TR / RTD: text service, not captions
            self.mode = "text"
        elif c == 0x2C:                             # EDM: clear the screen
            self.disp = self._blank()
            self.big = True
        elif c == 0x2D:                             # CR
            if self.mode == "roll":
                self._roll()
                self.big = True
            elif self.mode == "paint":
                self.row, self.col = min(self.row + 1, ROWS - 1), 0
        elif c == 0x2E:                             # ENM
            self.back = self._blank()
        elif c == 0x2F:                             # EOC: show the pop-on caption
            self.disp, self.back = self.back, self.disp
            self.mode = "pop"
            self.big = True


# ---------------------------------------------------------------- caption data in the video
def _rbsp(b):
    return b.replace(b"\x00\x00\x03", b"\x00\x00")


def _cc_block(c, out):
    """cc_data() of an ATSC A/53 caption block: field-1 (CC1/CC2) byte pairs onto `out`."""
    if len(c) < 2 or not c[0] & 0x40:
        return
    n = c[0] & 0x1F
    for k in range(n):
        j = 2 + 3 * k
        if j + 3 > len(c):
            break
        if c[j] & 0x04 and c[j] & 0x03 == 0:
            out.append((c[j + 1] & 0x7F, c[j + 2] & 0x7F))


def _t35(p, out):
    if len(p) >= 8 and p[0] == 0xB5 and p[1:3] == b"\x00\x31" and p[3:7] == b"GA94" and p[7] == 0x03:
        _cc_block(p[8:], out)


def _sei(rbsp, out):
    i = 0
    while i + 2 <= len(rbsp) and rbsp[i] != 0x80:
        t = 0
        while i < len(rbsp) and rbsp[i] == 0xFF:
            t += 255
            i += 1
        if i >= len(rbsp):
            return
        t += rbsp[i]
        i += 1
        s = 0
        while i < len(rbsp) and rbsp[i] == 0xFF:
            s += 255
            i += 1
        if i >= len(rbsp):
            return
        s += rbsp[i]
        i += 1
        if t == 4:
            _t35(rbsp[i:i + s], out)
        i += s


def cc_pairs(es, codec):
    """CEA-608 field-1 byte pairs in one frame's video. codec: "h264" | "hevc" | "mpeg2"."""
    out = []
    starts = []
    i = es.find(b"\x00\x00\x01")
    while i >= 0:
        starts.append(i + 3)
        i = es.find(b"\x00\x00\x01", i + 3)
    for k, s in enumerate(starts):
        if s >= len(es):
            break
        end = starts[k + 1] - 3 if k + 1 < len(starts) else len(es)
        b = es[s]
        if codec == "h264":
            t = b & 0x1F
            if t == 6:
                _sei(_rbsp(es[s + 1:end]), out)
            elif t in (1, 5):               # the picture itself: the captions came before it
                break
        elif codec == "hevc":
            t = (b >> 1) & 0x3F
            if t == 39:
                _sei(_rbsp(es[s + 2:end]), out)
            elif t < 32:
                break
        else:                               # MPEG-2: user data start code 0xB2
            if b == 0xB2 and es[s + 1:s + 6] == b"GA94\x03":
                _cc_block(es[s + 6:end], out)
    return out


_CODECS = {0x1B: "h264", 0x24: "hevc", 0x02: "mpeg2", 0x01: "mpeg2"}


class Extractor:
    """MPEG-TS in, caption screens out: [(pts_ms, lines, top, style)] whenever what's shown changes.
    style: "pop" (prerecorded captions, shown whole), "roll" or "paint" (typed live)."""

    def __init__(self):
        self.pmt = set()
        self.vpid, self.codec = None, None
        self.pes, self.pes_pts = bytearray(), None
        self.heap, self.n = [], 0
        self.dec = Cea608()
        self.shown, self.shown_at = ([], False), None
        self.first_ms = self.last_ms = self.active_ms = None
        self.carry = b""

    def feed(self, data):
        out = []
        data = self.carry + data
        n = len(data) - len(data) % TS
        self.carry = data[n:]
        for off in range(0, n, TS):
            pkt = data[off:off + TS]
            if pkt[0] != 0x47:
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pid == self.vpid:
                self._video(pkt, out)
            elif pid == 0 or pid in self.pmt:
                self._psi(pid, pkt)
        return out

    def _psi(self, pid, pkt):
        o = _payload(pkt)
        if o is None or not pkt[1] & 0x40:
            return
        p = o + 1 + pkt[o]
        if p + 12 > TS:
            return
        end = min(p + 3 + (((pkt[p + 1] & 0x0F) << 8) | pkt[p + 2]) - 4, TS)
        if pid == 0:
            pids = set()
            for i in range(p + 8, end - 3, 4):
                if (pkt[i] << 8) | pkt[i + 1]:
                    pids.add(((pkt[i + 2] & 0x1F) << 8) | pkt[i + 3])
            if pids:
                self.pmt = pids
            return
        i = p + 12 + (((pkt[p + 10] & 0x0F) << 8) | pkt[p + 11])
        while i + 5 <= end:
            if pkt[i] in _CODECS:
                vpid = ((pkt[i + 1] & 0x1F) << 8) | pkt[i + 2]
                if vpid != self.vpid:
                    self.vpid, self.codec = vpid, _CODECS[pkt[i]]
                    self.pes, self.pes_pts = bytearray(), None
                return
            i += 5 + (((pkt[i + 3] & 0x0F) << 8) | pkt[i + 4])

    def _video(self, pkt, out):
        o = _payload(pkt)
        if o is None:
            return
        if pkt[1] & 0x40:                           # a new frame starts
            self._frame(out)
            t = _pes_ts(pkt, o) if pkt[o:o + 3] == b"\x00\x00\x01" else None
            if t is None:
                self.pes, self.pes_pts = bytearray(), None
                return
            self.pes_pts = t[0]
            self.pes = bytearray(pkt[o + 9 + pkt[o + 8]:])
        elif self.pes_pts is not None and len(self.pes) < 48_000:   # the captions are near the start
            self.pes += pkt[o:]

    def _frame(self, out):
        if self.pes_pts is None:
            return
        pairs = cc_pairs(bytes(self.pes), self.codec)
        self.pes, ms = bytearray(), self.pes_pts // 90
        self.pes_pts = None
        self.n += 1
        heapq.heappush(self.heap, (ms, self.n, pairs))
        while len(self.heap) > REORDER:
            self._show(*heapq.heappop(self.heap)[::2], out)

    def flush(self):
        out = []
        self._frame(out)
        while self.heap:
            self._show(*heapq.heappop(self.heap)[::2], out)
        return out

    def _show(self, ms, pairs, out):
        if self.first_ms is None:
            self.first_ms = ms
        self.last_ms = ms
        for b1, b2 in pairs:
            if self.dec.pair(b1, b2):
                self.active_ms = ms
        scr = self.dec.screen()
        if scr != self.shown:
            # typed-out text (roll-up, paint-on) is sent a few times a second rather than every frame
            if self.dec.big or self.shown_at is None or ms - self.shown_at >= 250 or not scr[0]:
                out.append((ms, *scr, self.dec.mode or "pop"))
                self.shown, self.shown_at = scr, ms
                self.dec.big = False


# ---------------------------------------------------------------- speech-to-text
_model = None
_model_lock = threading.Lock()
_infer_lock = threading.Lock()
_model_err = ""


def speech_available():
    return importlib.util.find_spec("faster_whisper") is not None and importlib.util.find_spec("av") is not None


def _load_model():
    """The speech model, loaded once (and downloaded the first time, into the data folder)."""
    global _model, _model_err
    with _model_lock:
        if _model is None and not _model_err:
            try:
                from faster_whisper import WhisperModel
                name = config.get("cc_ai_model", "base.en") or "base.en"
                root = config.DATA / "whisper"
                root.mkdir(parents=True, exist_ok=True)
                os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
                log.info("Loading speech model %s", name)
                _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(root),
                                      cpu_threads=max(1, min(4, os.cpu_count() or 2)))
            except Exception as e:      # noqa: BLE001 - no network, a bad model name, a missing library
                _model_err = str(e) or e.__class__.__name__
                log.warning("Speech captions unavailable: %s", _model_err)
        return _model


class _Pipe:
    """A blocking file for PyAV to read the stream from."""

    def __init__(self):
        self.q = queue.Queue(maxsize=2000)
        self.buf = b""
        self.closed = False

    def write(self, b):
        try:
            self.q.put_nowait(b)
        except queue.Full:              # speech-to-text fell far behind: a gap rather than a stall
            pass

    def close(self):
        self.closed = True
        try:
            self.q.put_nowait(b"")
        except queue.Full:
            pass

    def read(self, n=-1):
        while not self.buf:
            if self.closed and self.q.empty():
                return b""
            b = self.q.get()
            if not b:
                self.closed = True
                return b""
            self.buf = b
        n = len(self.buf) if n is None or n < 0 else n
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _split(text, start, end, words=12):
    """A long line as shorter ones (sentence by sentence, then at most `words` words), each given its
    share of the time."""
    parts = []
    for sentence in re.split(r"(?<=[.?!])\s+", text.strip()):
        w = sentence.split()
        n = max(1, -(-len(w) // words))                 # pieces of about equal length
        size = -(-len(w) // n) if w else 1
        parts += [" ".join(w[i:i + size]) for i in range(0, len(w), size)]
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return [(start, end, text.strip())]
    total = sum(len(p) for p in parts)
    out, t = [], start
    for p in parts:
        d = (end - start) * len(p) / total
        out.append((t, t + d, p))
        t += d
    return out


class Speech(threading.Thread):
    """The tuner's audio, for two jobs: speech-to-text on a channel without broadcaster captions, and
    timing the broadcaster's captions on one with them (live captions are typed as people speak, so
    they come out seconds after the words: matching them against the speech says how many)."""

    def __init__(self, cap):
        super().__init__(daemon=True, name="cc-speech")
        self.cap = cap
        self.pipe = _Pipe()
        self.ready = False
        self.model = None

    def _model(self):
        if self.model is None and not self.cap.speech_failed:
            self.model = _load_model()
            if self.model is None:
                self.cap.speech_failed = True
            self.ready = self.model is not None
        return self.model

    def run(self):
        try:
            import av
            import numpy as np
        except ImportError:
            self.cap.speech_failed = True
            return
        try:
            container = av.open(self.pipe, format="mpegts", options={"probesize": "1000000", "analyzeduration": "2000000"})
            stream = next(s for s in container.streams if s.type == "audio")
        except Exception as e:          # noqa: BLE001 - no audio, or a stream PyAV can't read
            log.warning("Speech captions: can't read the stream's audio: %s", e)
            self.cap.speech_failed = True
            return
        rs = av.AudioResampler(format="flt", layout="mono", rate=16000)
        chunks, start_ms, have, prev = [], None, 0, ""      # speech-to-text, piece by piece
        roll, roll_ms, roll_n = [], None, 0                 # recent audio, for timing the captions
        try:
            for packet in container.demux(stream):
                if self.cap.stopped:
                    break
                try:
                    frames = packet.decode()
                except Exception:       # noqa: BLE001 - a damaged packet
                    continue
                for f in frames:
                    if f.pts is None:
                        continue
                    ms = int(f.pts * f.time_base * 1000)
                    if start_ms is not None and abs(ms - (start_ms + have / 16)) > 800:     # a jump in the stream
                        chunks, start_ms, have = [], None, 0
                    if roll_ms is not None and abs(ms - (roll_ms + roll_n / 16)) > 800:
                        roll, roll_ms, roll_n = [], None, 0
                    if start_ms is None:
                        start_ms = ms
                    if roll_ms is None:
                        roll_ms = ms
                    for r in rs.resample(f):
                        a = r.to_ndarray().reshape(-1).astype(np.float32)
                        chunks.append(a)
                        have += len(a)
                        roll.append(a)
                        roll_n += len(a)
                st = self.cap.state_now()
                if st == "cc":                              # nothing to transcribe
                    chunks, start_ms, have = [], None, 0
                elif st == "none" and have >= 16000 * 6:
                    if self._model() is None:
                        break
                    audio = np.concatenate(chunks)
                    cut = _quiet_cut(audio, np)
                    prev = self._words(audio[:cut], start_ms, prev)
                    rest = audio[cut:]
                    chunks, have = [rest], len(rest)
                    start_ms += cut // 16
                # captions lag the audio: time a window once the captions for all of it have come
                if roll_n >= 16000 * (SYNC_WINDOW + SYNC_SLACK):
                    if st == "cc" and self.cap.sync_due() and self._model() is not None:
                        audio = np.concatenate(roll)[:16000 * SYNC_WINDOW]
                        self.cap.sync(self.model, audio, roll_ms)
                        roll, roll_ms, roll_n = [], None, 0
                    else:                                   # keep the last window's worth
                        audio = np.concatenate(roll)[-16000 * SYNC_WINDOW:]
                        roll_ms += (roll_n - len(audio)) // 16
                        roll, roll_n = [audio], len(audio)
        except Exception as e:          # noqa: BLE001
            if not self.cap.stopped:
                log.warning("Speech captions stopped: %s", e)
        finally:
            try:
                container.close()
            except Exception:           # noqa: BLE001
                pass

    def _words(self, audio, start_ms, prev):
        with _infer_lock:
            try:
                segs, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                                vad_parameters={"min_silence_duration_ms": 400},
                                                condition_on_previous_text=False, initial_prompt=prev[-200:] or None)
                segs = list(segs)
            except Exception as e:      # noqa: BLE001
                log.warning("Speech captions: %s", e)
                return prev
        lines = []
        for s in segs:
            text = s.text.strip()
            if not text or s.no_speech_prob > 0.6 or s.avg_logprob < -1.2:
                continue
            for a, b, t in _split(text, s.start, s.end):
                lines.append((start_ms + int(a * 1000), start_ms + int(b * 1000) + 800, t))
        self.cap.add_speech(lines)
        return (prev + " " + " ".join(t for _, _, t in lines)).strip()


def _norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def caption_words(screens):
    """[(ms, word)]: each word of the captions with the moment it first appeared on screen.
    screens: [(pts_ms, lines)] in time order. A word typed out over several screens counts from its
    first letters."""
    out, prev = [], []
    for ms, lines in screens:
        words = " ".join(lines).split()
        sm = difflib.SequenceMatcher(None, prev, words, autojunk=False)
        old = set()
        for a, b, n in sm.get_matching_blocks():
            old.update(range(b, b + n))
        for k, w in enumerate(words):
            if k in old:
                continue
            if out and _norm(out[-1][1]) and _norm(w).startswith(_norm(out[-1][1])) and ms - out[-1][0] < 1500:
                out[-1] = (out[-1][0], w)           # the rest of a word that was still being typed
            else:
                out.append((ms, w))
        prev = words
    return out


def caption_lag(cap_words, speech_words):
    """How far (ms) the captions run behind the speech, from the words both have; None when too few
    match, or they disagree too much to trust."""
    a = [_norm(w) for _, w in cap_words]
    b = [_norm(w) for _, w in speech_words]
    lags = []
    for i, j, n in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        lags += [cap_words[i + k][0] - speech_words[j + k][0] for k in range(n) if len(a[i + k]) >= 3]
    if len(lags) < 10:
        return None
    lags.sort()
    lo, hi = lags[len(lags) // 10], lags[len(lags) * 9 // 10]
    if hi - lo > 3500:
        return None
    return lags[len(lags) // 2]


def _quiet_cut(audio, np):
    """Where to end a piece of audio: the quietest moment in its last 3 seconds (not mid-word)."""
    lo, hi = 16000 * 3, len(audio)
    if hi - lo < 1600:
        return hi
    win = 1600                          # 100 ms
    seg = audio[lo:lo + (hi - lo) // win * win].reshape(-1, win)
    k = int(np.argmin((seg ** 2).mean(axis=1)))
    return lo + k * win + win // 2


# ---------------------------------------------------------------- one per tuner
class Captioner(threading.Thread):
    def __init__(self, tuner):
        super().__init__(daemon=True, name="cc")
        self.tuner = tuner
        self.sub = tuner.tap("captions")
        self.ex = Extractor()
        self.lock = threading.Lock()
        self.cues = deque()                 # (seq, pts_ms, until_ms|None, lines, top, src, style)
        self.seq = 0
        self.last_ask = time.monotonic()
        self.stopped = False
        self.cc_seen = False
        self.speech = None
        self.speech_failed = False
        # per caption style: ms the broadcaster's captions run behind the speech, and when that was
        # measured. Live (roll-up) captions on a news channel lag; its prerecorded ads' captions don't.
        known = _shifts.get(tuner.url, ({}, {}))
        self.shifts, self.synced = dict(known[0]), dict(known[1])
        self.sync_tried = 0.0

    def run(self):
        while not self.stopped:
            try:
                chunk = self.sub.get(timeout=2)
            except queue.Empty:
                continue
            if not chunk:
                break
            shots = self.ex.feed(chunk)
            if shots:
                with self.lock:
                    for ms, lines, top, style in shots:
                        self._add(ms, None, lines, top, "cc", style)
                    self.cc_seen = self.cc_seen or any(shot[1] for shot in shots)
            self._audio(chunk)
        self.stop()

    def _audio(self, chunk):
        """The audio goes to speech-to-text from the start (it reads everything the tuner kept), whether
        it's needed to caption the channel or to time the captions it has."""
        if self.speech is None:
            if self.speech_failed or not self.speech_wanted():
                return
            self.speech = Speech(self)
            self.speech.start()
        self.speech.pipe.write(chunk)

    def sync_due(self):
        now = time.time()
        if now - self.sync_tried < 20:
            return False
        style = self.ex.dec.mode
        if style not in ("pop", "roll", "paint"):
            return False
        return style not in self.synced or now - self.synced[style] > SYNC_EVERY

    def sync(self, model, audio, start_ms):
        """Time the broadcaster's captions against `audio` (16 kHz, starting at start_ms)."""
        self.sync_tried = time.time()
        with _infer_lock:
            try:
                segs, _ = model.transcribe(audio, language="en", beam_size=1, word_timestamps=True, vad_filter=True,
                                           condition_on_previous_text=False)
                speech = [(start_ms + int(w.start * 1000), w.word.strip()) for seg in segs for w in (seg.words or [])]
            except Exception as e:      # noqa: BLE001
                log.warning("Caption timing: %s", e)
                return
        end = start_ms + len(audio) // 16
        style = self.ex.dec.mode or "pop"
        with self.lock:
            screens = [(c[1], c[3]) for c in self.cues
                       if c[5] == "cc" and c[6] == style and start_ms - 3000 <= c[1] <= end + SYNC_SLACK * 1000]
        lag = caption_lag(caption_words(screens), speech)
        if lag is None:
            log.info("Caption timing: not enough matching words yet")
            return
        # captions shown with the words (pop-on ones usually are already): only ever moved earlier
        shift = min(MAX_SHIFT, lag) if lag > 1000 else 0
        if abs(shift - self.shifts.get(style, -10_000)) > 400:
            log.info("Captions (%s) run %.1f s behind the speech: shown %.1f s earlier", style, lag / 1000, shift / 1000)
        self.shifts[style], self.synced[style] = shift, time.time()
        _shifts[self.tuner.url] = (dict(self.shifts), dict(self.synced))

    @staticmethod
    def speech_wanted():
        return bool(config.get("cc_ai", True)) and speech_available()

    def _add(self, ms, until, lines, top, src, style=""):
        self.seq += 1
        self.cues.append((self.seq, ms, until, lines, top, src, style))
        while self.cues and self.cues[-1][1] - self.cues[0][1] > KEEP_MS:
            self.cues.popleft()

    def add_speech(self, lines):
        with self.lock:
            for a, b, t in lines:
                self._add(a, b, [t], False, "ai", "ai")

    def state_now(self):
        """"cc": the broadcaster's captions; "none": the stream has had none for a while; "wait": too soon to say."""
        ex = self.ex
        if ex.last_ms is None:
            return "wait"
        if ex.active_ms is not None and ex.last_ms - ex.active_ms < CC_GONE_MS:
            return "cc"
        if ex.last_ms - ex.first_ms < NO_CC_AFTER_MS:
            return "wait"
        return "none"

    def status(self):
        st = self.state_now()
        if st != "none":
            return st
        if not self.speech_wanted() or self.speech_failed:
            return "none"
        return "ai" if self.speech and self.speech.ready else "ai-loading"

    def since(self, after):
        with self.lock:
            return [list(c) for c in self.cues if c[0] > after]

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.sub.close()
        if self.speech is not None:
            self.speech.pipe.close()
        with _lock:
            if _caps.get(id(self.tuner)) is self:
                del _caps[id(self.tuner)]


_caps = {}                              # id(tuner) -> Captioner
_lock = threading.Lock()
_reaper = None


def _reap():
    while True:
        time.sleep(5)
        now = time.monotonic()
        with _lock:
            old = [c for c in _caps.values() if now - c.last_ask > IDLE or c.tuner.ended]
        for c in old:
            c.stop()


def for_tuner(t):
    global _reaper
    with _lock:
        c = _caps.get(id(t))
        if c is None or c.stopped:
            if t.ended:
                return None
            c = _caps[id(t)] = Captioner(t)
            c.start()
            if _reaper is None:
                _reaper = threading.Thread(target=_reap, daemon=True, name="cc-reaper")
                _reaper.start()
        c.last_ask = time.monotonic()
        return c


def poll(client, after=0):
    """A screen's captions since `after` (a seq it got before). The screen shows a caption from
    (pts - base) on its own clock."""
    from . import hls, tuner
    sub = tuner.client_sub(client) if client else None
    if sub is None:
        if client and hls.live_client(client):
            return {"state": "hls"}         # Safari shows the broadcaster's captions itself
        return {"state": "idle"}
    c = for_tuner(sub.tuner)
    if c is None:
        return {"state": "idle"}
    st = c.status()
    if st in ("ai", "ai-loading") and not c.cc_seen:
        delay = AI_DELAY
    elif any(c.shifts.values()):    # captions shown earlier than they come: play far enough behind live
        delay = -(-max(c.shifts.values()) // 1000) + 3
    else:
        delay = 0
    return {"state": st, "id": id(c), "base": sub.base, "cues": c.since(after), "cc_seen": c.cc_seen,
            "shifts": c.shifts, "delay": delay}
