"""Everything that does not depend on which device is attached: the checksum,
the byte helpers, talking to a device, what happens around a write (backup,
confirmation, read-back), and the local config, keyed by serial."""

import argparse
import contextlib
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

# Bytes 1-2 of the settings report, big-endian: the version of its layout. It
# is 1 on the high flow NEXT and 2 on the Octo in every capture, and Aquasuite
# checks it before using a report. A firmware that moved the fields would have
# to change it.
STRUCTURE_OFFSET = 1

# Exit statuses, for scripts; the general help lists them. A plain
# sys.exit(message) exits with 1 and stays the way to reject a bad value.
EXIT_INVALID = 1          # a value or file aqdctl will not use
EXIT_USAGE = 2            # the command line itself (argparse uses 2 as well)
EXIT_DEVICE = 3           # device missing, unreadable, or of an unknown layout
EXIT_REFUSED = 4          # a safety check or protected channel; --force would pass
EXIT_DIFFERS = 5          # written, but the device reads back other values
EXIT_NOT_CONFIRMED = 6    # the write was not confirmed

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


class Failure(SystemExit):
    """Ends a command with one of the EXIT_ statuses. str() is the message,
    which cli.main prints on stderr, as Python does for sys.exit(message)."""

    def __init__(self, status, message):
        super().__init__(status)
        self.message = message

    def __str__(self):
        return self.message


def fail(status, message):
    raise Failure(status, message)


def confirm(question, notice=None, hint="Add -y to write without asking."):
    """Ask on the terminal; True for yes. The question goes to stderr, so it
    shows with --quiet. Without a terminal there is nobody to ask, so the
    command ends unconfirmed instead of waiting or failing on end of input."""
    if sys.stdin is None or not sys.stdin.isatty():
        fail(EXIT_NOT_CONFIRMED, "Nothing written: no terminal to confirm on. " + hint)
    if notice:
        print(notice, file=sys.stderr)
    sys.stderr.write(question + " [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def layout_version(settings):
    """The layout version a settings report declares."""
    return be16(settings, STRUCTURE_OFFSET)


def check_layout(kind, settings):
    """Refuse a settings report laid out differently from the one this module
    decodes. Every offset in it would be a guess."""
    found = layout_version(settings)
    if found != kind.STRUCTURE_ID:
        fail(EXIT_DEVICE, "The %s's settings use layout version %d; aqdctl knows version %d.\n"
                 "A firmware update may have moved its fields, so aqdctl reads no further\n"
                 "and writes nothing. 'backup' still saves the raw reports."
                 % (kind.TITLE, found, kind.STRUCTURE_ID))


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


def check_args(args, serial):
    """The checks a parsed command gets before it runs, from the command line
    or from a batch: percentages in range, a power window the right way round,
    and a protected fan channel."""
    for field, limit in (("percent", 100.0), ("min", 100.0), ("max", 100.0)):
        value = getattr(args, field, None)
        if value is not None and not 0.0 <= value <= limit:
            sys.exit("%s must be between 0 and %g." % (field, limit))
    require_window(getattr(args, "min", None), getattr(args, "max", None),
                   getattr(args, "channel", None))
    if getattr(args, "guarded", False):
        guard_channel(serial, getattr(args, "channel", None),
                      getattr(args, "force", False))


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
# Seconds after the last of a run of settings writes that Aquasuite sends the
# save command: 3.62-3.84 s in every capture, each write restarting the wait.
SAVE_DELAY = 3.8


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


@contextlib.contextmanager
def device_lock(serial, wait=10.0):
    """Hold the device for one read-modify-write. A second aqdctl changing the
    same device waits for the first, so neither writes back a report the other
    has changed in between."""
    import fcntl
    directory = os.path.join(data_dir(), "locks")
    _make_dir(directory)
    path = os.path.join(directory, "%s.lock" % serial)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    _give_back(path)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    fail(EXIT_DEVICE, "Another aqdctl has been changing the device %s "
                         "for %g s. Nothing written." % (serial, wait))
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


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


def _store(serial, key, value):
    """Set one entry of a device's config, or remove it when `value` is None."""
    cfg = load_config()
    devices = cfg.get("devices") if isinstance(cfg.get("devices"), dict) else {}
    entry = _device_config(cfg, serial)
    if value is None:
        entry.pop(key, None)
    else:
        entry[key] = value
    devices[serial] = entry
    cfg["devices"] = devices
    return save_config(cfg)


def set_protected(serial, channels):
    return _store(serial, "protected", sorted(channels))


# A local name for a device, accepted by --device in place of its serial. It
# starts with a letter, so it can never be read as a serial. Names stored on
# the device are not used: two devices may carry the same ones.
ALIAS_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def aliases():
    """alias -> serial, for every device in the config."""
    devices = load_config().get("devices")
    if not isinstance(devices, dict):
        return {}
    return {entry["alias"]: serial for serial, entry in devices.items()
            if isinstance(entry, dict) and isinstance(entry.get("alias"), str)}


def alias_of(serial):
    alias = _device_config(load_config(), serial).get("alias")
    return alias if isinstance(alias, str) else None


def set_alias(serial, alias):
    """Give a device its alias, or take it away with None."""
    if alias is not None:
        if not ALIAS_PATTERN.fullmatch(alias):
            fail(EXIT_INVALID, "%r is not an alias: up to 32 lower-case letters, digits, '-' "
                 "and '_', starting with a letter." % alias)
        owner = aliases().get(alias)
        if owner not in (None, serial):
            fail(EXIT_INVALID, "%r already stands for %s. Clear it there first:\n"
                 "  aqdctl --device %s alias -" % (alias, owner, owner))
    return _store(serial, "alias", alias)


def cmd_alias(dev, args):
    if args.name is None:
        print(alias_of(dev.serial) or "-")
        return
    path = set_alias(dev.serial, None if args.name == "-" else args.name)
    print("alias of %s %s: %s" % (dev.kind.TITLE, dev.serial,
                                  "none" if args.name == "-" else args.name))
    print("stored in %s" % path)


def guard_channel(serial, channel, force):
    if channel in protected_channels(serial) and not force:
        fail(EXIT_REFUSED, "Channel %d is protected. Re-run with --force if you mean it, or\n"
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
            fail(EXIT_DEVICE, "python-hidapi not found. Install it with 'pip install hidapi'\n"
                     "(on Arch: pacman -S python-hidapi).")
        self.dev = hid.device()
        try:
            self.dev.open_path(self.path)
        except (IOError, OSError) as exc:
            node = self.path.decode() if isinstance(self.path, bytes) else self.path
            fail(EXIT_DEVICE, "Cannot open the %s %s at %s (%s).\nRun as root, or install the "
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

    def read(self, report_id=CTRL_REPORT_ID, check=True):
        """One feature report, its size and checksum checked, and for the
        settings report its layout version. `check=False` skips the version
        check, for saving reports that are not decoded."""
        size = self.kind.REPORT_SIZES[report_id]
        self._pace()
        buf = bytes(self.dev.get_feature_report(report_id, size))
        self._last_op = time.monotonic()
        if len(buf) != size:
            fail(EXIT_DEVICE, "Short report %#04x: got %d bytes, expected %d."
                     % (report_id, len(buf), size))
        stored, computed = report_crc(buf)
        if stored != computed:
            fail(EXIT_DEVICE, "Report %#04x checksum mismatch (stored %#06x, computed %#06x).\n"
                     "Refusing to continue - the layout assumptions do not hold on this "
                     "firmware." % (report_id, stored, computed))
        if check and report_id == CTRL_REPORT_ID:
            check_layout(self.kind, buf)
        return bytearray(buf)

    def write(self, buf, save=True):
        """Send a resealed report. After a settings report, also send the
        save command, unless `save` is False and the caller sends it later
        with save(), as Aquasuite does once changes stop. After a name report
        Aquasuite sends nothing, and neither does this."""
        stored, computed = report_crc(buf)
        if stored != computed:
            sys.exit("Refusing to send a report with a stale checksum (internal error).")
        self._pace()
        if self.dev.send_feature_report(bytes(buf)) < 0:
            fail(EXIT_DEVICE, "send_feature_report failed.")
        self._last_op = time.monotonic()
        if buf[0] == CTRL_REPORT_ID and save:
            self.save()

    def save(self):
        """Command 0x02, which Aquasuite sends 3.6-3.8 s after the last of a
        run of settings writes. The write takes effect without it; it is
        presumably the save to flash."""
        # An output report, as declared and as Aquasuite sends it. The HID
        # interface has no interrupt OUT endpoint, so hidraw delivers this as a
        # SET_REPORT(output) control transfer - what the capture shows. The
        # result is checked: python-hidapi reports failure by returning -1, not
        # by raising.
        self._pace()
        if self.dev.write(SECONDARY_CTRL_REPORT) < 0:
            fail(EXIT_DEVICE, "The settings report was written, but report 0x02, which "
                     "Aquasuite sends after\nevery settings change, failed. The "
                     "change is active; it may not survive a power\ncycle. Run "
                     "the command again to retry.")
        self._last_op = time.monotonic()

    def command(self, code):
        """Send one command frame, as an output report, checked."""
        self._pace()
        if self.dev.write(command_frame(code)) < 0:
            fail(EXIT_DEVICE, "Command %#04x failed." % code)
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
        fh = _open_nofollow(path, "wb")
    else:
        directory = data_dir()
        _make_dir(directory)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = "%02x" % (rid if rid is not None else buf[0])
        stem = os.path.join(directory, "%s-%s-%s-%s" % (dev.kind.NAME, dev.serial, tag, stamp))
        # Two backups within one second get -2, -3, ...: an earlier backup is
        # never overwritten.
        n = 1
        while True:
            path = stem + (".bin" if n == 1 else "-%d.bin" % n)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644)
                break
            except FileExistsError:
                n += 1
        fh = os.fdopen(fd, "wb")
    with fh:
        fh.write(bytes(buf))
    _give_back(path)
    return path


def diff(before, after):
    return [(i, before[i], after[i]) for i in range(len(before)) if before[i] != after[i]]


def diff_lines(before, after):
    changes = diff(before, after)
    tail = len(before) - 2
    body = [c for c in changes if c[0] < tail]
    lines = ["byte changes (excluding checksum): %d" % len(body)]
    lines += ["  0x%03x: %02x -> %02x" % c for c in body]
    if len(changes) != len(body):
        lines.append("  checksum resealed")
    return lines


def show_diff(before, after):
    print("\n".join(diff_lines(before, after)))


def commit(dev, before, after, args):
    if getattr(dev, "staged", False):
        # In a batch: the change goes into the batch's copy of the report, and
        # the batch writes once at the end.
        dev.stage(after)
        return
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
        notice = ("These writes go into the %s's persistent memory - they survive a\n"
                  "reboot and will show up in Aquasuite." % dev.kind.TITLE)
        if not confirm("Proceed?", notice):
            fail(EXIT_NOT_CONFIRMED, "Aborted.")
    store(dev, after)
    print("Written and verified.")


def store(dev, after, save=True):
    """Write a report and read it back. Without `save`, the save command is
    left to the caller."""
    if save:
        dev.write(after)
    else:
        dev.write(after, save=False)
    verify = dev.read(after[0])
    if diff(verify, after):
        fail(EXIT_DIFFERS, "Written, but the device reads back other values:\n%s\n"
             "The firmware may have rejected or adjusted some of them."
             % "\n".join(diff_lines(after, verify)))


def profile_path(prefix, dev, rid):
    """PREFIX-SERIAL-RR.bin: what 'backup -o PREFIX' writes, named so that
    'restore' knows which device and report it holds."""
    if prefix.endswith(".bin"):
        prefix = prefix[:-len(".bin")]
    return "%s-%s-%02x.bin" % (prefix, dev.serial, rid)


def cmd_backup(dev, args):
    for rid in (CTRL_REPORT_ID, LABEL_REPORT_ID):
        # A report of an unknown layout is still worth saving: it is what a new
        # firmware's layout gets mapped from.
        buf = dev.read(rid, check=False)
        target = profile_path(args.output, dev, rid) if args.output else None
        path = backup(buf, dev, target, rid)
        print("report %#04x: wrote %d bytes to %s" % (rid, len(buf), path))


def _load_dump(dev, path):
    """A saved report, checked to be one of this device's: id, size, checksum
    and, for settings, the layout version."""
    try:
        with open(path, "rb") as fh:
            saved = bytearray(fh.read())
    except OSError as exc:
        sys.exit("Cannot read %s: %s." % (path, exc.strerror or exc))
    rid = saved[0] if saved else None
    sizes = dev.kind.REPORT_SIZES
    if rid not in sizes or len(saved) != sizes[rid]:
        sys.exit("%s is not a report of the %s: it has id %r and %d bytes, and the\n"
                 "%s's reports are %s."
                 % (path, dev.kind.TITLE, rid, len(saved), dev.kind.TITLE,
                    ", ".join("%#04x with %d bytes" % kv for kv in sorted(sizes.items()))))
    stored, computed = report_crc(saved)
    if stored != computed:
        sys.exit("%s has a bad checksum - refusing to restore it." % path)
    if rid == CTRL_REPORT_ID and layout_version(saved) != dev.kind.STRUCTURE_ID:
        sys.exit("%s holds settings of layout version %d; the %s uses version %d.\n"
                 "Its fields would land in the wrong places, so it is not restored."
                 % (path, layout_version(saved), dev.kind.TITLE, dev.kind.STRUCTURE_ID))
    return saved


def _check_origin(dev, path, other_device):
    """Settings reports carry no serial, so the file name is the only record of
    where a dump came from. Restoring another device's settings copies its
    whole configuration - pumps included - onto this one."""
    origin = SERIAL_PATTERN.search(os.path.basename(path))
    if other_device or origin is not None and origin.group(0) == dev.serial:
        return
    if origin is None:
        fail(EXIT_REFUSED, "%s does not say which device it came from: its name holds no\n"
             "serial, and the report itself carries none. Re-run with --other-device\n"
             "to restore it onto this %s (%s)." % (path, dev.kind.TITLE, dev.serial))
    fail(EXIT_REFUSED, "%s was taken from device %s, not from %s. Restoring it would\n"
         "copy that device's settings onto this one. Re-run with --other-device if\n"
         "that is what you want." % (path, origin.group(0), dev.serial))


def protected_changes(dev, before, after):
    """The protected fan channels whose bytes differ between two settings
    reports."""
    spans = getattr(dev.kind, "channel_spans", None)
    if spans is None:
        return []
    return [ch for ch in sorted(protected_channels(dev.serial))
            if any(before[a:b] != after[a:b] for a, b in spans(ch))]


def cmd_restore(dev, args):
    if len(args.files) > 2:
        fail(EXIT_USAGE, "restore takes a settings dump, a names dump, or one of each.")
    dumps = {}
    for path in args.files:
        saved = _load_dump(dev, path)
        if saved[0] in dumps:
            fail(EXIT_USAGE, "%s and %s are both report %#04x."
                 % (dumps[saved[0]][0], path, saved[0]))
        _check_origin(dev, path, getattr(args, "other_device", False))
        dumps[saved[0]] = (path, saved)
    force = getattr(args, "force", False)
    pairs = []
    for rid in sorted(dumps):
        path, saved = dumps[rid]
        before = dev.read(rid)
        if before == saved:
            continue
        if rid == CTRL_REPORT_ID:
            touched = protected_changes(dev, before, saved)
            if touched and not force:
                fail(EXIT_REFUSED, "%s changes protected channel%s %s. Re-run with --force\n"
                     "if that is what you want."
                     % (path, "" if len(touched) == 1 else "s",
                        ", ".join(str(c) for c in touched)))
            # The device's own safety checks apply to a restore as to any other
            # write: a dump can hold a USB current or a signal output mode just
            # as a command can.
            guard = getattr(dev.kind, "guard_write", None)
            if guard is not None:
                guard(dev, before, saved, args)
        pairs.append((before, saved))
    if not pairs:
        print("Nothing to change.")
        return
    commit_many(dev, pairs, args)


class replaced:
    """A command's flags with some of them replaced."""

    def __init__(self, args, **replace):
        self.__dict__.update(vars(args))
        self.__dict__.update(replace)


def backup_target(path, rid):
    """Where --backup FILE puts report `rid`: FILE for the settings, FILE-RR
    for another report, so two reports never share one file."""
    if not path or rid == CTRL_REPORT_ID:
        return path
    stem, dot, ext = path.rpartition(".")
    return "%s-%02x.%s" % (stem, rid, ext) if dot else "%s-%02x" % (path, rid)


def commit_many(dev, pairs, args):
    """Write several reports, [(before, after)], after one confirmation; each
    gets its own backup, write and read-back."""
    if len(pairs) == 1:
        before, after = pairs[0]
        commit(dev, before, after, args)
        return
    if args.dry_run:
        for before, after in pairs:
            show_diff(before, after)
        print("--dry-run: nothing written.")
        return
    if not args.yes:
        notice = ("These writes go into the %s's persistent memory - they survive a\n"
                  "reboot and will show up in Aquasuite." % dev.kind.TITLE)
        if not confirm("Proceed?", notice):
            fail(EXIT_NOT_CONFIRMED, "Aborted.")
    for before, after in pairs:
        commit(dev, before, after, replaced(args, yes=True, dry_run=False,
                                            backup=backup_target(args.backup, after[0])))


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
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print nothing on success; errors and the confirmation "
                        "still go to stderr")
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


def add_alias(sub, prefix):
    p = sub.add_parser(
        "alias", formatter_class=HelpFormatter,
        help="a local name that --device accepts in place of the serial",
        description="A name for this device that --device accepts in place of its "
                    "serial, kept in the local config under the serial. Up to 32 "
                    "lower-case letters, digits, '-' and '_', starting with a letter, "
                    "and unique across devices. Without NAME the alias is printed, "
                    "'-' for none; NAME '-' clears it.",
        epilog="examples:\n"
               "  {p} alias loop-a\n"
               "  aqdctl --device loop-a info\n"
               "  {p} alias -".format(p=prefix))
    p.add_argument("name", nargs="?", metavar="NAME")
    p.set_defaults(func=cmd_alias, needs_device=False)


def add_backup_restore(sub, kind, prefix):
    p = sub.add_parser(
        "backup", formatter_class=HelpFormatter, help="save the raw reports to files",
        description="Write both feature reports to disk, byte for byte, so 'restore' "
                    "can put them back. Without -o they go to the data directory, "
                    "named after the device's type and serial and the time. With -o "
                    "PREFIX they go to PREFIX-SERIAL-03.bin (settings) and "
                    "PREFIX-SERIAL-08.bin (names), replacing earlier ones: a profile.",
        epilog="examples:\n"
               "  {p} backup -o gaming\n"
               "  {p} restore gaming-SERIAL-03.bin gaming-SERIAL-08.bin -y"
               .format(p=prefix))
    p.add_argument("-o", "--output", metavar="PREFIX",
                   help="file name prefix; the serial and report number are appended")
    p.set_defaults(func=cmd_backup)

    p = add_write_flags(sub.add_parser(
        "restore", formatter_class=HelpFormatter,
        help="write saved reports back to the device",
        description="Restore files written by 'backup': a settings dump (03), a names "
                    "dump (08), or one of each, written after one confirmation. "
                    "Checksums and the layout version are checked before anything is "
                    "sent. A dump whose name holds another device's serial, or none, "
                    "needs --other-device: it copies that device's settings onto this "
                    "one. The safety checks of the commands apply to a restore too%s; "
                    "--force goes past them."
                    % (", and so does channel protection: a dump that changes a "
                       "protected channel is refused" if getattr(kind, "HAS_FANS", False)
                       else "")),
        force="go past a safety check%s" % (", or change a protected channel"
                                            if getattr(kind, "HAS_FANS", False) else ""))
    p.add_argument("files", metavar="FILE", nargs="+",
                   help="a settings dump, a names dump, or one of each")
    p.add_argument("--other-device", action="store_true", dest="other_device",
                   help="restore a dump taken from another device, or one whose name "
                        "holds no serial")
    p.set_defaults(func=cmd_restore)
