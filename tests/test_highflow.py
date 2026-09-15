#!/usr/bin/env python3
"""Offline test of the high flow NEXT support, against the captures in
highflow/. No hardware needed.

The captures are the ground truth: each one changed a single setting in
Aquasuite (the file name says which) and undid the one before. So each
capture's decoded settings must show the named change, and must differ from
the capture before it only in that change and the undo.
"""
import argparse, builtins, contextlib, glob, io, os, shlex, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from aqdctl import cli, core, discovery, highflow, names, octo, rgbpx

FIX = os.path.join(ROOT, "highflow")
SERIAL = "12345-67890"          # the fake device's serial
failures = []
checks = 0


def check(label, bad):
    """Report one check: `bad` is a list of what went wrong, empty if nothing."""
    global checks
    checks += 1
    if bad:
        failures.append(label)
        print("  FAIL  %s:\n        %s" % (label, "\n        ".join(bad)))
    else:
        print("  ok    %s" % label)


def capture(stem, rid=core.CTRL_REPORT_ID):
    suffix = "" if rid == core.CTRL_REPORT_ID else "-%02x" % rid
    with open(os.path.join(FIX, stem + suffix + ".bin"), "rb") as fh:
        return bytearray(fh.read())


STEMS = sorted(os.path.basename(p)[:-4]
               for p in glob.glob(os.path.join(FIX, "[0-9][0-9]-*.bin"))
               if not p.endswith(("-08.bin", "-0c.bin")))
BY_NUMBER = {int(s[:2]): s for s in STEMS}
LOG = [line.split() for line in open(os.path.join(FIX, "85-live-readings-01.log"))
       if line.strip() and not line.startswith("#")]
LIVE = [bytes.fromhex(row[1]) for row in LOG]


class FakeHighflow:
    """Serves captured reports and a live report. Keeps what is written, so
    the read-back after a write verifies, and records commands. `live` may be
    a function, called for each live report."""
    kind, serial = highflow, SERIAL

    def __init__(self, stem="01-baseline", live=LIVE[0]):
        self.blobs = {core.CTRL_REPORT_ID: capture(stem),
                      core.LABEL_REPORT_ID: capture(stem, core.LABEL_REPORT_ID)}
        self.live, self.written, self.commands = live, [], []

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def read(self, report_id=core.CTRL_REPORT_ID): return bytearray(self.blobs[report_id])
    def read_input(self, size, timeout_ms=0):
        return self.live() if callable(self.live) else self.live
    def write(self, buf):
        self.written.append(bytes(buf))
        self.blobs[buf[0]] = bytearray(buf)
    def command(self, code): self.commands.append(code)


def run(fn, dev, args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(dev, args)
    return out.getvalue()


print("high flow NEXT, %d captures and %d live reports" % (len(STEMS), len(LIVE)))

# ------------------------------------------------------- every capture decodes
bad = []
for stem in STEMS:
    for rid, size in sorted(highflow.REPORT_SIZES.items()):
        buf = capture(stem, rid)
        stored, computed = core.report_crc(buf)
        if len(buf) != size or buf[0] != rid or stored != computed:
            bad.append("%s report %#04x: %d bytes, checksum %#06x/%#06x"
                       % (stem, rid, len(buf), stored, computed))
    try:
        highflow.decode(capture(stem))
        names.decode(capture(stem, core.LABEL_REPORT_ID), highflow.NAME_GROUPS)
        run(highflow.cmd_info, FakeHighflow(stem), argparse.Namespace(section=None))
    except Exception as exc:
        bad.append("%s: %s: %s" % (stem, type(exc).__name__, exc))
check("all %d captures: size, checksum, decode and info" % len(STEMS), bad)

# ------------------------------------------- each capture matches its name
SEC = {v: k for k, v in highflow.CHART_INTERVALS.items()}    # seconds -> stored
# The change each capture's name describes, as the value it must now hold.
EXPECT = {
    2: {},
    3: {"rgb_enable": 0},                                   # 0 means on
    4: {"rgb_brightness": 115},                             # Aquasuite's "45 %"
    5: {"water_quality_good": 133, "water_quality_bad": 505},
    6: {"coolant": 1},
    7: {"connector": 1},
    8: {"calibration": (-100, 100, 100, 0, 0, 0, 0, 0, 0, 0)},   # from 08-...txt
    9: {"offset_internal": -50},
    10: {"brightness": 1}, 11: {"brightness": 0},
    12: {"idle_brightness": 2}, 13: {"idle_brightness": 1}, 14: {"idle_brightness": 0},
    15: {"page_interval": 10}, 16: {"page_interval": highflow.PAGE_CHANGE_OFF},
    17: {"display_rotate": True}, 18: {"display_invert": True},
    19: {"display_auto-invert": False}, 20: {"display_device-keys-disabled": True},
    21: {"display_menu-lock": True},
    22: {"temperature_unit": 1}, 23: {"flow_unit": 2},
    24: {"chart1_source": 0}, 25: {"chart1_source": 1}, 26: {"chart1_source": 2},
    27: {"chart1_source": 3}, 28: {"chart1_source": 4}, 29: {"chart1_source": 5},
    30: {"chart1_source": 6},
    31: {"chart1_interval": SEC[0.5]},
    32: {"chart2_interval": SEC[10]},
    33: {"chart3_source": 5, "chart3_interval": SEC[600]},
    34: {"chart4_source": 0, "chart4_interval": SEC[300]},
    51: {"pages": (1, 16)}, 52: {"pages": (1, 5)}, 53: {"pages": (1, 3, 5)},
    54: {"usb_over_spec": 1, "usb_current": 500},
    55: {"usb_current": 600}, 56: {"usb_current": 700}, 57: {"usb_current": 2000},
    58: {"standby_when-usb-disconnected": False},
    59: {"standby_on-usb-suspend": False},
    60: {"standby_without-aquabus": True},
    61: {"standby_alarms-off": False}, 62: {"standby_display-off": False},
    63: {"standby_leds-off": False}, 64: {"standby_volume-stops": False},
    65: {"aquabus_address": 59}, 66: {"aquabus_address": 60}, 67: {"aquabus_address": 61},
    68: {"chart1_interval": SEC[1], "chart2_interval": SEC[30]},
    69: {"offset_external": -130},
    70: {"alarm_buzzer": False}, 71: {"alarm_blink-led": False},
    72: {"alarm_delay": 10}, 73: {"alarm_stop-signal": False},
    74: {"signal_output": 0}, 75: {"signal_output": 1}, 76: {"signal_output": 2},
    77: {"signal_output": 3}, 78: {"signal_output": 4}, 79: {"signal_output": 5},
    80: {"alarm_flow": True}, 81: {"alarm_internal": False},
    82: {"alarm_external": True}, 83: {"alarm_water-quality": True},
    84: {"alarm_external": True},
}
for n in range(35, 51):
    EXPECT[n] = {"pages": (n - 34,)}
# Undos that reach further back than the capture before, with the value they
# restored (LAYOUT.md, "Capture method").
ALSO = {
    5: {"rgb_enable": 2},            # 04 left the RGB switch on; 05 undid both
    17: {"page_interval": 60},       # re-enabling page changes writes 60
    23: {"page_interval": 5},
    58: {"usb_over_spec": 0, "usb_current": 500},
    68: {"flow_unit": 0},
}
bad = []
if sorted(EXPECT) != sorted(n for n in BY_NUMBER if n > 1):
    bad.append("the table does not cover exactly captures 02-84")
for n in sorted(EXPECT):
    stem, before = BY_NUMBER[n], highflow.decode(capture(BY_NUMBER[n - 1]))
    after = highflow.decode(capture(stem))
    want = dict(EXPECT[n], **ALSO.get(n, {}))
    for key, value in want.items():
        if after[key] != value:
            bad.append("%s: %s is %r, the name says %r" % (stem, key, after[key], value))
    changed = {k for k in after if after[k] != before[k]}
    allowed = set(want) | set(EXPECT.get(n - 1, {}))
    if changed - allowed:
        bad.append("%s also changed %s" % (stem, sorted(changed - allowed)))
    if EXPECT[n] and not changed & set(EXPECT[n]):
        bad.append("%s changed none of %s" % (stem, sorted(EXPECT[n])))
    if not EXPECT[n] and changed:
        bad.append("%s should change nothing, changed %s" % (stem, sorted(changed)))
    raw_before, raw_after = capture(BY_NUMBER[n - 1]), capture(stem)
    stray = [i for i in range(len(raw_after))
             if raw_after[i] != raw_before[i] and i not in highflow.mapped_offsets()]
    if stray:
        bad.append("%s changed unmapped bytes %s" % (stem, ", ".join(map(hex, stray))))
check("captures 02-84: each shows the change its name says, and only that", bad)

# ---------------------------------------------------- the baseline's values
s = highflow.decode(capture("01-baseline"))
bad = ["baseline %s is %r, expected %r" % (k, s[k], v) for k, v in (
    ("temperature_unit", 0), ("flow_unit", 0), ("page_interval", 5), ("pages", (2, 3)),
    ("brightness", 2), ("idle_brightness", 3),
    ("display_auto-invert", True), ("display_rotate", False),
    ("usb_over_spec", 0), ("usb_current", 500), ("aquabus_address", 58),
    ("offset_internal", 0), ("offset_external", -80),
    ("coolant", 0), ("connector", 0), ("water_quality_good", 128),
    ("water_quality_bad", 500), ("rgb_enable", 2), ("alarm_delay", 15),
    ("signal_output", 1),
    # 84-alarm-limits-and-ranges.txt: flow 45, sensor 40, external 45, quality 30
    ("limit_flow", 450), ("limit_internal", 4000), ("limit_external", 4500),
    ("limit_water-quality", 3000),
    ("alarm_buzzer", True), ("alarm_blink-led", True), ("alarm_stop-signal", True),
    ("alarm_flow", False), ("alarm_internal", True), ("alarm_external", False),
    ("alarm_water-quality", False),
    ("standby_when-usb-disconnected", True), ("standby_on-usb-suspend", True),
    ("standby_without-aquabus", False), ("standby_alarms-off", True),
    ("standby_display-off", True), ("standby_leds-off", True),
    ("standby_volume-stops", True),
    # 08-...txt: the calibration points, in l/h
    ("calibration_points", (200, 300, 500, 700, 1000, 1250, 1500, 2000, 2500, 3000)),
    ("chart1_source", 6), ("chart1_interval", SEC[5]),
    ("chart2_source", 1), ("chart2_interval", SEC[60]),
    ("chart3_interval", 1), ("chart4_interval", 1)) if s[k] != v]
# 34-...txt: the charts after the last chart capture
last = highflow.decode(capture(BY_NUMBER[34]))
for n, source, seconds in ((1, 6, 5), (2, 1, 60), (3, 5, 600), (4, 0, 300)):
    got = (last["chart%d_source" % n], highflow.CHART_INTERVALS.get(last["chart%d_interval" % n]))
    if got != (source, seconds):
        bad.append("after capture 34, chart %d is %r, the note says %r" % (n, got, (source, seconds)))
check("baseline values and the notes beside the captures", bad)

# ---------------------------------------------------------------- names
labels = names.decode(capture("01-baseline", core.LABEL_REPORT_ID), highflow.NAME_GROUPS)
want = {"controller": ["LED Controller %d" % i for i in range(1, 9)],
        "sensor": ["Cold Point", "External Sensor", "Flow", "Conductivity",
                   "Power Dissipation", "Volume", "Water quality"],
        "virtual": ["Soft. Sensor %d" % i for i in range(1, 9)]}
bad = ["%s: %r, expected %r" % (g, labels[g], want[g]) for g in want if labels[g] != want[g]]
ident = highflow.identify(capture("01-baseline", core.LABEL_REPORT_ID))
if ident != "sensors: Cold Point, External Sensor, Flow":
    bad.append("identify: %r" % ident)
check("name groups decode to the baseline's names", bad)

# ------------------------------------------------- live readings vs. hwmon
bad = []
previous = None
for row, rep in zip(LOG, LIVE):
    hw = dict(kv.split("=") for kv in row[3:])
    live = highflow.decode_live(rep)
    pairs = (("fan1", live["flow"], 10), ("fan2", live["water_quality"], 100),
             ("fan3", live["conductivity"], 10), ("in0", live["vcc5"], 1000),
             ("in1", live["vcc5_usb"], 1000), ("temp1", live["internal"], 1000))
    for leaf, value, scale in pairs:
        if value is None or round(value * scale) != int(hw[leaf]):
            bad.append("t=%s %s: report %r, hwmon %s" % (row[0], leaf, value, hw[leaf]))
    if hw["temp2"] != "-" or live["external"] is not None:
        bad.append("t=%s external: report %r, hwmon %s" % (row[0], live["external"], hw["temp2"]))
    # The driver hands out -ENODATA as an unsigned number; there is no reading.
    if int(hw["power1"]) != 2 ** 32 - 61 or live["power"] is not None:
        bad.append("t=%s power: report %r, hwmon %s" % (row[0], live["power"], hw["power1"]))
    if (live["firmware"], live["serial"]) != (1017, "00000-00000"):
        bad.append("t=%s firmware %r, serial %r (scrubbed to zeros)"
                   % (row[0], live["firmware"], live["serial"]))
    if previous is not None:
        dt = float(row[0]) - float(previous[0])
        if not 0 <= live["since_reset"] - previous[1]["since_reset"] <= 2:
            bad.append("t=%s time since reset jumped" % row[0])
        # about 666 impulses per litre, at the logged flow
        rate = (live["impulses"] - previous[1]["impulses"]) / dt
        if not 30 < rate < 45:
            bad.append("t=%s %.1f impulses/s at %.1f l/h" % (row[0], rate, live["flow"]))
        if live["volume"] < previous[1]["volume"]:
            bad.append("t=%s volume went down" % row[0])
    previous = (row[0], live)
first = highflow.decode_live(LIVE[0])
# The owner's Aquasuite reading, a few minutes before the log: about 1036500 l,
# 690563900 impulses, 179 d 18:43:20 since the reset.
if not (1036400 < first["volume"] < 1036700 and 690_500_000 < first["impulses"] < 690_700_000
        and 179 * 86400 + 18 * 3600 < first["since_reset"] < 179 * 86400 + 19 * 3600):
    bad.append("counters %r do not match Aquasuite's reading"
               % ((first["volume"], first["impulses"], first["since_reset"]),))
check("all %d live reports match the kernel driver's readings beside them" % len(LIVE), bad)

# ------------------------------------------------ kernel driver fallback
bad = []
with tempfile.TemporaryDirectory() as tmp:
    for leaf, text in (("fan1_input", "2065"), ("fan2_input", "8683"), ("fan3_input", "177"),
                       ("in0_input", "4700"), ("in1_input", "4890"),
                       ("temp1_input", "26160"), ("power1_input", "4294967235")):
        with open(os.path.join(tmp, leaf), "w") as fh:
            fh.write(text + "\n")
    real = discovery.hwmon_dir
    discovery.hwmon_dir = lambda serial: tmp if serial == SERIAL else None
    try:
        hw = highflow.read_live_hwmon(SERIAL)
        missing = highflow.read_live_hwmon("99999-99999")
    finally:
        discovery.hwmon_dir = real
    for key, want in (("flow", 206.5), ("water_quality", 86.83), ("conductivity", 17.7),
                      ("vcc5", 4.7), ("vcc5_usb", 4.89), ("internal", 26.16),
                      ("external", None), ("power", None), ("volume", None)):
        if hw.get(key) != want:
            bad.append("%s: %r, expected %r" % (key, hw.get(key), want))
    if missing != {}:
        bad.append("another device's hwmon node was used: %r" % missing)
fake = FakeHighflow(live=None)
out = run(highflow.cmd_info, fake, argparse.Namespace(section="sensor"))
if "no live readings" not in out or "no reading" not in out:
    bad.append("without report 0x01 or hwmon, info does not say so:\n" + out)
check("kernel driver fallback, found by serial, and no live readings at all", bad)

# ------------------------------------------------------------ info output
bad = []
out = run(highflow.cmd_info, FakeHighflow(), argparse.Namespace(section=None))
for text in ("high flow NEXT %s   firmware 1017   (live readings from report 0x01)" % SERIAL,
             "brightness        low, when idle: off",
             "page change       every 5 s",
             "pages             2 flow, 3 internal temperature",
             "rotate off, invert off, auto-invert on, device-keys on, menu-lock off",
             "chart 1           system voltage, every 5 s",
             "chart 3           water quality, every 0.5 s",
             "calibration       no corrections",
             "volume            1036567 l, 690586965 impulses, 179 d 18:52:19 since",
             '"Cold Point"               26.16 C       offset +0.00 C',
             "no sensor     offset -0.80 C",
             "100 % at 12.8 uS/cm, 0 % at 50.0 uS/cm",
             "power          \"Power Dissipation\"        needs the external sensor",
             "flow           off    below 45.0 l/h     now 206.5 l/h",
             "internal       on     above 40.00 C      now 26.16 C",
             "external       off    above 45.00 C      no reading",
             "signal output     flow-sensor: Generate high flow sensor (53068) signal",
             "channel 1: controllers 1-6, up to 90 LEDs; channel 2: controllers 7-8",
             "3  LED Controller 3   channel 1, LEDs 1-9   effect colour gradient",
             "USB current limit 500 mA",
             "standby allowed   when-usb-disconnected on, on-usb-suspend on, without-aquabus off"):
    if text not in out:
        bad.append("missing: %r" % text)
out = run(highflow.cmd_info, FakeHighflow(BY_NUMBER[8]), argparse.Namespace(section="flow"))
if "calibration       20 l/h: -1.00, 30 l/h: +1.00, 50 l/h: +1.00" not in out:
    bad.append("capture 08's calibration is not shown:\n" + out)
out = run(highflow.cmd_info, FakeHighflow(BY_NUMBER[16]), argparse.Namespace(section="display"))
if "page change       off" not in out:
    bad.append("capture 16's page change is not shown as off")
out = run(highflow.cmd_info, FakeHighflow(BY_NUMBER[57]), argparse.Namespace(section="system"))
if "USB current limit 2000 mA, above the USB limit allowed" not in out:
    bad.append("capture 57's USB current is not shown:\n" + out)
out = run(highflow.cmd_info, FakeHighflow(), argparse.Namespace(section="alarm"))
if "display" in out or "system" in out or "alarm" not in out:
    bad.append("'info alarm' printed other sections")
check("info shows what the baseline and the captures hold", bad)

# ------------------------------------------------------- alarm directions
bad = []
by_key = {a.key: a for a in highflow.ALARMS}
for key, limit, reading, want in (("flow", 45, 30, True), ("flow", 45, 206.5, False),
                                  ("water-quality", 30, 20, True),
                                  ("water-quality", 30, 86.8, False),
                                  ("internal", 40, 41, True), ("internal", 40, 26, False),
                                  ("external", 45, 50, True), ("external", 45, None, None)):
    got = by_key[key].crossed(limit, reading)
    if got is not want:
        bad.append("%s limit %s, reading %s: %r, expected %r" % (key, limit, reading, got, want))
out = run(highflow.cmd_info, FakeHighflow(BY_NUMBER[84]), argparse.Namespace(section="alarm"))
if "external       on     above 45.00 C      no reading" not in out:
    bad.append("capture 84 (external alarm on, no sensor):\n" + out)
check("alarms: flow and water quality fire below, temperatures above", bad)

# ------------------------------------------------------------------ backup
bad = []
with tempfile.TemporaryDirectory() as tmp:
    real = core.data_dir
    core.data_dir = lambda: tmp
    try:
        run(core.cmd_backup, FakeHighflow(), argparse.Namespace(output=None))
        run(core.cmd_backup, FakeHighflow(), argparse.Namespace(output=os.path.join(tmp, "x.bin")))
    finally:
        core.data_dir = real
    files = sorted(os.listdir(tmp))
    for rid, size in ((0x03, 682), (0x08, 773)):
        hit = [f for f in files if f.startswith("highflow-%s-%02x-" % (SERIAL, rid))]
        if len(hit) != 1 or os.path.getsize(os.path.join(tmp, hit[0])) != size:
            bad.append("default backup of report %#04x: %r" % (rid, files))
    for name, size in (("x.bin", 682), ("x-08.bin", 773)):
        if name not in files or os.path.getsize(os.path.join(tmp, name)) != size:
            bad.append("-o backup %s missing or wrong size: %r" % (name, files))
# An Octo report is not a high flow NEXT report.
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "octo-%s-03-x.bin" % SERIAL)
    with open(path, "wb") as fh:
        fh.write(open(os.path.join(ROOT, "octo", "01-baseline.bin"), "rb").read())
    err = io.StringIO()
    fake = FakeHighflow()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            core.cmd_restore(fake, argparse.Namespace(file=path, force=True, dry_run=True,
                                                      verbose=False, yes=True, backup=None))
        bad.append("an Octo report was accepted for the high flow NEXT")
    except SystemExit as exc:
        if "is not a report of the high flow NEXT" not in str(exc):
            bad.append("wrong refusal: %s" % exc)
    if fake.written:
        bad.append("something was written")
check("backup names carry type and serial; an Octo report is refused", bad)

# ------------------------------------------------------ the command line
bad = []
HFN, OCTO = "07417-13891", "10989-29223"
both = [discovery.Found(octo, OCTO, b"/nonexistent/o"),
        discovery.Found(highflow, HFN, b"/nonexistent/h")]
real_attached, real_device = discovery.attached, core.Device
discovery.attached = lambda: both
opened = []


def fake_device(kind, path, serial):
    opened.append((kind.NAME, serial))
    return FakeHighflow()


core.Device = fake_device
try:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["info"])
    for text in ("Octo             %s" % OCTO, "high flow NEXT   %s" % HFN):
        if text not in out.getvalue():
            bad.append("device list lacks %r:\n%s" % (text, out.getvalue()))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["--device", HFN, "info", "alarm"])
    if opened != [("highflow", HFN)] or "alarm            state" not in out.getvalue():
        bad.append("--device %s info alarm: opened %r" % (HFN, opened))
    for argv, reason in ((["--device", HFN, "fan", "set", "1", "mode", "fixed", "40"],
                          "the high flow NEXT has no fans"),
                         (["--device", HFN, "display", "brightness", "dim"],
                          "not a brightness level")):
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cli.main(argv)
            bad.append("%s was accepted (%s)" % (" ".join(argv), reason))
        except SystemExit as exc:
            if exc.code != 2:
                bad.append("%s: exit %r, expected a usage error" % (" ".join(argv), exc.code))
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            cli.main(["--device", HFN, "--help"])
    except SystemExit:
        pass
    if "high flow NEXT" not in out.getvalue() or "fan" in out.getvalue().split("examples")[0]:
        bad.append("--device %s --help is not the high flow NEXT's:\n%s" % (HFN, out.getvalue()))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["--help-udev"])
    for pid in ("f011", "f012"):
        if 'ATTRS{idProduct}=="%s"' % pid not in out.getvalue():
            bad.append("--help-udev lacks product id %s" % pid)
finally:
    discovery.attached, core.Device = real_attached, real_device
check("command line: the device list shows both, --device picks the high flow NEXT", bad)

# ================================================================== writes
BACKUPS = tempfile.mkdtemp()
real_data_dir = core.data_dir
core.data_dir = lambda: BACKUPS       # the pre-write backups land here
PREFIX = "aqdctl --device %s" % SERIAL
PARSER = highflow.build_parser(PREFIX)


def do(fake, line, answer="y"):
    """Run one command line against a fake device, as cli.main does once it
    has chosen the device. Returns (stdout, stderr, exit), exit being the
    refusal message, or None if the command ran through."""
    argv = shlex.split(line)
    args = PARSER.parse_args(argv)
    args.typed = argv
    out, err = io.StringIO(), io.StringIO()
    real_input = builtins.input
    builtins.input = lambda prompt="": (out.write(prompt), answer)[1]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args.func(fake, args)
        code = None
    except SystemExit as exc:
        code = exc.code if exc.code else None
    finally:
        builtins.input = real_input
    return out.getvalue(), err.getvalue(), code


def flat(text):
    """Text with its line wrapping undone, for phrase checks."""
    return " ".join(str(text).split())


def changed_bytes(a, b):
    return {i for i in range(len(a) - 2) if a[i] != b[i]}


# ------------------------------------------------------------ capture replay
# The command that makes each capture's change. Applied to the capture before,
# it must produce the capture's value and touch no byte the capture left alone.
REPLAY = {
    3: "rgb switch on", 4: "rgb brightness 45",
    5: "sensor water-quality 13.3 50.5", 6: "flow coolant distilled",
    7: "flow connector under-7mm", 8: "flow calibration -1,1,1,0,0,0,0,0,0,0",
    9: "sensor offset internal -0.5",
    10: "display brightness medium", 11: "display brightness high",
    12: "display idle-brightness low", 13: "display idle-brightness medium",
    14: "display idle-brightness high", 15: "display page-change 10",
    16: "display page-change off", 17: "display rotate on", 18: "display invert on",
    19: "display auto-invert off", 20: "display device-keys off",
    21: "display menu-lock on", 22: "display units temperature fahrenheit",
    23: "display units flow gallons",
    24: "display chart 1 --source flow",
    25: "display chart 1 --source internal-temperature",
    26: "display chart 1 --source external-temperature",
    27: "display chart 1 --source conductivity",
    28: "display chart 1 --source water-quality",
    29: "display chart 1 --source power-dissipation",
    30: "display chart 1 --source system-voltage",
    31: "display chart 1 --interval 0.5", 32: "display chart 2 --interval 10",
    33: "display chart 3 --source power-dissipation --interval 600",
    34: "display chart 4 --source flow --interval 300",
    51: "display pages 1,16", 52: "display pages 1,5", 53: "display pages 1,3,5",
    55: "system usb-current 600 --force", 56: "system usb-current 700 --force",
    57: "system usb-current 2000 --force",
    58: "system standby when-usb-disconnected off",
    59: "system standby on-usb-suspend off", 60: "system standby without-aquabus on",
    61: "system in-standby alarms-off off", 62: "system in-standby display-off off",
    63: "system in-standby leds-off off", 64: "system in-standby volume-stops off",
    65: "system aquabus-address 59", 66: "system aquabus-address 60",
    67: "system aquabus-address 61",
    68: ["display chart 1 --interval 1", "display chart 2 --interval 30"],
    69: "sensor offset external -1.3",
    70: "alarm buzzer off", 71: "alarm blink-led off", 72: "alarm startup-delay 10",
    73: "alarm stop-signal off",
    74: "signal-output fan-speed", 75: "signal-output flow-sensor",
    76: "signal-output fan-speed-from-flow",
    77: "signal-output power-switch --force", 78: "signal-output on-during-alarm --force",
    79: "signal-output off-during-alarm --force",
    80: "alarm flow on --force",            # 79 left the output in off-during-alarm
    81: "alarm internal off", 82: "alarm external on --force",   # no external sensor
    83: "alarm water-quality on", 84: "alarm external on --force",
}
for n in range(35, 51):
    REPLAY[n] = "display pages %d" % (n - 34)
bad = []
for n in sorted(REPLAY):
    stem, prior = BY_NUMBER[n], BY_NUMBER[n - 1]
    fake = FakeHighflow(prior)
    lines = REPLAY[n] if isinstance(REPLAY[n], list) else [REPLAY[n]]
    for line in lines:
        out, err, code = do(fake, line)
        if code is not None:
            bad.append("%s: %r refused: %s" % (stem, line, code))
    if not fake.written:
        bad.append("%s: %r wrote nothing" % (stem, lines))
        continue
    written, target = fake.blobs[core.CTRL_REPORT_ID], capture(stem)
    got, want = highflow.decode(written), highflow.decode(target)
    for key in EXPECT[n]:
        if got[key] != want[key]:
            bad.append("%s: %r gave %s=%r, the capture has %r" % (stem, lines, key, got[key],
                                                                  want[key]))
    extra = changed_bytes(capture(prior), written) - changed_bytes(capture(prior), target)
    if extra:
        bad.append("%s: %r also changed %s" % (stem, lines, ", ".join(map(hex, sorted(extra)))))
    stored, computed = core.report_crc(written)
    if stored != computed:
        bad.append("%s: stale checksum" % stem)
    # Where the replay needed --force, it must be refused without it.
    for line in lines:
        if "--force" in line:
            out, err, code = do(FakeHighflow(prior), line.replace(" --force", ""))
            if code is None or not str(code).startswith("Nothing written."):
                bad.append("%s: %r without --force was not refused" % (stem, line))
check("replay: %d commands reproduce their capture's change, and nothing else"
      % len(REPLAY), bad)

# --------------------------------------------------------------- USB current
bad = []
WARNING = ("[### ATTENTION - DANGEROUS COMMAND ###]\n"
           "Setting a USB current above 0.5A can lead to damages on\n"
           "the USB header of the motherboard. The USB spec allows\n"
           "only for a current 0.5A at maximum. Use at your own risk.\n"
           "An external power supply should be used for safety.\n")
fake = FakeHighflow()
out, err, code = do(fake, "system usb-current 600")
if code != ("Nothing written. Increasing max current above 500 mA requires --force:\n"
            "  aqdctl --device %s system usb-current 600 --force" % SERIAL):
    bad.append("refusal: %r" % code)
if not err.startswith(WARNING):
    bad.append("warning on stderr: %r" % err)
if fake.written or out:
    bad.append("600 mA without --force wrote %d reports, printed %r" % (len(fake.written), out))
fake = FakeHighflow()
out, err, code = do(fake, "system usb-current 600 --force")
written = fake.written[-1] if fake.written else capture("01-baseline")
if code is not None or core.be16(written, highflow.USB_CURRENT) != 600 \
        or written[highflow.USB_OVER_SPEC] != 1:
    bad.append("600 mA with --force: exit %r, current %d, flag %d"
               % (code, core.be16(written, highflow.USB_CURRENT),
                  written[highflow.USB_OVER_SPEC]))
if not out.startswith(WARNING) or out.index(WARNING) > out.index("Proceed?"):
    bad.append("with --force the warning must come before the prompt:\n" + out)
for line in ("system usb-current 2001", "system usb-current 2001 --force",
             "system usb-current 400 --force", "system usb-current 650 --force"):
    fake = FakeHighflow()
    out, err, code = do(fake, line)
    if code is None or fake.written:
        bad.append("%r was not refused" % line)
fake = FakeHighflow(BY_NUMBER[57])            # 2000 mA, over-spec allowed
out, err, code = do(fake, "system usb-current 500")
written = fake.written[-1] if fake.written else b""
if code is not None or not written or core.be16(written, highflow.USB_CURRENT) != 500 \
        or written[highflow.USB_OVER_SPEC] != 0 or "ATTENTION" in out + err:
    bad.append("back to 500 mA: exit %r, output %r" % (code, out + err))


class Terminal(io.StringIO):
    def isatty(self): return True


tty = Terminal()
saved = os.environ.pop("NO_COLOR", None)
core.warn_danger("x", tty)
os.environ["NO_COLOR"] = "1"
plain = Terminal()
core.warn_danger("x", plain)
os.environ.pop("NO_COLOR")
if saved is not None:
    os.environ["NO_COLOR"] = saved
if tty.getvalue() != "\033[31m[### ATTENTION - DANGEROUS COMMAND ###]\033[0m\nx\n":
    bad.append("no red header on a terminal: %r" % tty.getvalue())
if plain.getvalue() != "[### ATTENTION - DANGEROUS COMMAND ###]\nx\n":
    bad.append("NO_COLOR still coloured: %r" % plain.getvalue())
help_out = io.StringIO()
try:
    with contextlib.redirect_stdout(help_out):
        PARSER.parse_args(["system", "usb-current", "--help"])
except SystemExit:
    pass
if "500 mA is the USB limit. Values above it need --force: they can damage the " \
        "motherboard's USB header." not in " ".join(help_out.getvalue().split()):
    bad.append("usb-current --help lacks the rule:\n" + help_out.getvalue())
check("USB current: warning, refusal with the command to copy, --force, limits", bad)

# ------------------------------------------------------------ signal output
bad = []
for prior, line, refused in (
        ("01-baseline", "signal-output power-switch", True),
        ("01-baseline", "signal-output on-during-alarm", True),
        ("01-baseline", "signal-output off-during-alarm", True),
        ("01-baseline", "signal-output fan-speed", False),
        (BY_NUMBER[79], "signal-output fan-speed", True),        # leaving mode 5
        (BY_NUMBER[79], "signal-output power-switch", True),
        (BY_NUMBER[77], "alarm buzzer off", True),               # alarm change in mode 3
        (BY_NUMBER[77], "alarm startup-delay 20", True),
        (BY_NUMBER[78], "system in-standby alarms-off off", True),
        (BY_NUMBER[77], "display brightness high", False),       # not an alarm change
        (BY_NUMBER[77], "system in-standby leds-off off", False),
        ("01-baseline", "alarm buzzer off", False)):
    fake = FakeHighflow(prior)
    out, err, code = do(fake, line)
    if refused and (code is None or fake.written):
        bad.append("%s: %r was not refused" % (prior[:2], line))
    if not refused and (code is not None or not fake.written):
        bad.append("%s: %r was refused: %s" % (prior[:2], line, code))
    if refused and code and ("--force:\n  aqdctl --device %s %s --force" % (SERIAL, line)) not in code:
        bad.append("%s: %r refusal lacks the command to copy: %s" % (prior[:2], line, code))
check("signal output: power modes, leaving them, and alarm changes in them need --force", bad)

# -------------------------------------------------------------------- alarms
# LIVE[0]: flow 206.5 l/h, internal 26.16 C, no external sensor, quality 86.83 %.
bad = []
for prior, line, refused in (
        ("01-baseline", "alarm external on", True),              # no reading
        ("01-baseline", "alarm flow on", False),                 # 206.5 > 45
        ("01-baseline", "alarm flow on --limit 300", True),      # 206.5 < 300
        ("01-baseline", "alarm flow --limit 300", False),        # off: only a note
        ("01-baseline", "alarm internal --limit 20", True),      # on, 26.16 > 20
        ("01-baseline", "alarm internal --limit 50", False),
        ("01-baseline", "alarm internal off", False),
        ("01-baseline", "alarm water-quality on --limit 90", True),   # 86.83 < 90
        ("01-baseline", "alarm water-quality on --limit 80", False),
        ("01-baseline", "sensor offset internal 15", True),      # 41.16 > 40
        ("01-baseline", "sensor offset internal 10", False),
        (BY_NUMBER[83], "sensor water-quality 12.8 19", True),   # -> 20.97 % < 30
        (BY_NUMBER[83], "sensor water-quality 12.8 20", False),  # -> 31.94 %
        ("01-baseline", "alarm flow --limit 1001", True),
        ("01-baseline", "alarm internal --limit 4", True),
        ("01-baseline", "alarm startup-delay 4", True),
        ("01-baseline", "alarm startup-delay 101", True)):
    fake = FakeHighflow(prior)
    out, err, code = do(fake, line)
    if refused and (code is None or fake.written):
        bad.append("%s: %r was not refused" % (prior[:2], line))
    if not refused and (code is not None or not fake.written):
        bad.append("%s: %r was refused: %s" % (prior[:2], line, code))
out, err, code = do(FakeHighflow(), "alarm flow --limit 300")
if "note: the flow alarm is off" not in out:
    bad.append("no note for a crossed limit on a disabled alarm:\n" + out)
out, err, code = do(FakeHighflow(), "alarm external on")
if not code or "the external sensor reads nothing" not in flat(code):
    bad.append("refusal does not say the sensor reads nothing: %r" % code)
out, err, code = do(FakeHighflow(live=None), "alarm flow on")
if not code or "no live readings are available" not in flat(code):
    bad.append("without live readings: %r" % code)
out, err, code = do(FakeHighflow(), "alarm external on --force")
if code is not None or "--force: The external alarm would fire at once" not in flat(out):
    bad.append("--force does not say what it overrides: %r" % out)
check("alarms: refused when they would fire at once, with or without a reading", bad)

# -------------------------------------------------------------- volume reset
bad = []
if core.command_frame(highflow.VOLUME_RESET_COMMAND) != bytes.fromhex("020000006400000000" "3cce"):
    bad.append("command 0x64 frame differs from the USB capture")
zeroed = bytearray(LIVE[0])
for off, value in ((highflow.LIVE_VOLUME, 0), (highflow.LIVE_IMPULSES, 10),
                   (highflow.LIVE_SINCE_RESET, 1)):
    zeroed[off:off + 4] = value.to_bytes(4, "big")
reports = []
fake = FakeHighflow(live=lambda: reports.pop(0))
reports[:] = [LIVE[0], bytes(zeroed)]
out, err, code = do(fake, "volume reset")
if fake.commands != [0x64] or "1036567 l" not in out or "Reset: 0 l, 10 impulses" not in out:
    bad.append("reset: commands %r, output %r" % (fake.commands, out))
fake = FakeHighflow()
out, err, code = do(fake, "volume reset -y", answer="n")
if fake.commands or code != "Aborted." or "-y does not apply" not in out:
    bad.append("-y skipped the question: commands %r, %r" % (fake.commands, out))
fake = FakeHighflow()
out, err, code = do(fake, "volume reset --dry-run")
if fake.commands or "Reset the volume counter?" in out:
    bad.append("--dry-run sent or asked: %r" % out)
fake = FakeHighflow(live=None)
out, err, code = do(fake, "volume reset")
if fake.commands or not code:
    bad.append("without a live report the reset was sent")
check("volume reset: shows the counters, always asks, -y does not skip it", bad)

# ------------------------------------------------------------------ restore
bad = []
with tempfile.TemporaryDirectory() as tmp:
    for stem, refused_for in ((BY_NUMBER[57], "USB current"), (BY_NUMBER[79], "signal output"),
                              (BY_NUMBER[9], None)):
        path = os.path.join(tmp, "highflow-%s-03-%s.bin" % (SERIAL, stem[:2]))
        shutil.copy(os.path.join(FIX, stem + ".bin"), path)
        fake = FakeHighflow()
        out, err, code = do(fake, "restore %s" % path)
        if refused_for and (code is None or fake.written):
            bad.append("restoring %s went past the %s check" % (stem, refused_for))
        if not refused_for and (code is not None or not fake.written):
            bad.append("restoring %s was refused: %s" % (stem, code))
        fake = FakeHighflow()
        out, err, code = do(fake, "restore %s --force" % path)
        if code is not None or bytes(fake.blobs[core.CTRL_REPORT_ID]) != bytes(capture(stem)):
            bad.append("restoring %s with --force: %s" % (stem, code))
    out, err, code = do(FakeHighflow(), "restore %s" % os.path.join(tmp, "missing.bin"))
    if not code or not code.startswith("Cannot read"):
        bad.append("a missing file: %r" % code)
check("restore: runs the same safety checks as the commands", bad)

# ---------------------------------------------------------------------- rgb
bad = []
layout = highflow.RGB


def slot(buf, n):
    base = layout.slot_base(n)
    return bytes(buf[base:base + 70])


fake = FakeHighflow()
out, err, code = do(fake, "rgb create 2 pos 1-11 --effect static")
if code is None or fake.written:
    bad.append("channel 2 accepted LED 11")
out, err, code = do(fake, "rgb create 2 pos 1-10 --effect static --colour 0000FF")
buf = fake.blobs[core.CTRL_REPORT_ID]
if code or "controller 7 (new) on channel 2" not in out or slot(buf, 7)[:4] != bytes([1, 0, 10, 1]):
    bad.append("first controller on channel 2: %r %s" % (code, slot(buf, 7)[:4].hex()))
out, err, code = do(fake, "rgb create 2 pos 1-5 --effect static")
if not code or "overlap controller 7" not in code:
    bad.append("overlap on channel 2 not refused: %r" % code)
do(fake, "rgb set controller 7 pos 1-5")
out, err, code = do(fake, "rgb create 2 pos 6-10 --effect static")
if code or "controller 8 (new)" not in out:
    bad.append("second controller on channel 2: %r" % code)
do(fake, "rgb set controller 8 pos 6-8")
out, err, code = do(fake, "rgb create 2 pos 9-10 --effect static")
if not code or "Channel 2 is full: its controllers 7, 8" not in code:
    bad.append("third controller on channel 2: %r" % code)
fake = FakeHighflow()
out, err, code = do(fake, "rgb create 1 pos 5-12 --effect static")
if not code or "overlap controller 3" not in code:
    bad.append("overlap with controller 3 not refused: %r" % code)
out, err, code = do(fake, "rgb create 1 pos 10-20 --effect static")
if code or "controller 1 (new) on channel 1, LEDs 10-20" not in out:
    bad.append("channel 1 did not take its lowest free controller: %r %r" % (code, out))
fake = FakeHighflow()
before = capture("01-baseline")
do(fake, "rgb set controller 3 pos 1-20")
if changed_bytes(before, fake.blobs[core.CTRL_REPORT_ID]) != {layout.slot_base(3) + 2}:
    bad.append("resizing changed more than the LED count")
fake = FakeHighflow()
do(fake, "rgb remove controller 3")
if slot(fake.blobs[core.CTRL_REPORT_ID], 3) != rgbpx.empty_slot(layout, 3) \
        or slot(fake.blobs[core.CTRL_REPORT_ID], 3)[0] != 0:
    bad.append("removing controller 3 did not empty its slot on channel 1")
fake = FakeHighflow()
do(fake, "rgb create 2 pos 1-10 --effect static")
do(fake, "rgb remove controller 7")
if slot(fake.blobs[core.CTRL_REPORT_ID], 7)[0] != 1:
    bad.append("removing controller 7 lost channel 2's port byte")
fake = FakeHighflow()
out, err, code = do(fake, "rgb set controller 3 --sensor '#5'")
if code or core.be16(fake.blobs[core.CTRL_REPORT_ID], layout.slot_base(3) + 6) != 5:
    bad.append("--sensor '#5': %r" % code)
out, err, code = do(FakeHighflow(), "rgb set controller 3 --sensor 1")
if not code or "have not been captured" not in code:
    bad.append("--sensor 1 was accepted without a known numbering: %r" % code)
check("rgb: channel 2 holds controllers 7-8 and LEDs 1-10; slots, overlaps, sources", bad)

# -------------------------------------------------------------------- names
bad = []
fake = FakeHighflow()
before = capture("01-baseline", core.LABEL_REPORT_ID)
do(fake, "name sensor internal 'Loop A'")
after = fake.blobs[core.LABEL_REPORT_ID]
if names.read_name(after, 0x183) != "Loop A" or \
        changed_bytes(before, after) - set(range(0x183, 0x183 + 24)):
    bad.append("name sensor internal: %r" % names.read_name(after, 0x183))
do(fake, "name virtual 8 Test")
if names.read_name(fake.blobs[core.LABEL_REPORT_ID], 0x243 + 7 * 24) != "Test":
    bad.append("name virtual 8")
if any(n == core.CTRL_REPORT_ID for n in (w[0] for w in fake.written)):
    bad.append("a name write touched the settings report")
check("names: sensor members and software sensors land in their slots", bad)

# ------------------------------------------------------------- command forms
bad = []
forms = ["info", "info alarm", "display brightness high", "display brightness",
         "display idle-brightness off", "display pages 2,3", "display pages",
         "display page-change off", "display page-change 10", "display rotate on",
         "display invert off", "display auto-invert on", "display device-keys off",
         "display menu-lock on", "display units temperature fahrenheit",
         "display units flow gallons", "display chart 1 --source flow --interval 60",
         "display chart 4", "flow coolant distilled", "flow connector over-7mm",
         "flow calibration", "flow calibration -1,1,1,0,0,0,0,0,0,0",
         "sensor offset internal -0.5", "sensor offset external",
         "sensor water-quality 12.8 50", "sensor water-quality",
         "alarm flow on --limit 45", "alarm flow --limit 45", "alarm flow",
         "alarm internal off", "alarm external on --force", "alarm water-quality on",
         "alarm startup-delay 10", "alarm buzzer off", "alarm blink-led on",
         "alarm stop-signal on", "signal-output", "signal-output power-switch --force",
         "system usb-current 600 --force", "system usb-current",
         "system standby when-usb-disconnected on", "system standby on-usb-suspend off",
         "system standby without-aquabus on", "system in-standby alarms-off on",
         "system in-standby display-off off", "system in-standby leds-off on",
         "system in-standby volume-stops off", "system aquabus-address 59",
         "system aquabus-address", "volume reset", "volume reset -y",
         "rgb create 2 pos 1-10 --effect static", "rgb set controller 8",
         "rgb remove controller 3", "rgb remove channel 1", "rgb switch on",
         "rgb brightness 45", "rgb effects", "name list", "name controller 8 X",
         "name sensor water-quality X", "name virtual 8", "backup -o x.bin",
         "restore x.bin --force"]
malformed = ["display brightness dim", "display units flow gallon", "display chart 5",
             "flow coolant water", "sensor offset middle 1", "alarm voltage on",
             "signal-output off", "system aquabus-address 57", "rgb set controller 9",
             "name sensor voltage X", "name controller 9 X", "fan set 1 mode fixed 40",
             "display rotate yes", "system usb-current 600 --forced",
             "display brightness high --force"]
for form in forms + malformed:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            PARSER.parse_args(shlex.split(form))
        ok = True
    except SystemExit:
        ok = False
    if ok != (form in forms):
        bad.append("%r %s" % (form, "was refused" if form in forms else "was accepted"))
check("command forms: %d parse, %d malformed ones refused" % (len(forms), len(malformed)),
      bad)

core.data_dir = real_data_dir
shutil.rmtree(BACKUPS)

print("\n%d/%d passed" % (checks - len(failures), checks))
sys.exit(1 if failures else 0)
