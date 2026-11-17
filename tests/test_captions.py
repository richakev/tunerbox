"""Live TV captions: the CEA-608 decoder, reading captions out of an MPEG-TS stream, the start time a
player counts from, and the tuner tap the captioner reads through."""
import time

from fastapi.testclient import TestClient

from tunerbox import captions, tuner
from tunerbox.captions import BaseFinder, Cea608, Extractor, cc_pairs

from test_tuner import _read, fake  # noqa: F401  (fixture)

V, A, PMT = 0x100, 0x101, 0x1000


def _odd(b):
    """A 608 byte with its odd-parity bit, as broadcast."""
    return b | (0x80 if bin(b).count("1") % 2 == 0 else 0)


def _ctrl(b1, b2):
    return [(b1, b2), (b1, b2)]                     # control codes go out twice


def _text(s):
    b = s.encode("latin-1")
    if len(b) % 2:
        b += b"\x00"
    return [(b[i], b[i + 1]) for i in range(0, len(b), 2)]


def _feed(dec, pairs):
    for p in pairs:
        dec.pair(*p)


# ---------------------------------------------------------------- 608 decoder
def test_pop_on_caption_shows_at_end_of_caption():
    d = Cea608()
    _feed(d, _ctrl(0x14, 0x20) + _ctrl(0x14, 0x70) + _text("HELLO THERE"))     # RCL, row 15, text
    assert d.screen() == ([], False)                # built off screen
    _feed(d, _ctrl(0x14, 0x2F))                     # EOC
    assert d.screen() == (["HELLO THERE"], False)
    _feed(d, _ctrl(0x14, 0x2C))                     # EDM
    assert d.screen() == ([], False)


def test_doubled_control_code_acts_once():
    d = Cea608()
    _feed(d, _ctrl(0x14, 0x29) + _ctrl(0x14, 0x70) + _text("AB"))              # paint-on
    _feed(d, _ctrl(0x14, 0x21))                     # one backspace, sent twice
    assert d.screen()[0] == ["A"]


def test_roll_up_keeps_its_rows_and_scrolls():
    d = Cea608()
    _feed(d, _ctrl(0x14, 0x25) + _ctrl(0x14, 0x70) + _text("ONE"))             # RU2
    _feed(d, _ctrl(0x14, 0x2D) + _text("TWO"))
    assert d.screen()[0] == ["ONE", "TWO"]
    _feed(d, _ctrl(0x14, 0x2D) + _text("THREE"))
    assert d.screen()[0] == ["TWO", "THREE"]        # two rows: the oldest one goes


def test_cc2_is_ignored():
    d = Cea608()
    _feed(d, _ctrl(0x14, 0x29) + _ctrl(0x14, 0x70) + _text("YES"))
    _feed(d, _ctrl(0x1C, 0x29) + _text("NO"))       # CC2 (another language): not ours
    assert d.screen()[0] == ["YES"]


def test_special_and_accented_characters():
    d = Cea608()
    _feed(d, _ctrl(0x14, 0x29) + _ctrl(0x14, 0x70))
    _feed(d, [(0x11, 0x37)])                        # ♪
    _feed(d, _text(" CAFE"))
    _feed(d, [(0x12, 0x21)])                        # É replaces the plain E before it
    _feed(d, _text(" ") + [(0x7E, 0x00)])           # ñ
    assert d.screen()[0] == ["♪ CAFÉ ñ"]


def test_captions_moved_to_the_top():
    d = Cea608()
    _feed(d, _ctrl(0x14, 0x29) + _ctrl(0x11, 0x40) + _text("UP HERE"))         # row 1
    assert d.screen() == (["UP HERE"], True)


# ---------------------------------------------------------------- captions in the video
def _sei(pairs):
    """An H.264 SEI NAL carrying ATSC A/53 caption data (with emulation prevention where needed)."""
    cc = bytes([0x40 | len(pairs), 0xFF]) + b"".join(bytes([0xFC, _odd(a), _odd(b)]) for a, b in pairs)
    t35 = b"\xb5\x00\x31GA94\x03" + cc + b"\xff"
    rbsp = bytes([4, len(t35)]) + t35 + b"\x80"
    ebsp = bytearray()
    zeros = 0
    for x in rbsp:
        if zeros >= 2 and x <= 3:
            ebsp.append(3)
            zeros = 0
        ebsp.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return b"\x00\x00\x00\x01\x06" + bytes(ebsp)


def _frame(pairs, key=False):
    aud = b"\x00\x00\x00\x01\x09\xf0"
    sl = b"\x00\x00\x00\x01" + (b"\x65" if key else b"\x41") + b"\x88" * 300
    return aud + (_sei(pairs) if pairs else b"") + sl


def test_cc_pairs_from_h264_sei():
    pairs = [(0x14, 0x20), (0x00, 0x00), (0x48, 0x49)]
    assert cc_pairs(_frame(pairs), "h264") == pairs


def _ts(pid, payload, start=False, cc=[0]):  # noqa: B006 - continuity counter shared on purpose
    out = b""
    first = True
    while payload or first:
        n = 184
        part, payload = payload[:n], payload[n:]
        b1 = (0x40 if start and first else 0) | (pid >> 8)
        if len(part) < 184:                         # stuff with an adaptation field
            pad = 184 - len(part) - 1
            af = bytes([pad]) + (b"\x00" + b"\xff" * (pad - 1) if pad else b"")
            out += bytes([0x47, b1, pid & 0xFF, 0x30 | (cc[0] & 0xF)]) + af + part
        else:
            out += bytes([0x47, b1, pid & 0xFF, 0x10 | (cc[0] & 0xF)]) + part
        cc[0] += 1
        first = False
    return out


def _pts_bytes(marker, t):
    return bytes([marker | ((t >> 29) & 0x0E) | 1, (t >> 22) & 0xFF, ((t >> 14) & 0xFE) | 1,
                  (t >> 7) & 0xFF, ((t << 1) & 0xFE) | 1])


def _pes(sid, es, pts, dts=None):
    if dts is None:
        hdr = bytes([0x80, 0x80, 5]) + _pts_bytes(0x20, pts)
    else:
        hdr = bytes([0x80, 0xC0, 10]) + _pts_bytes(0x30, pts) + _pts_bytes(0x10, dts)
    return b"\x00\x00\x01" + bytes([sid, 0, 0]) + hdr + es


PAT = _ts(0, bytes([0, 0, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xF0, 0x00, 0, 0, 0, 0]), start=True)
PMTP = _ts(PMT, bytes([0, 2, 0xB0, 23, 0, 1, 0xC1, 0, 0, 0xE1, 0x00, 0xF0, 0,
                       0x1B, 0xE1, 0x00, 0xF0, 0, 0x0F, 0xE1, 0x01, 0xF0, 0, 0, 0, 0, 0]), start=True)


def _stream(frames):
    """frames: [(pts_ms, pairs)] in decode order -> MPEG-TS."""
    out = PAT + PMTP
    for ms, pairs in frames:
        out += _ts(V, _pes(0xE0, _frame(pairs), ms * 90), start=True)
    return out


def test_extractor_reads_captions_in_display_order():
    caption = _ctrl(0x14, 0x20) + _ctrl(0x14, 0x70) + _text("NEWS AT TEN") + _ctrl(0x14, 0x2F)
    # decode order: I(1000) P(1100) B(1033) B(1066) ... the caption's last bytes sit in the B frames
    frames = [(1000, caption[:6]), (1100, []), (1033, caption[6:9]), (1066, caption[9:])]
    frames += [(1100 + 33 * k, []) for k in range(1, 20)]
    ex = Extractor()
    data = _stream(frames)
    shots = []
    for i in range(0, len(data), 1000):             # chunks that don't line up with packets
        shots += ex.feed(data[i:i + 1000])
    shots += ex.flush()
    assert ex.codec == "h264"
    assert shots == [(1066, ["NEWS AT TEN"], False, "pop")]
    assert ex.active_ms == 1066


def test_base_is_the_earliest_first_sample_of_audio_and_video():
    data = (PAT + PMTP + _ts(V, _pes(0xE0, _frame([], key=True), 5000 * 90, 4967 * 90), start=True)
            + _ts(A, _pes(0xC0, b"\xff\xf1" + b"\x00" * 50, 4990 * 90), start=True))
    assert BaseFinder().feed(data) == 4967


# ---------------------------------------------------------------- tuner tap and the API
def test_a_tap_never_keeps_the_tuner_running(fake):  # noqa: F811
    a = tuner.join("fake:1000:0.05:0", "copy", name="tv1")
    assert _read(a, 20000)
    tap = a.tuner.tap("captions")
    assert _read(tap, 20000)                        # it gets the stream...
    assert tuner.running()[0]["viewers"] == ["tv1"]  # ...without showing up as a viewer
    proc = a.tuner.proc
    a.close()
    assert proc.poll() is not None                  # the last viewer leaving still stops it
    end, last = time.monotonic() + 3, None
    while time.monotonic() < end and last != b"":
        last = tap.get(timeout=1)
    assert last == b""                              # and the tap is told


def test_captions_follow_the_screens_tuner(fake):  # noqa: F811
    sub = tuner.open_for("fake:1000:0.05:0", client_id="tvA", mode="copy")
    try:
        r = captions.poll("tvA")
        assert r["state"] in ("wait", "none", "ai", "ai-loading")
        assert r["cues"] == [] and "base" in r
        assert captions.for_tuner(sub.tuner) is captions.for_tuner(sub.tuner)   # one per tuner
    finally:
        for c in list(captions._caps.values()):
            c.stop()
        tuner.stop_client("tvA")
    assert captions.poll("tvA") == {"state": "idle"}


def test_api_cc_for_a_screen_thats_not_watching():
    from tunerbox.api import app
    assert TestClient(app).get("/api/cc", params={"client": "nobody"}).json() == {"state": "idle"}


def test_long_speech_lines_split_at_sentences():
    parts = captions._split("We have a job to do. We've seen the pickers out there and it is not going to be easy at all.", 0.0, 10.0)
    assert [p[2] for p in parts] == ["We have a job to do.", "We've seen the pickers out there and it",
                                     "is not going to be easy at all."]
    assert parts[0][0] == 0.0 and abs(parts[-1][1] - 10.0) < 1e-9
    assert captions._split("Short one.", 1.0, 2.0) == [(1.0, 2.0, "Short one.")]


# ---------------------------------------------------------------- timing live captions to the speech
def test_caption_words_count_from_when_they_start_appearing():
    screens = [(1000, ["THE MAR"]), (1250, ["THE MARKET"]), (1500, ["THE MARKET IS"]),
               (2000, ["THE MARKET IS", "UP"]), (2600, ["UP TODAY"])]
    assert captions.caption_words(screens) == [(1000, "THE"), (1000, "MARKET"), (1500, "IS"), (2000, "UP"),
                                                (2600, "TODAY")]


def test_caption_lag_from_matching_words():
    speech = [(1000 + 400 * k, w) for k, w in enumerate(
        "stocks closed higher today as investors cheered strong earnings from the biggest technology companies".split())]
    cap = [(t + 4800 + (k % 3) * 100, w.upper()) for k, (t, w) in enumerate(speech)]
    assert 4800 <= captions.caption_lag(cap, speech) <= 5000
    assert captions.caption_lag(cap[:4], speech) is None                # too few words to say
    noisy = [(t + (k % 2) * 8000, w) for k, (t, w) in enumerate(cap)]
    assert captions.caption_lag(noisy, speech) is None                 # all over the place: not trusted


def test_tuning_the_same_channel_again_keeps_its_tuner(fake):  # noqa: F811
    """Playing further behind live for captions re-opens the channel: it must not drop the provider
    connection, and the new stream starts from what the tuner kept."""
    a = tuner.open_for("fake:1000:0.05:0", client_id="tvA", mode="copy")
    _read(a, 60000)
    t, proc = a.tuner, a.tuner.proc
    b = tuner.open_for("fake:1000:0.05:0", client_id="tvA", mode="copy", buffer=10)
    assert b.tuner is t and proc.poll() is None and len(fake) == 1
    assert a not in t.subs and tuner.client_sub("tvA") is b
    tuner.stop_client("tvA")
    assert proc.poll() is not None


def test_a_held_tuner_waits_for_its_screen_to_come_back(fake):  # noqa: F811
    a = tuner.open_for("fake:1000:0.05:0", client_id="tvA", mode="copy")
    _read(a, 20000)
    t, proc = a.tuner, a.tuner.proc
    assert tuner.hold("tvA")
    a.close()                                       # the browser drops its stream first...
    assert proc.poll() is None and not t.ended
    b = tuner.open_for("fake:1000:0.05:0", client_id="tvA", mode="copy", buffer=10)
    assert b.tuner is t and len(fake) == 1          # ...and comes back to the same tuner
    tuner.stop_client("tvA")
    assert proc.poll() is not None


def test_a_held_tuner_goes_when_the_screen_picks_another_channel(fake):  # noqa: F811
    a = tuner.open_for("fake:1000:0.05:0", client_id="tvA", mode="copy")
    _read(a, 20000)
    proc = a.tuner.proc
    tuner.hold("tvA")
    a.close()
    b = tuner.open_for("fake:1000:0.04:0", client_id="tvA", mode="copy")
    time.sleep(0.5)
    assert proc.poll() is not None                  # freed at once, not after the hold
    tuner.stop_client("tvA")
    assert b.tuner.ended
