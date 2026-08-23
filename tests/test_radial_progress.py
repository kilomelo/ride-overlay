from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from ride_overlay import (
    ActivityData,
    AppConfig,
    ClipRange,
    ConfigError,
    FrameRenderer,
    MetricSource,
    RadialProgressDashboardConfig,
    RadialProgressRuntime,
    TimeSeries,
    build_dashboard_runtimes,
    resolve_paths,
)
from ride_overlay_dashboard import (
    RADIAL_MASK_CACHE_LIMIT,
    discretize_fill_ratio,
    radial_progress_mask,
)


def radial_config(**overrides) -> AppConfig:
    dashboard = {
        "type": "radial_progress",
        "id": "effort_dial",
        "source": "power",
        "unit": "W",
        "width": 1.0,
        "anchor": {"x": 0.0, "y": 0.0},
        "align": "top_left",
        "min_value": 0,
        "max_value": 100,
        "start_angle": 0,
        "angle_range": 180,
        "clip_granularity": 0,
        "image_file": "dial.png",
    }
    dashboard.update(overrides)
    return AppConfig.model_validate(
        {
            "schema_version": 1,
            "inputs": {},
            "clip": {},
            "output": {
                "width": 100,
                "height": 60,
                "fps": 10,
                "background": {"mode": "transparent"},
            },
            "dashboards": [dashboard],
        }
    )


def power_activity(value: float = 50.0) -> ActivityData:
    return ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={MetricSource.POWER: TimeSeries((0.0, 5.0, 10.0), (value, value, value))},
        record_count=3,
    )


def create_dial(path: Path, size: tuple[int, int] = (100, 60)) -> Path:
    Image.new("RGBA", size, (255, 80, 40, 255)).save(path)
    return path


def render_paths(config: AppConfig, image_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        font=Path("unused.ttf"),
        background_image=None,
        trajectory_markers={},
        heartbeat_images={},
        fill_progress_images={},
        radial_progress_images={config.dashboards[0].id: image_path},
    )


def build_runtime(config: AppConfig, image_path: Path) -> RadialProgressRuntime:
    runtime = build_dashboard_runtimes(
        config,
        power_activity(),
        ClipRange(0, 10),
        paths=render_paths(config, image_path),
    )[0]
    assert isinstance(runtime, RadialProgressRuntime)
    return runtime


def bare_renderer() -> FrameRenderer:
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.radial_progress_images = {}
    renderer.radial_progress_masks = {}
    return renderer


def test_radial_progress_validates_value_angle_and_granularity_ranges() -> None:
    assert radial_config().dashboards[0].center.x == pytest.approx(0.5)
    assert radial_config().dashboards[0].center.y == pytest.approx(0.5)
    assert radial_config(angle_range=-360).dashboards[0].angle_range == -360
    assert radial_config(start_angle=360, angle_range=360).dashboards[0].start_angle == 360
    for overrides in (
        {"angle_range": 0},
        {"angle_range": 361},
        {"angle_range": -361},
        {"start_angle": -1},
        {"start_angle": 361},
        {"clip_granularity": 0.501},
        {"center": {"x": -0.01, "y": 0.5}},
        {"center": {"x": 0.5, "y": 1.01}},
    ):
        with pytest.raises(ValidationError):
            radial_config(**overrides)

    with pytest.raises(ValidationError, match="max_value 必须大于 min_value"):
        RadialProgressDashboardConfig.model_validate(
            {
                "type": "radial_progress",
                "id": "invalid",
                "source": "power",
                "width": 0.2,
                "anchor": {"x": 0.5, "y": 0.5},
                "min_value": 100,
                "max_value": 100,
                "angle_range": 90,
            }
        )


def test_radial_progress_discretization_uses_percentage_of_angle_range() -> None:
    ratios = (0, 0.01, 0.3, 0.31, 0.6, 0.61, 0.9, 0.91, 1)
    spans = [70 * discretize_fill_ratio(ratio, 0.3, 1000) for ratio in ratios]

    assert spans == pytest.approx((0, 21, 21, 42, 42, 63, 63, 70, 70))


def test_radial_mask_uses_clock_face_angles_for_non_square_images() -> None:
    clockwise = radial_progress_mask((100, 60), 0, 90)
    counterclockwise = radial_progress_mask((100, 60), 0, -90)
    from_three_oclock = radial_progress_mask((100, 60), 90, 90)

    assert clockwise.getpixel((75, 10)) > 200
    assert clockwise.getpixel((25, 10)) < 20
    assert clockwise.getpixel((75, 50)) < 20
    assert counterclockwise.getpixel((25, 10)) > 200
    assert counterclockwise.getpixel((75, 10)) < 20
    assert from_three_oclock.getpixel((75, 50)) > 200
    assert from_three_oclock.getpixel((75, 10)) < 20


def test_radial_mask_uses_configured_center() -> None:
    offset_center = radial_progress_mask((100, 60), 0, 90, (0.25, 0.5))
    geometric_center = radial_progress_mask((100, 60), 0, 90)

    assert offset_center.getpixel((30, 25)) > 200
    assert geometric_center.getpixel((30, 25)) < 20


def test_180_and_360_degree_masks_cover_expected_areas() -> None:
    half = radial_progress_mask((100, 60), 0, 180)
    clockwise_three_quarters = radial_progress_mask((100, 60), 0, 270)
    counterclockwise_three_quarters = radial_progress_mask((100, 60), 0, -270)
    full = radial_progress_mask((100, 60), 123, -360)

    assert half.getpixel((75, 10)) > 200
    assert half.getpixel((75, 50)) > 200
    assert half.getpixel((25, 10)) < 20
    assert half.getpixel((25, 50)) < 20
    assert clockwise_three_quarters.getpixel((25, 10)) < 20
    assert clockwise_three_quarters.getpixel((75, 10)) > 200
    assert clockwise_three_quarters.getpixel((25, 50)) > 200
    assert counterclockwise_three_quarters.getpixel((75, 10)) < 20
    assert counterclockwise_three_quarters.getpixel((25, 10)) > 200
    assert counterclockwise_three_quarters.getpixel((75, 50)) > 200
    assert full.getextrema() == (255, 255)


def test_radial_progress_layout_and_report_preserve_rectangular_aspect(
    tmp_path: Path,
) -> None:
    image_path = create_dial(tmp_path / "dial.png", (80, 40))
    config = radial_config(
        width=0.5,
        start_angle=90,
        angle_range=-270,
        center={"x": 0.25, "y": 0.75},
    )
    report = SimpleNamespace(details={})

    runtimes = build_dashboard_runtimes(
        config,
        power_activity(),
        ClipRange(0, 10),
        report=report,
        paths=render_paths(config, image_path),
    )

    runtime = runtimes[0]
    assert isinstance(runtime, RadialProgressRuntime)
    assert (runtime.width_px, runtime.height_px) == (50, 25)
    details = report.details["dashboards"][0]
    assert details["source_image_size"] == {"width": 80, "height": 40}
    assert details["start_angle"] == 90
    assert details["angle_range"] == -270
    assert details["direction"] == "counterclockwise"
    assert details["center"] == {"x": 0.25, "y": 0.75}


def test_radial_progress_renderer_applies_value_ratio_and_keeps_bounds(
    tmp_path: Path,
) -> None:
    image_path = create_dial(tmp_path / "dial.png")
    runtime = build_runtime(radial_config(), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 60), (0, 0, 0, 0))

    renderer._render_radial_progress(frame, runtime, 50.0)

    assert frame.getchannel("A").getpixel((75, 10)) > 200
    assert frame.getchannel("A").getpixel((75, 50)) < 20
    assert renderer.dashboard_bounds(runtime, None) == pytest.approx((0, 0, 100, 60))


def test_radial_progress_mask_cache_is_bounded(tmp_path: Path) -> None:
    image_path = create_dial(tmp_path / "dial.png")
    runtime = build_runtime(radial_config(angle_range=360, clip_granularity=0.01), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 60), (0, 0, 0, 0))

    for value in range(1, 100):
        renderer._render_radial_progress(frame, runtime, float(value))

    assert len(renderer.radial_progress_masks) == RADIAL_MASK_CACHE_LIMIT


def test_continuous_radial_progress_does_not_cache_per_frame_masks(tmp_path: Path) -> None:
    image_path = create_dial(tmp_path / "dial.png")
    runtime = build_runtime(radial_config(clip_granularity=0), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 60), (0, 0, 0, 0))

    for value in range(1, 20):
        renderer._render_radial_progress(frame, runtime, float(value))

    assert renderer.radial_progress_masks == {}


def test_radial_progress_image_override_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "activity.gpx").touch()
    (project / "font.ttf").touch()
    create_dial(tmp_path / "outside.png")
    config = radial_config(image_file="../outside.png")

    with pytest.raises(ConfigError, match="不得指向项目目录外"):
        resolve_paths(project, config)


def test_default_radial_progress_image_is_resolved(tmp_path: Path) -> None:
    (tmp_path / "activity.gpx").touch()
    (tmp_path / "font.ttf").touch()
    config = radial_config(image_file=None)

    paths = resolve_paths(tmp_path, config)

    assert (
        paths.radial_progress_images["effort_dial"].name
        == "sector_fill_progress_bar.png"
    )
