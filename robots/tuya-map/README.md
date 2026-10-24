# tuya-map

Pure-Python decoder (LZ4 block, layout, path) and a standard-library PNG renderer for the Tuya cloud map files of robot vacuums. No network, no Home Assistant imports.

**Status (Task 32, 2026-10-24):** the file formats are reconstructed from the open-source `tuya-vacuum` library (MIT) and Tuya's laser-robot documentation. They are **not verified on the ETA Tiger**, and by Tuya's documentation these files are **not available over the LAN** (they come from the cloud, which Task 32 does not enable). All test fixtures are SYNTHETIC (invented rooms in a 40x30 grid, built by `tests/build.py`).

    python3 -m unittest discover -s tests

Never commit a real map: a floor plan is personal data.
