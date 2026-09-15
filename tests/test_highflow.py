#!/usr/bin/env python3
"""Offline test of the high flow NEXT support, against the captures in
highflow/. No hardware needed.

The captures are the ground truth: each one changed a single setting in
Aquasuite (the file name says which) and undid the one before. So each
capture's decoded settings must show the named change, and must differ from
the capture before it only in that change and the undo.
"""
import argparse, contextlib, glob, io, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from aqdctl import cli, core, discovery, highflow, names, octo

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
    """Serves captured reports and one live report; records writes."""
    kind, serial = highflow, SERIAL

    def __init__(self, stem="01-baseline", live=LIVE[0]):
        self.blobs = {core.CTRL_REPORT_ID: capture(stem),
                      core.LABEL_REPORT_ID: capture(stem, core.LABEL_REPORT_ID)}
        self.live, self.written = live, []

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def read(self, report_id=core.CTRL_REPORT_ID): return bytearray(self.blobs[report_id])
    def read_input(self, size, timeout_ms=0): return self.live
    def write(self, buf): self.written.append(bytes(buf))


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
                         (["--device", HFN, "restore", "x.bin"],
                          "no writes in this version")):
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

print("\n%d/%d passed" % (checks - len(failures), checks))
sys.exit(1 if failures else 0)
