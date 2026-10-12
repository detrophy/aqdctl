"""Many changes, one write.

'batch' takes key=value lines, the keys 'get' prints. Each line becomes the
command that sets its key, and runs as that command would, with the same
checks - but against a copy of the device's reports instead of the device.
When every line has run, the device's safety checks look at all the changes
together, and each changed report is written once: one backup, one
confirmation, one write, one save. A line that fails writes nothing at all.

A key whose value is the device's current one is left alone, and read-only
keys are skipped, so a batch made by 'get' changes nothing on the device it
came from."""

import contextlib
import io
import json
import re
import sys

from . import core, keys, rgbpx

# Fan mode settings, by the mode they belong to. sensor is shared: target and
# curve mode use the same stored sensor.
FAN_MODES = {"fixed": ["fixed.power"], "target": ["target.temperature", "sensor"],
             "curve": ["curve.points", "curve.startup", "sensor"], "follow": ["follow"]}
FAN_MODE_KEYS = {"mode"} | {k for ks in FAN_MODES.values() for k in ks}


class Staged:
    """The device as a batch's commands see it: a read returns the batch's
    copy of the report, read from the device once; a commit replaces the copy
    instead of writing it."""
    staged = True

    def __init__(self, dev):
        self.dev, self.kind, self.serial = dev, dev.kind, dev.serial
        self.original, self.reports = {}, {}

    def read(self, report_id=core.CTRL_REPORT_ID, check=True):
        if report_id not in self.reports:
            buf = bytes(self.dev.read(report_id))
            self.original[report_id] = buf
            self.reports[report_id] = bytearray(buf)
        return bytearray(self.reports[report_id])

    def stage(self, after):
        self.reports[after[0]] = bytearray(after)

    def read_input(self, size, timeout_ms=core.LIVE_WAIT_MS):
        return self.dev.read_input(size, timeout_ms)

    def changed(self):
        return [rid for rid in sorted(self.reports)
                if bytes(self.reports[rid]) != self.original[rid]]


class Line:
    def __init__(self, number, key, value):
        self.number, self.key, self.value = number, key, value

    def __str__(self):
        return "line %d (%s=%s)" % (self.number, self.key, self.value)


def _where(lines):
    if len(lines) == 1:
        return str(lines[0])
    return "lines %s" % ", ".join("%d (%s)" % (l.number, l.key) for l in lines)


def parse(text):
    """The key=value lines of a batch, without blank lines and # comments.
    A value runs to the end of its line, as 'get' prints it."""
    out, seen = [], {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.lstrip()
        if not line.strip() or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or " " in key:
            core.fail(core.EXIT_INVALID, "line %d: %r is not key=value." % (number, line))
        if key in seen:
            core.fail(core.EXIT_INVALID, "line %d: %s is already set on line %d."
                      % (number, key, seen[key]))
        seen[key] = number
        out.append(Line(number, key, value))
    return out


# ------------------------------------------------------ keys to commands

def _option(name, value):
    # --name=value, so a value starting with '-' is not read as an option
    return "--%s=%s" % (name, value)


# Keys one command sets together: (kind, pattern), the pattern's first group
# naming the command and its second the key within it.
GROUPS = [("rgb", r"(rgb\.controller\.\d+)\.(.+)$"),
          ("alarm", r"(alarm\.[a-z-]+)\.(state|limit)$"),
          ("chart", r"(display\.chart\.\d+)\.(source|interval)$"),
          ("calibration", r"(flow\.calibration)\.(points|corrections)$"),
          ("water-quality", r"(sensor\.water-quality)\.(good|bad)$"),
          ("limits", r"(fan\.\d+\.limits)\.(min|max)$"),
          ("pid", r"(fan\.\d+\.pid)\.([a-z]+)$")]


def _commands(lines, current):
    """[(argv, [Line])] that set the keys of `lines`: one command per group of
    keys one command sets together, in the order the groups first appear.
    `current` maps every settable key to its current text, for the values a
    command needs but the batch does not change."""
    groups = {}
    for line in lines:
        k = line.key
        for kind, pattern in GROUPS:
            m = re.match(pattern, k)
            if m:
                groups.setdefault((kind, m.group(1)), {})[m.group(2)] = line
                break
        else:
            m = re.match(r"(fan\.\d+)\.(.+)$", k)
            if m and m.group(2) in FAN_MODE_KEYS:
                groups.setdefault(("fan-mode", m.group(1)), {})[m.group(2)] = line
            else:
                groups.setdefault(("single", k), {})[None] = line

    out = []
    for (kind, gid), items in groups.items():
        values = {name: line.value for name, line in items.items()}
        used = list(items.values())
        parts = gid.split(".")
        if kind == "rgb":
            argv = ["rgb", "set", "controller", parts[2]]
            if "pos" in values:
                argv += ["pos", values.pop("pos")]
            for name, option in (("effect", "effect"), ("colours", "colour"),
                                 ("background", "background"), ("sensor", "sensor"),
                                 ("filter-rise", "filter-rise"),
                                 ("filter-fall", "filter-fall")):
                if name in values:
                    argv.append(_option(option, values.pop(name)))
            for name, value in values.items():
                what, _, word = name.partition(".")
                if what == "param":
                    argv.append(_option("param", "%s=%s" % (word, value)))
                else:
                    argv.append(_option("flag" if value == "on" else "no-flag", word))
            out.append((argv, used))
        elif kind == "alarm":
            argv = ["alarm", parts[1]] + ([values["state"]] if "state" in values else [])
            if "limit" in values:
                argv.append(_option("limit", values["limit"]))
            out.append((argv, used))
        elif kind in ("chart", "calibration", "pid"):
            command = {"chart": ["display", "chart", parts[-1]],
                       "calibration": ["flow", "calibration"],
                       "pid": ["fan", "set", parts[1], "pid"]}[kind]
            out.append((command + [_option(n, v) for n, v in values.items()], used))
        elif kind == "water-quality":
            out.append((["sensor", "water-quality"] +
                        [values.get(n, current[gid + "." + n]) for n in ("good", "bad")],
                        used))
        elif kind == "limits":
            out.append((["fan", "set", parts[1], "limits"] +
                        [values.get(n, current[gid + "." + n]) for n in ("min", "max")],
                        used))
        elif kind == "fan-mode":
            out += [(argv, used) for argv in _fan_mode(parts[1], values, current, used)]
        elif gid.startswith("fan."):
            out.append((["fan", "set", parts[1], parts[2], values[None]], used))
        elif gid.startswith("name."):
            out.append((parts + ["--", values[None]], used))
        else:
            out.append((parts + [values[None]], used))
    return out


def _fan_mode(channel, values, current, lines):
    """The 'fan set N mode' commands for a channel's mode settings. A setting
    of a mode other than the one the channel ends in goes through its own mode
    first; the last command sets the final mode. All of it lands in the
    batch's copy, so the device only ever sees the end result."""
    p = "fan.%s." % channel
    final = values.get("mode", current[p + "mode"])
    if final not in FAN_MODES:
        core.fail(core.EXIT_INVALID, "%s: %s is not a mode the batch can set."
                  % (_where(lines), final))
    pending = {k: v for k, v in values.items() if k != "mode"}
    if final != "follow" and pending.get("follow") == "":
        del pending["follow"]       # not following is what every other mode means
    argvs = []
    for mode in FAN_MODES:
        own = {k: pending.pop(k) for k in FAN_MODES[mode]
               if mode != final and k in pending and k not in FAN_MODES[final]}
        if own:
            argvs.append(_mode_argv(channel, mode, own, current, lines))
    if argvs or "mode" in values or pending:
        argvs.append(_mode_argv(channel, final, pending, current, lines))
    return argvs


def _mode_argv(channel, mode, values, current, lines):
    p = "fan.%s." % channel
    argv = ["fan", "set", channel, "mode", mode]
    if mode == "fixed":
        argv.append(values.get("fixed.power", current[p + "fixed.power"]))
    elif mode == "follow":
        target = values.get("follow", current[p + "follow"])
        if not target:
            core.fail(core.EXIT_INVALID, "%s: following needs %sfollow, the channel to "
                      "follow." % (_where(lines), p))
        argv.append(target)
    else:
        if mode == "target" and "target.temperature" in values:
            argv.append(values["target.temperature"])
        if mode == "curve" and "curve.points" in values:
            argv.append(values["curve.points"])
        if mode == "curve" and "curve.startup" in values:
            argv.append(_option("startup", values["curve.startup"]))
        if "sensor" in values:
            argv.append(_option("sensor", values["sensor"]))
    return argv


# ------------------------------------------------------------------ engine

def _settable(kind, ctx):
    """{name: (key, value)} for every settable key, as the reports in `ctx`
    hold them."""
    return {k.name: (k, k.read(ctx)) for k in keys.select(kind.key_table(), ctx)
            if k.settable}


def _same(key, value, text):
    if key.type == "text":
        return value == text
    value = value.strip()
    if key.type in ("colour", "colours"):
        return value.upper() == text.upper()
    return value == text


def _run(parser, staged, argv, lines, args):
    """Run one command against the batch's copies. Its output is not shown:
    the batch reports the changes of all its lines together."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            parsed = parser.parse_args(argv)
    except SystemExit:
        message = err.getvalue().strip().splitlines()[-1:] or ["not a command"]
        core.fail(core.EXIT_USAGE, "%s: %s" % (_where(lines),
                                              message[0].split("error: ", 1)[-1]))
    parsed.force = getattr(args, "force", False)
    parsed.typed = args.typed
    try:
        core.check_args(parsed, staged.serial)
        with contextlib.redirect_stdout(io.StringIO()):
            parsed.func(staged, parsed)
    except core.Failure as exc:
        core.fail(exc.code, "%s: %s" % (_where(lines), exc.message))
    except SystemExit as exc:
        core.fail(core.EXIT_INVALID, "%s: %s" % (_where(lines), exc.code))


class Plan:
    """A prepared batch: the reports as they will be, and what changes."""

    def __init__(self, staged, changes, protect):
        self.staged, self.changes, self.protect = staged, changes, protect


def prepare(dev, lines, args):
    """Run every line against copies of `dev`'s reports, then the safety
    checks on all the changes together. Nothing is written."""
    kind = dev.kind
    staged = Staged(dev)
    current = _settable(kind, keys.Context(kind, dev.serial, staged))
    texts = {name: key.text(value) for name, (key, value) in current.items()}
    # A controller that changes its effect gets every line given for it: its
    # parameter words and colour count mean something else under the new
    # effect, so a value equal to the old one's says nothing.
    new_effect = {re.match(r"(rgb\.controller\.\d+)\.", line.key).group(1)
                  for line in lines if line.key.endswith(".effect")
                  and line.key.startswith("rgb.controller.") and line.key in texts
                  and line.value.strip() != texts[line.key]}
    readonly = {k.name for k in keys.select(kind.key_table(),
                                            keys.Context(kind, dev.serial, staged))
                if not k.settable}
    todo, protect = [], {}
    for line in lines:
        if line.key in readonly:
            continue                # what get prints about the device, not a setting
        key = current[line.key][0] if line.key in current else \
            _unknown(kind, dev, staged, current, line)
        if line.key in texts and _same(key, line.value, texts[line.key]) and \
                ".".join(line.key.split(".")[:3]) not in new_effect:
            continue
        if key.type != "text":
            line.value = line.value.strip()
        if key.type == "on/off" and line.value not in ("on", "off"):
            core.fail(core.EXIT_INVALID, "%s: on or off." % line)
        if key.source == keys.CONFIG:
            protect[line.key] = line.value == "on"
        else:
            todo.append(line)

    parser = kind.build_parser("aqdctl --device %s" % dev.serial)
    for argv, used in _commands(todo, texts):
        _run(parser, staged, argv, used, args)

    layout = getattr(kind, "RGB", None)
    if layout and core.CTRL_REPORT_ID in staged.reports:
        clash = rgbpx.first_overlap(staged.reports[core.CTRL_REPORT_ID], layout)
        if clash:
            core.fail(core.EXIT_INVALID, "Controllers %d and %d would share LEDs on "
                      "channel %d. Nothing written." % clash)

    after = _settable(kind, keys.Context(kind, dev.serial, staged))
    for name, on in protect.items():
        after[name] = (current[name][0], on)
    # A controller's keys follow its effect, so a new effect brings keys the
    # old one did not have, and takes others away.
    changes = []
    for name in list(current) + [n for n in after if n not in current]:
        old, new = current.get(name, (None, None)), after.get(name, (None, None))
        if old[1] != new[1]:
            changes.append((new[0] or old[0], old[1], new[1]))
    guard = getattr(kind, "guard_write", None)
    if guard and core.CTRL_REPORT_ID in staged.changed():
        guard(dev, staged.original[core.CTRL_REPORT_ID],
              bytearray(staged.reports[core.CTRL_REPORT_ID]), args)
    return Plan(staged, changes, protect)


def _unknown(kind, dev, staged, current, line):
    """The key of a line that names no current settable key: one that a
    controller's new effect brings, or else an error saying why not."""
    m = re.match(r"(rgb\.controller\.\d+)\.", line.key)
    if m and m.group(1) + ".pos" in current:
        for entry in kind.key_table():
            for k in getattr(entry, "template", ()):
                if k.settable and keys.matches(k.name, line.key) and \
                        len(k.name.split(".")) == len(line.key.split(".")):
                    return k
    if m:
        why = "is not a key: that controller is not in use"
    else:
        why = "is not a key of this %s ('get --keys' lists them)" % kind.TITLE
    core.fail(core.EXIT_INVALID, "line %d: %s %s." % (line.number, line.key, why))


def write(dev, plan, args):
    """Write each changed report once, after one confirmation, then the
    protected channels."""
    if not args.yes:
        notice = ("These writes go into the %s's persistent memory - they survive a\n"
                  "reboot and will show up in Aquasuite." % dev.kind.TITLE)
        if not core.confirm("Proceed?", notice):
            core.fail(core.EXIT_NOT_CONFIRMED, "Aborted.")
    staged = plan.staged
    for rid in staged.changed():
        target = args.backup
        if target and rid != core.CTRL_REPORT_ID:
            stem, dot, ext = target.rpartition(".")
            target = "%s-%02x.%s" % (stem, rid, ext) if dot else "%s-%02x" % (target, rid)
        once = _Args(args, yes=True, dry_run=False, backup=target)
        core.commit(dev, bytearray(staged.original[rid]), staged.reports[rid], once)
    if plan.protect:
        stored = core.protected_channels(dev.serial)
        for name, on in plan.protect.items():
            channel = int(name.split(".")[1])
            stored = stored | {channel} if on else stored - {channel}
        core.set_protected(dev.serial, stored)


class _Args:
    """The batch's own flags, with a few replaced."""

    def __init__(self, args, **replace):
        self.__dict__.update(vars(args))
        self.__dict__.update(replace)


def _read(args):
    if args.file == "-":
        if not args.yes and not args.dry_run:
            core.fail(core.EXIT_NOT_CONFIRMED, "Nothing written: a batch from stdin "
                      "cannot be confirmed. Add -y, or -n to see what it would change.")
        return sys.stdin.read()
    try:
        with open(args.file) as fh:
            return fh.read()
    except OSError as exc:
        core.fail(core.EXIT_INVALID, "Cannot read %s: %s."
                  % (args.file, exc.strerror or exc))


def cmd_batch(dev, args):
    out = sys.stdout
    try:
        # With --json, stdout carries the JSON alone; everything else - the
        # list before a confirmation, the backup path - goes to stderr.
        with contextlib.redirect_stdout(sys.stderr) if args.json else \
                contextlib.nullcontext():
            plan = prepare(dev, parse(_read(args)), args)
            if not args.json or not args.yes and not args.dry_run:
                for k, old, new in plan.changes:
                    print("%s: %s -> %s" % (k.name, k.text(old), k.text(new)))
            written = bool(plan.changes) and not args.dry_run
            if written:
                write(dev, plan, args)
            elif plan.changes:
                if args.verbose or not args.json:
                    for rid in plan.staged.changed():
                        core.show_diff(plan.staged.original[rid], plan.staged.reports[rid])
                if not args.json:
                    print("--dry-run: nothing written.")
            elif not args.json:
                print("Nothing to change.")
    except core.Failure as exc:
        if args.json:
            print(json.dumps({"error": {"status": exc.code, "message": exc.message}},
                             indent=2, ensure_ascii=False), file=out)
        raise
    if args.json:
        print(json.dumps({"changes": [{"key": k.name, "old": k.json(old),
                                       "new": k.json(new)}
                                      for k, old, new in plan.changes],
                          "written": written}, indent=2, ensure_ascii=False), file=out)


def register(sub, prefix):
    p = core.add_write_flags(sub.add_parser(
        "batch", formatter_class=core.HelpFormatter,
        help="set many keys with one write",
        description="Set the keys of FILE, one key=value per line as 'get' prints them; "
                    "blank lines and lines starting with # are skipped. Each line runs "
                    "as the command that sets its key, with the same checks, and the "
                    "safety checks then judge all changes together. The device is "
                    "written once, and only if every line succeeds. Keys that already "
                    "hold their value, and read-only keys, are left alone. FILE '-' "
                    "reads stdin and needs -y or -n.",
        epilog="examples:\n"
               "  {p} get display > display.txt\n"
               "  {p} batch display.txt -n\n"
               "  printf 'display.brightness=low\\nalarm.flow.limit=60\\n' | "
               "{p} batch - -y".format(p=prefix)),
        force="go past a safety check, or write to a protected channel")
    p.add_argument("file", metavar="FILE", help="the key=value lines, or - for stdin")
    p.add_argument("--json", action="store_true",
                   help="print the changes as JSON: {changes: [{key, old, new}], "
                        "written}; a failure as {error: {status, message}}")
    p.set_defaults(func=cmd_batch)
