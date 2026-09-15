"""aqdctl - read and change the settings stored on Aquacomputer devices.

The package is split by what depends on the device and what does not:

  core       checksum, byte helpers, device I/O, write/backup/restore, the
             local config (keyed by serial)
  discovery  which supported devices are attached, found by USB serial
  rgbpx      the RGBpx controller format and its commands, shared by devices
  names      the name table format and its commands, shared by devices
  octo       the Octo: its report layout and its own commands
  highflow   the high flow NEXT: its report layout, live readings and commands
  cli        the command line: --device first, then that device's commands

A device module describes one product: its USB product id, report sizes,
layout, name groups, RGB layout, and the commands it offers. Adding a device
means adding one such module and its captures.
"""
