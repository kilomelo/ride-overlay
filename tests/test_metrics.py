from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ride_overlay import (
    DashboardConfig,
    GapStrategy,
    MetricSource,
    RawPoint,
    TimeSeries,
    build_activity,
    convert_value,
    format_current_time,
    format_dashboard_value,
)


def dashboard(source: str, **overrides) -> DashboardConfig:
    data = {
        "id": source,
        "source": source,
        "smoothing": {"method": "none"},
        "precision": 1,
        "pad_zeros": True,
        "update_interval_ms": 1000,
        "font_size": 30,
        "anchor": {"x": 0.5, "y": 0.5},
        "align": "center",
    }
    data.update(overrides)
    return DashboardConfig.model_validate(data)


def test_all_requested_metrics_are_built() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = [
        RawPoint(
            start,
            0,
            speed_mps=5,
            distance_m=0,
            altitude_m=100,
            temperature_c=20,
            pressure_pa=100_000,
            cadence_rpm=80,
            heart_rate_bpm=100,
            power_w=100,
            calories_kcal=0,
        ),
        RawPoint(
            start + timedelta(seconds=5),
            0,
            speed_mps=5,
            distance_m=20,
            altitude_m=102,
            temperature_c=21,
            pressure_pa=100_100,
            cadence_rpm=85,
            heart_rate_bpm=110,
            power_w=150,
        ),
        RawPoint(
            start + timedelta(seconds=10),
            0,
            speed_mps=5,
            distance_m=40,
            altitude_m=101,
            temperature_c=22,
            pressure_pa=100_200,
            cadence_rpm=90,
            heart_rate_bpm=120,
            power_w=200,
        ),
        RawPoint(
            start + timedelta(seconds=15),
            0,
            speed_mps=5,
            distance_m=60,
            altitude_m=104,
            temperature_c=23,
            pressure_pa=100_300,
            cadence_rpm=95,
            heart_rate_bpm=130,
            power_w=250,
            calories_kcal=100,
        ),
    ]

    activity = build_activity(points)
    assert set(activity.metrics) == set(MetricSource)
    assert activity.metrics[MetricSource.TOTAL_ASCENT].values[-1] == pytest.approx(5)
    assert activity.metrics[MetricSource.CALORIES].value_at(7.5) == pytest.approx(50)
    assert activity.metrics[MetricSource.AVERAGE_SPEED].values[-1] == pytest.approx(5)
    assert activity.metrics[MetricSource.AVERAGE_HEART_RATE].values[-1] == pytest.approx(115)
    assert activity.metrics[MetricSource.AVERAGE_CADENCE].values[-1] == pytest.approx(87.5)
    assert activity.metrics[MetricSource.AVERAGE_POWER].values[-1] == pytest.approx(175)
    assert activity.metrics[MetricSource.GRADE].values


def test_elapsed_time_uses_hh_mm_ss_format() -> None:
    config = dashboard("elapsed_time")
    assert config.unit == "hms"
    assert format_dashboard_value(config, 3661.99) == "01:01:01"


def test_current_time_uses_hh_mm_ss_and_wraps_at_midnight() -> None:
    config = dashboard("current_time")
    assert config.unit == "hms"
    assert format_dashboard_value(config, 86_399.99) == "23:59:59"
    assert format_current_time(86_401.0) == "00:00:01"


def test_instant_metric_is_missing_but_average_holds_during_long_gap() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = [
        RawPoint(start, 0, cadence_rpm=80),
        RawPoint(start + timedelta(seconds=1), 0, cadence_rpm=90),
        RawPoint(start + timedelta(seconds=10), 0, cadence_rpm=100),
        RawPoint(start + timedelta(seconds=11), 0, cadence_rpm=110),
    ]
    activity = build_activity(points)
    cadence = activity.metrics[MetricSource.CADENCE]
    average = activity.metrics[MetricSource.AVERAGE_CADENCE]

    assert cadence.value_at(5) is None
    assert average.value_at(5) == pytest.approx(85)
    assert average.value_at(10) == pytest.approx(85)


def test_cumulative_metric_holds_during_long_gap() -> None:
    series = TimeSeries(
        (0.0, 10.0),
        (0.0, 100.0),
        cumulative=True,
        gap_strategy=GapStrategy.HOLD,
    )
    assert series.value_at(5) == 0


@pytest.mark.parametrize(
    ("source", "unit", "value", "expected"),
    [
        (MetricSource.ALTITUDE, "ft", 100, 328.0839895),
        (MetricSource.TEMPERATURE, "F", 20, 68),
        (MetricSource.PRESSURE, "hPa", 101_325, 1013.25),
        (MetricSource.PRESSURE, "kPa", 101_325, 101.325),
        (MetricSource.PRESSURE, "mmHg", 101_325, 760.0),
        (MetricSource.AVERAGE_SPEED, "km/h", 10, 36),
    ],
)
def test_unit_conversion(source, unit, value, expected) -> None:
    assert convert_value(source, value, unit) == pytest.approx(expected, rel=1e-5)


@pytest.mark.parametrize(
    "source",
    [
        "elapsed_time",
        "total_ascent",
        "calories",
        "average_speed",
        "average_heart_rate",
        "average_cadence",
        "average_power",
    ],
)
def test_cumulative_and_average_metrics_reject_smoothing(source: str) -> None:
    with pytest.raises(ValidationError):
        dashboard(
            source,
            smoothing={"method": "moving_average", "window_seconds": 1},
        )
