from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ride_overlay import AppConfig
from ride_overlay_gui.config_sync import ConfigSynchronizer
from ride_overlay_gui.project import EditorProject
from ride_overlay_gui.timeline import (
    format_offset_frames,
    format_timecode,
    frame_step_target,
)


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


def test_gui_writes_elevation_width_and_height(tmp_path: Path, qtbot) -> None:
    del qtbot
    raw = _config()
    raw["dashboards"].append(
        {
            "type": "elevation_profile",
            "id": "elevation",
            "width": 0.4,
            "height": 0.2,
            "anchor": {"x": 0.5, "y": 0.8},
        }
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    sync = ConfigSynchronizer(tmp_path)

    sync.update_dashboard_geometry("elevation", width=0.333333, height=0.166666)
    sync.flush()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["dashboards"][2]["width"] == 0.333
    assert saved["dashboards"][2]["height"] == 0.167


def test_editor_resizes_elevation_width_and_height_proportionally(qtbot) -> None:
    del qtbot
    raw = _config()
    raw["dashboards"] = [
        {
            "type": "elevation_profile",
            "id": "elevation",
            "width": 0.4,
            "height": 0.2,
            "anchor": {"x": 0.5, "y": 0.8},
        }
    ]
    project = EditorProject.__new__(EditorProject)
    project.config = AppConfig.model_validate(raw)
    rebuild_calls: list[bool] = []
    project.rebuild_dashboard_runtime = lambda: rebuild_calls.append(True)

    project.update_dashboard_geometry("elevation", size=0.2, rebuild_runtime=False)

    dashboard = project.config.dashboards[0]
    assert dashboard.width == pytest.approx(0.2)
    assert dashboard.height == pytest.approx(0.1)
    assert rebuild_calls == []

    project.update_dashboard_geometry("elevation", size=0.3)
    assert rebuild_calls == [True]


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


def test_video_time_display_includes_frame_number() -> None:
    assert format_timecode(1 * 3600 + 32 * 60 + 45 + 26 / 30, 30) == "01:32:45:26"
    assert format_timecode(0.033, 30) == "00:00:00:01"


def test_frame_step_snaps_backend_milliseconds_to_output_frames() -> None:
    assert frame_step_target(0.033, 10.0, 30, 1) == 2 / 30
    assert frame_step_target(0.033, 10.0, 30, -1) == 0.0
    assert frame_step_target(10.0, 10.0, 30, 1) == 10.0


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
