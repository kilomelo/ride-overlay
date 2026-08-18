"""Command-line entry point for the Qt editor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from ride_overlay_gui.project import EditorProject
from ride_overlay_gui.window import EditorWindow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ride-overlay-editor",
        description="在实际骑行视频上预览并编辑 ride-overlay 仪表盘",
    )
    parser.add_argument("project_dir", type=Path, help="包含 config.json 和素材的项目目录")
    return parser


def run_editor(project_dir: Path) -> int:
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("ride-overlay")
    application.setOrganizationName("ride-overlay")
    application.setStyle("Fusion")
    try:
        model = EditorProject.load(project_dir)
    except Exception as exc:
        QMessageBox.critical(None, "ride-overlay 无法打开项目", str(exc))
        return 2
    window = EditorWindow(model)
    window.show()
    return application.exec()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_editor(args.project_dir)


if __name__ == "__main__":
    raise SystemExit(main())
