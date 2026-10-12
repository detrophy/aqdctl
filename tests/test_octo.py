#!/usr/bin/env python3
"""Offline test of the Octo support: run every command against captured reports.

Catches undefined names, bad offsets and checksum mistakes without touching
hardware. Reads the Octo captures in octo/.
"""
import argparse, io, os, sys, tempfile, traceback, contextlib, types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from aqdctl import cli, core, discovery, names, octo, rgbpx

# The checks below address everything as m.NAME, as they did when the tool was
# one module. This namespace keeps them readable: every module's names, with the
# helpers that now take a layout or a serial bound to the fake Octo.
SERIAL = "12345-67890"          # the fake device's serial
m = types.SimpleNamespace()
for module in (core, rgbpx, names, octo):
    m.__dict__.update({k: v for k, v in vars(module).items() if not k.startswith("__")})
m.read_entry = lambda buf, index, entry=0: rgbpx.read_entry(buf, octo.RGB, index, entry)
m.decode_labels = lambda buf: names.decode(buf, octo.NAME_GROUPS)
m.LABEL_GROUPS = {g.cli: (g.base, g.count) for g in octo.NAME_GROUPS}
m.protected_channels = lambda: core.protected_channels(SERIAL)
m.guard_channel = lambda channel, force: core.guard_channel(SERIAL, channel, force)

FIX = os.path.join(ROOT, "octo")
BLOBS = {m.CTRL_REPORT_ID: open(os.path.join(FIX, "03-rgb-controllers-8-to-10-effects.bin"), "rb").read(),
         m.LABEL_REPORT_ID: open(os.path.join(FIX, "03-rgb-controllers-8-to-10-effects-08.bin"), "rb").read()}


class FakeOcto:
    """Serves captured reports; records writes and keeps them, as a device that
    accepts them does, so the read-back after a write verifies."""
    kind, serial = octo, SERIAL
    def __init__(self, ctrl=None):
        self.written, self.blobs = [], dict(BLOBS)
        if ctrl is not None:
            self.blobs[m.CTRL_REPORT_ID] = bytes(ctrl)
    def read(self, report_id=m.CTRL_REPORT_ID, check=True):
        buf = bytearray(self.blobs[report_id])
        if check and report_id == m.CTRL_REPORT_ID:
            core.check_layout(self.kind, buf)      # as core.Device.read does
        return buf
    def write(self, buf):
        stored, computed = m.report_crc(buf)
        assert stored == computed, "wrote a report with a stale checksum"
        self.written.append(bytes(buf))
        self.blobs[buf[0]] = bytes(buf)
    def read_input(self, size, timeout_ms=0):
        return None                     # no input report was captured


def _quiet(fn, args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(FakeOcto(), args)


def ns(**kw):
    base = dict(dry_run=True, verbose=False, yes=True, backup=None, force=False,
                flag=None, no_flag=None, sensor=None, param=None,
                effect=None, colour=None, background=None,
                filter_rise=None, filter_fall=None, pos=None, preset=None)
    base.update(kw)
    return argparse.Namespace(**base)


# (label, handler, namespace, expect)
#   expect None  -> must run cleanly
#   expect "exit" -> must refuse with SystemExit, i.e. the tool rejects bad input
#                    instead of writing something wrong
CASES = [
    ("info all",      m.cmd_info,       ns(section=None), None),
    ("info fan",      m.cmd_info,       ns(section="fan"), None),
    ("info rgb",      m.cmd_info,       ns(section="rgb"), None),
    ("info sensor",   m.cmd_info,       ns(section="sensor"), None),

    ("name list",     m.cmd_name_list,  ns(), None),
    ("name read",     m.cmd_name,       ns(group="sensor", index=1, text=None), None),
    ("name sensor",   m.cmd_name,       ns(group="sensor", index=1, text="Wasser Vorlauf"), None),
    ("name fan",      m.cmd_name,       ns(group="fan", index=3, text="Unten Seite"), None),
    ("name umlaut",   m.cmd_name,       ns(group="fan", index=7, text="h\u00f6ren"), None),
    ("name too long", m.cmd_name,       ns(group="fan", index=3, text="x" * 40), "exit"),
    ("name with NUL", m.cmd_name,       ns(group="fan", index=3, text="a\x00b"), "exit"),
    ("name bad index",m.cmd_name,       ns(group="sensor", index=9, text="x"), "exit"),

    ("offset read",   m.cmd_offset,     ns(sensor=1, celsius=None), None),
    ("offset write",  m.cmd_offset,     ns(sensor=2, celsius=-1.25), None),
    ("offset range",  m.cmd_offset,     ns(sensor=2, celsius=-40.0), "exit"),
    ("flow read",     m.cmd_flow,       ns(impulses=None), None),
    ("flow write",    m.cmd_flow,       ns(impulses=147), None),

    ("mode fixed",    m.cmd_mode_fixed, ns(channel=3, percent=40.0), None),
    ("mode target",   m.cmd_mode_target,ns(channel=3, celsius=36.0), None),
    ("target keep",   m.cmd_mode_target,ns(channel=3, celsius=None), None),
    ("target sensor", m.cmd_mode_target,ns(channel=3, celsius=36.0, sensor="2"), None),
    ("target flow",   m.cmd_mode_target,ns(channel=3, celsius=36.0, sensor="flow"), None),
    ("target raw src",m.cmd_mode_target,ns(channel=3, celsius=36.0, sensor="#43"), None),
    ("target bad src",m.cmd_mode_target,ns(channel=3, celsius=36.0, sensor="9"), "exit"),
    ("mode curve",    m.cmd_mode_curve, ns(channel=7, points=None, linear=None,
                                           startup=None), None),
    ("curve linear",  m.cmd_mode_curve, ns(channel=7, points=None, startup=None,
                                           linear=[27.0, 45.0, 0.0, 50.0]), None),
    ("curve points",  m.cmd_mode_curve, ns(channel=7, linear=None, startup=None,
                                           points=",".join("%d:%d" % (20 + i, i * 5)
                                                           for i in range(16))), None),
    ("curve short",   m.cmd_mode_curve, ns(channel=7, linear=None, startup=None,
                                           points="20:0,30:50"), "exit"),
    ("curve both",    m.cmd_mode_curve, ns(channel=7, points="20:0", startup=None,
                                           linear=[27.0, 45.0, 0.0, 50.0]), "exit"),
    ("curve startup", m.cmd_mode_curve, ns(channel=7, points=None, linear=None,
                                           startup=30.0), None),
    # CURVE_TEMP_MAX is aqdctl's own bound, not the device's: at it, and past it
    ("curve at max",  m.cmd_mode_curve, ns(channel=7, points=None, startup=None,
                                           linear=[30.0, m.CURVE_TEMP_MAX,
                                                   0.0, 50.0]), None),
    ("curve over max",m.cmd_mode_curve, ns(channel=7, points=None, startup=None,
                                           linear=[30.0, m.CURVE_TEMP_MAX + 1,
                                                   0.0, 50.0]), "exit"),
    ("startup at max",m.cmd_mode_curve, ns(channel=7, points=None, linear=None,
                                           startup=m.CURVE_TEMP_MAX), None),
    ("startup over",  m.cmd_mode_curve, ns(channel=7, points=None, linear=None,
                                           startup=m.CURVE_TEMP_MAX + 0.01), "exit"),
    ("mode follow",   m.cmd_mode_follow,ns(channel=6, target=3), None),
    ("follow self",   m.cmd_mode_follow,ns(channel=6, target=6), "exit"),

    ("limits",        m.cmd_limits,     ns(channel=3, min=25.0, max=35.0), None),
    ("limits pin",    m.cmd_limits,     ns(channel=4, min=35.0, max=35.0), None),
    ("limits inverted", m.cmd_limits,   ns(channel=3, min=100.0, max=0.0), "exit"),
    ("limits over 100", m.cmd_limits,   ns(channel=3, min=0.0, max=500.0), "exit"),
    ("fallback",      m.cmd_fallback,   ns(channel=3, percent=35.0), None),
    ("fallback range", m.cmd_fallback,  ns(channel=3, percent=500.0), "exit"),
    ("fixed over 100", m.cmd_mode_fixed, ns(channel=3, percent=150.0), "exit"),
    ("fixed negative", m.cmd_mode_fixed, ns(channel=3, percent=-1.0), "exit"),
    ("boost on",      m.cmd_boost,      ns(channel=6, state="on"), None),
    ("boost off",     m.cmd_boost,      ns(channel=6, state="off"), None),
    ("hold-min off",  m.cmd_holdmin,    ns(channel=6, state="off"), None),
    ("hold-min on",   m.cmd_holdmin,    ns(channel=6, state="on"), None),
    ("max-rpm read",  m.cmd_maxrpm,     ns(channel=3, rpm=None), None),
    ("max-rpm write", m.cmd_maxrpm,     ns(channel=3, rpm=2600), None),

    ("pid read",      m.cmd_pid,        ns(channel=5, p=None, i=None, d=None,
                                           reset=None, hysteresis=None), None),
    ("pid explicit",  m.cmd_pid,        ns(channel=5, p=1400.0, i=1200.0, d=0.0,
                                           reset=4.0, hysteresis=0.2), None),
    ("pid preset",    m.cmd_pid,        ns(channel=5, preset="fast", p=None, i=None,
                                           d=None, reset=None, hysteresis=None), None),
    ("pid override",  m.cmd_pid,        ns(channel=5, preset="fast", p=None, i=None,
                                           d=600.0, reset=None, hysteresis=None), None),

    ("rgb off",       m.cmd_rgb_switch, ns(state="off"), None),
    ("rgb on",        m.cmd_rgb_switch, ns(state="on"), None),
    ("rgb bright",    m.cmd_rgb_brightness, ns(percent=45.0), None),
    ("rgb bright bad",m.cmd_rgb_brightness, ns(percent=140.0), "exit"),
    ("rgb effects",   m.cmd_rgb_effects,ns(effect=None), None),
    ("rgb effect one",m.cmd_rgb_effects,ns(effect="wave"), None),
    ("rgb effect bad",m.cmd_rgb_effects,ns(effect="nope"), "exit"),

    # The fixture is capture 03. Controller 1 is channel 1, LEDs 1-28, static.
    # On channel 2: 7 = 61-79 wave, 8 = 1-15 colour change, 9 = 16-30 scanner,
    # 10 = 31-45 rain, 11 = 46-60 static. Slots 2-6 and 12 are free.
    ("rgb edit",      m.cmd_rgb_set,    ns(controller=7, param=["speed=30"]), None),
    ("rgb create",    m.cmd_rgb_create, ns(channel=1, pos="40-54",
                                           effect="static", colour="FF0000"), None),
    ("rgb create no fx", m.cmd_rgb_create, ns(channel=1, pos="40-54"), "exit"),
    ("rgb overlap",   m.cmd_rgb_create, ns(channel=2, pos="10-20",
                                           effect="static"), "exit"),
    ("rgb colour",    m.cmd_rgb_set,    ns(controller=1, colour="DD2FA7"), None),
    ("rgb bg+list",   m.cmd_rgb_set,    ns(controller=7, background="0A0A0A",
                                           colour="FF0000,00FF00,0000FF"), None),
    ("rgb too many",  m.cmd_rgb_set,    ns(controller=9,
                                           colour="FF0000,00FF00,0000FF"), "exit"),
    ("rgb no bg",     m.cmd_rgb_set,    ns(controller=8, background="0A0A0A"), "exit"),
    ("rgb count kw",  m.cmd_rgb_set,    ns(controller=7, param=["count=3"]), "exit"),
    ("rgb bad param", m.cmd_rgb_set,    ns(controller=7, param=["nope=3"]), "exit"),
    ("rgb effect set",m.cmd_rgb_set,    ns(controller=11, effect="wave"), None),
    ("rgb unimpl",    m.cmd_rgb_set,    ns(controller=11, effect="0x06"), None),
    ("rgb flag on",   m.cmd_rgb_set,    ns(controller=9, flag=["reverse"]), None),
    ("rgb flag off",  m.cmd_rgb_set,    ns(controller=9, no_flag=["circular"]), None),
    ("rgb bad flag",  m.cmd_rgb_set,    ns(controller=9, flag=["nonsense"]), "exit"),
    ("rgb fx+flag",   m.cmd_rgb_set,    ns(controller=10, effect="rain",
                                           flag=["snow"]), None),
    ("rgb sensor",    m.cmd_rgb_set,    ns(controller=7, sensor="1"), None),
    ("rgb no sensor", m.cmd_rgb_set,    ns(controller=7, sensor="none"), None),
    ("rgb filters",   m.cmd_rgb_set,    ns(controller=7, filter_rise=11,
                                           filter_fall=16), None),
    ("rgb srcflag",   m.cmd_rgb_set,    ns(controller=7, flag=["source_speed"]), None),
    ("rgb unused",    m.cmd_rgb_set,    ns(controller=3, colour="FF0000"), "exit"),
    ("rgb resize",    m.cmd_rgb_set,    ns(controller=7, pos="61-85"), None),
    ("rgb resize clash", m.cmd_rgb_set, ns(controller=7, pos="1-20"), "exit"),
    ("rgb bad pos",   m.cmd_rgb_create, ns(channel=2, pos="30-10",
                                           effect="static"), "exit"),
    ("rgb pos overrun",m.cmd_rgb_create,ns(channel=2, pos="80-200",
                                           effect="static"), "exit"),
    ("rgb remove one",m.cmd_rgb_remove, ns(what="controller", number=7), None),
    ("rgb remove ch", m.cmd_rgb_remove, ns(what="channel", number=2), None),
    ("rgb remove none",m.cmd_rgb_remove,ns(what="controller", number=3), "exit"),
    ("rgb remove bad ch",m.cmd_rgb_remove,ns(what="channel", number=3), "exit"),
]

failures = []
for name, fn, args, expect in CASES:
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn(FakeOcto(), args)
        if expect == "exit":
            failures.append((name, "expected a refusal, but it ran"))
            print("  FAIL  %s -> should have been refused" % name)
        else:
            print("  ok    %s" % name)
    except SystemExit as e:
        if expect == "exit":
            print("  ok    %s (refused: %s)" % (name, str(e).split(chr(10))[0][:60]))
        elif e.code:
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
            m.cmd_backup(FakeOcto(), ns(output=os.path.join(tmp, "profile")))
        produced = sorted(os.listdir(tmp))
        assert len(produced) == 2, "backup should write both reports, got %s" % produced
        print("  ok    backup -> %s" % produced)
        for f in produced:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_restore(FakeOcto(), ns(files=[os.path.join(tmp, f)], other_device=False))
            print("  ok    restore %s" % f)
    except Exception:
        failures.append(("backup/restore", traceback.format_exc()))
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
ctrl7 = bytearray(open(os.path.join(FIX, "04-rgb-controllers-8-to-10-static.bin"), "rb").read())
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
NEW = bytearray(open(os.path.join(FIX, "03-rgb-controllers-8-to-10-effects.bin"), "rb").read())
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
WAVE = bytearray(open(os.path.join(FIX, "06-rgb-wave-with-data-source.bin"), "rb").read())
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
W2 = bytearray(open(os.path.join(FIX, "07-rgb-wave-filters-and-source-toggles.bin"), "rb").read())
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
W3 = bytearray(open(os.path.join(FIX, "08-rgb-wave-source-brightness-off.bin"), "rb").read())
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
    ("09-rgb-effect-catalogue-1.bin", 7,  0x03, [50, 100], 0x00),
    ("09-rgb-effect-catalogue-1.bin", 8,  0x13, [35, 10, 5, 90, 25], 0x00),
    ("09-rgb-effect-catalogue-1.bin", 9,  0x01, [], 0x00),
    ("09-rgb-effect-catalogue-1.bin", 10, 0x02, [60, 80, 5, 20], 0x00),
    ("09-rgb-effect-catalogue-1.bin", 11, 0x0C, [30, 50, 25], 0x00),
    ("10-rgb-effect-catalogue-2.bin", 7,  0x05, [40, 2], 0x04),
    ("10-rgb-effect-catalogue-2.bin", 8,  0x04, [40, 1], 0x06),
    ("10-rgb-effect-catalogue-2.bin", 9,  0x0B, [30, 40, 1, 2, 80], 0x00),
    ("10-rgb-effect-catalogue-2.bin", 10, 0x07, [25, 30, 2, 10, 5], 0x00),
    ("10-rgb-effect-catalogue-2.bin", 11, 0x08, [25, 40, 20], 0x00),
    ("11-rgb-effect-catalogue-3.bin", 7,  0x09, [25, 40, 20], 0x00),
    ("11-rgb-effect-catalogue-3.bin", 8,  0x0A, [25, 40, 20, 1], 0x00),
    ("11-rgb-effect-catalogue-3.bin", 9,  0x0E, [50], 0x00),
    ("11-rgb-effect-catalogue-3.bin", 10, 0x0F, [40, 4, 25, 30], 0x00),
    ("11-rgb-effect-catalogue-3.bin", 11, 0x10, [50, 3, 10, 15], 0x10),
    ("12-rgb-effect-catalogue-4.bin", 7,  0x11, [25, 3, 60, 15], 0x00),
    ("12-rgb-effect-catalogue-4.bin", 8,  0x12, None, 0x00),
    ("12-rgb-effect-catalogue-4.bin", 9,  0x0D, None, 0x06),
    ("12-rgb-effect-catalogue-4.bin", 10, 0x21, None, 0x07),
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
grad = bytearray(open(os.path.join(FIX, "12-rgb-effect-catalogue-4.bin"), "rb").read())
gb = m.RGB_BASE + m.RGB_STRIDE * 9
limits = [m.get_param(grad, gb, k) for k in (1, 3, 4, 5, 6)]
if limits != [1000, 3, 250, 500, 750]:
    failures.append(("param endianness", "gradient limits %s want [1000, 3, 250, 500, 750]" % limits))
    print("  FAIL  param endianness: %s" % limits)
else:
    print("  ok    param endianness: gradient limits 250/500/750 prove BE16 at +22")

# a >255 parameter must round-trip through the writer
rt = bytearray(open(os.path.join(FIX, "12-rgb-effect-catalogue-4.bin"), "rb").read())
m.set_param(rt, gb, 4, 1000)
if m.get_param(rt, gb, 4) != 1000:
    failures.append(("param round-trip", "1000 did not survive set/get"))
    print("  FAIL  param round-trip")
else:
    print("  ok    param round-trip: 1000 survives set/get")

# curve captures: mode id, the 16 points, startup temperature, power window
CA = bytearray(open(os.path.join(FIX, "13-fan-ch7-curve-automatic-setup.bin"), "rb").read())
CM = bytearray(open(os.path.join(FIX, "14-fan-ch7-curve-manual-setup-writes-nothing.bin"), "rb").read())
P2 = bytearray(open(os.path.join(FIX, "15-profile-2-loaded.bin"), "rb").read())
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
MF = bytearray(open(os.path.join(FIX, "16-fan-pid-and-boost-several-channels.bin"), "rb").read())
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
SRC = bytearray(open(os.path.join(FIX, "17-fan-ch3-source-sensor-changed.bin"), "rb").read())
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
HM = bytearray(open(os.path.join(FIX, "18-fan-ch6-hold-minimum-power-off.bin"), "rb").read())
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
WR = bytearray(open(os.path.join(FIX, "19-rgb-switch-on-and-wave-five-colours.bin"), "rb").read())
w7 = m.RGB_BASE + m.RGB_STRIDE * 6
used = sum(1 for e in range(1, 6) if any(m.read_entry(WR, 7, e)))
if m.get_param(WR, w7, 3) != 5 or used != 5:
    failures.append(("wave count", "k3=%d with %d effect colours, want 5/5"
                     % (m.get_param(WR, w7, 3), used)))
    print("  FAIL  wave count: k3=%d, %d colours" % (m.get_param(WR, w7, 3), used))
elif m.get_param(HM if False else bytearray(open(os.path.join(FIX, "06-rgb-wave-with-data-source.bin"), "rb").read()),
                 w7, 3) != 1:
    failures.append(("wave count", "earlier capture should have k3=1"))
    print("  FAIL  wave count: earlier capture k3 != 1")
else:
    print("  ok    wave k3 is the effect-colour count (1 colour -> 1, 5 -> 5)")

# the RGBpx master switch, and that it is the only byte the toggle moves
RGBOFF = bytearray(open(os.path.join(FIX, "20-rgb-switch-off.bin"), "rb").read())
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
LK = bytearray(open(os.path.join(FIX, "21-fan-ch6-follows-ch3.bin"), "rb").read())
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
if m.mode_name(0x05) != "follow ch3" or m.mode_name(0x02) != "curve":
    bad.append("mode_name wrong: %r / %r" % (m.mode_name(0x05), m.mode_name(0x02)))
# curve mode (0x02) must still be distinguishable from a link
CAA = bytearray(open(os.path.join(FIX, "13-fan-ch7-curve-automatic-setup.bin"), "rb").read())
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
OV = bytearray(open(os.path.join(FIX, "22-fan-controller-override-stores-nothing.bin"), "rb").read())
bad = []
moved = [i for i in range(len(LK)) if LK[i] != OV[i] and i < len(LK) - 2]
if moved != [m.channel_base(6) + m.OFF_MODE]:
    bad.append("report 0x03 moved at %s, expected only the ch6 mode byte"
               % [hex(i) for i in moved])
OV8 = open(os.path.join(FIX, "22-fan-controller-override-stores-nothing-08.bin"), "rb").read()
LK8 = open(os.path.join(FIX, "21-fan-ch6-follows-ch3-08.bin"), "rb").read()
if OV8 != LK8:
    bad.append("report 0x08 changed, so labels are not the only thing it holds")
if bad:
    failures.append(("controller override", "; ".join(bad)))
    print("  FAIL  controller override: %s" % "; ".join(bad))
else:
    print("  ok    controller override absent from both feature reports (negative result)")

# the five host-DLL effects, and that they are flagged as needing Aquasuite
SP = bytearray(open(os.path.join(FIX, "23-rgb-host-driven-effects.bin"), "rb").read())
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

# global RGB brightness: one byte, u8 over 0..255. The fixture is Aquasuite's
# slider showing "45", which stored 114; the slider moves one byte per step, so
# 115 shows as 45 as well. The handler stores the byte nearest the percentage.
GB = bytearray(open(os.path.join(FIX, "24-rgb-brightness-45.bin"), "rb").read())
bad = []
moved = [i for i in range(len(SP)) if SP[i] != GB[i] and i < len(SP) - 2]
if moved != [m.RGB_BRIGHTNESS]:
    bad.append("moved %s want [%#05x]" % ([hex(i) for i in moved], m.RGB_BRIGHTNESS))
if SP[m.RGB_BRIGHTNESS] != 255 or GB[m.RGB_BRIGHTNESS] != 114:
    bad.append("brightness %d -> %d want 255 -> 114"
               % (SP[m.RGB_BRIGHTNESS], GB[m.RGB_BRIGHTNESS]))
if abs(GB[m.RGB_BRIGHTNESS] * 100.0 / 255 - 44.7) > 0.05:
    bad.append("114/255 should read as 44.7%")
for percent, want in ((45.0, 115), (44.7, 114), (0.0, 0), (100.0, 255)):
    fake = FakeOcto()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        m.cmd_rgb_brightness(fake, ns(percent=percent, dry_run=False,
                                      backup=os.path.join(tmp, "b.bin")))
    # the fixture already holds 255, so 100% must write nothing at all
    got = fake.written[-1][m.RGB_BRIGHTNESS] if fake.written else \
        BLOBS[m.CTRL_REPORT_ID][m.RGB_BRIGHTNESS]
    if got != want:
        bad.append("%g%% stored %d, want %d" % (percent, got, want))
if bad:
    failures.append(("global brightness", "; ".join(bad)))
    print("  FAIL  global brightness: %s" % "; ".join(bad))
else:
    print("  ok    global brightness at 0x304, u8 0-255, nearest byte (45%% -> 115)")

# sequence vs colour sequence: flags, counts, and the RGB data source index
S1 = bytearray(open(os.path.join(FIX, "25-rgb-sequence-effects-1.bin"), "rb").read())
S2 = bytearray(open(os.path.join(FIX, "26-rgb-sequence-effects-2.bin"), "rb").read())
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
WB = bytearray(open(os.path.join(FIX, "27-rgb-wave-and-breathing-data-source.bin"), "rb").read())
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
CM = bytearray(open(os.path.join(FIX, "28-rgb-scanner-and-laser-colour-mode-2.bin"), "rb").read())
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
    if m.RGB_FLAG_NAMES[mode].get("colour_mode2") != 0x20:
        bad.append("%s colour_mode2 mask wrong" % name)
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
PA = bytearray(open(os.path.join(FIX, "35-profile-switch-before.bin"), "rb").read())
PB = bytearray(open(os.path.join(FIX, "36-profile-2-selected-writes-nothing.bin"), "rb").read())
PC = bytearray(open(os.path.join(FIX, "37-profile-2-flushed.bin"), "rb").read())
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
UM = bytearray(open(os.path.join(FIX, "39-name-fan7-with-umlaut-08.bin"), "rb").read())
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

# Channel protection is a local setting now, not a hardcoded pump list, so the
# test drives it through a throwaway config directory instead of the real one.
bad = []
with tempfile.TemporaryDirectory() as tmp:
    real_config_path = core.config_path
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        if m.protected_channels():
            bad.append("a fresh config should protect nothing")
        _quiet(m.cmd_protect, ns(channel=1, state="on"))
        _quiet(m.cmd_protect, ns(channel=2, state="on"))
        if m.protected_channels() != {1, 2}:
            bad.append("protect on did not stick: %r" % (m.protected_channels(),))
        for ch in (1, 2):
            try:
                m.guard_channel(ch, False)
                bad.append("channel %d was not refused" % ch)
            except SystemExit:
                pass
        try:
            m.guard_channel(1, True)          # --force must get through
        except SystemExit:
            bad.append("--force did not override the guard")
        try:
            m.guard_channel(3, False)         # unprotected must not be blocked
        except SystemExit:
            bad.append("channel 3 was refused but is not protected")
        _quiet(m.cmd_protect, ns(channel=1, state="off"))
        if m.protected_channels() != {2}:
            bad.append("protect off did not stick: %r" % (m.protected_channels(),))
    except Exception:
        bad.append(traceback.format_exc())
    finally:
        core.config_path = real_config_path
if bad:
    failures.append(("channel protection", "; ".join(bad)))
    print("  FAIL  channel protection: %s" % "; ".join(bad))
else:
    print("  ok    channel protection: on/off persists, guard refuses, --force wins")

# Percentages are bounded in the handler as well as in main(), because the
# handlers are reachable without going through the CLI. put_be16 must raise on
# an out-of-range word rather than mask it into a plausible one.
bad = []
try:
    for value in (-1.0, 100.01, 150.0, 700.0, float("nan")):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_mode_fixed(FakeOcto(), ns(channel=3, percent=value))
            bad.append("fixed %r was accepted" % value)
        except SystemExit:
            pass
    for off_value in (-1, 0x10000):
        try:
            m.put_be16(bytearray(4), 0, off_value)
            bad.append("put_be16 masked %r instead of raising" % off_value)
        except ValueError:
            pass
    buf = bytearray(4)
    m.put_be16(buf, 0, 0xFFFF)            # the legitimate top of the range
    if bytes(buf[:2]) != b"\xff\xff":
        bad.append("put_be16 mangled 0xFFFF")
except Exception:
    bad.append(traceback.format_exc())
if bad:
    failures.append(("input bounds", "; ".join(bad)))
    print("  FAIL  input bounds: %s" % "; ".join(bad))
else:
    print("  ok    percentages bounded in-handler; put_be16 raises, never masks")

# Config and backups are written while root but live in the invoking user's
# home, so a symlink left in place of one must not be followed.
bad = []
with tempfile.TemporaryDirectory() as tmp:
    real_config_path = core.config_path
    try:
        victim = os.path.join(tmp, "victim")
        with open(victim, "w") as fh:
            fh.write("untouched")
        link = os.path.join(tmp, "config.json")
        os.symlink(victim, link)
        core.config_path = lambda: link
        try:
            m.save_config({"protected": [1]})
            bad.append("save_config wrote through a symlink")
        except SystemExit:
            pass
        try:
            core.backup(bytearray(BLOBS[m.CTRL_REPORT_ID]), FakeOcto(), link)
            bad.append("backup wrote through a symlink")
        except SystemExit:
            pass
        if open(victim).read() != "untouched":
            bad.append("the symlink target was modified")
        plain = os.path.join(tmp, "plain.bin")
        if len(open(core.backup(bytearray(BLOBS[m.CTRL_REPORT_ID]), FakeOcto(), plain),
                    "rb").read()) \
                != m.CTRL_REPORT_SIZE:
            bad.append("a backup to a real path did not round-trip")
    except Exception:
        bad.append(traceback.format_exc())
    finally:
        core.config_path = real_config_path
if bad:
    failures.append(("symlink safety", "; ".join(bad)))
    print("  FAIL  symlink safety: %s" % "; ".join(bad))
else:
    print("  ok    config and backup refuse to write through a symlink")

# Device.write is the one path the fake device replaces everywhere else, so it
# gets its own check against a recording stand-in for the hidapi handle: the
# settings report goes out as a feature report followed by the command frame as
# an output report, a name report is followed by nothing, and a failed
# follow-up stops with an error instead of passing silently.
class RecordingHid:
    def __init__(self, fail_output=False):
        self.calls, self.fail_output = [], fail_output
    def send_feature_report(self, data):
        self.calls.append(("feature", bytes(data)))
        return len(data)
    def write(self, data):
        self.calls.append(("output", bytes(data)))
        return -1 if self.fail_output else len(data)
bad = []
saved_delay, core.CTRL_REPORT_DELAY = core.CTRL_REPORT_DELAY, 0
try:
    ctrl = bytearray(BLOBS[m.CTRL_REPORT_ID])
    labels = bytearray(BLOBS[m.LABEL_REPORT_ID])
    dev = core.Device(octo, "recording", SERIAL)
    dev.dev = RecordingHid()
    dev.write(ctrl)
    if dev.dev.calls != [("feature", bytes(ctrl)), ("output", m.SECONDARY_CTRL_REPORT)]:
        bad.append("settings write sent %s" % [(k, len(d)) for k, d in dev.dev.calls])
    dev.dev = RecordingHid()
    dev.write(labels)
    if dev.dev.calls != [("feature", bytes(labels))]:
        bad.append("name write sent %s" % [(k, len(d)) for k, d in dev.dev.calls])
    dev.dev = RecordingHid(fail_output=True)
    try:
        dev.write(ctrl)
        bad.append("a failed report 0x02 passed silently")
    except SystemExit:
        pass
    frame = m.SECONDARY_CTRL_REPORT
    if int.from_bytes(frame[9:11], "big") != m.crc16_usb(frame[1:9]):
        bad.append("command frame checksum is not CRC-16/USB over bytes 1-8")
except Exception:
    bad.append(traceback.format_exc())
finally:
    core.CTRL_REPORT_DELAY = saved_delay
if bad:
    failures.append(("write path", "; ".join(bad)))
    print("  FAIL  write path: %s" % "; ".join(bad))
else:
    print("  ok    write path: feature 0x03 then output 0x02; names alone; failures stop")

# RGB controllers keep to their channel's slots: 1-6 on channel 1, 7-12 on
# channel 2. Capture 03 has slot 1 in use on channel 1, and slots 7-11 (LEDs
# 1-79) on channel 2.
def _write(fn, ctrl=None, **kw):
    """Run a handler for real against the fake device; return what it wrote."""
    fake = FakeOcto(ctrl)
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        fn(fake, ns(dry_run=False, backup=os.path.join(tmp, "b.bin"), **kw))
    return fake.written[-1] if fake.written else None

def _refusal(fn, ctrl=None, **kw):
    """The message a handler refuses with, or None if it ran."""
    try:
        _write(fn, ctrl, **kw)
    except SystemExit as e:
        return str(e)
    return None

def _slots_changed(before, after):
    first = m.RGB_BASE
    return sorted({(i - first) // m.RGB_STRIDE + 1
                   for i in range(first, first + 12 * m.RGB_STRIDE) if before[i] != after[i]})

bad = []
try:
    CAP = BLOBS[m.CTRL_REPORT_ID]
    w = _write(m.cmd_rgb_create, channel=1, pos="29-40", effect="static", colour="FF0000")
    if _slots_changed(CAP, w) != [2]:
        bad.append("channel 1 create went to %s, want slot 2" % _slots_changed(CAP, w))
    w12 = _write(m.cmd_rgb_create, channel=2, pos="80-85", effect="static", colour="FF0000")
    if _slots_changed(CAP, w12) != [12]:
        bad.append("channel 2 create went to %s, want slot 12" % _slots_changed(CAP, w12))
    msg = _refusal(m.cmd_rgb_create, ctrl=w12, channel=2, pos="86-90", effect="static")
    if not msg or "full" not in msg or "7, 8, 9, 10, 11, 12" not in msg:
        bad.append("a seventh controller on channel 2 was not refused as full: %r" % msg)
    msg = _refusal(m.cmd_rgb_create, channel=2, pos="10-20", effect="static")
    if not msg or "controller 8" not in msg:
        bad.append("an overlapping create did not name controller 8: %r" % msg)
    w = _write(m.cmd_rgb_set, controller=7, pos="61-85")
    b7 = m.RGB_BASE + m.RGB_STRIDE * 6
    moved = [i for i in range(len(CAP) - 2) if CAP[i] != w[i]]
    if moved != [b7 + m.RGB_COUNT_OFF] or w[b7 + m.RGB_COUNT_OFF] != 25:
        bad.append("resizing controller 7 changed %s, want only its LED count (25)"
                   % ["%#05x" % i for i in moved])
    msg = _refusal(m.cmd_rgb_set, controller=7, pos="1-20")
    if not msg or "controller 8" not in msg:
        bad.append("resizing onto controller 8's LEDs was not refused: %r" % msg)
    w = _write(m.cmd_rgb_remove, what="controller", number=9)
    b9 = m.RGB_BASE + m.RGB_STRIDE * 8
    if bytes(w[b9:b9 + m.RGB_STRIDE]) != rgbpx.empty_slot(octo.RGB, 9) or w[b9] != 1:
        bad.append("removing controller 9 did not leave an empty slot on channel 2")
    if _slots_changed(CAP, w) != [9]:
        bad.append("removing controller 9 touched slots %s" % _slots_changed(CAP, w))
    w = _write(m.cmd_rgb_remove, what="channel", number=2)
    if _slots_changed(CAP, w) != [7, 8, 9, 10, 11]:
        bad.append("clearing channel 2 touched slots %s" % _slots_changed(CAP, w))
    if (rgbpx.empty_slot(octo.RGB, 2)[0], rgbpx.empty_slot(octo.RGB, 12)[0]) != (0, 1):
        bad.append("empty slots do not keep their channel's port byte")
except Exception:
    bad.append(traceback.format_exc())
if bad:
    failures.append(("rgb controllers", "; ".join(bad)))
    print("  FAIL  rgb controllers: %s" % "; ".join(bad))
else:
    print("  ok    rgb controllers: own-channel slots, full/overlap refusals, resize, removal")

# restore only accepts a backup of this device, unless --other-device says
# otherwise; --force is for the safety checks, not for this
bad = []
with tempfile.TemporaryDirectory() as tmp:
    def _restore(path, force=False, other=False):
        """None if it restored, else the refusal - or the crash, which fails."""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_restore(FakeOcto(), ns(files=[path], force=force, other_device=other))
            return None
        except SystemExit as e:
            return str(e) or "exit"
        except Exception:
            bad.append("restore crashed: %s" % traceback.format_exc().splitlines()[-1])
            return "crashed"
    ours = os.path.join(tmp, "octo-%s-03-20260915.bin" % SERIAL)
    theirs = os.path.join(tmp, "octo-99999-88888-03-20260915.bin")
    anonymous = os.path.join(tmp, "settings.bin")
    other_kind = os.path.join(tmp, "highflow-%s.bin" % SERIAL)
    for path in (ours, theirs, anonymous):
        open(path, "wb").write(BLOBS[m.CTRL_REPORT_ID])
    open(other_kind, "wb").write(open(os.path.join(ROOT, "highflow", "01-baseline.bin"), "rb").read())
    if _restore(ours):
        bad.append("a backup of this device was refused: %s" % _restore(ours))
    if not _restore(theirs) or "99999-88888" not in _restore(theirs):
        bad.append("another device's backup was not refused")
    if not _restore(anonymous) or "does not say which device" not in _restore(anonymous):
        bad.append("a backup without a serial was not refused")
    if _restore(theirs, other=True) or _restore(anonymous, other=True):
        bad.append("--other-device did not let another device's backup through")
    if not _restore(theirs, force=True):
        bad.append("--force let another device's backup through")
    if not _restore(other_kind, other=True) or "not a report of the Octo" \
            not in _restore(other_kind, other=True):
        bad.append("a high flow NEXT report was accepted by the Octo, even with --force")
if bad:
    failures.append(("restore origin", "; ".join(bad)))
    print("  FAIL  restore origin: %s" % "; ".join(bad))
else:
    print("  ok    restore: own backups pass; other or unknown origin needs --other-device; "
          "wrong type never")

# a protected channel blocks a restore only where the dump would change it
bad = []
with tempfile.TemporaryDirectory() as tmp:
    real_config = core.config_path
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        core.set_protected(SERIAL, {1})
        before = bytearray(BLOBS[m.CTRL_REPORT_ID])
        for name, change, refused in (("channel 3", 3, False), ("channel 1", 1, True)):
            dump = bytearray(before)
            block = m.channel_base(change) + m.OFF_SETPOINT
            dump[block] ^= 0x01
            core.reseal(dump)
            path = os.path.join(tmp, "octo-%s-03-%d.bin" % (SERIAL, change))
            open(path, "wb").write(dump)
            for force in (False, True):
                fake = FakeOcto()
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        m.cmd_restore(fake, ns(files=[path], force=force, other_device=False,
                                               dry_run=False, backup=os.path.join(tmp, "b.bin")))
                    outcome_ = None
                except SystemExit as exc:
                    outcome_ = exc
                if refused and not force:
                    if outcome_ is None or outcome_.code != core.EXIT_REFUSED or fake.written \
                            or "protected channel 1" not in str(outcome_):
                        bad.append("%s without --force: %r" % (name, outcome_))
                elif outcome_ is not None or len(fake.written) != 1:
                    bad.append("%s%s: %r" % (name, " with --force" if force else "", outcome_))
    finally:
        core.config_path = real_config
if bad:
    failures.append(("restore protection", "; ".join(bad)))
    print("  FAIL  restore protection: %s" % "; ".join(bad))
else:
    print("  ok    restore: a protected channel blocks it only where the dump changes it")

# protection belongs to one device's serial, never to a channel number alone
bad = []
with tempfile.TemporaryDirectory() as tmp:
    real_config_path = core.config_path
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        core.set_protected("11111-11111", {1})
        if core.protected_channels("22222-22222"):
            bad.append("protecting channel 1 on one Octo protected it on another")
        try:
            core.guard_channel("22222-22222", 1, False)
        except SystemExit:
            bad.append("the other Octo's channel 1 was refused as protected")
        try:
            core.guard_channel("11111-11111", 1, False)
            bad.append("the protected device's channel 1 was not refused")
        except SystemExit:
            pass
    except Exception:
        bad.append(traceback.format_exc())
    finally:
        core.config_path = real_config_path
if bad:
    failures.append(("protection per device", "; ".join(bad)))
    print("  FAIL  protection per device: %s" % "; ".join(bad))
else:
    print("  ok    protection is kept per serial")

# --device picks one device by serial, must come first, and is required
bad = []
two = [discovery.Found(octo, "11111-11111", b"/nonexistent/a"),
       discovery.Found(octo, "22222-22222", b"/nonexistent/b")]
real_attached = discovery.attached
discovery.attached = lambda: two
with tempfile.TemporaryDirectory() as tmp:
    real_config_path = core.config_path
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--device", "22222-22222", "fan", "set", "1", "protect", "on"])
        if (core.protected_channels("22222-22222"), core.protected_channels("11111-11111")) \
                != ({1}, set()):
            bad.append("--device 22222-22222 did not act on that device alone")
        # each must be refused for its own reason, not for some later failure
        for argv, why, reason in (
                (["--device", "33333-33333", "info"], "an unknown serial",
                 "No attached device has the serial"),
                (["fan", "set", "1", "mode", "fixed", "40"], "no --device",
                 "A device is required"),
                (["info", "fan", "--device", "11111-11111"], "--device not first",
                 "--device must come first"),
                (["--device"], "--device without a serial", "--device needs a serial")):
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    cli.main(argv)
                bad.append("%s was accepted" % why)
            except SystemExit as e:
                said = "%s %s" % (e.code if isinstance(e.code, str) else "", err.getvalue())
                if reason not in said:
                    bad.append("%s was refused, but not because of it: %r" % (why, said[:80]))
    except Exception:
        bad.append(traceback.format_exc())
    finally:
        core.config_path = real_config_path
        discovery.attached = real_attached
if bad:
    failures.append(("device selection", "; ".join(bad)))
    print("  FAIL  device selection: %s" % "; ".join(bad))
else:
    print("  ok    --device selects by serial, first and required; others refused")

# every command form in the plan parses; the malformed ones are refused
bad = []
ap = octo.build_parser("aqdctl --device %s" % SERIAL)
forms = ["info", "info rgb", "fan set 3 mode fixed 40", "fan set 3 mode target",
         "fan set 3 mode target 36 --sensor 2", "fan set 7 mode curve --linear 30 45 20 100",
         "fan set 6 mode follow 3", "fan set 3 limits 25 35", "fan set 3 fallback 35",
         "fan set 3 boost on", "fan set 3 hold-min off", "fan set 3 max-rpm",
         "fan set 3 pid --preset fast --d 600", "fan set 1 protect on",
         "rgb create 2 pos 1-15 --effect static --colour FF0000",
         "rgb set controller 7", "rgb set controller 7 pos 61-85 --param speed=30",
         "rgb remove controller 7", "rgb remove channel 2", "rgb switch on",
         "rgb brightness 45", "rgb effects", "rgb effects wave", "name list",
         "name fan 3 Front", "name controller 7", "name virtual 16 X",
         "sensor offset 1", "sensor offset 1 -0.6", "sensor flow", "backup -o x.bin",
         "restore x.bin --force"]
malformed = ["rgb create 2 pos 1-15", "rgb set 7", "rgb set controller 13",
             "rgb remove 2", "rgb set controller 7 61-85", "fan set 9 mode fixed 40",
             "name controller 13"]
for form in forms + malformed:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            ap.parse_args(form.split())
        ok = True
    except SystemExit:
        ok = False
    if ok != (form in forms):
        bad.append("%r %s" % (form, "was refused" if form in forms else "was accepted"))
if bad:
    failures.append(("command forms", "; ".join(bad)))
    print("  FAIL  command forms: %s" % "; ".join(bad))
else:
    print("  ok    command forms: %d parse, %d malformed ones refused" % (len(forms), len(malformed)))

# every Octo capture declares the layout the module decodes, and another is refused
bad = []
for f in sorted(os.listdir(FIX)):
    if f.endswith(".bin") and not f.endswith("-08.bin"):
        buf = open(os.path.join(FIX, f), "rb").read()
        if len(buf) == m.CTRL_REPORT_SIZE and core.layout_version(buf) != octo.STRUCTURE_ID:
            bad.append(f)
moved = bytearray(BLOBS[m.CTRL_REPORT_ID])
core.put_be16(moved, core.STRUCTURE_OFFSET, 3)
core.reseal(moved)
try:
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_info(FakeOcto(ctrl=moved), ns(section=None))
    bad.append("info ran on layout version 3")
except SystemExit as exc:
    if "layout version 3; aqdctl knows version 2" not in " ".join(str(exc).split()):
        bad.append("wrong refusal: %s" % exc)
if bad:
    failures.append(("layout version", "; ".join(bad)))
    print("  FAIL  layout version: %s" % "; ".join(bad))
else:
    print("  ok    layout version: every capture is at 2, and 3 is refused")

# the three ways of starting the tool, from the repository root and from elsewhere
import subprocess
bad = []
for cwd, argv in ((ROOT, ["aqdctl.py"]), (ROOT, ["-m", "aqdctl"]), (ROOT, ["aqdctl"]),
                  (tempfile.gettempdir(), [os.path.join(ROOT, "aqdctl")])):
    r = subprocess.run([sys.executable] + argv + ["--help-udev"], cwd=cwd,
                       capture_output=True, text=True)
    if r.returncode != 0 or "udev" not in r.stdout:
        bad.append("python3 %s (in %s): %s" % (" ".join(argv), cwd,
                                               (r.stderr.strip().splitlines() or ["no output"])[-1]))
if bad:
    failures.append(("starting the tool", "; ".join(bad)))
    print("  FAIL  starting the tool: %s" % "; ".join(bad))
else:
    print("  ok    starting the tool: aqdctl.py, -m aqdctl and the aqdctl directory")

# a protected channel refuses with the status scripts can tell apart
bad = []
with tempfile.TemporaryDirectory() as tmp:
    real_config = core.config_path
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        core.set_protected(SERIAL, {1})
        try:
            core.guard_channel(SERIAL, 1, False)
            bad.append("channel 1 was not refused")
        except SystemExit as exc:
            if exc.code != core.EXIT_REFUSED or "protected" not in str(exc):
                bad.append("status %r: %s" % (exc.code, exc))
        core.guard_channel(SERIAL, 1, True)          # --force passes
        core.guard_channel(SERIAL, 2, False)         # another channel passes
    finally:
        core.config_path = real_config
if bad:
    failures.append(("protected channel status", "; ".join(bad)))
    print("  FAIL  protected channel status: %s" % "; ".join(bad))
else:
    print("  ok    protected channel: refused with status 4, --force passes")

# the flow header's reading, through the kernel driver: 0.01 l/h, whatever
# its label says
bad = []
with tempfile.TemporaryDirectory() as tmp:
    for leaf, text in (("fan9_input", "19119"), ("fan9_label", "Flow speed [dL/h]"),
                       ("temp1_input", "26160")):
        with open(os.path.join(tmp, leaf), "w") as fh:
            fh.write(text + "\n")
    real = discovery.hwmon_dir
    discovery.hwmon_dir = lambda serial: tmp
    try:
        live = m.read_live(SERIAL)
    finally:
        discovery.hwmon_dir = real
if live.get("flow") != 191.19:
    bad.append("19119 read as %r l/h, not 191.19" % live.get("flow"))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    m.info_sensor(BLOBS[m.CTRL_REPORT_ID], [""] * 4, live)
if "reading: 191.19 l/h" not in out.getvalue():
    bad.append("info shows:\n" + out.getvalue())
if bad:
    failures.append(("flow unit", "; ".join(bad)))
    print("  FAIL  flow unit: %s" % "; ".join(bad))
else:
    print("  ok    flow header: 0.01 l/h through the kernel driver")

# ------------------------------------------------------------ keys and get
# The key list is pinned, every fixture reads under every key, and the values
# are what the fixtures' names say they hold.
import glob, json
from aqdctl import keys


def report(path):
    return open(path, "rb").read()


def fixture_keys(stem):
    fake = FakeOcto(report(os.path.join(FIX, stem + ".bin")))
    fake.blobs[m.LABEL_REPORT_ID] = report(os.path.join(FIX, stem + "-08.bin"))
    return keys.read(octo.key_table(), keys.Context(octo, SERIAL, fake))


def leaf_clashes(names):
    ordered = sorted(names)
    return [a for a, b in zip(ordered, ordered[1:]) if b.startswith(a + ".")]


def outcome(label, bad):
    if bad:
        failures.append((label, "; ".join(bad)))
        print("  FAIL  %s: %s" % (label, "; ".join(bad)))
    else:
        print("  ok    %s" % label)


bad = []
stems = sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(FIX, "[0-9]*.bin"))
               if not p.endswith("-08.bin") and os.path.exists(p[:-4] + "-08.bin"))
real_config = core.config_path
with tempfile.TemporaryDirectory() as tmp:
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        for stem in stems:
            try:
                rows = fixture_keys(stem)
                json.dumps({k.name: k.json(v) for k, v in rows})
                for k, v in rows:
                    k.text(v)
                clash = leaf_clashes([k.name for k, _v in rows])
                if clash:
                    bad.append("%s: %s is a prefix" % (stem, clash[0]))
            except Exception as exc:
                bad.append("%s: %s: %s" % (stem, type(exc).__name__, exc))
    finally:
        core.config_path = real_config
pinned = [line.strip() for line in open(os.path.join(HERE, "keys-octo.txt"))
          if line.strip() and not line.startswith("#")]
listed = [k.name for k in keys.schema(octo.key_table())]
if listed != pinned:
    bad.append("the key list differs from keys-octo.txt: gone %s, new %s, or reordered"
               % (sorted(set(pinned) - set(listed)), sorted(set(listed) - set(pinned))))
if leaf_clashes(listed):
    bad.append("%s is the prefix of another key" % leaf_clashes(listed)[0])
outcome("keys: all %d fixtures read under every key, the list is the pinned one" % len(stems),
        bad)

bad = []
for stem, want in (("21-fan-ch6-follows-ch3", {"fan.6.mode": "follow", "fan.6.follow": "3",
                                               "fan.3.mode": "target", "fan.3.follow": ""}),
                   ("13-fan-ch7-curve-automatic-setup", {"fan.7.mode": "curve",
                                                         "fan.7.curve.startup": "28.79"}),
                   ("24-rgb-brightness-45", {"rgb.brightness": "44.71"}),
                   ("39-name-fan7-with-umlaut", {"name.fan.7": "h\xf6ren"}),
                   ("15-profile-2-loaded", {"device.profile": "2"}),
                   ("17-fan-ch3-source-sensor-changed", {"fan.3.sensor": "2"}),
                   ("40-fan-pid-presets-ch3-to-ch6", {
                       "fan.3.pid.p": "2500", "fan.3.pid.i": "2000", "fan.3.pid.d": "500",
                       "fan.3.pid.reset": "1", "fan.3.pid.hysteresis": "0.1"}),
                   ("06-rgb-wave-with-data-source", {
                       "rgb.controller.7.effect": "wave", "rgb.controller.7.pos": "61-79",
                       "rgb.controller.7.colours": "FF8000",
                       "rgb.controller.7.background": "DD30A8",
                       "rgb.controller.7.param.speed": "30",
                       "rgb.controller.7.flag.reverse": "on",
                       "rgb.controller.7.sensor": "1"})):
    values = {k.name: k.text(v) for k, v in fixture_keys(stem)}
    for key, value in want.items():
        if values.get(key) != value:
            bad.append("%s: %s=%r, expected %r" % (stem, key, values.get(key), value))
points = dict((k.name, v) for k, v in fixture_keys("13-fan-ch7-curve-automatic-setup"))
if points["fan.7.curve.points"] != m.read_curve(report(os.path.join(
        FIX, "13-fan-ch7-curve-automatic-setup.bin")), 7):
    bad.append("fan.7.curve.points is not the stored curve")
outcome("keys: values as the fixtures hold them, written as the commands take them", bad)

# live keys come from the kernel driver and need no access to the device
bad = []


class Closed(FakeOcto):
    """A device that must not be opened."""
    def __enter__(self):
        raise AssertionError("the device was opened")


with tempfile.TemporaryDirectory() as tmp:
    for leaf, text in (("fan1_input", "3966"), ("power1_input", "1500000"),
                       ("in0_input", "11930"), ("curr1_input", "125"),
                       ("temp1_input", "24020"), ("fan9_input", "19115")):
        with open(os.path.join(tmp, leaf), "w") as fh:
            fh.write(text + "\n")
    real_hwmon, real_config = discovery.hwmon_dir, core.config_path
    discovery.hwmon_dir = lambda serial: tmp
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        core.set_protected(SERIAL, {1})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            keys.cmd_get(Closed(), argparse.Namespace(
                prefix=["live.fan.1", "live.sensor.1", "live.virtual.1", "live.flow",
                        "fan.1.protect", "fan.2.protect"],
                keys=False, json=False, value=False))
    except Exception as exc:
        out = io.StringIO("%s: %s" % (type(exc).__name__, exc))
    finally:
        discovery.hwmon_dir, core.config_path = real_hwmon, real_config
want = ("fan.1.protect=on\nfan.2.protect=off\nlive.fan.1.rpm=3966\nlive.fan.1.power=1.50\n"
        "live.fan.1.voltage=11.93\nlive.fan.1.current=0.125\nlive.sensor.1=24.02\n"
        "live.virtual.1=\nlive.flow=191.15\n")
if out.getvalue() != want:
    bad.append("got:\n" + out.getvalue())
outcome("get: live readings and protected channels without opening the device", bad)

# ------------------------------------------------------------------ batch
from aqdctl import batch


def octo_values(fake):
    ctx = keys.Context(octo, SERIAL, fake)
    return {k.name: k.text(k.read(ctx)) for k in keys.select(octo.key_table(), ctx)
            if k.settable and k.source != keys.CONFIG}


def octo_batch(text, ctrl=None, force=False):
    """(the failure, or None; the fake) after a batch on the Octo fixture."""
    fake = FakeOcto(ctrl)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "batch.txt")
        with open(path, "w") as fh:
            fh.write(text)
        args = argparse.Namespace(file=path, json=False, dry_run=False, yes=True,
                                  verbose=False, quiet=False, force=force,
                                  backup=os.path.join(tmp, "b.bin"), typed=["batch", path])
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                batch.cmd_batch(fake, args)
            return None, fake
        except SystemExit as exc:
            return exc, fake


bad = []
before = octo_values(FakeOcto())
real_config = core.config_path
with tempfile.TemporaryDirectory() as tmp:
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        # a setting of another mode lands without leaving the current mode
        exc, fake = octo_batch("fan.1.target.temperature=40\n")
        after = octo_values(fake)
        if exc or len(fake.written) != 1 or after["fan.1.mode"] != "fixed" or \
                after["fan.1.target.temperature"] != "40.00" or \
                after["fan.1.fixed.power"] != before["fan.1.fixed.power"]:
            bad.append("target temperature on a fixed channel: %s %s"
                       % (exc, {k: after[k] for k in after if k.startswith("fan.1.")}))
        exc, fake = octo_batch("fan.3.mode=curve\nfan.3.curve.startup=30\n")
        after = octo_values(fake)
        if exc or (after["fan.3.mode"], after["fan.3.curve.startup"]) != ("curve", "30.00"):
            bad.append("into curve mode: %s" % exc)
        exc, fake = octo_batch("fan.1.mode=follow\n")
        if not exc or exc.code != core.EXIT_INVALID or "fan.1.follow" not in str(exc):
            bad.append("follow without a channel: %r" % exc)
        exc, fake = octo_batch("fan.2.mode=fixed\nfan.2.fixed.power=50\nfan.2.follow=\n")
        after = octo_values(fake)
        if exc or (after["fan.2.mode"], after["fan.2.fixed.power"], after["fan.2.follow"]) \
                != ("fixed", "50.00", ""):
            bad.append("from follow to fixed: %s" % exc)
        # protected channels: a change is refused, an unchanged value is not one
        core.set_protected(SERIAL, {1})
        exc, fake = octo_batch("fan.1.limits.max=60\n")
        if not exc or exc.code != core.EXIT_REFUSED or fake.written or "line 1" not in str(exc):
            bad.append("protected channel: %r" % exc)
        exc, fake = octo_batch("fan.1.limits.max=60\n", force=True)
        if exc or len(fake.written) != 1:
            bad.append("protected channel with --force: %r" % exc)
        exc, fake = octo_batch("fan.1.limits.max=%s\n" % before["fan.1.limits.max"])
        if exc or fake.written:
            bad.append("an unchanged protected channel: %r" % exc)
        exc, fake = octo_batch("fan.2.protect=on\nfan.3.limits.max=150\n")
        if not exc or core.protected_channels(SERIAL) != {1}:
            bad.append("a failed batch changed the protected channels: %r %r"
                       % (exc, core.protected_channels(SERIAL)))
        exc, fake = octo_batch("fan.2.protect=on\nfan.1.protect=off\n")
        if exc or fake.written or core.protected_channels(SERIAL) != {2}:
            bad.append("protect keys: %r %r" % (exc, core.protected_channels(SERIAL)))
    finally:
        core.config_path = real_config
outcome("batch: fan modes and their settings, protected channels", bad)

bad = []
# a new effect brings its own parameters; two controllers can trade places
exc, fake = octo_batch("rgb.controller.1.effect=wave\nrgb.controller.1.param.speed=30\n"
                       "rgb.controller.1.background=000000\n")
after = octo_values(fake)
if exc or (after["rgb.controller.1.effect"], after["rgb.controller.1.param.speed"],
           after["rgb.controller.1.background"]) != ("wave", "30", "000000"):
    bad.append("static to wave: %s" % exc)
exc, fake = octo_batch("rgb.controller.8.pos=16-30\nrgb.controller.9.pos=1-15\n")
after = octo_values(fake)
if exc or (after["rgb.controller.8.pos"], after["rgb.controller.9.pos"]) != ("16-30", "1-15"):
    bad.append("swapping two controllers: %s" % exc)
exc, fake = octo_batch("rgb.controller.8.pos=16-30\n")
if not exc or exc.code != core.EXIT_INVALID or fake.written or "share LEDs" not in str(exc):
    bad.append("an overlap: %r" % exc)
exc, fake = octo_batch("rgb.controller.1.param.limits=2\n")
if not exc or "line 1" not in str(exc):
    bad.append("a parameter static does not have: %r" % exc)
# command lines create and remove controllers; key lines after them see the result
exc, fake = octo_batch("rgb remove controller 11\n"
                       "rgb create 2 pos 46-60 --effect static --colour 00FF00\n"
                       "rgb.controller.11.colours=0000FF\n")
after = octo_values(fake)
if exc or (after.get("rgb.controller.11.pos"), after.get("rgb.controller.11.colours")) \
        != ("46-60", "0000FF") or len(fake.written) != 1:
    bad.append("remove, create, then a key: %s" % exc)
exc, fake = octo_batch("rgb.controller.11.colours=0000FF\nrgb remove controller 11\n")
if exc or "rgb.controller.11.pos" in octo_values(fake):
    bad.append("a key, then remove: %s" % exc)
exc, fake = octo_batch("fan set 1 protect on\n")
if not exc or "fan.N.protect" not in str(exc):
    bad.append("protect as a command line: %r" % exc)
outcome("batch: RGB controllers - a new effect's keys, trading places, overlaps, "
        "created and removed by command lines", bad)

# every fixture's keys, given to another fixture with the same controllers,
# make every key equal; given back to their own, change nothing
bad = []
with tempfile.TemporaryDirectory() as tmp:
    core.config_path = lambda: os.path.join(tmp, "config.json")
    try:
        texts_of = {stem: octo_values(FakeOcto(report(os.path.join(FIX, stem + ".bin"))))
                    for stem in stems}
        for i, stem in enumerate(stems):
            source = texts_of[stem]
            ctrl = report(os.path.join(FIX, stem + ".bin"))
            exc, fake = octo_batch("".join("%s=%s\n" % kv for kv in source.items()), ctrl)
            if exc or fake.written:
                bad.append("%s onto itself: %s" % (stem, exc))
            pos = sorted(k for k in source if k.endswith(".pos"))
            peers = [x for x in stems if x != stem and
                     sorted(k for k in texts_of[x] if k.endswith(".pos")) == pos]
            if not peers:
                continue
            target = peers[i % len(peers)]
            exc, fake = octo_batch("".join("%s=%s\n" % kv for kv in source.items()),
                                   report(os.path.join(FIX, target + ".bin")), force=True)
            got = octo_values(fake)
            wrong = [k for k in source if got.get(k) != source[k]]
            if exc or wrong:
                bad.append("%s onto %s: %s %s" % (stem, target, exc,
                                                  [(k, source[k], got.get(k)) for k in wrong[:3]]))
    finally:
        core.config_path = real_config
outcome("batch: every fixture copies onto one with the same controllers, and onto itself "
        "changes nothing", bad[:4])

total = len(CASES) + 50
print("\n%d/%d passed" % (total - len(failures), total))
sys.exit(1 if failures else 0)
