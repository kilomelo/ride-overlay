from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ride_overlay_data import MetricSource, RawPoint, activity_details, build_activity


def _startup_points() -> list[RawPoint]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    altitude = (
        (None, *(0.4 for _ in range(13)), 114.6, 114.6, 111.2, 109.6, 109.2, 108.4)
        + (100.8, 101.4, 102.2, 102.4, 102.6, 103.2, 104.2, 103.6, 103, 101.8)
        + (102.0,) * 41
    )
    return [
        RawPoint(
            start + timedelta(seconds=second),
            0,
            altitude_m=height,
            distance_m=5 * second,
            speed_mps=5,
            pressure_pa=100_400,
            power_w=160,
        )
        for second, height in enumerate(altitude)
    ]


def test_startup_altitude_removal_preserves_timeline_and_other_metrics() -> None:
    points = _startup_points()
    clean = build_activity(points)
    raw = build_activity(points, auto_fix_startup_altitude=False)
    altitude = clean.metrics[MetricSource.ALTITUDE]
    correction = clean.startup_altitude_correction

    assert correction is not None
    assert correction.jump_seconds == 14
    assert correction.first_trusted_seconds == 20
    assert correction.removed_sample_count == 19
    assert altitude.times[0] == 20
    assert altitude.value_at(19) is None
    assert altitude.moving_average(5).value_at(19) is None
    assert altitude.value_at(20) == pytest.approx(100.8)
    assert raw.metrics[MetricSource.ALTITUDE].value_at(14) == pytest.approx(114.6)
    assert raw.startup_altitude_correction is None
    assert raw.metrics[MetricSource.TOTAL_ASCENT].value_at(30) > 100
    assert clean.metrics[MetricSource.TOTAL_ASCENT].value_at(30) < 5
    assert clean.metrics[MetricSource.GRADE].value_at(19) is None
    assert clean.start_time == raw.start_time
    assert clean.duration_seconds == raw.duration_seconds
    assert clean.record_count == raw.record_count
    for source in (MetricSource.SPEED, MetricSource.DISTANCE, MetricSource.POWER):
        assert clean.metrics[source] == raw.metrics[source]
    assert activity_details(clean)["startup_altitude_correction"]["removed_sample_count"] == 19


@pytest.mark.parametrize(
    "scenario", ["isolated_spike", "sea_level", "pressure_agrees", "no_evidence"]
)
def test_startup_altitude_ambiguous_or_normal_prefix_is_preserved(scenario: str) -> None:
    points = _startup_points()
    if scenario == "isolated_spike":
        points = [
            replace(point, altitude_m=104 if index == 14 else 0.4)
            for index, point in enumerate(points)
        ]
    elif scenario == "sea_level":
        points = [
            replace(point, altitude_m=-2 + index * 0.05) for index, point in enumerate(points)
        ]
    elif scenario == "pressure_agrees":
        points = [
            replace(point, pressure_pa=100_400 * math.exp(-(point.altitude_m or 0) / 8430))
            for point in points
        ]
    else:
        points = [replace(point, pressure_pa=None) for point in points]

    assert build_activity(points).startup_altitude_correction is None
