"""Which supported devices are attached, told apart by serial.

The serial is the USB serial number, which on Aquacomputer devices is the same
serial Aquasuite shows (e.g. 12345-67890). It is readable without root, from
the USB descriptor, and it is the only identifier that does not move: hidraw
and hwmon numbers change between boots, and even between two runs."""

import glob
import os
import sys

from . import core


class Found:
    """An attached device before it is opened: its kind, serial and hidraw path."""

    def __init__(self, kind, serial, path):
        self.kind, self.serial, self.path = kind, serial, path


def device_kinds():
    """USB product id -> the module describing that device."""
    from . import highflow, octo
    return {octo.PRODUCT_ID: octo, highflow.PRODUCT_ID: highflow}


def attached():
    if core.hid is None:
        return []
    kinds = device_kinds()
    found = []
    for info in core.hid.enumerate(core.VENDOR_ID):
        kind = kinds.get(info.get("product_id"))
        if kind is None:
            continue
        serial = (info.get("serial_number") or "").strip()
        found.append(Found(kind, serial, info["path"]))
    return sorted(found, key=lambda f: (f.kind.NAME, f.serial))


def find(serial, candidates=None):
    for f in attached() if candidates is None else candidates:
        if f.serial == serial:
            return f
    return None


def hwmon_dir(serial):
    """The kernel driver's hwmon directory for this device, or None.

    Matched by serial through the HID device behind the hwmon node, never by
    hwmon name: two devices of one kind share a name."""
    for node in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(node, "device", "uevent")) as fh:
                if "HID_UNIQ=%s" % serial in (line.strip() for line in fh):
                    return node
        except OSError:
            continue
    return None


def list_devices(found, out=None):
    out = out or sys.stdout
    supported = sorted("%s (%04x:%04x)" % (k.TITLE, core.VENDOR_ID, pid)
                       for pid, k in device_kinds().items())
    if not found:
        print("No supported Aquacomputer device found on USB.", file=out)
        print("Supported: %s." % ", ".join(supported), file=out)
        return
    print("Attached devices:", file=out)
    print("  %-16s %-12s %-9s %s" % ("device", "serial", "firmware", "names stored on it"),
          file=out)
    unreadable = False
    for f in found:
        labels, live = core.peek(f, core.LABEL_REPORT_ID)
        unreadable = unreadable or labels is None
        print("  %-16s %-12s %-9s %s"
              % (f.kind.TITLE, f.serial or "(none)",
                 core.firmware(live) if live else "-",
                 f.kind.identify(labels) if labels is not None else "-"), file=out)
    print(file=out)
    if unreadable:
        print("Firmware and names need access to the device: run as root, or install\n"
              "the udev rule that 'aqdctl --help-udev' prints.\n", file=out)
    print("Every other command takes the serial first, e.g.", file=out)
    print("  aqdctl --device %s info" % (found[0].serial or "SERIAL"), file=out)
