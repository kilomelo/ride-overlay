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
    GradeLevelDashboardConfig,
    GradeLevelRuntime,
    MetricSource,
    TimeSeries,
    build_dashboard_runtimes,
    resolve_paths,
    sample_dashboard_texts,
)


def grade_level_config(**overrides) -> AppConfig:
    dashboard = {
        "type": "grade_level",
        "id": "level",
        "width": 0.2,
        "anchor": {"x": 0.5, "y": 0.5},
        "align": "center",
        "update_interval_ms": 100,
        "angle_smoothing_seconds": 0,
        "image_file": "level.png",
    }
    dashboard.update(overrides)
    return AppConfig.model_validate(
        {
            "schema_version": 2,
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


def grade_activity(
    values: tuple[float, ...] = (100.0, 100.0, 100.0),
    times: tuple[float, ...] = (0.0, 5.0, 10.0),
    *,
    gap_seconds: float | None = 5.0,
) -> ActivityData:
    return ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={
            MetricSource.GRADE: TimeSeries(
                times,
                values,
                interpolation_gap_seconds=gap_seconds,
            )
        },
        record_count=len(times),
    )


def create_level_image(path: Path) -> Path:
    image = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    ImageDraw.Draw(image).line((10, 10, 18, 10), fill=(0, 180, 255, 255), width=2)
    image.save(path)
    return path


def render_paths(config: AppConfig, image_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        grade_level_images={config.dashboards[0].id: image_path},
    )


def build_runtime(
    config: AppConfig,
    image_path: Path,
    activity: ActivityData | None = None,
) -> GradeLevelRuntime:
    runtime = build_dashboard_runtimes(
        config,
        activity or grade_activity(),
        ClipRange(0, 10),
        paths=render_paths(config, image_path),
    )[0]
    assert isinstance(runtime, GradeLevelRuntime)
    return runtime


def bare_renderer() -> FrameRenderer:
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.rotation_source_canvases = {}
    renderer.rotation_rendered_images = {}
    return renderer


def test_grade_level_config_validates_smoothing_and_forbids_generic_source() -> None:
    dashboard = grade_level_config().dashboards[0]
    assert isinstance(dashboard, GradeLevelDashboardConfig)
    assert grade_level_config(angle_smoothing_seconds=3).dashboards[
        0
    ].angle_smoothing_seconds == pytest.approx(3)

    for invalid in (-0.01, 3600.01, float("nan")):
        with pytest.raises(ValidationError):
            grade_level_config(angle_smoothing_seconds=invalid)
    with pytest.raises(ValidationError):
        grade_level_config(source="grade")


def test_grade_percent_is_converted_to_physical_angle(tmp_path: Path) -> None:
    image_path = create_level_image(tmp_path / "level.png")
    runtime = build_runtime(grade_level_config(), image_path)

    assert runtime.series.values == pytest.approx((45, 45, 45))
    assert sample_dashboard_texts([runtime], ClipRange(0, 10), 5) == pytest.approx((45,))


def test_smoothing_is_applied_after_grade_to_angle_conversion(tmp_path: Path) -> None:
    image_path = create_level_image(tmp_path / "level.png")
    config = grade_level_config(angle_smoothing_seconds=10)
    runtime = build_runtime(config, image_path, grade_activity((0.0, 100.0, 0.0)))

    # mean(atan(0), atan(1), atan(0)) = 15 degrees. Converting the mean
    # grade instead would incorrectly produce atan(1/3) = 18.435 degrees.
    assert runtime.series.value_at(5) == pytest.approx(15)


def test_smoothing_does_not_cross_grade_data_gaps(tmp_path: Path) -> None:
    image_path = create_level_image(tmp_path / "level.png")
    config = grade_level_config(angle_smoothing_seconds=20)
    activity = grade_activity(
        (0.0, 0.0, 100.0),
        (0.0, 1.0, 10.0),
        gap_seconds=5.0,
    )
    runtime = build_runtime(config, image_path, activity)

    assert runtime.series.values == pytest.approx((0, 0, 45))
    assert runtime.series.value_at(5) is None


def test_positive_grade_raises_image_right_side(tmp_path: Path) -> None:
    image_path = create_level_image(tmp_path / "level.png")
    runtime = build_runtime(grade_level_config(), image_path)
    renderer = bare_renderer()
    uphill = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    downhill = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    renderer._render_grade_level(uphill, runtime, 45)
    renderer._render_grade_level(downhill, runtime, -45)

    assert uphill.getchannel("A").getpixel((55, 45)) > 100
    assert uphill.getchannel("A").getpixel((55, 56)) < 30
    assert downhill.getchannel("A").getpixel((55, 56)) > 100
    assert downhill.getchannel("A").getpixel((55, 45)) < 30


def test_grade_level_bounds_and_continuous_cache_are_stable(tmp_path: Path) -> None:
    image_path = create_level_image(tmp_path / "level.png")
    runtime = build_runtime(grade_level_config(), image_path)
    renderer = bare_renderer()
    frame = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    bounds = renderer.dashboard_bounds(runtime, None)
    for angle in range(-20, 21):
        renderer._render_grade_level(frame, runtime, float(angle))

    assert bounds is not None
    assert bounds[0] < runtime.origin_x
    assert bounds[1] < runtime.origin_y
    assert bounds[2] > runtime.origin_x + runtime.width_px
    assert bounds[3] > runtime.origin_y + runtime.height_px
    assert renderer.rotation_rendered_images == {}
    assert len(renderer.rotation_source_canvases) == 1


def test_missing_grade_skips_level_dashboard(tmp_path: Path) -> None:
    image_path = create_level_image(tmp_path / "level.png")
    config = grade_level_config()
    activity = ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={},
        record_count=0,
    )

    assert (
        build_dashboard_runtimes(
            config,
            activity,
            ClipRange(0, 10),
            paths=render_paths(config, image_path),
        )
        == []
    )


def test_grade_level_image_override_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "activity.gpx").touch()
    (project / "font.ttf").touch()
    create_level_image(tmp_path / "outside.png")
    config = grade_level_config(image_file="../outside.png")

    with pytest.raises(ConfigError, match="不得指向项目目录外"):
        resolve_paths(project, config)


def test_default_grade_level_image_is_resolved(tmp_path: Path) -> None:
    (tmp_path / "activity.gpx").touch()
    (tmp_path / "font.ttf").touch()
    config = grade_level_config(image_file=None)

    paths = resolve_paths(tmp_path, config)

    assert paths.grade_level_images["level"].name == "level.png"
