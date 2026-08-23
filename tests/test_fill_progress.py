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
    FillProgressDashboardConfig,
    FillProgressRuntime,
    FrameRenderer,
    MetricSource,
    TimeSeries,
    build_dashboard_runtimes,
    resolve_paths,
    sample_dashboard_texts,
)
from ride_overlay_dashboard import discretize_fill_ratio


def progress_config(**overrides) -> AppConfig:
    dashboard = {
        "type": "fill_progress",
        "id": "effort",
        "source": "heart_rate",
        "unit": "bpm",
        "width": 0.1,
        "anchor": {"x": 0.0, "y": 0.0},
        "align": "top_left",
        "min_value": 0,
        "max_value": 100,
        "direction": "left_to_right",
        "image_file": "progress.png",
    }
    dashboard.update(overrides)
    return AppConfig.model_validate(
        {
            "schema_version": 1,
            "inputs": {},
            "clip": {},
            "output": {
                "width": 40,
                "height": 20,
                "fps": 10,
                "background": {"mode": "transparent"},
            },
            "dashboards": [dashboard],
        }
    )


def metric_activity(source: MetricSource, value: float = 50.0) -> ActivityData:
    return ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={source: TimeSeries((0.0, 5.0, 10.0), (value, value, value))},
        record_count=3,
    )


def create_progress_image(path: Path, size: tuple[int, int] = (4, 2)) -> Path:
    Image.new("RGBA", size, (50, 180, 255, 255)).save(path)
    return path


def render_paths(config: AppConfig, image_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        font=Path("unused.ttf"),
        background_image=None,
        trajectory_markers={},
        heartbeat_images={},
        fill_progress_images={config.dashboards[0].id: image_path},
    )


def build_runtime(config: AppConfig, image_path: Path) -> FillProgressRuntime:
    runtime = build_dashboard_runtimes(
        config,
        metric_activity(config.dashboards[0].source),
        ClipRange(0, 10),
        paths=render_paths(config, image_path),
    )[0]
    assert isinstance(runtime, FillProgressRuntime)
    return runtime


def test_fill_progress_validates_range_and_metric_options() -> None:
    with pytest.raises(ValidationError, match="max_value 必须大于 min_value"):
        FillProgressDashboardConfig.model_validate(
            {
                "type": "fill_progress",
                "id": "invalid",
                "source": "heart_rate",
                "width": 0.2,
                "anchor": {"x": 0.5, "y": 0.5},
                "min_value": 100,
                "max_value": 100,
            }
        )

    assert progress_config().dashboards[0].clip_granularity == 0
    assert progress_config(clip_granularity=0.5).dashboards[0].clip_granularity == 0.5
    for invalid in (-0.001, 0.501, float("nan")):
        with pytest.raises(ValidationError):
            progress_config(clip_granularity=invalid)


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.0, 0.0),
        (0.0001, 0.3),
        (0.3, 0.3),
        (0.3001, 0.6),
        (0.6, 0.6),
        (0.6001, 0.9),
        (0.9, 0.9),
        (0.9001, 1.0),
        (1.0, 1.0),
    ],
)
def test_fill_progress_granularity_rounds_up_to_expected_states(
    ratio: float,
    expected: float,
) -> None:
    assert discretize_fill_ratio(ratio, 0.3, 100) == pytest.approx(expected)


def test_fill_progress_granularity_is_disabled_at_zero_or_below_one_pixel() -> None:
    assert discretize_fill_ratio(0.061, 0, 10) == pytest.approx(0.061)
    assert discretize_fill_ratio(0.061, 0.06, 10) == pytest.approx(0.061)
    assert discretize_fill_ratio(0.21, 0.2, 4) == pytest.approx(0.21)
    assert discretize_fill_ratio(0.21, 0.25, 4) == pytest.approx(0.25)


def test_fill_progress_renderer_applies_granularity(tmp_path: Path) -> None:
    image_path = create_progress_image(tmp_path / "progress.png", (10, 1))
    runtime = build_runtime(
        progress_config(width=0.25, clip_granularity=0.3),
        image_path,
    )
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.fill_progress_images = {}
    frame = Image.new("RGBA", (10, 1), (0, 0, 0, 0))

    renderer._render_fill_progress(frame, runtime, 1.0)

    assert sum(frame.getchannel("A").getpixel((x, 0)) > 0 for x in range(10)) == 3


def test_fill_progress_layout_preserves_image_aspect_ratio(tmp_path: Path) -> None:
    image_path = create_progress_image(tmp_path / "progress.png", (8, 2))
    config = progress_config(width=0.5)
    report = SimpleNamespace(details={})

    runtimes = build_dashboard_runtimes(
        config,
        metric_activity(MetricSource.HEART_RATE),
        ClipRange(0, 10),
        report=report,
        paths=render_paths(config, image_path),
    )

    runtime = runtimes[0]
    assert isinstance(runtime, FillProgressRuntime)
    assert (runtime.width_px, runtime.height_px) == (20, 5)
    details = report.details["dashboards"][0]
    assert details["source_image_size"] == {"width": 8, "height": 2}
    assert details["direction"] == "left_to_right"


@pytest.mark.parametrize(
    ("direction", "expected_alpha"),
    [
        ("left_to_right", [[255, 255, 0, 0], [255, 255, 0, 0]]),
        ("right_to_left", [[0, 0, 255, 255], [0, 0, 255, 255]]),
        ("top_to_bottom", [[255, 255, 255, 255], [0, 0, 0, 0]]),
        ("bottom_to_top", [[0, 0, 0, 0], [255, 255, 255, 255]]),
    ],
)
def test_fill_progress_reveals_image_in_configured_direction(
    tmp_path: Path,
    direction: str,
    expected_alpha: list[list[int]],
) -> None:
    image_path = create_progress_image(tmp_path / f"{direction}.png")
    runtime = build_runtime(progress_config(direction=direction), image_path)
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.fill_progress_images = {}
    frame = Image.new("RGBA", (4, 2), (0, 0, 0, 0))

    renderer._render_fill_progress(frame, runtime, 50.0)

    alpha = frame.getchannel("A")
    assert [[alpha.getpixel((x, y)) for x in range(4)] for y in range(2)] == expected_alpha


def test_fill_progress_clamps_values_and_keeps_full_editor_bounds(tmp_path: Path) -> None:
    image_path = create_progress_image(tmp_path / "progress.png")
    runtime = build_runtime(progress_config(), image_path)
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.fill_progress_images = {}
    empty = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
    full = Image.new("RGBA", (4, 2), (0, 0, 0, 0))

    renderer._render_fill_progress(empty, runtime, -1.0)
    renderer._render_fill_progress(full, runtime, 101.0)

    assert empty.getbbox() is None
    assert full.getchannel("A").getextrema() == (255, 255)
    assert renderer.dashboard_bounds(runtime, None) == pytest.approx((0, 0, 4, 2))


def test_fill_progress_sampling_compares_thresholds_in_configured_unit(
    tmp_path: Path,
) -> None:
    image_path = create_progress_image(tmp_path / "progress.png")
    config = progress_config(source="speed", unit="km/h", min_value=0, max_value=50)
    runtime = build_dashboard_runtimes(
        config,
        metric_activity(MetricSource.SPEED, 10.0),
        ClipRange(0, 10),
        paths=render_paths(config, image_path),
    )[0]

    assert sample_dashboard_texts([runtime], ClipRange(0, 10), 1.0) == pytest.approx((36.0,))


def test_fill_progress_image_override_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "activity.gpx").touch()
    (project / "font.ttf").touch()
    create_progress_image(tmp_path / "outside.png")
    config = progress_config(image_file="../outside.png")

    with pytest.raises(ConfigError, match="不得指向项目目录外"):
        resolve_paths(project, config)


def test_default_fill_progress_image_is_resolved(tmp_path: Path) -> None:
    (tmp_path / "activity.gpx").touch()
    (tmp_path / "font.ttf").touch()
    config = progress_config(image_file=None)

    paths = resolve_paths(tmp_path, config)

    assert paths.fill_progress_images["effort"].name == "fill_progress_bar.png"
