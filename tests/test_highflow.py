#!/usr/bin/env python3
"""Offline test of the high flow NEXT support, against the captures in
highflow/. No hardware needed.

The captures are the ground truth: each one changed a single setting in
Aquasuite (the file name says which) and undid the one before. So each
capture's decoded settings must show the named change, and must differ from
the capture before it only in that change and the undo.
"""
import argparse, builtins, contextlib, glob, io, os, shlex, shutil, struct, sys, tempfile

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
    def read(self, report_id=core.CTRL_REPORT_ID, check=True):
        buf = bytearray(self.blobs[report_id])
        if check and report_id == core.CTRL_REPORT_ID:
            core.check_layout(self.kind, buf)      # as core.Device.read does
        return buf
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
if "calibration       20 l/h: -1.00 %, 30 l/h: +1.00 %, 50 l/h: +1.00 %" not in out:
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
HFN, OCTO = SERIAL, "09876-54321"
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


class Refusal(str):
    """A refusal's message, carrying the exit status it would end with."""
    def __new__(cls, message, status):
        obj = str.__new__(cls, message)
        obj.status = status
        return obj


def do(fake, line, answer="y"):
    """Run one command line against a fake device, as cli.main does once it
    has chosen the device. Returns (stdout, stderr, exit), exit being the
    refusal message - a Refusal, with its .status - or None if the command ran
    through. `answer` is what the confirmation gets; the question is written
    to stdout so tests can see where it came."""
    argv = shlex.split(line)
    args = PARSER.parse_args(argv)
    args.typed = argv
    out, err = io.StringIO(), io.StringIO()
    real_confirm = core.confirm
    core.confirm = lambda question, notice=None, hint=None: (
        out.write(question + " [y/N] "), answer.strip().lower() in ("y", "yes"))[1]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args.func(fake, args)
        code = None
    except core.Failure as exc:
        code = Refusal(exc.message, exc.code)
    except SystemExit as exc:
        code = Refusal(exc.code, core.EXIT_INVALID) if exc.code else None
    finally:
        core.confirm = real_confirm
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
    7: "flow connector under-7mm", 8: "flow calibration --corrections -1,1,1,0,0,0,0,0,0,0",
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
         "flow calibration", "flow calibration --corrections -1,1,1,0,0,0,0,0,0,0",
         "flow calibration --points 20,31,51,70,99,124,149,202,248,300",
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

# ------------------------------ Aquasuite's own calibration edits (USB capture)
# 10-highflow-flow-calibration.pcapng holds 18 settings writes, one per field
# Aquasuite changed: seven of the ten flow rates and all ten corrections. Each
# write differs from the one before in exactly one value, so the matching
# command, applied to one write, must reproduce the next byte for byte.
def usb_writes(path):
    """The 682-byte SET_REPORT(feature) payloads of a USBPcap file, in order."""
    data, off, out = open(path, "rb").read(), 0, []
    while off < len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12:
            break
        if btype == 6:                                  # enhanced packet block
            caplen = struct.unpack_from("<I", data, off + 20)[0]
            pkt = data[off + 28: off + 28 + caplen]
            hlen, xfer = struct.unpack_from("<H", pkt, 0)[0], pkt[22]
            if xfer == 2 and caplen >= hlen + 8 and pkt[27] == 0:
                setup, payload = pkt[28:36], pkt[36:hlen + 8 + 682]
                if setup[0] == 0x21 and len(payload) == highflow.CTRL_REPORT_SIZE:
                    out.append(bytearray(payload))
        off += blen
    return out


CAP = os.path.join(ROOT, "usb-captures", "10-highflow-flow-calibration.pcapng")
bad = []
writes = usb_writes(CAP)
if len(writes) != 18:
    bad.append("expected 18 settings writes in the capture, found %d" % len(writes))
for n, buf in enumerate(writes, 1):
    stored, computed = core.report_crc(buf)
    if stored != computed:
        bad.append("write %d has a bad checksum" % n)
for n in range(1, len(writes)):
    before, target = writes[n - 1], writes[n]
    a, b = highflow.decode(before), highflow.decode(target)
    argv = []
    if a["calibration_points"] != b["calibration_points"]:
        argv.append("--points " + ",".join("%g" % (p / 10.0) for p in b["calibration_points"]))
    if a["calibration"] != b["calibration"]:
        argv.append("--corrections " + ",".join("%g" % (c / 100.0) for c in b["calibration"]))
    if not argv:
        bad.append("writes %d and %d differ outside the calibration" % (n, n + 1))
        continue
    fake = FakeHighflow()
    fake.blobs[core.CTRL_REPORT_ID] = bytearray(before)
    out, err, code = do(fake, "flow calibration " + " ".join(argv))
    written = fake.blobs[core.CTRL_REPORT_ID]
    if code is not None:
        bad.append("write %d: refused: %s" % (n + 1, code))
    elif bytes(written) != bytes(target):
        diff = [hex(i) for i in range(len(target)) if written[i] != target[i]]
        bad.append("write %d: differs from Aquasuite's at %s" % (n + 1, ", ".join(diff)))
check("the %d calibration edits in Aquasuite's USB capture are reproduced exactly"
      % (len(writes) - 1), bad)

# the rates the tool reads back are the ones Aquasuite shows
first, last = highflow.decode(writes[0]), highflow.decode(writes[-1])
bad = []
if [p / 10.0 for p in first["calibration_points"]] != [20, 30, 50, 70, 100, 125, 150, 200, 250, 300]:
    bad.append("the capture does not start from the factory rates: %s" % (first["calibration_points"],))
if [p / 10.0 for p in last["calibration_points"]] != [20, 31, 51, 70, 99, 124, 149, 202, 248, 300]:
    bad.append("end rates: %s" % (last["calibration_points"],))
if [c / 100.0 for c in last["calibration"]] != [0, 1, 1, 1, -1, -1, -1, 1, -1, 2]:
    bad.append("end corrections: %s" % (last["calibration"],))
fake = FakeHighflow()
fake.blobs[core.CTRL_REPORT_ID] = bytearray(writes[-1])
out, err, code = do(fake, "flow calibration")
for text in ("20 l/h", "31 l/h", "+1.00", "-1.00", "+2.00"):
    if text not in out:
        bad.append("the table does not show %r:\n%s" % (text, out))
if fake.written:
    bad.append("showing the table wrote to the device")
# 11-...-min-max: Aquasuite wrote both ends of the correction range, +50 and
# -50. Its second write must come out of the first one exactly.
minmax = usb_writes(os.path.join(ROOT, "usb-captures",
                                 "11-highflow-flow-calibration-min-max.pcapng"))
if len(minmax) != 2:
    bad.append("expected 2 writes in the min/max capture, found %d" % len(minmax))
else:
    ends = [c / 100.0 for c in highflow.decode(minmax[-1])["calibration"]]
    if (max(ends), min(ends)) != (highflow.CALIBRATION_LIMIT, -highflow.CALIBRATION_LIMIT):
        bad.append("the capture's extremes are %r, the bound is +-%g"
                   % (ends, highflow.CALIBRATION_LIMIT))
    fake = FakeHighflow()
    fake.blobs[core.CTRL_REPORT_ID] = bytearray(minmax[0])
    out, err, code = do(fake, "flow calibration --corrections "
                        + ",".join("%g" % c for c in ends))
    if code is not None or bytes(fake.blobs[core.CTRL_REPORT_ID]) != bytes(minmax[1]):
        bad.append("writing both extremes does not match Aquasuite's write: %s" % code)
for line, why in (("flow calibration --points 20,31,51,70,99,124,149,202,248", "only nine rates"),
                  ("flow calibration --points 20,31,51,70,99,124,149,202,300,248", "not rising"),
                  ("flow calibration --points 20,31,51,70,99,124,149,202,248,751", "beyond 750 l/h"),
                  ("flow calibration --points -1,31,51,70,99,124,149,202,248,300", "a negative rate"),
                  ("flow calibration --corrections 60,0,0,0,0,0,0,0,0,0", "beyond +50"),
                  ("flow calibration --corrections -50.1,0,0,0,0,0,0,0,0,0", "beyond -50")):
    fake = FakeHighflow()
    out, err, code = do(fake, line)
    if code is None or fake.written:
        bad.append("%s was accepted (%s)" % (line, why))
fake = FakeHighflow()
out, err, code = do(fake, "flow calibration --points 0,31,51,70,99,124,149,202,248,750")
if code is not None or not fake.written:
    bad.append("the ends of Aquasuite's range, 0 and 750 l/h, were refused: %s" % code)
elif highflow.decode(fake.blobs[core.CTRL_REPORT_ID])["calibration_points"][:1] != (0,):
    bad.append("a rate of 0 was not stored as 0")
check("calibration: the table reads back, and bad input is refused", bad)


# --------------------------- colour gradient, from the USB captures 12-14
# Each capture switches one control and puts it back, so one write must turn
# into the other with the matching flag or parameter.
GRADIENT = os.path.join(ROOT, "usb-captures", "%s.pcapng")
bad = []
for name, flag, bit in (("13-highflow-rgb-gradient-reverse-direction-on-then-off",
                         "reverse_direction", 0x08),
                        ("14-highflow-rgb-gradient-reverse-rotation-on-then-off",
                         "reverse_rotation", 0x10)):
    w = usb_writes(GRADIENT % name)
    if len(w) != 2:
        bad.append("%s: expected 2 writes, found %d" % (name[:2], len(w)))
        continue
    on, off = w                      # the capture switches it on, then off again
    base = highflow.RGB.slot_base(1) + rgbpx.RGB_FLAGS_OFFSET
    if not (on[base] & bit) or off[base] & bit:
        bad.append("%s: %#04x is not the bit that moved (%#04x -> %#04x)"
                   % (name[:2], bit, on[base], off[base]))
    if rgbpx.RGB_FLAG_NAMES[0x21].get(flag) != bit:
        bad.append("%s: the table does not map %s to %#04x" % (name[:2], flag, bit))
    for source, target, argv in ((off, on, "--flag " + flag),
                                 (on, off, "--no-flag " + flag)):
        fake = FakeHighflow()
        fake.blobs[core.CTRL_REPORT_ID] = bytearray(source)
        out, err, code = do(fake, "rgb set controller 1 " + argv)
        if code is not None or bytes(fake.blobs[core.CTRL_REPORT_ID]) != bytes(target):
            bad.append("%s: 'rgb set controller 1 %s' does not reproduce the capture: %s"
                       % (name[:2], argv, code))

w = usb_writes(GRADIENT % "12-highflow-rgb-gradient-775-up-back-down")
if len(w) != 2:
    bad.append("capture 12: expected 2 writes, found %d" % len(w))
else:
    base = highflow.RGB.slot_base(1)
    moved = [k for k in range(9)
             if rgbpx.get_param(w[0], base, k) != rgbpx.get_param(w[1], base, k)]
    if moved != [4] or (rgbpx.get_param(w[0], base, 4),
                        rgbpx.get_param(w[1], base, 4)) != (777, 775):
        bad.append("capture 12: parameters %s moved, expected only 4 (777 -> 775)" % moved)
    fake = FakeHighflow()
    fake.blobs[core.CTRL_REPORT_ID] = bytearray(w[1])
    # by name and by index, both reproducing Aquasuite's write
    for argv in ("--param limit1=777", "--param 4=777"):
        fake = FakeHighflow()
        fake.blobs[core.CTRL_REPORT_ID] = bytearray(w[1])
        out, err, code = do(fake, "rgb set controller 1 " + argv)
        if code is not None or bytes(fake.blobs[core.CTRL_REPORT_ID]) != bytes(w[0]):
            bad.append("capture 12: %r does not reproduce the capture: %s" % (argv, code))
    # the two parameters the owner confirmed against Aquasuite
    if rgbpx.RGB_PARAM_NAMES[0x21][3] != "limits" or rgbpx.get_param(w[0], base, 3) != 1:
        bad.append("capture 12: the limit count is not where the table says")
    if rgbpx.RGB_PARAM_NAMES[0x21][4] != "limit1":
        bad.append("capture 12: the first limit is not named")

# 15-...-rotation-speed moves parameter 2 alone, twice
w = usb_writes(GRADIENT % "15-highflow-rgb-gradient-rotation-speed")
if len(w) != 3:
    bad.append("capture 15: expected 3 writes, found %d" % len(w))
else:
    base = highflow.RGB.slot_base(1)
    speeds = [rgbpx.get_param(b, base, 2) for b in w]
    moved = {k for a, b in zip(w, w[1:]) for k in range(9)
             if rgbpx.get_param(a, base, k) != rgbpx.get_param(b, base, k)}
    if speeds != [19, 20, 0] or moved != {2}:
        bad.append("capture 15: parameter 2 reads %s, parameters %s moved"
                   % (speeds, sorted(moved)))
    if rgbpx.RGB_PARAM_NAMES[0x21][2] != "rotation":
        bad.append("capture 15: parameter 2 is not named")
    for source, target, value in ((w[0], w[1], 20), (w[1], w[2], 0)):
        fake = FakeHighflow()
        fake.blobs[core.CTRL_REPORT_ID] = bytearray(source)
        out, err, code = do(fake, "rgb set controller 1 --param rotation=%d" % value)
        if code is not None or bytes(fake.blobs[core.CTRL_REPORT_ID]) != bytes(target):
            bad.append("capture 15: rotation=%d does not reproduce the capture: %s"
                       % (value, code))

# what is named shows up, what is not stays raw and unwritable
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rgbpx.describe_rgb(usb_writes(GRADIENT % "13-highflow-rgb-gradient-reverse-direction-on-then-off")[0],
                       highflow.RGB, 1, "strip", highflow.source_label)
text = out.getvalue()
for want in ("limits=1", "limit1=775", "rotation=", "reverse_direction",
             "colour 1:", "background default", "(unused)"):
    if want not in text:
        bad.append("'info rgb' on a gradient lacks %r:\n%s" % (want, text))
if "unknown bits" in text:
    bad.append("the gradient's constant flag bits are still reported as unknown:\n" + text)
# one of them missing would mean this firmware writes the effect differently
probe = bytearray(usb_writes(GRADIENT % "13-highflow-rgb-gradient-reverse-direction-on-then-off")[1])
probe[highflow.RGB.slot_base(1) + rgbpx.RGB_FLAGS_OFFSET] &= ~0x04
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rgbpx.describe_rgb(probe, highflow.RGB, 1, "strip", highflow.source_label)
if "expected bits 0x04 not set" not in out.getvalue():
    bad.append("a missing constant bit is not reported:\n" + out.getvalue())
check("colour gradient: the two captured flags and the limit count are named", bad)

# ----------------- the gradient's limits and colours, from USB capture 16
# 16-...-add-remove-limits walks 2, 3, 2 and 1 limits. Its colours live in
# palette entries 2-5, one more than the number of limits, and the entries past
# them repeat the last colour - so the matching command must reproduce each
# write from the one before, byte for byte.
bad = []
w = usb_writes(GRADIENT % "16-highflow-rgb-gradient-add-remove-limits-194-388-775")
base = highflow.RGB.slot_base(1)
if len(w) != 4:
    bad.append("capture 16: expected 4 writes, found %d" % len(w))
else:
    if [rgbpx.get_param(b, base, 3) for b in w] != [2, 3, 2, 1]:
        bad.append("capture 16: the limit counts are not 2, 3, 2, 1")
    for n, buf in enumerate(w, 1):
        limits = rgbpx.get_param(buf, base, 3)
        pal = [rgbpx.read_entry(buf, highflow.RGB, 1, e) for e in range(6)]
        if any(e != pal[2 + limits] for e in pal[3 + limits:]):
            bad.append("write %d: the entries past the %d colours do not repeat the last"
                       % (n, limits + 1))
        if [rgbpx.get_param(buf, base, k) for k in (4, 5, 6)][limits:] != \
                [rgbpx.get_param(buf, base, 6)] * (3 - limits):
            bad.append("write %d: the unused positions do not repeat the last" % n)
    YELLOW, RED = "CEC028", "DD2227"
    for source, target, argv in (
            (w[0], w[1], "--colour %s,%s,%s,%s --param limit1=194 "
                         "--param limit2=388" % (YELLOW, YELLOW, YELLOW, RED)),
            (w[2], w[3], "--colour %s,%s --param limit1=775" % (YELLOW, RED))):
        fake = FakeHighflow()
        fake.blobs[core.CTRL_REPORT_ID] = bytearray(source)
        out, err, code = do(fake, "rgb set controller 1 " + argv)
        written = fake.blobs[core.CTRL_REPORT_ID]
        # A colour given as RRGGBB can land one step off Aquasuite's saturation:
        # its picker works in HSV, which is what the report stores, so the RRGGBB
        # it displays is the lossy view. Everything else must match byte for byte,
        # the checksum included once those bytes are accounted for.
        soft = {highflow.RGB.slot_base(1) + rgbpx.RGB_COLOUR + 4 * e + 2 for e in range(6)}
        diff = [i for i in range(682 - 2) if written[i] != target[i]]
        off_by_one = [i for i in diff if i in soft and abs(written[i] - target[i]) <= 1]
        if code is not None:
            bad.append("capture 16: %r refused: %s" % (argv, code))
        elif set(diff) - set(off_by_one):
            bad.append("capture 16: %r differs from Aquasuite at %s"
                       % (argv, ", ".join(hex(i) for i in set(diff) - set(off_by_one))))
        elif not diff and bytes(written) != bytes(target):
            bad.append("capture 16: %r left a stale checksum" % argv)
    # the start and the end are 0 and 1000 in every gradient, and unwritable
    for buf in w:
        if (rgbpx.get_param(buf, base, 0), rgbpx.get_param(buf, base, 1)) != (0, 1000):
            bad.append("capture 16: the start and end are not 0 and 1000")
    for key in ("start", "end"):
        if key not in rgbpx.RGB_PARAM_NAMES[0x21]:
            bad.append("%s is not named" % key)
        fake = FakeHighflow()
        fake.blobs[core.CTRL_REPORT_ID] = bytearray(w[0])
        out, err, code = do(fake, "rgb set controller 1 --param %s=500" % key)
        if code is None or "cannot be moved" not in flat(code) or fake.written:
            bad.append("%s was written: %r" % (key, code))
    # the limit count follows from the colours and cannot be typed
    for line, why in (("rgb set controller 1 --colour %s" % YELLOW, "one colour"),
                      ("rgb set controller 1 --colour " + ",".join([YELLOW] * 5), "five"),
                      ("rgb set controller 1 --param limits=2", "limits by hand")):
        fake = FakeHighflow()
        fake.blobs[core.CTRL_REPORT_ID] = bytearray(w[0])
        out, err, code = do(fake, line)
        if code is None or fake.written:
            bad.append("%s was accepted (%s)" % (line, why))
    # 'rgb effects' describes it from the same tables
    out, err, code = do(FakeHighflow(), "rgb effects colour_gradient")
    for want in ("--colour RRGGBB,RRGGBB[,...up to 4]", "rotation",
                 "limit1", "reverse_rotation"):
        if want not in out:
            bad.append("'rgb effects colour_gradient' lacks %r:\n%s" % (want, out))
    if "limits" in out.split("parameters")[1].split("flags")[0]:
        bad.append("'rgb effects' offers 'limits' as a parameter to set")
check("colour gradient: limits, their colours and the palette match capture 16", bad)


# --------------------------------------- the calibrated flow is at 0x53
# The live report carries the flow twice: 0x51 before the calibration table
# and 0x53 after it. In capture 10 they part as soon as a correction near the
# flow becomes non-zero.
def usb_sequence(path):
    """('write', settings) and ('live', report) in capture order."""
    data, off, out = open(path, "rb").read(), 0, []
    while off < len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12:
            break
        if btype == 6:
            caplen = struct.unpack_from("<I", data, off + 20)[0]
            pkt = data[off + 28: off + 28 + caplen]
            hlen, xfer = struct.unpack_from("<H", pkt, 0)[0], pkt[22]
            payload = pkt[hlen:]
            if xfer == 1 and len(payload) == highflow.INPUT_REPORT_SIZE and payload[0] == 1:
                out.append(("live", bytes(payload)))
            elif xfer == 2 and pkt[27] == 0 and pkt[28] == 0x21 \
                    and len(pkt) - 36 >= highflow.CTRL_REPORT_SIZE:
                out.append(("write", bytes(pkt[36:36 + highflow.CTRL_REPORT_SIZE])))
        off += blen
    return out


bad, parted = [], 0
for kind, buf in usb_sequence(CAP):
    if kind != "live":
        continue
    live = highflow.decode_live(buf)
    if (live["flow"], live["flow_raw"]) != (core.be16(buf, 0x53) / 10.0, core.be16(buf, 0x51) / 10.0):
        bad.append("decode_live does not read the calibrated flow at 0x53, the raw one at 0x51")
        break
    parted += live["flow"] != live["flow_raw"]
if parted < 10:
    bad.append("only %d live reports with the two flows apart" % parted)
check("live flow: calibrated at 0x53, raw at 0x51, as capture 10 shows", bad)

# ------------------------ the calibration table applied, from capture 10
# calibrated_flow() must turn the raw flow into the device's calibrated one,
# under the settings written last, in every live report of capture 10.
bad, settings, compared, parted = [], None, 0, 0
for kind, buf in usb_sequence(CAP):
    if kind == "write":
        settings = buf
        continue
    if settings is None:
        continue
    live = highflow.decode_live(buf)
    predicted = highflow.calibrated_flow(live["flow_raw"], settings)
    compared += 1
    parted += live["flow"] != live["flow_raw"]
    # the device shows the flow to 0.1 l/h
    if predicted is None or abs(predicted - live["flow"]) > 0.1:
        bad.append("raw %.1f l/h: predicted %r, the device shows %.1f"
                   % (live["flow_raw"], predicted, live["flow"]))
if compared < 30 or parted < 10:
    bad.append("only %d live reports compared, %d with a correction in effect" % (compared, parted))
# outside the table nothing is predicted; inside it with no corrections, nothing changes
table = bytearray(capture("01-baseline"))
if highflow.calibrated_flow(206.0, table) != 206.0:
    bad.append("with no corrections the flow changed")
for i in range(highflow.CALIBRATION_COUNT):
    struct.pack_into(">h", table, highflow.CALIBRATION + 2 * i, 100)
if highflow.calibrated_flow(400.0, table) is not None:
    bad.append("a flow above the last rate was given a calibrated value")
if abs(highflow.calibrated_flow(100.0, table) - 101.0) > 1e-9:
    bad.append("+1 %% everywhere does not give +1 %%")
check("calibrated flow: %d live reports of capture 10 predicted to 0.1 l/h" % compared, bad)

# the fallback through the kernel driver, which only has the raw flow
bad = []
with tempfile.TemporaryDirectory() as tmp:
    for leaf, text in (("fan1_input", "2060"), ("temp1_input", "26160")):
        with open(os.path.join(tmp, leaf), "w") as fh:
            fh.write(text + "\n")
    real = discovery.hwmon_dir
    discovery.hwmon_dir = lambda serial: tmp
    try:
        plain = highflow.read_live_hwmon(SERIAL, capture("01-baseline"))
        corrected = highflow.read_live_hwmon(SERIAL, table)
        bare = highflow.read_live_hwmon(SERIAL)
    finally:
        discovery.hwmon_dir = real
if (plain["flow"], plain["flow_raw"]) != (206.0, 206.0):
    bad.append("no corrections: %r" % ((plain["flow"], plain["flow_raw"]),))
if corrected["flow_raw"] != 206.0 or abs(corrected["flow"] - 208.06) > 1e-9:
    bad.append("+1 %% everywhere: %r" % ((corrected["flow"], corrected["flow_raw"]),))
if bare["flow"] != 206.0:
    bad.append("without settings the driver's flow is not passed through")
check("kernel driver fallback: its flow is the raw one, calibrated from the settings", bad)

# a calibration change moves the flow the flow alarm sees
bad = []
armed = bytearray(capture("01-baseline"))           # the live flow is 206.5 l/h
armed[highflow.ALARM_ENABLE] |= 0x01
core.put_be16(armed, highflow.ALARM_BY_KEY["flow"].offset, 2000)   # alarm below 200 l/h
for line, refused, why in (
        ("flow calibration --corrections 0,0,0,0,0,0,0,-5,-5,0", True,
         "-5 % at 200 and 250 l/h puts 206.5 l/h at 196.2"),
        ("flow calibration --corrections 0,0,0,0,0,0,0,1,1,0", False, "it rises"),
        ("flow calibration --points 20,30,50,70,100,125,150,200,205,300", False,
         "moving a rate with no corrections changes nothing"),
        ("flow calibration --corrections 0,0,0,0,0,0,0,-5,-5,0 --force", False, "forced")):
    fake = FakeHighflow()
    fake.blobs[core.CTRL_REPORT_ID] = bytearray(armed)
    out, err, code = do(fake, line)
    if refused and (code is None or fake.written):
        bad.append("%r was not refused (%s)" % (line, why))
    if not refused and (code is not None or not fake.written):
        bad.append("%r was refused (%s): %s" % (line, why, code))
fake = FakeHighflow()
fake.blobs[core.CTRL_REPORT_ID] = bytearray(armed)
out, err, code = do(fake, "flow calibration --corrections 0,0,0,0,0,0,0,-5,-5,0")
if not code or "196" not in code:
    bad.append("the refusal does not give the flow the alarm would see: %r" % code)
check("flow alarm: a calibration change that crosses its limit is refused", bad)


# ------------------------------------------- the settings layout version
# Bytes 1-2 of the settings report declare its layout. Everything aqdctl
# decodes depends on it, so a report of another version is refused before
# anything is read from it or written - except by 'backup', whose raw copy is
# what a new layout gets mapped from.
def other_layout(stem="01-baseline", version=2):
    buf = bytearray(capture(stem))
    core.put_be16(buf, core.STRUCTURE_OFFSET, version)
    return core.reseal(buf)


bad = [stem for stem in STEMS if core.layout_version(capture(stem)) != highflow.STRUCTURE_ID]
if bad:
    bad = ["captures not at layout version %d: %s" % (highflow.STRUCTURE_ID, ", ".join(bad))]


class FakeHid:
    """Stands in for hidapi below core.Device, so its own read() is tested."""
    def __init__(self, blob):
        self.blob = bytes(blob)

    def get_feature_report(self, report_id, size):
        return list(self.blob)


for blob, verify, refused in ((capture("01-baseline"), True, False),
                             (other_layout(), True, True),
                             (other_layout(), False, False)):
    dev = core.Device(highflow, b"/nonexistent", SERIAL)
    dev.dev = FakeHid(blob)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            dev.read(check=verify)
        if refused:
            bad.append("core.Device.read accepted layout version %d" % core.layout_version(blob))
    except SystemExit as exc:
        if not refused:
            bad.append("core.Device.read refused (check=%s): %s" % (verify, exc))
        elif "layout version 2; aqdctl knows version 1" not in flat(exc):
            bad.append("the refusal does not name both versions: %s" % exc)
for line in ("info", "display brightness high", "flow calibration"):
    fake = FakeHighflow()
    fake.blobs[core.CTRL_REPORT_ID] = other_layout()
    out, err, code = do(fake, line)
    if code is None or fake.written:
        bad.append("%r ran on another layout" % line)
with tempfile.TemporaryDirectory() as tmp:
    fake = FakeHighflow()
    fake.blobs[core.CTRL_REPORT_ID] = other_layout()
    out, err, code = do(fake, "backup -o %s" % os.path.join(tmp, "new-layout.bin"))
    saved = os.path.join(tmp, "new-layout.bin")
    if code is not None or not os.path.exists(saved) or open(saved, "rb").read() != bytes(other_layout()):
        bad.append("backup did not save a report of another layout: %s" % code)
    theirs = os.path.join(tmp, "highflow-%s-03-other.bin" % SERIAL)
    with open(theirs, "wb") as fh:
        fh.write(bytes(other_layout()))
    fake = FakeHighflow()
    out, err, code = do(fake, "restore %s --force" % theirs)
    if code is None or "layout version 2" not in flat(code) or fake.written:
        bad.append("a backup of another layout was restored: %r" % code)
check("layout version: every capture is at 1; other versions are refused, backup excepted", bad)


# ------------------------------------------- twelve RGB parameter words
# +22..+45: word 11 is the last before the palette. None is used by any
# captured controller beyond word 8, so these are written here.
bad = []
fake = FakeHighflow()
base = highflow.RGB.slot_base(3)                    # the baseline's colour gradient
before = capture("01-baseline")
out, err, code = do(fake, "rgb set controller 3 --param 11=5")
after = fake.blobs[core.CTRL_REPORT_ID]
if code is not None or rgbpx.get_param(after, base, 11) != 5:
    bad.append("--param 11=5 did not write word 11: %s" % code)
elif changed_bytes(before, after) != {base + 22 + 2 * 11 + 1}:
    bad.append("--param 11 touched %s" % sorted(hex(i) for i in changed_bytes(before, after)))
elif base + 22 + 2 * 11 + 2 != base + rgbpx.RGB_COLOUR:
    bad.append("word 11 does not end where the palette starts")
text = io.StringIO()
with contextlib.redirect_stdout(text):
    rgbpx.describe_rgb(after, highflow.RGB, 3, "", highflow.source_label)
if "unnamed11=5" not in text.getvalue():
    bad.append("info does not show word 11:\n" + text.getvalue())
out, err, code = do(FakeHighflow(), "rgb set controller 3 --param 12=5")
if not code or "0-11" not in code:
    bad.append("word 12, which would overwrite the palette, was accepted: %r" % code)
check("RGB: all twelve parameter words are read and written, and no more", bad)


# ---------------------------------------------- exit statuses and -q, -y
# Scripts branch on the status, so each class of outcome has its own, and the
# confirmation can neither block nor crash a run without a terminal.
bad = []
for line, status, why in (
        ("display page-change 99", core.EXIT_INVALID, "a value out of range"),
        ("system usb-current 600", core.EXIT_REFUSED, "a safety check"),
        ("signal-output power-switch", core.EXIT_REFUSED, "a power mode"),
        ("alarm external on", core.EXIT_REFUSED, "an alarm that would fire"),
        ("display brightness high", core.EXIT_NOT_CONFIRMED, "answered no")):
    fake = FakeHighflow()
    out, err, code = do(fake, line, answer="n")
    if code is None or code.status != status or fake.written:
        bad.append("%r (%s): status %r, expected %d" % (line, why, code and code.status, status))
fake = FakeHighflow()
fake.blobs[core.CTRL_REPORT_ID] = other_layout()
out, err, code = do(fake, "info")
if code is None or code.status != core.EXIT_DEVICE:
    bad.append("an unknown layout: status %r, expected %d" % (code and code.status, core.EXIT_DEVICE))


class Stubborn(FakeHighflow):
    """Accepts writes but keeps reading back what it had: a firmware that
    rejected the change."""
    def write(self, buf):
        self.written.append(bytes(buf))


out, err, code = do(Stubborn(), "display brightness high -y")
if code is None or code.status != core.EXIT_DIFFERS or "0x00f: 00 -> 02" not in code:
    bad.append("a write the device did not keep: %r" % code)


class Terminal(io.StringIO):
    def isatty(self):
        return True


real_stdin = sys.stdin
try:
    for stdin, answer, status in ((io.StringIO("y\n"), None, core.EXIT_NOT_CONFIRMED),
                                  (Terminal(""), False, None),        # end of input
                                  (Terminal("yes\n"), True, None)):
        sys.stdin = stdin
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                got = core.confirm("Proceed?")
            if status is not None or got is not answer:
                bad.append("confirm() on %r gave %r" % (stdin.getvalue(), got))
        except core.Failure as exc:
            if exc.code != status or "-y" not in exc.message:
                bad.append("confirm() without a terminal: %r %r" % (exc.code, exc.message))
    sys.stdin = io.StringIO("")
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            highflow.cmd_volume_reset(FakeHighflow(), argparse.Namespace(dry_run=False, yes=True))
        bad.append("volume reset ran without a terminal")
    except core.Failure as exc:
        if exc.code != core.EXIT_NOT_CONFIRMED or "-y" in exc.message:
            bad.append("volume reset without a terminal: %r %r" % (exc.code, exc.message))
finally:
    sys.stdin = real_stdin

# through cli.main: usage, an unknown serial, --quiet
discovery.attached = lambda: both
core.Device = fake_device
try:
    for argv, status in ((["display", "brightness", "high"], core.EXIT_USAGE),
                         (["--device"], core.EXIT_USAGE),
                         (["info", "--device", HFN], core.EXIT_USAGE),
                         (["--device", "99999-99999", "info"], core.EXIT_DEVICE),
                         (["--device", HFN, "system", "usb-current", "600", "-y", "-q"],
                          core.EXIT_REFUSED),
                         (["--device", HFN, "display", "brightness", "high", "-y", "-q"], 0)):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.main(argv)
            got = 0
        except SystemExit as exc:
            got = exc.code
        if got != status:
            bad.append("%s: status %r, expected %d" % (" ".join(argv), got, status))
        if "-q" in argv and out.getvalue():
            bad.append("%s printed on stdout: %r" % (" ".join(argv), out.getvalue()))
        if "-q" in argv and status and "Nothing written" not in err.getvalue():
            bad.append("%s: the refusal is missing from stderr" % " ".join(argv))
finally:
    discovery.attached, core.Device = real_attached, real_device
if "6  not confirmed" not in cli.general_help():
    bad.append("the general help does not list the exit statuses")
check("exit statuses: each outcome its own, -q silent, no terminal never blocks", bad)

# ------------------------------------------------- list and aliases
# 'aqdctl list' is the device list for scripts; an alias stands in for the
# serial in --device, and is kept in the config next to the protected channels.
def run_cli(argv):
    """(status, stdout, stderr) of one command line."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main(argv)
        status = 0
    except SystemExit as exc:
        status = exc.code
    return status, out.getvalue(), err.getvalue()


bad = []
real_config = core.config_path
tmp = tempfile.mkdtemp()
core.config_path = lambda: os.path.join(tmp, "config.json")
def own_serial(kind, path, serial):
    """A fake that answers to the serial it was opened with."""
    dev = fake_device(kind, path, serial)
    dev.serial = serial
    return dev


discovery.attached = lambda: both
core.Device = own_serial
try:
    status, out, err = run_cli(["list"])
    if (status, out) != (0, "%s octo -\n%s highflow -\n" % (OCTO, HFN)):
        bad.append("list without aliases: %r %r" % (status, out))
    core.set_protected(HFN, {1})
    status, out, err = run_cli(["--device", HFN, "alias", "loop-a"])
    if status or "loop-a" not in out:
        bad.append("setting an alias: %r %r" % (status, err))
    if core.protected_channels(HFN) != {1}:
        bad.append("setting an alias lost the protected channels")
    status, out, err = run_cli(["list"])
    if out != "%s octo -\n%s highflow loop-a\n" % (OCTO, HFN):
        bad.append("list with an alias: %r" % out)
    status, out, err = run_cli(["list", "--json"])
    import json
    if json.loads(out) != [{"serial": OCTO, "type": "octo", "alias": None},
                           {"serial": HFN, "type": "highflow", "alias": "loop-a"}]:
        bad.append("list --json: %r" % out)
    for argv, want in (([HFN], "loop-a\n"), ([OCTO], "-\n")):
        status, out, err = run_cli(["--device"] + argv + ["alias"])
        if (status, out) != (0, want):
            bad.append("alias of %s: %r %r" % (argv[0], status, out))
    del opened[:]
    status, out, err = run_cli(["--device", "loop-a", "info", "alarm"])
    if status or opened != [("highflow", HFN)]:
        bad.append("--device loop-a: status %r, opened %r" % (status, opened))
    before = open(core.config_path()).read()
    for name, why in (("Loop", "upper case"), ("1loop", "a digit first"),
                      ("12345-67890", "a serial"), ("a b", "a space"), ("", "empty"),
                      ("a" * 33, "33 characters"), ("loop-a", "taken by the other device")):
        status, out, err = run_cli(["--device", OCTO, "alias", name])
        if status != core.EXIT_INVALID:
            bad.append("alias %r (%s): status %r" % (name, why, status))
    if open(core.config_path()).read() != before:
        bad.append("a refused alias changed the config")
    status, out, err = run_cli(["--device", HFN, "alias", "loop-a"])
    if status:
        bad.append("setting a device's own alias again was refused: %r" % err)
    for argv, status_wanted, text in (
            (["--device", "loop-b", "info"], core.EXIT_DEVICE, "neither"),
            (["list", "--keys"], core.EXIT_USAGE, "usage")):
        status, out, err = run_cli(argv)
        if status != status_wanted or text not in err:
            bad.append("%s: %r %r" % (" ".join(argv), status, err))
    core.set_alias("11111-22222", "spare")                 # a device not attached
    status, out, err = run_cli(["--device", "spare", "info"])
    if status != core.EXIT_DEVICE or "11111-22222" not in err:
        bad.append("the alias of a detached device: %r %r" % (status, err))
    status, out, err = run_cli(["--device", HFN, "alias", "-"])
    status, out, err = run_cli(["list"])
    if out != "%s octo -\n%s highflow -\n" % (OCTO, HFN) or core.protected_channels(HFN) != {1}:
        bad.append("after clearing: %r, protected %r" % (out, core.protected_channels(HFN)))
    if run_cli(["--device", "loop-a", "info"])[0] != core.EXIT_DEVICE:
        bad.append("a cleared alias still selects the device")
finally:
    discovery.attached, core.Device = real_attached, real_device
    core.config_path = real_config
    shutil.rmtree(tmp)
check("list and aliases: one line per device, aliases unique, never serial-shaped", bad)

# ------------------------------------------------------------ keys and get
# The key names are the contract scripts build on, so the list is pinned; every
# capture reads under every key; only the reports a request needs are read.
import json
from aqdctl import keys

TABLE = highflow.key_table()


def all_keys(stem):
    ctx = keys.Context(highflow, SERIAL, FakeHighflow(stem))
    return keys.read(TABLE, ctx)


def leaf_clashes(names):
    """Names that are a whole-part prefix of another name."""
    ordered = sorted(names)
    return [a for a, b in zip(ordered, ordered[1:]) if b.startswith(a + ".")]


bad = []
for stem in STEMS:
    try:
        rows = all_keys(stem)
        for k, v in rows:
            k.text(v)
        json.dumps({k.name: k.json(v) for k, v in rows})
        clash = leaf_clashes([k.name for k, _v in rows])
        if clash:
            bad.append("%s: %s is the prefix of another key" % (stem, clash[0]))
    except Exception as exc:
        bad.append("%s: %s: %s" % (stem, type(exc).__name__, exc))
pinned = [line.strip() for line in open(os.path.join(HERE, "keys-highflow.txt"))
          if line.strip() and not line.startswith("#")]
listed = [k.name for k in keys.schema(TABLE)]
if listed != pinned:
    gone, new = sorted(set(pinned) - set(listed)), sorted(set(listed) - set(pinned))
    bad.append("the key list differs from keys-highflow.txt: gone %s, new %s, or reordered"
               % (gone, new))
if leaf_clashes(listed):
    bad.append("%s is the prefix of another key" % leaf_clashes(listed)[0])
check("keys: all %d captures read under every key, the list is the pinned one" % len(STEMS),
      bad)

# values, against what the captures are known to hold
bad = []
values = {k.name: k.text(v) for k, v in all_keys(BY_NUMBER[8])}
live = highflow.decode_live(LIVE[0])
for key, want in (("device.serial", SERIAL), ("device.type", "highflow"),
                  ("device.firmware", "1017"),
                  ("flow.calibration.corrections", "-1,1,1,0,0,0,0,0,0,0"),
                  ("flow.calibration.points", "20,30,50,70,100,125,150,200,250,300"),
                  ("display.page-change", "5"), ("alarm.flow.limit", "45.0"),
                  ("live.flow", "%.1f" % live["flow"]), ("live.external", ""),
                  ("live.volume", "%d" % live["volume"]),
                  ("name.sensor.internal", names.decode(capture(BY_NUMBER[8], 8),
                                                        highflow.NAME_GROUPS)["sensor"][0])):
    if values.get(key) != want:
        bad.append("%s=%r, expected %r" % (key, values.get(key), want))
# colours: as many as the effect keeps, here a gradient's limits + 1; the
# parameters that follow from them, or that cannot move, are read-only
gradients = 0
for stem in STEMS:
    rows = dict((k.name, (k, v)) for k, v in all_keys(stem))
    for name, (k, limits) in rows.items():
        if name.endswith(".param.limits"):
            gradients += 1
            p = name[:-len("limits")]
            colours = rows[p[:-len("param.")] + "colours"][1]
            if len(colours) != limits + 1:
                bad.append("%s %s: %d colours for %d limits"
                           % (stem, name, len(colours), limits))
            access = [(n, rows[p + n][0].settable) for n in ("start", "end", "limits", "limit1")]
            if access != [("start", False), ("end", False), ("limits", False), ("limit1", True)]:
                bad.append("%s: settable %r" % (stem, access))
if not gradients:
    bad.append("no capture holds a gradient")
# every capture: the keys say what the decoder says
for stem in STEMS:
    d = highflow.decode(capture(stem))
    rows = dict((k.name, k.text(v)) for k, v in all_keys(stem))
    want = {"display.units.flow": highflow.FLOW_UNITS.get(d["flow_unit"]),
            "flow.coolant": highflow.COOLANT_CHOICES[d["coolant"]],
            "sensor.offset.external": "%.2f" % (d["offset_external"] / 100.0),
            "sensor.water-quality.bad": "%.1f" % (d["water_quality_bad"] / 10.0),
            "system.usb-current": "%d" % d["usb_current"],
            "alarm.startup-delay": "%d" % d["alarm_delay"]}
    for n in range(1, highflow.CHART_COUNT + 1):
        want["display.chart.%d.source" % n] = \
            highflow.CHART_SOURCES[d["chart%d_source" % n]].replace(" ", "-")
        want["display.chart.%d.interval" % n] = \
            "%g" % highflow.CHART_INTERVALS[d["chart%d_interval" % n]]
    for a in highflow.ALARMS:
        want["alarm.%s.state" % a.key] = "on" if d["alarm_" + a.key] else "off"
        want["alarm.%s.limit" % a.key] = a.fmt % (d["limit_" + a.key] / a.scale)
    for key, value in sorted(want.items()):
        if rows.get(key) != value:
            bad.append("%s: %s=%r, the decoder says %r" % (stem, key, rows.get(key), value))
# a parameter word the effect does not name is listed, by number, where it holds something
fake = FakeHighflow()
settings = fake.blobs[core.CTRL_REPORT_ID]
slot = highflow.RGB.slot_base(1)
settings[slot + rgbpx.RGB_MODE] = 0x01                     # static: no named words
rgbpx.set_param(settings, slot, 3, 42)
core.reseal(settings)
rows = dict((k.name, k) for k in keys.select(TABLE, keys.Context(highflow, SERIAL, fake),
                                             ["rgb.controller.1.param"]))
if list(rows) != ["rgb.controller.1.param.3"] or not rows["rgb.controller.1.param.3"].settable:
    bad.append("unnamed words: %r" % list(rows))
check("keys: values as the captures hold them, written as the commands take them", bad)


class Counting(FakeHighflow):
    """Records every report read."""
    def __init__(self, *a, **kw):
        FakeHighflow.__init__(self, *a, **kw)
        self.reads = []

    def read(self, report_id=core.CTRL_REPORT_ID, check=True):
        self.reads.append(report_id)
        return FakeHighflow.read(self, report_id, check)

    def read_input(self, size, timeout_ms=0):
        self.reads.append(1)
        return FakeHighflow.read_input(self, size, timeout_ms)


bad = []
for prefixes, want in ((["device.serial"], []), (["display"], [3]), (["name"], [8]),
                       (["live"], [1]), (["live.flow", "device.firmware"], [1]),
                       (["alarm", "display", "rgb"], [3]), ([], [3, 1, 8])):
    fake = Counting()
    with keys.Context(highflow, SERIAL, fake, opened=False) as ctx:
        keys.read(TABLE, ctx, prefixes)
    if sorted(fake.reads) != sorted(want):
        bad.append("get %s read %r, expected %r" % (" ".join(prefixes), fake.reads, want))
check("keys: a request reads only the reports its keys need, each once", bad)

# 'get' through the command line
bad = []
discovery.attached = lambda: both
core.Device = own_serial
try:
    status, out, err = run_cli(["--device", HFN, "get", "alarm.flow"])
    if (status, out) != (0, "alarm.flow.state=off\nalarm.flow.limit=45.0\n"):
        bad.append("get alarm.flow: %r %r" % (status, out))
    status, out, err = run_cli(["--device", HFN, "get", "--value", "live.flow"])
    if (status, out) != (0, "%.1f\n" % live["flow"]):
        bad.append("get --value live.flow: %r %r" % (status, out))
    status, out, err = run_cli(["--device", HFN, "get", "--json", "display.pages",
                                "live.external"])
    if json.loads(out) != {"display.pages": [2, 3], "live.external": None}:
        bad.append("get --json: %r" % out)
    status, out, err = run_cli(["--device", HFN, "get", "--json", "--value", "alarm.flow"])
    if json.loads(out) != [False, 45]:
        bad.append("get --json --value: %r" % out)
    status, out, err = run_cli(["--device", HFN, "get", "--keys", "rgb.controller.3.param"])
    if not out.startswith("rgb.controller.N.param.NAME  integer") or out.count("\n") != 1:
        bad.append("get --keys rgb.controller.3.param: %r" % out)
    status, out, err = run_cli(["--device", HFN, "get", "--keys", "--json",
                                "display.brightness"])
    if json.loads(out) != [{"key": "display.brightness", "type": "choice", "settable": True,
                            "choices": ["high", "medium", "low"]}]:
        bad.append("get --keys --json: %r" % out)
    for argv in (["get", "nosuch"], ["get", "live.flo"], ["get", "rgb.controller.5"],
                 ["get", "--keys", "nosuch"]):
        status, out, err = run_cli(["--device", HFN] + argv)
        if status != core.EXIT_INVALID or out or "No key" not in err:
            bad.append("%s: %r %r %r" % (" ".join(argv), status, out, err))
finally:
    discovery.attached, core.Device = real_attached, real_device
check("get: key=value lines, --value, --json, --keys; a prefix matching nothing exits 1",
      bad)

# rgb.brightness has two decimals: enough for every byte to come back as itself
bad = []
for byte in range(256):
    fake = FakeHighflow()
    fake.blobs[core.CTRL_REPORT_ID][highflow.RGB_BRIGHTNESS] = byte
    core.reseal(fake.blobs[core.CTRL_REPORT_ID])
    (k, v), = keys.read(TABLE, keys.Context(highflow, SERIAL, fake), ["rgb.brightness"])
    out, err, code = do(fake, "rgb brightness %s -y" % k.text(v))
    if code is not None or fake.written:
        bad.append("byte %d -> %s -> written %r" % (byte, k.text(v), fake.written[:1]))
check("rgb.brightness: all 256 bytes survive the percentage", bad[:3])

# ------------------------------------------------------------------ batch
# A batch runs each key=value line as the command that sets it, against copies
# of the reports, judges the changes together, and writes each report once.
from aqdctl import batch


def settable(fake):
    ctx = keys.Context(highflow, SERIAL, fake)
    return {k.name: k.text(k.read(ctx)) for k in keys.select(TABLE, ctx) if k.settable}


def run_batch(fake, text, *flags, stdin=False):
    """(status, stdout, stderr) of 'batch' on `fake`, from a file or stdin."""
    path = os.path.join(BACKUPS, "batch.txt")
    with open(path, "w") as fh:
        fh.write(text)
    fake.serial = HFN
    discovery.attached = lambda: both
    core.Device = lambda kind, p, serial: fake
    real_stdin = sys.stdin
    if stdin:
        sys.stdin = io.StringIO(text)
    try:
        return run_cli(["--device", HFN, "batch", "-" if stdin else path] + list(flags))
    finally:
        discovery.attached, core.Device = real_attached, real_device
        sys.stdin = real_stdin


def as_lines(values):
    return "".join("%s=%s\n" % kv for kv in values.items())


# what get prints, given back, changes nothing - read-only keys included, which
# a batch skips; given to another capture, it makes every key equal the source's
bad = []
fake = FakeHighflow()
everything = "".join("%s=%s\n" % (k.name, k.text(v)) for k, v in
                     keys.read(TABLE, keys.Context(highflow, SERIAL, fake)))
everything = everything.replace("device.serial=%s" % SERIAL, "device.serial=09876-54321")
status, out, err = run_batch(fake, everything, "-y")
if (status, out, fake.written) != (0, "Nothing to change.\n", []):
    bad.append("all of get's output: %r %r %r" % (status, out, err))
for i, stem in enumerate(STEMS):
    fake = FakeHighflow(stem)
    status, out, err = run_batch(fake, as_lines(settable(fake)), "-y")
    if (status, out, fake.written) != (0, "Nothing to change.\n", []):
        bad.append("%s onto itself: %r %r %r" % (stem, status, out, err[-200:]))
    source = settable(FakeHighflow(stem))
    target = FakeHighflow(STEMS[(i * 7 + 3) % len(STEMS)])
    status, out, err = run_batch(target, as_lines(source), "-y", "--force")
    got = settable(target)
    wrong = [k for k in source if got[k] != source[k]]
    if status or wrong or len(target.written) > 1:
        bad.append("%s onto %s: status %r, %d writes, differ: %s %s"
                   % (stem, STEMS[(i * 7 + 3) % len(STEMS)], status, len(target.written),
                      wrong[:3], err[-200:]))
check("batch: get's output changes nothing on its own capture, and copies onto another",
      bad[:4])

# one write for many lines; settings and names are two
bad = []
fake = FakeHighflow()
status, out, err = run_batch(fake, """
# a comment, and blank lines

display.brightness=high
alarm.flow.limit=60
system.standby.without-aquabus=on
flow.calibration.corrections=0,0,0,0,0,0,0,1,1,0
""", "-y")
if status or len(fake.written) != 1:
    bad.append("four settings: status %r, %d writes: %s" % (status, len(fake.written), err))
for line in ("display.brightness: low -> high", "alarm.flow.limit: 45.0 -> 60.0",
             "flow.calibration.corrections: 0,0,0,0,0,0,0,0,0,0 -> 0,0,0,0,0,0,0,1,1,0"):
    if line not in out:
        bad.append("missing %r in:\n%s" % (line, out))
fake = FakeHighflow()
status, out, err = run_batch(fake, "display.rotate=on\nname.sensor.internal=Loop In\n", "-y")
if status or sorted(buf[0] for buf in fake.written) != [3, 8]:
    bad.append("settings and names: %r, wrote %r" % (status, [b[0] for b in fake.written]))
check("batch: many lines make one write per report", bad)

# all or nothing, and the line that failed is named
bad = []
for text, status_wanted, words in (
        ("display.brightness=high\nalarm.flow.limit=60\ndisplay.brightness=low\n",
         core.EXIT_INVALID, "line 3: display.brightness is already set on line 1"),
        ("display.brightness=high\ndisplay.idle-brightness=dim\n", core.EXIT_USAGE,
         "line 2 (display.idle-brightness=dim): argument value: invalid choice"),
        ("alarm.flow.limit=2000\n", core.EXIT_INVALID, "line 1 (alarm.flow.limit=2000): "
         "The flow alarm limit is 0-1000"),
        ("display.rotat=on\n", core.EXIT_INVALID, "is not a key of this high flow NEXT"),
        ("rgb.controller.5.pos=1-3\n", core.EXIT_INVALID, "that controller is not in use"),
        ("display.rotate on\n", core.EXIT_USAGE, "invalid choice: 'display.rotate'"),
        ("display.rotate=maybe\n", core.EXIT_INVALID, "on or off"),
        ("system.usb-current=1000\n", core.EXIT_REFUSED, "requires --force")):
    fake = FakeHighflow()
    status, out, err = run_batch(fake, text, "-y")
    if status != status_wanted or fake.written or words not in err:
        bad.append("%r: status %r, %d writes: %s" % (text, status, len(fake.written), err))
check("batch: a bad line writes nothing and is named", bad)

# command lines: run in order, with the key=value lines around them
bad = []
fake = FakeHighflow()
status, out, err = run_batch(fake, 'display brightness high\nalarm.flow.limit=60\n'
                             'name sensor internal "Loop In"\n', "-y")
if status or sorted(b[0] for b in fake.written) != [3, 8]:
    bad.append("mixed: %r %s" % (status, err))
for line in ("display.brightness: low -> high", "alarm.flow.limit: 45.0 -> 60.0",
             "name.sensor.internal: "):
    if line not in out:
        bad.append("missing %r in:\n%s" % (line, out))
for text, words in (("display brightness high -y\n", "-y belongs to the batch"),
                    ("alarm flow --limit 60 --backup=x.bin\n", "--backup belongs"),
                    ("volume reset\n", "volume reset always asks"),
                    ("info\n", "this command changes nothing"),
                    ("get display\n", "this command changes nothing"),
                    ("display brightnes high\n", "invalid choice: 'brightnes'"),
                    ("name sensor internal \"open\n", "No closing quotation")):
    fake = FakeHighflow()
    status, out, err = run_batch(fake, "display.rotate=on\n" + text, "-y")
    if status != core.EXIT_USAGE or fake.written or words not in err or "line 2" not in err:
        bad.append("%r: %r %s" % (text, status, err))
check("batch: command lines run in order with the key=value lines; batch-only flags and "
      "commands that change nothing are refused", bad)

# changes judged together: each line passes alone, the two together make the
# flow alarm fire (206.5 l/h, 5 % less at 200-250 l/h, limit 200)
bad = []
together = ("alarm.flow.state=on\nalarm.flow.limit=200\n"
            "flow.calibration.corrections=0,0,0,0,0,0,0,-5,-5,0\n")
for text in ("alarm.flow.state=on\nalarm.flow.limit=200\n",
             "flow.calibration.corrections=0,0,0,0,0,0,0,-5,-5,0\n"):
    fake = FakeHighflow()
    status, out, err = run_batch(fake, text, "-y")
    if status or len(fake.written) != 1:
        bad.append("alone, %r: %r %s" % (text, status, err))
fake = FakeHighflow()
status, out, err = run_batch(fake, together, "-y")
if status != core.EXIT_REFUSED or fake.written or "196" not in err:
    bad.append("together: %r %d writes %s" % (status, len(fake.written), err))
fake = FakeHighflow()
status, out, err = run_batch(fake, together, "-y", "--force")
if status or len(fake.written) != 1:
    bad.append("together with --force: %r %s" % (status, err))
# and the other way round: turning the alarm on at 210 l/h alone would fire it,
# the calibration that comes with it lifts the flow to 216.8
fake = FakeHighflow()
status, out, err = run_batch(fake, "alarm.flow.state=on\nalarm.flow.limit=210\n"
                             "flow.calibration.corrections=0,0,0,0,0,0,0,5,5,0\n", "-y")
if status or len(fake.written) != 1:
    bad.append("refused alone, fine together: %r %s" % (status, err))
check("batch: the safety checks judge all changes together", bad)

# dry run, stdin, JSON
bad = []
fake = FakeHighflow()
status, out, err = run_batch(fake, "display.brightness=high\n", "-n")
if (status, out, fake.written) != (0, "display.brightness: low -> high\n"
                                      "byte changes (excluding checksum): 1\n"
                                      "  0x00f: 02 -> 00\n  checksum resealed\n"
                                      "--dry-run: nothing written.\n", []):
    bad.append("-n: %r %r" % (status, out))
fake = FakeHighflow()
status, out, err = run_batch(fake, "display.brightness=high\n")
if status != core.EXIT_NOT_CONFIRMED or fake.written:
    bad.append("no -y and no terminal: %r %r" % (status, err))
fake = FakeHighflow()
status, out, err = run_batch(fake, "display.brightness=high\n", stdin=True)
if status != core.EXIT_NOT_CONFIRMED or fake.written or "-y" not in err:
    bad.append("stdin without -y: %r %r" % (status, err))
status, out, err = run_batch(fake, "display.brightness=high\n", "-y", stdin=True)
if status or len(fake.written) != 1:
    bad.append("stdin with -y: %r %r" % (status, err))
fake = FakeHighflow()
status, out, err = run_batch(fake, "display.brightness=high\nalarm.flow.limit=60\n",
                             "-y", "--json")
if json.loads(out) != {"changes": [{"key": "display.brightness", "old": "low", "new": "high"},
                                   {"key": "alarm.flow.limit", "old": 45, "new": 60}],
                       "written": True}:
    bad.append("--json: %r" % out)
fake = FakeHighflow()
status, out, err = run_batch(fake, "display.brightness=dim\n", "-y", "--json")
if status != core.EXIT_USAGE or json.loads(out)["error"]["status"] != core.EXIT_USAGE:
    bad.append("--json failure: %r %r" % (status, out))
check("batch: --dry-run, confirmation, stdin needs -y, --json for changes and failures", bad)

# one aqdctl at a time changes a device
bad = []
with core.device_lock(HFN):
    try:
        with core.device_lock(HFN, wait=0.2):
            bad.append("a second lock was granted")
    except core.Failure as exc:
        if exc.code != core.EXIT_DEVICE:
            bad.append("status %r" % exc.code)
with core.device_lock(HFN, wait=0.2):
    pass                                    # released again
check("device lock: a second writer waits, then gives up with status 3", bad)

# ------------------------------------------------------------- backups
# two backups of one report within a second are two files
bad = []
first = core.backup(capture("01-baseline"), FakeHighflow())
second = core.backup(capture(BY_NUMBER[8]), FakeHighflow())
if first == second or open(first, "rb").read() != bytes(capture("01-baseline")):
    bad.append("%s, %s" % (first, second))
check("backups: a second backup in the same second does not overwrite the first", bad)

# Every write above backed up into BACKUPS, never the real data directory.
core.data_dir = real_data_dir
shutil.rmtree(BACKUPS)

print("\n%d/%d passed" % (checks - len(failures), checks))
sys.exit(1 if failures else 0)
