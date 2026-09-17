# high flow NEXT: report layout

Mapped from the captures in this directory, taken with `../probe_highflow.py`
(GET_REPORT only). Nothing here has been written to the device.

USB `0c70:f012`. The kernel driver `aquacomputer_d5next` binds it as hwmon
`highflownext` and exposes it read-only: no attribute is writable.

Offsets are absolute byte positions, with the report id at offset 0.
Multi-byte values are big-endian. **Confirmed** means a capture that changed
one setting moved exactly that value. Everything else is marked as unconfirmed.

Capture method: captures are numbered in the order they were taken. Git
does not keep file times, so the order is in the name. Each capture changes
one setting and undoes the previous change. Some `*-display-*` captures do
not undo it, and some undo it without restoring the original value. Diff each
capture against the one numbered just before it, never against
`01-baseline`. A `.txt` file with the same number records what the name
cannot.

USB captures of Aquasuite itself (USBPcap, in `../usb-captures/`) show how it
talks to the device; see "How Aquasuite writes".

Scrubbed before publication:

- Both devices' serial numbers (report 0x01, bytes 0x03–0x06) are zeroed
  in every capture: `85-live-readings-01.log` and all of `../usb-captures/`.
- The pcapng headers no longer carry the capture host's hardware and
  operating system strings.
- Nothing else was changed. The settings, name and framebuffer reports
  never contained a serial.


## Reports

| Id   | Kind    | Size incl. id | Content                                             |
|------|---------|--------------:|-----------------------------------------------------|
| 0x01 | input   |           164 | live readings, sent once a second                   |
| 0x02 | output  |            11 | command frame, see "How Aquasuite writes"          |
| 0x03 | feature |           682 | settings                                            |
| 0x08 | feature |           773 | names                                               |
| 0x0c | feature |          1025 | display framebuffer                                 |

Ids and sizes come from the HID report descriptor
(`/sys/class/hidraw/hidrawN/device/report_descriptor`).

**Checksum** (0x03 and 0x08): CRC-16/USB over bytes 1 to size−3, stored
big-endian in the last two bytes. This is the Octo's scheme; aqdctl's
`core.crc16_usb` and `core.reseal` apply unchanged. Report 0x0c has no checksum.

`02-idle-no-change` (no change made) left 0x03 and 0x08 byte-identical, so neither report
contains values that change on their own.


## 0x03: settings

### Display

| Offset | Type        | Meaning                                                   | Capture                     |
|--------|-------------|-----------------------------------------------------------|-----------------------------|
| 0x003  | u8          | temperature unit: 0 °C, 1 °F                              | `22-display-temperature-unit-celsius-to-fahrenheit`       |
| 0x004  | u8          | flow and volume unit on the display: 0 litres, 2 US gallons | `23-display-flow-unit-litres-to-gallons` |
| 0x006  | u8          | seconds between page changes; 61 = off                    | `15-display-page-interval-5s-to-10s`, `16-display-page-change-off` |
| 0x009  | u16         | active pages, bit n−1 = page n                            | `*-display-pages-*`    |
| 0x00f  | u8          | brightness: 0 high, 1 medium, 2 low                       | `*-display-brightness-*`      |
| 0x010  | u8          | idle brightness: 0 high, 1 medium, 2 low, 3 off           | `*-display-idle-brightness-*` |
| 0x015  | bits        | 0x01 rotate, 0x04 invert, 0x08 auto-invert, 0x10 device keys disabled, 0x20 menu locked | `17-display-rotate-on` … `21-display-lock-menu-checked` |
| 0x016  | 4 × (u16, u16) | charts 1–4: source, then interval                      | `*-display-chart*`          |

- 0x004: Aquasuite offers only litres and gallons, and never writes 1. A
  writer must not write 1 either.
- 0x004 changes only the display. The calibration points at 0x045 stay in
  dL/h. The display showed 53.7 g/h at about 204 l/h, i.e. 3.8 l per gallon,
  so these are US gallons.
- 0x006: 3–60 s, 61 = off. Baseline is 5. Re-enabling page changes in
  Aquasuite writes 60, not the previous value.
- 0x009: baseline 0x0006, i.e. pages 2 and 3.
- Aquasuite's own writes match the 0x009 and 0x010 entries: switching page 4
  on set bit 0x08, and idle brightness "low" wrote 2 over 3
  (`09-highflow-display-idle-brightness-low-and-page-4-on` in `../usb-captures/`).
- 0x015: baseline 0x08, auto-invert on. Bit 0x02 is unknown.

**Chart source:** 0 flow, 1 internal temperature, 2 external temperature,
3 conductivity, 4 water quality, 5 power dissipation, 6 system voltage. The
titles on the rendered chart pages confirm 0, 1 and 5.

**Chart interval:** Aquasuite offers these eight values:

| seconds | 0.5 | 1  | 5  | 10  | 30  | 60  | 300  | 600  |
|---------|----:|---:|---:|----:|----:|----:|-----:|-----:|
| stored  |   1 | 10 | 50 | 100 | 300 | 600 | 3000 | 6000 |

Tenths of a second, except 0.5 s, which is stored as 1 rather than 5. Write
only these eight values: because of the exception, the device's rule for any
other value is unknown. Charts 3 and 4 held 1 at baseline. 1 s and 30 s are
from `68-display-chart1-1s-chart2-30s`.

**Pages** (0x009), identified from report 0x0c in each single-page capture:

| #  | Page                   | #  | Page                                  |
|---:|------------------------|---:|---------------------------------------|
|  1 | logo                   |  9 | flow + internal temperature           |
|  2 | flow                   | 10 | conductivity + water quality          |
|  3 | internal temperature   | 11 | internal + external temperature       |
|  4 | external temperature   | 12 | flow + volume                         |
|  5 | conductivity           | 13 | chart 1                               |
|  6 | water quality          | 14 | chart 2                               |
|  7 | volume                 | 15 | chart 3                               |
|  8 | power dissipation      | 16 | chart 4                               |

### Sensors and flow calculation

| Offset | Type      | Meaning                                                   | Capture                        |
|--------|-----------|-----------------------------------------------------------|--------------------------------|
| 0x02b  | s16       | internal temperature offset, 0.01 °C (−0.5 → 0xffce)      | `09-internal-sensor-offset-0-to-minus-0.5`              |
| 0x02d  | s16       | external temperature offset, 0.01 °C (−0.8 → −1.3 = −80 → −130) | `69-external-sensor-offset-minus-0.8-to-minus-1.3` |
| 0x02f  | u8        | coolant: 0 DP Ultra, 1 distilled water                    | `06-flow-coolant-dp-ultra-to-distilled`    |
| 0x030  | u8        | connector: 0 larger than 7 mm, 1 smaller than 7 mm        | `07-flow-connector-over-7mm-to-under-7mm` |
| 0x031  | 10 × s16  | manual calibration, correction per point, ×100 (−1 → −100) | `08-flow-manual-calibration-set`, `../usb-captures/10-highflow-flow-calibration` |
| 0x045  | 10 × u16  | the flow rates those corrections apply at, dL/h: 200 … 3000 = 20 … 300 l/h | `../usb-captures/10-highflow-flow-calibration` |
| 0x292  | u16       | water quality 100 % point, 0.1 µS/cm (12.8 → 13.3)        | `05-water-quality-range-12.8-50.0-to-13.3-50.5`      |
| 0x294  | u16       | water quality 0 % point, 0.1 µS/cm (50.0 → 50.5)          | `05-water-quality-range-12.8-50.0-to-13.3-50.5`      |

- 0x02b is the internal sensor by elimination, since 0x02d is the external
  one.
- Both offsets store Aquasuite's displayed value × 100, with the same sign.
  Aquasuite showed the 0x02d change as −0.8 → −1.3; the capture name omits
  the minus signs.
- For 0x02f and 0x030, other values of the coolant and connector lists are
  unknown.
- The correction unit at 0x031 was not recorded; the stored value is the
  entered value × 100. Aquasuite's range is −50 to +50: both ends were written
  in `../usb-captures/11-highflow-flow-calibration-min-max`.
- Both arrays are writable and fully confirmed: in
  `../usb-captures/10-highflow-flow-calibration` Aquasuite moved seven of the
  ten rates (30 → 31, 50 → 51, 100 → 99, 125 → 124, 150 → 149, 200 → 202,
  250 → 248 l/h) and every one of the ten corrections, one field per write.
  Each write touched only the two bytes of the value changed, so both strides
  hold across the whole array.
- The rates rise in every capture. Aquasuite's own limits for the rates were
  not captured, and neither was whether the table can hold other than ten
  entries. The rates were back at their factory values by capture 11, so
  something restores them; how was not captured either.

Water quality is linear in conductivity:
`(0% point − conductivity) / (0% point − 100% point)`. This was checked
against live values: predicted 85.48 %, device 85.49 %.

### RGBpx

| Offset | Type     | Meaning                                                  | Capture         |
|--------|----------|----------------------------------------------------------|-----------------|
| 0x059  | u8       | brightness, 0–255                                        | `04-rgb-brightness-45` |
| 0x05a  | u8       | 0, unknown                                               | —               |
| 0x05b  | u8       | on/off: 0x00 on, 0x02 off                                | `03-rgb-switch-on`        |
| 0x05c  | 8 × 70 B | controllers, in the Octo's slot format                   | aqdctl's decoder |

- 0x059: Aquasuite's slider moves one byte per step on both devices (High
  Flow Next 19 → 20 → 21 → 22; Octo 255 → 254 → 253). One shown percentage
  covers several bytes: "45 %" stored 114 on the Octo and 115 here. There is
  no conversion rule to copy.
- Which percentage Aquasuite shows for a byte cannot be read from the
  captures. The slider's position moves a pixel per click, its value changes
  only after several pixels, and the shown number is not recorded at each
  write. The rule does not matter to a writer: the device stores only the
  byte.
- 0x05b: the same inverted sense as the Octo.
- 0x05c: port byte 0 is channel 1, the external header (up to 90 LEDs).
  Port byte 1 is channel 2, the built-in LEDs on top (10). `03-rgb-switch-on` lit the
  external strip, and the only active controller is on port 0.
- Brightness and the on/off byte sit 3 and 1 bytes before the controller
  block, the same as on the Octo.

### System

| Offset | Type | Meaning                                                  | Capture                     |
|--------|------|----------------------------------------------------------|-----------------------------|
| 0x027  | u8   | allow exceeding the USB spec: 0 / 1                      | `*-usb-*`     |
| 0x028  | u16  | USB current limit, mA: 500 default, 2000 at the slider's top | `*-usb-*` |
| 0x02a  | u8   | aquabus address, baseline 58                             | `*-aquabus-address-*`  |
| 0x28d  | bits | standby flags, see below                                 | `*standby-*`          |

0x28d flags (baseline 0xf3):

- Standby is allowed when: 0x01 USB is not connected, 0x02 a USB suspend is
  received, 0x04 there is no aquabus connection.
- In standby: 0x10 alarm detection off, 0x20 display off, 0x40 LEDs off,
  0x80 volume counter stops.
- Bit 0x08 is unknown.

The USB current limit is the current the device draws from the USB port for
the LEDs. Anything above 500 mA exceeds USB 2.0. A writer must cap it at 2000
and refuse anything above 500 unless 0x027 is set.

### Alarms

| Offset | Type | Meaning                                                    | Capture                     |
|--------|------|------------------------------------------------------------|-----------------------------|
| 0x29a  | bits | alarm actions, see below                                   | `70-alarm-activate-buzzer-unchecked`, `71-alarm-blink-internal-led-red-unchecked`, `73-alarm-disable-fan-flow-signal-unchecked` |
| 0x29b  | bits | alarms enabled, see below                                  | `8?-alarm-*`        |
| 0x29d  | u8   | ignore alarms after startup, seconds, 5–100 (baseline 15)  | `72-alarm-startup-delay-15s-to-10s`           |
| 0x29e  | u16  | flow alarm limit, dL/h: 0–1000 l/h = 0–10000 (baseline 450 = 45 l/h) | values match `84-alarm-limits-and-ranges.txt` |
| 0x2a0  | u16  | temperature sensor alarm limit, 0.01 °C, 5–100 °C (baseline 4000) | values match `84-alarm-limits-and-ranges.txt` |
| 0x2a2  | u16  | external sensor alarm limit, 0.01 °C, 5–100 °C (baseline 4500) | `84-alarm-limits-and-ranges.txt`, alarm screen |
| 0x2a4  | u16  | water quality alarm limit, 0.01 %, 5–100 % (baseline 3000) | values match `84-alarm-limits-and-ranges.txt`   |
| 0x2a6  | u8   | signal output mode, see below (baseline 1)                 | `*-signal-output-*`           |

0x29a, alarm actions (baseline 0xe0, all three on):

- 0x80 sound the buzzer during an alarm
- 0x40 blink the internal LED red during an alarm
- 0x20 switch off the fan or flow signal during an alarm
- bits 0x01–0x10 unknown

0x29b, alarms enabled (baseline 0x02):

- 0x01 flow
- 0x02 temperature sensor
- 0x04 external sensor
- 0x08 water quality
- bits 0x10–0x80 unknown

0x2a6, signal output mode:

- 0 fan speed signal
- 1 high flow sensor signal
- 2 fan speed signal from the flow rate, 1000 rpm = 100 l/h
- 3 power switch: a 1 s pulse, which turns the PC off
- 4 output permanently on during an alarm. Used with an add-on on the
  motherboard's power header that shuts the system down.
- 5 output permanently on while there is no alarm, and off during one. Used
  with add-ons that cut power when the signal disappears.

Notes:

- 0x29d: 0x29c is 0, so the field may be a u16. For 5–100 the two are
  identical.
- No capture moved the four limits. Their positions come from four distinct
  values matching `84-alarm-limits-and-ranges.txt`, in the note's order and in the order of the
  0x29b bits. The alarm screen showed the external sensor's limit as 45 °C.
- The temperature sensor alarm always uses the internal sensor. Aquasuite
  offers no selection.
- With 0x20 set, modes 0–2 drop the output during an alarm. A motherboard
  that reads the output as a fan sees that fan stop.
- Whether an alarm is currently active is not stored in 0x03. The capture
  taken during an alarm changed only the enable bit.
- The alarm screen is in German ("Grenzwert", "Aktuell"). A display language
  setting may be among the unmapped bytes.

### Unmapped

- Bytes: 0x001–0x002 (`00 01`), 0x005, 0x007–0x008, 0x00b–0x00e,
  0x011–0x014 (u16 700, 600), 0x026, 0x05a, 0x28c, 0x28e–0x291,
  0x296–0x299 (`00 00 00 96`), 0x29c and 0x2a7 (`01`; it sits at size−3,
  where the Octo keeps its profile index).
- Bits: 0x02 of 0x015, 0x08 of 0x28d, 0x01–0x10 of 0x29a, 0x10–0x80 of
  0x29b.


## 0x01: live readings

The device sends this report on its own, once a second. It carries no
checksum. `85-live-readings-01.log` holds 11 consecutive reports, recorded passively
(nothing was sent to the device), with the kernel driver's hwmon readings
beside each one. 0x7fff marks a reading that is not available.

| Offset | Type      | Meaning                                            | Evidence                        |
|--------|-----------|----------------------------------------------------|---------------------------------|
| 0x03   | 2 × u16   | serial number: each u16 as 5 digits, zero-padded, joined by a dash, e.g. `01234-00567` | matches Aquasuite's device information |
| 0x0d   | u16       | firmware version (1017)                             | matches Aquasuite's device information |
| 0x51   | u16       | flow, dL/h                                          | = hwmon `fan1` in all 11 reports |
| 0x53   | u16       | flow, second copy                                   | equal to 0x51 in every report   |
| 0x55   | s16       | internal temperature, 0.01 °C                       | = hwmon `temp1`                 |
| 0x57   | s16       | external temperature, 0.01 °C; 0x7fff = no sensor   | hwmon `temp2` reports no data   |
| 0x59   | u16       | water quality, 0.01 %                               | = hwmon `fan2`                  |
| 0x5b   | s16       | power dissipation; 0 here, and the display showed 0 W | unconfirmed without an external sensor |
| 0x5f   | u16       | conductivity, 0.1 µS/cm                             | = hwmon `fan3`                  |
| 0x61   | u16       | +5 V supply, 0.01 V                                 | = hwmon `in0`                   |
| 0x63   | u16       | USB +5 V, 0.01 V                                    | = hwmon `in1`                   |
| 0x65   | u32       | volume, litres                                      | owner's Aquasuite reading; +1 in 10 s |
| 0x69   | u32       | impulse count                                       | owner's Aquasuite reading; +38.2/s at 206 l/h |
| 0x6d   | u32       | time since the volume counter was last reset, s     | owner's Aquasuite reading; +1/s |

- 0x53: an identical copy of the flow at 0x51. It may be the reading before
  the calibration correction. The corrections were all 0 during this capture,
  so the two could not differ.
- 0x65/0x69: about 666 impulses per litre.
- 0x94–0x9b repeat the internal temperature, external temperature, +5 V and
  USB +5 V in the same encoding. Both temperature copies include the offset:
  +1.00 appeared in both at the same moment.

Candidates, unconfirmed. Aquasuite does not show the time counter or the
power-on count, so the owner could not check them; both values are
plausible.

| Offset | Value                      | Candidate                                   |
|--------|----------------------------|---------------------------------------------|
| 0x14   | u32 136675099, +10/s       | time in 0.1 s: 158 d 04:31:50 at 19:25:03   |
| 0x18   | u32 246                    | power-on count                              |
| 0x2f–0x3e | 8 × 0x7fff              | software sensors 1–8, unset without Aquasuite |
| 0x5d   | u16 182                    | near conductivity (177); unidentified       |

Unidentified: 0x23–0x2e jitter by a few counts from report to report and
match no driver reading; they are probably raw measurements. 0x49 varies the
same way. Also unidentified: 0x07 (1000), 0x0b (100), 0x4b (1610) and
0x7e–0x83 (10000, 7500, 4878).


## 0x08: names

The Octo's format: a 3-byte header, then 32 slots of 24 bytes each,
NUL-padded Latin-1 (3 + 32 × 24 = 771, the checksum offset). The Octo has
42 slots.

| Slot  | Offset        | Baseline text        | Meaning                            |
|------:|---------------|----------------------|------------------------------------|
|     0 | 0x003         | high flow NEXT       | device                             |
|     1 | 0x01b         | VCC5                 | 5 V supply (hwmon `in0`)           |
|     2 | 0x033         | VCC5 USB             | USB 5 V (hwmon `in1`)              |
|   3–5 | 0x04b–0x07b   | (empty)              | unknown                            |
|     6 | 0x093         | Strip                | RGB channel 1, external            |
|     7 | 0x0ab         | Sensor               | RGB channel 2, built-in            |
|  8–15 | 0x0c3–0x16b   | LED Controller 1–8   | controllers                        |
|    16 | 0x183         | Cold Point           | internal temperature               |
|    17 | 0x19b         | External Sensor      | external temperature               |
|    18 | 0x1b3         | Flow                 |                                    |
|    19 | 0x1cb         | Conductivity         |                                    |
|    20 | 0x1e3         | Power Dissipation    |                                    |
|    21 | 0x1fb         | Volume               |                                    |
|    22 | 0x213         | Water quality        |                                    |
|    23 | 0x22b         | (empty)              | unknown                            |
| 24–31 | 0x243–0x2eb   | Soft. Sensor 1–8     | virtual sensors                    |

- The baseline texts are the device defaults, except "Cold Point": that name
  was set by the owner.
- The display draws its page titles from these slots. Slot 16 appears as
  the title of page 3.
- Slots 6 and 7 are assigned to channels by their order.


## 0x0c: display framebuffer

- 128 × 64 pixels at 1 bit per pixel, in eight bands of 128 column bytes.
  Pixel (x, y) is byte (y / 8) · 128 + x, bit y mod 8, with the LSB at the
  top (SSD1306 layout).
- Live: it differs between any two captures.
- It holds the picture before rotation and inversion. Captures taken with
  either setting look normal.
- No checksum.
- Read-only: nothing is known about what a SET_REPORT to it does.


## How Aquasuite writes

Taken from the captures in `../usb-captures/`.

- **Settings (0x03) and names (0x08):** a SET_REPORT (feature) control
  transfer carrying the whole report with a valid checksum. It differs from
  the device's state only in the setting changed. Aquasuite never reads a
  report before writing; it keeps its own copy.
- **Report 0x02** is a command frame, sent as a SET_REPORT (output) control
  transfer. The HID interface has no interrupt OUT endpoint; its only
  endpoint is interrupt IN 0x83, which carries report 0x01.

  | Byte | Content                                             |
  |------|-----------------------------------------------------|
  | 0    | report id 0x02                                      |
  | 1–3  | 0                                                   |
  | 4    | command                                             |
  | 5–8  | 0                                                   |
  | 9–10 | CRC-16/USB over bytes 1–8, big-endian               |

  | Command | Frame                              | Meaning                    |
  |---------|------------------------------------|----------------------------|
  | 0x02    | `02 00 00 00 02 00 00 00 00 34 c6` | sent after settings writes |
  | 0x64    | `02 00 00 00 64 00 00 00 00 3c ce` | reset the volume counter   |

- **Command 0x02** follows report 0x03 writes, about 3.8 s after the *last*
  one (3.62–3.84 s across all captures). The Octo gets the same frame as an output report, 3.84 s after its
  settings write (`07-octo-rgb-brightness-100-to-99-then-10s-idle`). Several quick changes get a single 0x02. It never follows a name write.
- **A setting takes effect at the settings write, not at command 0x02.**
  - An internal offset of +1.00 appeared in the next report 0x01, 0.83 s
    after the write and 3 s before the 0x02.
  - Setting it back to 0 showed up 0.15 s after that write.
  - Source: `08-highflow-internal-sensor-offset-0-to-1-to-0`.
  - So 0x02 applies nothing. It is presumably the save to flash, which
    Aquasuite delays until changes stop. Only a power-cycle test would prove
    the save.
- **Command 0x64** set volume, impulse count and time since reset to 0 at
  once. The next report 0x01, 0.26 s later, read 0 l, 10 impulses, 1 s. It
  cannot be undone.
- The frame for command 0x02 is byte for byte the constant aqdctl takes from
  the kernel driver.
- The vendor interface's bulk endpoints (0x81 IN, 0x02 OUT) carry nothing in
  any capture. Settings never go through them.


## Before any write

- **0x0c:** no writes.
- **USB current limit:** see System.
- **Alarms fire as soon as they are enabled if their condition already
  holds.** Enabling the external sensor alarm with no sensor attached raised
  it at once (`84-alarm-external-temperature-on-alarming`).
  Before a writer enables an alarm or changes a limit, it must check the
  live readings (hwmon). It must refuse, or ask for explicit confirmation,
  when the sensor reads nothing or the limit is already crossed.
- **Signal output modes 3, 4 and 5 can switch the PC off or cut its power.**
  In mode 3 an alarm presses the power button. In mode 4 it triggers a
  shutdown add-on. In mode 5 the add-on cuts power whenever the signal is
  absent.
  - Leaving mode 5 is dangerous in itself: modes 3 and 4 hold the output off
    outside an alarm, so switching to either cuts power. How such an add-on
    reacts to the pulse signals of modes 0–2 is unknown.
  - The device cannot report what is attached to its output, so a writer
    cannot know either. Any change to 0x2a6 that enters or leaves modes 3–5,
    and any alarm change while 0x2a6 is in 3–5, needs explicit
    confirmation.


## Kernel driver notes

- `fan3_input` (conductivity) is in 0.1 µS/cm, not the nS/cm its label says.
  The water quality formula above only works in 0.1 µS/cm.
- `power1_input` (dissipated power) reads 4294967235. That is −61 as an
  unsigned 32-bit value, and 61 is ENODATA, the "no data" error that `temp2`
  returns.
  - The device itself sends 0 at 0x5b, and its display shows 0 W.
  - So the driver marks power as unavailable because no external sensor is
    attached, and then hands that marker out as a reading.
  - Tools that show the value as-is report about 4.29 kW.


## Differences from the Octo that matter for aqdctl

- No fan channels.
- RGB: 8 controllers at 0x05c (the Octo has 12 at 0x307).
- The LED limit is per channel: 90 and 10 (the Octo has 90 on both).
- Names: 32 slots, in different groups.
- Settings that have no Octo counterpart: display, flow calculation, water
  quality range, USB current limit, standby, aquabus address, alarms and
  signal output.
