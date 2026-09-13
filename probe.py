#!/usr/bin/env python3
"""Read-only discovery: control report 0x03 (RGBpx region) + unknown report 0x08.
Issues GET_REPORT only. Writes nothing to the device."""
import os, struct, sys, hid

VID, PID = 0x0C70, 0xF011
REPORTS = {0x03: 1631, 0x08: 1013}          # sizes from the HID report descriptor
NAMES = ["Wasser", "Front Rad Hot", "Exhaust temp", "Pumpe 1", "Pumpe 2",
         "Unten Seite", "Heck", "Vorne", "Oben"]

def crc16_usb(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c ^ 0xFFFF

path = None
for info in hid.enumerate(VID, PID):
    try:
        p = hid.device(); p.open_path(info["path"])
        p.get_feature_report(0x03, 1631); p.close(); path = info["path"]; break
    except Exception: pass
if not path: sys.exit("No Octo interface answered report 0x03 (need root?)")

dev = hid.device(); dev.open_path(path)
blobs = {}
for rid, size in REPORTS.items():
    try:
        raw = bytes(dev.get_feature_report(rid, size))
    except Exception as e:
        print("report %#04x: FAILED (%s)" % (rid, e)); continue
    blobs[rid] = raw
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "octo",
                      "report_%02x.bin" % rid), "wb").write(raw)
    stored = struct.unpack_from(">H", raw, len(raw) - 2)[0]
    calc = crc16_usb(raw[1:len(raw) - 2])
    print("report %#04x: %d bytes, id=%#04x, trailing CRC %s (stored %#06x / calc %#06x)"
          % (rid, len(raw), raw[0], "MATCHES" if stored == calc else "no match", stored, calc))
dev.close()

print("\n=== searching both reports for your labels ===")
for rid, raw in blobs.items():
    for name in NAMES:
        for enc in ("ascii", "utf-16-le", "utf-16-be", "latin-1"):
            try: needle = name.encode(enc)
            except Exception: continue
            i = raw.find(needle)
            if i >= 0:
                print("  %#04x @0x%03x  %-14s as %s" % (rid, i, repr(name), enc))

print("\n=== printable runs (>=4 chars) in report 0x08 ===")
raw = blobs.get(0x08, b"")
for enc, step in (("ascii", 1), ("utf-16-le", 2)):
    print("-- %s --" % enc)
    run, start = [], None
    for i in range(0, len(raw) - step + 1, step):
        ch = raw[i] if step == 1 else (raw[i] if raw[i + 1] == 0 else 0)
        if 32 <= ch < 127:
            if start is None: start = i
            run.append(chr(ch))
        else:
            if len(run) >= 4: print("   @0x%03x: %s" % (start, "".join(run)))
            run, start = [], None
    if len(run) >= 4: print("   @0x%03x: %s" % (start, "".join(run)))

print("\n=== report 0x08 hexdump (first 320 B) ===")
for r in range(0, min(320, len(raw)), 16):
    row = raw[r:r + 16]
    print("  %04x: %-47s %s" % (r, " ".join("%02x" % x for x in row),
                                "".join(chr(x) if 32 <= x < 127 else "." for x in row)))

print("\n=== RGBpx slots in report 0x03 (70-byte Farbwerk-360 format) ===")
ctrl = blobs.get(0x03, b"")
if ctrl:
    base, stride = 0x307, 70
    n = 0
    while base + stride * n + stride <= len(ctrl) - 2:
        s = ctrl[base + stride * n: base + stride * n + stride]
        n += 1
        if s[0] == 0 and not any(s[:10]): continue
        pal = ["%02X%02X%02X" % (s[46+4*k+2], s[46+4*k+1], s[46+4*k+3]) for k in range(6)]
        print("  slot %2d @0x%03x  enable=%#04x mode=%#04x flags=%#04x width=%d"
              % (n-1, base+stride*(n-1), s[0], s[1], s[5], s[9]))
        print("       params LE:", [s[23+2*k] | (s[24+2*k] << 8) for k in range(9)])
        print("       palette   :", " ".join(pal))
    print("  (scanned %d slots from 0x%03x)" % (n, base))
    print("\n  tail after slots, 0x%03x..0x%03x:" % (base + stride * n, len(ctrl) - 1))
    print("   " + " ".join("%02x" % x for x in ctrl[base + stride * n:]))
