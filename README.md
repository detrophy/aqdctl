# aqdctl

A command-line tool for the Aquacomputer Octo and high flow NEXT on Linux. 
It reads and changes the settings stored on the device without Aquasuite. 
Changes are being saved direct to the device's memory, the same way 
Aquasuite writes them. As such, they will survive reboots and are
compatible with Aquasuite.

The protocol was reverse engineered. Every field aqdctl touches is backed by
a capture in this repository, and the test suite checks the code against
those captures.

This project is not affiliated with Aqua Computer GmbH & Co. KG.

## Status

| Device         | USB id      | State                                                        |
|----------------|-------------|--------------------------------------------------------------|
| Octo           | `0c70:f011` | Supported.                                                   |
| high flow NEXT | `0c70:f012` | Supported.                                                   |

aqdctl sends the same reports as Aquasuite, of the same types and in the
same order; USB captures of Aquasuite confirm this. Only the delay before the
final report differs: aqdctl waits 0.2 s, Aquasuite about 3.8 s. The Octo
support has been used on real hardware; the high flow NEXT's commands are so
far only checked against the captures.

## Requirements

- Linux and Python 3.
- `hidapi` - `pip install hidapi`
  on Arch: `pacman -S python-hidapi`
- Root access to supported hidraw devices.
  run aqdctl as root,
  or use the udev helper `aqdctl --help-udev` for root-less access.
- Optional:
  For the Octo `aquacomputer_d5next` used with `info` command for live fan speeds, 
  temperatures and flow.
  For the high flow NEXT the kernel driver is only a fallback,
  live readings come from the report the device sends every second.

## Getting started

```
git clone <repository URL>
cd aqdctl
sudo python3 aqdctl.py info
```

`info` only reads. Before the first write, add the flag `-n` or `--dry-run` 
to see what it would change. No root required.

## Commands

Without a device:

```
aqdctl info                     list the attached devices and their serials
aqdctl --help-udev              udev rule for root-less access
aqdctl --device SERIAL --help   the commands that device offers
```

### Supported commands

Both devices:

```
aqdctl --device SERIAL info [SECTION]       configuration and live readings
                       rgb ...              RGB channels 1-2
                       name ...             names stored on the device
                       backup [-o FILE]     save the raw reports to a file
                       restore FILE         write a saved report back to the device
```

Octo:

```
aqdctl --device SERIAL info [fan|rgb|sensor]
                       fan set CH ...       fan channels 1-8
                       sensor ...           temperature sensors and the flow meter
```

high flow NEXT:

```
aqdctl --device SERIAL info [display|flow|sensor|alarm|rgb|system]
                       display ...          brightness, pages, units, charts
                       flow ...             coolant, connector, calibration table
                       sensor ...           temperature offsets, water quality range
                       alarm ...            alarms, their limits and actions
                       signal-output ...    what the signal header puts out
                       system ...           USB current, standby, aquabus address
                       volume reset         reset the volume counter
```

Every level has `--help`. Displays units and ranges where they apply.
Most commands also show an example. `aqdctl --device SERIAL rgb effects`
lists every RGB effect with the colours, parameters and flags it accepts.

Terminology the commands use:

```
channel     a physical header. Octo: fan 1-8, rgb 1-2, sensor 1-4.
            high flow NEXT: rgb 1 is the external header (up to 90 LEDs),
            rgb 2 the 10 LEDs built into the top.
controller  one RGB config slot - "on channel N, LEDs A-B, effect X". Each
            channel has its own controller numbers: 1-6 and 7-12 on the Octo,
            1-6 and 7-8 on the high flow NEXT. Commands address a controller
            by that number, and 'info rgb' lists the ones in use.
position    an LED range on a channel, 1-based and inclusive: 1-15 is the
            first fifteen LEDs.
```

### Commands usage

Every line below takes `aqdctl --device SERIAL` in front. Notation:

```
CAPITALS    a typed value, e.g. CH 3 or PERCENT 40
a|b         one of these words
[...]       optional. Left out, a value is shown instead of changed, except
            where the command says otherwise.
```

Write flags, accepted by every command that changes something:

```
-n, --dry-run     read the device, print what would change, write nothing
-v, --verbose     also print the changed bytes
-y, --yes         skip the confirmation question
--backup FILE     where to put the automatic pre-write backup
--force           go past a refusal: a protected channel, a backup from
                  another device, or a high flow NEXT safety check
```

Reading, on both devices:

```
info [SECTION]                      what the device is configured to do, next to
                                    what it reads now. Octo sections: fan, rgb,
                                    sensor. high flow NEXT: display, flow,
                                    sensor, alarm, rgb, system. Without one,
                                    all of them.
```

Fan channels (Octo), CH is 1-8:

```
fan set CH mode fixed PERCENT       hold a constant power, 0-100
fan set CH mode follow CH2          copy another channel's output, CH2 is 1-8
fan set CH limits MIN MAX           lowest and highest power it may use, 0-100
fan set CH fallback PERCENT         power used when the sensor reads nothing
fan set CH boost on|off             brief full power when starting from standstill
fan set CH hold-min on|off          keep running at the minimum instead of stopping
fan set CH max-rpm [RPM]            full scale for Aquasuite's chart, 0-65535
fan set CH protect on|off           refuse writes to this channel unless --force

fan set CH mode target [CELSIUS] [--sensor S]
                                    regulate a sensor to a temperature, 0-100 C
    --sensor S                      1-4, 'flow', or '#N' for a raw index
    without CELSIUS it switches back to target mode and keeps the stored value

fan set CH mode curve [POINTS] [--linear TMIN TMAX PMIN PMAX]
                      [--startup CELSIUS] [--sensor S]
                                    follow a 16-point temperature/power curve
    POINTS                          16 TEMP:POWER pairs, e.g. 27:0,28.2:3.3,...
                                    temperatures 0-100 C, powers 0-100
    --linear TMIN TMAX PMIN PMAX    build those 16 points as a straight line
                                    between two temperatures and two powers,
                                    in the same ranges
    --startup CELSIUS               startup temperature, 0-100 C
    --sensor S                      1-4, 'flow', or '#N' for a raw index
    curve without additional arguments activates the mode and prints it

fan set CH pid [--preset NAME] [--p V] [--i V] [--d V]
               [--reset SECONDS] [--hysteresis KELVIN]
                                    PID tuning; prints the tuning if no flag is given
    --preset NAME                   fastest, fast, normal, slow or slowest
    --p V  --i V  --d V             the three factors; each overrides the preset
    --reset SECONDS                 reset time
    --hysteresis KELVIN             hysteresis in kelvin

presets           P     I     D   reset  hysteresis
  fastest (+2)  4000  3500  1000   0.5s     0.10 K
  fast    (+1)  2500  2000   500   1.0s     0.10 K
  normal   (0)  1400  1200     0   4.0s     0.20 K
  slow    (-1)  1000   800     0   8.0s     0.30 K
  slowest (-2)   500   300     0  10.0s     0.30 K
```

The device stores no preset name, only those five numbers, so `--preset` is
just a named set of them.

The 0-100 C limit on curve and startup temperatures is aqdctl's own. The
report stores hundredths of a degree, so the field holds far more, and what
Aquasuite allows was never captured.

Sensors (Octo):

```
sensor offset SENSOR [CELSIUS]      calibration offset, SENSOR 1-4,
                                    -15.00 to +15.00 C
sensor flow [IMPULSES]              flow meter calibration, 10-1000 impulses
                                    per litre
```

Display (high flow NEXT):

```
display brightness [high|medium|low]            brightness while in use
display idle-brightness [high|medium|low|off]   brightness when idle
display pages [N[,N...]]                        which pages it cycles through,
                                                1-16
display page-change [SECONDS|off]               seconds between pages, 3-60
display rotate [on|off]                         turn the picture upside down
display invert [on|off]                         invert the picture permanently
display auto-invert [on|off]                    invert it half of the time, so
                                                the OLED wears evenly
display device-keys [on|off]                    the keys on the device
display menu-lock [on|off]                      lock the device's menu
display units temperature [celsius|fahrenheit]  unit for temperatures
display units flow [litres|gallons]             unit for flow and volume

display chart CHART [--source SOURCE] [--interval SECONDS]
                                    what one of the four chart pages shows,
                                    CHART is 1-4; with no flag it prints both
    --source SOURCE                 flow, internal-temperature,
                                    external-temperature, conductivity,
                                    water-quality, power-dissipation,
                                    system-voltage
    --interval SECONDS              0.5, 1, 5, 10, 30, 60, 300 or 600 - the time
                                    between stored points, which sets the period
                                    the chart spans

pages
   1 logo                       9 flow + internal temperature
   2 flow                      10 conductivity + water quality
   3 internal temperature      11 internal + external temperature
   4 external temperature      12 flow + volume
   5 conductivity              13 chart 1
   6 water quality             14 chart 2
   7 volume                    15 chart 3
   8 power dissipation         16 chart 4
```

Flow calculation (high flow NEXT):

```
flow coolant [dp-ultra|distilled]       the coolant in the loop
flow connector [over-7mm|under-7mm]     inner diameter of the connectors
flow calibration [--points P1,...,P10] [--corrections C1,...,C10]
                                        ten flow rates and the correction at
                                        each; with neither flag the table is
                                        printed. Ten values per flag, comma
                                        separated. The rates are 0-750 l/h and
                                        have to rise, the factory ones being 20,
                                        30, 50, 70, 100, 125, 150, 200, 250
                                        and 300.
                                        Corrections run from -50 to +50, the
                                        range Aquasuite allows. The flow alarm
                                        sees the corrected flow.
```

Sensors (high flow NEXT):

```
sensor offset internal|external [CELSIUS]
                                    calibration offset, -15.00 to +15.00 C
sensor water-quality [GOOD BAD]     conductivity in uS/cm at 100 % and at 0 %
                                    water quality; GOOD below BAD
```

Both are refused while they would make an enabled alarm fire.

Alarms (high flow NEXT):

```
alarm flow [on|off] [--limit LITRES_PER_HOUR]
                                            fires below the limit, 0-1000 l/h
alarm internal [on|off] [--limit CELSIUS]   fires above the limit, 5-100 C
alarm external [on|off] [--limit CELSIUS]   fires above the limit, 5-100 C
alarm water-quality [on|off] [--limit PERCENT]
                                            fires below the limit, 5-100 %
alarm startup-delay [SECONDS]               alarms are ignored for this long
                                            after startup, 5-100 s
alarm buzzer [on|off]                       sound the buzzer during an alarm
alarm blink-led [on|off]                    blink the internal LED red
alarm stop-signal [on|off]                  disable the fan speed/flow signal
                                            output during an alarm
```

State and limit are both optional, but they can be given together:
`alarm flow on --limit 60` sets both, `alarm flow --limit 60` only the limit,
and `alarm flow` prints the current setting.

Signal output (high flow NEXT), and Aquasuite's name for each mode:

```
signal-output [MODE]

  fan-speed             Generate fan speed signal
  flow-sensor           Generate high flow sensor (53068) signal
                        (DP Ultra, inner diameter > 7mm)
  fan-speed-from-flow   Generate fan speed signal from flow rate
                        (1000 rpm = 100 l/h)
  power-switch          Power switch, activate for 1 second on alarm
                        (suitable for article no. 53216)
  on-during-alarm       Activate in case of alarm
  off-during-alarm      Activate permanently and deactivate in case of alarm
```

The last three need `--force`, and so does leaving them.

System (high flow NEXT):

```
system usb-current [MILLIAMPS]      most current drawn from USB for the LEDs,
                                    500-2000 in steps of 100. Above 500 needs
                                    --force; 500 or less also clears the
                                    device's permission to exceed the USB limit.
system aquabus-address [58|59|60|61]
                                    its address on the aquabus
system standby when-usb-disconnected [on|off]   allow standby when USB is
                                                not connected
system standby on-usb-suspend [on|off]          allow standby on a USB suspend
system standby without-aquabus [on|off]         allow standby without an
                                                aquabus connection
system in-standby alarms-off [on|off]           in standby, stop alarm detection
system in-standby display-off [on|off]          in standby, switch the display off
system in-standby leds-off [on|off]             in standby, switch the LEDs off
system in-standby volume-stops [on|off]         in standby, stop the volume counter
```

Volume counter (high flow NEXT):

```
volume reset                        set volume, impulse count and the time
                                    since the last reset to 0. Shows the
                                    counters first and always asks; -y does
                                    not skip that, -n sends nothing.
```

RGB (both devices):

```
rgb create CH pos FIRST-LAST --effect NAME [options]
                                    add a controller on channel CH for the LEDs
                                    FIRST-LAST; it takes the channel's lowest
                                    free controller number and prints it
rgb set controller N [pos FIRST-LAST] [options]
                                    change controller N; 'pos' moves or resizes
                                    it on its own channel, everything not
                                    mentioned stays
rgb remove controller N             clear one controller
rgb remove channel CH               clear every controller on a channel
rgb switch on|off                   the whole RGB function
rgb brightness PERCENT              global brightness for every channel, 0-100
rgb effects [NAME]                  what each effect accepts; reads nothing
                                    from the device

options for 'create' and 'set'
    --effect NAME                   required for 'create'; see the list below
    --colour RRGGBB[,RRGGBB...]     the effect's colours, in role order
    --background RRGGBB             background colour, for effects that have one
    --param NAME=VALUE              one effect parameter, repeatable,
                                    e.g. --param speed=25
    --flag NAME                     turn an effect flag on, repeatable
    --no-flag NAME                  turn one off, repeatable
    --sensor S                      drive the effect from a sensor. Octo: 1-4,
                                    'flow', '#N' or 'none'. high flow NEXT:
                                    '#N' or 'none' - how it numbers its sources
                                    has not been captured.
    --filter-rise N                 damping for rising sensor values, 0-255
    --filter-fall N                 damping for falling values, 0-255

effects
    static, breathing, rotating_rainbow, blinking, colour_change, sequence,
    scanner, laser, wave, colour_sequence, colour_shift, bar_graph, flame,
    rain, snowfall, stardust, colour_switch, swiping_rainbow, colour_gradient,
    sound_flash, sound_bars, sound_slider, sound_shift, ambientpx
```

Which colours, parameters and flags an effect takes differs per effect;
`rgb effects NAME` prints them. The last five are driven by an Aquasuite host
DLL: the device stores them, but nothing animates without Aquasuite running.

Names (both devices):

```
name list                           every name stored on the device
name GROUP INDEX [TEXT]             show or change one name, up to 23
                                    characters of single-byte (latin-1) text

Octo groups            fan 1-8, controller 1-12, sensor 1-4, flow 1-2,
                       virtual 1-16
high flow NEXT groups  controller 1-8, virtual 1-8, and sensor with the names
                       internal, external, flow, conductivity, power, volume
                       and water-quality instead of numbers
```

Backup and restore (both devices):

```
backup [-o FILE]                    write both feature reports to disk, byte for
                                    byte. Without -o they go to
                                    ~/.local/share/aqdctl/ with the device's
                                    type and serial in the name.
restore FILE                        write a saved report back
```

Examples (Octo):

```
aqdctl --device SERIAL fan set 3 mode fixed 40
                       fan set 3 mode curve --linear 30 45 20 100 --sensor 1
                       fan set 3 pid --preset fast
                       fan set 1 protect on
                       rgb create 2 pos 1-15 --effect static --colour FF0000
                       name fan 3 "Front Rad"
```

Examples (high flow NEXT):

```
aqdctl --device SERIAL display pages 2,3,13
                       display chart 1 --source flow --interval 60
                       alarm flow on --limit 60
                       sensor offset internal -0.5
                       rgb create 2 pos 1-10 --effect static --colour 0000FF
                       name sensor internal "Cold Point"
```

## Before writes

- Changes are saved to flash on device.
- To prevent mistakes, a protection mode was added that will refuse writes
  to one or more protected channels. 
  Set protected channels on the Octo with `aqdctl --device SERIAL fan set CH protect on`; 
  aqdctl then refuses writes to the channel unless `--force` flag is added.
  Protected channels are stored per serial, so if you have more than one
  Octo, you have to set them on each of them.

Every write goes through the same steps:

1. Read the current report and verify its checksum.
2. Display changes (with `-v`, changed bytes will be displayed).
3. Save the current state to `~/.local/share/aqdctl/`.
4. Ask for confirmation (to skip, add `-y`).
5. Write, then read the report back and compare.

Some high flow NEXT settings can switch the PC off, cut its power or damage
hardware. Those writes are refused unless the `--force` flag is added; 
a refusal prints the reason. Commands that require `--force` flag:

- `system usb-current` above 500 mA. It can damage the motherboard's USB
  header, a warning is printed whether or not you force it.
- `signal-output power-switch`, `on-during-alarm` and `off-during-alarm`.
  An alarm is able to switch the PC off or cut its power. This could lead to
  dataloss. 
- An alarm that would fire at once: switching one on, or changing its limit,
  its sensor's offset or the water quality range, while the reading is
  missing or already past the limit.

`volume reset` can't be undone, so it always asks; `-y` does not skip that.

To create a standalone backup, use `aqdctl --device SERIAL backup -o FILE`.

`aqdctl --device SERIAL restore FILE` restores a backup. 
Files checksum and device serial are verified first, restoring a settings report 
is refused while any channel is protected. Restoring provides the same 
safety checks as the commands; adding `--force` flag will ignore all safety measures.

Reports, as Aquasuite uses them:

| Report | Transfer                | Use                                                   |
|--------|-------------------------|-------------------------------------------------------|
| 0x03   | SET/GET_REPORT feature  | settings; whole report, checksum recomputed           |
| 0x08   | SET/GET_REPORT feature  | names                                                 |
| 0x02   | SET_REPORT output, 11 B | command frame; 0x02 after settings, 0x64 volume reset |
| 0x01   | interrupt IN            | live readings, 1/s, unsolicited                       |
| 0x0c   | GET_REPORT feature      | high flow NEXT framebuffer, read-only                 |

The vendor-specific bulk interface is unused.

## Repository

| Path                                              | Contents |
|---------------------------------------------------|----------|
| `aqdctl.py`                                       | Entry script: `python3 aqdctl.py --device SERIAL ...`. |
| `aqdctl/`                                         | The tool. `octo.py` and `highflow.py` are the field maps, with the evidence for each field; `core.py`, `rgbpx.py`, `names.py`, `discovery.py` and `cli.py` are shared by both devices. |
| `octo/`                                           | Octo report captures, numbered in the order taken ([README](octo/README.md)). |
| `highflow/`                                       | high flow NEXT captures, and [`LAYOUT.md`](highflow/LAYOUT.md), the report map. |
| `usb-captures/`                                   | USB captures of Aquasuite writing to both devices ([README](usb-captures/README.md)). |
| `probe.py`, `probe_bulk.py`, `probe_highflow.py`  | Read-only discovery scripts. |
| `probe_modes.py`                                  | Writes candidate RGB effect ids to one controller to find out which ones the firmware accepts, then restores the controller. |
| `tests/test_octo.py`                              | Runs every Octo command against the captures, no hardware needed: `python3 tests/test_octo.py`. |
| `tests/test_highflow.py`                          | The same for the high flow NEXT, and replays each capture's change: `python3 tests/test_highflow.py`. |

## Contributing captures

Captures of other devices, or of settings that are not mapped yet, are
welcome.

- Change one setting per capture, and note what was changed, from what to
  what.
- Name captures `NN-description`, numbered in the order taken. Git does not
  keep file times, each capture is compared with the one before it.
- For supported devices, `aqdctl --device SERIAL backup -o FILE` saves both reports.
- Before sharing, remove identifying data:
  - The live report 0x01 carries the device's serial number at bytes
    0x03-0x06. `probe_highflow.py --input` logs and USB captures contain it;
    the settings and name reports do not.
  - pcapng files from USBPcap or Wireshark record the capturing machine's
    hardware and operating system in their header.

## Acknowledgements

The fan control offsets, the report 0x02 frame and the pacing between
reports come from the Linux kernel driver `aquacomputer_d5next`, which was
the starting point for this work.

## License

Copyright (C) 2026 detrophy

Licensed under the GNU General Public License, version 3 or later. See
[LICENSE](LICENSE).
