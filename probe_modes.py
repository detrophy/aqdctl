#!/usr/bin/env python3
"""Probe unknown RGB effect ids on one controller.

Writes each candidate id, reads the settings report back, and reports whether
the firmware kept the value or replaced it. Restores the original slot at the
end, and on Ctrl-C. Read-back is the signal: an id the firmware rejects or
normalises tells us as much as one it accepts.

Usage:  sudo python3 probe_modes.py --device SERIAL CONTROLLER [--ids ...]
'python3 aqdctl.py info' lists the serials.
"""
import argparse, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aqdctl import core, discovery, rgbpx

ap = argparse.ArgumentParser()
ap.add_argument("--device", required=True, metavar="SERIAL",
                help="the device's serial; 'python3 aqdctl.py info' lists them")
ap.add_argument("controller", type=int, help="controller number, as 'info rgb' shows it")
ap.add_argument("--ids", default="0x06,0x19,0x1a,0x1b,0x1c,0x1d,0x1e,0x1f,0x20",
                help="comma-separated ids to try")
ap.add_argument("--settle", type=float, default=1.5,
                help="seconds to leave each id applied so you can look at the LEDs")
args = ap.parse_args()

found = discovery.find(args.device)
if found is None:
    sys.exit("No attached device has the serial %r." % args.device)
layout = found.kind.RGB
if not 1 <= args.controller <= layout.count:
    sys.exit("The %s's controllers are 1-%d." % (found.kind.TITLE, layout.count))
ids = [int(x, 0) for x in args.ids.split(",")]
base = layout.slot_base(args.controller)

with core.Device(found.kind, found.path, found.serial) as dev:
    original = dev.read()
    print("backup: %s" % core.backup(original, dev))
    print("controller %d currently mode %#04x (%s)\n"
          % (args.controller, original[base + rgbpx.RGB_MODE],
             rgbpx.RGB_MODES.get(original[base + rgbpx.RGB_MODE], "unknown")))
    results = []
    try:
        for want in ids:
            buf = bytearray(dev.read())
            buf[base + rgbpx.RGB_MODE] = want
            core.reseal(buf)
            dev.write(buf)
            time.sleep(args.settle)
            got = dev.read()[base + rgbpx.RGB_MODE]
            verdict = "kept" if got == want else "REPLACED with %#04x" % got
            results.append((want, got, verdict))
            print("  %#04x -> %s" % (want, verdict))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        print("\nrestoring original slot...")
        buf = bytearray(dev.read())
        buf[base:base + rgbpx.RGB_STRIDE] = original[base:base + rgbpx.RGB_STRIDE]
        core.reseal(buf)
        dev.write(buf)
        back = dev.read()[base + rgbpx.RGB_MODE]
        print("controller %d back to mode %#04x %s"
              % (args.controller, back,
                 "(ok)" if back == original[base + rgbpx.RGB_MODE] else "(MISMATCH)"))

    if results:
        kept = [hex(w) for w, g, v in results if v == "kept"]
        print("\naccepted by the firmware: %s" % (", ".join(kept) or "none"))
        print("rejected: %s" % (", ".join(hex(w) for w, g, v in results
                                         if v != "kept") or "none"))
