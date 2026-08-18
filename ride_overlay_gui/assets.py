"""Resolve GUI image assets in editable and installed layouts."""

from __future__ import annotations

import sys
from pathlib import Path


def image_asset(filename: str) -> Path:
    candidates = (
        Path(__file__).resolve().parent.parent / "assets" / "images" / filename,
        Path(sys.prefix) / "share" / "ride-overlay" / "assets" / "images" / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"找不到 GUI 图片素材 {filename}；已检查: {searched}")
