#!/usr/bin/env python3
"""READ-ONLY probe of the Octo's vendor-specific interface (class 0xff).

Claims interface 0 only - never interface 1, which the HID driver owns and
which carries fan control. Performs bulk IN reads and NOTHING else: no bulk
writes, no control transfers, no resets. If the device only answers requests
rather than pushing data, this will simply time out, which is itself the
answer.

RESULT (2026-09-06, firmware as shipped on this unit): six reads, six
timeouts. The device sends nothing unsolicited on ep 0x81, so the interface
is request/response. Going further means writing to the bulk OUT endpoint,
which must NOT be attempted blind - on Aquacomputer hardware a vendor bulk
interface is a plausible firmware-flashing path. The safe route is a USBPcap
capture of Aquasuite driving interface 0 (e.g. while switching profiles) and
replaying known-good requests from that.
"""
import sys, time

try:
    import usb.core, usb.util
except ImportError:
    sys.exit("pyusb not installed:  sudo pacman -S python-pyusb")

VID, PID = 0x0C70, 0xF011
IFACE, EP_IN = 0, 0x81
READS, TIMEOUT = 6, 1000          # ms

dev = usb.core.find(idVendor=VID, idProduct=PID)
if dev is None:
    sys.exit("Octo not found (need root?)")

cfg = dev.get_active_configuration()
intf = cfg[(IFACE, 0)]
print("interface %d: class=%#04x, endpoints %s"
      % (IFACE, intf.bInterfaceClass,
         ", ".join("%#04x(%s)" % (e.bEndpointAddress,
                                  usb.util.endpoint_direction(e.bEndpointAddress)
                                  and "in" or "out") for e in intf)))
if intf.bInterfaceClass != 0xFF:
    sys.exit("interface %d is not vendor-specific - refusing" % IFACE)

if dev.is_kernel_driver_active(IFACE):
    sys.exit("a kernel driver holds interface %d; refusing to detach it" % IFACE)

usb.util.claim_interface(dev, IFACE)
print("claimed interface %d (HID interface 1 untouched)\n" % IFACE)
got = []
try:
    for i in range(READS):
        try:
            data = dev.read(EP_IN, 64, timeout=TIMEOUT)
            got.append(bytes(data))
            print("  read %d: %d bytes  %s" % (i + 1, len(data), bytes(data).hex(" ")))
        except usb.core.USBTimeoutError:
            print("  read %d: timeout" % (i + 1))
        except usb.core.USBError as e:
            print("  read %d: USBError %s" % (i + 1, e))
            break
        time.sleep(0.2)
finally:
    usb.util.release_interface(dev, IFACE)
    usb.util.dispose_resources(dev)
    print("\nreleased interface %d" % IFACE)

print("\n%d of %d reads returned data." % (len(got), READS))
if not got:
    print("The device sends nothing unsolicited on this endpoint. Anything")
    print("further would require writing to the bulk OUT endpoint, which is")
    print("not something to do blind.")
