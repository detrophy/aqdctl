#!/usr/bin/env python3
"""Read-only capture of the high flow NEXT's feature reports.

Issues GET_REPORT only - nothing is sent to the device. Ids and sizes come from
the device's HID report descriptor, which declares the Octo's four reports at
different sizes, plus one the Octo does not have:

    0x01 input     164  sensor stream, what the kernel driver reads
    0x02 output     11  same size as the Octo's post-write commit report
    0x03 feature   682  control report, if the Octo's scheme carries over
    0x08 feature   773  on the Octo this is the name table
    0x0c feature  1025  no Octo counterpart

The first question is whether octoctl's framing transfers: CRC-16/USB over
everything after the report id, stored big-endian in the last two bytes. If it
holds, the codec, pacing and write path carry over and only the layout is new.

Usage:  sudo python3 probe_highflow.py [NAME]
        sudo python3 probe_highflow.py --input [NAME]

The first form saves highflow/NAME.bin, NAME-08.bin and NAME-0c.bin (NAME
defaults to 'baseline').

The second records input report 0x01 for 10 seconds into highflow/NAME-01.log
(NAME defaults to 'input'), with the kernel driver's hwmon readings beside
each report. It sends nothing to the device, not even GET_REPORT: the device
pushes these reports on its own, and hidraw hands every reader a copy, so the
kernel driver keeps receiving them as before.

Files are owned by the invoking user. Existing captures are never overwritten.
"""
import glob, os, pwd, struct, sys, time

try:
    import hid
except ImportError:
    sys.exit("python-hidapi not found (Arch: pacman -S python-hidapi)")

VID, PID = 0x0C70, 0xF012
REPORTS = [(0x03, 682), (0x08, 773), (0x0C, 1025)]
DELAY = 0.2     # s between requests; unpaced reads gave the Octo USB I/O errors
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "highflow")
INPUT_ID, INPUT_SIZE = 0x01, 164
INPUT_SECONDS = 10     # long enough for counters to show their rate


def crc16_usb(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def framing(raw):
    """Which checksum scheme the report satisfies, or None. The Octo's is the
    first; the others are the obvious neighbours, tried so a miss says more
    than 'no match'."""
    tail = len(raw) - 2
    for label, start, order in (("octo: BE, over bytes 1..end-2", 1, ">H"),
                                ("BE, over bytes 0..end-2 (id included)", 0, ">H"),
                                ("LE, over bytes 1..end-2", 1, "<H"),
                                ("LE, over bytes 0..end-2 (id included)", 0, "<H")):
        if struct.unpack_from(order, raw, tail)[0] == crc16_usb(raw[start:tail]):
            return label
    return None


def owner():
    name = os.environ.get("SUDO_USER")
    try:
        return pwd.getpwnam(name) if name else None
    except KeyError:
        return None


def give_back(path):
    user = owner()
    if user:
        os.lchown(path, user.pw_uid, user.pw_gid)


def save(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    give_back(path)


def printable_runs(raw, minimum=4):
    runs, start = [], None
    for i, b in enumerate(raw + b"\x00"):
        if 0x20 <= b < 0x7F or 0xA0 <= b <= 0xFF:
            start = i if start is None else start
        else:
            if start is not None and i - start >= minimum:
                runs.append((start, raw[start:i].decode("latin-1")))
            start = None
    return runs


def hwmon_dir():
    for name_file in sorted(glob.glob("/sys/class/hwmon/hwmon*/name")):
        try:
            with open(name_file) as fh:
                if fh.read().strip() == "highflownext":
                    return os.path.dirname(name_file)
        except OSError:
            pass
    return None


def hwmon_readings(directory):
    """Every *_input value the kernel driver exposes, as name=value pairs."""
    if not directory:
        return "(kernel driver not loaded)"
    out = []
    for leaf in sorted(os.listdir(directory)):
        if leaf.endswith("_input"):
            try:
                with open(os.path.join(directory, leaf)) as fh:
                    out.append("%s=%s" % (leaf[:-6], fh.read().strip()))
            except OSError:
                out.append("%s=-" % leaf[:-6])
    return " ".join(out)


def capture_input(name):
    target = os.path.join(OUT, "%s-01.log" % name)
    if os.path.lexists(target):
        sys.exit("Capture '%s' exists (%s). Give another NAME." % (name, target))
    hw = hwmon_dir()
    dev, first = None, b""
    for info in hid.enumerate(VID, PID):
        candidate = hid.device()
        try:
            candidate.open_path(info["path"])
        except Exception:
            continue
        first = bytes(candidate.read(INPUT_SIZE, 2000))
        if first[:1] == bytes([INPUT_ID]):
            dev = candidate
            break
        candidate.close()
    if dev is None:
        sys.exit("No high flow NEXT interface delivered input report 0x01 (need root?)")

    start = time.monotonic()
    lines, sample, misses = [], first, 0
    try:
        while True:
            if sample[:1] == bytes([INPUT_ID]):
                lines.append("%7.3f %s | %s" % (time.monotonic() - start, sample.hex(),
                                                hwmon_readings(hw)))
            if time.monotonic() - start >= INPUT_SECONDS:
                break
            sample = bytes(dev.read(INPUT_SIZE, 2000))
            misses = 0 if sample else misses + 1
            if misses >= 3:
                print("no report for 6 s; stopping early")
                break
    finally:
        dev.close()

    if not os.path.isdir(OUT):
        os.makedirs(OUT)
        give_back(OUT)
    header = ("# high flow NEXT, input report %#04x (%d bytes incl. id), captured %s\n"
              "# seconds since the first report | report, hex | hwmon *_input at receipt\n"
              % (INPUT_ID, INPUT_SIZE, time.strftime("%Y-%m-%d %H:%M:%S")))
    save(target, (header + "\n".join(lines) + "\n").encode())
    span = float(lines[-1].split()[0]) if lines else 0.0
    print("input report %#04x: %d reports over %.1f s -> %s"
          % (INPUT_ID, len(lines), span, target))


if len(sys.argv) > 1 and sys.argv[1] == "--input":
    capture_input(sys.argv[2] if len(sys.argv) > 2 else "input")
    sys.exit(0)

name = sys.argv[1] if len(sys.argv) > 1 else "baseline"
paths = {rid: os.path.join(OUT, "%s%s.bin" % (name, "" if rid == 0x03 else "-%02x" % rid))
         for rid, _ in REPORTS}
taken = [p for p in paths.values() if os.path.lexists(p)]
if taken:
    sys.exit("Capture '%s' exists (%s). Give another NAME." % (name, taken[0]))

path = None
for info in hid.enumerate(VID, PID):
    probe = hid.device()
    try:
        probe.open_path(info["path"])
        time.sleep(DELAY)
        probe.get_feature_report(0x03, 682)
        path = info["path"]
    except Exception:
        pass
    finally:
        try:
            probe.close()
        except Exception:
            pass
    if path:
        break
if not path:
    sys.exit("No high flow NEXT interface answered report 0x03 (need root?)")

if not os.path.isdir(OUT):
    os.makedirs(OUT)
    give_back(OUT)

dev = hid.device()
dev.open_path(path)
blobs = {}
try:
    for rid, size in REPORTS:
        time.sleep(DELAY)
        try:
            raw = bytes(dev.get_feature_report(rid, size))
        except Exception as exc:
            print("report %#04x: FAILED (%s)" % (rid, exc))
            continue
        blobs[rid] = raw
        save(paths[rid], raw)
        scheme = framing(raw) if len(raw) > 3 else None
        print("report %#04x: %d of %d bytes, id %#04x, checksum: %s"
              % (rid, len(raw), size, raw[0] if raw else -1,
                 scheme or "none of the known schemes"))
        print("             -> %s" % paths[rid])
finally:
    dev.close()

for rid, raw in blobs.items():
    runs = printable_runs(raw)
    if runs:
        print("\n=== text in report %#04x ===" % rid)
        for off, text in runs[:40]:
            print("  @0x%03x  %r" % (off, text))
        if len(runs) > 40:
            print("  ... %d more" % (len(runs) - 40))
