"""The Aquacomputer high flow NEXT: a flow meter with an internal and an
optional external temperature sensor, conductivity and water quality, a
display, alarms with a signal output, and two RGBpx channels.

The layout comes from highflow/LAYOUT.md, which names the capture behind every
field. Only fields a capture confirmed are decoded; unknown values are shown
raw rather than guessed. The rgb and name tables are the shared ones from
rgbpx and names, pointed at this device's layout."""

import argparse
import os
import struct
import sys

from . import core, names, rgbpx
from .core import be16

NAME = "highflow"
TITLE = "high flow NEXT"
PRODUCT_ID = 0xF012
# Report ids and sizes come from the device's HID report descriptor.
CTRL_REPORT_SIZE = 682
LABEL_REPORT_SIZE = 773
INPUT_REPORT_SIZE = 164
REPORT_SIZES = {core.CTRL_REPORT_ID: CTRL_REPORT_SIZE,
                core.LABEL_REPORT_ID: LABEL_REPORT_SIZE}
HAS_FANS = False

# --- report 0x08: 32 name slots of 24 bytes (3 + 32*24 = 771, the checksum
# offset). Only the groups Aquasuite lets you edit are offered: the voltage
# and RGB-header slots (1, 2, 6, 7) are not editable there, and the purpose of
# slots 3-5 and 23 is unknown.
SENSOR_MEMBERS = ["internal", "external", "flow", "conductivity", "power",
                  "volume", "water-quality"]
NAME_GROUPS = [
    names.NameGroup("controller", 0x0C3, 8, "8"),
    names.NameGroup("sensor", 0x183, 7, "the device's own readings",
                    members=SENSOR_MEMBERS),
    names.NameGroup("virtual", 0x243, 8, "8, the device's software sensors"),
]

# --- report 0x03, display
TEMPERATURE_UNIT = 0x003    # u8: 0 celsius, 1 fahrenheit
FLOW_UNIT = 0x004           # u8: 0 litres, 2 US gallons; Aquasuite never writes 1
PAGE_INTERVAL = 0x006       # u8: seconds between page changes, 3-60; 61 = off
PAGE_CHANGE_OFF = 61
PAGES = 0x009               # u16: bit n-1 = page n
BRIGHTNESS = 0x00F          # u8: 0 high, 1 medium, 2 low
IDLE_BRIGHTNESS = 0x010     # u8: 0 high, 1 medium, 2 low, 3 off
DISPLAY_FLAGS = 0x015       # bits, DISPLAY_BITS; 0x02 unknown
CHARTS = 0x016              # 4 x (u16 source, u16 interval)
CHART_COUNT = 4

TEMPERATURE_UNITS = {0: "celsius", 1: "fahrenheit"}
FLOW_UNITS = {0: "litres", 2: "gallons"}
BRIGHTNESS_LEVELS = {0: "high", 1: "medium", 2: "low"}
IDLE_LEVELS = {0: "high", 1: "medium", 2: "low", 3: "off"}
DISPLAY_BITS = {"rotate": 0x01, "invert": 0x04, "auto-invert": 0x08,
                "device-keys-disabled": 0x10, "menu-lock": 0x20}
# Identified from the framebuffer (report 0x0c) of each single-page capture.
PAGE_NAMES = {1: "logo", 2: "flow", 3: "internal temperature",
              4: "external temperature", 5: "conductivity", 6: "water quality",
              7: "volume", 8: "power dissipation", 9: "flow + internal temperature",
              10: "conductivity + water quality",
              11: "internal + external temperature", 12: "flow + volume",
              13: "chart 1", 14: "chart 2", 15: "chart 3", 16: "chart 4"}
CHART_SOURCES = {0: "flow", 1: "internal temperature", 2: "external temperature",
                 3: "conductivity", 4: "water quality", 5: "power dissipation",
                 6: "system voltage"}
# Stored value -> seconds. Tenths of a second, except 0.5 s, stored as 1: the
# device's rule for any other value is unknown, so only these eight are known.
CHART_INTERVALS = {1: 0.5, 10: 1, 50: 5, 100: 10, 300: 30, 600: 60,
                   3000: 300, 6000: 600}

# --- report 0x03, system
USB_OVER_SPEC = 0x027       # u8: 1 allows a current limit above 500 mA
USB_CURRENT = 0x028         # u16: mA, 500 default, 2000 at Aquasuite's maximum
AQUABUS_ADDRESS = 0x02A     # u8: 58-61 (manual, 13.5)
STANDBY = 0x28D             # bits, STANDBY_BITS; 0x08 unknown
STANDBY_BITS = {"when-usb-disconnected": 0x01, "on-usb-suspend": 0x02,
                "without-aquabus": 0x04}
IN_STANDBY_BITS = {"alarms-off": 0x10, "display-off": 0x20, "leds-off": 0x40,
                   "volume-stops": 0x80}

# --- report 0x03, sensors and flow calculation
OFFSET_INTERNAL = 0x02B     # s16: 0.01 C
OFFSET_EXTERNAL = 0x02D     # s16: 0.01 C
COOLANT = 0x02F             # u8: 0 DP Ultra, 1 distilled water
CONNECTOR = 0x030           # u8: 0 inner diameter over 7 mm, 1 under
CALIBRATION = 0x031         # 10 x s16: correction per point, x100
CALIBRATION_POINTS = 0x045  # 10 x u16: the flows they apply at, dL/h
CALIBRATION_COUNT = 10
WATER_QUALITY_GOOD = 0x292  # u16: conductivity at 100 %, 0.1 uS/cm
WATER_QUALITY_BAD = 0x294   # u16: conductivity at 0 %, 0.1 uS/cm

COOLANTS = {0: "DP Ultra", 1: "distilled water"}
CONNECTORS = {0: "inner diameter over 7 mm", 1: "inner diameter under 7 mm"}

# --- report 0x03, alarms
ALARM_ACTIONS = 0x29A       # bits, ALARM_ACTION_BITS; 0x01-0x10 unknown
ALARM_ENABLE = 0x29B        # bits, one per alarm; 0x10-0x80 unknown
ALARM_DELAY = 0x29D         # u8: alarms ignored for this long after startup, 5-100 s
SIGNAL_OUTPUT = 0x2A6       # u8: SIGNAL_MODES
ALARM_ACTION_BITS = {"buzzer": 0x80, "blink-led": 0x40, "stop-signal": 0x20}
# Aquasuite's wording, for the output.
ALARM_ACTION_TEXT = {"buzzer": "sound the buzzer",
                     "blink-led": "blink the internal LED red",
                     "stop-signal": "disable the fan speed/flow signal output"}


class Alarm:
    """One alarm: its enable bit, where its limit is stored, and how the limit
    reads. `below` is the manual's rule (11.2): flow and water quality alarm
    when the reading drops below the limit, temperatures when it rises above."""

    def __init__(self, key, bit, offset, scale, fmt, unit, below, live_key):
        self.key, self.bit, self.offset = key, bit, offset
        self.scale, self.fmt, self.unit = scale, fmt, unit
        self.below, self.live_key = below, live_key

    def limit(self, buf):
        return be16(buf, self.offset) / self.scale

    def crossed(self, limit, reading):
        """True if the reading is past the limit, None if there is no reading."""
        if reading is None:
            return None
        return reading < limit if self.below else reading > limit


ALARMS = [
    Alarm("flow", 0x01, 0x29E, 10.0, "%.1f", "l/h", True, "flow"),
    Alarm("internal", 0x02, 0x2A0, 100.0, "%.2f", "C", False, "internal"),
    Alarm("external", 0x04, 0x2A2, 100.0, "%.2f", "C", False, "external"),
    Alarm("water-quality", 0x08, 0x2A4, 100.0, "%.2f", "%", True, "water_quality"),
]

# Aquasuite's names for the modes (manual, 11.1), under the names the CLI uses.
SIGNAL_MODES = {
    0: ("fan-speed", "Generate fan speed signal"),
    1: ("flow-sensor", "Generate high flow sensor (53068) signal "
                       "(DP Ultra, inner diameter > 7mm)"),
    2: ("fan-speed-from-flow", "Generate fan speed signal from flow rate "
                               "(1000 rpm = 100 l/h)"),
    3: ("power-switch", "Power switch, activate for 1 second on alarm "
                        "(suitable for article no. 53216)"),
    4: ("on-during-alarm", "Activate in case of alarm"),
    5: ("off-during-alarm", "Activate permanently and deactivate in case of alarm"),
}

# --- RGBpx: 8 controllers of 70 bytes, format in rgbpx. Slots 1-6 belong to
# channel 1, the external header (up to 90 LEDs); slots 7-8 to channel 2, the
# 10 LEDs built into the top. The manual: "up to eight LED controllers (two for
# integrated lighting, six for external components)", and every slot in the
# captures carries its channel's port byte.
RGB_BRIGHTNESS = 0x059
RGB_ENABLE = 0x05B
RGB_BASE = 0x05C
RGB = rgbpx.RgbLayout(base=RGB_BASE, brightness=RGB_BRIGHTNESS, enable=RGB_ENABLE,
                      channels={1: (1, 6, 90), 2: (7, 8, 10)},
                      # The only effect seen on this device so far.
                      verified={0x21})
# No capture has set a data source on this device, so its numbering is unknown.
RGB_SOURCE_HELP = "'#N' for a raw source index, or 'none' (the high flow NEXT's " \
                  "source numbers have not been captured)"


def source_label(index):
    return "source #%d" % index


# --- report 0x01, live readings. Sent by the device once a second, without a
# checksum; 0x7fff marks a reading that is not available.
LIVE_NONE = 0x7FFF
LIVE_FLOW = 0x51            # u16: dL/h
LIVE_INTERNAL = 0x55        # s16: 0.01 C, offset included
LIVE_EXTERNAL = 0x57        # s16: 0.01 C, offset included
LIVE_WATER_QUALITY = 0x59   # u16: 0.01 %
LIVE_POWER = 0x5B           # s16: W as the kernel driver reads it; unconfirmed
LIVE_CONDUCTIVITY = 0x5F    # u16: 0.1 uS/cm
LIVE_VCC5 = 0x61            # u16: 0.01 V
LIVE_VCC5_USB = 0x63        # u16: 0.01 V
LIVE_VOLUME = 0x65          # u32: litres
LIVE_IMPULSES = 0x69        # u32
LIVE_SINCE_RESET = 0x6D     # u32: seconds since the volume counter was reset


# ------------------------------------------------------------------ decode

def _bits(value, table):
    return {name: bool(value & bit) for name, bit in table.items()}


def decode(buf):
    """Every decoded setting of a settings report, by name, as stored: integers
    in the report's own units, bit fields split into named booleans."""
    s = {
        "temperature_unit": buf[TEMPERATURE_UNIT],
        "flow_unit": buf[FLOW_UNIT],
        "page_interval": buf[PAGE_INTERVAL],
        "pages": tuple(n for n in range(1, 17) if be16(buf, PAGES) & (1 << (n - 1))),
        "brightness": buf[BRIGHTNESS],
        "idle_brightness": buf[IDLE_BRIGHTNESS],
        "usb_over_spec": buf[USB_OVER_SPEC],
        "usb_current": be16(buf, USB_CURRENT),
        "aquabus_address": buf[AQUABUS_ADDRESS],
        "offset_internal": core.sbe16(buf, OFFSET_INTERNAL),
        "offset_external": core.sbe16(buf, OFFSET_EXTERNAL),
        "coolant": buf[COOLANT],
        "connector": buf[CONNECTOR],
        "calibration": tuple(core.sbe16(buf, CALIBRATION + 2 * i)
                             for i in range(CALIBRATION_COUNT)),
        "calibration_points": tuple(be16(buf, CALIBRATION_POINTS + 2 * i)
                                    for i in range(CALIBRATION_COUNT)),
        "rgb_brightness": buf[RGB_BRIGHTNESS],
        "rgb_enable": buf[RGB_ENABLE],
        "water_quality_good": be16(buf, WATER_QUALITY_GOOD),
        "water_quality_bad": be16(buf, WATER_QUALITY_BAD),
        "alarm_delay": buf[ALARM_DELAY],
        "signal_output": buf[SIGNAL_OUTPUT],
    }
    for n in range(1, CHART_COUNT + 1):
        s["chart%d_source" % n] = be16(buf, CHARTS + 4 * (n - 1))
        s["chart%d_interval" % n] = be16(buf, CHARTS + 4 * (n - 1) + 2)
    for key, value in _bits(buf[DISPLAY_FLAGS], DISPLAY_BITS).items():
        s["display_" + key] = value
    for key, value in _bits(buf[STANDBY], dict(STANDBY_BITS, **IN_STANDBY_BITS)).items():
        s["standby_" + key] = value
    for key, value in _bits(buf[ALARM_ACTIONS], ALARM_ACTION_BITS).items():
        s["alarm_" + key] = value
    for alarm in ALARMS:
        s["alarm_%s" % alarm.key] = bool(buf[ALARM_ENABLE] & alarm.bit)
        s["limit_%s" % alarm.key] = be16(buf, alarm.offset)
    return s


def mapped_offsets():
    """Every byte of the settings report that decode() or the RGB table reads,
    plus the checksum. A capture that changes a byte outside this set changed
    something the map does not know."""
    spans = [(TEMPERATURE_UNIT, 1), (FLOW_UNIT, 1), (PAGE_INTERVAL, 1), (PAGES, 2),
             (BRIGHTNESS, 1), (IDLE_BRIGHTNESS, 1), (DISPLAY_FLAGS, 1),
             (CHARTS, 4 * CHART_COUNT), (USB_OVER_SPEC, 1), (USB_CURRENT, 2),
             (AQUABUS_ADDRESS, 1), (OFFSET_INTERNAL, 2), (OFFSET_EXTERNAL, 2),
             (COOLANT, 1), (CONNECTOR, 1), (CALIBRATION, 2 * CALIBRATION_COUNT),
             (CALIBRATION_POINTS, 2 * CALIBRATION_COUNT), (RGB_BRIGHTNESS, 1),
             (RGB_ENABLE, 1), (RGB_BASE, rgbpx.RGB_STRIDE * RGB.count),
             (STANDBY, 1), (WATER_QUALITY_GOOD, 2), (WATER_QUALITY_BAD, 2),
             (ALARM_ACTIONS, 1), (ALARM_ENABLE, 1), (ALARM_DELAY, 1),
             (SIGNAL_OUTPUT, 1), (CTRL_REPORT_SIZE - 2, 2)]
    spans += [(a.offset, 2) for a in ALARMS]
    return {off + i for off, size in spans for i in range(size)}


def decode_live(buf):
    """Report 0x01 -> the readings, in display units; None where the device
    reports none."""
    def u16(off, scale):
        raw = be16(buf, off)
        return None if raw == LIVE_NONE else raw / scale

    def s16(off, scale):
        raw = core.sbe16(buf, off)
        return None if raw == LIVE_NONE else raw / scale

    def u32(off):
        return struct.unpack_from(">I", buf, off)[0]

    external = s16(LIVE_EXTERNAL, 100.0)
    return {
        "source": "report 0x01",
        "serial": "%05d-%05d" % (be16(buf, 3), be16(buf, 5)),
        "firmware": core.firmware(buf),
        "flow": u16(LIVE_FLOW, 10.0),
        "internal": s16(LIVE_INTERNAL, 100.0),
        "external": external,
        "water_quality": u16(LIVE_WATER_QUALITY, 100.0),
        # Computed from the temperature difference and the flow, so there is
        # nothing to compute without the external sensor; the device then
        # sends 0, and so does its display.
        "power": None if external is None else s16(LIVE_POWER, 1.0),
        "conductivity": u16(LIVE_CONDUCTIVITY, 10.0),
        "vcc5": u16(LIVE_VCC5, 100.0),
        "vcc5_usb": u16(LIVE_VCC5_USB, 100.0),
        "volume": u32(LIVE_VOLUME),
        "impulses": u32(LIVE_IMPULSES),
        "since_reset": u32(LIVE_SINCE_RESET),
    }


def read_live_hwmon(serial):
    """The kernel driver's readings, if report 0x01 could not be read. It has
    no volume counter, and a few of its units differ from its labels."""
    from . import discovery
    path = discovery.hwmon_dir(serial)
    if path is None:
        return {}

    def read(leaf, scale):
        try:
            with open(os.path.join(path, leaf)) as fh:
                return int(fh.read().strip()) / scale
        except (OSError, ValueError):
            return None       # temp2 fails with ENODATA when no sensor is attached

    external = read("temp2_input", 1000.0)
    power = read("power1_input", 1000000.0)
    return {
        "source": "the kernel driver",
        "firmware": None,
        "flow": read("fan1_input", 10.0),
        "internal": read("temp1_input", 1000.0),
        "external": external,
        "water_quality": read("fan2_input", 100.0),
        # Without the external sensor the driver hands out -ENODATA as an
        # unsigned number (about 4.29 kW); there is no reading then.
        "power": power if external is not None else None,
        # Labelled nS/cm by the driver, but in 0.1 uS/cm like the report.
        "conductivity": read("fan3_input", 10.0),
        "vcc5": read("in0_input", 1000.0),
        "vcc5_usb": read("in1_input", 1000.0),
        "volume": None, "impulses": None, "since_reset": None,
    }


def read_live(dev):
    """Live readings: report 0x01, which the device sends on its own (nothing
    is sent to it), or the kernel driver's if none arrives in time."""
    buf = dev.read_input(INPUT_REPORT_SIZE, 1500)
    return decode_live(buf) if buf else read_live_hwmon(dev.serial)


# ------------------------------------------------------------------ output

def _name(table, value):
    return table.get(value, "unknown (%d)" % value)


def _on(flag):
    return "on" if flag else "off"


def _num(value, fmt, unit, none="no reading"):
    return none if value is None else (fmt % value) + (" " + unit if unit else "")


def _duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    return "%d d %02d:%02d:%02d" % (days, rest // 3600, rest % 3600 // 60, rest % 60)


def chart_interval_text(stored):
    seconds = CHART_INTERVALS.get(stored)
    return "every %g s" % seconds if seconds is not None else "unknown interval (%d)" % stored


def info_display(buf, s):
    print("display")
    print("  brightness        %s, when idle: %s"
          % (_name(BRIGHTNESS_LEVELS, s["brightness"]),
             _name(IDLE_LEVELS, s["idle_brightness"])))
    interval = s["page_interval"]
    print("  page change       %s" % ("off" if interval == PAGE_CHANGE_OFF
                                      else "every %d s" % interval))
    print("  pages             %s" % (", ".join("%d %s" % (n, PAGE_NAMES[n])
                                               for n in s["pages"]) or "none"))
    print("  switches          rotate %s, invert %s, auto-invert %s, device-keys %s, "
          "menu-lock %s"
          % (_on(s["display_rotate"]), _on(s["display_invert"]),
             _on(s["display_auto-invert"]), _on(not s["display_device-keys-disabled"]),
             _on(s["display_menu-lock"])))
    print("  units             %s, %s"
          % (_name(TEMPERATURE_UNITS, s["temperature_unit"]),
             _name(FLOW_UNITS, s["flow_unit"])))
    for n in range(1, CHART_COUNT + 1):
        print("  chart %d           %s, %s"
              % (n, _name(CHART_SOURCES, s["chart%d_source" % n]),
                 chart_interval_text(s["chart%d_interval" % n])))
    unknown = buf[DISPLAY_FLAGS] & ~sum(DISPLAY_BITS.values())
    if unknown:
        print("  unknown display bits %#04x" % unknown)


def info_flow(s, live):
    print("flow")
    print("  now               %s" % _num(live.get("flow"), "%.1f", "l/h"))
    print("  coolant           %s" % _name(COOLANTS, s["coolant"]))
    print("  connector         %s" % _name(CONNECTORS, s["connector"]))
    corrections = ["%g l/h: %+.2f" % (point / 10.0, value / 100.0)
                   for point, value in zip(s["calibration_points"], s["calibration"])
                   if value]
    print("  calibration       %s" % (", ".join(corrections) or "no corrections"))
    if live.get("volume") is not None:
        print("  volume            %d l, %d impulses, %s since the last reset"
              % (live["volume"], live["impulses"], _duration(live["since_reset"])))
    else:
        print("  volume            needs report 0x01, which could not be read")


def info_sensor(s, labels, live):
    sensor_names = dict(zip(SENSOR_MEMBERS, labels))

    def row(key, reading, extra=""):
        name = sensor_names.get(key, "")
        print(("  %-14s %-26s %-13s %s"
               % (key, '"%s"' % name if name else "-", reading, extra)).rstrip())

    print("sensor           stored name                reading")
    row("internal", _num(live.get("internal"), "%.2f", "C"),
        "offset %+.2f C" % (s["offset_internal"] / 100.0))
    row("external", _num(live.get("external"), "%.2f", "C", "no sensor"),
        "offset %+.2f C" % (s["offset_external"] / 100.0))
    row("flow", _num(live.get("flow"), "%.1f", "l/h"))
    row("conductivity", _num(live.get("conductivity"), "%.1f", "uS/cm"))
    row("water-quality", _num(live.get("water_quality"), "%.2f", "%"),
        "100 %% at %.1f uS/cm, 0 %% at %.1f uS/cm"
        % (s["water_quality_good"] / 10.0, s["water_quality_bad"] / 10.0))
    power = live.get("power")
    row("power", "needs the external sensor" if power is None and live.get("external") is None
        else _num(power, "%d", "W"))
    volume = live.get("volume")
    row("volume", "%d l" % volume if volume is not None else "no reading")
    print("  supply         %s, USB %s"
          % (_num(live.get("vcc5"), "%.2f", "V"), _num(live.get("vcc5_usb"), "%.2f", "V")))


def info_alarm(buf, s, live):
    print("alarm            state  fires when the reading is")
    for alarm in ALARMS:
        limit = alarm.limit(buf)
        reading = live.get(alarm.live_key)
        crossed = alarm.crossed(limit, reading)
        print(("  %-14s %-6s %s %-12s %-16s %s"
               % (alarm.key, _on(s["alarm_%s" % alarm.key]),
                  "below" if alarm.below else "above",
                  alarm.fmt % limit + " " + alarm.unit,
                  "now " + alarm.fmt % reading + " " + alarm.unit
                  if reading is not None else "no reading",
                  "<- limit crossed" if crossed else "")).rstrip())
    print("  after startup     alarms are ignored for %d s" % s["alarm_delay"])
    actions = [ALARM_ACTION_TEXT[k] for k in ALARM_ACTION_BITS if s["alarm_" + k]]
    print("  during an alarm   %s" % ("; ".join(actions) if actions else "no actions"))
    unknown = buf[ALARM_ACTIONS] & ~sum(ALARM_ACTION_BITS.values())
    if unknown:
        print("                    unknown action bits %#04x" % unknown)
    unknown = buf[ALARM_ENABLE] & ~sum(a.bit for a in ALARMS)
    if unknown:
        print("  unknown alarm bits %#04x" % unknown)
    mode = s["signal_output"]
    cli, text = SIGNAL_MODES.get(mode, ("unknown", "mode %d" % mode))
    print("  signal output     %s: %s" % (cli, text))


def info_system(buf, s):
    print("system")
    print("  USB current limit %d mA%s"
          % (s["usb_current"], ", above the USB limit allowed" if s["usb_over_spec"]
             else ""))
    print("  aquabus address   %d" % s["aquabus_address"])
    print("  standby allowed   %s" % ", ".join("%s %s" % (k, _on(s["standby_" + k]))
                                              for k in STANDBY_BITS))
    print("  in standby        %s" % ", ".join("%s %s" % (k, _on(s["standby_" + k]))
                                              for k in IN_STANDBY_BITS))
    unknown = buf[STANDBY] & ~sum(dict(STANDBY_BITS, **IN_STANDBY_BITS).values())
    if unknown:
        print("  unknown standby bits %#04x" % unknown)


SECTIONS = ["display", "flow", "sensor", "alarm", "rgb", "system"]


def show(buf, labels, live, sections, serial):
    """The info output; separate from cmd_info so the tests can feed it
    captures instead of a device."""
    s = decode(buf)
    firmware = live.get("firmware")
    print("%s %s%s%s"
          % (TITLE, serial, "   firmware %d" % firmware if firmware else "",
             "   (live readings from %s)" % live["source"] if live
             else "   (no live readings: report 0x01 did not arrive and the "
                  "kernel driver is not loaded)"))
    for section in sections:
        print()
        if section == "display":
            info_display(buf, s)
        elif section == "flow":
            info_flow(s, live)
        elif section == "sensor":
            info_sensor(s, labels["sensor"], live)
        elif section == "alarm":
            info_alarm(buf, s, live)
        elif section == "rgb":
            rgbpx.info_rgb(buf, RGB, labels["controller"], source_label)
        elif section == "system":
            info_system(buf, s)


def cmd_info(dev, args):
    """Everything the device is configured to do, plus what it is doing now."""
    buf = dev.read()
    try:
        labels = names.decode(dev.read(core.LABEL_REPORT_ID), NAME_GROUPS)
    except Exception:
        labels = {g.cli: [""] * g.count for g in NAME_GROUPS}
    want = getattr(args, "section", None)
    show(buf, labels, read_live(dev), [want] if want else SECTIONS, dev.serial)


def identify(labels):
    """A few of the names stored on this device, to tell two apart."""
    sensors = names.decode(labels, NAME_GROUPS)["sensor"][:3]
    text = "sensors: " + ", ".join(n for n in sensors if n)
    return text if len(text) <= 60 else text[:57] + "..."


# ------------------------------------------------------------------ parser

def _epilog(prefix):
    return """\
vocabulary
  channel     an RGBpx header: 1 is the external header (up to 90 LEDs),
              2 the 10 LEDs built into the top
  controller  one RGB config slot - "on channel N, LEDs A-B, effect X".
              Channel 1 has controllers 1-6, channel 2 has 7-8.

examples
  {p} info
  {p} info alarm
  {p} backup

Settings cannot be changed with this version yet; 'info' and 'backup' only
read from the device.""".format(p=prefix)


def build_parser(prefix):
    ap = argparse.ArgumentParser(
        prog=prefix, formatter_class=core.HelpFormatter,
        description="Read the configuration of this Aquacomputer high flow NEXT.",
        epilog=_epilog(prefix))
    sub = ap.add_subparsers(dest="group")

    p = sub.add_parser("info", help="show configuration and live readings",
                       description="Print what the high flow NEXT is configured to "
                                   "do, and what it reads right now. The live "
                                   "readings come from the report the device sends "
                                   "every second, so nothing is sent to it for them.")
    p.add_argument("section", nargs="?", choices=SECTIONS,
                   help="limit the output to one section (default: all)")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("backup", help="save the raw reports to a file",
                       description="Write both feature reports to disk, byte for "
                                   "byte. Without -o the file names carry the "
                                   "device's serial.")
    p.add_argument("-o", "--output", metavar="FILE", help="output file")
    p.set_defaults(func=core.cmd_backup)
    return ap
