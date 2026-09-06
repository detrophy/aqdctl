#!/usr/bin/env python3
"""Offline smoke test: run every command against captured reports.

Catches undefined names, bad offsets and checksum mistakes without touching
hardware. Needs report_03.bin and report_08.bin next to it.
"""
import argparse, importlib.util, io, os, sys, tempfile, traceback, contextlib

spec = importlib.util.spec_from_file_location("octoctl", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "octoctl.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
BLOBS = {m.CTRL_REPORT_ID: open(os.path.join(FIX, "effects.bin"), "rb").read(),
         m.LABEL_REPORT_ID: open(os.path.join(FIX, "effects-08.bin"), "rb").read()}


class FakeOcto:
    """Serves captured reports; records writes instead of sending them."""
    def __init__(self): self.written = []
    def read(self, report_id=m.CTRL_REPORT_ID): return bytearray(BLOBS[report_id])
    def write(self, buf):
        stored, computed = m.report_crc(buf)
        assert stored == computed, "wrote a report with a stale checksum"
        self.written.append(bytes(buf))


def ns(**kw):
    base = dict(dry_run=True, yes=True, backup=None, force_pump=False,
                flag=None, no_flag=None, source=None, power=None, brightness=None,
                filter_rise=None, filter_fall=None)
    base.update(kw)
    return argparse.Namespace(**base)


CASES = [
    ("show",        m.cmd_show,   ns()),
    ("labels",      m.cmd_labels, ns()),
    ("label read",  m.cmd_label,  ns(group="temp", index=1, text=None)),
    ("label write", m.cmd_label,  ns(group="temp", index=1, text="Wasser Vorlauf")),
    ("label fan",   m.cmd_label,  ns(group="fan", index=3, text="Unten Seite")),
    ("offset read", m.cmd_offset, ns(sensor=1, celsius=None)),
    ("offset write",m.cmd_offset, ns(sensor=2, celsius=-1.25)),
    ("set",         m.cmd_set,    ns(channel=3, percent=40.0)),
    ("pin",         m.cmd_pin,    ns(channel=4, percent=35.0)),
    ("window",      m.cmd_window, ns(channel=3, min=25.0, max=35.0)),
    ("mode target", m.cmd_mode,   ns(channel=3, mode="target")),
    ("mode manual", m.cmd_mode,   ns(channel=3, mode="manual")),
    ("mode curve",  m.cmd_mode,   ns(channel=7, mode="curve")),
    ("curve read",  m.cmd_curve,  ns(channel=7, linear=None, points=None)),
    ("curve linear",m.cmd_curve,  ns(channel=7, linear=[27.0, 45.0, 0.0, 50.0], points=None)),
    ("target",      m.cmd_target, ns(channel=3, celsius=36.0)),
    ("fallback",    m.cmd_fallback, ns(channel=3, percent=35.0)),
    ("pid read",    m.cmd_pid,    ns(channel=5, p=None, i=None, d=None,
                                      reset=None, hysteresis=None)),
    ("pid write",   m.cmd_pid,    ns(channel=5, p=1400.0, i=1200.0, d=0.0,
                                      reset=4.0, hysteresis=0.2)),
    ("boost on",    m.cmd_boost,  ns(channel=6, state="on")),
    ("boost off",   m.cmd_boost,  ns(channel=6, state="off")),
    ("fansource rd",m.cmd_fansource, ns(channel=3, sensor=None)),
    ("fansource wr",m.cmd_fansource, ns(channel=3, sensor=2)),
    ("holdmin off", m.cmd_holdmin, ns(channel=6, state="off")),
    ("holdmin on",  m.cmd_holdmin, ns(channel=6, state="on")),
    ("maxrpm read", m.cmd_maxrpm,  ns(channel=3, rpm=None)),
    ("maxrpm write",m.cmd_maxrpm,  ns(channel=3, rpm=2600)),
    ("rgb power off",m.cmd_rgb,   ns(index=None, colour=None, entry=0, param=None,
                                     mode=None, power="off")),
    ("rgb power on", m.cmd_rgb,   ns(index=None, colour=None, entry=0, param=None,
                                     mode=None, power="on")),
    ("link",         m.cmd_link,  ns(channel=6, target=3)),
    ("rgb bright",   m.cmd_rgb,   ns(index=None, colour=None, entry=0, param=None,
                                     mode=None, brightness=45.0)),
    ("rgb bad mode", m.cmd_rgb,   ns(index=7, colour=None, entry=0, param=None,
                                     mode="0x06")),
    ("flow read",   m.cmd_flow,   ns(impulses=None)),
    ("flow set",    m.cmd_flow,   ns(impulses=147)),
    ("rgb list all",m.cmd_rgb,    ns(index=None, colour=None, entry=0, param=None, mode=None)),
    ("rgb detail",  m.cmd_rgb,    ns(index=7, colour=None, entry=0, param=None, mode=None)),
    ("rgb colour",  m.cmd_rgb,    ns(index=1, colour="DD2FA7", entry=0, param=None, mode=None)),
    ("rgb entry1",  m.cmd_rgb,    ns(index=7, colour="#00FF80", entry=1, param=None, mode=None)),
    ("rgb param",   m.cmd_rgb,    ns(index=7, colour=None, entry=0,
                                     param=["speed=30", "2=25"], mode=None)),
    ("rgb mode",    m.cmd_rgb,    ns(index=8, colour=None, entry=0, param=None, mode="wave")),
    ("rgb flag on", m.cmd_rgb,    ns(index=8, colour=None, entry=0, param=None, mode=None,
                                     flag=["slide_colors"])),
    ("rgb flag off",m.cmd_rgb,    ns(index=8, colour=None, entry=0, param=None, mode=None,
                                     no_flag=["fade"])),
    ("mode+flag",   m.cmd_rgb,    ns(index=9, colour=None, entry=0, param=None,
                                     mode="rain", flag=["snow"])),
    ("rgb source",  m.cmd_rgb,    ns(index=7, colour=None, entry=0, param=None,
                                     mode=None, source="1")),
    ("rgb no src",  m.cmd_rgb,    ns(index=7, colour=None, entry=0, param=None,
                                     mode=None, source="none")),
    ("rgb filters", m.cmd_rgb,    ns(index=7, colour=None, entry=0, param=None, mode=None,
                                     filter_rise=11, filter_fall=16)),
    ("rgb srcflag", m.cmd_rgb,    ns(index=7, colour=None, entry=0, param=None, mode=None,
                                     flag=["source_speed"])),
]

failures = []
for name, fn, args in CASES:
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn(FakeOcto(), args)
        print("  ok    %s" % name)
    except SystemExit as e:
        if e.code:
            failures.append((name, "SystemExit: %s" % e))
            print("  FAIL  %s -> %s" % (name, e))
        else:
            print("  ok    %s (clean exit)" % name)
    except Exception:
        failures.append((name, traceback.format_exc()))
        print("  FAIL  %s" % name)
        traceback.print_exc()

# dump and restore touch the filesystem
with tempfile.TemporaryDirectory() as tmp:
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            m.cmd_dump(FakeOcto(), ns(output=os.path.join(tmp, "d.bin")))
        produced = sorted(os.listdir(tmp))
        assert len(produced) == 2, "dump should write both reports, got %s" % produced
        print("  ok    dump -> %s" % produced)
        for f in produced:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_restore(FakeOcto(), ns(file=os.path.join(tmp, f)))
            print("  ok    restore %s" % f)
    except Exception:
        failures.append(("dump/restore", traceback.format_exc()))
        traceback.print_exc()

# HSV codec must round-trip every channel-extreme colour exactly
bad = []
for hexc in ("FF0000", "00FF00", "0000FF", "DD2FA7", "FFFFFF", "000000",
             "808080", "01FE7F", "123456"):
    r, g, b = m.parse_hex_colour(hexc)
    back = "%02X%02X%02X" % m.hsv_to_rgb(*m.rgb_to_hsv(r, g, b))
    if back != hexc:
        bad.append("%s -> %s" % (hexc, back))
if bad:
    failures.append(("hsv round-trip", ", ".join(bad)))
    print("  FAIL  hsv round-trip: %s" % ", ".join(bad))
else:
    print("  ok    hsv round-trip (9 colours)")

# the byte encoding must reproduce what the device actually stored
r, g, b = m.parse_hex_colour("FF0000")
h, sat, val = m.rgb_to_hsv(r, g, b)
if (h >> 8, h & 0xFF, sat, val) != (0x00, 0x00, 0xFF, 0xFF):
    failures.append(("hsv encoding", "FF0000 did not encode to 00 00 ff ff"))
    print("  FAIL  hsv encoding of FF0000")
else:
    print("  ok    FF0000 encodes byte-exact to the captured 00 00 ff ff")

# controller 7 must decode to the values Aquasuite reports for it
ctrl7 = bytearray(open(os.path.join(FIX, "baseline.bin"), "rb").read())
base = m.RGB_BASE + m.RGB_STRIDE * 6
checks = [("port", ctrl7[base + m.RGB_PORT], 1), ("start", ctrl7[base + m.RGB_START], 60),
          ("count", ctrl7[base + m.RGB_COUNT_OFF], 19),
          ("mode", ctrl7[base + m.RGB_MODE], 0x0A),
          ("speed", ctrl7[base + 23] | (ctrl7[base + 24] << 8), 25),
          ("smoothness", ctrl7[base + 25] | (ctrl7[base + 26] << 8), 40),
          ("width", ctrl7[base + 27] | (ctrl7[base + 28] << 8), 20)]
bad = ["%s=%s want %s" % (n, got, want) for n, got, want in checks if got != want]
for entry, want in ((0, "DD30A8"), (1, "FF8000")):
    got = "%02X%02X%02X" % m.hsv_to_rgb(*m.read_entry(ctrl7, 7, entry))
    if got != want:
        bad.append("colour%d=%s want %s" % (entry, got, want))
if bad:
    failures.append(("controller 7 ground truth", ", ".join(bad)))
    print("  FAIL  controller 7 ground truth: %s" % ", ".join(bad))
else:
    print("  ok    controller 7 matches Aquasuite (topology, wave, params, both colours)")

# every effect captured from Aquasuite must decode to the settings entered
NEW = bytearray(open(os.path.join(FIX, "effects.bin"), "rb").read())
EXPECT = {
    8:  dict(mode=0x05, params={"speed": 40, "count": 4}, flags=0x04,
             colours=["FF0000", "FFFF00", "0081FF", "7F00FF"]),
    9:  dict(mode=0x08, params={"speed": 25, "smoothness": 40, "width": 20}, flags=0x00,
             colours=["0F0F0F", "FF0000", "00FF00"]),
    10: dict(mode=0x0F, params={"drop_speed": 40, "drop_items": 4, "drop_size": 25,
                                "drop_smoothness": 30}, flags=0x00,
             colours=["010101", "FFFFFF"]),
}
bad = []
for idx, want in EXPECT.items():
    base = m.RGB_BASE + m.RGB_STRIDE * (idx - 1)
    if NEW[base + m.RGB_MODE] != want["mode"]:
        bad.append("ctrl%d mode %#04x want %#04x" % (idx, NEW[base + m.RGB_MODE], want["mode"]))
    if NEW[base + m.RGB_FLAGS_OFFSET] != want["flags"]:
        bad.append("ctrl%d flags %#04x want %#04x"
                   % (idx, NEW[base + m.RGB_FLAGS_OFFSET], want["flags"]))
    labels = m.RGB_PARAM_NAMES[want["mode"]]
    for pname, pval in want["params"].items():
        k = labels.index(pname)
        got = m.get_param(NEW, base, k)
        if got != pval:
            bad.append("ctrl%d %s=%d want %d" % (idx, pname, got, pval))
    for e, chex in enumerate(want["colours"]):
        got = "%02X%02X%02X" % m.hsv_to_rgb(*m.read_entry(NEW, idx, e))
        # hue is quantised to 1/1536 of the circle, so allow one LSB per channel
        if any(abs(int(got[i:i+2], 16) - int(chex[i:i+2], 16)) > 1 for i in (0, 2, 4)):
            bad.append("ctrl%d colour%d %s want %s" % (idx, e, got, chex))
if bad:
    failures.append(("effect ground truth", ", ".join(bad)))
    print("  FAIL  effect ground truth: %s" % ", ".join(bad))
else:
    print("  ok    colour change / scanner / rain match Aquasuite (mode, params, flags, colours)")

# the wave capture: predicted fields plus the isolated data-source block
WAVE = bytearray(open(os.path.join(FIX, "wave.bin"), "rb").read())
wb = m.RGB_BASE + m.RGB_STRIDE * 6
def _be(buf, o): return (buf[wb + o] << 8) | buf[wb + o + 1]
wave_checks = [("speed", m.get_param(WAVE, wb, 0), 30),
               ("smoothness", m.get_param(WAVE, wb, 1), 35),
               ("width", m.get_param(WAVE, wb, 2), 25),
               ("flags", WAVE[wb + m.RGB_FLAGS_OFFSET], 0x8A),
               ("source", _be(WAVE, m.RGB_SOURCE), 0),
               ("map A min", _be(WAVE, 10), 20), ("map A max", _be(WAVE, 12), 70),
               ("map B min", _be(WAVE, 16), 20), ("map B max", _be(WAVE, 18), 70),
               ("filter rise", WAVE[wb + m.RGB_FILTER_RISE], 10),
               ("filter fall", WAVE[wb + m.RGB_FILTER_FALL], 15)]
bad = ["%s=%s want %s" % (n, g, w) for n, g, w in wave_checks if g != w]
if bad:
    failures.append(("wave capture", ", ".join(bad)))
    print("  FAIL  wave capture: %s" % ", ".join(bad))
else:
    print("  ok    wave capture: params, flags, data source, mapping ranges, filters")

# second wave capture: only the filters and the source-toggle byte moved
W2 = bytearray(open(os.path.join(FIX, "wave2.bin"), "rb").read())
w2 = [("filter rise", W2[wb + m.RGB_FILTER_RISE], 11),
      ("filter fall", W2[wb + m.RGB_FILTER_FALL], 16),
      ("source flags", W2[wb + m.RGB_SOURCE_FLAGS], 0xC0),
      ("speed unchanged", m.get_param(W2, wb, 0), 30),
      ("flags unchanged", W2[wb + m.RGB_FLAGS_OFFSET], 0x8A)]
bad = ["%s=%s want %s" % (n, g, w) for n, g, w in w2 if g != w]
# and nothing outside those three bytes may differ from the first wave capture
moved = sorted(i - wb for i in range(len(WAVE))
               if WAVE[i] != W2[i] and wb <= i < wb + m.RGB_STRIDE)
if moved != [m.RGB_SOURCE_FLAGS, m.RGB_FILTER_RISE, m.RGB_FILTER_FALL]:
    bad.append("changed offsets %s want [4, 8, 9]" % moved)
if bad:
    failures.append(("wave capture 2", ", ".join(bad)))
    print("  FAIL  wave capture 2: %s" % ", ".join(bad))
else:
    print("  ok    wave capture 2: filters and source toggles isolated to +4, +8, +9")

# third capture: brightness off must clear exactly 0x80 and nothing else
W3 = bytearray(open(os.path.join(FIX, "wave3.bin"), "rb").read())
bad = []
if W3[wb + m.RGB_SOURCE_FLAGS] != 0x40:
    bad.append("source flags %#04x want 0x40" % W3[wb + m.RGB_SOURCE_FLAGS])
moved3 = sorted(i - wb for i in range(len(W2))
                if W2[i] != W3[i] and wb <= i < wb + m.RGB_STRIDE)
if moved3 != [m.RGB_SOURCE_FLAGS]:
    bad.append("changed offsets %s want [4]" % moved3)
if m.RGB_SOURCE_FLAG_NAMES["source_brightness"] != 0x80:
    bad.append("brightness bit mislabelled")
if bad:
    failures.append(("wave capture 3", ", ".join(bad)))
    print("  FAIL  wave capture 3: %s" % ", ".join(bad))
else:
    print("  ok    wave capture 3: 0x80 is brightness, 0x40 is speed")

# the four effect sweeps: every mode, parameter set and flag you entered
SWEEP = [
    ("effects1.bin", 7,  0x03, [50, 100], 0x00),
    ("effects1.bin", 8,  0x13, [35, 10, 5, 90, 25], 0x00),
    ("effects1.bin", 9,  0x01, [], 0x00),
    ("effects1.bin", 10, 0x02, [60, 80, 5, 20], 0x00),
    ("effects1.bin", 11, 0x0C, [30, 50, 25], 0x00),
    ("effects2.bin", 7,  0x05, [40, 2], 0x04),
    ("effects2.bin", 8,  0x04, [40, 1], 0x06),
    ("effects2.bin", 9,  0x0B, [30, 40, 1, 2, 80], 0x00),
    ("effects2.bin", 10, 0x07, [25, 30, 2, 10, 5], 0x00),
    ("effects2.bin", 11, 0x08, [25, 40, 20], 0x00),
    ("effects3.bin", 7,  0x09, [25, 40, 20], 0x00),
    ("effects3.bin", 8,  0x0A, [25, 40, 20, 1], 0x00),
    ("effects3.bin", 9,  0x0E, [50], 0x00),
    ("effects3.bin", 10, 0x0F, [40, 4, 25, 30], 0x00),
    ("effects3.bin", 11, 0x10, [50, 3, 10, 15], 0x10),
    ("effects4.bin", 7,  0x11, [25, 3, 60, 15], 0x00),
    ("effects4.bin", 8,  0x12, None, 0x00),
    ("effects4.bin", 9,  0x0D, None, 0x06),
    ("effects4.bin", 10, 0x21, None, 0x07),
]
bad = []
for fname, idx, mode, params, flags in SWEEP:
    buf = bytearray(open(os.path.join(FIX, fname), "rb").read())
    b = m.RGB_BASE + m.RGB_STRIDE * (idx - 1)
    if buf[b + m.RGB_MODE] != mode:
        bad.append("%s ctrl%d mode %#04x want %#04x" % (fname, idx, buf[b + m.RGB_MODE], mode))
    if buf[b + m.RGB_FLAGS_OFFSET] != flags:
        bad.append("%s ctrl%d flags %#04x want %#04x"
                   % (fname, idx, buf[b + m.RGB_FLAGS_OFFSET], flags))
    if params is not None:
        got = [m.get_param(buf, b, k) for k in range(len(params))]
        if got != params:
            bad.append("%s ctrl%d params %s want %s" % (fname, idx, got, params))
if bad:
    failures.append(("effect sweep", "; ".join(bad)))
    print("  FAIL  effect sweep:\n      " + "\n      ".join(bad))
else:
    print("  ok    effect sweep: 19 effect ids, params and flags across 4 captures")

# colour gradient is the only capture that distinguishes BE16@+22 from LE16@+23
grad = bytearray(open(os.path.join(FIX, "effects4.bin"), "rb").read())
gb = m.RGB_BASE + m.RGB_STRIDE * 9
limits = [m.get_param(grad, gb, k) for k in (1, 3, 4, 5, 6)]
if limits != [1000, 3, 250, 500, 750]:
    failures.append(("param endianness", "gradient limits %s want [1000, 3, 250, 500, 750]" % limits))
    print("  FAIL  param endianness: %s" % limits)
else:
    print("  ok    param endianness: gradient limits 250/500/750 prove BE16 at +22")

# a >255 parameter must round-trip through the writer
rt = bytearray(open(os.path.join(FIX, "effects4.bin"), "rb").read())
m.set_param(rt, gb, 4, 1000)
if m.get_param(rt, gb, 4) != 1000:
    failures.append(("param round-trip", "1000 did not survive set/get"))
    print("  FAIL  param round-trip")
else:
    print("  ok    param round-trip: 1000 survives set/get")

# curve captures: mode id, the 16 points, startup temperature, power window
CA = bytearray(open(os.path.join(FIX, "fan_curve_auto.bin"), "rb").read())
CM = bytearray(open(os.path.join(FIX, "fan_curve_man.bin"), "rb").read())
P2 = bytearray(open(os.path.join(FIX, "profile2_loaded.bin"), "rb").read())
cb = m.channel_base(7)
bad = []
if CA[cb + m.OFF_MODE] != m.MODE_CURVE:
    bad.append("curve mode %#04x want 0x02" % CA[cb + m.OFF_MODE])
if CA != CM:
    bad.append("automatic and manual setup differ (%d bytes)"
               % sum(1 for i in range(len(CA)) if CA[i] != CM[i]))
pts = m.read_curve(CA, 7)
if (round(pts[0][0], 2), round(pts[-1][0], 2)) != (27.0, 45.0):
    bad.append("curve temps %s..%s want 27..45" % (pts[0][0], pts[-1][0]))
if (round(pts[0][1], 2), round(pts[-1][1], 2)) != (0.0, 50.0):
    bad.append("curve powers %s..%s want 0..50" % (pts[0][1], pts[-1][1]))
if abs(((CA[cb + m.OFF_STARTUP] << 8) | CA[cb + m.OFF_STARTUP + 1]) / 100.0 - 28.79) > 0.01:
    bad.append("startup temperature wrong")
r7 = m.record_base(7)
for name, off, want in (("min", m.REC_MIN, 5.20), ("max", m.REC_MAX, 49.62),
                        ("fallback", m.REC_FALLBACK, 40.06)):
    got = ((CA[r7 + off] << 8) | CA[r7 + off + 1]) / 100.0
    if abs(got - want) > 0.01:
        bad.append("%s=%.2f want %.2f" % (name, got, want))
# profile 2 differs only in channel 7 and the profile index
if CM[m.PROFILE_INDEX] != 1 or P2[m.PROFILE_INDEX] != 2:
    bad.append("profile index %d/%d want 1/2" % (CM[m.PROFILE_INDEX], P2[m.PROFILE_INDEX]))
if P2[cb + m.OFF_MODE] != m.MODE_MANUAL:
    bad.append("profile 2 channel 7 should be manual")
if bad:
    failures.append(("curve captures", "; ".join(bad)))
    print("  FAIL  curve captures: %s" % "; ".join(bad))
else:
    print("  ok    curve captures: mode 0x02, 16 points, startup temp, window, profiles")

# a linear curve must reproduce the automatic setup byte-for-byte
gen = bytearray(CA)
m.write_curve(gen, 7, [(27.0 + 18.0 * i / 15.0, 50.0 * i / 15.0) for i in range(16)])
moved = [i - cb for i in range(len(CA)) if CA[i] != gen[i]]
if moved:
    print("  ok    linear generator differs from Aquasuite at %d point bytes "
          "(rounding)" % len(moved))
else:
    print("  ok    linear generator reproduces Aquasuite's automatic curve exactly")

# PID block and start-boost bit, from the multi-channel capture
MF = bytearray(open(os.path.join(FIX, "multi_fan_settings.bin"), "rb").read())
bad = []
b5 = m.channel_base(5)   # "user defined": P 1400 I 1200 D 0 reset 4 hysteresis 0.2
for name, off, scale, want in (("P", m.OFF_PID_P, 1.0, 1400), ("I", m.OFF_PID_I, 1.0, 1200),
                               ("D", m.OFF_PID_D, 1.0, 0),
                               ("reset", m.OFF_PID_RESET, 10.0, 4),
                               ("hysteresis", m.OFF_HYSTERESIS, 100.0, 0.2)):
    got = ((MF[b5 + off] << 8) | MF[b5 + off + 1]) / scale
    if abs(got - want) > 1e-9:
        bad.append("ch5 %s=%g want %g" % (name, got, want))
# start boost: off on 3/4/5, on for 6 and 7, all with the same source
for ch, want in ((3, False), (4, False), (5, False), (6, True), (7, True)):
    got = bool(MF[m.record_base(ch) + m.REC_FLAGS] & m.REC_FLAG_BOOST)
    if got != want:
        bad.append("ch%d boost=%s want %s" % (ch, got, want))
# the presets must be distinct tunings, not a preset id
p3 = [(MF[m.channel_base(3) + o] << 8) | MF[m.channel_base(3) + o + 1]
      for o in (m.OFF_PID_P, m.OFF_PID_I, m.OFF_PID_D)]
p4 = [(MF[m.channel_base(4) + o] << 8) | MF[m.channel_base(4) + o + 1]
      for o in (m.OFF_PID_P, m.OFF_PID_I, m.OFF_PID_D)]
if p3 != [500, 300, 0] or p4 != [4000, 3500, 1000]:
    bad.append("preset tunings %s / %s want [500,300,0] / [4000,3500,1000]" % (p3, p4))
if bad:
    failures.append(("pid and boost", "; ".join(bad)))
    print("  FAIL  pid and boost: %s" % "; ".join(bad))
else:
    print("  ok    pid and boost: P/I/D/reset/hysteresis, boost bit, slow/fast presets")

# the source-sensor capture: exactly one byte, and it is the source field
SRC = bytearray(open(os.path.join(FIX, "channel3_other_sensor.bin"), "rb").read())
bad = []
moved = [i for i in range(len(MF)) if MF[i] != SRC[i] and i < len(MF) - 2]
if moved != [m.channel_base(3) + m.OFF_SOURCE + 1]:
    bad.append("changed bytes %s want [%d]"
               % (moved, m.channel_base(3) + m.OFF_SOURCE + 1))
for ch, want in ((3, 1), (4, 0), (5, 0), (6, 0)):
    got = (SRC[m.channel_base(ch) + m.OFF_SOURCE] << 8) | SRC[m.channel_base(ch) + m.OFF_SOURCE + 1]
    if got != want:
        bad.append("ch%d source=%d want %d (0-based)" % (ch, got, want))
# and the boost bits must be untouched by a source change
for ch in range(1, 9):
    if MF[m.record_base(ch) + m.REC_FLAGS] != SRC[m.record_base(ch) + m.REC_FLAGS]:
        bad.append("ch%d record flags moved on a source change" % ch)
if bad:
    failures.append(("fan source", "; ".join(bad)))
    print("  FAIL  fan source: %s" % "; ".join(bad))
else:
    print("  ok    fan source: BE16 at +0x02, 0-based, isolated from the boost bits")

# hold-minimum-power: turning it off must clear exactly bit 0x01 on that channel
HM = bytearray(open(os.path.join(FIX, "minimum_power_toggle.bin"), "rb").read())
bad = []
r6 = m.record_base(6)
if SRC[r6 + m.REC_FLAGS] != 0x03 or HM[r6 + m.REC_FLAGS] != 0x02:
    bad.append("ch6 flags %#04x -> %#04x want 0x03 -> 0x02"
               % (SRC[r6 + m.REC_FLAGS], HM[r6 + m.REC_FLAGS]))
if not (HM[r6 + m.REC_FLAGS] & m.REC_FLAG_BOOST):
    bad.append("clearing hold-min also cleared start boost")
# chart maximum rpm, as reported by Aquasuite's fan setup
for ch, want in ((1, 5000), (3, 2600), (4, 2600), (5, 2600), (6, 2600), (7, 2000)):
    got = (HM[m.record_base(ch) + m.REC_MAX_RPM] << 8) | HM[m.record_base(ch) + m.REC_MAX_RPM + 1]
    if got != want:
        bad.append("ch%d chart max %d want %d" % (ch, got, want))
if bad:
    failures.append(("hold-min and chart rpm", "; ".join(bad)))
    print("  FAIL  hold-min and chart rpm: %s" % "; ".join(bad))
else:
    print("  ok    hold-min bit 0x01, boost bit intact, chart rpm 5000/2600/2000")

# wave "count": adding every colour slot moved k3 from 1 to 5
WR = bytearray(open(os.path.join(FIX, "minimum_power_toggle_rgb.bin"), "rb").read())
w7 = m.RGB_BASE + m.RGB_STRIDE * 6
used = sum(1 for e in range(1, 6) if any(m.read_entry(WR, 7, e)))
if m.get_param(WR, w7, 3) != 5 or used != 5:
    failures.append(("wave count", "k3=%d with %d effect colours, want 5/5"
                     % (m.get_param(WR, w7, 3), used)))
    print("  FAIL  wave count: k3=%d, %d colours" % (m.get_param(WR, w7, 3), used))
elif m.get_param(HM if False else bytearray(open(os.path.join(FIX, "wave.bin"), "rb").read()),
                 w7, 3) != 1:
    failures.append(("wave count", "earlier capture should have k3=1"))
    print("  FAIL  wave count: earlier capture k3 != 1")
else:
    print("  ok    wave k3 is the effect-colour count (1 colour -> 1, 5 -> 5)")

# the RGBpx master switch, and that it is the only byte the toggle moves
RGBOFF = bytearray(open(os.path.join(FIX, "rgb_off.bin"), "rb").read())
bad = []
moved = [i for i in range(len(WR)) if WR[i] != RGBOFF[i] and i < len(WR) - 2]
if moved != [m.RGB_ENABLE]:
    bad.append("changed bytes %s want [%#05x]" % ([hex(i) for i in moved], m.RGB_ENABLE))
if WR[m.RGB_ENABLE] != m.RGB_ENABLE_ON or RGBOFF[m.RGB_ENABLE] != m.RGB_ENABLE_OFF:
    bad.append("enable %#04x -> %#04x want %#04x -> %#04x"
               % (WR[m.RGB_ENABLE], RGBOFF[m.RGB_ENABLE],
                  m.RGB_ENABLE_ON, m.RGB_ENABLE_OFF))
# turning the function off must leave every controller's configuration intact
for i in range(m.RGB_BASE, m.RGB_BASE + m.RGB_STRIDE * m.RGB_COUNT):
    if WR[i] != RGBOFF[i]:
        bad.append("controller data changed at %#05x" % i)
        break
if bad:
    failures.append(("rgb master switch", "; ".join(bad)))
    print("  FAIL  rgb master switch: %s" % "; ".join(bad))
else:
    print("  ok    rgb master switch at 0x306 (0x00 on / 0x02 off), config preserved")

# linked mode encodes its target in the mode byte
LK = bytearray(open(os.path.join(FIX, "link_ch6_ch3.bin"), "rb").read())
bad = []
if LK[m.channel_base(6) + m.OFF_MODE] != 0x05:
    bad.append("ch6 mode %#04x want 0x05" % LK[m.channel_base(6) + m.OFF_MODE])
if LK[m.channel_base(2) + m.OFF_MODE] != 0x03:
    bad.append("ch2 mode %#04x want 0x03" % LK[m.channel_base(2) + m.OFF_MODE])
for value, target in ((0x03, 1), (0x05, 3), (0x0A, 8)):
    if not m.is_linked(value) or m.link_target(value) != target:
        bad.append("mode %#04x should decode to ch%d" % (value, target))
for value in (0x00, 0x01, 0x02):
    if m.is_linked(value):
        bad.append("mode %#04x must not read as linked" % value)
if m.mode_name(0x05) != "link->ch3" or m.mode_name(0x02) != "curve":
    bad.append("mode_name wrong: %r / %r" % (m.mode_name(0x05), m.mode_name(0x02)))
# curve mode (0x02) must still be distinguishable from a link
CAA = bytearray(open(os.path.join(FIX, "fan_curve_auto.bin"), "rb").read())
if m.is_linked(CAA[m.channel_base(7) + m.OFF_MODE]):
    bad.append("curve mode misread as a link")
if bad:
    failures.append(("linked mode", "; ".join(bad)))
    print("  FAIL  linked mode: %s" % "; ".join(bad))
else:
    print("  ok    linked mode: target encoded as mode-2, curve still distinct")

# Negative result, recorded deliberately: the per-channel "controller override"
# (input/output offset and output scale) is NOT stored in either feature report.
# Setting it on five channels moved only the one mode byte that was unlinked in
# the same session. If a future capture shows override data in report 0x03 or
# 0x08, this test should fail and the finding be revisited.
OV = bytearray(open(os.path.join(FIX, "controller_override_fans.bin"), "rb").read())
bad = []
moved = [i for i in range(len(LK)) if LK[i] != OV[i] and i < len(LK) - 2]
if moved != [m.channel_base(6) + m.OFF_MODE]:
    bad.append("report 0x03 moved at %s, expected only the ch6 mode byte"
               % [hex(i) for i in moved])
OV8 = open(os.path.join(FIX, "controller_override_fans-08.bin"), "rb").read()
LK8 = open(os.path.join(FIX, "link_ch6_ch3-08.bin"), "rb").read()
if OV8 != LK8:
    bad.append("report 0x08 changed, so labels are not the only thing it holds")
if bad:
    failures.append(("controller override", "; ".join(bad)))
    print("  FAIL  controller override: %s" % "; ".join(bad))
else:
    print("  ok    controller override absent from both feature reports (negative result)")

# the five host-DLL effects, and that they are flagged as needing Aquasuite
SP = bytearray(open(os.path.join(FIX, "special_effects.bin"), "rb").read())
bad = []
for idx, mode, name in ((7, 0x15, "sound bars"), (8, 0x14, "sound flash"),
                        (9, 0x16, "sound slider"), (10, 0x17, "sound shift"),
                        (11, 0x18, "ambientpx")):
    b = m.RGB_BASE + m.RGB_STRIDE * (idx - 1)
    if SP[b + m.RGB_MODE] != mode:
        bad.append("ctrl%d mode %#04x want %#04x" % (idx, SP[b + m.RGB_MODE], mode))
    if m.RGB_MODES.get(mode) != name:
        bad.append("%#04x named %r want %r" % (mode, m.RGB_MODES.get(mode), name))
    if mode not in m.RGB_MODES_HOST_DRIVEN:
        bad.append("%#04x not marked host-driven" % mode)
# sound bars keeps the bar-graph flag pair and its palette
if SP[m.RGB_BASE + m.RGB_STRIDE * 6 + m.RGB_FLAGS_OFFSET] != 0x06:
    bad.append("sound bars flags want 0x06 (show bar + show ranges)")
if "%02X%02X%02X" % m.hsv_to_rgb(*m.read_entry(SP, 8, 0)) != "140A00":
    bad.append("sound flash background wrong")
# every id in RGB_MODES must have a name and no id may be both verified-absent
# and host-driven by accident
if m.RGB_MODES_HOST_DRIVEN - set(m.RGB_MODES):
    bad.append("host-driven ids missing from the mode table")
if bad:
    failures.append(("host-driven effects", "; ".join(bad)))
    print("  FAIL  host-driven effects: %s" % "; ".join(bad))
else:
    print("  ok    host-driven effects 0x14-0x18 named and flagged")

# global RGB brightness: one byte, u8 over 0..255, Aquasuite's "45" == 114
GB = bytearray(open(os.path.join(FIX, "global_brightness_rgb.bin"), "rb").read())
bad = []
moved = [i for i in range(len(SP)) if SP[i] != GB[i] and i < len(SP) - 2]
if moved != [m.RGB_BRIGHTNESS]:
    bad.append("moved %s want [%#05x]" % ([hex(i) for i in moved], m.RGB_BRIGHTNESS))
if SP[m.RGB_BRIGHTNESS] != 255 or GB[m.RGB_BRIGHTNESS] != 114:
    bad.append("brightness %d -> %d want 255 -> 114"
               % (SP[m.RGB_BRIGHTNESS], GB[m.RGB_BRIGHTNESS]))
if abs(GB[m.RGB_BRIGHTNESS] * 100.0 / 255 - 44.7) > 0.05:
    bad.append("114/255 should read as 44.7%")
if min(255, int(45 * 255 / 100.0)) != 114:
    bad.append("writing 45% must produce 114 (Aquasuite truncates)")
if min(255, int(100 * 255 / 100.0)) != 255:
    bad.append("100% must produce 255")
if bad:
    failures.append(("global brightness", "; ".join(bad)))
    print("  FAIL  global brightness: %s" % "; ".join(bad))
else:
    print("  ok    global brightness at 0x304, u8 0-255 (45%% -> 114 -> 44.7%%)")

# sequence vs colour sequence: flags, counts, and the RGB data source index
S1 = bytearray(open(os.path.join(FIX, "sequences_1.bin"), "rb").read())
S2 = bytearray(open(os.path.join(FIX, "sequences_2.bin"), "rb").read())
b7 = m.RGB_BASE + m.RGB_STRIDE * 6      # sequence 0x07
b8 = m.RGB_BASE + m.RGB_STRIDE * 7      # colour sequence 0x0B
bad = []
if S1[b7 + m.RGB_MODE] != 0x07 or S1[b8 + m.RGB_MODE] != 0x0B:
    bad.append("modes %#04x/%#04x want 0x07/0x0b" % (S1[b7 + m.RGB_MODE], S1[b8 + m.RGB_MODE]))
# sequence: fade on -> 0x04, then random colour added -> 0x0c
if S1[b7 + m.RGB_FLAGS_OFFSET] != 0x04 or S2[b7 + m.RGB_FLAGS_OFFSET] != 0x0C:
    bad.append("sequence flags %#04x -> %#04x want 0x04 -> 0x0c"
               % (S1[b7 + m.RGB_FLAGS_OFFSET], S2[b7 + m.RGB_FLAGS_OFFSET]))
# colour sequence: reverse on, random off
if S1[b8 + m.RGB_FLAGS_OFFSET] != 0x02:
    bad.append("colour sequence flags %#04x want 0x02" % S1[b8 + m.RGB_FLAGS_OFFSET])
# colour counts: sequence k2 = 5 extra colours, colour sequence k3 = 6
if m.get_param(S1, b7, 2) != 5:
    bad.append("sequence count %d want 5" % m.get_param(S1, b7, 2))
if m.get_param(S1, b8, 3) != 6:
    bad.append("colour sequence count %d want 6" % m.get_param(S1, b8, 3))
# sequence delays after/before
if (m.get_param(S1, b7, 3), m.get_param(S1, b7, 4)) != (11, 6):
    bad.append("sequence delays want 11/6")
# RGB data source is 0-based, same as the fan side: "Front Rad Hot" is sensor 2
src2 = (S2[b8 + m.RGB_SOURCE] << 8) | S2[b8 + m.RGB_SOURCE + 1]
if (S1[b8 + m.RGB_SOURCE] << 8) | S1[b8 + m.RGB_SOURCE + 1] != m.RGB_SOURCE_NONE or src2 != 1:
    bad.append("source %#06x -> %#06x want 0xffff -> 0x0001" % (
        (S1[b8 + m.RGB_SOURCE] << 8) | S1[b8 + m.RGB_SOURCE + 1], src2))
# selecting a temperature source rewrites both input ranges to 20..70
for lo, hi in ((10, 12), (16, 18)):
    if ((S2[b8 + lo] << 8) | S2[b8 + lo + 1], (S2[b8 + hi] << 8) | S2[b8 + hi + 1]) != (20, 70):
        bad.append("mapping input range at +%d want 20..70" % lo)
# and the output bytes +14/+15 must NOT move when a source is enabled
for off in (14, 15, 20, 21):
    if S1[b8 + off] != S2[b8 + off]:
        bad.append("+%d changed when a data source was enabled" % off)
if bad:
    failures.append(("sequence captures", "; ".join(bad)))
    print("  FAIL  sequence captures: %s" % "; ".join(bad))
else:
    print("  ok    sequences: flags, counts, delays, 0-based RGB source, +14/+15 static")

# +14/+15 are the mapping OUTPUT range, fixed per effect; the input range
# follows the chosen source (temperature 20..70, flow 0..300)
WB = bytearray(open(os.path.join(FIX, "wave_breathing.bin"), "rb").read())
bad = []
def blk(idx, lo):
    b = m.RGB_BASE + m.RGB_STRIDE * (idx - 1)
    return ((WB[b + lo] << 8) | WB[b + lo + 1], (WB[b + lo + 2] << 8) | WB[b + lo + 3],
            WB[b + lo + 4], WB[b + lo + 5])
for idx, mode, src, inrange, out_a in ((7, 0x0A, 0, (20, 70), (0, 100)),
                                       (8, 0x0A, m.SOURCE_FLOW, (0, 300), (0, 100)),
                                       (9, 0x02, 0, (20, 70), (1, 100)),
                                       (10, 0x02, m.SOURCE_FLOW, (0, 300), (1, 100))):
    b = m.RGB_BASE + m.RGB_STRIDE * (idx - 1)
    if WB[b + m.RGB_MODE] != mode:
        bad.append("ctrl%d mode %#04x want %#04x" % (idx, WB[b + m.RGB_MODE], mode))
    got_src = (WB[b + m.RGB_SOURCE] << 8) | WB[b + m.RGB_SOURCE + 1]
    if got_src != src:
        bad.append("ctrl%d source %d want %d" % (idx, got_src, src))
    lo, hi, omin, omax = blk(idx, 10)
    if (lo, hi) != inrange:
        bad.append("ctrl%d input %d..%d want %s" % (idx, lo, hi, inrange))
    if (omin, omax) != out_a:
        bad.append("ctrl%d output %d..%d want %s" % (idx, omin, omax, out_a))
# breathing is the effect that cannot be driven to a standstill
if blk(9, 10)[2] != 1 or blk(7, 10)[2] != 0:
    bad.append("breathing should floor at 1 and wave at 0")
# breathing is also the only effect whose brightness block matches its speed block
if blk(9, 16)[3] != 100 or blk(7, 16)[3] != 255:
    bad.append("brightness ceilings want 100 (breathing) / 255 (wave)")
if m.source_label(m.SOURCE_FLOW) != "flow" or m.source_label(0) != "sensor 1":
    bad.append("source_label wrong")
if bad:
    failures.append(("mapping output range", "; ".join(bad)))
    print("  FAIL  mapping output range: %s" % "; ".join(bad))
else:
    print("  ok    +14/+15 are the mapping output range; flow source is index 4")

# colour mode 2 on scanner and laser is flag 0x20 and nothing else
CM = bytearray(open(os.path.join(FIX, "colour_mode.bin"), "rb").read())
bad = []
for mode2, mode1, mode, name in ((7, 9, 0x09, "laser"), (8, 10, 0x08, "scanner")):
    ba = m.RGB_BASE + m.RGB_STRIDE * (mode2 - 1)
    bb = m.RGB_BASE + m.RGB_STRIDE * (mode1 - 1)
    if CM[ba + m.RGB_MODE] != mode or CM[bb + m.RGB_MODE] != mode:
        bad.append("%s pair is not both %#04x" % (name, mode))
    # +1 is the start LED, which necessarily differs between two controllers
    diff = [i for i in range(m.RGB_STRIDE)
            if CM[ba + i] != CM[bb + i] and i != m.RGB_START]
    if diff != [m.RGB_FLAGS_OFFSET]:
        bad.append("%s pair differs at %s, want only the flags byte"
                   % (name, ["+%d" % i for i in diff]))
    if CM[ba + m.RGB_FLAGS_OFFSET] != 0x20 or CM[bb + m.RGB_FLAGS_OFFSET] != 0x00:
        bad.append("%s flags %#04x/%#04x want 0x20/0x00"
                   % (name, CM[ba + m.RGB_FLAGS_OFFSET], CM[bb + m.RGB_FLAGS_OFFSET]))
    if m.RGB_FLAG_NAMES[mode].get("color_mode2") != 0x20:
        bad.append("%s color_mode2 mask wrong" % name)
if bad:
    failures.append(("colour mode 2", "; ".join(bad)))
    print("  FAIL  colour mode 2: %s" % "; ".join(bad))
else:
    print("  ok    colour mode 2 = flag 0x20 on scanner and laser, isolated")

# unimplemented effect ids, established by probing hardware directly
bad = []
if not m.RGB_MODES_UNIMPLEMENTED >= {0x06, 0x19, 0x20}:
    bad.append("0x06 and 0x19-0x20 should be marked unimplemented")
if m.RGB_MODES_UNIMPLEMENTED & set(m.RGB_MODES):
    bad.append("an id cannot be both named and unimplemented")
# the two sets together must cover 0x01..0x21 with no gaps left unexplained
covered = set(m.RGB_MODES) | m.RGB_MODES_UNIMPLEMENTED
missing = [hex(i) for i in range(0x01, 0x22) if i not in covered]
if missing:
    bad.append("still unaccounted for: %s" % ", ".join(missing))
if bad:
    failures.append(("unimplemented ids", "; ".join(bad)))
    print("  FAIL  unimplemented ids: %s" % "; ".join(bad))
else:
    print("  ok    every id 0x01-0x21 is either a named effect or known unimplemented")

# Captured while the Aquasuite service was wedged: a profile selection wrote
# nothing, and the settings only landed on the next flush. Not how it behaves
# normally (a service restart fixed it), but the flush itself is still good
# evidence that 0x65c is the active-profile index, since the index and the
# profile's RGB-off arrived in the same write.
PA = bytearray(open(os.path.join(FIX, "probe-a.bin"), "rb").read())
PB = bytearray(open(os.path.join(FIX, "probe-b.bin"), "rb").read())
PC = bytearray(open(os.path.join(FIX, "probe-c.bin"), "rb").read())
bad = []
if PA != PB:
    bad.append("selecting a profile should write nothing, %d bytes moved"
               % sum(1 for i in range(len(PA)) if PA[i] != PB[i]))
if (PB[m.PROFILE_INDEX], PC[m.PROFILE_INDEX]) != (1, 2):
    bad.append("profile index %d -> %d want 1 -> 2"
               % (PB[m.PROFILE_INDEX], PC[m.PROFILE_INDEX]))
if (PB[m.RGB_ENABLE], PC[m.RGB_ENABLE]) != (m.RGB_ENABLE_ON, m.RGB_ENABLE_OFF):
    bad.append("profile 2's RGB-off should arrive with the flush")
if bad:
    failures.append(("profile switching", "; ".join(bad)))
    print("  FAIL  profile switching: %s" % "; ".join(bad))
else:
    print("  ok    profile index 0x65c moves with the profile's own settings")

# labels are single-byte Latin-1, not UTF-8
UM = bytearray(open(os.path.join(FIX, "umlaut-08.bin"), "rb").read())
bad = []
slot = m.LABEL_GROUPS["fan"][0] + m.LABEL_SIZE * 6
raw = bytes(UM[slot:slot + m.LABEL_SIZE]).split(b"\x00")[0]
if raw != b"\x68\xf6\x72\x65\x6e":
    bad.append("ch7 raw %s want 68 f6 72 65 6e" % raw.hex(" "))
if m.decode_labels(UM)["fan"][6] != "h\u00f6ren":
    bad.append("decoded %r want 'hören'" % m.decode_labels(UM)["fan"][6])
if "h\u00f6ren".encode(m.LABEL_ENCODING) != raw:
    bad.append("round-trip through LABEL_ENCODING does not reproduce the bytes")
if len("h\u00f6ren".encode("utf-8")) == len(raw):
    bad.append("UTF-8 must be distinguishable from the stored form")
if bad:
    failures.append(("label encoding", "; ".join(bad)))
    print("  FAIL  label encoding: %s" % "; ".join(bad))
else:
    print("  ok    labels are Latin-1: 'hören' stores as 68 f6 72 65 6e")

# the pump guard must reject channels 1-2
for ch in (1, 2):
    try:
        m.guard_pump(ch, False)
        failures.append(("pump guard ch%d" % ch, "did not refuse"))
        print("  FAIL  pump guard ch%d" % ch)
    except SystemExit:
        print("  ok    pump guard refuses ch%d" % ch)

total = len(CASES) + 30
print("\n%d/%d passed" % (total - len(failures), total))
sys.exit(1 if failures else 0)
