from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ride_overlay import (
    ActivityData,
    AppConfig,
    ClipRange,
    ConfigError,
    MetricSource,
    RawPoint,
    TimeSeries,
    build_activity,
    build_dashboard_runtimes,
    format_value,
    resolve_clip,
    resolve_paths,
    sample_dashboard_texts,
)


def config_data() -> dict:
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
                "unit": "km/h",
                "smoothing": {"method": "none"},
                "precision": 1,
                "pad_zeros": True,
                "update_interval_ms": 1000,
                "font_size": 30,
                "anchor": {"x": 0.5, "y": 0.5},
                "align": "center",
            },
            {
                "id": "distance",
                "source": "distance",
                "unit": "km",
                "smoothing": {"method": "none"},
                "precision": 2,
                "pad_zeros": True,
                "update_interval_ms": 1000,
                "font_size": 30,
                "anchor": {"x": 0.5, "y": 0.7},
                "align": "center",
            },
        ],
    }


def test_distance_rejects_smoothing() -> None:
    raw = config_data()
    raw["dashboards"][1]["smoothing"] = {
        "method": "moving_average",
        "window_seconds": 1,
    }
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_duplicate_dashboard_ids_are_rejected() -> None:
    raw = config_data()
    raw["dashboards"][1]["id"] = "speed"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_global_opacity_defaults_to_one_and_is_validated() -> None:
    assert AppConfig.model_validate(config_data()).opacity == 1

    raw = config_data()
    raw["opacity"] = 0.35
    assert AppConfig.model_validate(raw).opacity == pytest.approx(0.35)

    raw["opacity"] = 1.01
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_linear_interpolation_and_gap_limit() -> None:
    series = TimeSeries((0.0, 2.0, 10.0), (0.0, 4.0, 20.0))
    assert series.value_at(1.0) == pytest.approx(2.0)
    assert series.value_at(5.0) is None
    assert series.value_at(5.0, max_gap_seconds=None) == pytest.approx(10.0)


def test_centered_moving_average() -> None:
    series = TimeSeries((0.0, 1.0, 2.0), (0.0, 3.0, 6.0))
    averaged = series.moving_average(2.0)
    assert averaged.values == pytest.approx((1.5, 3.0, 4.5))


def test_formatting_padding() -> None:
    assert format_value(12.0, 2, True) == "12.00"
    assert format_value(12.0, 2, False) == "12"
    assert format_value(-0.001, 2, True) == "0.00"


def test_build_activity_uses_direct_metrics() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = [
        RawPoint(start, 0, speed_mps=1.0, distance_m=0.0),
        RawPoint(start + timedelta(seconds=1), 0, speed_mps=2.0, distance_m=2.0),
        RawPoint(start + timedelta(seconds=2), 0, speed_mps=3.0, distance_m=5.0),
    ]
    activity = build_activity(points)
    assert activity.duration_seconds == 2
    assert activity.metrics[MetricSource.SPEED].values == (1.0, 2.0, 3.0)
    assert activity.metrics[MetricSource.DISTANCE].values == (0.0, 2.0, 5.0)


def test_sampling_obeys_update_interval_and_units() -> None:
    config = AppConfig.model_validate(config_data())
    activity = ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=2.0,
        metrics={
            MetricSource.SPEED: TimeSeries((0.0, 2.0), (0.0, 10.0)),
            MetricSource.DISTANCE: TimeSeries((0.0, 2.0), (0.0, 2000.0), cumulative=True),
        },
        record_count=2,
    )
    clip = ClipRange(0.0, 2.0)
    runtimes = build_dashboard_runtimes(config, activity, clip)
    assert sample_dashboard_texts(runtimes, clip, 0.9) == ("0.0", "0.00")
    assert sample_dashboard_texts(runtimes, clip, 1.2) == ("18.0", "1.00")


def test_clip_range_must_be_nonempty_and_inside_activity() -> None:
    config = AppConfig.model_validate(config_data())
    config.clip.start_seconds = 4
    config.clip.end_seconds = 4
    with pytest.raises(ConfigError):
        resolve_clip(config.clip, 10)
    config.clip.start_seconds = 0
    config.clip.end_seconds = 11
    with pytest.raises(ConfigError):
        resolve_clip(config.clip, 10)


def test_resolve_paths_uses_deterministic_defaults(tmp_path) -> None:
    (tmp_path / "b.fit").touch()
    (tmp_path / "a.gpx").touch()
    (tmp_path / "font.ttf").touch()
    config = AppConfig.model_validate(config_data())
    paths = resolve_paths(tmp_path, config)
    assert paths.activity.name == "a.gpx"
    assert paths.font.name == "font.ttf"
    assert paths.output.name == "overlay.mp4"
    assert paths.output.parent == tmp_path / "export"
    assert paths.preview == tmp_path / "export" / "preview.png"


def test_resolve_paths_does_not_treat_legacy_custom_output_as_source_video(tmp_path) -> None:
    (tmp_path / "activity.gpx").touch()
    (tmp_path / "font.ttf").touch()
    (tmp_path / "custom.mp4").touch()
    config = AppConfig.model_validate(config_data())
    config.output.filename = "custom.mp4"

    paths = resolve_paths(tmp_path, config)

    assert paths.videos == ()


def test_dashboard_without_data_in_clip_renders_dash() -> None:
    raw = config_data()
    raw["dashboards"] = [raw["dashboards"][0]]
    config = AppConfig.model_validate(raw)
    activity = ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=20,
        metrics={MetricSource.SPEED: TimeSeries((0.0, 1.0), (1.0, 2.0))},
        record_count=2,
    )
    runtimes = build_dashboard_runtimes(config, activity, ClipRange(10, 20))
    assert len(runtimes) == 1
    assert sample_dashboard_texts(runtimes, ClipRange(10, 20), 15) == ("-",)


def test_instant_metric_renders_dash_during_long_gap() -> None:
    raw = config_data()
    raw["dashboards"] = [raw["dashboards"][0]]
    config = AppConfig.model_validate(raw)
    activity = ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={MetricSource.SPEED: TimeSeries((0.0, 10.0), (1.0, 2.0))},
        record_count=2,
    )
    clip = ClipRange(0, 10)
    runtimes = build_dashboard_runtimes(config, activity, clip)
    assert sample_dashboard_texts(runtimes, clip, 5) == ("-",)
