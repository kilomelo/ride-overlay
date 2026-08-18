"""Qt graphical editor for ride-overlay projects."""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from ride_overlay_gui.main import main as gui_main

    return gui_main(argv)
