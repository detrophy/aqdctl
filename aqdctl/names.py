"""The name table (report 0x08), shared by every device that has one.

24-byte slots of NUL-padded single-byte text, after a 3-byte header. Each
device describes its groups with NameGroup: where a group starts, how many
slots it has, and - for groups whose members are roles rather than numbers,
like the high flow NEXT's sensors - what each slot is called."""

import sys

from . import core

LABEL_SIZE = 24
LABEL_MAX = LABEL_SIZE - 1        # keep a terminator
# Single-byte Latin-1, confirmed on hardware: renaming a channel to "hören"
# stored 68 f6 72 65 6e - five bytes with o-umlaut as 0xF6, not the six bytes
# UTF-8 would need. CP1252 is identical for everything except 0x80-0x9F, which
# no capture has exercised.
LABEL_ENCODING = "latin-1"


class NameGroup:
    def __init__(self, cli, base, count, what, members=None):
        self.cli, self.base, self.count, self.what = cli, base, count, what
        self.members = members        # None: addressed as 1..count

    def slot(self, index):
        """Offset of slot `index` (1-based) in the report."""
        return self.base + LABEL_SIZE * (index - 1)

    def index_of(self, key):
        """1-based slot for a CLI argument: a number, or a member's name."""
        if self.members is None:
            if not 1 <= key <= self.count:
                sys.exit("%s index must be 1-%d." % (self.cli, self.count))
            return key
        if key not in self.members:
            sys.exit("%s is one of: %s." % (self.cli, ", ".join(self.members)))
        return self.members.index(key) + 1

    def label(self, index):
        return self.members[index - 1] if self.members else "%d" % index


def read_name(buf, offset):
    raw = bytes(buf[offset:offset + LABEL_SIZE])
    return raw.split(b"\x00")[0].decode(LABEL_ENCODING, "replace")


def decode(buf, groups):
    """{group: [name, ...]} for every group of a device."""
    return {g.cli: [read_name(buf, g.slot(i)) for i in range(1, g.count + 1)]
            for g in groups}


def _group(kind, cli):
    for g in kind.NAME_GROUPS:
        if g.cli == cli:
            return g
    sys.exit("The %s has no name group %r." % (kind.TITLE, cli))


def cmd_name_list(dev, args):
    """Every name stored on the device, in the groups the CLI uses."""
    buf = dev.read(core.LABEL_REPORT_ID)
    for g in dev.kind.NAME_GROUPS:
        print("%s:" % g.cli)
        for i in range(1, g.count + 1):
            name = read_name(buf, g.slot(i))
            print("  %-11s %-13s %s" % (g.cli, g.label(i), name if name else "-"))


def cmd_name(dev, args):
    group = _group(dev.kind, args.group)
    index = group.index_of(args.index)
    buf = dev.read(core.LABEL_REPORT_ID)
    slot = group.slot(index)
    current = read_name(buf, slot)
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
    core.reseal(after)
    print("%s %s: %r -> %r" % (group.cli, group.label(index), current, args.text))
    core.commit(dev, buf, after, args)


def register(sub, kind, prefix):
    total = sum(g.count for g in kind.NAME_GROUPS)
    name = sub.add_parser(
        "name", formatter_class=core.HelpFormatter,
        help="names stored on the device",
        description="The %s stores names that the official software shows. "
                    "Groups: %s." % (kind.TITLE, ", ".join(
                        "%s (%s)" % (g.cli, g.what) for g in kind.NAME_GROUPS)))
    name.set_defaults(_helper=name)
    namesub = name.add_subparsers(dest="namegroup")
    p = namesub.add_parser("list", help="every stored name (%d)" % total)
    p.set_defaults(func=cmd_name_list)

    for g in kind.NAME_GROUPS:
        if g.members is None:
            example_key, key_help = "1", "1-%d" % g.count
        else:
            example_key, key_help = g.members[0], ", ".join(g.members)
        p = core.add_write_flags(namesub.add_parser(
            g.cli, formatter_class=core.HelpFormatter,
            help="%s names (%s)" % (g.cli, key_help),
            epilog="Names are single-byte text (latin-1), up to %d characters.\n\n"
                   "examples:\n"
                   "  %s name %s %s \"Front\"\n"
                   "  %s name %s %s            (read it)"
                   % (LABEL_MAX, prefix, g.cli, example_key, prefix, g.cli, example_key)))
        if g.members is None:
            p.add_argument("index", type=int, choices=range(1, g.count + 1),
                           metavar="INDEX", help=key_help)
        else:
            p.add_argument("index", choices=g.members, metavar="WHICH", help=key_help)
        p.add_argument("text", nargs="?", help="the new name (omit to read)")
        p.set_defaults(func=cmd_name, group=g.cli)
