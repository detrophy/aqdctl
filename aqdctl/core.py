"""Everything that does not depend on which device is attached: the checksum,
the byte helpers, talking to a device, what happens around a write (backup,
confirmation, read-back), and the local config, keyed by serial."""

import argparse
import datetime
import errno
import json
import os
import re
import struct
import sys
import time

try:
    import hid
except ImportError:      # the help and the tests do not need it; opening a device does
    hid = None

VENDOR_ID = 0x0C70
INPUT_REPORT_ID = 0x01      # live readings, sent by the device about once a second
COMMAND_REPORT_ID = 0x02    # command frames, see command_frame()
CTRL_REPORT_ID = 0x03       # settings
LABEL_REPORT_ID = 0x08      # names
CTRL_REPORT_DELAY = 0.2     # s between requests; the kernel driver keeps the same gap
# In the live readings of both the Octo and the high flow NEXT (USB captures of
# Aquasuite, and Aquasuite's device information for the high flow NEXT).
FIRMWARE_OFFSET = 0x0D
LIVE_WAIT_MS = 1500         # the device sends its live readings once a second

# A serial as the USB descriptor and Aquasuite give it: two five-digit groups.
SERIAL_PATTERN = re.compile(r"\d{5}-\d{5}")


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
    """(stored, computed) checksum of a report."""
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


def firmware(live):
    """Firmware version from a live-readings report."""
    return be16(live, FIRMWARE_OFFSET)


def pct(raw):
    return raw / 100.0


def to_raw_pct(percent):
    return int(round(percent * 100))


def require_pct(value, label="percent"):
    """Bound a percentage inside the handler, not only in the CLI.

    The CLI screens its arguments, but the handlers are also called directly -
    by the test suite, and by anything that imports this package - where
    nothing else stands between a bad value and the device."""
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


def command_frame(code):
    """Report 0x02 is a command frame: byte 4 is the command, bytes 9-10 a
    CRC-16/USB over bytes 1-8, stored big-endian like every other checksum."""
    frame = bytearray(11)
    frame[0], frame[4] = COMMAND_REPORT_ID, code
    return bytes(reseal(frame))


# Command 0x02 follows every settings write. USB captures of Aquasuite show it
# sent as an *output* report (as the descriptor declares it), 3.8 s after the
# last settings write, and never after a name write. It applies nothing: a
# changed offset shows up in the live readings before 0x02 arrives. So it is
# presumably the save to flash, which Aquasuite delays until changes stop.
SECONDARY_CTRL_REPORT = command_frame(0x02)


# ------------------------------------------------------------------- paths

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


def _home():
    user = _invoking_user()
    return user.pw_dir if user else os.path.expanduser("~")


def data_dir():
    return os.path.join(_home(), ".local", "share", "aqdctl")


def config_path():
    return os.path.join(_home(), ".config", "aqdctl", "config.json")


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


def _make_dir(directory):
    fresh = not os.path.isdir(directory)
    os.makedirs(directory, exist_ok=True)
    if fresh:
        _give_back(directory)


# ------------------------------------------------------------------ config

def load_config():
    try:
        with open(config_path()) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def save_config(cfg):
    path = config_path()
    _make_dir(os.path.dirname(path))
    with _open_nofollow(path, "w") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    _give_back(path)
    return path


def _device_config(cfg, serial):
    devices = cfg.get("devices")
    if not isinstance(devices, dict):
        return {}
    entry = devices.get(serial)
    return entry if isinstance(entry, dict) else {}


def protected_channels(serial):
    """Fan channels of one device that the user has protected from writes.

    Not a device setting - the hardware has no such field - so it lives in the
    local config. Keyed by serial: a pump on channel 1 of one Octo says nothing
    about channel 1 of another, and a multi-loop system can hold several."""
    raw = _device_config(load_config(), serial).get("protected", [])
    if not isinstance(raw, list):
        return set()
    return {int(c) for c in raw if str(c).isdigit() or isinstance(c, int)}


def set_protected(serial, channels):
    cfg = load_config()
    devices = cfg.get("devices") if isinstance(cfg.get("devices"), dict) else {}
    entry = _device_config(cfg, serial)
    entry["protected"] = sorted(channels)
    devices[serial] = entry
    cfg["devices"] = devices
    return save_config(cfg)


def guard_channel(serial, channel, force):
    if channel in protected_channels(serial) and not force:
        sys.exit("Channel %d is protected. Re-run with --force if you mean it, or\n"
                 "lift the protection with 'aqdctl --device %s fan set %d protect off'."
                 % (channel, serial, channel))


# ------------------------------------------------------------------ device

class Device:
    """One attached device. `kind` is the module that describes it - its report
    sizes, layout and commands - and `serial` the only stable way to tell two
    devices of the same kind apart: hidraw and hwmon numbers move between boots."""

    def __init__(self, kind, path, serial):
        self.kind, self.path, self.serial = kind, path, serial
        self.dev = None
        self._last_op = 0.0

    def __enter__(self):
        if hid is None:
            sys.exit("python-hidapi not found. Install it with 'pip install hidapi'\n"
                     "(on Arch: pacman -S python-hidapi).")
        self.dev = hid.device()
        try:
            self.dev.open_path(self.path)
        except (IOError, OSError) as exc:
            node = self.path.decode() if isinstance(self.path, bytes) else self.path
            sys.exit("Cannot open the %s %s at %s (%s).\nRun as root, or install the "
                     "udev rule that 'aqdctl --help-udev' prints."
                     % (self.kind.TITLE, self.serial, node, exc))
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
        size = self.kind.REPORT_SIZES[report_id]
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

    def command(self, code):
        """Send one command frame, as an output report, checked."""
        self._pace()
        if self.dev.write(command_frame(code)) < 0:
            sys.exit("Command %#04x failed." % code)
        self._last_op = time.monotonic()

    def read_input(self, size, timeout_ms=LIVE_WAIT_MS):
        """Wait for one live-readings report. The device sends these on its own;
        hidraw hands every reader a copy, so this sends nothing and the kernel
        driver keeps receiving them as before."""
        return _await_input(self.dev, size, timeout_ms)


def _await_input(handle, size, timeout_ms):
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        left = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            buf = bytes(handle.read(size, left))
        except (IOError, OSError, ValueError):
            return None
        if buf[:1] == bytes([INPUT_REPORT_ID]) and len(buf) == size:
            return buf
    return None


def peek(found, report_id):
    """Read from a device that has not been selected, for the device list only:
    feature report `report_id` and one live-readings report. Returns (report,
    live report), each None when it cannot be had. Never exits, so one device
    that cannot be opened does not hide the others."""
    if hid is None:
        return None, None
    probe = hid.device()
    try:
        probe.open_path(found.path)
    except (IOError, OSError, ValueError):
        return None, None
    report = None
    try:
        size = found.kind.REPORT_SIZES[report_id]
        try:
            buf = bytes(probe.get_feature_report(report_id, size))
        except (IOError, OSError, ValueError):
            buf = b""
        if len(buf) == size and report_crc(buf)[0] == report_crc(buf)[1]:
            report = bytearray(buf)
        live = _await_input(probe, found.kind.INPUT_REPORT_SIZE, LIVE_WAIT_MS)
    finally:
        try:
            probe.close()
        except Exception:
            pass
    return report, live


# ------------------------------------------------------------------- write

def backup(buf, dev, explicit=None, rid=None):
    """Save a raw report. The default name carries the device's type and serial:
    settings reports hold no serial, so the name is the only record of which
    device a backup came from, and restore relies on it."""
    if explicit:
        path = explicit
    else:
        directory = data_dir()
        _make_dir(directory)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = "%02x" % (rid if rid is not None else buf[0])
        path = os.path.join(directory, "%s-%s-%s-%s.bin"
                            % (dev.kind.NAME, dev.serial, tag, stamp))
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


def commit(dev, before, after, args):
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
    path = backup(before, dev, args.backup)
    print("\nbackup written to %s" % path)
    if not args.yes:
        print("These writes go into the %s's persistent memory - they survive a"
              % dev.kind.TITLE)
        print("reboot and will show up in Aquasuite.")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Aborted.")
    dev.write(after)
    verify = dev.read(before[0])
    if diff(verify, after):
        print("WARNING: device state differs from what was written:")
        show_diff(after, verify)
        print("The firmware may have rejected or adjusted some values.")
    else:
        print("Written and verified.")


def cmd_backup(dev, args):
    for rid in (CTRL_REPORT_ID, LABEL_REPORT_ID):
        buf = dev.read(rid)
        target = args.output
        if target and rid != CTRL_REPORT_ID:
            root, ext = os.path.splitext(target)
            target = "%s-%02x%s" % (root, rid, ext)
        path = backup(buf, dev, target, rid)
        print("report %#04x: wrote %d bytes to %s" % (rid, len(buf), path))


def cmd_restore(dev, args):
    try:
        with open(args.file, "rb") as fh:
            saved = bytearray(fh.read())
    except OSError as exc:
        sys.exit("Cannot read %s: %s." % (args.file, exc.strerror or exc))
    rid = saved[0] if saved else None
    sizes = dev.kind.REPORT_SIZES
    if rid not in sizes or len(saved) != sizes[rid]:
        sys.exit("%s is not a report of the %s: it has id %r and %d bytes, and the\n"
                 "%s's reports are %s."
                 % (args.file, dev.kind.TITLE, rid, len(saved), dev.kind.TITLE,
                    ", ".join("%#04x with %d bytes" % kv for kv in sorted(sizes.items()))))
    stored, computed = report_crc(saved)
    if stored != computed:
        sys.exit("%s has a bad checksum - refusing to restore it." % args.file)
    force = getattr(args, "force", False)
    # Settings reports carry no serial, so the file name is the only record of
    # where a backup came from. Restoring another device's settings would copy
    # its whole configuration - pumps included - onto this one.
    origin = SERIAL_PATTERN.search(os.path.basename(args.file))
    if not force and origin is None:
        sys.exit("%s does not say which device it came from: its name holds no\n"
                 "serial, and the report itself carries none. Re-run with --force if\n"
                 "it was taken from this %s (%s)."
                 % (args.file, dev.kind.TITLE, dev.serial))
    if not force and origin.group(0) != dev.serial:
        sys.exit("%s was taken from device %s, not from %s. Restoring it would\n"
                 "copy that device's settings onto this one. Re-run with --force if\n"
                 "that is what you want." % (args.file, origin.group(0), dev.serial))
    # A restore rewrites the whole report, so it reaches protected channels too.
    # There is no per-channel version of this, hence the blanket refusal.
    guarded = protected_channels(dev.serial)
    if guarded and rid == CTRL_REPORT_ID and not force:
        sys.exit("Restoring rewrites every channel, including protected channel%s "
                 "%s.\nRe-run with --force if that is what you want."
                 % ("" if len(guarded) == 1 else "s",
                    ", ".join(str(c) for c in sorted(guarded))))
    before = dev.read(rid)
    # The device's own safety checks apply to a restore as to any other write:
    # a backup can hold a USB current or signal output mode just as a command can.
    guard = getattr(dev.kind, "guard_write", None)
    if guard is not None and rid == CTRL_REPORT_ID and before != saved:
        guard(dev, before, saved, args)
    commit(dev, before, saved, args)


# ---------------------------------------------------------------- parsing

class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Wraps plain descriptions to the terminal, but leaves any text with line
    breaks of its own - examples, tables - exactly as written."""

    def _fill_text(self, text, width, indent):
        if "\n" in text:
            return super()._fill_text(text, width, indent)
        return argparse.HelpFormatter._fill_text(self, text, width, indent)


def add_write_flags(p, guarded=False, force=None):
    """Flags common to every command that writes to the device.
    Described together under "write flags" in the device's help.

    guarded: the command writes to a fan channel, which may be protected.
    force: help text for a --force that goes past some other check."""
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show what would change and write nothing "
                        "(implies --verbose)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="also show the byte-level diff")
    p.add_argument("-y", "--yes", action="store_true",
                   help="skip the confirmation prompt")
    p.add_argument("--backup", metavar="FILE",
                   help="where to put the pre-write backup")
    if guarded or force:
        p.add_argument("--force", action="store_true",
                       help=force or "write even if the channel is protected")
    p.set_defaults(guarded=guarded)
    return p


DANGER_HEADER = "[### ATTENTION - DANGEROUS COMMAND ###]"


def warn_danger(text, stream=None):
    """Print a warning under the danger header, in red when the stream is a
    terminal and NO_COLOR is not set (https://no-color.org)."""
    stream = stream or sys.stdout
    header = DANGER_HEADER
    if getattr(stream, "isatty", lambda: False)() and not os.environ.get("NO_COLOR"):
        header = "\033[31m%s\033[0m" % header
    print(header, file=stream)
    print(text, file=stream)


def force_hint(dev, args):
    """The command as typed, with --force added, for a refusal to quote."""
    typed = getattr(args, "typed", None)
    if typed is None:
        return "  (the same command with --force)"
    import shlex
    return "  aqdctl --device %s %s --force" % (dev.serial,
                                               " ".join(shlex.quote(w) for w in typed))


def add_backup_restore(sub, kind):
    p = sub.add_parser("backup", help="save the raw reports to a file",
                       description="Write both feature reports to disk, byte for "
                                   "byte, so 'restore' can put them back. Without "
                                   "-o the file names carry the device's serial.")
    p.add_argument("-o", "--output", metavar="FILE", help="output file")
    p.set_defaults(func=cmd_backup)

    p = add_write_flags(sub.add_parser(
        "restore", help="write a saved report back to the device",
        description="Restore a file written by 'backup'. The checksum is checked "
                    "before anything is sent. A backup whose name holds another "
                    "device's serial, or none, needs --force: restoring it would "
                    "copy that device's settings onto this one.%s"
                    % (" It rewrites every channel, so it is also refused while "
                       "any channel is protected." if getattr(kind, "HAS_FANS", False)
                       else " The safety checks of the commands apply to it too.")),
        guarded=True, force="restore even though one of the checks above refuses it")
    p.add_argument("file", metavar="FILE")
    p.set_defaults(func=cmd_restore)
