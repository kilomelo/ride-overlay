from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ride_overlay import (
    ActivityData,
    AppConfig,
    ClipRange,
    ConfigError,
    FrameRenderer,
    HeartbeatAnimationState,
    HeartbeatRuntime,
    MetricSource,
    TimeSeries,
    build_dashboard_runtimes,
    resolve_paths,
    sample_dashboard_texts,
)


def heartbeat_config(**overrides) -> AppConfig:
    dashboard = {
        "type": "heartbeat",
        "id": "heartbeat",
        "width": 0.25,
        "anchor": {"x": 1.0, "y": 1.0},
        "align": "bottom_right",
        "heart_image_file": "heart.png",
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


def heart_activity(series: TimeSeries | None = None) -> ActivityData:
    metrics = {MetricSource.HEART_RATE: series} if series is not None else {}
    return ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics=metrics,
        record_count=len(series.times) if series is not None else 2,
    )


def render_paths(config: AppConfig, image_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        font=Path("unused.ttf"),
        background_image=None,
        trajectory_markers={},
        heartbeat_images={config.dashboards[0].id: image_path},
    )


def create_heart(path: Path, size: tuple[int, int] = (40, 20)) -> Path:
    Image.new("RGBA", size, (255, 0, 0, 255)).save(path)
    return path


def test_heartbeat_layout_uses_width_ratio_and_image_aspect_ratio(tmp_path: Path) -> None:
    image_path = create_heart(tmp_path / "heart.png")
    config = heartbeat_config()
    report = SimpleNamespace(details={})
    runtimes = build_dashboard_runtimes(
        config,
        heart_activity(TimeSeries((0.0, 10.0), (120.0, 120.0))),
        ClipRange(0, 10),
        report=report,
        paths=render_paths(config, image_path),
    )

    runtime = runtimes[0]
    assert isinstance(runtime, HeartbeatRuntime)
    assert (runtime.width_px, runtime.height_px) == (80, 40)
    assert (runtime.origin_x, runtime.origin_y) == pytest.approx((240, 140))
    details = report.details["dashboards"][0]
    assert details["source_image_size"] == {"width": 40, "height": 20}
    assert details["animation"]["frequency_update"] == "at_cycle_boundary"


def test_120_bpm_opacity_cycle_is_one_to_zero_to_one_in_half_second() -> None:
    series = TimeSeries((0.0, 2.0), (120.0, 120.0))
    state = HeartbeatAnimationState()

    assert state.opacity_at(series, 0.0, 0.0) == pytest.approx(1)
    assert state.opacity_at(series, 0.125, 0.0) == pytest.approx(0.5)
    assert state.opacity_at(series, 0.25, 0.0) == pytest.approx(0)
    assert state.opacity_at(series, 0.5, 0.0) == pytest.approx(1)


def test_new_heart_rate_only_applies_at_cycle_boundary() -> None:
    series = TimeSeries((0.0, 0.1, 2.0), (60.0, 120.0, 120.0))
    state = HeartbeatAnimationState()

    assert state.opacity_at(series, 0.5, 0.0) == pytest.approx(0)
    before_boundary = state.opacity_at(series, 1.0 - 1e-6, 0.0)
    after_boundary = state.opacity_at(series, 1.0 + 1e-6, 0.0)
    assert before_boundary == pytest.approx(1, abs=1e-9)
    assert after_boundary == pytest.approx(1, abs=1e-9)
    assert state.current_bpm == pytest.approx(120)
    assert state.opacity_at(series, 1.25, 0.0) == pytest.approx(0)


def test_heart_rate_gap_retains_previous_cycle_frequency() -> None:
    series = TimeSeries((0.0, 10.0), (120.0, 60.0))
    state = HeartbeatAnimationState()

    assert state.opacity_at(series, 0.0, 0.0) == pytest.approx(1)
    assert state.opacity_at(series, 3.25, 0.0) == pytest.approx(0)
    assert state.current_bpm == pytest.approx(120)


def test_heartbeat_sampling_updates_every_video_frame(tmp_path: Path) -> None:
    image_path = create_heart(tmp_path / "heart.png")
    config = heartbeat_config()
    clip = ClipRange(0, 10)
    runtime = build_dashboard_runtimes(
        config,
        heart_activity(TimeSeries((0.0, 10.0), (120.0, 120.0))),
        clip,
        paths=render_paths(config, image_path),
    )[0]

    assert sample_dashboard_texts([runtime], clip, 0.123) == (0.123,)


def test_preview_always_uses_full_opacity_even_inside_heart_rate_gap(tmp_path: Path) -> None:
    image_path = create_heart(tmp_path / "heart.png", (20, 20))
    config = heartbeat_config(width=0.1)
    runtime = build_dashboard_runtimes(
        config,
        heart_activity(TimeSeries((0.0, 10.0), (120.0, 120.0))),
        ClipRange(0, 10),
        paths=render_paths(config, image_path),
    )[0]
    assert isinstance(runtime, HeartbeatRuntime)
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.heart_images = {}
    renderer.heartbeat_states = {}
    preview_frame = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
    animated_frame = Image.new("RGBA", (320, 180), (0, 0, 0, 0))

    renderer._render_heartbeat(preview_frame, runtime, 5.0, preview=True)
    renderer._render_heartbeat(animated_frame, runtime, 0.25, preview=False)

    assert preview_frame.getchannel("A").getextrema()[1] == 255
    assert animated_frame.getbbox() is None


def test_heartbeat_without_heart_rate_is_skipped(tmp_path: Path) -> None:
    image_path = create_heart(tmp_path / "heart.png")
    config = heartbeat_config()
    report = SimpleNamespace(details={})

    runtimes = build_dashboard_runtimes(
        config,
        heart_activity(),
        ClipRange(0, 10),
        report=report,
        paths=render_paths(config, image_path),
    )

    assert runtimes == []
    assert report.details["dashboards"][0]["status"] == "SKIPPED"


def test_heart_image_override_cannot_escape_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "activity.gpx").touch()
    (project / "font.ttf").touch()
    create_heart(tmp_path / "outside.png")
    config = heartbeat_config(heart_image_file="../outside.png")

    with pytest.raises(ConfigError, match="不得指向项目目录外"):
        resolve_paths(project, config)


def test_default_heart_image_is_resolved_when_override_is_empty(tmp_path: Path) -> None:
    (tmp_path / "activity.gpx").touch()
    (tmp_path / "font.ttf").touch()
    config = heartbeat_config(heart_image_file=None)

    paths = resolve_paths(tmp_path, config)

    assert paths.heartbeat_images["heartbeat"].name == "heart.png"
