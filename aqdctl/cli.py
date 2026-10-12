"""The command line.

'aqdctl info' lists the attached devices. Everything else names one device by
its serial, first - 'aqdctl --device SERIAL ...' - and then uses the commands
that device offers. The serial chooses which command tree applies, so it has to
be known before the rest is parsed; that is why it must come first."""

import contextlib
import os
import sys

from . import core, discovery


def _supported():
    return ", ".join("%s (%04x:%04x)" % (kind.TITLE, core.VENDOR_ID, pid)
                     for pid, kind in sorted(discovery.device_kinds().items()))


def general_help():
    return """\
usage: aqdctl info
       aqdctl --device SERIAL COMMAND ...
       aqdctl --device SERIAL --help
       aqdctl --help-udev

Read and change the settings stored on Aquacomputer devices, without Aquasuite.
Supported: {supported}.

  aqdctl info                     list the attached devices with their serials
  aqdctl --device SERIAL --help   the commands that device offers
  aqdctl --device SERIAL info     its settings and live readings

--device comes first, and every command other than the device list needs it.
The serial is the one Aquasuite shows, e.g. 12345-67890. It is the only way of
telling two devices of the same kind apart that survives a reboot, and a
command never changes more than one device.

exit status
  0  done, or nothing to change
  1  a value or file aqdctl will not use
  2  usage: the command line itself
  3  device missing, unreadable, or of an unknown layout
  4  refused by a safety check or a protected channel; --force would pass
  5  written, but the device reads back other values
  6  not confirmed: answered no, or no terminal and no -y""".format(supported=_supported())


def udev_help():
    rules = "\n".join('KERNEL=="hidraw*", ATTRS{idVendor}=="%04x", ATTRS{idProduct}=="%04x", '
                      'MODE="0660", GROUP="wheel"' % (core.VENDOR_ID, pid)
                      for pid in sorted(discovery.device_kinds()))
    return """\
Install a udev rule so root is not needed:

  sudo tee /etc/udev/rules.d/99-aquacomputer.rules <<'RULES'
{rules}
RULES
  sudo udevadm control --reload && sudo udevadm trigger

Replace wheel with a group you are in.""".format(rules=rules)


def _take_device(argv):
    """(serial, remaining arguments). --device is only accepted in front."""
    head = argv[0]
    if head == "--device":
        if len(argv) < 2:
            core.fail(core.EXIT_USAGE, "--device needs a serial, e.g. --device 12345-67890. "
                      "'aqdctl info' lists them.")
        return argv[1], argv[2:]
    if head.startswith("--device="):
        return head.split("=", 1)[1], argv[1:]
    if any(a == "--device" or a.startswith("--device=") for a in argv):
        core.fail(core.EXIT_USAGE, "--device must come first: aqdctl --device SERIAL COMMAND ...")
    return None, argv


def _refuse(message, found, status):
    print(message, file=sys.stderr)
    print(file=sys.stderr)
    discovery.list_devices(found, out=sys.stderr)
    sys.exit(status)


def main(argv=None):
    """The command line. A Failure ends it with its own status, its message on
    stderr like any other refusal."""
    try:
        _main(argv)
    except core.Failure as failure:
        print(failure.message, file=sys.stderr)
        sys.exit(failure.code)


def _main(argv):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(general_help())
        return
    if argv[0] == "--help-udev":
        print(udev_help())
        return
    if argv == ["info"]:
        discovery.list_devices(discovery.attached())
        return

    serial, rest = _take_device(argv)
    if serial is None:
        _refuse("A device is required, and --device must come first:\n"
                "  aqdctl --device SERIAL %s" % " ".join(argv), discovery.attached(),
                core.EXIT_USAGE)
    candidates = discovery.attached()
    found = discovery.find(serial, candidates)
    if found is None:
        _refuse("No attached device has the serial %r." % serial, candidates,
                core.EXIT_DEVICE)

    ap = found.kind.build_parser("aqdctl --device %s" % serial)
    args = ap.parse_args(rest)
    args.typed = rest       # for refusals that quote the command back with --force
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
    core.require_window(getattr(args, "min", None), getattr(args, "max", None),
                        getattr(args, "channel", None))
    if getattr(args, "guarded", False):
        core.guard_channel(serial, getattr(args, "channel", None),
                           getattr(args, "force", False))

    dev = core.Device(found.kind, found.path, serial)
    # --quiet: nothing on stdout. Errors and the confirmation go to stderr.
    with contextlib.ExitStack() as stack:
        if getattr(args, "quiet", False):
            stack.enter_context(contextlib.redirect_stdout(
                stack.enter_context(open(os.devnull, "w"))))
        if getattr(args, "needs_device", True) is not False:
            stack.enter_context(dev)
        args.func(dev, args)
