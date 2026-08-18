from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ride_overlay_gui.config_sync import ConfigSynchronizer
from ride_overlay_gui.timeline import format_offset_frames


def _config() -> dict:
    return {
        "schema_version": 1,
        "inputs": {},
        "clip": {},
        "output": {
            "width": 320,
            "height": 180,
            "fps": 30,
            "background": {"mode": "chroma_key", "chroma_key_color": "#00FF00"},
        },
        "dashboards": [
            {
                "id": "speed",
                "source": "speed",
                "font_size": 30,
                "anchor": {"x": 0.5, "y": 0.5},
            },
            {
                "type": "heartbeat",
                "id": "heart",
                "width": 0.1,
                "anchor": {"x": 0.2, "y": 0.3},
            },
        ],
    }


def test_gui_writes_geometry_with_bounded_precision_and_upgrades_schema(
    tmp_path: Path, qtbot
) -> None:
    del qtbot
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config()), encoding="utf-8")
    sync = ConfigSynchronizer(tmp_path)

    sync.update_dashboard_geometry(
        "heart",
        anchor_x=0.123456789,
        anchor_y=0.987654321,
        width=0.333333333,
    )
    sync.flush()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert saved["dashboards"][1]["anchor"] == {"x": 0.123, "y": 0.988}
    assert saved["dashboards"][1]["width"] == 0.333


def test_gui_writes_offset_as_integer_frames(tmp_path: Path, qtbot) -> None:
    del qtbot
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config()), encoding="utf-8")
    sync = ConfigSynchronizer(tmp_path)

    sync.update_offset_frames(-27)
    sync.flush()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["timeline"]["activity_start_offset_frames"] == -27


def test_offset_display_uses_frames_for_integer_fps() -> None:
    assert format_offset_frames(4979, 30) == "+00:02:45:29"
    assert format_offset_frames(-1, 30) == "-00:00:00:01"


def test_external_valid_and_invalid_edits_are_reported(tmp_path: Path, qtbot) -> None:
    del qtbot
    path = tmp_path / "config.json"
    raw = _config()
    path.write_text(json.dumps(raw), encoding="utf-8")
    sync = ConfigSynchronizer(tmp_path)
    changes: list[bool] = []
    errors: list[str] = []
    sync.configChanged.connect(lambda: changes.append(True))
    sync.configError.connect(errors.append)

    raw["dashboards"][0]["font_size"] = 42
    path.write_text(json.dumps(raw), encoding="utf-8")
    sync._read_external()
    path.write_text("{", encoding="utf-8")
    sync._read_external()

    assert changes == [True]
    assert errors and "JSON 无效" in errors[0]
