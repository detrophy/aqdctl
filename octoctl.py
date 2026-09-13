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
import errno
import json
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
# Report 0x02 is a command frame: byte 4 is the command, bytes 9-10 a
# CRC-16/USB over bytes 1-8, stored big-endian like every other checksum here.
# Command 0x02 follows every settings write. USB captures of Aquasuite show it
# sent to the Octo as an *output* report (as the descriptor declares it), 3.8 s
# after the last settings write. On the high flow NEXT, which uses the same
# frame, it also never follows a name write.
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
# LEDs addressable on one RGBpx header, per the manual. Both headers count from
# their own LED 1. (The Farbwerk 360 has the same limit.)
RGB_MAX_LED = 90
# The two RGBpx headers. The 12 controllers above are a pool shared between them,
# not six each: every slot carries its own port byte.
RGB_CHANNELS = 2
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


def source_short(index):
    """The same thing as source_label, in the form you would type after
    --sensor. Used where a table column has to stay narrow."""
    if index == SOURCE_FLOW:
        return "flow"
    if 0 <= index < 4:
        return "%d" % (index + 1)
    return "#%d" % index
# Parameters are BE16 words at +22+2k. Values below 256 decode identically
# under a LE16-at-+23 reading, so only an effect with a large value (colour
# gradient, limits up to 1000) distinguishes them - that capture settled it.
RGB_PARAM = 22
RGB_COLOUR = 46        # 6 palette entries of 4 bytes
RGB_PALETTE_ENTRIES = 6
HUE_FULL = 1536

# Effect ids and parameter names come from the Farbwerk 360 (AquaControl's
# PROTOCOL_EFFECTS.md). Verified on this Octo for 0x01 static and 0x0A wave;
# the rest are carried over on the strength of that match, not observed here.
RGB_MODES = {0x01: "static", 0x02: "breathing", 0x03: "rotating rainbow",
             0x04: "blinking", 0x05: "colour change", 0x07: "sequence",
             0x08: "scanner", 0x09: "laser", 0x0A: "wave",
             0x0B: "colour sequence", 0x0C: "colour shift", 0x0D: "bar graph",
             0x0E: "flame", 0x0F: "rain", 0x10: "snowfall", 0x11: "stardust",
             0x12: "colour switch", 0x13: "swiping rainbow",
             0x14: "sound flash", 0x15: "sound bars", 0x16: "sound slider",
             0x17: "sound shift", 0x18: "ambientpx",
             0x21: "colour gradient"}
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
    0x03: ["speed", "colour_range"],
    0x04: ["speed", "count"],
    0x05: ["speed", "count"],
    0x07: ["speed", "smoothness", "count", "delay_after", "delay_before"],
    0x0B: ["speed", "smoothness", "", "count", "colour_change_speed"],
    0x0C: ["speed", "colour_range", "total_area"],
    0x13: ["point_speed", "point_smoothness", "point_size",
           "colour_change_speed", "colour_range"],
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
    0x04: {"fade": 0x04, "random_colour": 0x08, "slide_colours": 0x10},
    0x05: {"fade": 0x04, "random_colour": 0x08, "slide_colours": 0x10},
    # colour_mode2 swaps the symmetric [bg, c2, c1, c2, bg] dot for a plain
    # 50/50 split [bg, c1, c2, bg]. Confirmed: it is the only byte that differs
    # between an otherwise identical mode-1 and mode-2 scanner or laser.
    0x08: {"reverse": 0x02, "random_colour": 0x08, "colour_mode2": 0x20,
           "colour_change": 0x40, "circular": 0x80},
    0x09: {"reverse": 0x02, "random_colour": 0x08, "colour_mode2": 0x20,
           "colour_change": 0x40, "circular": 0x80},
    0x0A: {"reverse": 0x02, "random_colour": 0x08, "circular": 0x80},
    0x0F: {"reverse": 0x02, "random_colour": 0x08, "snow": 0x10},
}
RGB_FLAG_NAMES[0x10] = RGB_FLAG_NAMES[0x11] = RGB_FLAG_NAMES[0x0F]
RGB_FLAG_NAMES[0x02] = {}
RGB_FLAG_NAMES[0x03] = {"reverse": 0x02}
RGB_FLAG_NAMES[0x0C] = {"reverse": 0x02}
RGB_FLAG_NAMES[0x13] = {"reverse": 0x01}
RGB_FLAG_NAMES[0x07] = {"reverse": 0x02, "fade": 0x04, "random_colour": 0x08}
RGB_FLAG_NAMES[0x0B] = {"reverse": 0x02, "random_colour": 0x08}
# Which palette entries an effect uses, and what they mean.
RGB_PALETTE_ROLES = {
    0x01: ["colour"],
    0x05: ["colour list"],            # entries 0..count-1, no background
    0x08: ["background", "colour 1", "colour 2"],
    0x09: ["background", "colour 1", "colour 2"],
    0x0A: ["background", "colour"],
    0x0F: ["background", "colour"],
}

# Palette SHAPE per effect: (has_background, min_colours, max_colours).
#
# Entries are used contiguously from the start. Where an effect has a background
# it occupies entry 0 and the colours follow. Effects with a 'count' parameter
# take a variable-length list and 'count' is derived from its length - never
# typed by hand. Entries past 'count' seen in some captures are leftovers that
# the official software does not clear.
#
# Confirmed on hardware: wave with count=1 fills 2 entries and with count=5 fills
# 6; colour sequence, which has no background, fills exactly 'count'.
RGB_PALETTE_SPEC = {
    0x01: (False, 1, 1),      # static
    0x02: (False, 1, 1),      # breathing
    0x04: (True, 1, 5),       # blinking
    0x05: (False, 1, 6),      # colour change
    0x07: (True, 1, 5),       # sequence
    0x08: (True, 2, 2),       # scanner   - background + colour 1 + colour 2
    0x09: (True, 2, 2),       # laser
    0x0A: (True, 1, 5),       # wave
    0x0B: (False, 1, 6),      # colour sequence
    0x0F: (True, 1, 1),       # rain
    0x10: (True, 1, 1),       # snowfall
    0x11: (True, 1, 1),       # stardust
}
# Effects absent from the table take no user-settable colours: the rainbow family
# generates its own, and the audio/ambient ones are driven from the host.

# The 'count' parameter of a variable-length effect is the length of its colour
# list, so the CLI derives it and never exposes it as a settable parameter.
RGB_COUNT_PARAM = "count"

# CLI name -> label-report group. The report's own keys are internal shorthand;
# these are the words used everywhere the user can see, matching the vocabulary
# of the rest of the tool (a sensor is a sensor, not a "temp").
NAME_GROUPS = {"fan": "fan", "controller": "led", "sensor": "temp",
               "flow": "flow", "virtual": "soft"}

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


def config_path():
    user = _invoking_user()
    home = user.pw_dir if user else os.path.expanduser("~")
    return os.path.join(home, ".config", "octoctl", "config.json")


def load_config():
    try:
        with open(config_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = config_path()
    directory = os.path.dirname(path)
    fresh = not os.path.isdir(directory)
    os.makedirs(directory, exist_ok=True)
    if fresh:
        _give_back(directory)
    with _open_nofollow(path, "w") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    _give_back(path)
    return path


def protected_channels():
    """Fan channels the user has asked to be protected from accidental writes.

    Not a device setting - the Octo has no such field - so it lives in the local
    config. The point is that a pump is a fan header like any other, and a typo
    that stops one is not recoverable by undoing the command."""
    raw = load_config().get("protected", [])
    return {int(c) for c in raw if str(c).isdigit() or isinstance(c, int)}


def guard_channel(channel, force):
    if channel in protected_channels() and not force:
        sys.exit("Channel %d is protected. Re-run with --force if you mean it, or\n"
                 "lift the protection with 'octoctl fan set %d protect off'."
                 % (channel, channel))


def _give_back(path):
    """Hand a file created under sudo to the invoking user."""
    user = _invoking_user()
    if user:
        try:
            # lchown, not chown: if the path is a symlink we hand over the link
            # itself rather than whatever it points at.
            os.lchown(path, user.pw_uid, user.pw_gid)
        except OSError:
            pass


def _open_nofollow(path, mode):
    """Open a file for writing, refusing a symlink at the final component.

    The config and the backups live in the invoking user's home but are written
    while root. Without this, a symlink left in place of one of them has root
    truncate - and _give_back chown - whatever it points at. Only the last
    component is covered; a symlinked parent directory is still followed."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     0o644)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            sys.exit("%s is a symlink; refusing to write through it." % path)
        raise
    return os.fdopen(fd, mode)


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
    # Masking here would turn a negative percentage into a plausible 600% rather
    # than an error, so out-of-range is a bug to raise on, not to round off.
    if not 0 <= val <= 0xFFFF:
        raise ValueError("BE16 value %r out of range at offset %#05x" % (val, off))
    struct.pack_into(">H", buf, off, val)


def pct(raw):
    return raw / 100.0


def to_raw_pct(percent):
    return int(round(percent * 100))


def require_pct(value, label="percent"):
    """Bound a percentage inside the handler, not only in main().

    main() screens the CLI, but the handlers are also called directly - by the
    test suite, and by anything that imports this module - where nothing else
    stands between a bad value and the device."""
    if value is None or not 0.0 <= value <= 100.0:
        sys.exit("%s must be between 0 and 100." % label)
    return value


def require_window(low, high, channel=None):
    """Refuse an inverted power window.

    Both ends can be valid percentages while the pair is not: a minimum above
    the maximum is not how you pin a channel - equal limits are."""
    if low is None or high is None or low <= high:
        return
    where = " %d" % channel if channel is not None else ""
    sys.exit("Minimum power (%.2f%%) is above the maximum (%.2f%%).\n"
             "For a fixed point use equal limits: 'fan set%s limits %g %g'."
             % (low, high, where, low, low))


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
        """Send a resealed report. After a settings report, also send the
        command Aquasuite sends after every settings change; after a name
        report it sends nothing, and neither does this."""
        stored, computed = report_crc(buf)
        if stored != computed:
            sys.exit("Refusing to send a report with a stale checksum (internal error).")
        self._pace()
        if self.dev.send_feature_report(bytes(buf)) < 0:
            sys.exit("send_feature_report failed.")
        self._last_op = time.monotonic()
        if buf[0] != CTRL_REPORT_ID:
            return
        # An output report, as declared and as Aquasuite sends it. The HID
        # interface has no interrupt OUT endpoint, so hidraw delivers this as a
        # SET_REPORT(output) control transfer - what the capture shows. The
        # result is checked: python-hidapi reports failure by returning -1, not
        # by raising.
        self._pace()
        if self.dev.write(SECONDARY_CTRL_REPORT) < 0:
            sys.exit("The settings report was written, but report 0x02, which "
                     "Aquasuite sends after\nevery settings change, failed. The "
                     "change is active; it may not survive a power\ncycle. Run "
                     "the command again to retry.")
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


def read_hwmon():
    """Live readings from the kernel driver: {"fan": {1: rpm}, "temp": {1: degC},
    "flow": litres/hour}. Empty if aquacomputer_d5next is not loaded.

    This is a plain sysfs read, so it needs no root and no USB traffic - the
    report tells you what the Octo is CONFIGURED to do, this tells you what it is
    ACTUALLY doing, and having both side by side is usually the question."""
    import glob
    path = None
    for name_file in sorted(glob.glob("/sys/class/hwmon/hwmon*/name")):
        try:
            with open(name_file) as fh:
                if "octo" in fh.read().strip().lower():
                    path = os.path.dirname(name_file)
                    break
        except OSError:
            continue
    if path is None:
        return {}

    def read(leaf, scale=1.0):
        try:
            with open(os.path.join(path, leaf)) as fh:
                return int(fh.read().strip()) / scale
        except (OSError, ValueError):
            return None

    out = {"fan": {}, "temp": {}, "flow": None}
    for ch in range(1, 9):
        out["fan"][ch] = read("fan%d_input" % ch)
    for s in range(1, 5):
        out["temp"][s] = read("temp%d_input" % s, 1000.0)
    # fan9 is the flow sensor, reported in decilitres per hour.
    out["flow"] = read("fan9_input", 10.0)
    return out


def pid_preset_name(values):
    """Name of the preset matching these five numbers, or None. The device stores
    no preset id, so this is a lookup by value - see PID_PRESETS."""
    for name, row in PID_PRESETS.items():
        if all(abs(a - b) < 1e-6 for a, b in zip(values, row)):
            return name
    return None


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


def info_rgb(buf, names):
    print("RGB  (2 headers, 12 controllers shared between them)")
    print("  function: %s     global brightness: %.0f%% (%d/255)"
          % ("on" if buf[RGB_ENABLE] == RGB_ENABLE_ON else "off",
             buf[RGB_BRIGHTNESS] * 100.0 / 255, buf[RGB_BRIGHTNESS]))
    used = False
    for i in range(1, RGB_COUNT + 1):
        if buf[RGB_BASE + RGB_STRIDE * (i - 1) + RGB_MODE] == 0:
            continue
        used = True
        describe_rgb(buf, i, names[i - 1])
    if not used:
        print("  no controllers configured")
    print()
    print("  A controller is a config slot, not a header: it says \"on channel N,")
    print("  LEDs A-B, run effect X\". Address one with its channel and position,")
    print("  e.g. 'rgb set 2 pos 61-79'. The slot number is shown so it lines up")
    print("  with Aquasuite's \"LED Controller n\" and with 'name controller n'.")


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
    with _open_nofollow(path, "wb") as fh:
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
    return RGB_BASE + RGB_STRIDE * (index - 1) + RGB_COLOUR


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
    changes = diff(before, after)
    if not changes:
        print("Nothing to change.")
        return
    # The byte diff is for checking the tool, not for using it: on by request,
    # and always on for a dry run, where seeing the bytes is the whole point.
    if getattr(args, "verbose", False) or args.dry_run:
        show_diff(before, after)
    if args.dry_run:
        print("--dry-run: nothing written.")
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

def cmd_info(octo, args):
    """Everything the device is configured to do, plus what it is doing now."""
    buf = octo.read()
    try:
        labels = decode_labels(octo.read(LABEL_REPORT_ID))
    except Exception:
        labels = {g: [""] * n for g, (_, n) in LABEL_GROUPS.items()}
    live = read_hwmon()

    want = getattr(args, "section", None)
    sections = [want] if want else ["fan", "rgb", "sensor"]

    print("Octo   active profile %d%s"
          % (buf[PROFILE_INDEX], "" if live else "   (kernel driver not loaded)"))
    for i, section in enumerate(sections):
        print()
        if section == "fan":
            info_fan(buf, labels["fan"], live)
        elif section == "rgb":
            info_rgb(buf, labels["led"])
        elif section == "sensor":
            info_sensor(buf, labels["temp"], live)


def cmd_backup(octo, args):
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
    # A restore rewrites the whole report, so it reaches protected channels too.
    # There is no per-channel version of this, hence the blanket refusal.
    guarded = protected_channels()
    if guarded and rid == CTRL_REPORT_ID and not getattr(args, "force", False):
        sys.exit("Restoring rewrites every channel, including protected channel%s "
                 "%s.\nRe-run with --force if that is what you want."
                 % ("" if len(guarded) == 1 else "s",
                    ", ".join(str(c) for c in sorted(guarded))))
    before = octo.read(rid)
    commit(octo, before, saved, args)


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


def cmd_mode_fixed(octo, args):
    """Fixed power: the channel stops regulating and holds the given percentage."""
    before = octo.read()
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
    commit(octo, before, after, args)


def cmd_limits(octo, args):
    """Set a channel's minimum and maximum power window."""
    require_pct(args.min, "min")
    require_pct(args.max, "max")
    require_window(args.min, args.max, args.channel)
    before = octo.read()
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
    commit(octo, before, after, args)


def get_param(buf, base, k):
    o = base + RGB_PARAM + 2 * k
    return (buf[o] << 8) | buf[o + 1]


def set_param(buf, base, k, value):
    o = base + RGB_PARAM + 2 * k
    buf[o] = (value >> 8) & 0xFF
    buf[o + 1] = value & 0xFF


def rgb_entry(index, entry=0):
    return RGB_BASE + RGB_STRIDE * (index - 1) + RGB_COLOUR + 4 * entry


def read_entry(buf, index, entry=0):
    o = rgb_entry(index, entry)
    return (buf[o] << 8) | buf[o + 1], buf[o + 2], buf[o + 3]


def palette_roles(mode):
    """Role of each palette entry, in order, for an effect. Derived from
    RGB_PALETTE_SPEC so it says the same thing as 'rgb effects' and as the
    validation in 'rgb set'."""
    has_bg, _lo, hi = RGB_PALETTE_SPEC.get(mode, (False, 0, 0))
    roles = ["background"] if has_bg else []
    # A single-colour effect just has "colour"; numbering one thing is noise.
    if hi == 1:
        return roles + ["colour"]
    return roles + ["colour %d" % (n + 1) for n in range(hi)]


def describe_rgb(buf, index, name):
    """One controller, in the vocabulary the commands use: channel, position,
    effect, and colours by role. Positions are 1-based here and in --pos."""
    base = RGB_BASE + RGB_STRIDE * (index - 1)
    mode = buf[base + RGB_MODE]
    effect = RGB_MODES.get(mode)
    tag = effect if effect else ("unimplemented"
                                 if mode in RGB_MODES_UNIMPLEMENTED else "unknown")
    if effect and mode not in RGB_MODES_VERIFIED:
        tag += "?"
    if mode in RGB_MODES_HOST_DRIVEN:
        tag += "  [needs the official software running]"
    first = buf[base + RGB_START] + 1
    count = buf[base + RGB_COUNT_OFF]
    print("  %2d  %-18s channel %d, LEDs %d-%d   effect %s"
          % (index, name, buf[base + RGB_PORT] + 1, first, first + count - 1, tag))

    # 'count' is the length of the colour list, shown with the colours instead.
    labels = RGB_PARAM_NAMES.get(mode, [])
    shown = []
    for k in range(9):
        value = get_param(buf, base, k)
        label = labels[k] if k < len(labels) else ""
        if label == RGB_COUNT_PARAM:
            continue
        if label:
            shown.append("%s=%d" % (label, value))
        elif value:
            shown.append("unnamed%d=%d" % (k, value))
    if shown:
        print("      params: " + "  ".join(shown))

    flags = buf[base + RGB_FLAGS_OFFSET]
    known = RGB_FLAG_NAMES.get(mode, {})
    on = [n for n, bit in sorted(known.items()) if flags & bit]
    leftover = flags & ~sum(known.values()) if known else flags
    if on or leftover:
        extra = "   unknown bits %#04x" % leftover if leftover else ""
        print("      flags : %s%s" % (", ".join(on) or "none", extra))

    src = be16(buf, base + RGB_SOURCE)
    if src != RGB_SOURCE_NONE:
        print("      sensor: %s   filter rise=%d fall=%d"
              % (source_label(src), buf[base + RGB_FILTER_RISE],
                 buf[base + RGB_FILTER_FALL]))
        sflags = buf[base + RGB_SOURCE_FLAGS]
        on = [n for n, bit in sorted(RGB_SOURCE_FLAG_NAMES.items()) if sflags & bit]
        rest = sflags & ~sum(RGB_SOURCE_FLAG_NAMES.values())
        if sflags:
            print("      sensor drives: %s%s"
                  % (", ".join(on) or "none",
                     "   unknown bits %#04x" % rest if rest else ""))
        for tag_, (lo, hi, omin, omax) in zip(("speed ", "bright"), RGB_MAPS):
            print("      maps %s: %d .. %d  ->  %d .. %d"
                  % (tag_, be16(buf, base + lo), be16(buf, base + hi),
                     buf[base + omin], buf[base + omax]))

    roles = palette_roles(mode)
    for e in range(RGB_PALETTE_ENTRIES):
        h, sat, val = read_entry(buf, index, e)
        if not (h or sat or val):
            continue
        role = roles[e] if e < len(roles) else "stored, unused by this effect"
        print("      %-12s #%02X%02X%02X" % (role + ":", *hsv_to_rgb(h, sat, val)))


# A controller slot as the device leaves it when nothing is configured: mode 0,
# one LED, no data source, filters at 10/15, both mapping blocks neutral.
# Taken from the unused slots of a real capture, so removing a controller leaves
# exactly what the device would have had there anyway.
RGB_SLOT_EMPTY = bytes.fromhex(
    "000001000000ffff0a0f00000064006400000064006400" + "00" * 47)


def parse_position(text):
    """'61-79' -> (start, count) with start 1-based inclusive, as shown by the
    official software. The report stores start 0-based; the caller converts."""
    part = str(text).split("-")
    if len(part) != 2:
        sys.exit("Position is FIRST-LAST, e.g. --pos 1-15 for the first 15 LEDs.")
    try:
        first, last = int(part[0]), int(part[1])
    except ValueError:
        sys.exit("Position is FIRST-LAST, e.g. --pos 1-15 for the first 15 LEDs.")
    if first < 1:
        sys.exit("LED positions start at 1.")
    if last < first:
        sys.exit("Position %s ends before it starts." % text)
    if last > RGB_MAX_LED:
        sys.exit("The last LED on a channel is %d." % RGB_MAX_LED)
    return first, last - first + 1


def controller_span(buf, index):
    """(channel, first_led, last_led) of a slot, all 1-based, or None if unused."""
    base = RGB_BASE + RGB_STRIDE * (index - 1)
    if buf[base + RGB_MODE] == 0:
        return None
    start = buf[base + RGB_START] + 1
    return (buf[base + RGB_PORT] + 1, start, start + buf[base + RGB_COUNT_OFF] - 1)


def resolve_controller(buf, channel, first, count):
    """Which slot a channel+position refers to.

    Exact match wins, so editing an existing controller is the common case. A
    different controller overlapping the range is refused rather than silently
    layered - overlapping controllers on one channel is how LEDs end up
    flickering between two effects. Otherwise the lowest free slot is taken."""
    last = first + count - 1
    free = None
    for i in range(1, RGB_COUNT + 1):
        span = controller_span(buf, i)
        if span is None:
            if free is None:
                free = i
            continue
        ch, lo, hi = span
        if ch != channel:
            continue
        if lo == first and hi == last:
            return i, False
        if lo <= last and first <= hi:
            sys.exit("Controller %d already covers channel %d LEDs %d-%d, which "
                     "overlaps %d-%d.\nEdit it with '--pos %d-%d', or remove it "
                     "first with 'rgb remove %d pos %d-%d'."
                     % (i, ch, lo, hi, first, last, lo, hi, ch, lo, hi))
    if free is None:
        sys.exit("All %d controllers are in use. Free one with 'rgb remove'."
                 % RGB_COUNT)
    return free, True


def cmd_rgb_switch(octo, args):
    """Turn the whole RGB function on or off."""
    buf = octo.read()
    after = bytearray(buf)
    after[RGB_ENABLE] = RGB_ENABLE_ON if args.state == "on" else RGB_ENABLE_OFF
    reseal(after)
    print("RGB function: %s -> %s"
          % ("on" if buf[RGB_ENABLE] == RGB_ENABLE_ON else "off", args.state))
    commit(octo, buf, after, args)


def cmd_rgb_brightness(octo, args):
    """Global brightness for every controller on both channels."""
    buf = octo.read()
    if not 0 <= args.percent <= 100:
        sys.exit("Brightness is a percentage, 0-100.")
    # Aquasuite's slider moves one byte per step (captured on both the Octo and
    # the high flow NEXT) and shows a whole percentage, so one shown value
    # covers several bytes: its "45" stored 114 on the Octo and 115 on the high
    # flow NEXT. There is no conversion rule to copy; store the byte nearest the
    # requested percentage.
    raw = int(args.percent * 255 / 100.0 + 0.5)
    after = bytearray(buf)
    after[RGB_BRIGHTNESS] = raw
    reseal(after)
    print("global brightness: %.0f%% (%d) -> %.0f%% (%d)"
          % (buf[RGB_BRIGHTNESS] * 100.0 / 255, buf[RGB_BRIGHTNESS],
             raw * 100.0 / 255, raw))
    commit(octo, buf, after, args)


def cmd_rgb_remove(octo, args):
    """Clear one controller, or every controller on a channel."""
    buf = octo.read()
    after = bytearray(buf)
    targets = []
    for i in range(1, RGB_COUNT + 1):
        span = controller_span(buf, i)
        if span is None or span[0] != args.channel:
            continue
        if args.pos is None:
            targets.append(i)
        else:
            first, count = parse_position(args.pos)
            if span[1] == first and span[2] == first + count - 1:
                targets.append(i)
    if not targets:
        sys.exit("Nothing to remove on channel %d%s. 'info rgb' lists what is set."
                 % (args.channel, "" if args.pos is None else " at %s" % args.pos))
    for i in targets:
        base = RGB_BASE + RGB_STRIDE * (i - 1)
        span = controller_span(buf, i)
        print("removing controller %d (channel %d, LEDs %d-%d)"
              % (i, span[0], span[1], span[2]))
        after[base:base + RGB_STRIDE] = RGB_SLOT_EMPTY
    reseal(after)
    commit(octo, buf, after, args)


def cmd_rgb_set(octo, args):
    buf = octo.read()
    first, count = parse_position(args.pos)
    index, is_new = resolve_controller(buf, args.channel, first, count)
    print("controller %d (%s) on channel %d, LEDs %d-%d"
          % (index, "new" if is_new else "existing", args.channel,
             first, first + count - 1))

    base = RGB_BASE + RGB_STRIDE * (index - 1)
    after = bytearray(buf)
    if is_new:
        after[base:base + RGB_STRIDE] = RGB_SLOT_EMPTY
        if args.effect is None:
            sys.exit("A new controller needs an effect: add --effect NAME. "
                     "'rgb effects' lists them.")
    after[base + RGB_PORT] = args.channel - 1
    after[base + RGB_START] = first - 1
    after[base + RGB_COUNT_OFF] = count

    # Apply the effect first: parameter, flag and colour names are per-effect, so
    # "--effect wave --flag reverse" in one go must validate against wave.
    if args.effect is not None:
        lookup = {v.replace(" ", "_"): k for k, v in RGB_MODES.items()}
        mode = lookup.get(args.effect.lower().replace(" ", "_"))
        if mode is None:
            try:
                mode = int(args.effect, 0)
            except ValueError:
                sys.exit("Unknown effect %r. Run 'octoctl rgb effects' for the list."
                         % args.effect)
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
        print("  effect: %s -> %s"
              % (RGB_MODES.get(buf[base + RGB_MODE], "none"),
                 RGB_MODES.get(mode, "%#04x" % mode)))

    mode = after[base + RGB_MODE]

    if args.sensor is not None:
        if str(args.sensor).lower() in ("none", "off"):
            value = RGB_SOURCE_NONE
        else:
            value = parse_sensor(args.sensor)
        old = be16(after, base + RGB_SOURCE)
        put_be16(after, base + RGB_SOURCE, value)

        def _name(v):
            return "none" if v == RGB_SOURCE_NONE else source_label(v)
        print("  sensor: %s -> %s" % (_name(old), _name(value)))
        if value != RGB_SOURCE_NONE and old == RGB_SOURCE_NONE:
            print("    Aquasuite also rewrites both mapping ranges to 20-70 here;")
            print("    octoctl leaves them alone - 'info rgb' shows the current ones.")
    for value, off, label in ((args.filter_rise, RGB_FILTER_RISE, "rise"),
                              (args.filter_fall, RGB_FILTER_FALL, "fall")):
        if value is None:
            continue
        if not 0 <= value <= 255:
            sys.exit("Filter values are a single byte (0-255).")
        print("  filter %s: %d -> %d" % (label, after[base + off], value))
        after[base + off] = value

    if args.colour is not None or args.background is not None:
        has_bg, lo, hi = RGB_PALETTE_SPEC.get(mode, (False, 0, 0))
        effect = RGB_MODES.get(mode, "%#04x" % mode)
        if hi == 0:
            sys.exit("Effect '%s' takes no colours of its own. "
                     "'rgb effects %s' explains what it does accept."
                     % (effect, effect.replace(" ", "_")))
        if args.background is not None and not has_bg:
            sys.exit("Effect '%s' has no background colour." % effect)

        colours = []
        if args.colour is not None:
            for text in args.colour.split(","):
                colours.append(parse_hex_colour(text.strip()))
            if not lo <= len(colours) <= hi:
                sys.exit("Effect '%s' takes %s colour%s, got %d."
                         % (effect, lo if lo == hi else "%d-%d" % (lo, hi),
                            "" if hi == 1 else "s", len(colours)))

        entry = 0
        if has_bg:
            if args.background is not None:
                r, g, b = parse_hex_colour(args.background)
                h, sat, val = rgb_to_hsv(r, g, b)
                o = rgb_entry(index, 0)
                after[o], after[o + 1], after[o + 2], after[o + 3] = \
                    (h >> 8) & 0xFF, h & 0xFF, sat, val
                print("  background: #%02X%02X%02X" % (r, g, b))
            entry = 1
        for n, (r, g, b) in enumerate(colours):
            h, sat, val = rgb_to_hsv(r, g, b)
            o = rgb_entry(index, entry + n)
            after[o], after[o + 1], after[o + 2], after[o + 3] = \
                (h >> 8) & 0xFF, h & 0xFF, sat, val
            print("  colour %d: #%02X%02X%02X" % (n + 1, r, g, b))
        if colours:
            # Clear the entries past the list: the official software leaves stale
            # colours behind, and showing them back would be a lie.
            for e in range(entry + len(colours), RGB_PALETTE_ENTRIES):
                o = rgb_entry(index, e)
                after[o:o + 4] = b"\x00\x00\x00\x00"
            # 'count' is the length of the list, never typed by hand.
            names = RGB_PARAM_NAMES.get(mode, [])
            if RGB_COUNT_PARAM in names:
                set_param(after, base, names.index(RGB_COUNT_PARAM), len(colours))

    labels = RGB_PARAM_NAMES.get(mode, [])
    for spec in (args.param or []):
        if "=" not in spec:
            sys.exit("--param takes NAME=VALUE, e.g. --param speed=25. "
                     "'rgb effects %s' lists the names."
                     % RGB_MODES.get(mode, "").replace(" ", "_"))
        key, _, raw = spec.partition("=")
        key = key.strip()
        if key == RGB_COUNT_PARAM:
            sys.exit("'count' is the number of colours, so it comes from --colour "
                     "and is not set by hand.")
        if key in labels:
            k = labels.index(key)
        else:
            try:
                k = int(key)
            except ValueError:
                sys.exit("Effect '%s' has no parameter %r. Known: %s"
                         % (RGB_MODES.get(mode, "%#04x" % mode), key,
                            ", ".join(n for n in labels if n and n != RGB_COUNT_PARAM)
                            or "none"))
        if not 0 <= k < 9:
            sys.exit("Parameter index must be 0-8.")
        try:
            value = int(raw)
        except ValueError:
            sys.exit("Parameter %s takes a whole number, got %r." % (key, raw))
        if not 0 <= value <= 0xFFFF:
            sys.exit("Parameter value must fit in 16 bits (0-65535).")
        print("  %s: %d -> %d" % (key, get_param(buf, base, k), value))
        set_param(after, base, k, value)

    for spec, want in [(f, True) for f in (args.flag or [])] + \
                      [(f, False) for f in (args.no_flag or [])]:
        known = RGB_FLAG_NAMES.get(mode, {})
        if spec in RGB_SOURCE_FLAG_NAMES:
            off, bit = RGB_SOURCE_FLAGS, RGB_SOURCE_FLAG_NAMES[spec]
        elif spec in known:
            off, bit = RGB_FLAGS_OFFSET, known[spec]
        else:
            sys.exit("Effect '%s' has no flag %r. Known: %s"
                     % (RGB_MODES.get(mode, "%#04x" % mode), spec,
                        ", ".join(sorted(set(known) | set(RGB_SOURCE_FLAG_NAMES))) or "none"))
        old = after[base + off]
        after[base + off] = (old | bit) if want else (old & ~bit)
        print("  flag %s: %s -> %s" % (spec, "on" if old & bit else "off",
                                       "on" if want else "off"))
    reseal(after)
    commit(octo, buf, after, args)


def cmd_rgb_effects(octo, args):
    """Print what each effect accepts. Needs no device.

    Generated from the same tables the writer validates against, so it cannot
    drift from what the tool will actually let you set."""
    def describe(mode):
        name = RGB_MODES[mode]
        print("%s  (%#04x)" % (name.replace(" ", "_"), mode))
        if mode in RGB_MODES_HOST_DRIVEN:
            print("  NOT USABLE ON LINUX: the device stores this effect but the")
            print("  animation is streamed from an Aquasuite host DLL, so the LEDs")
            print("  sit at their background with nothing running.")
        has_bg, lo, hi = RGB_PALETTE_SPEC.get(mode, (False, 0, 0))
        if hi == 0:
            print("  colours   : none (the effect generates its own)")
        else:
            bits = []
            if has_bg:
                bits.append("--background RRGGBB")
            bits.append("--colour %s"
                        % ",".join(["RRGGBB"] * lo)
                        + ("[,...up to %d]" % hi if hi > lo else ""))
            print("  colours   : %s" % "  ".join(bits))
        names = [n for n in RGB_PARAM_NAMES.get(mode, []) if n and n != RGB_COUNT_PARAM]
        print("  parameters: %s"
              % (", ".join("%s=N" % n for n in names) if names else "none"))
        flags = sorted(RGB_FLAG_NAMES.get(mode, {}))
        print("  flags     : %s" % (", ".join(flags) if flags else "none"))
        print("  data source: --sensor 1-4 | flow | none, with --filter-rise /")
        print("               --filter-fall and source_speed / source_brightness flags")

    if args.effect:
        lookup = {v.replace(" ", "_"): k for k, v in RGB_MODES.items()}
        key = args.effect.lower().replace(" ", "_")
        if key not in lookup:
            sys.exit("Unknown effect %r. Run 'octoctl rgb effects' for the list."
                     % args.effect)
        describe(lookup[key])
        return

    print("Effects, as stored in the report. Parameter values are whole numbers;")
    print("most are 0-100 in the official software's sliders.\n")
    for mode in sorted(RGB_MODES):
        if mode in RGB_MODES_UNIMPLEMENTED:
            continue
        describe(mode)
        print()
    print("Not implemented on this firmware, and rejected by 'rgb set': %s"
          % ", ".join("%#04x" % m for m in sorted(RGB_MODES_UNIMPLEMENTED)))


def cmd_name_list(octo, args):
    """Every name stored on the device, in the groups the CLI uses."""
    decoded = decode_labels(octo.read(LABEL_REPORT_ID))
    for cli_group, report_group in NAME_GROUPS.items():
        names = decoded[report_group]
        print("%s:" % cli_group)
        for i, name in enumerate(names, 1):
            print("  %-11s %-2d  %s" % (cli_group, i, name if name else "-"))


def cmd_name(octo, args):
    base, count = LABEL_GROUPS[NAME_GROUPS[args.group]]
    if not 1 <= args.index <= count:
        sys.exit("%s index must be 1-%d." % (args.group, count))
    buf = octo.read(LABEL_REPORT_ID)
    slot = base + LABEL_SIZE * (args.index - 1)
    current = buf[slot:slot + LABEL_SIZE].split(b"\x00")[0].decode(LABEL_ENCODING, "replace")
    if args.text is None:
        print(current)
        return
    if "\x00" in args.text:
        # The device terminates a name at the first NUL, so this would store a
        # label that reads back shorter than what was asked for.
        sys.exit("A label cannot contain a NUL byte.")
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


def cmd_mode_curve(octo, args):
    """Curve: 16 temperature/power points the channel interpolates between.

    With no points given this just switches the channel into curve mode and
    prints the 16 points already stored, so you can see what you enabled.
    Aquasuite's "automatic" and "manual" curve setup write the same 16 points -
    the automatic dialog only fills them in for you."""
    before = octo.read()
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
    commit(octo, before, after, args)


def cmd_pid(octo, args):
    """Read or set a channel's controller tuning.

    The device stores no preset identifier - the official software just writes
    these five numbers - so --preset is a named row of values, and any explicit
    flag given alongside it wins."""
    before = octo.read()
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


def cmd_mode_target(octo, args):
    """Target temperature: the channel regulates to hold a sensor at a value.

    Both the temperature and the sensor are optional, so this doubles as a plain
    switch back into target mode with whatever was already configured."""
    before = octo.read()
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
    commit(octo, before, after, args)


def cmd_mode_follow(octo, args):
    """Make a channel follow another channel's output."""
    if args.target == args.channel:
        sys.exit("A channel cannot follow itself.")
    before = octo.read()
    after = bytearray(before)
    base = channel_base(args.channel)
    after[base + OFF_MODE] = args.target + LINK_OFFSET
    reseal(after)
    print("channel %d: %s -> follow channel %d"
          % (args.channel, mode_name(before[base + OFF_MODE]), args.target))
    print("  Aquasuite also copies the source channel's power window onto the")
    print("  follower when you do this in the GUI; octoctl only sets the mode.")
    commit(octo, before, after, args)


def cmd_fallback(octo, args):
    before = octo.read()
    after = bytearray(before)
    rec = record_base(args.channel)
    put_be16(after, rec + REC_FALLBACK, to_raw_pct(require_pct(args.percent)))
    reseal(after)
    print("channel %d fallback: %.2f%% -> %.2f%%"
          % (args.channel, pct(be16(before, rec + REC_FALLBACK)), args.percent))
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
    """Read or set one temperature sensor's calibration offset."""
    before = octo.read()
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
    commit(octo, before, after, args)


def cmd_protect(octo, args):
    """Mark a fan channel as protected against accidental writes.

    Local setting, not a device one: the Octo has no such field. Protect the
    headers a pump is on - a typo that stops a pump is not undone by undoing the
    command."""
    cfg = load_config()
    current = protected_channels()
    if args.state == "on":
        current.add(args.channel)
    else:
        current.discard(args.channel)
    cfg["protected"] = sorted(current)
    path = save_config(cfg)
    print("channel %d protection: %s" % (args.channel, args.state))
    print("protected channels: %s"
          % (", ".join(str(c) for c in sorted(current)) if current else "none"))
    print("stored in %s" % path)


UDEV_HELP = """\
Install a udev rule so root is not needed:

  echo 'KERNEL=="hidraw*", ATTRS{idVendor}=="0c70", ATTRS{idProduct}=="f011", \\
MODE="0660", GROUP="wheel"' | sudo tee /etc/udev/rules.d/99-aquacomputer.rules
  sudo udevadm control --reload && sudo udevadm trigger

Replace wheel with a group you are in.
"""


# Commands that used to exist, and what replaced them. A clean break is only
# kind if it says where the command went.
OLD_COMMANDS = {
    "show": "info",
    "dump": "backup",
    "set": "fan set <ch> mode fixed <percent>",
    "mode": "fan set <ch> mode fixed|target|curve",
    "target": "fan set <ch> mode target <celsius>",
    "fansource": "fan set <ch> mode target --sensor N",
    "curve": "fan set <ch> mode curve ...",
    "link": "fan set <ch> mode follow <other-ch>",
    "window": "fan set <ch> limits <min> <max>",
    "pin": "fan set <ch> limits <percent> <percent>",
    "fallback": "fan set <ch> fallback <percent>",
    "boost": "fan set <ch> boost on|off",
    "holdmin": "fan set <ch> hold-min on|off",
    "maxrpm": "fan set <ch> max-rpm <rpm>",
    "pid": "fan set <ch> pid --preset NAME  (or --p/--i/--d/...)",
    "labels": "name list",
    "label": "name <group> <index> [text]",
    "offset": "sensor offset <1-4> [celsius]",
    "flow": "sensor flow [impulses]",
}

EPILOG = """\
vocabulary
  channel     a physical header: fan 1-8, rgb 1-2, sensor 1-4
  controller  one of 12 RGB config slots - "on channel N, LEDs A-B, effect X".
              Several can sit on one RGB channel; address them by channel and
              position, not by slot number.
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
  octoctl info
  octoctl fan set 3 mode fixed 40
  octoctl fan set 3 mode curve --linear 30 45 20 100 --sensor 1
  octoctl fan set 3 pid --preset fast
  octoctl fan set 1 protect on
  octoctl rgb set 2 pos 1-15 --effect static --colour FF0000
  octoctl rgb effects wave
  octoctl name fan 3 "Front Rad"

Every write is preceded by an automatic backup into
<your home>/.local/share/octoctl/ (correct under sudo)."""


def build_parser():
    ap = argparse.ArgumentParser(
        prog="octoctl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Read and modify the configuration of an Aquacomputer Octo.",
        epilog=EPILOG)
    ap.add_argument("--help-udev", action="store_true",
                    help="show a udev rule that avoids needing root, and exit")
    sub = ap.add_subparsers(dest="group")

    def writer(p, guarded=False):
        """Flags common to every command that writes to the device.
        Described together under "write flags" in the top-level help."""
        p.add_argument("-n", "--dry-run", action="store_true",
                       help="show what would change and write nothing "
                            "(implies --verbose)")
        p.add_argument("-v", "--verbose", action="store_true",
                       help="also show the byte-level diff")
        p.add_argument("-y", "--yes", action="store_true",
                       help="skip the confirmation prompt")
        p.add_argument("--backup", metavar="FILE",
                       help="where to put the pre-write backup")
        if guarded:
            p.add_argument("--force", action="store_true",
                           help="write even if the channel is protected")
        p.set_defaults(guarded=guarded)
        return p

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
        "fan", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="fan channels 1-8",
        description="A fan channel is one of the eight fan headers. Each decides "
                    "its speed one of four ways (see 'fan set <ch> mode') and has "
                    "its own power limits, fallback and start behaviour.")
    fan.set_defaults(_helper=fan)
    fansub = fan.add_subparsers(dest="fancmd")
    fset = fansub.add_parser("set", help="change one channel's settings")
    fset.add_argument("channel", type=int, choices=range(1, 9), metavar="CHANNEL",
                      help="fan header, 1-8")
    fset.set_defaults(_helper=fset)
    what = fset.add_subparsers(dest="what")

    mode = what.add_parser(
        "mode", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="how the channel decides its speed",
        description="How the channel decides its speed. Whatever you do not "
                    "mention keeps its stored value, so 'mode target' with no "
                    "arguments just switches back into target mode.")
    mode.set_defaults(_helper=mode)
    modesub = mode.add_subparsers(dest="mode")

    p = writer(modesub.add_parser(
        "fixed", help="hold a constant power, no regulation",
        epilog="example:  octoctl fan set 3 mode fixed 40",
        formatter_class=argparse.RawDescriptionHelpFormatter), guarded=True)
    p.add_argument("percent", type=float, metavar="PERCENT",
                   help="power to hold, 0-100")
    p.set_defaults(func=cmd_mode_fixed)

    p = writer(modesub.add_parser(
        "target", help="regulate to hold a sensor at a temperature",
        epilog="example:  octoctl fan set 3 mode target 36 --sensor 1",
        formatter_class=argparse.RawDescriptionHelpFormatter), guarded=True)
    p.add_argument("celsius", type=float, nargs="?", metavar="CELSIUS",
                   help="temperature to hold, 0-100 C (default: keep current)")
    p.add_argument("--sensor", metavar="S",
                   help="which sensor drives it: 1-4, 'flow', or '#N' for a raw index")
    p.set_defaults(func=cmd_mode_target)

    p = writer(modesub.add_parser(
        "curve", help="follow a 16-point temperature/power curve",
        epilog="examples:\n"
               "  octoctl fan set 7 mode curve --linear 30 45 20 100\n"
               "  octoctl fan set 7 mode curve 30:20,31:25,...  (16 pairs)\n"
               "  octoctl fan set 7 mode curve                  (switch back, show it)",
        formatter_class=argparse.RawDescriptionHelpFormatter), guarded=True)
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
        epilog="example:  octoctl fan set 6 mode follow 3",
        formatter_class=argparse.RawDescriptionHelpFormatter), guarded=True)
    p.add_argument("target", type=int, choices=range(1, 9), metavar="CHANNEL",
                   help="the channel to follow, 1-8")
    p.set_defaults(func=cmd_mode_follow)

    p = writer(what.add_parser(
        "limits", help="minimum and maximum power the channel may use",
        epilog="Equal values pin the channel while its controller keeps running.\n"
               "example:  octoctl fan set 3 limits 25 80",
        formatter_class=argparse.RawDescriptionHelpFormatter), guarded=True)
    p.add_argument("min", type=float, metavar="MIN", help="minimum power, 0-100 %%")
    p.add_argument("max", type=float, metavar="MAX", help="maximum power, 0-100 %%")
    p.set_defaults(func=cmd_limits)

    p = writer(what.add_parser(
        "fallback", help="power used when the sensor reads nothing",
        epilog="example:  octoctl fan set 3 fallback 50",
        formatter_class=argparse.RawDescriptionHelpFormatter), guarded=True)
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
        "pid", formatter_class=argparse.RawDescriptionHelpFormatter,
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
               "  octoctl fan set 5 pid              (read)\n"
               "  octoctl fan set 5 pid --preset fast\n"
               "  octoctl fan set 5 pid --preset fast --d 600"), guarded=True)
    p.add_argument("--preset", choices=sorted(PID_PRESETS),
                   help="a named tuning; explicit flags below override it")
    p.add_argument("--p", type=float, metavar="V", help="proportional factor")
    p.add_argument("--i", type=float, metavar="V", help="integral factor")
    p.add_argument("--d", type=float, metavar="V", help="derivative factor")
    p.add_argument("--reset", type=float, metavar="SECONDS", help="reset time")
    p.add_argument("--hysteresis", type=float, metavar="KELVIN", help="in kelvin")
    p.set_defaults(func=cmd_pid)

    p = what.add_parser(
        "protect", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="refuse writes to this channel unless --force",
        description="Guard a channel against accidental writes. Local setting - "
                    "the Octo has no such field - so it costs nothing on the "
                    "device. Use it on the headers your pumps are on: a typo that "
                    "stops a pump is not undone by undoing the command.",
        epilog="example:  octoctl fan set 1 protect on")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_protect, needs_device=False)

    # ----------------------------------------------------------------- rgb
    rgb = sub.add_parser(
        "rgb", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="RGB channels 1-2",
        description="An RGB channel is one of the two RGBpx headers. A controller "
                    "is one of 12 config slots saying \"on channel N, LEDs A-B, "
                    "run effect X\"; several can sit on one channel. You address "
                    "them by channel and position - the tool picks the slot.")
    rgb.set_defaults(_helper=rgb)
    rgbsub = rgb.add_subparsers(dest="rgbcmd")

    p = writer(rgbsub.add_parser(
        "set", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="configure a stretch of LEDs on a channel",
        description="Configure the LEDs at a position on a channel. If a "
                    "controller already covers exactly that range it is edited; "
                    "otherwise a free slot is used. An overlapping range is "
                    "refused rather than layered.",
        epilog="Colours are per-effect - run 'octoctl rgb effects NAME' to see\n"
               "what one accepts. Positions are 1-based and inclusive.\n\n"
               "examples:\n"
               "  octoctl rgb set 2 pos 1-15 --effect static --colour FF0000\n"
               "  octoctl rgb set 2 pos 61-79 --effect wave --background 0A0A0A \\\n"
               "                              --colour FF0000,00FF00 --param speed=25\n"
               "  octoctl rgb set 1 pos 1-28 --sensor 1 --flag source_brightness"))
    p.add_argument("channel", type=int, choices=range(1, RGB_CHANNELS + 1),
                   metavar="CHANNEL", help="RGBpx header, 1-%d" % RGB_CHANNELS)
    p.add_argument("pos_kw", metavar="pos", choices=["pos"], help="the word 'pos'")
    p.add_argument("pos", metavar="FIRST-LAST",
                   help="LED range on this channel, 1-based inclusive, e.g. 1-15")
    p.add_argument("--effect", metavar="NAME",
                   help="effect name; 'rgb effects' lists them")
    p.add_argument("--colour", metavar="RRGGBB[,RRGGBB...]",
                   help="the effect's colours, in role order")
    p.add_argument("--background", metavar="RRGGBB",
                   help="background colour, for effects that have one")
    p.add_argument("--param", action="append", metavar="NAME=VALUE",
                   help="an effect parameter, repeatable, e.g. --param speed=25")
    p.add_argument("--flag", action="append", metavar="NAME",
                   help="turn a flag on, repeatable, e.g. --flag reverse")
    p.add_argument("--no-flag", action="append", dest="no_flag", metavar="NAME",
                   help="turn a flag off, repeatable")
    p.add_argument("--sensor", metavar="S",
                   help="drive the effect from a sensor: 1-4, 'flow', '#N', or 'none'")
    p.add_argument("--filter-rise", type=int, dest="filter_rise", metavar="N",
                   help="damping for rising sensor values, 0-255")
    p.add_argument("--filter-fall", type=int, dest="filter_fall", metavar="N",
                   help="damping for falling sensor values, 0-255")
    p.set_defaults(func=cmd_rgb_set)

    p = writer(rgbsub.add_parser(
        "remove", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="clear a controller, or a whole channel",
        epilog="examples:\n"
               "  octoctl rgb remove 2 pos 61-79   (one controller)\n"
               "  octoctl rgb remove 2             (every controller on channel 2)"))
    p.add_argument("channel", type=int, choices=range(1, RGB_CHANNELS + 1),
                   metavar="CHANNEL", help="RGBpx header, 1-%d" % RGB_CHANNELS)
    p.add_argument("pos_kw", metavar="pos", nargs="?", choices=["pos", None],
                   help="the word 'pos'")
    p.add_argument("pos", metavar="FIRST-LAST", nargs="?",
                   help="LED range to clear (omit to clear the whole channel)")
    p.set_defaults(func=cmd_rgb_remove)

    p = writer(rgbsub.add_parser("switch", help="turn the whole RGB function on or off"))
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_rgb_switch)

    p = writer(rgbsub.add_parser("brightness",
                                 help="global brightness for both channels"))
    p.add_argument("percent", type=float, metavar="PERCENT", help="0-100")
    p.set_defaults(func=cmd_rgb_brightness)

    p = rgbsub.add_parser("effects", help="what each effect accepts",
                          description="Print the colours, parameters and flags "
                                      "each effect takes. Needs no device.")
    p.add_argument("effect", nargs="?", metavar="NAME",
                   help="one effect (omit for all of them)")
    p.set_defaults(func=cmd_rgb_effects, needs_device=False)

    # ---------------------------------------------------------------- name
    name = sub.add_parser(
        "name", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="names stored on the device",
        description="The Octo stores 42 names and shows them in the official "
                    "software. Groups: fan (8), controller (12), sensor (4), "
                    "flow (2), virtual (16, the device's software sensors).")
    name.set_defaults(_helper=name)
    namesub = name.add_subparsers(dest="namegroup")
    p = namesub.add_parser("list", help="every stored name")
    p.set_defaults(func=cmd_name_list)

    for group, (_base, count) in ((g, LABEL_GROUPS[NAME_GROUPS[g]])
                                  for g in sorted(NAME_GROUPS)):
        p = writer(namesub.add_parser(
            group, formatter_class=argparse.RawDescriptionHelpFormatter,
            help="%s names (1-%d)" % (group, count),
            epilog="Names are single-byte text (latin-1), up to %d characters.\n\n"
                   "examples:\n"
                   "  octoctl name %s 1 \"Wasser\"\n"
                   "  octoctl name %s 1            (read it)"
                   % (LABEL_MAX, group, group)))
        p.add_argument("index", type=int, choices=range(1, count + 1),
                       metavar="INDEX", help="1-%d" % count)
        p.add_argument("text", nargs="?", help="the new name (omit to read)")
        p.set_defaults(func=cmd_name, group=group)

    # -------------------------------------------------------------- sensor
    sensor = sub.add_parser(
        "sensor", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="temperature sensors and the flow meter",
        description="The four temperature headers, and the single flow header.")
    sensor.set_defaults(_helper=sensor)
    sensorsub = sensor.add_subparsers(dest="sensorcmd")

    p = writer(sensorsub.add_parser(
        "offset", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="calibration offset for one sensor",
        epilog="Stored on the device and applied to its readings.\n\n"
               "examples:\n"
               "  octoctl sensor offset 1 -0.6\n"
               "  octoctl sensor offset 1        (read it)"))
    p.add_argument("sensor", type=int, choices=range(1, 5), metavar="SENSOR",
                   help="temperature header, 1-4")
    p.add_argument("celsius", type=float, nargs="?", metavar="CELSIUS",
                   help="offset in degrees, -15.00 to +15.00 (omit to read)")
    p.set_defaults(func=cmd_offset)

    p = writer(sensorsub.add_parser(
        "flow", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="flow meter calibration",
        description="The Octo has one flow header, so this takes no index.",
        epilog="example:  octoctl sensor flow 169"))
    p.add_argument("impulses", type=int, nargs="?", metavar="IMPULSES",
                   help="impulses per litre, 10-1000 (omit to read)")
    p.set_defaults(func=cmd_flow)

    # ------------------------------------------------------ backup/restore
    p = sub.add_parser("backup", help="save the raw reports to a file",
                       description="Write both feature reports to disk, byte for "
                                   "byte, so 'restore' can put them back.")
    p.add_argument("-o", "--output", metavar="FILE", help="output file")
    p.set_defaults(func=cmd_backup)

    p = writer(sub.add_parser(
        "restore", help="write a saved report back to the device",
        description="Restore a file written by 'backup'. The checksum is checked "
                    "before anything is sent. This rewrites every channel, so it "
                    "is refused while any channel is protected."), guarded=True)
    p.add_argument("file", metavar="FILE")
    p.set_defaults(func=cmd_restore)

    return ap


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in OLD_COMMANDS:
        sys.exit("'%s' no longer exists. Use:\n  octoctl %s\n\n"
                 "The commands are grouped now - run 'octoctl --help'."
                 % (argv[0], OLD_COMMANDS[argv[0]]))

    ap = build_parser()
    args = ap.parse_args()

    if args.help_udev:
        print(UDEV_HELP)
        return
    if not getattr(args, "func", None):
        # A group named with no verb: show THAT group's help, not the top level.
        # Each intermediate parser stashes itself in _helper, and the deepest one
        # parsed wins because its set_defaults runs last.
        getattr(args, "_helper", ap).print_help()
        return

    for field, limit in (("percent", 100.0), ("min", 100.0), ("max", 100.0)):
        value = getattr(args, field, None)
        if value is not None and not 0.0 <= value <= limit:
            sys.exit("%s must be between 0 and %g." % (field, limit))
    require_window(getattr(args, "min", None), getattr(args, "max", None),
                   getattr(args, "channel", None))

    if getattr(args, "guarded", False):
        guard_channel(getattr(args, "channel", None), getattr(args, "force", False))

    if getattr(args, "needs_device", True) is False:
        args.func(None, args)
        return

    with Octo.find() as octo:
        args.func(octo, args)


if __name__ == "__main__":
    main()
