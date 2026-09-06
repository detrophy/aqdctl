#!/usr/bin/env python3
"""Probe unknown RGB effect ids on one controller.

Writes each candidate id, reads the control report back, and reports whether
the firmware kept the value or replaced it. Restores the original slot at the
end, and on Ctrl-C. Read-back is the signal: an id the firmware rejects or
normalises tells us as much as one it accepts.
"""
import argparse, importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("octoctl", os.path.join(HERE, "octoctl.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

ap = argparse.ArgumentParser()
ap.add_argument("controller", type=int, choices=range(1, 13))
ap.add_argument("--ids", default="0x06,0x19,0x1a,0x1b,0x1c,0x1d,0x1e,0x1f,0x20",
                help="comma-separated ids to try")
ap.add_argument("--settle", type=float, default=1.5,
                help="seconds to leave each id applied so you can look at the LEDs")
args = ap.parse_args()

ids = [int(x, 0) for x in args.ids.split(",")]
base = m.RGB_BASE + m.RGB_STRIDE * (args.controller - 1)

with m.Octo.find() as octo:
    original = octo.read()
    backup = m.backup(original)
    print("backup: %s" % backup)
    print("controller %d currently mode %#04x (%s)\n"
          % (args.controller, original[base + m.RGB_MODE],
             m.RGB_MODES.get(original[base + m.RGB_MODE], "unknown")))
    results = []
    try:
        for want in ids:
            buf = bytearray(octo.read())
            buf[base + m.RGB_MODE] = want
            m.reseal(buf)
            octo.write(buf)
            time.sleep(args.settle)
            got = octo.read()[base + m.RGB_MODE]
            verdict = "kept" if got == want else "REPLACED with %#04x" % got
            results.append((want, got, verdict))
            print("  %#04x -> %s" % (want, verdict))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        print("\nrestoring original slot...")
        buf = bytearray(octo.read())
        buf[base:base + m.RGB_STRIDE] = original[base:base + m.RGB_STRIDE]
        m.reseal(buf)
        octo.write(buf)
        back = octo.read()[base + m.RGB_MODE]
        print("controller %d back to mode %#04x %s"
              % (args.controller, back,
                 "(ok)" if back == original[base + m.RGB_MODE] else "(MISMATCH)"))

    if results:
        kept = [hex(w) for w, g, v in results if v == "kept"]
        print("\naccepted by the firmware: %s" % (", ".join(kept) or "none"))
        print("rejected: %s" % (", ".join(hex(w) for w, g, v in results
                                         if v != "kept") or "none"))
