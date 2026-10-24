"""tuya_map: decoder for the Tuya cloud map files of robot vacuums (layout + path) with a standard-library PNG renderer.

Pure Python, no network, no Home Assistant imports. Formats are reconstructed from public sources and NOT verified on the ETA Tiger (see layout.py / path.py).
"""

from .errors import MapError
from .layout import Layout, Room, classify, parse_layout
from .path import Path, parse_path
from .render import render_png

__all__ = ["MapError", "Layout", "Room", "Path", "classify", "parse_layout", "parse_path", "render_png"]
