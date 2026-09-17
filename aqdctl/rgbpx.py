"""RGBpx lighting, shared by every device with RGBpx headers.

A controller is one 70-byte config slot in the settings report: "on channel N,
LEDs A-B, run effect X, with these colours and parameters". The slot format is
the same on the Octo and the high flow NEXT; what differs is where the slots
start, how many there are, and which belong to which channel - RgbLayout.

Each channel owns a fixed range of slots. Both manuals say so (the Octo allows
"bis zu 6 LED-Controller" per output; the high flow NEXT "two for integrated
lighting, six for external components"), every active slot in all captures
fits, and even unused slots carry their channel's port byte. So a controller's
number tells you its channel, and a new one is only ever placed in its own
channel's slots."""

import sys

from . import core

RGB_STRIDE = 70
RGB_PORT, RGB_START, RGB_COUNT_OFF, RGB_MODE = 0, 1, 2, 3
# Second flags byte, holding the data-source control toggles. Both bits are
# confirmed: switching "control brightness by data source" off cleared 0x80
# and left 0x40 standing.
RGB_SOURCE_FLAGS = 4
RGB_SOURCE_FLAG_NAMES = {"source_speed": 0x40, "source_brightness": 0x80}
RGB_FLAGS_OFFSET = 5
RGB_SOURCE = 6        # BE16 data source: 0xFFFF none, else a sensor index
RGB_SOURCE_NONE = 0xFFFF
RGB_FILTER_RISE = 8   # u8, "filtering of fluctuating values" - rising
RGB_FILTER_FALL = 9   # u8, same, falling
# Two mapping blocks: input min/max as BE16, then output min and output max as
# single bytes. Block A drives speed, block B brightness. The input range comes
# from the chosen source (a temperature reads 20..70, the flow sensor 0..300);
# the output range is a fixed property of the effect - wave accepts 0..100 while
# breathing accepts 1..100, because a breathing speed of 0 would simply stop.
RGB_MAPS = ((10, 12, 14, 15), (16, 18, 20, 21))
# Parameters are BE16 words at +22+2k. Values below 256 decode identically
# under a LE16-at-+23 reading, so only an effect with a large value (colour
# gradient, limits up to 1000) distinguishes them - that capture settled it.
RGB_PARAM = 22
# The colour at +46 is HSV, not the Farbwerk 360's A/G/R/B palette: hue is a
# BE16 over 0..1535 (six 256-step sectors), then saturation and value as bytes.
# The official software's picker works in HSV as well, so what it shows as
# RRGGBB is the lossy view: a colour typed here as RRGGBB can come out one step
# off its saturation, without changing the colour the LEDs produce.
RGB_COLOUR = 46        # 6 palette entries of 4 bytes
RGB_PALETTE_ENTRIES = 6
HUE_FULL = 1536

# The master switch sits just before the slot array, and its sense is
# inverted: 0x00 means the LED function is ON. Same on both devices.
RGB_ENABLE_ON, RGB_ENABLE_OFF = 0x00, 0x02

# Effect ids and parameter names were first taken from the Farbwerk 360. Every
# id below has since been observed on the Octo and matched against the
# settings Aquasuite showed (captures 03-12, 19-28 in octo/).
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
# number with its "select a mode" placeholder. Probed directly on the Octo.
RGB_MODES_UNIMPLEMENTED = {0x06} | set(range(0x19, 0x21))
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
# Colour gradient, from five USB captures of Aquasuite in ../usb-captures/ and
# the owner's reading of its sliders:
#   2  rotation speed, moved 19 -> 20 -> 0 in 15-...-rotation-speed
#   3  the number of stops, the boundaries inside the gradient
#   4-6  where each stop sits. 12-...-775-up-back-down moved the first, and it
#      reads 775 in the software too, so a position is stored as shown;
#      16-...-add-remove-limits names all three, 194-388-775. Unused positions
#      repeat the last one.
# Parameters 0 and 1 keep their index; 1 is 1000 in every gradient seen.
RGB_PARAM_NAMES[0x21] = ["", "", "rotation_speed", "stops",
                         "stop1_position", "stop2_position", "stop3_position"]
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
# Both captured on the high flow NEXT, each switched on and off again:
# ../usb-captures/13-...-reverse-direction and 14-...-reverse-rotation. Bits
# 0x01, 0x02 and 0x04 are set in every gradient seen so far, on both devices,
# and no capture has moved them.
RGB_FLAG_NAMES[0x21] = {"reverse_direction": 0x08, "reverse_rotation": 0x10}
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
    # Colour gradient: 2 to 4 colours in entries 2-5, one more than the number
    # of stops. 16-highflow-rgb-gradient-add-remove-limits walks 3, 2 and 1
    # stops with 4, 3 and 2 colours; the Octo's three-stop gradient holds four.
    0x21: (False, 2, 4),
}
# Effects absent from the table take no user-settable colours: the rainbow family
# generates its own, and the audio/ambient ones are driven from the host. Colour
# gradient is the exception - it clearly uses the palette (the Octo's three-stop
# gradient holds red, green and blue in entries 2-4), but which entry belongs to
# which stop has not been captured, so it is listed apart rather than guessed.
# Where an effect's colours start in the palette. Colour gradient keeps entries
# 0 and 1 out of it: they read #000000 and #050505 in every gradient captured on
# either device, and no capture has moved them.
RGB_PALETTE_START = {0x21: 2}
# Effects whose unused palette entries repeat the last colour instead of being
# cleared, as the official software writes them.
RGB_PALETTE_PAD = {0x21}
# Effects that derive a parameter from the number of colours, other than the
# plain 'count': {mode: (parameter, offset)}. A gradient of n colours has n-1
# stops - the boundaries between them.

# The 'count' parameter of a variable-length effect is the length of its colour
# list, so the CLI derives it and never exposes it as a settable parameter.
RGB_COUNT_PARAM = "count"
RGB_DERIVED_COUNT = {0x21: ("stops", -1)}

# A controller slot as the device leaves it when nothing is configured: mode 0,
# one LED, no data source, filters at 10/15, both mapping blocks neutral. Taken
# from the unused slots of real captures. The first byte is the port, which
# empty_slot() sets to the slot's own channel, as the device keeps it.
RGB_SLOT_EMPTY = bytes.fromhex(
    "000001000000ffff0a0f00000064006400000064006400" + "00" * 47)


class RgbLayout:
    """Where one device keeps its RGBpx settings.

    channels maps each channel to (first slot, last slot, LEDs): the slots are
    that channel's controllers, numbered as Aquasuite numbers them. verified is
    the set of effect ids actually observed on this device."""

    def __init__(self, base, brightness, enable, channels, verified):
        self.base, self.brightness, self.enable = base, brightness, enable
        self.channels = channels
        self.verified = verified

    @property
    def count(self):
        return max(last for _first, last, _leds in self.channels.values())

    def slot_base(self, index):
        return self.base + RGB_STRIDE * (index - 1)

    def slots(self, channel):
        first, last, _leds = self.channels[channel]
        return range(first, last + 1)

    def channel_of(self, index):
        for channel, (first, last, _leds) in self.channels.items():
            if first <= index <= last:
                return channel
        raise ValueError("slot %d belongs to no channel" % index)

    def max_led(self, channel):
        return self.channels[channel][2]

    def describe(self):
        return "; ".join("channel %d: controllers %d-%d, up to %d LEDs"
                         % (ch, first, last, leds)
                         for ch, (first, last, leds) in sorted(self.channels.items()))


# ------------------------------------------------------------------ colour

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


def parse_hex_colour(text):
    t = text.lstrip("#")
    if len(t) != 6:
        sys.exit("Colour must be RRGGBB, e.g. FF0000.")
    try:
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        sys.exit("Colour must be RRGGBB, e.g. FF0000.")


# ------------------------------------------------------------------- slots

def get_param(buf, base, k):
    o = base + RGB_PARAM + 2 * k
    return (buf[o] << 8) | buf[o + 1]


def set_param(buf, base, k, value):
    o = base + RGB_PARAM + 2 * k
    buf[o] = (value >> 8) & 0xFF
    buf[o + 1] = value & 0xFF


def read_entry(buf, layout, index, entry=0):
    o = layout.slot_base(index) + RGB_COLOUR + 4 * entry
    return (buf[o] << 8) | buf[o + 1], buf[o + 2], buf[o + 3]


def _put_entry(buf, base, entry, rgb):
    h, sat, val = rgb_to_hsv(*rgb)
    o = base + RGB_COLOUR + 4 * entry
    buf[o], buf[o + 1], buf[o + 2], buf[o + 3] = (h >> 8) & 0xFF, h & 0xFF, sat, val


def palette_roles(mode):
    """Role of each palette entry, in order, for an effect. Derived from
    RGB_PALETTE_SPEC so it says the same thing as 'rgb effects' and as the
    validation in 'rgb create' and 'rgb set'."""
    start = RGB_PALETTE_START.get(mode, 0)
    has_bg, _lo, hi = RGB_PALETTE_SPEC.get(mode, (False, 0, 0))
    roles = ["not part of this effect"] * start + (["background"] if has_bg else [])
    # A single-colour effect just has "colour"; numbering one thing is noise.
    if hi == 1:
        return roles + ["colour"]
    return roles + ["colour %d" % (n + 1) for n in range(hi)]


def empty_slot(layout, index):
    """An unused slot, with the port byte of the channel the slot belongs to -
    as the device keeps its own unused slots."""
    slot = bytearray(RGB_SLOT_EMPTY)
    slot[RGB_PORT] = layout.channel_of(index) - 1
    return bytes(slot)


def parse_position(text, max_led):
    """'61-79' -> (start, count) with start 1-based inclusive, as shown by the
    official software. The report stores start 0-based; the caller converts."""
    part = str(text).split("-")
    if len(part) != 2:
        sys.exit("Position is FIRST-LAST, e.g. pos 1-15 for the first 15 LEDs.")
    try:
        first, last = int(part[0]), int(part[1])
    except ValueError:
        sys.exit("Position is FIRST-LAST, e.g. pos 1-15 for the first 15 LEDs.")
    if first < 1:
        sys.exit("LED positions start at 1.")
    if last < first:
        sys.exit("Position %s ends before it starts." % text)
    if last > max_led:
        sys.exit("The last LED on this channel is %d." % max_led)
    return first, last - first + 1


def controller_span(buf, layout, index):
    """(channel, first_led, last_led) of a slot, all 1-based, or None if unused."""
    base = layout.slot_base(index)
    if buf[base + RGB_MODE] == 0:
        return None
    start = buf[base + RGB_START] + 1
    return (buf[base + RGB_PORT] + 1, start, start + buf[base + RGB_COUNT_OFF] - 1)


def overlapping(buf, layout, channel, first, last, exclude=None):
    """Controllers on `channel` whose LEDs overlap first-last: [(n, lo, hi)]."""
    hits = []
    for i in range(1, layout.count + 1):
        span = controller_span(buf, layout, i)
        if i == exclude or span is None or span[0] != channel:
            continue
        if span[1] <= last and first <= span[2]:
            hits.append((i, span[1], span[2]))
    return hits


def free_slot(buf, layout, channel):
    """The channel's lowest unused controller number, or None."""
    for i in layout.slots(channel):
        if controller_span(buf, layout, i) is None:
            return i
    return None


def _refuse_overlap(clash, channel, first, last):
    n, lo, hi = clash[0]
    sys.exit("LEDs %d-%d on channel %d overlap controller %d (LEDs %d-%d).\n"
             "Aquasuite can layer overlapping controllers - the one higher in its\n"
             "list wins - but how its list order maps to controller numbers has not\n"
             "been captured, so aqdctl does not create overlaps. Resize the other\n"
             "controller with 'rgb set controller %d pos A-B', or remove it with\n"
             "'rgb remove controller %d'." % (first, last, channel, n, lo, hi, n, n))


def _controller_name(dev, index):
    for g in getattr(dev.kind, "NAME_GROUPS", ()):
        if g.cli == "controller":
            try:
                from . import names
                return names.read_name(dev.read(core.LABEL_REPORT_ID), g.slot(index))
            except Exception:
                return ""
    return ""


# ------------------------------------------------------------------ output

def describe_rgb(buf, layout, index, name, source_label):
    """One controller, in the vocabulary the commands use: its number, channel,
    position, effect, and colours by role. Positions are 1-based."""
    base = layout.slot_base(index)
    mode = buf[base + RGB_MODE]
    effect = RGB_MODES.get(mode)
    tag = effect if effect else ("unimplemented"
                                 if mode in RGB_MODES_UNIMPLEMENTED else "unknown")
    if effect and mode not in layout.verified:
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

    src = core.be16(buf, base + RGB_SOURCE)
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
                  % (tag_, core.be16(buf, base + lo), core.be16(buf, base + hi),
                     buf[base + omin], buf[base + omax]))

    roles = palette_roles(mode)
    # Where a count follows from the colours, the entries past it are copies the
    # official software leaves behind, not colours in use.
    used = None
    if mode in RGB_DERIVED_COUNT:
        key, offset = RGB_DERIVED_COUNT[mode]
        used = get_param(buf, base, RGB_PARAM_NAMES[mode].index(key)) - offset
    start = RGB_PALETTE_START.get(mode, 0)
    for e in range(RGB_PALETTE_ENTRIES):
        h, sat, val = read_entry(buf, layout, index, e)
        if not (h or sat or val):
            continue
        role = roles[e] if e < len(roles) else "stored, unused by this effect"
        if used is not None and start <= e < RGB_PALETTE_ENTRIES and e - start >= used:
            role += " (unused)"
        print("      %-12s #%02X%02X%02X" % (role + ":", *hsv_to_rgb(h, sat, val)))


def info_rgb(buf, layout, names, source_label):
    print("RGB  (%s)" % layout.describe())
    print("  function: %s     global brightness: %.0f%% (%d/255)"
          % ("on" if buf[layout.enable] == RGB_ENABLE_ON else "off",
             buf[layout.brightness] * 100.0 / 255, buf[layout.brightness]))
    used = False
    for i in range(1, layout.count + 1):
        if controller_span(buf, layout, i) is None:
            continue
        used = True
        describe_rgb(buf, layout, i, names[i - 1] if i <= len(names) else "",
                     source_label)
    if not used:
        print("  no controllers configured")
    print()
    print("  The number in front is the controller's: Aquasuite shows it as")
    print("  \"LED Controller n\", and 'name controller n' uses it too. Add one with")
    print("  'rgb create CH pos A-B', edit or resize one with 'rgb set controller N',")
    print("  and remove one with 'rgb remove controller N'.")


# ---------------------------------------------------------------- commands

def _lookup_effect(text):
    lookup = {v.replace(" ", "_"): k for k, v in RGB_MODES.items()}
    mode = lookup.get(text.lower().replace(" ", "_"))
    if mode is None:
        try:
            mode = int(text, 0)
        except ValueError:
            sys.exit("Unknown effect %r. Run 'rgb effects' for the list." % text)
    return mode


def _apply_settings(dev, buf, after, base, args):
    """Effect, data source, filters, colours, parameters and flags - the part
    that 'rgb create' and 'rgb set' share."""
    layout = dev.kind.RGB
    # Apply the effect first: parameter, flag and colour names are per-effect, so
    # "--effect wave --flag reverse" in one go must validate against wave.
    if args.effect is not None:
        mode = _lookup_effect(args.effect)
        if mode in RGB_MODES_UNIMPLEMENTED or mode not in RGB_MODES:
            print("Warning: %#04x is not an implemented effect on this firmware." % mode)
            print("         The device stores the byte without validating it, so this")
            print("         write will verify cleanly, but the LEDs will stay dark and")
            print("         Aquasuite will show a placeholder instead of an effect.")
        elif mode not in layout.verified:
            print("Note: effect %#04x has not been seen on the %s. Its id and"
                  % (mode, dev.kind.TITLE))
            print("      parameters come from the Octo, whose controllers use the")
            print("      same format.")
        if mode in RGB_MODES_HOST_DRIVEN:
            print("Note: effect %#04x is driven by an Aquasuite host DLL. The device" % mode)
            print("      stores it, but nothing animates without Aquasuite running,")
            print("      so on Linux the channel will simply sit at its background.")
        print("  effect: %s -> %s"
              % (RGB_MODES.get(buf[base + RGB_MODE], "none"),
                 RGB_MODES.get(mode, "%#04x" % mode)))
        after[base + RGB_MODE] = mode

    mode = after[base + RGB_MODE]

    if args.sensor is not None:
        if str(args.sensor).lower() in ("none", "off"):
            value = RGB_SOURCE_NONE
        else:
            value = dev.kind.parse_sensor(args.sensor)
        old = core.be16(after, base + RGB_SOURCE)
        core.put_be16(after, base + RGB_SOURCE, value)

        def _name(v):
            return "none" if v == RGB_SOURCE_NONE else dev.kind.source_label(v)
        print("  sensor: %s -> %s" % (_name(old), _name(value)))
        if value != RGB_SOURCE_NONE and old == RGB_SOURCE_NONE:
            print("    Aquasuite also rewrites both mapping ranges to 20-70 here;")
            print("    aqdctl leaves them alone - 'info rgb' shows the current ones.")
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

        entry = RGB_PALETTE_START.get(mode, 0)
        if has_bg:
            if args.background is not None:
                rgb = parse_hex_colour(args.background)
                _put_entry(after, base, entry, rgb)
                print("  background: #%02X%02X%02X" % rgb)
            entry += 1
        for n, rgb in enumerate(colours):
            _put_entry(after, base, entry + n, rgb)
            print("  colour %d: #%02X%02X%02X" % ((n + 1,) + rgb))
        if colours:
            # Past the list: repeat the last colour where the official software
            # does, otherwise clear the entries. Leaving stale colours behind
            # would show them back as if they were set.
            tail = base + RGB_COLOUR + 4 * (entry + len(colours) - 1)
            fill = bytes(after[tail:tail + 4]) if mode in RGB_PALETTE_PAD else b"\0" * 4
            for e in range(entry + len(colours), RGB_PALETTE_ENTRIES):
                o = base + RGB_COLOUR + 4 * e
                after[o:o + 4] = fill
            # A count that follows from the list is never typed by hand.
            names = RGB_PARAM_NAMES.get(mode, [])
            if RGB_COUNT_PARAM in names:
                set_param(after, base, names.index(RGB_COUNT_PARAM), len(colours))
            elif mode in RGB_DERIVED_COUNT:
                key, offset = RGB_DERIVED_COUNT[mode]
                set_param(after, base, names.index(key), len(colours) + offset)
                print("  %s: %d" % (key, len(colours) + offset))

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
        if mode in RGB_DERIVED_COUNT and key == RGB_DERIVED_COUNT[mode][0]:
            sys.exit("'%s' follows from the number of colours, so it comes from "
                     "--colour:\n%d colours make %d." % (key, 4, 4 + RGB_DERIVED_COUNT[mode][1]))
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


def cmd_rgb_create(dev, args):
    """A new controller on a channel, in the channel's lowest free slot."""
    layout = dev.kind.RGB
    if args.channel not in layout.channels:
        sys.exit("The %s's RGB channels are %s."
                 % (dev.kind.TITLE, ", ".join(map(str, sorted(layout.channels)))))
    if args.effect is None:
        sys.exit("A new controller needs an effect: add --effect NAME. "
                 "'rgb effects' lists them.")
    first, count = parse_position(args.pos, layout.max_led(args.channel))
    last = first + count - 1
    buf = dev.read()
    # Full comes first: when no slot is free, no range would help.
    index = free_slot(buf, layout, args.channel)
    if index is None:
        used = [str(i) for i in layout.slots(args.channel)]
        sys.exit("Channel %d is full: its controllers %s are all in use. Remove one\n"
                 "with 'rgb remove controller N' first."
                 % (args.channel, ", ".join(used)))
    clash = overlapping(buf, layout, args.channel, first, last)
    if clash:
        _refuse_overlap(clash, args.channel, first, last)
    base = layout.slot_base(index)
    after = bytearray(buf)
    after[base:base + RGB_STRIDE] = empty_slot(layout, index)
    after[base + RGB_PORT] = args.channel - 1
    after[base + RGB_START] = first - 1
    after[base + RGB_COUNT_OFF] = count
    print("controller %d (new) on channel %d, LEDs %d-%d, name %r"
          % (index, args.channel, first, last, _controller_name(dev, index)))
    _apply_settings(dev, buf, after, base, args)
    core.reseal(after)
    core.commit(dev, buf, after, args)


def cmd_rgb_set(dev, args):
    """Edit, resize or move an existing controller, addressed by its number."""
    layout = dev.kind.RGB
    index = args.controller
    if not 1 <= index <= layout.count:
        sys.exit("The %s's controllers are 1-%d." % (dev.kind.TITLE, layout.count))
    buf = dev.read()
    span = controller_span(buf, layout, index)
    if span is None:
        sys.exit("Controller %d is not in use. 'info rgb' lists the controllers in\n"
                 "use; 'rgb create CH pos A-B --effect NAME' adds one." % index)
    channel, lo, hi = span
    base = layout.slot_base(index)
    after = bytearray(buf)
    print("controller %d on channel %d, LEDs %d-%d" % (index, channel, lo, hi))
    if args.pos is not None:
        first, count = parse_position(args.pos, layout.max_led(channel))
        last = first + count - 1
        clash = overlapping(buf, layout, channel, first, last, exclude=index)
        if clash:
            _refuse_overlap(clash, channel, first, last)
        after[base + RGB_START] = first - 1
        after[base + RGB_COUNT_OFF] = count
        print("  LEDs: %d-%d -> %d-%d" % (lo, hi, first, last))
    _apply_settings(dev, buf, after, base, args)
    core.reseal(after)
    core.commit(dev, buf, after, args)


def cmd_rgb_remove(dev, args):
    """Clear one controller by number, or every controller on a channel."""
    layout = dev.kind.RGB
    buf = dev.read()
    after = bytearray(buf)
    if args.what == "controller":
        if not 1 <= args.number <= layout.count:
            sys.exit("The %s's controllers are 1-%d." % (dev.kind.TITLE, layout.count))
        if controller_span(buf, layout, args.number) is None:
            sys.exit("Controller %d is not in use; nothing to remove. 'info rgb' lists\n"
                     "the controllers in use." % args.number)
        targets = [args.number]
    else:
        if args.number not in layout.channels:
            sys.exit("The %s's RGB channels are %s."
                     % (dev.kind.TITLE, ", ".join(map(str, sorted(layout.channels)))))
        targets = [i for i in range(1, layout.count + 1)
                   if (controller_span(buf, layout, i) or (None,))[0] == args.number]
        if not targets:
            sys.exit("Channel %d has no controllers to remove." % args.number)
    for i in targets:
        span = controller_span(buf, layout, i)
        print("removing controller %d (channel %d, LEDs %d-%d)" % ((i,) + span))
        base = layout.slot_base(i)
        after[base:base + RGB_STRIDE] = empty_slot(layout, i)
    core.reseal(after)
    core.commit(dev, buf, after, args)


def cmd_rgb_switch(dev, args):
    """Turn the whole RGB function on or off."""
    layout = dev.kind.RGB
    buf = dev.read()
    after = bytearray(buf)
    after[layout.enable] = RGB_ENABLE_ON if args.state == "on" else RGB_ENABLE_OFF
    core.reseal(after)
    print("RGB function: %s -> %s"
          % ("on" if buf[layout.enable] == RGB_ENABLE_ON else "off", args.state))
    core.commit(dev, buf, after, args)


def cmd_rgb_brightness(dev, args):
    """Global brightness for every controller on every channel."""
    layout = dev.kind.RGB
    buf = dev.read()
    if not 0 <= args.percent <= 100:
        sys.exit("Brightness is a percentage, 0-100.")
    # Aquasuite's slider moves one byte per step (captured on both the Octo and
    # the high flow NEXT) and shows a whole percentage, so one shown value
    # covers several bytes: its "45" stored 114 on the Octo and 115 on the high
    # flow NEXT. There is no conversion rule to copy; store the byte nearest the
    # requested percentage.
    raw = int(args.percent * 255 / 100.0 + 0.5)
    after = bytearray(buf)
    after[layout.brightness] = raw
    core.reseal(after)
    print("global brightness: %.0f%% (%d) -> %.0f%% (%d)"
          % (buf[layout.brightness] * 100.0 / 255, buf[layout.brightness],
             raw * 100.0 / 255, raw))
    core.commit(dev, buf, after, args)


def cmd_rgb_effects(dev, args):
    """Print what each effect accepts. Needs no device.

    Generated from the same tables the writer validates against, so it cannot
    drift from what the tool will actually let you set."""
    layout, kind = dev.kind.RGB, dev.kind

    def describe(mode):
        name = RGB_MODES[mode]
        seen = "" if mode in layout.verified else \
            "   (not yet seen on the %s; id and parameters from the Octo)" % kind.TITLE
        print("%s  (%#04x)%s" % (name.replace(" ", "_"), mode, seen))
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
        derived = {RGB_COUNT_PARAM, RGB_DERIVED_COUNT.get(mode, ("",))[0]}
        names = [n for n in RGB_PARAM_NAMES.get(mode, []) if n and n not in derived]
        print("  parameters: %s"
              % (", ".join("%s=N" % n for n in names) if names else "none"))
        flags = sorted(RGB_FLAG_NAMES.get(mode, {}))
        print("  flags     : %s" % (", ".join(flags) if flags else "none"))
        print("  data source: --sensor %s," % kind.RGB_SOURCE_HELP)
        print("               with --filter-rise / --filter-fall and the")
        print("               source_speed / source_brightness flags")

    if args.effect:
        lookup = {v.replace(" ", "_"): k for k, v in RGB_MODES.items()}
        key = args.effect.lower().replace(" ", "_")
        if key not in lookup:
            sys.exit("Unknown effect %r. Run 'rgb effects' for the list." % args.effect)
        describe(lookup[key])
        return

    print("Effects, as stored in the report. Parameter values are whole numbers;")
    print("most are 0-100 in the official software's sliders.\n")
    for mode in sorted(RGB_MODES):
        if mode in RGB_MODES_UNIMPLEMENTED:
            continue
        describe(mode)
        print()
    print("Not implemented on this firmware: %s"
          % ", ".join("%#04x" % m for m in sorted(RGB_MODES_UNIMPLEMENTED)))


# ------------------------------------------------------------------ parser

def _effect_options(p, sensor_help, required_effect):
    p.add_argument("--effect", metavar="NAME", required=required_effect,
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
    p.add_argument("--sensor", metavar="S", help=sensor_help)
    p.add_argument("--filter-rise", type=int, dest="filter_rise", metavar="N",
                   help="damping for rising sensor values, 0-255")
    p.add_argument("--filter-fall", type=int, dest="filter_fall", metavar="N",
                   help="damping for falling sensor values, 0-255")


def register(sub, kind, prefix):
    layout = kind.RGB
    channels = sorted(layout.channels)
    rgb = sub.add_parser(
        "rgb", formatter_class=core.HelpFormatter,
        help="RGB channels %s" % "-".join(map(str, (channels[0], channels[-1]))),
        description="An RGB channel is one of the RGBpx headers. A controller is "
                    "one config slot saying \"on channel N, LEDs A-B, run effect "
                    "X\"; each channel has its own controller numbers (%s). "
                    "'info rgb' lists the controllers in use with their numbers."
                    % layout.describe())
    rgb.set_defaults(_helper=rgb)
    rgbsub = rgb.add_subparsers(dest="rgbcmd")
    sensor_help = "drive the effect from a sensor: %s" % kind.RGB_SOURCE_HELP

    p = core.add_write_flags(rgbsub.add_parser(
        "create", formatter_class=core.HelpFormatter,
        help="add a controller on a channel",
        description="Add a controller for a stretch of LEDs on a channel. It takes "
                    "the channel's lowest free controller number, and the output "
                    "says which. A range overlapping another controller is refused.",
        epilog="Colours are per-effect - run 'rgb effects NAME' to see what one\n"
               "accepts. Positions are 1-based and inclusive.\n\n"
               "examples:\n"
               "  %s rgb create 1 pos 1-15 --effect static --colour FF0000\n"
               "  %s rgb create 1 pos 16-30 --effect wave --background 0A0A0A \\\n"
               "        --colour FF0000,00FF00 --param speed=25" % (prefix, prefix)))
    p.add_argument("channel", type=int, choices=channels, metavar="CHANNEL",
                   help="RGBpx header: %s" % ", ".join(map(str, channels)))
    p.add_argument("pos_kw", metavar="pos", choices=["pos"], help="the word 'pos'")
    p.add_argument("pos", metavar="FIRST-LAST",
                   help="LED range on this channel, 1-based inclusive, e.g. 1-15")
    _effect_options(p, sensor_help, required_effect=True)
    p.set_defaults(func=cmd_rgb_create)

    p = core.add_write_flags(rgbsub.add_parser(
        "set", formatter_class=core.HelpFormatter,
        help="edit, resize or move one controller",
        description="Change an existing controller, addressed by its number. "
                    "'pos' gives it a new LED range on its own channel and keeps "
                    "its effect and colours; everything not mentioned stays.",
        epilog="examples:\n"
               "  %s rgb set controller 7 --colour 00FF00\n"
               "  %s rgb set controller 7 pos 61-85\n"
               "  %s rgb set controller 1 --sensor 1 --flag source_brightness"
               % (prefix, prefix, prefix)))
    p.add_argument("controller_kw", metavar="controller", choices=["controller"],
                   help="the word 'controller'")
    p.add_argument("controller", type=int, metavar="N",
                   choices=range(1, layout.count + 1),
                   help="controller number, 1-%d ('info rgb' lists them)" % layout.count)
    p.add_argument("pos_kw", metavar="pos", nargs="?", choices=["pos"],
                   help="the word 'pos', to give it a new LED range")
    p.add_argument("pos", metavar="FIRST-LAST", nargs="?",
                   help="new LED range on its channel, 1-based inclusive")
    _effect_options(p, sensor_help, required_effect=False)
    p.set_defaults(func=cmd_rgb_set)

    p = core.add_write_flags(rgbsub.add_parser(
        "remove", formatter_class=core.HelpFormatter,
        help="remove one controller, or every controller on a channel",
        description="Remove a controller by its number, or every controller on a "
                    "channel. The word 'controller' or 'channel' is required, so a "
                    "bare number can never clear a whole channel by mistake.",
        epilog="examples:\n"
               "  %s rgb remove controller 7\n"
               "  %s rgb remove channel 2" % (prefix, prefix)))
    p.add_argument("what", choices=["controller", "channel"],
                   help="'controller' and its number, or 'channel' and its number")
    p.add_argument("number", type=int, metavar="N")
    p.set_defaults(func=cmd_rgb_remove)

    p = core.add_write_flags(rgbsub.add_parser(
        "switch", help="turn the whole RGB function on or off"))
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_rgb_switch)

    p = core.add_write_flags(rgbsub.add_parser(
        "brightness", help="global brightness for every channel"))
    p.add_argument("percent", type=float, metavar="PERCENT", help="0-100")
    p.set_defaults(func=cmd_rgb_brightness)

    p = rgbsub.add_parser("effects", help="what each effect accepts",
                          description="Print the colours, parameters and flags "
                                      "each effect takes. Needs no device.")
    p.add_argument("effect", nargs="?", metavar="NAME",
                   help="one effect (omit for all of them)")
    p.set_defaults(func=cmd_rgb_effects, needs_device=False)
