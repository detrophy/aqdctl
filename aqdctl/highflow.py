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
import textwrap

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

    def __init__(self, key, bit, offset, scale, fmt, unit, below, live_key, lo, hi):
        self.key, self.bit, self.offset = key, bit, offset
        self.scale, self.fmt, self.unit = scale, fmt, unit
        self.below, self.live_key = below, live_key
        self.lo, self.hi = lo, hi      # the limit's range in Aquasuite

    def limit(self, buf):
        return be16(buf, self.offset) / self.scale

    def crossed(self, limit, reading):
        """True if the reading is past the limit, None if there is no reading."""
        if reading is None:
            return None
        return reading < limit if self.below else reading > limit


# Limit ranges from 84-alarm-limits-and-ranges.txt: flow 0-1000 l/h, the rest 5-100.
ALARMS = [
    Alarm("flow", 0x01, 0x29E, 10.0, "%.1f", "l/h", True, "flow", 0, 1000),
    Alarm("internal", 0x02, 0x2A0, 100.0, "%.2f", "C", False, "internal", 5, 100),
    Alarm("external", 0x04, 0x2A2, 100.0, "%.2f", "C", False, "external", 5, 100),
    Alarm("water-quality", 0x08, 0x2A4, 100.0, "%.2f", "%", True, "water_quality", 5, 100),
]
ALARM_BY_KEY = {a.key: a for a in ALARMS}
ALARM_DELAY_RANGE = (5, 100)

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


def parse_sensor(spec):
    """A --sensor argument for an RGB controller: only a raw index, because no
    capture has shown how this device numbers its data sources."""
    text = str(spec).strip()
    if text.startswith("#"):
        try:
            return int(text[1:], 0)
        except ValueError:
            pass
    sys.exit("The high flow NEXT's data source numbers have not been captured, so\n"
             "--sensor takes only a raw index as '#N', or 'none'.")


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
    print("  calibrated at     %s l/h"
          % ", ".join("%g" % (p / 10.0) for p in s["calibration_points"]))
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


# ----------------------------------------------------------------- safety

USB_SPEC_LIMIT = 500        # mA, USB 2.0
USB_MAX = 2000              # mA, the top of Aquasuite's slider
USB_WARNING = ("Setting a USB current above 0.5A can lead to damages on\n"
               "the USB header of the motherboard. The USB spec allows\n"
               "only for a current 0.5A at maximum. Use at your own risk.\n"
               "An external power supply should be used for safety.")
USB_HELP = ("500 mA is the USB limit. Values above it need --force: they can damage "
            "the motherboard's USB header.")

# Modes in which an alarm can switch the PC off (3) or cut its power (4, 5).
POWER_MODES = {3, 4, 5}
POWER_MODE_RISK = {
    3: "an alarm presses the PC's power button for 1 s, which turns the PC off",
    4: "an alarm switches the output on, which a shutdown add-on on the power "
       "header answers by shutting the PC down",
    5: "the output is on only while there is no alarm, so an add-on that cuts "
       "power when the signal disappears cuts it during every alarm",
}
VOLUME_RESET_COMMAND = 0x64


def _usb_guard(dev, before, after, args):
    """Above 500 mA needs --force; the warning is shown either way. Idempotent,
    so a command can run it before printing its change and the general guard
    can run it again."""
    if getattr(args, "usb_checked", False):
        return
    args.usb_checked = True
    touched = before[USB_OVER_SPEC:USB_CURRENT + 2] != after[USB_OVER_SPEC:USB_CURRENT + 2]
    if not touched or be16(after, USB_CURRENT) <= USB_SPEC_LIMIT:
        return
    if getattr(args, "force", False):
        core.warn_danger(USB_WARNING)
        print()
        return
    core.warn_danger(USB_WARNING, sys.stderr)
    print(file=sys.stderr)
    sys.exit("Nothing written. Increasing max current above 500 mA requires --force:\n"
             + core.force_hint(dev, args))


def _refuse_or_note(dev, args, text, ask):
    """With --force, say what is being overridden; without, refuse and quote
    the command with --force added."""
    if getattr(args, "force", False):
        print(textwrap.fill("--force: " + text, 78) + "\n")
        return
    sys.exit(textwrap.fill("Nothing written. " + text, 78) + "\n" + ask + "\n"
             + core.force_hint(dev, args))


def _alarm_bytes_changed(before, after):
    if before[ALARM_ACTIONS:SIGNAL_OUTPUT] != after[ALARM_ACTIONS:SIGNAL_OUTPUT]:
        return True
    # Alarm detection in standby: in standby the pump may stop, and with
    # detection on, the flow alarm then fires.
    return bool((before[STANDBY] ^ after[STANDBY]) & IN_STANDBY_BITS["alarms-off"])


def _signal_guard(dev, before, after, args):
    """Entering or leaving the power modes, or changing an alarm while in one,
    needs --force: the device cannot tell what is attached to its output."""
    old, new = before[SIGNAL_OUTPUT], after[SIGNAL_OUTPUT]
    reasons = []
    if old != new and new in POWER_MODES:
        reasons.append("In %s mode, %s." % (SIGNAL_MODES[new][0], POWER_MODE_RISK[new]))
    if old != new and old in POWER_MODES:
        if old == 5 and new in (3, 4):
            reasons.append("Leaving off-during-alarm switches the output off outside "
                           "alarms, so an add-on that cuts power when the signal "
                           "disappears cuts it at once.")
        elif old == 5:
            reasons.append("Leaving off-during-alarm replaces the permanent signal with "
                           "a pulse signal; how an add-on that cuts power reacts to that "
                           "is unknown.")
        else:
            reasons.append("Leaving %s mode changes what an add-on attached to the "
                           "output receives." % SIGNAL_MODES[old][0])
    if old == new and old in POWER_MODES and _alarm_bytes_changed(before, after):
        reasons.append("The signal output is in %s mode: %s. Changing an alarm "
                       "setting changes when that happens."
                       % (SIGNAL_MODES[old][0], POWER_MODE_RISK[old]))
    if not reasons:
        return
    text = " ".join(reasons) + (" The device cannot report what is attached to its "
                                "signal output, so aqdctl cannot check.")
    _refuse_or_note(dev, args, text, "Changing this requires --force:")


def _predicted(alarm, live, before, after):
    """The reading the alarm will see once `after` is written: the live one,
    moved by any change to the offset or water-quality points it depends on."""
    reading = live.get(alarm.live_key)
    if reading is None:
        return None
    offset = {"internal": OFFSET_INTERNAL, "external": OFFSET_EXTERNAL}.get(alarm.key)
    if offset is not None:
        return reading + (core.sbe16(after, offset) - core.sbe16(before, offset)) / 100.0
    if alarm.key == "water-quality":
        good, bad = be16(after, WATER_QUALITY_GOOD) / 10.0, be16(after, WATER_QUALITY_BAD) / 10.0
        points_moved = (before[WATER_QUALITY_GOOD:WATER_QUALITY_BAD + 2]
                        != after[WATER_QUALITY_GOOD:WATER_QUALITY_BAD + 2])
        if points_moved and live.get("conductivity") is not None and bad > good:
            # Linear in conductivity; checked against the device (85.48 % vs 85.49 %).
            return min(100.0, max(0.0, (bad - live["conductivity"]) / (bad - good) * 100))
    return reading


def _alarm_guard(dev, before, after, args):
    """An alarm that is on after the write, and was just turned on or had its
    limit, offset or water-quality points changed, must not already be past
    its limit or without a reading: the device raises it at once."""
    offsets = {"internal": OFFSET_INTERNAL, "external": OFFSET_EXTERNAL}
    points = slice(WATER_QUALITY_GOOD, WATER_QUALITY_BAD + 2)
    candidates = []
    for alarm in ALARMS:
        was_on = bool(before[ALARM_ENABLE] & alarm.bit)
        on = bool(after[ALARM_ENABLE] & alarm.bit)
        moved = (be16(before, alarm.offset) != be16(after, alarm.offset)
                 or (alarm.key in offsets and core.sbe16(before, offsets[alarm.key])
                     != core.sbe16(after, offsets[alarm.key]))
                 or (alarm.key == "water-quality" and before[points] != after[points]))
        if (on and not was_on) or moved:
            candidates.append((alarm, on))
    if not candidates:
        return
    live = read_live(dev)
    problems = []
    for alarm, on in candidates:
        limit = alarm.limit(after)
        reading = _predicted(alarm, live, before, after)
        crossed = alarm.crossed(limit, reading)
        if crossed is False:
            continue
        shown = (alarm.fmt % limit) + " " + alarm.unit
        if reading is None:
            why = ("no live readings are available" if not live
                   else "the %s sensor reads nothing" % alarm.key)
        else:
            why = "it will read %s %s, %s the limit of %s" % (
                alarm.fmt % reading, alarm.unit, "below" if alarm.below else "above", shown)
        if on:
            problems.append("The %s alarm would fire at once: %s." % (alarm.key, why))
        else:
            print("note: the %s alarm is off, but %s; turning it on would fire it."
                  % (alarm.key, why))
    if not problems:
        return
    text = " ".join(problems) + " The device raises an alarm as soon as its condition holds."
    _refuse_or_note(dev, args, text, "Writing this anyway requires --force:")


def guard_write(dev, before, after, args):
    """Every safety check for a settings write, whichever command made it;
    'restore' runs it too."""
    _usb_guard(dev, before, after, args)
    _signal_guard(dev, before, after, args)
    _alarm_guard(dev, before, after, args)


# ---------------------------------------------------------------- commands

def _commit(dev, before, after, args, lines):
    """Check, then print what changes, then write. The checks come first, so
    a refusal is not preceded by a change that did not happen."""
    core.reseal(after)
    if after != before:
        guard_write(dev, before, after, args)
    for line in lines:
        print(line)
    core.commit(dev, before, after, args)


def cmd_choice(dev, args):
    """A one-byte setting chosen from a list (args.offset, args.table)."""
    before = dev.read()
    old = _name(args.table, before[args.offset])
    if args.value is None:
        print("%s: %s" % (args.label, old))
        return
    after = bytearray(before)
    after[args.offset] = {v: k for k, v in args.table.items()}[args.value]
    _commit(dev, before, after, args, ["%s: %s -> %s" % (args.label, old, args.value)])


def cmd_switch(dev, args):
    """One bit of a flags byte. args.inverted: the bit set means off."""
    before = dev.read()
    old = bool(before[args.offset] & args.bit) != args.inverted
    if args.state is None:
        print("%s: %s" % (args.label, _on(old)))
        return
    after = bytearray(before)
    if (args.state == "on") != args.inverted:
        after[args.offset] |= args.bit
    else:
        after[args.offset] &= ~args.bit & 0xFF
    _commit(dev, before, after, args, ["%s: %s -> %s" % (args.label, _on(old), args.state)])


def cmd_pages(dev, args):
    before = dev.read()
    old = decode(before)["pages"]

    def text(pages):
        return ", ".join("%d (%s)" % (n, PAGE_NAMES[n]) for n in pages) or "none"
    if args.pages is None:
        print("display pages: %s" % text(old))
        return
    try:
        pages = sorted({int(p) for p in args.pages.split(",")})
    except ValueError:
        sys.exit("Pages are numbers 1-16, comma separated, e.g. 2,3. "
                 "'display pages --help' lists them.")
    if not pages or not all(1 <= p <= 16 for p in pages):
        sys.exit("Pages are 1-16. 'display pages --help' lists them.")
    after = bytearray(before)
    core.put_be16(after, PAGES, sum(1 << (p - 1) for p in pages))
    _commit(dev, before, after, args, ["display pages: %s -> %s" % (text(old), text(pages))])


def cmd_page_change(dev, args):
    before = dev.read()

    def text(value):
        return "off" if value == PAGE_CHANGE_OFF else "every %d s" % value
    if args.seconds is None:
        print("display page change: %s" % text(before[PAGE_INTERVAL]))
        return
    if args.seconds == "off":
        value = PAGE_CHANGE_OFF
    else:
        try:
            value = int(args.seconds)
        except ValueError:
            value = 0
        if not 3 <= value <= 60:
            sys.exit("Page change is 3-60 seconds, or 'off'.")
    after = bytearray(before)
    after[PAGE_INTERVAL] = value
    _commit(dev, before, after, args, ["display page change: %s -> %s"
                                       % (text(before[PAGE_INTERVAL]), text(value))])


CHART_SOURCE_BY_NAME = {v.replace(" ", "-"): k for k, v in CHART_SOURCES.items()}


def cmd_chart(dev, args):
    before = dev.read()
    base = CHARTS + 4 * (args.chart - 1)
    source, interval = be16(before, base), be16(before, base + 2)

    def text(src, ivl):
        return "%s, %s" % (_name(CHART_SOURCES, src), chart_interval_text(ivl))
    if args.source is None and args.interval is None:
        print("display chart %d: %s" % (args.chart, text(source, interval)))
        return
    after = bytearray(before)
    if args.source is not None:
        core.put_be16(after, base, CHART_SOURCE_BY_NAME[args.source])
    if args.interval is not None:
        stored = {v: k for k, v in CHART_INTERVALS.items()}.get(args.interval)
        if stored is None:
            sys.exit("Chart intervals are Aquasuite's eight: %s seconds."
                     % ", ".join("%g" % v for v in sorted(CHART_INTERVALS.values())))
        core.put_be16(after, base + 2, stored)
    _commit(dev, before, after, args, [
        "display chart %d: %s -> %s" % (args.chart, text(source, interval),
                                        text(be16(after, base), be16(after, base + 2)))])


# Aquasuite's own limit, both ends written in
# ../usb-captures/11-highflow-flow-calibration-min-max. The unit the software
# puts beside the field was not recorded; the device stores the value x100.
CALIBRATION_LIMIT = 50.0
# Aquasuite's range for the flow rates, read off its UI by the owner rather than
# from a capture. They must rise, which is how every capture has them and what
# the interpolation between them needs; whether Aquasuite enforces that too is
# not known.
POINT_MIN, POINT_MAX = 0.0, 750.0


def _ten(text, what):
    try:
        values = [float(v) for v in text.split(",")]
    except ValueError:
        values = []
    if len(values) != CALIBRATION_COUNT:
        sys.exit("Give all ten %s, comma separated. 'flow calibration' shows the "
                 "current ones." % what)
    return values


def _pair(old, new, fmt):
    """'20.0' if it stays, '20.0 -> 31.0' if it moves."""
    return fmt % old if old == new else (fmt + " -> " + fmt) % (old, new)


def cmd_calibration(dev, args):
    """The ten flow rates and the correction at each. Both arrays are confirmed
    by 10-highflow-flow-calibration in ../usb-captures/, where Aquasuite moved
    every entry of both."""
    before = dev.read()
    s = decode(before)
    rates = [p / 10.0 for p in s["calibration_points"]]
    values = [c / 100.0 for c in s["calibration"]]
    after = bytearray(before)

    if args.points is not None:
        new_rates = _ten(args.points, "flow rates, in l/h")
        if any(not POINT_MIN <= r <= POINT_MAX for r in new_rates):
            sys.exit("Flow rates are %g-%g l/h, the range Aquasuite allows."
                     % (POINT_MIN, POINT_MAX))
        if any(b <= a for a, b in zip(new_rates, new_rates[1:])):
            sys.exit("The ten flow rates have to rise: the device interpolates "
                     "between them.\nGot %s." % ", ".join("%g" % r for r in new_rates))
        for i, rate in enumerate(new_rates):
            core.put_be16(after, CALIBRATION_POINTS + 2 * i, int(round(rate * 10)))
    else:
        new_rates = rates

    if args.corrections is not None:
        new_values = _ten(args.corrections, "corrections")
        if any(abs(v) > CALIBRATION_LIMIT for v in new_values):
            sys.exit("Corrections are %+g to %+g, the range Aquasuite allows."
                     % (-CALIBRATION_LIMIT, CALIBRATION_LIMIT))
        for i, value in enumerate(new_values):
            struct.pack_into(">h", after, CALIBRATION + 2 * i, int(round(value * 100)))
    else:
        new_values = values

    lines = ["flow calibration", "   #   flow rate            correction"]
    for i in range(CALIBRATION_COUNT):
        lines.append("  %2d   %-20s %s"
                     % (i + 1, _pair(rates[i], new_rates[i], "%g") + " l/h",
                        _pair(values[i], new_values[i], "%+.2f")))
    if args.points is None and args.corrections is None:
        print("\n".join(lines))
        return
    _commit(dev, before, after, args, lines)


def cmd_offset(dev, args):
    before = dev.read()
    off = OFFSET_INTERNAL if args.sensor == "internal" else OFFSET_EXTERNAL
    current = core.sbe16(before, off) / 100.0
    if args.celsius is None:
        print("sensor %s offset: %+.2f C" % (args.sensor, current))
        return
    if not -15.0 <= args.celsius <= 15.0:
        sys.exit("Offset must be within +/-15.00 C (manual, 8.3 and 8.4).")
    after = bytearray(before)
    struct.pack_into(">h", after, off, int(round(args.celsius * 100)))
    _commit(dev, before, after, args, ["sensor %s offset: %+.2f C -> %+.2f C"
                                       % (args.sensor, current, args.celsius)])


def cmd_water_quality(dev, args):
    before = dev.read()
    good, bad = be16(before, WATER_QUALITY_GOOD) / 10.0, be16(before, WATER_QUALITY_BAD) / 10.0
    if args.good is None:
        print("water quality: 100 %% at %.1f uS/cm, 0 %% at %.1f uS/cm" % (good, bad))
        return
    if args.bad is None:
        sys.exit("Give both points: GOOD (100 %) and BAD (0 %), in uS/cm.")
    # The manual gives no range; this is the report's own limit and the order
    # the formula needs.
    if not 0 < args.good < args.bad <= 6553.5:
        sys.exit("The 100 % point must be above 0 and below the 0 % point, e.g. 12.8 50.")
    after = bytearray(before)
    core.put_be16(after, WATER_QUALITY_GOOD, int(round(args.good * 10)))
    core.put_be16(after, WATER_QUALITY_BAD, int(round(args.bad * 10)))
    _commit(dev, before, after, args, [
        "water quality: 100 %% at %.1f -> %.1f uS/cm, 0 %% at %.1f -> %.1f uS/cm"
        % (good, args.good, bad, args.bad)])


def cmd_alarm(dev, args):
    alarm = ALARM_BY_KEY[args.alarm]
    before = dev.read()
    on = bool(before[ALARM_ENABLE] & alarm.bit)
    limit = alarm.limit(before)
    shown = (alarm.fmt % limit) + " " + alarm.unit
    if args.state is None and args.limit is None:
        print("alarm %s: %s, fires when the reading is %s %s"
              % (alarm.key, _on(on), "below" if alarm.below else "above", shown))
        return
    after = bytearray(before)
    lines = []
    if args.state is not None:
        if args.state == "on":
            after[ALARM_ENABLE] |= alarm.bit
        else:
            after[ALARM_ENABLE] &= ~alarm.bit & 0xFF
        lines.append("alarm %s: %s -> %s" % (alarm.key, _on(on), args.state))
    if args.limit is not None:
        if not alarm.lo <= args.limit <= alarm.hi:
            sys.exit("The %s alarm limit is %g-%g %s." % (alarm.key, alarm.lo, alarm.hi,
                                                       alarm.unit))
        core.put_be16(after, alarm.offset, int(round(args.limit * alarm.scale)))
        lines.append("alarm %s limit: %s -> %s %s"
                     % (alarm.key, shown, alarm.fmt % args.limit, alarm.unit))
    _commit(dev, before, after, args, lines)


def cmd_alarm_delay(dev, args):
    before = dev.read()
    if args.seconds is None:
        print("alarms are ignored for %d s after startup" % before[ALARM_DELAY])
        return
    lo, hi = ALARM_DELAY_RANGE
    if not lo <= args.seconds <= hi:
        sys.exit("The startup delay is %d-%d seconds." % (lo, hi))
    after = bytearray(before)
    after[ALARM_DELAY] = args.seconds
    _commit(dev, before, after, args, ["alarm startup delay: %d s -> %d s"
                                       % (before[ALARM_DELAY], args.seconds)])


SIGNAL_MODE_BY_NAME = {cli: mode for mode, (cli, _text) in SIGNAL_MODES.items()}


def cmd_signal_output(dev, args):
    before = dev.read()
    old = before[SIGNAL_OUTPUT]
    old_cli, old_text = SIGNAL_MODES.get(old, ("unknown", "mode %d" % old))
    if args.mode is None:
        print("signal output: %s (%s)" % (old_cli, old_text))
        return
    after = bytearray(before)
    after[SIGNAL_OUTPUT] = SIGNAL_MODE_BY_NAME[args.mode]
    _commit(dev, before, after, args, [
        "signal output: %s -> %s" % (old_cli, args.mode),
        "  %s" % SIGNAL_MODES[SIGNAL_MODE_BY_NAME[args.mode]][1]])


def cmd_usb_current(dev, args):
    before = dev.read()
    current = be16(before, USB_CURRENT)
    if args.milliamps is None:
        print("USB current limit: %d mA%s" % (current, ", above the USB limit allowed"
                                               if before[USB_OVER_SPEC] else ""))
        return
    if not USB_SPEC_LIMIT <= args.milliamps <= USB_MAX or args.milliamps % 100:
        sys.exit("The USB current limit is 500-2000 mA, in steps of 100 mA.")
    after = bytearray(before)
    core.put_be16(after, USB_CURRENT, args.milliamps)
    # Above 500 mA the device also needs its permission flag, which Aquasuite
    # sets with a checkbox. At 500 mA or less it is cleared, as it was from the
    # factory.
    after[USB_OVER_SPEC] = 1 if args.milliamps > USB_SPEC_LIMIT else 0
    _usb_guard(dev, before, after, args)
    _commit(dev, before, after, args, ["USB current limit: %d mA -> %d mA"
                                       % (current, args.milliamps)])


def cmd_aquabus(dev, args):
    before = dev.read()
    if args.address is None:
        print("aquabus address: %d" % before[AQUABUS_ADDRESS])
        return
    after = bytearray(before)
    after[AQUABUS_ADDRESS] = args.address
    _commit(dev, before, after, args, ["aquabus address: %d -> %d"
                                       % (before[AQUABUS_ADDRESS], args.address)])


def cmd_volume_reset(dev, args):
    """Reset the volume counter: command 0x64, as Aquasuite sends it. It sets
    volume, impulse count and time since reset to 0, and cannot be undone, so
    it always asks - -y does not skip that."""
    buf = dev.read_input(INPUT_REPORT_SIZE)
    if buf is None:
        sys.exit("Report 0x01 did not arrive, so the counters cannot be shown before\n"
                 "the reset, nor the reset checked after it. Nothing sent.")
    live = decode_live(buf)
    print("volume counter: %d l, %d impulses, %s since the last reset"
          % (live["volume"], live["impulses"], _duration(live["since_reset"])))
    print("A reset sets all three to 0. It cannot be undone.")
    if args.dry_run:
        print("--dry-run: nothing sent.")
        return
    if args.yes:
        print("(-y does not apply here: a reset cannot be undone, so it always asks.)")
    try:
        answer = input("Reset the volume counter? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        sys.exit("Aborted.")
    dev.command(VOLUME_RESET_COMMAND)
    buf = dev.read_input(INPUT_REPORT_SIZE)
    after = decode_live(buf) if buf else None
    if after is not None and after["volume"] == 0 and after["since_reset"] < 10:
        print("Reset: %d l, %d impulses, %s since the reset."
              % (after["volume"], after["impulses"], _duration(after["since_reset"])))
    else:
        print("WARNING: the next live report does not show a reset counter%s."
              % (": %d l, %s" % (after["volume"], _duration(after["since_reset"]))
                 if after else " (none arrived)"))


# ------------------------------------------------------------------ parser

def _epilog(prefix):
    return """\
vocabulary
  channel     an RGBpx header: 1 is the external header (up to 90 LEDs),
              2 the 10 LEDs built into the top
  controller  one RGB config slot - "on channel N, LEDs A-B, effect X".
              Channel 1 has controllers 1-6, channel 2 has 7-8; 'info rgb'
              lists the ones in use with their numbers.

Leaving out a command's value shows the current one.

write flags  (every command that changes something accepts these)
  -n, --dry-run   read the device, show what would change, write nothing.
                  Implies --verbose.
  -v, --verbose   also print the byte-level diff of the report
  -y, --yes       skip the confirmation prompt (not for 'volume reset')
  --backup FILE   where to put the automatic pre-write backup
  --force         go past a safety check below; the refusal says which

safety checks  (each refuses without --force, and says why)
  system usb-current above 500 mA: it can damage the motherboard's USB header.
  signal-output power-switch, on-during-alarm or off-during-alarm, leaving
    one of them, or any alarm change while in one: an alarm can then switch
    the PC off or cut its power, and the device cannot say what is attached.
  An alarm that would fire at once: turning it on, or changing its limit,
    offset or water-quality points, while its reading is missing or past the
    limit. Flow and water quality alarm below their limit, temperatures
    above it.
  volume reset always asks, even with -y: it cannot be undone.

examples
  {p} info
  {p} display pages 2,3,13
  {p} alarm flow on --limit 60
  {p} sensor offset internal -0.5
  {p} rgb create 2 pos 1-10 --effect static --colour 0000FF
  {p} name sensor internal "Cold Point"

Every write is preceded by an automatic backup into
<your home>/.local/share/aqdctl/ (correct under sudo).""".format(p=prefix)


def _choice(sub, name, help, offset, table, label, **kw):
    p = core.add_write_flags(sub.add_parser(name, help=help, **kw))
    p.add_argument("value", nargs="?", choices=[table[k] for k in sorted(table)],
                   help="omit to show the current one")
    p.set_defaults(func=cmd_choice, offset=offset, table=table, label=label)
    return p


def _switch(sub, name, help, offset, bit, label, inverted=False, force=None, **kw):
    p = core.add_write_flags(sub.add_parser(name, help=help, **kw), force=force)
    p.add_argument("state", nargs="?", choices=["on", "off"],
                   help="omit to show the current state")
    p.set_defaults(func=cmd_switch, offset=offset, bit=bit, label=label,
                   inverted=inverted)
    return p


def build_parser(prefix):
    ap = argparse.ArgumentParser(
        prog=prefix, formatter_class=core.HelpFormatter,
        description="Read and modify the configuration of this Aquacomputer high "
                    "flow NEXT.",
        epilog=_epilog(prefix))
    sub = ap.add_subparsers(dest="group")
    writer = core.add_write_flags
    alarm_force = "go past a safety check (see 'alarm --help')"

    # ---------------------------------------------------------------- info
    p = sub.add_parser("info", help="show configuration and live readings",
                       description="Print what the high flow NEXT is configured to "
                                   "do, and what it reads right now. The live "
                                   "readings come from the report the device sends "
                                   "every second, so nothing is sent to it for them.")
    p.add_argument("section", nargs="?", choices=SECTIONS,
                   help="limit the output to one section (default: all)")
    p.set_defaults(func=cmd_info)

    # ------------------------------------------------------------- display
    display = sub.add_parser("display", help="the built-in display",
                             description="Brightness, pages, orientation, units and "
                                         "the four charts of the built-in display.")
    display.set_defaults(_helper=display)
    dsub = display.add_subparsers(dest="displaycmd")
    _choice(dsub, "brightness", "brightness while in use", BRIGHTNESS,
            BRIGHTNESS_LEVELS, "display brightness")
    _choice(dsub, "idle-brightness", "brightness when idle", IDLE_BRIGHTNESS,
            IDLE_LEVELS, "display idle brightness")
    p = writer(dsub.add_parser(
        "pages", formatter_class=core.HelpFormatter, help="which pages it shows",
        description="The pages the display cycles through.",
        epilog="pages\n" + "\n".join("  %2d  %s" % (n, PAGE_NAMES[n])
                                     for n in sorted(PAGE_NAMES))
               + "\n\nexample:  %s display pages 2,3,13" % prefix))
    p.add_argument("pages", nargs="?", metavar="N[,N...]",
                   help="page numbers, comma separated (omit to show)")
    p.set_defaults(func=cmd_pages)
    p = writer(dsub.add_parser("page-change", help="seconds between pages, or off"))
    p.add_argument("seconds", nargs="?", metavar="SECONDS|off",
                   help="3-60, or 'off' (omit to show)")
    p.set_defaults(func=cmd_page_change)
    _switch(dsub, "rotate", "turn the picture upside down", DISPLAY_FLAGS,
            DISPLAY_BITS["rotate"], "display rotate")
    _switch(dsub, "invert", "invert the picture permanently", DISPLAY_FLAGS,
            DISPLAY_BITS["invert"], "display invert")
    _switch(dsub, "auto-invert", "invert it half of the time, so the OLED wears evenly",
            DISPLAY_FLAGS, DISPLAY_BITS["auto-invert"], "display auto-invert")
    _switch(dsub, "device-keys", "the keys on the device", DISPLAY_FLAGS,
            DISPLAY_BITS["device-keys-disabled"], "display device keys", inverted=True)
    _switch(dsub, "menu-lock", "lock the device's menu", DISPLAY_FLAGS,
            DISPLAY_BITS["menu-lock"], "display menu lock")
    units = dsub.add_parser("units", help="units on the display",
                            description="Units the display uses. They change only the "
                                        "display, not what the device stores.")
    units.set_defaults(_helper=units)
    usub = units.add_subparsers(dest="unitscmd")
    _choice(usub, "temperature", "celsius or fahrenheit", TEMPERATURE_UNIT,
            TEMPERATURE_UNITS, "display temperature unit")
    _choice(usub, "flow", "litres or US gallons", FLOW_UNIT, FLOW_UNITS,
            "display flow unit")
    p = writer(dsub.add_parser(
        "chart", formatter_class=core.HelpFormatter, help="what a chart shows",
        description="The source and time interval of one of the four chart pages "
                    "(13-16). Give either or both; give neither to show them.",
        epilog="sources:   %s\nintervals: %s seconds\n\nexample:  %s display chart 1 "
               "--source flow --interval 60"
               % (", ".join(sorted(CHART_SOURCE_BY_NAME, key=CHART_SOURCE_BY_NAME.get)),
                  ", ".join("%g" % v for v in sorted(CHART_INTERVALS.values())), prefix)))
    p.add_argument("chart", type=int, choices=range(1, CHART_COUNT + 1), metavar="CHART",
                   help="1-4")
    p.add_argument("--source", choices=sorted(CHART_SOURCE_BY_NAME,
                                              key=CHART_SOURCE_BY_NAME.get),
                   metavar="SOURCE", help="what it plots")
    p.add_argument("--interval", type=float, metavar="SECONDS",
                   help="log interval: the time between stored points, which sets "
                        "the period the chart spans (one of Aquasuite's eight)")
    p.set_defaults(func=cmd_chart)

    # ---------------------------------------------------------------- flow
    flow = sub.add_parser("flow", help="how the flow is calculated",
                          description="Coolant, connector and manual calibration: "
                                      "the flow calculation's inputs.")
    flow.set_defaults(_helper=flow)
    fsub = flow.add_subparsers(dest="flowcmd")
    _choice(fsub, "coolant", "the coolant in the loop", COOLANT,
            {0: "dp-ultra", 1: "distilled"}, "flow coolant")
    _choice(fsub, "connector", "the inner diameter of the connectors", CONNECTOR,
            {0: "over-7mm", 1: "under-7mm"}, "flow connector")
    p = writer(fsub.add_parser(
        "calibration", formatter_class=core.HelpFormatter,
        help="the ten flow rates and the correction at each",
        description="Aquasuite's manual calibration: ten flow rates and the "
                    "correction applied at each of them. Both are settable, ten "
                    "values at a time; with neither flag the table is printed. "
                    "The flow alarm sees the corrected flow.",
        epilog="The factory rates are 20,30,50,70,100,125,150,200,250,300 l/h.\n\n"
               "examples:\n"
               "  {p} flow calibration\n"
               "  {p} flow calibration --corrections -1,1,1,0,0,0,0,0,0,0\n"
               "  {p} flow calibration --points 20,31,51,70,99,124,149,202,248,300"
               .format(p=prefix)))
    p.add_argument("--points", metavar="P1,...,P10",
                   help="the ten flow rates in l/h, %g-%g and rising"
                        % (POINT_MIN, POINT_MAX))
    p.add_argument("--corrections", metavar="C1,...,C10",
                   help="the correction at each rate, %+g to %+g"
                        % (-CALIBRATION_LIMIT, CALIBRATION_LIMIT))
    p.set_defaults(func=cmd_calibration)

    # -------------------------------------------------------------- sensor
    sensor = sub.add_parser("sensor", help="temperature offsets and water quality",
                            description="Calibration of the temperature sensors, and "
                                        "the conductivity range water quality is "
                                        "computed from.")
    sensor.set_defaults(_helper=sensor)
    ssub = sensor.add_subparsers(dest="sensorcmd")
    p = writer(ssub.add_parser(
        "offset", formatter_class=core.HelpFormatter,
        help="calibration offset of a temperature sensor",
        description="Stored on the device and added to the sensor's readings at "
                    "once. Refused if it would make an enabled temperature alarm "
                    "fire.",
        epilog="examples:\n  {p} sensor offset internal -0.5\n"
               "  {p} sensor offset external      (show it)".format(p=prefix)),
        force="write although it makes an alarm fire")
    p.add_argument("sensor", choices=["internal", "external"])
    p.add_argument("celsius", type=float, nargs="?", metavar="CELSIUS",
                   help="-15.00 to +15.00 (omit to show)")
    p.set_defaults(func=cmd_offset)
    p = writer(ssub.add_parser(
        "water-quality", formatter_class=core.HelpFormatter,
        help="conductivity at 100 %% and 0 %% water quality",
        description="Water quality is linear in conductivity between these two "
                    "points. Refused if it would make an enabled water-quality "
                    "alarm fire.",
        epilog="example:  %s sensor water-quality 12.8 50" % prefix),
        force="write although it makes an alarm fire")
    p.add_argument("good", type=float, nargs="?", metavar="GOOD",
                   help="uS/cm at 100 %% (omit both to show)")
    p.add_argument("bad", type=float, nargs="?", metavar="BAD", help="uS/cm at 0 %%")
    p.set_defaults(func=cmd_water_quality)

    # --------------------------------------------------------------- alarm
    alarm = sub.add_parser(
        "alarm", formatter_class=core.HelpFormatter, help="alarms and what they do",
        description="Four alarms, their limits, and the actions during an alarm. "
                    "Flow and water quality alarm below their limit, temperatures "
                    "above it. Turning an alarm on, or changing its limit, is "
                    "refused while its reading is missing or already past the limit, "
                    "because the device raises it at once; so is any alarm change "
                    "while the signal output is in a power mode.")
    alarm.set_defaults(_helper=alarm)
    asub = alarm.add_subparsers(dest="alarmcmd")
    metavars = {"l/h": "LITRES_PER_HOUR", "C": "CELSIUS", "%": "PERCENT"}
    examples = {"flow": 60, "internal": 45, "external": 45, "water-quality": 30}
    for a in ALARMS:
        p = writer(asub.add_parser(
            a.key, formatter_class=core.HelpFormatter,
            help="%s alarm: on/off and limit (%g-%g %s)"
                 % (a.key, a.lo, a.hi, a.unit.replace("%", "%%")),
            description="Turn the %s alarm on or off, change its limit, or both. It "
                        "fires when the reading %s the limit. Refused without --force "
                        "if the reading is missing or already past the limit, because "
                        "the device raises the alarm at once, and while the signal "
                        "output is in a power mode."
                        % (a.key, "drops below" if a.below else "rises above"),
            epilog="examples:\n  {p} alarm {k} on --limit {v}\n  {p} alarm {k} --limit {v}"
                   "      (only the limit)\n  {p} alarm {k}                 (show it)"
                   .format(p=prefix, k=a.key, v=examples[a.key])),
            force=alarm_force)
        p.add_argument("state", nargs="?", choices=["on", "off"])
        p.add_argument("--limit", type=float, metavar=metavars[a.unit],
                       help="%g-%g %s" % (a.lo, a.hi, a.unit.replace("%", "%%")))
        p.set_defaults(func=cmd_alarm, alarm=a.key)
    p = writer(asub.add_parser("startup-delay", help="seconds alarms are ignored "
                                                     "after startup (5-100)",
                               description="How long alarms are ignored after the "
                                           "device starts, so a pump that is still "
                                           "starting does not raise the flow alarm."),
               force=alarm_force)
    p.add_argument("seconds", type=int, nargs="?", metavar="SECONDS")
    p.set_defaults(func=cmd_alarm_delay)
    for key, help in (("buzzer", "sound the buzzer during an alarm"),
                      ("blink-led", "blink the internal LED red during an alarm"),
                      ("stop-signal", "disable the fan speed/flow signal output "
                                      "during an alarm")):
        _switch(asub, key, help, ALARM_ACTIONS, ALARM_ACTION_BITS[key],
                "alarm %s" % key, force=alarm_force)

    # ------------------------------------------------------- signal output
    p = writer(sub.add_parser(
        "signal-output", formatter_class=core.HelpFormatter,
        help="what the signal header puts out",
        description="What the 'signal' header puts out. The last three modes can "
                    "switch the PC off or cut its power during an alarm, so "
                    "entering or leaving them needs --force.",
        epilog="modes, with Aquasuite's name for each (manual, 11.1)\n"
               + "\n".join("  %-20s %s" % (cli, text)
                           for _m, (cli, text) in sorted(SIGNAL_MODES.items()))),
        force="enter or leave a power mode")
    p.add_argument("mode", nargs="?", choices=[SIGNAL_MODES[m][0] for m in sorted(SIGNAL_MODES)],
                   metavar="MODE", help="omit to show the current one")
    p.set_defaults(func=cmd_signal_output)

    # -------------------------------------------------------------- system
    system = sub.add_parser("system", help="USB current, standby, aquabus",
                            description="USB power, standby behaviour and the "
                                        "aquabus address.")
    system.set_defaults(_helper=system)
    ysub = system.add_subparsers(dest="systemcmd")
    p = writer(ysub.add_parser("usb-current", help="maximum current drawn from USB",
                               description="The most current the device draws from "
                                           "USB, for its LEDs. " + USB_HELP),
               force="allow more than 500 mA")
    p.add_argument("milliamps", type=int, nargs="?", metavar="MILLIAMPS",
                   help="500-2000, in steps of 100 (omit to show)")
    p.set_defaults(func=cmd_usb_current)
    standby = ysub.add_parser("standby", help="when the device may enter standby")
    standby.set_defaults(_helper=standby)
    stsub = standby.add_subparsers(dest="standbycmd")
    for key, help in (("when-usb-disconnected", "when USB is not connected"),
                      ("on-usb-suspend", "when a USB suspend is received"),
                      ("without-aquabus", "when there is no aquabus connection")):
        _switch(stsub, key, "allow standby " + help, STANDBY, STANDBY_BITS[key],
                "standby allowed %s" % key)
    instandby = ysub.add_parser("in-standby", help="what standby switches off")
    instandby.set_defaults(_helper=instandby)
    isub = instandby.add_subparsers(dest="instandbycmd")
    for key, help in (("alarms-off", "alarm detection"), ("display-off", "the display"),
                      ("leds-off", "the LEDs"), ("volume-stops", "the volume counter")):
        _switch(isub, key, "in standby, stop %s" % help, STANDBY, IN_STANDBY_BITS[key],
                "in standby %s" % key,
                force="change alarm detection while the signal output is in a "
                      "power mode" if key == "alarms-off" else None)
    p = writer(ysub.add_parser("aquabus-address", help="address on the aquabus",
                               description="Each high flow NEXT on one aquaero needs "
                                           "its own address (manual, 13.5)."))
    p.add_argument("address", type=int, nargs="?", choices=[58, 59, 60, 61],
                   metavar="ADDRESS", help="58-61 (omit to show)")
    p.set_defaults(func=cmd_aquabus)

    # -------------------------------------------------------------- volume
    volume = sub.add_parser("volume", help="the volume counter")
    volume.set_defaults(_helper=volume)
    vsub = volume.add_subparsers(dest="volumecmd")
    p = vsub.add_parser(
        "reset", help="set volume, impulses and time to 0",
        description="Reset the volume counter, as Aquasuite does. The current "
                    "counts are shown first. It cannot be undone, so it always "
                    "asks - -y does not skip that.")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show the counters and send nothing")
    p.add_argument("-y", "--yes", action="store_true",
                   help="accepted, but it does not skip the question")
    p.set_defaults(func=cmd_volume_reset)

    # ------------------------------------------------------ rgb and names
    rgbpx.register(sub, sys.modules[__name__], prefix)
    names.register(sub, sys.modules[__name__], prefix)

    # ------------------------------------------------------ backup/restore
    core.add_backup_restore(sub, sys.modules[__name__])
    return ap
