#!/usr/bin/env python3
"""aqdctl - read and change the settings stored on Aquacomputer devices.

  python3 aqdctl.py info                     list the attached devices
  python3 aqdctl.py --device SERIAL --help   that device's commands

The code lives in the aqdctl/ package next to this file."""

from aqdctl.cli import main

if __name__ == "__main__":
    main()
