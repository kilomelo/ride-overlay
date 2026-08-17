from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ride_overlay import (
    AppConfig,
    ClipRange,
    ConfigError,
    FrameRenderer,
    RawPoint,
    TrajectoryRuntime,
    build_activity,
    build_dashboard_runtimes,
    project_trajectory,
    resolve_paths,
)
from ride_overlay_dashboard import _colorize_mask, _draw_rounded_paths, trajectory_paths_at


def trajectory_config(**overrides) -> AppConfig:
    dashboard = {
        "type": "trajectory",
        "id": "route",
        "width": 0.5,
        "anchor": {"x": 0.0, "y": 0.0},
        "align": "top_left",
        "update_interval_ms": 200,
        "line_width": 8,
        "remaining_color": "#FFFFFF80",
        "completed_color": "#00E676CC",
        "marker_scale": 2,
    }
    dashboard.update(overrides)
    return AppConfig.model_validate(
        {
            "schema_version": 1,
            "inputs": {},
            "clip": {},
            "output": {
                "width": 320,
                "height": 180,
                "fps": 30,
                "background": {"mode": "transparent"},
            },
            "dashboards": [dashboard],
        }
    )


def marker_paths(config: AppConfig) -> SimpleNamespace:
    marker = Path(__file__).parents[1] / "assets" / "images" / "arrow.png"
    return SimpleNamespace(
        font=Path("unused.ttf"),
        background_image=None,
        trajectory_markers={config.dashboards[0].id: marker},
    )


def test_trajectory_projection_is_north_up_and_width_is_normalized() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = build_activity(
        [
            RawPoint(start, 0, latitude=30.0, longitude=120.0),
            RawPoint(
                start + timedelta(seconds=10),
                0,
                latitude=30.001,
                longitude=120.001,
            ),
        ]
    )
    config = trajectory_config()
    runtimes = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 10),
        paths=marker_paths(config),
    )

    runtime = runtimes[0]
    assert isinstance(runtime, TrajectoryRuntime)
    assert runtime.width_px == pytest.approx(160)
    assert runtime.points[1].x > runtime.points[0].x
    assert runtime.points[1].y < runtime.points[0].y


def test_trajectory_height_is_not_shrunk_when_it_overflows() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = build_activity(
        [
            RawPoint(start, 0, latitude=30.0, longitude=120.0),
            RawPoint(
                start + timedelta(seconds=10),
                0,
                latitude=30.003,
                longitude=120.001,
            ),
        ]
    )
    config = trajectory_config()
    report = SimpleNamespace(details={})
    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 10),
        report=report,
        paths=marker_paths(config),
    )[0]

    assert isinstance(runtime, TrajectoryRuntime)
    assert runtime.height_px > config.output.height
    assert "bottom" in report.details["dashboards"][0]["overflow_edges"]


def test_position_gap_holds_marker_and_breaks_the_route() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = build_activity(
        [
            RawPoint(start, 0, latitude=30.0, longitude=120.0),
            RawPoint(
                start + timedelta(seconds=1),
                0,
                latitude=30.0,
                longitude=120.0001,
            ),
            RawPoint(
                start + timedelta(seconds=10),
                0,
                latitude=30.0001,
                longitude=120.0002,
            ),
            RawPoint(
                start + timedelta(seconds=11),
                0,
                latitude=30.0002,
                longitude=120.0002,
            ),
        ]
    )
    assert activity.trajectory is not None
    assert activity.trajectory.segment_count == 2
    assert activity.trajectory.breaks[0].reason == "position_gap"

    projected = project_trajectory(activity.trajectory)
    held = projected.sample_at(5)
    last_reliable = projected.points[1]
    assert held is not None
    assert held.x_m == pytest.approx(last_reliable.x_m)
    assert held.y_m == pytest.approx(last_reliable.y_m)
    assert held.heading_degrees == pytest.approx(90, abs=0.1)

    config = trajectory_config()
    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(5, 11),
        paths=marker_paths(config),
    )[0]
    assert isinstance(runtime, TrajectoryRuntime)
    completed, remaining = trajectory_paths_at(runtime, 5)
    assert len(completed) == 1
    assert len(remaining) == 1
    assert completed[0][-1] != remaining[0][0]


def test_transparent_path_color_is_applied_once_at_rounded_joint() -> None:
    mask = _draw_rounded_paths(
        (64, 64),
        [[(8, 56), (32, 8), (56, 56)]],
        line_width=12,
    )
    layer = _colorize_mask(mask, "#FFFFFF80")

    assert layer.getpixel((32, 8))[3] == 128
    assert layer.getpixel((20, 32))[3] == 128


def test_trajectory_layer_is_rendered_and_reused_for_same_update_time() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = build_activity(
        [
            RawPoint(start, 0, latitude=30.0, longitude=120.0),
            RawPoint(
                start + timedelta(seconds=10),
                0,
                latitude=30.001,
                longitude=120.001,
            ),
        ]
    )
    config = trajectory_config(anchor={"x": 0.25, "y": 0.5}, align="center")
    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 10),
        paths=marker_paths(config),
    )[0]
    assert isinstance(runtime, TrajectoryRuntime)
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.width = 320
    renderer.height = 180
    renderer.marker_images = {}
    renderer.trajectory_layers = {}
    first_frame = Image.new("RGBA", (320, 180), (0, 0, 0, 0))

    renderer._render_trajectory(first_frame, runtime, 5.0)
    cached_layer = renderer.trajectory_layers[runtime.config.id][1]
    second_frame = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
    renderer._render_trajectory(second_frame, runtime, 5.0)

    assert renderer.trajectory_layers[runtime.config.id][1] is cached_layer
    assert first_frame.getbbox() is not None
    assert first_frame.tobytes() == second_frame.tobytes()


def test_isolated_gps_spike_is_filtered_without_expanding_bounds() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = build_activity(
        [
            RawPoint(start, 0, latitude=30.0, longitude=120.0),
            RawPoint(
                start + timedelta(seconds=1),
                0,
                latitude=31.0,
                longitude=121.0,
            ),
            RawPoint(
                start + timedelta(seconds=2),
                0,
                latitude=30.0,
                longitude=120.0001,
            ),
        ]
    )

    assert activity.trajectory is not None
    assert activity.trajectory.filtered_spike_count == 1
    assert len(activity.trajectory.points) == 2


def test_trajectory_without_position_data_is_skipped() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = build_activity([RawPoint(start, 0), RawPoint(start + timedelta(seconds=1), 0)])
    config = trajectory_config()
    report = SimpleNamespace(details={})

    runtimes = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 1),
        report=report,
    )

    assert runtimes == []
    assert report.details["dashboards"][0]["status"] == "SKIPPED"


def test_project_marker_override_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "activity.gpx").touch()
    (project / "font.ttf").touch()
    outside = tmp_path / "outside.png"
    outside.touch()
    config = trajectory_config(marker_image_file="../outside.png")

    with pytest.raises(ConfigError, match="不得指向项目目录外"):
        resolve_paths(project, config)


def test_legacy_numeric_dashboard_still_defaults_to_numeric_type() -> None:
    config = AppConfig.model_validate(
        {
            "schema_version": 1,
            "dashboards": [
                {
                    "id": "speed",
                    "source": "speed",
                    "font_size": 30,
                    "anchor": {"x": 0.5, "y": 0.5},
                }
            ],
        }
    )

    assert config.dashboards[0].type == "numeric"
