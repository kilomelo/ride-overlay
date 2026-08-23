from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from ride_overlay import (
    ActivityData,
    AppConfig,
    ClipRange,
    ConfigError,
    FrameRenderer,
    MetricSource,
    RotationDashboardConfig,
    RotationRuntime,
    TimeSeries,
    build_dashboard_runtimes,
    resolve_paths,
)
from ride_overlay_dashboard import ROTATION_IMAGE_CACHE_LIMIT


def rotation_config(**overrides) -> AppConfig:
    dashboard = {
        "type": "rotation",
        "id": "speed_needle",
        "source": "speed",
        "unit": "km/h",
        "width": 0.2,
        "anchor": {"x": 0.5, "y": 0.5},
        "align": "center",
        "min_value": 0,
        "max_value": 100,
        "start_angle": 0,
        "angle_range": 180,
        "clip_granularity": 0,
        "center": {"x": 0.25, "y": 0.75},
        "image_file": "needle.png",
    }
    dashboard.update(overrides)
    return AppConfig.model_validate(
        {
            "schema_version": 1,
            "inputs": {},
            "clip": {},
            "output": {
                "width": 100,
                "height": 100,
                "fps": 10,
                "background": {"mode": "transparent"},
            },
            "dashboards": [dashboard],
        }
    )


def speed_activity(value_mps: float = 50 / 3.6) -> ActivityData:
    return ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={
            MetricSource.SPEED: TimeSeries(
                (0.0, 5.0, 10.0),
                (value_mps, value_mps, value_mps),
            )
        },
        record_count=3,
    )


def create_needle(path: Path, size: tuple[int, int] = (20, 20)) -> Path:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pivot_x = round(size[0] * 0.25)
    pivot_y = round(size[1] * 0.75)
    draw.line((pivot_x, pivot_y, pivot_x, 2), fill=(255, 0, 0, 255), width=3)
    draw.ellipse(
        (pivot_x - 2, pivot_y - 2, pivot_x + 2, pivot_y + 2),
        fill=(255, 255, 255, 255),
    )
    image.save(path)
    return path


def render_paths(config: AppConfig, image_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        font=Path("unused.ttf"),
        background_image=None,
        trajectory_markers={},
        heartbeat_images={},
        fill_progress_images={},
        radial_progress_images={},
        rotation_images={config.dashboards[0].id: image_path},
    )


def build_runtime(config: AppConfig, image_path: Path) -> RotationRuntime:
    runtime = build_dashboard_runtimes(
        config,
        speed_activity(),
        ClipRange(0, 10),
        paths=render_paths(config, image_path),
    )[0]
    assert isinstance(runtime, RotationRuntime)
    return runtime


def bare_renderer() -> FrameRenderer:
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.rotation_source_canvases = {}
    renderer.rotation_rendered_images = {}
    return renderer


def test_rotation_config_validates_angles_center_and_value_range() -> None:
    assert rotation_config().dashboards[0].center.model_dump() == {"x": 0.25, "y": 0.75}
    assert rotation_config(angle_range=-360).dashboards[0].angle_range == -360
    for overrides in (
        {"angle_range": 0},
        {"angle_range": 361},
        {"start_angle": -1},
        {"center": {"x": 1.01, "y": 0.5}},
        {"clip_granularity": -0.01},
    ):
        with pytest.raises(ValidationError):
            rotation_config(**overrides)

    with pytest.raises(ValidationError, match="max_value 必须大于 min_value"):
        RotationDashboardConfig.model_validate(
            {
                "type": "rotation",
                "id": "invalid",
                "source": "speed",
                "width": 0.2,
                "anchor": {"x": 0.5, "y": 0.5},
                "min_value": 10,
                "max_value": 10,
                "angle_range": 90,
            }
        )


def test_rotation_runtime_reports_pivot_and_stable_envelope(tmp_path: Path) -> None:
    image_path = create_needle(tmp_path / "needle.png")
    config = rotation_config()
    report = SimpleNamespace(details={})

    runtime = build_dashboard_runtimes(
        config,
        speed_activity(),
        ClipRange(0, 10),
        report=report,
        paths=render_paths(config, image_path),
    )[0]

    assert isinstance(runtime, RotationRuntime)
    assert (runtime.origin_x, runtime.origin_y) == pytest.approx((40, 40))
    assert (runtime.pivot_x, runtime.pivot_y) == pytest.approx((45, 55))
    details = report.details["dashboards"][0]
    assert details["center"] == {"x": 0.25, "y": 0.75}
    assert details["pivot"] == {"x": 45.0, "y": 55.0}


def test_rotation_is_clockwise_and_keeps_custom_pivot_fixed(tmp_path: Path) -> None:
    image_path = create_needle(tmp_path / "needle.png")
    runtime = build_runtime(rotation_config(), image_path)
    renderer = bare_renderer()
    initial = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    clockwise = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    renderer._render_rotation(initial, runtime, 0.0)
    renderer._render_rotation(clockwise, runtime, 50.0)

    assert initial.getchannel("A").getpixel((45, 45)) > 150
    assert initial.getchannel("A").getpixel((55, 55)) < 20
    assert clockwise.getchannel("A").getpixel((55, 55)) > 150
    assert clockwise.getchannel("A").getpixel((45, 45)) < 20
    assert initial.getchannel("A").getpixel((45, 55)) > 150
    assert clockwise.getchannel("A").getpixel((45, 55)) > 150


def test_negative_angle_range_rotates_counterclockwise(tmp_path: Path) -> None:
    image_path = create_needle(tmp_path / "needle.png")
    runtime = build_runtime(rotation_config(angle_range=-180), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    renderer._render_rotation(frame, runtime, 50.0)

    assert frame.getchannel("A").getpixel((35, 55)) > 150
    assert frame.getchannel("A").getpixel((55, 55)) < 20


def test_start_angle_applies_at_minimum_value(tmp_path: Path) -> None:
    image_path = create_needle(tmp_path / "needle.png")
    runtime = build_runtime(rotation_config(start_angle=90), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    renderer._render_rotation(frame, runtime, 0.0)

    assert frame.getchannel("A").getpixel((55, 55)) > 150


def test_rotation_bounds_cover_every_angle_and_are_available_without_data(
    tmp_path: Path,
) -> None:
    image_path = create_needle(tmp_path / "needle.png")
    runtime = build_runtime(rotation_config(), image_path)
    renderer = bare_renderer()

    bounds = renderer.dashboard_bounds(runtime, None)

    assert bounds is not None
    assert bounds[0] < runtime.origin_x
    assert bounds[1] < runtime.origin_y
    assert bounds[2] > runtime.origin_x + runtime.width_px
    assert bounds[3] > runtime.origin_y + runtime.height_px


def test_rotation_discrete_cache_is_bounded(tmp_path: Path) -> None:
    image_path = create_needle(tmp_path / "needle.png")
    runtime = build_runtime(rotation_config(angle_range=360, clip_granularity=0.01), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    for value in range(100):
        renderer._render_rotation(frame, runtime, float(value))

    assert len(renderer.rotation_rendered_images) == ROTATION_IMAGE_CACHE_LIMIT


def test_continuous_rotation_does_not_cache_frames(tmp_path: Path) -> None:
    image_path = create_needle(tmp_path / "needle.png")
    runtime = build_runtime(rotation_config(clip_granularity=0), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    for value in range(20):
        renderer._render_rotation(frame, runtime, float(value))

    assert renderer.rotation_rendered_images == {}
    assert len(renderer.rotation_source_canvases) == 1


def test_rotation_image_override_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "activity.gpx").touch()
    (project / "font.ttf").touch()
    create_needle(tmp_path / "outside.png")
    config = rotation_config(image_file="../outside.png")

    with pytest.raises(ConfigError, match="不得指向项目目录外"):
        resolve_paths(project, config)


def test_default_rotation_image_is_sector_pointer(tmp_path: Path) -> None:
    (tmp_path / "activity.gpx").touch()
    (tmp_path / "font.ttf").touch()
    config = rotation_config(image_file=None)

    paths = resolve_paths(tmp_path, config)

    assert paths.rotation_images["speed_needle"].name == "sector_pointer.png"
