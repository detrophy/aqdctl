"""The Aquacomputer Octo: eight fan channels, four temperature sensors, one
flow input, two RGBpx channels.

Everything specific to the Octo lives here: its report layout, derived by
reverse engineering and backed by the captures in octo/, and the commands only
it offers (fan, sensor). The rgb and name commands are the shared ones from
rgbpx and names, pointed at the Octo's layout. Offsets are firmware dependent
- if a checksum check fails, stop."""

import argparse
import struct
import sys

from . import core, names, rgbpx
from .core import (be16, sbe16, put_be16, reseal, pct, to_raw_pct, require_pct,
                   require_window, commit)

NAME = "octo"
TITLE = "Octo"
PRODUCT_ID = 0xF011
# Report ids and sizes come from the device's HID report descriptor.
CTRL_REPORT_SIZE = 0x65F          # 1631
LABEL_REPORT_SIZE = 1013
REPORT_SIZES = {core.CTRL_REPORT_ID: CTRL_REPORT_SIZE,
                core.LABEL_REPORT_ID: LABEL_REPORT_SIZE}
HAS_FANS = True
FAN_CHANNELS = 8

# --- report 0x08: 42 name slots of 24 bytes, NUL-padded single-byte text.
# 3 + 42*24 == 1011 == the checksum offset, so the table has no slack.
NAME_GROUPS = [
    names.NameGroup("fan", 0x003, 8, "8"),
    names.NameGroup("controller", 0x0C3, 12, "12"),
    names.NameGroup("sensor", 0x1E3, 4, "4"),
    names.NameGroup("flow", 0x243, 2, "2"),
    names.NameGroup("virtual", 0x273, 16, "16, the device's software sensors"),
]

# --- RGBpx: 12 controllers of 70 bytes, format in rgbpx. Slots 1-6 belong to
# channel 1 and 7-12 to channel 2: the manual allows "bis zu 6 LED-Controller"
# per output, and every one of the 40 captures fits that split, unused slots
# included (they keep their channel's port byte).
RGB_BASE, RGB_COUNT = 0x307, 12
# Global brightness for every controller, u8 over 0..255.
RGB_BRIGHTNESS = 0x304
# Master switch, just before the slot array; 0x00 means on (see rgbpx).
RGB_ENABLE = 0x306
# LEDs addressable on one RGBpx header, per the manual. Both headers count from
# their own LED 1.
RGB_MAX_LED = 90
RGB = rgbpx.RgbLayout(base=RGB_BASE, brightness=RGB_BRIGHTNESS, enable=RGB_ENABLE,
                      channels={1: (1, 6, RGB_MAX_LED), 2: (7, 12, RGB_MAX_LED)},
                      verified=set(rgbpx.RGB_MODES))
RGB_SOURCE_HELP = "1-4 (temperature sensors), 'flow', '#N' for a raw index, or 'none'"

# Offset of each channel's *speed* field, from aquacomputer_d5next.c.
# The channel's record starts one byte earlier, at the mode byte.
FAN_CTRL = [0x5B, 0xB0, 0x105, 0x15A, 0x1AF, 0x204, 0x259, 0x2AE]

OFF_MODE = -1        # u8   0=manual  1=target-temperature  3=linked to another channel
OFF_SETPOINT = 0x00  # BE16 manual speed, percent x100 (authoritative in mode 0 only)
OFF_SOURCE = 0x02    # BE16 controller source, 0-based sensor index (0 = Sensor 1)
OFF_TARGET = 0x04    # BE16 target temperature, degC x100 (target-temp mode)

CTRL_TABLE = 0x12    # 8 records x 9 bytes, record n == channel n+1
CTRL_STRIDE = 9
# +0 is a bitfield, NOT a sensor index (the source lives at block +0x02).
# Both bits confirmed by single-toggle captures: turning start boost on set
# 0x02, turning "hold minimum power" off cleared 0x01.
REC_FLAGS = 0
REC_FLAG_HOLD_MIN = 0x01   # keep fans running below the target instead of stopping
REC_FLAG_BOOST = 0x02      # start boost
REC_MIN = 1          # BE16 minimum power, percent x100
REC_MAX = 3          # BE16 maximum power, percent x100
REC_FALLBACK = 5     # BE16 power used when the source sensor reads nothing
REC_MAX_RPM = 7      # BE16 "maximum fan speed for bar graph and chart", rpm

TEMP_OFFSETS = 0x0A  # 4 x BE16 signed, degC x100

# Source index space, shared with the fan controllers: temperature sensors
# first, then the flow sensor. Index 4 confirmed by a capture whose chart
# switched to the flow sensor's 0..300 range.
SOURCE_FLOW = 4

# Controller tuning presets. The device stores NO preset identifier - the official
# software simply writes these five numbers, so a preset here is just a named row.
# Captured by setting channels 3-6 to each preset in turn, and cross-checked
# against two channels of an earlier capture that used the same values.
# Order: P, I, D, reset time (seconds), hysteresis (kelvin).
PID_PRESETS = {
    "fastest": (4000, 3500, 1000, 0.5, 0.10),   # Aquasuite "+2"
    "fast":    (2500, 2000,  500, 1.0, 0.10),   # "+1"
    "normal":  (1400, 1200,    0, 4.0, 0.20),   # "0", the factory default
    "slow":    (1000,  800,    0, 8.0, 0.30),   # "-1"
    "slowest": ( 500,  300,    0, 10.0, 0.30),  # "-2"
}
# Aquasuite labels them on a +2..-2 scale; the CLI uses words because a leading
# '+' or '-' would be parsed as a flag.
PID_PRESET_SCALE = {"fastest": "+2", "fast": "+1", "normal": "0",
                    "slow": "-1", "slowest": "-2"}
FLOW_PULSES = 0x06   # BE16 impulses per litre

# The mode byte is not a small enum: 0x00-0x02 are the three controller types,
# and anything from 0x03 up means "follow another channel", with the target
# encoded as mode - LINK_OFFSET. Confirmed by two captures - channel 2 follows
# channel 1 and reads 0x03; pointing channel 6 at channel 3 wrote 0x05.
# Linking also copies the source channel's power window onto the follower.
MODE_MANUAL, MODE_TARGET, MODE_CURVE = 0x00, 0x01, 0x02
LINK_OFFSET = 2
LINK_MIN = LINK_OFFSET + 1          # 0x03 == follow channel 1
# The names here are the CLI verbs, deliberately: reading "manual" and typing
# "fixed" is exactly the sort of mismatch this tool is trying to stop having.
MODE_BY_NAME = {"fixed": MODE_MANUAL, "target": MODE_TARGET, "curve": MODE_CURVE}


def is_linked(value):
    return value >= LINK_MIN


def link_target(value):
    return value - LINK_OFFSET


def mode_name(value):
    if is_linked(value):
        target = link_target(value)
        return "follow ch%d" % target if 1 <= target <= 8 else "follow ?%d" % target
    return {MODE_MANUAL: "fixed", MODE_TARGET: "target",
            MODE_CURVE: "curve"}.get(value, "unknown(%#04x)" % value)

# Curve mode: 16 points, temperature and power arrays, both BE16 hundredths.
# Aquasuite's "automatic" and "manual" setup are the same stored data - the
# automatic dialog just fills these 16 points for you; switching between the
# two writes nothing.
CURVE_TEMPS = 0x14
CURVE_POWERS = 0x34
CURVE_POINTS = 16
OFF_STARTUP = 0x12   # BE16 startup temperature, degC x100

# PID block, confirmed against a "user defined" controller reading
# P 1400 / I 1200 / D 0 / reset time 4 / hysteresis 0.2 K.
OFF_PID_P = 0x06     # BE16
OFF_PID_I = 0x08     # BE16
OFF_PID_D = 0x0A     # BE16
OFF_PID_RESET = 0x0C # BE16, tenths of a unit  (40 -> 4)
OFF_HYSTERESIS = 0x0E  # BE16, hundredths of a kelvin (20 -> 0.2 K)

# u8, 1-based; the report always holds the active profile. Confirmed twice:
# it moved 1 -> 2 in the same write that applied profile 2's settings.
# (One capture set showed a profile selection writing nothing at all, with the
# settings only landing once another change forced a flush. That turned out to
# be a wedged Aquasuite service, not normal behaviour - restarting it restored
# immediate application. Kept as a fixture because the index/content pairing it
# captured is still valid evidence for this byte.)
PROFILE_INDEX = 0x65C


def source_label(index):
    if index == SOURCE_FLOW:
        return "flow"
    if 0 <= index < 4:
        return "sensor %d" % (index + 1)
    return "source %d" % index


def source_short(index):
    """The same thing as source_label, in the form you would type after
    --sensor. Used where a table column has to stay narrow."""
    if index == SOURCE_FLOW:
        return "flow"
    if 0 <= index < 4:
        return "%d" % (index + 1)
    return "#%d" % index


def channel_base(ch):
    return FAN_CTRL[ch - 1]


def record_base(ch):
    return CTRL_TABLE + CTRL_STRIDE * (ch - 1)


def decode(buf):
    out = {"flow_pulses": be16(buf, FLOW_PULSES),
           "temp_offsets": [sbe16(buf, TEMP_OFFSETS + 2 * i) / 100.0 for i in range(4)],
           "channels": []}
    for ch in range(1, 9):
        base, rec = channel_base(ch), record_base(ch)
        mode = buf[base + OFF_MODE]
        out["channels"].append({
            "channel": ch,
            "mode": mode,
            "mode_name": mode_name(mode),
            "setpoint": pct(be16(buf, base + OFF_SETPOINT)),
            "target_temp": be16(buf, base + OFF_TARGET) / 100.0,
            "source": be16(buf, base + OFF_SOURCE),
            "flags": buf[rec + REC_FLAGS],
            "boost": bool(buf[rec + REC_FLAGS] & REC_FLAG_BOOST),
            "hold_min": bool(buf[rec + REC_FLAGS] & REC_FLAG_HOLD_MIN),
            "max_rpm": be16(buf, rec + REC_MAX_RPM),
            "min": pct(be16(buf, rec + REC_MIN)),
            "max": pct(be16(buf, rec + REC_MAX)),
            "fallback": pct(be16(buf, rec + REC_FALLBACK)),
        })
    return out


def pid_preset_name(values):
    """Name of the preset matching these five numbers, or None. The device stores
    no preset id, so this is a lookup by value - see PID_PRESETS."""
    for name, row in PID_PRESETS.items():
        if all(abs(a - b) < 1e-6 for a, b in zip(values, row)):
            return name
    return None


def read_curve(buf, ch):
    base = channel_base(ch)
    pts = []
    for k in range(CURVE_POINTS):
        t = (buf[base + CURVE_TEMPS + 2 * k] << 8) | buf[base + CURVE_TEMPS + 2 * k + 1]
        p = (buf[base + CURVE_POWERS + 2 * k] << 8) | buf[base + CURVE_POWERS + 2 * k + 1]
        pts.append((t / 100.0, p / 100.0))
    return pts


def write_curve(buf, ch, points):
    base = channel_base(ch)
    for k, (t, p) in enumerate(points):
        struct.pack_into(">H", buf, base + CURVE_TEMPS + 2 * k, int(round(t * 100)))
        struct.pack_into(">H", buf, base + CURVE_POWERS + 2 * k, int(round(p * 100)))


def info_fan(buf, names, live):
    st = decode(buf)
    print("FAN CHANNELS  (8 headers)")
    print("  ch  name             mode         setpoint  target  sensor   rpm"
          "   boost  hold    min     max  fallback")
    print("  --  ---------------  -----------  --------  ------  ------  -----"
          "  -----  ----  ------  ------  --------")
    for c in st["channels"]:
        ch = c["channel"]
        uses_source = c["mode"] in (MODE_TARGET, MODE_CURVE)
        if uses_source:
            ctrl = "%6.2f  %6.2f  %8.2f" % (c["min"], c["max"], c["fallback"])
        else:
            ctrl = "     -       -         -"
        target = "%6.2f" % c["target_temp"] if c["mode"] == MODE_TARGET else "     -"
        rpm = live.get("fan", {}).get(ch)
        print("  %2d  %-15s  %-11s  %7.2f%%  %s  %6s  %5s  %5s  %4s  %s"
              % (ch, (names[ch - 1] or "-")[:15], c["mode_name"], c["setpoint"],
                 target, source_short(c["source"]) if uses_source else "-",
                 "-" if rpm is None else "%d" % rpm,
                 "on" if c["boost"] else "off",
                 "on" if c["hold_min"] else "off", ctrl))
        if c["mode"] == MODE_CURVE:
            pts = read_curve(buf, ch)
            print("      curve %.1f-%.1fC -> %.1f-%.1f%%   startup %.2fC"
                  % (pts[0][0], pts[-1][0], pts[0][1], pts[-1][1],
                     be16(buf, channel_base(ch) + OFF_STARTUP) / 100.0))
        if uses_source:
            # Only shown where the channel regulates: a fixed or following
            # channel stores a tuning too, but never uses it.
            base = channel_base(ch)
            tuning = (be16(buf, base + OFF_PID_P), be16(buf, base + OFF_PID_I),
                      be16(buf, base + OFF_PID_D),
                      be16(buf, base + OFF_PID_RESET) / 10.0,
                      be16(buf, base + OFF_HYSTERESIS) / 100.0)
            preset = pid_preset_name(tuning)
            print("      pid %s  (P %d  I %d  D %d  reset %gs  hysteresis %gK)"
                  % (preset if preset else "custom", tuning[0], tuning[1],
                     tuning[2], tuning[3], tuning[4]))
    print()
    print("  'setpoint' is the stored fixed speed. It is what hwmon pwmN reads, and")
    print("  it is only what the channel actually runs at in fixed mode.")
    print("  'sensor' is the number you would pass to --sensor.")
    print("  Read a channel's full curve with 'fan set <ch> mode curve', and its")
    print("  bar-graph maximum with 'fan set <ch> max-rpm'.")


def info_sensor(buf, names, live):
    st = decode(buf)
    print("SENSORS  (4 temperature headers, 1 flow header)")
    print("  ch  name             offset    reading")
    print("  --  ---------------  --------  -------")
    for i in range(4):
        value = live.get("temp", {}).get(i + 1)
        print("  %2d  %-15s  %+7.2f  %s"
              % (i + 1, (names[i] or "-")[:15], st["temp_offsets"][i],
                 "-" if value is None else "%.2f C" % value))
    print()
    flow = live.get("flow")
    print("  flow: %d impulses/litre    reading: %s"
          % (st["flow_pulses"], "-" if flow is None else "%.1f l/h" % flow))
    print()
    print("  Offsets are stored on the device and survive a reboot; the reading")
    print("  already has the offset applied.")


def parse_sensor(spec):
    """Turn a --sensor argument into the raw source index the report stores.

    Accepts "1".."4" (temperature channels), "flow", or "#N" for a raw index the
    tool cannot name - Aquasuite can point a controller at one of the device's
    software sensors, and those live higher in the same index space."""
    text = str(spec).strip().lower()
    if text == "flow":
        return SOURCE_FLOW
    if text.startswith("#"):
        try:
            return int(text[1:], 0)
        except ValueError:
            sys.exit("--sensor #N takes a number, e.g. --sensor '#43'.")
    try:
        n = int(text, 0)
    except ValueError:
        sys.exit("--sensor takes 1-4, 'flow', or '#N'. Got %r." % spec)
    if not 1 <= n <= 4:
        sys.exit("Temperature channels are 1-4. For the flow sensor use "
                 "--sensor flow; for anything else use --sensor '#%d'." % n)
    return n - 1


def _apply_sensor(after, before, base, spec):
    """Write a channel's controller source, printing the change."""
    value = parse_sensor(spec)
    old = be16(before, base + OFF_SOURCE)
    put_be16(after, base + OFF_SOURCE, value)
    if old != value:
        print("  sensor: %s -> %s" % (source_label(old), source_label(value)))


def cmd_mode_fixed(dev, args):
    """Fixed power: the channel stops regulating and holds the given percentage."""
    before = dev.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    prev = before[base + OFF_MODE]
    after[base + OFF_MODE] = MODE_MANUAL
    put_be16(after, base + OFF_SETPOINT, to_raw_pct(require_pct(args.percent)))
    reseal(after)
    print("channel %d: %s -> fixed %.2f%%"
          % (args.channel, mode_name(prev), args.percent))
    if prev == MODE_TARGET:
        print("  Thermal regulation stops (target was %.2f C). Restore it with"
              % (be16(before, base + OFF_TARGET) / 100.0))
        print("  'fan set %d mode target'." % args.channel)
    elif prev == MODE_CURVE:
        print("  The curve stays stored but idle. Restore it with")
        print("  'fan set %d mode curve'." % args.channel)
    commit(dev, before, after, args)


def cmd_limits(dev, args):
    """Set a channel's minimum and maximum power window."""
    require_pct(args.min, "min")
    require_pct(args.max, "max")
    require_window(args.min, args.max, args.channel)
    before = dev.read()
    rec = record_base(args.channel)
    after = bytearray(before)
    put_be16(after, rec + REC_MIN, to_raw_pct(args.min))
    put_be16(after, rec + REC_MAX, to_raw_pct(args.max))
    reseal(after)
    print("channel %d limits: %.2f%%-%.2f%% -> %.2f%%-%.2f%%"
          % (args.channel, pct(be16(before, rec + REC_MIN)),
             pct(be16(before, rec + REC_MAX)), args.min, args.max))
    if args.min == args.max:
        print("  Equal limits hold the channel at %.2f%% while its controller keeps"
              % args.min)
        print("  running - that is how you pin a regulated channel without")
        print("  switching it to fixed power.")
    commit(dev, before, after, args)


def cmd_mode_curve(dev, args):
    """Curve: 16 temperature/power points the channel interpolates between.

    With no points given this just switches the channel into curve mode and
    prints the 16 points already stored, so you can see what you enabled.
    Aquasuite's "automatic" and "manual" curve setup write the same 16 points -
    the automatic dialog only fills them in for you."""
    before = dev.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    prev = before[base + OFF_MODE]
    after[base + OFF_MODE] = MODE_CURVE
    print("channel %d: %s -> curve" % (args.channel, mode_name(prev)))

    points = None
    if args.linear is not None and args.points is not None:
        sys.exit("Give either explicit points or --linear, not both.")
    if args.linear is not None:
        t_min, t_max, p_min, p_max = args.linear
        if t_min >= t_max:
            sys.exit("Minimum temperature must be below the maximum.")
        span = CURVE_POINTS - 1
        points = [(t_min + (t_max - t_min) * i / span,
                   p_min + (p_max - p_min) * i / span) for i in range(CURVE_POINTS)]
    elif args.points is not None:
        try:
            points = [tuple(float(x) for x in pair.split(":"))
                      for pair in args.points.split(",")]
        except ValueError:
            sys.exit("Points are TEMP:POWER pairs, e.g. 27:0,28.2:3.3,...")
        if len(points) != CURVE_POINTS or any(len(pt) != 2 for pt in points):
            sys.exit("Exactly %d TEMP:POWER points are required (got %d)."
                     % (CURVE_POINTS, len(points)))

    if points is not None:
        for t, p in points:
            if not 0 <= t <= 150 or not 0 <= p <= 100:
                sys.exit("Temperatures must be 0-150 C and powers 0-100%.")
        write_curve(after, args.channel, points)
        print("  curve: %.1f-%.1f C -> %.1f-%.1f%%"
              % (points[0][0], points[-1][0], points[0][1], points[-1][1]))

    if args.startup is not None:
        if not 0 <= args.startup <= 150:
            sys.exit("Startup temperature must be 0-150 C.")
        print("  startup: %.2f C -> %.2f C"
              % (be16(before, base + OFF_STARTUP) / 100.0, args.startup))
        put_be16(after, base + OFF_STARTUP, int(round(args.startup * 100)))
    if args.sensor is not None:
        _apply_sensor(after, before, base, args.sensor)

    reseal(after)
    print("  #   temp     power")
    for i, (t, p) in enumerate(read_curve(after, args.channel), 1):
        print("  %2d  %5.2fC  %6.2f%%" % (i, t, p))
    print("  startup temperature: %.2f C"
          % (be16(after, base + OFF_STARTUP) / 100.0))
    commit(dev, before, after, args)


def cmd_pid(dev, args):
    """Read or set a channel's controller tuning.

    The device stores no preset identifier - the official software just writes
    these five numbers - so --preset is a named row of values, and any explicit
    flag given alongside it wins."""
    before = dev.read()
    base = channel_base(args.channel)
    fields = (("P", OFF_PID_P, 1.0), ("I", OFF_PID_I, 1.0), ("D", OFF_PID_D, 1.0),
              ("reset", OFF_PID_RESET, 10.0), ("hysteresis", OFF_HYSTERESIS, 100.0))
    given = dict(P=args.p, I=args.i, D=args.d,
                 reset=args.reset, hysteresis=args.hysteresis)

    preset = getattr(args, "preset", None)
    if preset is not None:
        row = PID_PRESETS[preset]
        print("preset %s (Aquasuite %s)" % (preset, PID_PRESET_SCALE[preset]))
        for (name, _off, _scale), value in zip(fields, row):
            if given[name] is None:
                given[name] = value

    if all(v is None for v in given.values()):
        current = tuple(be16(before, base + off) / scale for _n, off, scale in fields)
        match = pid_preset_name(current)
        print("  channel %d tuning: %s"
              % (args.channel,
                 "%s (Aquasuite %s)" % (match, PID_PRESET_SCALE[match])
                 if match else "custom"))
        for (name, off, scale), value in zip(fields, current):
            print("  %-11s %g" % (name, value))
        return
    after = bytearray(before)
    for name, off, scale in fields:
        value = given[name]
        if value is None:
            continue
        raw = int(round(value * scale))
        if not 0 <= raw <= 0xFFFF:
            sys.exit("%s is out of range." % name)
        print("  %-11s %g -> %g" % (name, be16(before, base + off) / scale, value))
        put_be16(after, base + off, raw)
    reseal(after)
    commit(dev, before, after, args)


def _set_record_flag(dev, args, bit, label):
    before = dev.read()
    after = bytearray(before)
    rec = record_base(args.channel)
    old = after[rec + REC_FLAGS]
    want = args.state == "on"
    after[rec + REC_FLAGS] = (old | bit) if want else (old & ~bit)
    reseal(after)
    print("channel %d %s: %s -> %s"
          % (args.channel, label, "on" if old & bit else "off", args.state))
    commit(dev, before, after, args)


def cmd_boost(dev, args):
    _set_record_flag(dev, args, REC_FLAG_BOOST, "start boost")


def cmd_holdmin(dev, args):
    _set_record_flag(dev, args, REC_FLAG_HOLD_MIN, "hold minimum power")


def cmd_maxrpm(dev, args):
    """The full-scale value Aquasuite uses for this channel's bar graph."""
    before = dev.read()
    rec = record_base(args.channel)
    if args.rpm is None:
        print("channel %d chart maximum: %d rpm"
              % (args.channel, be16(before, rec + REC_MAX_RPM)))
        return
    if not 0 <= args.rpm <= 0xFFFF:
        sys.exit("RPM must fit in 16 bits.")
    after = bytearray(before)
    put_be16(after, rec + REC_MAX_RPM, args.rpm)
    reseal(after)
    print("channel %d chart maximum: %d -> %d rpm"
          % (args.channel, be16(before, rec + REC_MAX_RPM), args.rpm))
    commit(dev, before, after, args)


def cmd_mode_target(dev, args):
    """Target temperature: the channel regulates to hold a sensor at a value.

    Both the temperature and the sensor are optional, so this doubles as a plain
    switch back into target mode with whatever was already configured."""
    before = dev.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    prev = before[base + OFF_MODE]
    after[base + OFF_MODE] = MODE_TARGET
    print("channel %d: %s -> target temperature" % (args.channel, mode_name(prev)))

    if args.celsius is not None:
        if not 0 <= args.celsius <= 100:
            sys.exit("Target temperature must be 0-100 C.")
        print("  target: %.2f C -> %.2f C"
              % (be16(before, base + OFF_TARGET) / 100.0, args.celsius))
        put_be16(after, base + OFF_TARGET, int(round(args.celsius * 100)))
    else:
        print("  target: %.2f C (unchanged)"
              % (be16(before, base + OFF_TARGET) / 100.0))
    if args.sensor is not None:
        _apply_sensor(after, before, base, args.sensor)
    reseal(after)
    commit(dev, before, after, args)


def cmd_mode_follow(dev, args):
    """Make a channel follow another channel's output."""
    if args.target == args.channel:
        sys.exit("A channel cannot follow itself.")
    before = dev.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    after[base + OFF_MODE] = args.target + LINK_OFFSET
    reseal(after)
    print("channel %d: %s -> follow channel %d"
          % (args.channel, mode_name(before[base + OFF_MODE]), args.target))
    print("  Aquasuite also copies the source channel's power window onto the")
    print("  follower when you do this in the GUI; aqdctl only sets the mode.")
    commit(dev, before, after, args)


def cmd_fallback(dev, args):
    before = dev.read()
    after = bytearray(before)
    rec = record_base(args.channel)
    put_be16(after, rec + REC_FALLBACK, to_raw_pct(require_pct(args.percent)))
    reseal(after)
    print("channel %d fallback: %.2f%% -> %.2f%%"
          % (args.channel, pct(be16(before, rec + REC_FALLBACK)), args.percent))
    commit(dev, before, after, args)


def cmd_flow(dev, args):
    before = dev.read()
    if args.impulses is None:
        print("%d impulses/litre" % be16(before, FLOW_PULSES))
        return
    if not 10 <= args.impulses <= 1000:
        sys.exit("The driver clamps this to 10-1000 impulses/litre.")
    after = bytearray(before)
    put_be16(after, FLOW_PULSES, args.impulses)
    reseal(after)
    print("flow calibration: %d -> %d impulses/litre"
          % (be16(before, FLOW_PULSES), args.impulses))
    commit(dev, before, after, args)


def cmd_offset(dev, args):
    """Read or set one temperature sensor's calibration offset."""
    before = dev.read()
    off = TEMP_OFFSETS + 2 * (args.sensor - 1)
    current = sbe16(before, off) / 100.0
    if args.celsius is None:
        print("sensor %d offset: %+.2f C" % (args.sensor, current))
        return
    # the official software and the kernel driver both cap this at +/-15 K
    if not -15.0 <= args.celsius <= 15.0:
        sys.exit("Offset must be within +/-15.00 C.")
    after = bytearray(before)
    struct.pack_into(">h", after, off, int(round(args.celsius * 100)))
    reseal(after)
    print("sensor %d offset: %+.2f C -> %+.2f C"
          % (args.sensor, current, args.celsius))
    commit(dev, before, after, args)

# -------------------------------------------------------------------- live

def read_live(serial):
    """Live readings from the kernel driver: {"fan": {1: rpm}, "temp": {1: degC},
    "flow": litres/hour}. Empty if aquacomputer_d5next is not loaded.

    Found by serial, not by hwmon name: with two Octos, the first node named
    "octo" may be the other one. A plain sysfs read, so it needs no root and no
    USB traffic - the report tells you what the Octo is CONFIGURED to do, this
    tells you what it is ACTUALLY doing, and both side by side is usually the
    question."""
    import os
    from . import discovery
    path = discovery.hwmon_dir(serial)
    if path is None:
        return {}

    def read(leaf, scale=1.0):
        try:
            with open(os.path.join(path, leaf)) as fh:
                return int(fh.read().strip()) / scale
        except (OSError, ValueError):
            return None

    out = {"fan": {}, "temp": {}, "flow": None}
    for ch in range(1, FAN_CHANNELS + 1):
        out["fan"][ch] = read("fan%d_input" % ch)
    for s in range(1, 5):
        out["temp"][s] = read("temp%d_input" % s, 1000.0)
    # fan9 is the flow sensor, reported in decilitres per hour.
    out["flow"] = read("fan9_input", 10.0)
    return out


# ---------------------------------------------------------------- commands

def cmd_info(dev, args):
    """Everything the device is configured to do, plus what it is doing now."""
    buf = dev.read()
    try:
        labels = names.decode(dev.read(core.LABEL_REPORT_ID), NAME_GROUPS)
    except Exception:
        labels = {g.cli: [""] * g.count for g in NAME_GROUPS}
    live = read_live(dev.serial)

    want = getattr(args, "section", None)
    sections = [want] if want else ["fan", "rgb", "sensor"]

    print("%s %s   active profile %d%s"
          % (TITLE, dev.serial, buf[PROFILE_INDEX],
             "" if live else "   (kernel driver not loaded)"))
    for section in sections:
        print()
        if section == "fan":
            info_fan(buf, labels["fan"], live)
        elif section == "rgb":
            rgbpx.info_rgb(buf, RGB, labels["controller"], source_label)
        elif section == "sensor":
            info_sensor(buf, labels["sensor"], live)


def cmd_protect(dev, args):
    """Mark a fan channel of this Octo as protected against accidental writes.

    Local setting, not a device one: the Octo has no such field, so it is kept
    in the config under this device's serial. Protect the headers a pump is on
    - a typo that stops a pump is not undone by undoing the command."""
    current = core.protected_channels(dev.serial)
    if args.state == "on":
        current.add(args.channel)
    else:
        current.discard(args.channel)
    path = core.set_protected(dev.serial, current)
    print("channel %d protection: %s" % (args.channel, args.state))
    print("protected channels on %s %s: %s"
          % (TITLE, dev.serial,
             ", ".join(str(c) for c in sorted(current)) if current else "none"))
    print("stored in %s" % path)


def identify(found):
    """A few of the names stored on this Octo, to tell two devices apart."""
    buf = core.peek(found, core.LABEL_REPORT_ID)
    if buf is None:
        return "(run as root, or install the udev rule, to show them)"
    fans = [n for n in names.decode(buf, NAME_GROUPS)["fan"] if n]
    text = "fans: " + ", ".join(fans) if fans else "(no fan names set)"
    return text if len(text) <= 60 else text[:57] + "..."


# ------------------------------------------------------------------ parser

def _epilog(prefix):
    return """\
vocabulary
  channel     a physical header: fan 1-8, rgb 1-2, sensor 1-4
  controller  one RGB config slot - "on channel N, LEDs A-B, effect X".
              Channel 1 has controllers 1-6, channel 2 has 7-12; 'info rgb'
              lists the ones in use with their numbers.
  position    an LED range on a channel, 1-based and inclusive: 1-15 is the
              first fifteen LEDs.

write flags  (every command that changes something accepts these)
  -n, --dry-run   read the device, show what would change, write nothing.
                  Implies --verbose.
  -v, --verbose   also print the byte-level diff of the report
  -y, --yes       skip the confirmation prompt
  --force         write even to a channel marked with 'protect on'
  --backup FILE   where to put the automatic pre-write backup

examples
  {p} info
  {p} fan set 3 mode fixed 40
  {p} fan set 3 mode curve --linear 30 45 20 100 --sensor 1
  {p} fan set 3 pid --preset fast
  {p} fan set 1 protect on
  {p} rgb create 2 pos 1-15 --effect static --colour FF0000
  {p} rgb set controller 7 --colour 00FF00
  {p} name fan 3 "Front Rad"

Every write is preceded by an automatic backup into
<your home>/.local/share/aqdctl/ (correct under sudo).""".format(p=prefix)


def build_parser(prefix):
    ap = argparse.ArgumentParser(
        prog=prefix, formatter_class=core.HelpFormatter,
        description="Read and modify the configuration of this Aquacomputer Octo.",
        epilog=_epilog(prefix))
    sub = ap.add_subparsers(dest="group")
    writer = core.add_write_flags

    # ---------------------------------------------------------------- info
    p = sub.add_parser("info", help="show configuration and live readings",
                       description="Print what the Octo is configured to do and, "
                                   "if the kernel driver is loaded, what it is "
                                   "doing right now.")
    p.add_argument("section", nargs="?", choices=["fan", "rgb", "sensor"],
                   help="limit the output to one section (default: all three)")
    p.set_defaults(func=cmd_info)

    # ----------------------------------------------------------------- fan
    fan = sub.add_parser(
        "fan", formatter_class=core.HelpFormatter,
        help="fan channels 1-8",
        description="A fan channel is one of the eight fan headers. Each decides "
                    "its speed one of four ways (see 'fan set <ch> mode') and has "
                    "its own power limits, fallback and start behaviour. If an "
                    "aquaero is connected over aquabus, it controls the fans and "
                    "the Octo ignores these settings (manual, 10.5).")
    fan.set_defaults(_helper=fan)
    fansub = fan.add_subparsers(dest="fancmd")
    fset = fansub.add_parser("set", help="change one channel's settings")
    fset.add_argument("channel", type=int, choices=range(1, FAN_CHANNELS + 1),
                      metavar="CHANNEL", help="fan header, 1-8")
    fset.set_defaults(_helper=fset)
    what = fset.add_subparsers(dest="what")

    mode = what.add_parser(
        "mode", formatter_class=core.HelpFormatter,
        help="how the channel decides its speed",
        description="How the channel decides its speed. Whatever you do not "
                    "mention keeps its stored value, so 'mode target' with no "
                    "arguments just switches back into target mode.")
    mode.set_defaults(_helper=mode)
    modesub = mode.add_subparsers(dest="mode")

    p = writer(modesub.add_parser(
        "fixed", help="hold a constant power, no regulation",
        epilog="example:  %s fan set 3 mode fixed 40" % prefix,
        formatter_class=core.HelpFormatter), guarded=True)
    p.add_argument("percent", type=float, metavar="PERCENT",
                   help="power to hold, 0-100")
    p.set_defaults(func=cmd_mode_fixed)

    p = writer(modesub.add_parser(
        "target", help="regulate to hold a sensor at a temperature",
        epilog="example:  %s fan set 3 mode target 36 --sensor 1" % prefix,
        formatter_class=core.HelpFormatter), guarded=True)
    p.add_argument("celsius", type=float, nargs="?", metavar="CELSIUS",
                   help="temperature to hold, 0-100 C (default: keep current)")
    p.add_argument("--sensor", metavar="S",
                   help="which sensor drives it: 1-4, 'flow', or '#N' for a raw index")
    p.set_defaults(func=cmd_mode_target)

    p = writer(modesub.add_parser(
        "curve", help="follow a 16-point temperature/power curve",
        epilog="examples:\n"
               "  {p} fan set 7 mode curve --linear 30 45 20 100\n"
               "  {p} fan set 7 mode curve 30:20,31:25,...  (16 pairs)\n"
               "  {p} fan set 7 mode curve                  (switch back, show it)"
               .format(p=prefix),
        formatter_class=core.HelpFormatter), guarded=True)
    p.add_argument("points", nargs="?", metavar="POINTS",
                   help="16 TEMP:POWER pairs, comma separated, e.g. 27:0,28.2:3.3,...")
    p.add_argument("--linear", nargs=4, type=float, metavar=("TMIN", "TMAX", "PMIN", "PMAX"),
                   help="generate the 16 points as a straight line between two "
                        "temperatures (C) and two powers (%%)")
    p.add_argument("--startup", type=float, metavar="CELSIUS",
                   help="startup temperature, 0-150 C")
    p.add_argument("--sensor", metavar="S",
                   help="which sensor drives it: 1-4, 'flow', or '#N' for a raw index")
    p.set_defaults(func=cmd_mode_curve)

    p = writer(modesub.add_parser(
        "follow", help="copy another channel's output",
        epilog="example:  %s fan set 6 mode follow 3" % prefix,
        formatter_class=core.HelpFormatter), guarded=True)
    p.add_argument("target", type=int, choices=range(1, FAN_CHANNELS + 1),
                   metavar="CHANNEL", help="the channel to follow, 1-8")
    p.set_defaults(func=cmd_mode_follow)

    p = writer(what.add_parser(
        "limits", help="minimum and maximum power the channel may use",
        epilog="Equal values pin the channel while its controller keeps running.\n"
               "example:  %s fan set 3 limits 25 80" % prefix,
        formatter_class=core.HelpFormatter), guarded=True)
    p.add_argument("min", type=float, metavar="MIN", help="minimum power, 0-100 %%")
    p.add_argument("max", type=float, metavar="MAX", help="maximum power, 0-100 %%")
    p.set_defaults(func=cmd_limits)

    p = writer(what.add_parser(
        "fallback", help="power used when the sensor reads nothing",
        epilog="example:  %s fan set 3 fallback 50" % prefix,
        formatter_class=core.HelpFormatter), guarded=True)
    p.add_argument("percent", type=float, metavar="PERCENT", help="power, 0-100 %%")
    p.set_defaults(func=cmd_fallback)

    p = writer(what.add_parser(
        "boost", help="brief full power when starting from a standstill"), guarded=True)
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_boost)

    p = writer(what.add_parser(
        "hold-min", help="keep running at the minimum instead of stopping"), guarded=True)
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_holdmin)

    p = writer(what.add_parser(
        "max-rpm", help="full-scale rpm for the bar graph and chart"), guarded=True)
    p.add_argument("rpm", type=int, nargs="?", metavar="RPM",
                   help="full-scale value, 0-65535 (omit to read)")
    p.set_defaults(func=cmd_maxrpm)

    p = writer(what.add_parser(
        "pid", formatter_class=core.HelpFormatter,
        help="controller tuning",
        description="Controller tuning. The device stores no preset identifier - "
                    "the official software just writes these five numbers - so "
                    "--preset is a named set of them and any explicit flag wins.",
        epilog="presets           P     I     D   reset  hysteresis\n"
               "  fastest (+2)  4000  3500  1000   0.5s     0.10 K\n"
               "  fast    (+1)  2500  2000   500   1.0s     0.10 K\n"
               "  normal   (0)  1400  1200     0   4.0s     0.20 K\n"
               "  slow    (-1)  1000   800     0   8.0s     0.30 K\n"
               "  slowest (-2)   500   300     0  10.0s     0.30 K\n\n"
               "examples:\n"
               "  {p} fan set 5 pid              (read)\n"
               "  {p} fan set 5 pid --preset fast\n"
               "  {p} fan set 5 pid --preset fast --d 600".format(p=prefix)),
        guarded=True)
    p.add_argument("--preset", choices=sorted(PID_PRESETS),
                   help="a named tuning; explicit flags below override it")
    p.add_argument("--p", type=float, metavar="V", help="proportional factor")
    p.add_argument("--i", type=float, metavar="V", help="integral factor")
    p.add_argument("--d", type=float, metavar="V", help="derivative factor")
    p.add_argument("--reset", type=float, metavar="SECONDS", help="reset time")
    p.add_argument("--hysteresis", type=float, metavar="KELVIN", help="in kelvin")
    p.set_defaults(func=cmd_pid)

    p = what.add_parser(
        "protect", formatter_class=core.HelpFormatter,
        help="refuse writes to this channel unless --force",
        description="Guard a channel of this Octo against accidental writes. Local "
                    "setting, kept under the device's serial - the Octo has no such "
                    "field. Use it on the headers your pumps are on: a typo that "
                    "stops a pump is not undone by undoing the command.",
        epilog="example:  %s fan set 1 protect on" % prefix)
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_protect, needs_device=False)

    # ------------------------------------------------------ rgb and names
    rgbpx.register(sub, sys.modules[__name__], prefix)
    names.register(sub, sys.modules[__name__], prefix)

    # -------------------------------------------------------------- sensor
    sensor = sub.add_parser(
        "sensor", formatter_class=core.HelpFormatter,
        help="temperature sensors and the flow meter",
        description="The four temperature headers, and the single flow header.")
    sensor.set_defaults(_helper=sensor)
    sensorsub = sensor.add_subparsers(dest="sensorcmd")

    p = writer(sensorsub.add_parser(
        "offset", formatter_class=core.HelpFormatter,
        help="calibration offset for one sensor",
        epilog="Stored on the device and applied to its readings.\n\n"
               "examples:\n"
               "  {p} sensor offset 1 -0.6\n"
               "  {p} sensor offset 1        (read it)".format(p=prefix)))
    p.add_argument("sensor", type=int, choices=range(1, 5), metavar="SENSOR",
                   help="temperature header, 1-4")
    p.add_argument("celsius", type=float, nargs="?", metavar="CELSIUS",
                   help="offset in degrees, -15.00 to +15.00 (omit to read)")
    p.set_defaults(func=cmd_offset)

    p = writer(sensorsub.add_parser(
        "flow", formatter_class=core.HelpFormatter,
        help="flow meter calibration",
        description="The Octo has one flow header, so this takes no index.",
        epilog="example:  %s sensor flow 169" % prefix))
    p.add_argument("impulses", type=int, nargs="?", metavar="IMPULSES",
                   help="impulses per litre, 10-1000 (omit to read)")
    p.set_defaults(func=cmd_flow)

    # ------------------------------------------------------ backup/restore
    core.add_backup_restore(sub, sys.modules[__name__])
    return ap
