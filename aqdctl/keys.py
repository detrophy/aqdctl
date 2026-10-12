"""Dotted keys: every value aqdctl reads from a device, under a stable name.

A settable key is named after the command that sets it - display.brightness is
what 'display brightness' sets - and its value is written the way that command
takes it. Read-only keys (device.*, live.*) have no command. No key is the prefix of another, so a full key always
selects exactly one value.

The names are the contract scripts and other tools build on: renaming one
changes that contract."""

import json

from . import core

# Where a key's value comes from. Only the sources a request needs are read.
IDENTITY = "identity"       # what discovery already knows: serial and type
SETTINGS = "settings"       # feature report 0x03
NAMES = "names"             # feature report 0x08
LIVE = "live"               # the live readings
INPUT = "input"             # one input report 0x01, for the firmware version
CONFIG = "config"           # aqdctl's own config, not the device


class Key:
    """One value: its name, where it is read from, how to read it, and what it
    holds.

    type is one of: on/off, choice, integer, number, numbers (a list), points
    (TEMP:POWER pairs), colour, colours, leds (FIRST-LAST), source (a data
    source as --sensor takes it), text. fmt formats one number. choices lists
    the values a choice takes, and for integer and number the words allowed
    besides a number ('off'). low and high bound a number."""

    def __init__(self, name, source, read, type="number", unit="", fmt="%g",
                 choices=None, low=None, high=None, settable=True):
        self.name, self.source, self.read = name, source, read
        self.type, self.unit, self.fmt = type, unit, fmt
        self.choices, self.low, self.high = choices, low, high
        self.settable = settable

    def text(self, value):
        """The value as 'get' prints it."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if self.type == "on/off":
            return "on" if value else "off"
        if self.type in ("number", "integer"):
            return self.fmt % value
        if self.type == "numbers":
            return ",".join(self.fmt % v for v in value)
        if self.type == "points":
            return ",".join("%g:%g" % point for point in value)
        if self.type == "colours":
            return ",".join(value)
        return str(value)

    def json(self, value):
        """The value as JSON: numbers as numbers, on/off as a boolean, lists
        as arrays, a missing reading as null. Numbers carry the precision the
        text form has, so both say the same."""
        if value is None or isinstance(value, str):
            return value
        if self.type in ("number", "integer"):
            return _number(self.fmt % value)
        if self.type == "numbers":
            return [_number(self.fmt % v) for v in value]
        if self.type == "points":
            return [[_number("%g" % t), _number("%g" % p)] for t, p in value]
        return value

    def schema(self):
        """What a program needs to know about the key, without its value."""
        out = {"key": self.name, "type": self.type, "settable": self.settable}
        if self.unit:
            out["unit"] = self.unit
        if self.choices is not None:
            out["choices"] = list(self.choices)
        if self.low is not None:
            out["min"] = self.low
        if self.high is not None:
            out["max"] = self.high
        return out


def _number(text):
    value = float(text)
    return int(value) if value.is_integer() and "." not in text else value


class Family:
    """Keys that exist only for what the settings hold, such as one set per RGB
    controller in use. expand(ctx) lists them; template lists them once with
    placeholders (N, NAME) for the schema."""

    def __init__(self, prefix, expand, template):
        self.prefix, self.expand, self.template = prefix, expand, template


class Context:
    """The reports one request reads, each at most once and only when a key
    needs it. Unless `opened`, `dev` is opened on the first read that needs it
    and closed with the context, so keys that need no device - the Octo's live
    readings, for one - work without access to it."""

    def __init__(self, kind, serial, dev=None, opened=True):
        self.kind, self.serial, self.dev = kind, serial, dev
        self._opened, self._close = opened, False
        self._cache = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._close:
            self.dev.__exit__(*exc)

    def _device(self):
        if not self._opened:
            self.dev.__enter__()
            self._opened = self._close = True
        return self.dev

    def _load(self, what, load):
        if what not in self._cache:
            self._cache[what] = load()
        return self._cache[what]

    @property
    def settings(self):
        return self._load(SETTINGS, lambda: self._device().read())

    @property
    def names(self):
        return self._load(NAMES, lambda: self._device().read(core.LABEL_REPORT_ID))

    @property
    def live(self):
        return self._load(LIVE, lambda: self.kind.live_for_keys(self))

    @property
    def input(self):
        return self._load(INPUT, lambda: self._device().read_input(
            self.kind.INPUT_REPORT_SIZE))


def _under(name, prefix):
    """True if `name` is `prefix` or lies below it, by whole segments:
    live.flow is under live, live.flow-raw is not under live.flow."""
    return name == prefix or name.startswith(prefix + ".")


def select(table, ctx, prefixes=()):
    """The keys of `table` equal to or under one of `prefixes` (all of them for
    none), in table order. A family is only expanded, and its source only read,
    when a prefix can reach into it."""
    out = []
    for entry in table:
        if isinstance(entry, Family):
            if prefixes and not any(_under(entry.prefix, p) or _under(p, entry.prefix)
                                    for p in prefixes):
                continue
            candidates = entry.expand(ctx)
        else:
            candidates = [entry]
        out += [k for k in candidates
                if not prefixes or any(_under(k.name, p) for p in prefixes)]
    return out


def _matches(name, prefix):
    """_under for template names, where N and NAME stand for any one part:
    rgb.controller.N.param.NAME is under rgb.controller.3.param.speed's
    prefixes, and rgb.controller.3 is a prefix of it."""
    parts, want = name.split("."), prefix.split(".")
    return len(want) <= len(parts) and all(
        a == b or a in ("N", "NAME") for a, b in zip(parts, want))


def schema(table, prefixes=()):
    """The keys of a table, families by their template, for --keys."""
    out = []
    for entry in table:
        candidates = entry.template if isinstance(entry, Family) else [entry]
        out += [k for k in candidates
                if not prefixes or any(_matches(k.name, p) for p in prefixes)]
    return out


def read(table, ctx, prefixes=()):
    """[(key, value)] for the selected keys."""
    return [(k, k.read(ctx)) for k in select(table, ctx, prefixes)]


# --------------------------------------------------------------------- get

def _constraint(key):
    words = [str(c) for c in key.choices or ()]
    if key.low is not None and key.high is not None:
        words.insert(0, "%g..%g" % (key.low, key.high))
    return ",".join(words) or "-"


def cmd_get(dev, args):
    table = dev.kind.key_table()
    prefixes = args.prefix
    if args.keys:
        listed = schema(table, prefixes)
        _require_matches(dev, prefixes, [k.name for k in listed], _matches)
        if args.json:
            print(json.dumps([k.schema() for k in listed], indent=2, ensure_ascii=False))
            return
        width = max(len(k.name) for k in listed)
        for k in listed:
            print("%-*s  %-7s  %-10s  %-2s  %s"
                  % (width, k.name, k.type, k.unit or "-", "rw" if k.settable else "ro",
                     _constraint(k)))
        return
    with Context(dev.kind, dev.serial, dev, opened=False) as ctx:
        rows = read(table, ctx, prefixes)
    _require_matches(dev, prefixes, [k.name for k, _v in rows], _under)
    if args.json:
        if args.value:
            out = [k.json(v) for k, v in rows]
        else:
            out = {k.name: k.json(v) for k, v in rows}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    for k, v in rows:
        print(k.text(v) if args.value else "%s=%s" % (k.name, k.text(v)))


def _require_matches(dev, prefixes, found, match):
    """Each prefix has to select something: a script asking for a key that
    does not exist gets an error, not an empty answer."""
    for p in prefixes:
        if not any(match(name, p) for name in found):
            core.fail(core.EXIT_INVALID, "No key of this %s matches %r. 'get --keys' lists "
                      "them." % (dev.kind.TITLE, p))


def register(sub, prefix):
    p = sub.add_parser(
        "get", formatter_class=core.HelpFormatter,
        help="settings and readings as key=value lines, for scripts",
        description="Every key, or those equal to or under a PREFIX: 'get live' gives "
                    "every live reading, 'get alarm.flow' the flow alarm's state and "
                    "limit. One key=value per line and nothing else on stdout. A "
                    "settable key is named after the command that sets it; an empty "
                    "value is a missing reading. Only the reports the selected keys "
                    "need are read.",
        epilog="examples:\n"
               "  {p} get\n"
               "  {p} get live\n"
               "  {p} get --value live.flow\n"
               "  {p} get --json display alarm\n"
               "  {p} get --keys rgb".format(p=prefix))
    p.add_argument("prefix", nargs="*", metavar="PREFIX",
                   help="a key, or the start of keys, by whole dotted parts")
    p.add_argument("--value", action="store_true",
                   help="print only the values, one per line, in key order")
    p.add_argument("--json", action="store_true",
                   help="print a JSON object of keys and values; with --value, an "
                        "array of the values")
    p.add_argument("--keys", action="store_true",
                   help="list the keys instead of their values: type, unit, rw or "
                        "ro, and range or choices")
    p.set_defaults(func=cmd_get, needs_device=False)


# ------------------------------------------------- builders for the tables

def named(table, value):
    """The name a table gives a stored value; '#N' for one it does not know."""
    return table.get(value, "#%d" % value)


def choice(name, offset, table, unit=""):
    """A one-byte setting chosen from `table`, {stored: word}."""
    return Key(name, SETTINGS, lambda c: named(table, c.settings[offset]), "choice",
               unit=unit, choices=[table[k] for k in sorted(table)])


def switch(name, offset, bit, inverted=False):
    """One bit of a flags byte; `inverted`: the bit set means off."""
    return Key(name, SETTINGS, lambda c: bool(c.settings[offset] & bit) != inverted,
               "on/off")


def number(name, offset, scale, fmt, unit, low=None, high=None, signed=False):
    """A 16-bit big-endian number, stored as value x scale."""
    get = core.sbe16 if signed else core.be16
    return Key(name, SETTINGS, lambda c: get(c.settings, offset) / scale, "number",
               unit=unit, fmt=fmt, low=low, high=high)


def integer(name, read, unit="", low=None, high=None, source=SETTINGS,
            settable=True, choices=None):
    return Key(name, source, read, "integer", unit=unit, fmt="%d", low=low,
               high=high, settable=settable, choices=choices)


def identity(kind):
    """device.serial and device.type: known without reading anything."""
    return [Key("device.serial", IDENTITY, lambda c: c.serial, "text", settable=False),
            Key("device.type", IDENTITY, lambda c: kind.NAME, "text", settable=False)]


def firmware():
    """device.firmware, from one input report."""
    return integer("device.firmware",
                   lambda c: core.firmware(c.input) if c.input else None,
                   source=INPUT, settable=False)
