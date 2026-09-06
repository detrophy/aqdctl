#!/usr/bin/env python3
"""octoctl - read and modify the Aquacomputer Octo control report.

Field map derived by reverse engineering; see FIELDS below. Verified against
Aquasuite 's own readout on firmware-current hardware. Offsets are firmware
dependent - if the CRC check or the sanity check fails, stop.

Requires: python-hidapi. Needs write access to the Octo's hidraw node
(run as root, or install a udev rule - see --help-udev).
"""

import argparse
import datetime
import os
import struct
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("python-hidapi not found (Arch: pacman -S python-hidapi)")

VENDOR_ID = 0x0C70
PRODUCT_ID = 0xF011
CTRL_REPORT_ID = 0x03
CTRL_REPORT_SIZE = 0x65F          # 1631
LABEL_REPORT_ID = 0x08
LABEL_REPORT_SIZE = 1013
# Report ids and payload sizes come from the device's HID report descriptor.
REPORT_SIZES = {CTRL_REPORT_ID: CTRL_REPORT_SIZE,
                LABEL_REPORT_ID: LABEL_REPORT_SIZE}
SECONDARY_CTRL_REPORT = bytes([0x02, 0x00, 0x00, 0x00, 0x02,
                               0x00, 0x00, 0x00, 0x00, 0x34, 0xC6])
CTRL_REPORT_DELAY = 0.2           # s; the kernel driver enforces the same gap

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

# --- report 0x08: 42 name slots of 24 bytes, NUL-padded single-byte text.
# 3 + 42*24 == 1011 == the checksum offset, so the table has no slack.
LABEL_SIZE = 24
LABEL_GROUPS = {"fan": (0x003, 8), "led": (0x0C3, 12), "temp": (0x1E3, 4),
                "flow": (0x243, 2), "soft": (0x273, 16)}
LABEL_MAX = LABEL_SIZE - 1        # keep a terminator
# Single-byte Latin-1, confirmed on hardware: renaming a channel to "hören"
# stored 68 f6 72 65 6e - five bytes with o-umlaut as 0xF6, not the six bytes
# UTF-8 would need. CP1252 is identical for everything except 0x80-0x9F, which
# no capture has exercised.
LABEL_ENCODING = "latin-1"

# --- RGBpx: 12 controllers of 70 bytes in report 0x03. The colour at +46 is
# HSV, not the Farbwerk 360's A/G/R/B palette: hue is a BE16 over 0..1535
# (six 256-step sectors), then saturation and value as single bytes.
# Master switch for the RGBpx outputs, sitting just before the slot array.
# The sense is inverted: 0x00 means the LED function is ON. Confirmed across
# every capture, and independent of which profile is loaded.
RGB_ENABLE = 0x306
RGB_ENABLE_ON, RGB_ENABLE_OFF = 0x00, 0x02

# Global brightness for every RGBpx controller, u8 over 0..255. Aquasuite's
# slider is a whole percentage but the stored value is 8-bit, so its "45"
# really means 114/255 = 44.7%.
RGB_BRIGHTNESS = 0x304

RGB_BASE, RGB_STRIDE, RGB_COUNT = 0x307, 70, 12
RGB_PORT, RGB_START, RGB_COUNT_OFF, RGB_MODE = 0, 1, 2, 3
RGB_SOURCE = 6        # BE16 data source: 0xFFFF none, else 0-based sensor index
RGB_SOURCE_NONE = 0xFFFF
RGB_FILTER_RISE = 8   # u8, "filtering of fluctuating values" - rising
RGB_FILTER_FALL = 9   # u8, same, falling
# Second flags byte, holding the data-source control toggles. Both bits are
# confirmed: switching "control brightness by data source" off cleared 0x80
# and left 0x40 standing.
RGB_SOURCE_FLAGS = 4
RGB_SOURCE_FLAG_NAMES = {"source_speed": 0x40, "source_brightness": 0x80}
# Two mapping blocks: input min/max as BE16, then output min and output max as
# single bytes. Block A drives speed, block B brightness. The input range comes
# from the chosen source (a temperature reads 20..70, the flow sensor 0..300);
# the output range is a fixed property of the effect - wave accepts 0..100 while
# breathing accepts 1..100, because a breathing speed of 0 would simply stop.
RGB_MAPS = ((10, 12, 14, 15), (16, 18, 20, 21))

# Source index space, shared with the fan controllers: temperature sensors
# first, then the flow sensor. Index 4 confirmed by a capture whose chart
# switched to the flow sensor's 0..300 range.
SOURCE_FLOW = 4


def source_label(index):
    if index == SOURCE_FLOW:
        return "flow"
    if 0 <= index < 4:
        return "sensor %d" % (index + 1)
    return "source %d" % index
# Parameters are BE16 words at +22+2k. Values below 256 decode identically
# under a LE16-at-+23 reading, so only an effect with a large value (colour
# gradient, limits up to 1000) distinguishes them - that capture settled it.
RGB_PARAM = 22
RGB_COLOR = 46        # 6 palette entries of 4 bytes
RGB_PALETTE_ENTRIES = 6
HUE_FULL = 1536

# Effect ids and parameter names come from the Farbwerk 360 (AquaControl's
# PROTOCOL_EFFECTS.md). Verified on this Octo for 0x01 static and 0x0A wave;
# the rest are carried over on the strength of that match, not observed here.
RGB_MODES = {0x01: "static", 0x02: "breathing", 0x03: "rotating rainbow",
             0x04: "blinking", 0x05: "color change", 0x07: "sequence",
             0x08: "scanner", 0x09: "laser", 0x0A: "wave",
             0x0B: "color sequence", 0x0C: "color shift", 0x0D: "bar graph",
             0x0E: "flame", 0x0F: "rain", 0x10: "snowfall", 0x11: "stardust",
             0x12: "color switch", 0x13: "swiping rainbow",
             0x14: "sound flash", 0x15: "sound bars", 0x16: "sound slider",
             0x17: "sound shift", 0x18: "ambientpx",
             0x21: "color gradient"}
# 0x14-0x18 are driven by an Aquasuite host DLL (audio capture, screen capture).
# The device stores the mode and the palette, but the animation data is streamed
# from the PC, so these do nothing on a machine without Aquasuite running.
RGB_MODES_HOST_DRIVEN = {0x14, 0x15, 0x16, 0x17, 0x18}

# Ids that are NOT implemented on this firmware. The device stores whatever
# byte you put here - it does not validate the field - so a write "verifies"
# cleanly, the LEDs stay dark, and Aquasuite falls back to showing the raw
# number with its "select a mode" placeholder. Probed directly on hardware.
RGB_MODES_UNIMPLEMENTED = {0x06} | set(range(0x19, 0x21))
# Every id above has been observed on this Octo and matched against the
# settings shown in Aquasuite. 0x06 is the only gap left in the range.
RGB_MODES_VERIFIED = set(RGB_MODES)
RGB_FLAGS_OFFSET = 5
RGB_PARAM_NAMES = {
    0x01: [],
    0x02: ["speed", "intensity", "delay_max", "delay_min"],
    0x03: ["speed", "color_range"],
    0x04: ["speed", "count"],
    0x05: ["speed", "count"],
    0x07: ["speed", "smoothness", "count", "delay_after", "delay_before"],
    0x0B: ["speed", "smoothness", "", "count", "color_change_speed"],
    0x0C: ["speed", "color_range", "total_area"],
    0x13: ["point_speed", "point_smoothness", "point_size",
           "color_change_speed", "color_range"],
    0x08: ["speed", "smoothness", "width", "start_delay",
           "interval_min", "interval_max"],
    0x09: ["speed", "smoothness", "width", "start_delay",
           "interval_min", "interval_max"],
    0x0A: ["speed", "smoothness", "width", "count"],
    0x0E: ["intensity"],
    0x0F: ["drop_speed", "drop_items", "drop_size", "drop_smoothness",
           "runtime", "interval_min", "interval_max"],
}
RGB_PARAM_NAMES[0x10] = RGB_PARAM_NAMES[0x11] = RGB_PARAM_NAMES[0x0F]
# Bitmasks in the flags byte at +5. fade on colour-change is directly confirmed;
# the all-off captures confirm the others are absent, not their values.
RGB_FLAG_NAMES = {
    0x04: {"fade": 0x04, "random_color": 0x08, "slide_colors": 0x10},
    0x05: {"fade": 0x04, "random_color": 0x08, "slide_colors": 0x10},
    # color_mode2 swaps the symmetric [bg, c2, c1, c2, bg] dot for a plain
    # 50/50 split [bg, c1, c2, bg]. Confirmed: it is the only byte that differs
    # between an otherwise identical mode-1 and mode-2 scanner or laser.
    0x08: {"reverse": 0x02, "random_color": 0x08, "color_mode2": 0x20,
           "color_change": 0x40, "circular": 0x80},
    0x09: {"reverse": 0x02, "random_color": 0x08, "color_mode2": 0x20,
           "color_change": 0x40, "circular": 0x80},
    0x0A: {"reverse": 0x02, "random_color": 0x08, "circular": 0x80},
    0x0F: {"reverse": 0x02, "random_color": 0x08, "snow": 0x10},
}
RGB_FLAG_NAMES[0x10] = RGB_FLAG_NAMES[0x11] = RGB_FLAG_NAMES[0x0F]
RGB_FLAG_NAMES[0x02] = {}
RGB_FLAG_NAMES[0x03] = {"reverse": 0x02}
RGB_FLAG_NAMES[0x0C] = {"reverse": 0x02}
RGB_FLAG_NAMES[0x13] = {"reverse": 0x01}
RGB_FLAG_NAMES[0x07] = {"reverse": 0x02, "fade": 0x04, "random_color": 0x08}
RGB_FLAG_NAMES[0x0B] = {"reverse": 0x02, "random_color": 0x08}
# Which palette entries an effect uses, and what they mean.
RGB_PALETTE_ROLES = {
    0x01: ["colour"],
    0x05: ["colour list"],            # entries 0..count-1, no background
    0x08: ["background", "colour 1", "colour 2"],
    0x09: ["background", "colour 1", "colour 2"],
    0x0A: ["background", "colour"],
    0x0F: ["background", "colour"],
}
FLOW_PULSES = 0x06   # BE16 impulses per litre

# The mode byte is not a small enum: 0x00-0x02 are the three controller types,
# and anything from 0x03 up means "follow another channel", with the target
# encoded as mode - LINK_OFFSET. Confirmed by two captures - channel 2 follows
# channel 1 and reads 0x03; pointing channel 6 at channel 3 wrote 0x05.
# Linking also copies the source channel's power window onto the follower.
MODE_MANUAL, MODE_TARGET, MODE_CURVE = 0x00, 0x01, 0x02
LINK_OFFSET = 2
LINK_MIN = LINK_OFFSET + 1          # 0x03 == follow channel 1
MODE_BY_NAME = {"manual": MODE_MANUAL, "target": MODE_TARGET, "curve": MODE_CURVE}


def is_linked(value):
    return value >= LINK_MIN


def link_target(value):
    return value - LINK_OFFSET


def mode_name(value):
    if is_linked(value):
        target = link_target(value)
        return "link->ch%d" % target if 1 <= target <= 8 else "link->?%d" % target
    return {MODE_MANUAL: "manual", MODE_TARGET: "target-temp",
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

PUMP_CHANNELS = {1, 2}


def _invoking_user():
    """Under sudo, os.path.expanduser('~') is /root - so backups would land where
    the user cannot see them. Resolve the real invoking account instead."""
    name = os.environ.get("SUDO_USER")
    if not name:
        return None
    try:
        import pwd
        return pwd.getpwnam(name)
    except (ImportError, KeyError):
        return None


def backup_dir():
    user = _invoking_user()
    home = user.pw_dir if user else os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "octoctl")


def _give_back(path):
    """Hand a file created under sudo to the invoking user."""
    user = _invoking_user()
    if user:
        try:
            os.chown(path, user.pw_uid, user.pw_gid)
        except OSError:
            pass


# --------------------------------------------------------------------- codec

def crc16_usb(data):
    """CRC-16/USB: poly 0x8005 reflected, init 0xFFFF, refin/refout, xorout 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def report_crc(buf):
    """(stored, computed) checksum of a control report."""
    return struct.unpack_from(">H", buf, len(buf) - 2)[0], crc16_usb(buf[1:len(buf) - 2])


def reseal(buf):
    """Recompute the trailing checksum in place. Every write must go through this."""
    struct.pack_into(">H", buf, len(buf) - 2, crc16_usb(bytes(buf[1:len(buf) - 2])))
    return buf


def be16(buf, off):
    return struct.unpack_from(">H", buf, off)[0]


def sbe16(buf, off):
    return struct.unpack_from(">h", buf, off)[0]


def put_be16(buf, off, val):
    struct.pack_into(">H", buf, off, val & 0xFFFF)


def pct(raw):
    return raw / 100.0


def to_raw_pct(percent):
    return int(round(percent * 100))


# -------------------------------------------------------------------- device

class Octo:
    def __init__(self, path):
        self.path = path
        self.dev = None
        self._last_op = 0.0

    @staticmethod
    def find():
        """Return the first HID interface that answers the control report."""
        candidates = list(hid.enumerate(VENDOR_ID, PRODUCT_ID))
        if not candidates:
            sys.exit("No Octo found on USB (%04x:%04x)." % (VENDOR_ID, PRODUCT_ID))
        for info in candidates:
            probe = hid.device()
            try:
                probe.open_path(info["path"])
                probe.get_feature_report(CTRL_REPORT_ID, CTRL_REPORT_SIZE)
                probe.close()
                return Octo(info["path"])
            except Exception:
                try:
                    probe.close()
                except Exception:
                    pass
        sys.exit("Found an Octo but no interface answered report 0x03.\n"
                 "This usually means insufficient permissions - run as root, or see "
                 "--help-udev.")

    def __enter__(self):
        self.dev = hid.device()
        self.dev.open_path(self.path)
        return self

    def __exit__(self, *exc):
        try:
            self.dev.close()
        except Exception:
            pass

    def _pace(self):
        wait = CTRL_REPORT_DELAY - (time.monotonic() - self._last_op)
        if wait > 0:
            time.sleep(wait)

    def read(self, report_id=CTRL_REPORT_ID):
        size = REPORT_SIZES[report_id]
        self._pace()
        buf = bytes(self.dev.get_feature_report(report_id, size))
        self._last_op = time.monotonic()
        if len(buf) != size:
            sys.exit("Short report %#04x: got %d bytes, expected %d."
                     % (report_id, len(buf), size))
        stored, computed = report_crc(buf)
        if stored != computed:
            sys.exit("Report %#04x checksum mismatch (stored %#06x, computed %#06x).\n"
                     "Refusing to continue - the layout assumptions do not hold on this "
                     "firmware." % (report_id, stored, computed))
        return bytearray(buf)

    def write(self, buf):
        """Send a resealed control report, then the commit report the official
        software sends after every change."""
        stored, computed = report_crc(buf)
        if stored != computed:
            sys.exit("Refusing to send a report with a stale checksum (internal error).")
        self._pace()
        if self.dev.send_feature_report(bytes(buf)) < 0:
            sys.exit("send_feature_report failed.")
        self._last_op = time.monotonic()
        self._pace()
        try:
            self.dev.send_feature_report(SECONDARY_CTRL_REPORT)
        except Exception:
            self.dev.write(SECONDARY_CTRL_REPORT)   # some builds route it as output
        self._last_op = time.monotonic()


# -------------------------------------------------------------------- decode

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


def print_state(buf):
    st = decode(buf)
    print("active profile   : %d" % buf[PROFILE_INDEX])
    print("flow calibration : %d impulses/litre" % st["flow_pulses"])
    print("sensor offsets   : " + ", ".join("%+.2f" % o for o in st["temp_offsets"]))
    print()
    print("ch  mode         setpoint  target  sensor  boost  hold    min     max"
          "  fallback  chart")
    print("--  -----------  --------  ------  ------  -----  ----  ------  ------"
          "  --------  -----")
    for c in st["channels"]:
        pump = "  <- pump" if c["channel"] in PUMP_CHANNELS else ""
        # the power window and fallback apply to any controller mode; the
        # target temperature only means something in target-temp mode
        if c["mode"] in (MODE_TARGET, MODE_CURVE):
            ctrl = "%6.2f  %6.2f  %8.2f" % (c["min"], c["max"], c["fallback"])
        else:
            ctrl = "     -       -         -"
        target = "%6.2f" % c["target_temp"] if c["mode"] == MODE_TARGET else "     -"
        uses_source = c["mode"] in (MODE_TARGET, MODE_CURVE)
        print("%2d  %-11s  %7.2f%%  %s  %6s  %5s  %4s  %s  %5d%s"
              % (c["channel"], c["mode_name"], c["setpoint"], target,
                 str(c["source"] + 1) if uses_source else "-",
                 "on" if c["boost"] else "off",
                 "on" if c["hold_min"] else "off", ctrl, c["max_rpm"], pump))
        if c["mode"] == MODE_CURVE:
            pts = read_curve(buf, c["channel"])
            print("      curve %.1f-%.1fC -> %.1f-%.1f%%   startup %.2fC"
                  % (pts[0][0], pts[-1][0], pts[0][1], pts[-1][1],
                     be16(buf, channel_base(c["channel"]) + OFF_STARTUP) / 100.0))
    print()
    print("Note: 'setpoint' is the stored manual speed - it is what hwmon pwmN reads,")
    print("      and it is only what the channel actually runs at in manual mode.")


# ------------------------------------------------------------------- helpers

def backup(buf, explicit=None, rid=None):
    if explicit:
        path = explicit
    else:
        directory = backup_dir()
        fresh = not os.path.isdir(directory)
        os.makedirs(directory, exist_ok=True)
        if fresh:
            _give_back(directory)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = "%02x" % (rid if rid is not None else buf[0])
        path = os.path.join(directory, "octo-%s-%s.bin" % (tag, stamp))
    with open(path, "wb") as fh:
        fh.write(bytes(buf))
    _give_back(path)
    return path


def diff(before, after):
    return [(i, before[i], after[i]) for i in range(len(before)) if before[i] != after[i]]


def show_diff(before, after):
    changes = diff(before, after)
    tail = len(before) - 2
    body = [c for c in changes if c[0] < tail]
    print("byte changes (excluding checksum): %d" % len(body))
    for off, old, new in body:
        print("  0x%03x: %02x -> %02x" % (off, old, new))
    if len(changes) != len(body):
        print("  checksum resealed")


def guard_pump(ch, force):
    if ch in PUMP_CHANNELS and not force:
        sys.exit("Channel %d is a pump. Refusing without --force-pump." % ch)


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


def rgb_to_hsv(r, g, b):
    hi, lo = max(r, g, b), min(r, g, b)
    d = hi - lo
    if d == 0:
        hue = 0.0
    elif hi == r:
        hue = (((g - b) / d) % 6) * 256
    elif hi == g:
        hue = (((b - r) / d) + 2) * 256
    else:
        hue = (((r - g) / d) + 4) * 256
    return int(round(hue)) % HUE_FULL, (0 if hi == 0 else int(round(d * 255 / hi))), hi


def hsv_to_rgb(h, s, v):
    deg, sat, val = h * 360.0 / HUE_FULL, s / 255.0, v / 255.0
    c = val * sat
    x = c * (1 - abs((deg / 60.0) % 2 - 1))
    m = val - c
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x),
               (0, x, c), (x, 0, c), (c, 0, x)][int(deg // 60) % 6]
    return tuple(int(round((q + m) * 255)) for q in (r, g, b))


def rgb_slot(index):
    return RGB_BASE + RGB_STRIDE * (index - 1) + RGB_COLOR


def parse_hex_colour(text):
    t = text.lstrip("#")
    if len(t) != 6:
        sys.exit("Colour must be RRGGBB, e.g. FF0000.")
    try:
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        sys.exit("Colour must be RRGGBB, e.g. FF0000.")


def decode_labels(buf):
    out = {}
    for group, (base, count) in LABEL_GROUPS.items():
        names = []
        for i in range(count):
            raw = buf[base + LABEL_SIZE * i: base + LABEL_SIZE * (i + 1)]
            names.append(raw.split(b"\x00")[0].decode(LABEL_ENCODING, "replace"))
        out[group] = names
    return out


def commit(octo, before, after, args):
    if not diff(before, after):
        print("Nothing to change.")
        return
    show_diff(before, after)
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return
    path = backup(before, args.backup)
    print("\nbackup written to %s" % path)
    if not args.yes:
        print("These writes go into the Octo's persistent profile - they survive a")
        print("reboot and will show up in Aquasuite.")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Aborted.")
    octo.write(after)
    verify = octo.read(before[0])
    if diff(verify, after):
        print("WARNING: device state differs from what was written:")
        show_diff(after, verify)
        print("The firmware may have rejected or adjusted some values.")
    else:
        print("Written and verified.")


# ------------------------------------------------------------------ commands

def cmd_show(octo, args):
    print_state(octo.read())


def cmd_dump(octo, args):
    for rid in (CTRL_REPORT_ID, LABEL_REPORT_ID):
        buf = octo.read(rid)
        target = args.output
        if target and rid != CTRL_REPORT_ID:
            root, ext = os.path.splitext(target)
            target = "%s-%02x%s" % (root, rid, ext)
        path = backup(buf, target, rid)
        print("report %#04x: wrote %d bytes to %s" % (rid, len(buf), path))


def cmd_restore(octo, args):
    with open(args.file, "rb") as fh:
        saved = bytearray(fh.read())
    rid = saved[0] if saved else None
    if rid not in REPORT_SIZES or len(saved) != REPORT_SIZES[rid]:
        sys.exit("%s is not a recognised report dump (id %r, %d bytes)."
                 % (args.file, rid, len(saved)))
    stored, computed = report_crc(saved)
    if stored != computed:
        sys.exit("%s has a bad checksum - refusing to restore it." % args.file)
    before = octo.read(rid)
    commit(octo, before, saved, args)


def cmd_set(octo, args):
    """Take manual control of a channel: mode -> manual, write the speed."""
    before = octo.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    prev = before[base + OFF_MODE]
    after[base + OFF_MODE] = MODE_MANUAL
    put_be16(after, base + OFF_SETPOINT, to_raw_pct(args.percent))
    reseal(after)
    if prev == MODE_TARGET:
        print("Channel %d leaves target-temperature control (target %.2f C)."
              % (args.channel, be16(before, base + OFF_TARGET) / 100.0))
        print("Its thermal regulation stops until you run 'mode %d target'."
              % args.channel)
    commit(octo, before, after, args)


def cmd_pin(octo, args):
    """Hold a channel at a fixed percentage without leaving controller mode,
    by collapsing its min/max power window."""
    before = octo.read()
    base, rec = channel_base(args.channel), record_base(args.channel)
    if before[base + OFF_MODE] != MODE_TARGET:
        sys.exit("Channel %d is in %s mode; pin only applies to target-temperature "
                 "mode. Use 'set' instead."
                 % (args.channel, mode_name(before[base + OFF_MODE])))
    print("current window: %.2f%% - %.2f%%  (save these to undo)"
          % (pct(be16(before, rec + REC_MIN)), pct(be16(before, rec + REC_MAX))))
    after = bytearray(before)
    raw = to_raw_pct(args.percent)
    put_be16(after, rec + REC_MIN, raw)
    put_be16(after, rec + REC_MAX, raw)
    reseal(after)
    commit(octo, before, after, args)


def cmd_window(octo, args):
    """Restore or set a channel's min/max power window."""
    before = octo.read()
    rec = record_base(args.channel)
    after = bytearray(before)
    put_be16(after, rec + REC_MIN, to_raw_pct(args.min))
    put_be16(after, rec + REC_MAX, to_raw_pct(args.max))
    reseal(after)
    commit(octo, before, after, args)


def get_param(buf, base, k):
    o = base + RGB_PARAM + 2 * k
    return (buf[o] << 8) | buf[o + 1]


def set_param(buf, base, k, value):
    o = base + RGB_PARAM + 2 * k
    buf[o] = (value >> 8) & 0xFF
    buf[o + 1] = value & 0xFF


def rgb_entry(index, entry=0):
    return RGB_BASE + RGB_STRIDE * (index - 1) + RGB_COLOR + 4 * entry


def read_entry(buf, index, entry=0):
    o = rgb_entry(index, entry)
    return (buf[o] << 8) | buf[o + 1], buf[o + 2], buf[o + 3]


def describe_rgb(buf, index, name):
    base = RGB_BASE + RGB_STRIDE * (index - 1)
    mode = buf[base + RGB_MODE]
    tag = RGB_MODES.get(mode, "unimplemented"
                        if mode in RGB_MODES_UNIMPLEMENTED else "unknown")
    if mode not in RGB_MODES_VERIFIED and mode in RGB_MODES:
        tag += "?"
    if mode in RGB_MODES_HOST_DRIVEN:
        tag += " [needs Aquasuite on the host]"
    print("  %2d  %-18s port %d  LEDs %d..%d (%d)  mode %#04x %s"
          % (index, name, buf[base + RGB_PORT], buf[base + RGB_START],
             buf[base + RGB_START] + buf[base + RGB_COUNT_OFF] - 1,
             buf[base + RGB_COUNT_OFF], mode, tag))
    words = [get_param(buf, base, k) for k in range(9)]
    labels = RGB_PARAM_NAMES.get(mode, [])
    shown = [("%s=%d" % (labels[k], w)) if k < len(labels) else "k%d=%d" % (k, w)
             for k, w in enumerate(words) if w or k < len(labels)]
    if shown:
        print("      params: " + "  ".join(shown))
    flags = buf[base + RGB_FLAGS_OFFSET]
    known = RGB_FLAG_NAMES.get(mode, {})
    on = [n for n, bit in sorted(known.items()) if flags & bit]
    leftover = flags & ~sum(known.values()) if known else flags
    if on or leftover:
        extra = "  unknown bits %#04x" % leftover if leftover else ""
        print("      flags : %#04x  %s%s" % (flags, ", ".join(on) or "none", extra))
    src = (buf[base + RGB_SOURCE] << 8) | buf[base + RGB_SOURCE + 1]
    if src != RGB_SOURCE_NONE:
        print("      source: %s (index %d)   filter rise=%d fall=%d"
              % (source_label(src), src,
                 buf[base + RGB_FILTER_RISE], buf[base + RGB_FILTER_FALL]))
        sflags = buf[base + RGB_SOURCE_FLAGS]
        on = [n for n, bit in sorted(RGB_SOURCE_FLAG_NAMES.items()) if sflags & bit]
        rest = sflags & ~sum(RGB_SOURCE_FLAG_NAMES.values())
        if sflags:
            print("      source flags: %#04x  %s%s"
                  % (sflags, ", ".join(on) or "none",
                     "  unknown bits %#04x" % rest if rest else ""))
        for tag, (lo, hi, omin, omax) in zip(("A speed", "B bright"), RGB_MAPS):
            print("      map %-8s: %d .. %d  ->  %d .. %d" % (tag,
                  (buf[base + lo] << 8) | buf[base + lo + 1],
                  (buf[base + hi] << 8) | buf[base + hi + 1],
                  buf[base + omin], buf[base + omax]))
    roles = RGB_PALETTE_ROLES.get(mode, [])
    for e in range(RGB_PALETTE_ENTRIES):
        h, sat, val = read_entry(buf, index, e)
        if h or sat or val:
            if mode == 0x05:
                role = " (colour %d of list)" % (e + 1)
            elif e < len(roles):
                role = " (%s)" % roles[e]
            else:
                role = ""
            print("      colour %d: #%02X%02X%02X%s" % (e, *hsv_to_rgb(h, sat, val), role))


def cmd_rgb(octo, args):
    buf = octo.read()
    try:
        names = decode_labels(octo.read(LABEL_REPORT_ID))["led"]
    except Exception:
        names = ["LED Controller %d" % i for i in range(1, RGB_COUNT + 1)]

    if args.power is not None:
        want = RGB_ENABLE_ON if args.power == "on" else RGB_ENABLE_OFF
        after = bytearray(buf)
        after[RGB_ENABLE] = want
        reseal(after)
        print("RGBpx function: %s -> %s"
              % ("on" if buf[RGB_ENABLE] == RGB_ENABLE_ON else "off", args.power))
        commit(octo, buf, after, args)
        return

    if args.brightness is not None:
        if not 0 <= args.brightness <= 100:
            sys.exit("Brightness is a percentage, 0-100.")
        # Aquasuite truncates rather than rounds: its "45" stores 114, not 115.
        raw = min(255, int(args.brightness * 255 / 100.0))
        after = bytearray(buf)
        after[RGB_BRIGHTNESS] = raw
        reseal(after)
        print("global brightness: %.1f%% (%d) -> %.1f%% (%d)"
              % (buf[RGB_BRIGHTNESS] * 100.0 / 255, buf[RGB_BRIGHTNESS],
                 raw * 100.0 / 255, raw))
        commit(octo, buf, after, args)
        return

    if args.index is None:
        print("RGBpx function: %s   global brightness: %.1f%% (%d/255)"
              % ("on" if buf[RGB_ENABLE] == RGB_ENABLE_ON else "off",
                 buf[RGB_BRIGHTNESS] * 100.0 / 255, buf[RGB_BRIGHTNESS]))
        for i in range(1, RGB_COUNT + 1):
            describe_rgb(buf, i, names[i - 1])
        return
    if not 1 <= args.index <= RGB_COUNT:
        sys.exit("Controller must be 1..%d." % RGB_COUNT)
    if (args.colour is None and args.param is None and args.mode is None
            and args.source is None and args.filter_rise is None
            and args.filter_fall is None and not args.flag and not args.no_flag):
        describe_rgb(buf, args.index, names[args.index - 1])
        return

    base = RGB_BASE + RGB_STRIDE * (args.index - 1)
    after = bytearray(buf)
    # Apply the mode first: parameter and flag names are per-effect, so setting
    # "--mode wave --flag reverse" in one go must validate against wave.
    if args.mode is not None:
        lookup = {v: k for k, v in RGB_MODES.items()}
        mode = lookup.get(args.mode, None)
        if mode is None:
            try:
                mode = int(args.mode, 0)
            except ValueError:
                sys.exit("Unknown effect %r. Known: %s"
                         % (args.mode, ", ".join(sorted(lookup))))
        if mode in RGB_MODES_UNIMPLEMENTED or mode not in RGB_MODES:
            print("Warning: %#04x is not an implemented effect on this firmware." % mode)
            print("         The device stores the byte without validating it, so this")
            print("         write will verify cleanly, but the LEDs will stay dark and")
            print("         Aquasuite will show a placeholder instead of an effect.")
        elif mode not in RGB_MODES_VERIFIED:
            print("Note: effect %#04x has not been verified on the Octo - its id and")
            print("      parameter layout are carried over from the Farbwerk 360.")
        if mode in RGB_MODES_HOST_DRIVEN:
            print("Note: effect %#04x is driven by an Aquasuite host DLL. The device")
            print("      stores it, but nothing animates without Aquasuite running,")
            print("      so on Linux the channel will simply sit at its background.")
        after[base + RGB_MODE] = mode
        print("mode: %#04x -> %#04x (%s)"
              % (buf[base + RGB_MODE], mode, RGB_MODES.get(mode, "?")))
    if args.source is not None:
        if args.source.lower() in ("none", "off"):
            value = RGB_SOURCE_NONE
        elif args.source.lower() == "flow":
            value = SOURCE_FLOW
        else:
            n = int(args.source, 0)
            if not 1 <= n <= 4:
                print("Note: sensors 1-4 and \"flow\" are confirmed as sources;")
                print("      other indices are untested.")
            value = n - 1
        old = (after[base + RGB_SOURCE] << 8) | after[base + RGB_SOURCE + 1]
        after[base + RGB_SOURCE] = (value >> 8) & 0xFF
        after[base + RGB_SOURCE + 1] = value & 0xFF
        def _name(v):
            return "none" if v == RGB_SOURCE_NONE else "sensor %d" % (v + 1)
        print("source: %s -> %s" % (_name(old), _name(value)))
        if value != RGB_SOURCE_NONE and old == RGB_SOURCE_NONE:
            print("      (Aquasuite also rewrites both mapping ranges to 20..70 here;")
            print("       octoctl leaves them alone - set them yourself if needed.)")
    for value, off, label in ((args.filter_rise, RGB_FILTER_RISE, "rise"),
                              (args.filter_fall, RGB_FILTER_FALL, "fall")):
        if value is None:
            continue
        if not 0 <= value <= 255:
            sys.exit("Filter values are a single byte (0-255).")
        print("filter %s: %d -> %d" % (label, after[base + off], value))
        after[base + off] = value
    if args.colour is not None:
        if not 0 <= args.entry < RGB_PALETTE_ENTRIES:
            sys.exit("Palette entry must be 0..%d." % (RGB_PALETTE_ENTRIES - 1))
        r, g, b = parse_hex_colour(args.colour)
        h, sat, val = rgb_to_hsv(r, g, b)
        o = rgb_entry(args.index, args.entry)
        after[o], after[o + 1], after[o + 2], after[o + 3] = \
            (h >> 8) & 0xFF, h & 0xFF, sat, val
        print("colour %d: #%02X%02X%02X -> #%02X%02X%02X"
              % (args.entry, *hsv_to_rgb(*read_entry(buf, args.index, args.entry)), r, g, b))
    for spec in (args.param or []):
        if "=" not in spec:
            sys.exit("--param takes K=VALUE, e.g. --param 0=30")
        key, _, raw = spec.partition("=")
        labels = RGB_PARAM_NAMES.get(after[base + RGB_MODE], [])
        k = labels.index(key) if key in labels else int(key)
        if not 0 <= k < 9:
            sys.exit("Parameter index must be 0..8.")
        value = int(raw)
        if not 0 <= value <= 0xFFFF:
            sys.exit("Parameter value must fit in 16 bits.")
        print("param k%d: %d -> %d" % (k, get_param(buf, base, k), value))
        set_param(after, base, k, value)
    for spec, want in [(f, True) for f in (args.flag or [])] + \
                      [(f, False) for f in (args.no_flag or [])]:
        known = RGB_FLAG_NAMES.get(after[base + RGB_MODE], {})
        if spec in RGB_SOURCE_FLAG_NAMES:
            off, bit = RGB_SOURCE_FLAGS, RGB_SOURCE_FLAG_NAMES[spec]
        elif spec in known:
            off, bit = RGB_FLAGS_OFFSET, known[spec]
        else:
            sys.exit("Effect %#04x has no flag %r. Known: %s"
                     % (after[base + RGB_MODE], spec,
                        ", ".join(sorted(set(known) | set(RGB_SOURCE_FLAG_NAMES))) or "none"))
        old = after[base + off]
        after[base + off] = (old | bit) if want else (old & ~bit)
        print("flag %s: %s -> %s" % (spec, "on" if old & bit else "off",
                                     "on" if want else "off"))
    reseal(after)
    commit(octo, buf, after, args)


def cmd_labels(octo, args):
    buf = octo.read(LABEL_REPORT_ID)
    for group, names in decode_labels(buf).items():
        print("%s:" % group)
        for i, name in enumerate(names, 1):
            print("  %-5s %-2d  %s" % (group, i, name if name else "-"))


def cmd_label(octo, args):
    base, count = LABEL_GROUPS[args.group]
    if not 1 <= args.index <= count:
        sys.exit("%s index must be 1..%d." % (args.group, count))
    buf = octo.read(LABEL_REPORT_ID)
    slot = base + LABEL_SIZE * (args.index - 1)
    current = buf[slot:slot + LABEL_SIZE].split(b"\x00")[0].decode(LABEL_ENCODING, "replace")
    if args.text is None:
        print(current)
        return
    try:
        encoded = args.text.encode(LABEL_ENCODING)
    except UnicodeEncodeError:
        sys.exit("%r cannot be encoded as %s. Only single-byte text is known to work."
                 % (args.text, LABEL_ENCODING))
    if len(encoded) > LABEL_MAX:
        sys.exit("Label is %d bytes; the slot holds %d." % (len(encoded), LABEL_MAX))
    after = bytearray(buf)
    after[slot:slot + LABEL_SIZE] = encoded.ljust(LABEL_SIZE, b"\x00")
    reseal(after)
    print("%s %d: %r -> %r" % (args.group, args.index, current, args.text))
    commit(octo, buf, after, args)


def cmd_curve(octo, args):
    before = octo.read()
    base = channel_base(args.channel)
    if args.linear is None and args.points is None:
        if before[base + OFF_MODE] != MODE_CURVE:
            print("Note: channel %d is in %s mode, so this curve is stored but idle."
                  % (args.channel, mode_name(before[base + OFF_MODE])))
        print("  #   temp     power")
        for i, (t, p) in enumerate(read_curve(before, args.channel), 1):
            print("  %2d  %5.2fC  %6.2f%%" % (i, t, p))
        print("  startup temperature: %.2fC"
              % (be16(before, base + OFF_STARTUP) / 100.0))
        return

    if args.linear is not None:
        t_min, t_max, p_min, p_max = args.linear
        if t_min >= t_max:
            sys.exit("Minimum temperature must be below the maximum.")
        span = CURVE_POINTS - 1
        points = [(t_min + (t_max - t_min) * i / span,
                   p_min + (p_max - p_min) * i / span) for i in range(CURVE_POINTS)]
    else:
        try:
            points = [tuple(float(x) for x in pair.split(":"))
                      for pair in args.points.split(",")]
        except ValueError:
            sys.exit("--points takes TEMP:POWER pairs, e.g. 27:0,28.2:3.3,...")
        if len(points) != CURVE_POINTS or any(len(pt) != 2 for pt in points):
            sys.exit("Exactly %d TEMP:POWER points are required." % CURVE_POINTS)
    for t, p in points:
        if not 0 <= t <= 150 or not 0 <= p <= 100:
            sys.exit("Temperatures must be 0-150 C and powers 0-100%.")
    after = bytearray(before)
    write_curve(after, args.channel, points)
    reseal(after)
    print("curve: %.1f-%.1fC -> %.1f-%.1f%%"
          % (points[0][0], points[-1][0], points[0][1], points[-1][1]))
    commit(octo, before, after, args)


def cmd_pid(octo, args):
    """Read or set a channel's controller tuning."""
    before = octo.read()
    base = channel_base(args.channel)
    fields = (("P", OFF_PID_P, 1.0), ("I", OFF_PID_I, 1.0), ("D", OFF_PID_D, 1.0),
              ("reset", OFF_PID_RESET, 10.0), ("hysteresis", OFF_HYSTERESIS, 100.0))
    given = dict(P=args.p, I=args.i, D=args.d,
                 reset=args.reset, hysteresis=args.hysteresis)
    if all(v is None for v in given.values()):
        for name, off, scale in fields:
            print("  %-11s %g" % (name, be16(before, base + off) / scale))
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
    commit(octo, before, after, args)


def _set_record_flag(octo, args, bit, label):
    before = octo.read()
    after = bytearray(before)
    rec = record_base(args.channel)
    old = after[rec + REC_FLAGS]
    want = args.state == "on"
    after[rec + REC_FLAGS] = (old | bit) if want else (old & ~bit)
    reseal(after)
    print("channel %d %s: %s -> %s"
          % (args.channel, label, "on" if old & bit else "off", args.state))
    commit(octo, before, after, args)


def cmd_boost(octo, args):
    _set_record_flag(octo, args, REC_FLAG_BOOST, "start boost")


def cmd_link(octo, args):
    """Make a channel follow another channel's output."""
    if args.target == args.channel:
        sys.exit("A channel cannot follow itself.")
    before = octo.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    after[base + OFF_MODE] = args.target + LINK_OFFSET
    reseal(after)
    print("channel %d: %s -> link->ch%d"
          % (args.channel, mode_name(before[base + OFF_MODE]), args.target))
    print("      (Aquasuite also copies the source channel's power window onto the")
    print("       follower when you do this in the GUI; octoctl only sets the mode.)")
    commit(octo, before, after, args)


def cmd_holdmin(octo, args):
    _set_record_flag(octo, args, REC_FLAG_HOLD_MIN, "hold minimum power")


def cmd_maxrpm(octo, args):
    """The full-scale value Aquasuite uses for this channel's bar graph."""
    before = octo.read()
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
    commit(octo, before, after, args)


def cmd_target(octo, args):
    before = octo.read()
    if not 0 <= args.celsius <= 100:
        sys.exit("Target temperature must be 0-100 C.")
    after = bytearray(before)
    base = channel_base(args.channel)
    put_be16(after, base + OFF_TARGET, int(round(args.celsius * 100)))
    reseal(after)
    print("channel %d target: %.2fC -> %.2fC"
          % (args.channel, be16(before, base + OFF_TARGET) / 100.0, args.celsius))
    commit(octo, before, after, args)


def cmd_fallback(octo, args):
    before = octo.read()
    after = bytearray(before)
    rec = record_base(args.channel)
    put_be16(after, rec + REC_FALLBACK, to_raw_pct(args.percent))
    reseal(after)
    print("channel %d fallback: %.2f%% -> %.2f%%"
          % (args.channel, pct(be16(before, rec + REC_FALLBACK)), args.percent))
    commit(octo, before, after, args)


def cmd_fansource(octo, args):
    """Choose which temperature sensor drives a channel's controller."""
    before = octo.read()
    base = channel_base(args.channel)
    if args.sensor is None:
        print("channel %d source: sensor %d"
              % (args.channel, be16(before, base + OFF_SOURCE) + 1))
        return
    if not 1 <= args.sensor <= 4:
        print("Note: only temperature sensors 1-4 are confirmed; higher indices")
        print("      may address flow or software sensors but are untested.")
    after = bytearray(before)
    put_be16(after, base + OFF_SOURCE, args.sensor - 1)
    reseal(after)
    print("channel %d source: sensor %d -> sensor %d"
          % (args.channel, be16(before, base + OFF_SOURCE) + 1, args.sensor))
    commit(octo, before, after, args)


def cmd_flow(octo, args):
    before = octo.read()
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
    commit(octo, before, after, args)


def cmd_offset(octo, args):
    """Read or set a temperature sensor's calibration offset."""
    before = octo.read()
    if args.celsius is None:
        for i in range(4):
            print("sensor %d: %+.2f C" % (i + 1, sbe16(before, TEMP_OFFSETS + 2 * i) / 100.0))
        return
    # the official software and the kernel driver both cap this at +/-15 K
    if not -15.0 <= args.celsius <= 15.0:
        sys.exit("Offset must be within +/-15.00 C.")
    after = bytearray(before)
    struct.pack_into(">h", after, TEMP_OFFSETS + 2 * (args.sensor - 1),
                     int(round(args.celsius * 100)))
    reseal(after)
    commit(octo, before, after, args)


def cmd_mode(octo, args):
    before = octo.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    after[base + OFF_MODE] = MODE_BY_NAME[args.mode]
    reseal(after)
    commit(octo, before, after, args)


UDEV_HELP = """\
Install a udev rule so root is not needed:

  echo 'KERNEL=="hidraw*", ATTRS{idVendor}=="0c70", ATTRS{idProduct}=="f011", \\
MODE="0660", GROUP="wheel"' | sudo tee /etc/udev/rules.d/99-aquacomputer.rules
  sudo udevadm control --reload && sudo udevadm trigger

Replace wheel with a group you are in.
"""


def main():
    ap = argparse.ArgumentParser(
        description="Read and modify the Aquacomputer Octo control report.",
        epilog="Every write is preceded by an automatic backup into "
               "<your home>/.local/share/octoctl/ (correct under sudo).")
    ap.add_argument("--help-udev", action="store_true", help="show the udev rule and exit")
    sub = ap.add_subparsers(dest="cmd")

    def writer(p):
        p.add_argument("-n", "--dry-run", action="store_true",
                       help="show the byte diff, write nothing")
        p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
        p.add_argument("--backup", help="path for the pre-write backup")
        p.add_argument("--force-pump", action="store_true",
                       help="allow operating on channels 1-2")
        return p

    sub.add_parser("show", help="decode and print the current configuration")

    p = sub.add_parser("dump", help="save the raw 1631-byte control report")
    p.add_argument("-o", "--output", help="output file")

    p = writer(sub.add_parser("restore", help="write a saved control report back"))
    p.add_argument("file")

    p = writer(sub.add_parser("set", help="manual mode + fixed speed on a channel"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("percent", type=float)

    p = writer(sub.add_parser(
        "pin", help="hold a channel at a fixed %% while staying under its controller"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("percent", type=float)

    p = writer(sub.add_parser("window", help="set a channel's min/max power window"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("min", type=float)
    p.add_argument("max", type=float)

    p = writer(sub.add_parser("rgb", help="read or set an RGBpx controller colour"))
    p.add_argument("index", type=int, nargs="?", default=None)
    p.add_argument("colour", nargs="?", default=None, help="RRGGBB, e.g. FF0000")
    p.add_argument("--entry", type=int, default=0,
                   help="palette entry: 0 background, 1 effect colour")
    p.add_argument("--param", action="append",
                   help="K=VALUE or NAME=VALUE, e.g. speed=30 (repeatable)")
    p.add_argument("--mode", help="effect name or id, e.g. wave / 0x0a")
    p.add_argument("--power", choices=["on", "off"],
                   help="turn the whole RGBpx function on or off")
    p.add_argument("--brightness", type=float, default=None,
                   help="global RGBpx brightness, 0-100%%")
    p.add_argument("--source",
                   help="data source: none, flow, or sensor number 1-4")
    p.add_argument("--filter-rise", type=int, dest="filter_rise",
                   help="filtering of fluctuating values, rising (0-255)")
    p.add_argument("--filter-fall", type=int, dest="filter_fall",
                   help="filtering of fluctuating values, falling (0-255)")
    p.add_argument("--flag", action="append", help="turn a flag on, e.g. --flag fade")
    p.add_argument("--no-flag", action="append", dest="no_flag",
                   help="turn a flag off")

    sub.add_parser("labels", help="list every name stored on the device")

    p = writer(sub.add_parser("label", help="read or set one stored name"))
    p.add_argument("group", choices=sorted(LABEL_GROUPS))
    p.add_argument("index", type=int)
    p.add_argument("text", nargs="?", default=None)

    p = writer(sub.add_parser("offset", help="read or set sensor calibration offsets"))
    p.add_argument("sensor", type=int, nargs="?", default=1, choices=range(1, 5))
    p.add_argument("celsius", type=float, nargs="?", default=None)

    p = writer(sub.add_parser("mode", help="switch a channel's control mode"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("mode", choices=["manual", "target", "curve"])

    p = writer(sub.add_parser("curve", help="read or set a channel's 16-point curve"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("--linear", nargs=4, type=float,
                   metavar=("TMIN", "TMAX", "PMIN", "PMAX"),
                   help="fill 16 evenly spaced points on a straight line")
    p.add_argument("--points", help="16 explicit TEMP:POWER pairs, comma separated")

    p = writer(sub.add_parser("target", help="set a channel's target temperature"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("celsius", type=float)

    p = writer(sub.add_parser("fallback", help="set a channel's fallback power"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("percent", type=float)

    p = writer(sub.add_parser("pid", help="read or set controller tuning"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    for flag in ("p", "i", "d"):
        p.add_argument("--" + flag, type=float, default=None)
    p.add_argument("--reset", type=float, default=None)
    p.add_argument("--hysteresis", type=float, default=None, help="in kelvin")

    for name, helptext in (("boost", "turn start boost on or off"),
                           ("holdmin", "hold minimum power instead of stopping")):
        q = writer(sub.add_parser(name, help=helptext))
        q.add_argument("channel", type=int, choices=range(1, 9))
        q.add_argument("state", choices=["on", "off"])

    p = writer(sub.add_parser("link", help="make a channel follow another channel"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("target", type=int, choices=range(1, 9))

    p = writer(sub.add_parser("maxrpm", help="chart full-scale rpm for a channel"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("rpm", type=int, nargs="?", default=None)

    p = writer(sub.add_parser("fansource",
                              help="which sensor drives a channel's controller"))
    p.add_argument("channel", type=int, choices=range(1, 9))
    p.add_argument("sensor", type=int, nargs="?", default=None,
                   help="1-based sensor number")

    p = writer(sub.add_parser("flow", help="read or set flow calibration (impulses/litre)"))
    p.add_argument("impulses", type=int, nargs="?", default=None)

    args = ap.parse_args()
    if args.help_udev:
        print(UDEV_HELP)
        return
    if not args.cmd:
        ap.print_help()
        return

    for name in ("percent", "min", "max"):
        val = getattr(args, name, None)
        if val is not None and not 0.0 <= val <= 100.0:
            sys.exit("%s must be between 0 and 100." % name)

    if (args.cmd not in ("offset", "label", "rgb")
            and getattr(args, "channel", None) is not None):
        guard_pump(args.channel, args.force_pump)

    handler = {"show": cmd_show, "dump": cmd_dump, "restore": cmd_restore,
               "set": cmd_set, "pin": cmd_pin, "window": cmd_window,
               "offset": cmd_offset, "mode": cmd_mode, "curve": cmd_curve,
               "target": cmd_target, "fallback": cmd_fallback,
               "pid": cmd_pid, "boost": cmd_boost, "flow": cmd_flow,
               "holdmin": cmd_holdmin, "maxrpm": cmd_maxrpm, "link": cmd_link,
               "fansource": cmd_fansource,
               "labels": cmd_labels, "label": cmd_label, "rgb": cmd_rgb}[args.cmd]
    with Octo.find() as octo:
        handler(octo, args)


if __name__ == "__main__":
    main()
