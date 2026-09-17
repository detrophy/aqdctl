# Octo captures

Feature report dumps of an Aquacomputer Octo (USB `0c70:f011`), each taken
after changing one thing in Aquasuite. They are the evidence behind the field
map in `../aqdctl/octo.py`, and `../tests/test_octo.py` checks the code against
them.

- `NN-description.bin`: report 0x03, the settings, 1631 bytes.
- `NN-description-08.bin`: report 0x08, the names, 1013 bytes, taken at the
  same moment. Capture 01 has none.

`NN` is the capture order. Git does not keep file times, so the order is in
the name. Diff each capture against the one before it, not against 01. Most
captures change one thing, but some also carry changes made between captures.

Some captures are byte-identical on purpose, because the identity is the
finding:

- 14 equals 13: switching Aquasuite's curve setup from automatic to manual
  writes nothing.
- 29-32 and 36: profile selections made while the Aquasuite service was
  hung, which wrote nothing. 37 shows the write that followed.

Name reports repeat wherever no name changed.
