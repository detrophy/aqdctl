# USB captures of Aquasuite

USBPcap captures (pcapng, link type 249) of Aquasuite talking to a high flow
NEXT and an Octo, taken on the Windows machine that runs Aquasuite. They show
how Aquasuite writes: which reports, of which type, in which order and with
which delays. `../highflow/LAYOUT.md` ("How Aquasuite writes") summarises
them.

- `NN` is the capture order; the second part of each name is the device.
- Each capture holds one action, with a few seconds of quiet before and after
  it so the write stands out from the device's once-a-second live reports.
- Both devices' serial numbers are zeroed in every live report (report 0x01,
  bytes 0x03–0x06). The capture host's hardware and operating system strings
  are removed from the file headers. Nothing else is changed.

Wireshark opens these directly.
