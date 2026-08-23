from __future__ import annotations

from datetime import UTC, datetime

import pytest
from PIL import Image
from pydantic import ValidationError

from ride_overlay import (
    ActivityData,
    AppConfig,
    ClipRange,
    ConfigError,
    ElevationProfileRuntime,
    FrameRenderer,
    MetricSource,
    TimeSeries,
    build_dashboard_runtimes,
    elevation_point_at,
    sample_dashboard_texts,
)


def elevation_config(**overrides) -> AppConfig:
    dashboard = {
        "type": "elevation_profile",
        "id": "elevation",
        "width": 0.5,
        "height": 0.5,
        "margin": 0.05,
        "anchor": {"x": 0.0, "y": 0.0},
        "align": "top_left",
        "update_interval_ms": 200,
        "line_color": "#FF0000FF",
        "line_width": 6,
        "dot_color": "#00FF00FF",
        "dot_size": 16,
        "vertical_line_color": "#0000FFFF",
        "vertical_line_width": 2,
        "background_color": None,
        "background_corner_radius": 8,
    }
    dashboard.update(overrides)
    return AppConfig.model_validate(
        {
            "schema_version": 2,
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


def altitude_activity(
    times: tuple[float, ...] = (0.0, 5.0, 10.0),
    values: tuple[float, ...] = (100.0, 200.0, 150.0),
    *,
    duration: float = 10.0,
) -> ActivityData:
    return ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=duration,
        metrics={MetricSource.ALTITUDE: TimeSeries(times, values)},
        record_count=len(times),
    )


def build_runtime(
    config: AppConfig,
    activity: ActivityData | None = None,
    clip: ClipRange | None = None,
) -> ElevationProfileRuntime:
    runtime = build_dashboard_runtimes(
        config,
        activity or altitude_activity(),
        clip or ClipRange(0, 10),
    )[0]
    assert isinstance(runtime, ElevationProfileRuntime)
    return runtime


def bare_renderer() -> FrameRenderer:
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.width = 320
    renderer.height = 180
    renderer.elevation_static_layers = {}
    renderer.elevation_layers = {}
    return renderer


def test_elevation_profile_validates_geometry_and_colors() -> None:
    config = elevation_config()
    dashboard = config.dashboards[0]
    assert dashboard.height == pytest.approx(0.5)

    for overrides in (
        {"height": 0},
        {"height": 1.01},
        {"margin": -0.251},
        {"dot_size": 0},
        {"vertical_line_width": 0},
        {"curve_smoothing": -0.01},
        {"curve_smoothing": 1.01},
        {"line_color": "red"},
    ):
        with pytest.raises(ValidationError):
            elevation_config(**overrides)


def test_elevation_profile_maps_full_activity_time_and_requested_y_range() -> None:
    config = elevation_config()
    report = type("Report", (), {"details": {}})()
    runtimes = build_dashboard_runtimes(
        config,
        altitude_activity(),
        ClipRange(2, 8),
        report=report,
    )

    runtime = runtimes[0]
    assert isinstance(runtime, ElevationProfileRuntime)
    assert (runtime.width_px, runtime.height_px) == pytest.approx((160, 90))
    assert runtime.elevation_min_m == pytest.approx(100)
    assert runtime.elevation_max_m == pytest.approx(200)
    assert runtime.y_axis_min_m == pytest.approx(80)
    assert runtime.points[0].x == pytest.approx(runtime.plot_left)
    assert runtime.points[-1].x == pytest.approx(runtime.plot_right)
    assert runtime.points[1].y == pytest.approx(runtime.plot_top)
    assert runtime.points[0].y < runtime.plot_bottom
    details = report.details["dashboards"][0]
    assert details["y_axis_min_m"] == pytest.approx(80)
    assert details["requested_height_ratio"] == pytest.approx(0.5)


def test_elevation_profile_visual_sizes_are_inside_outer_rectangle() -> None:
    runtime = build_runtime(elevation_config(margin=0))
    radius = runtime.config.dot_size / 2

    assert runtime.plot_left - radius >= runtime.origin_x
    assert runtime.plot_right + radius <= runtime.origin_x + runtime.width_px
    assert runtime.plot_top - radius >= runtime.origin_y
    assert runtime.plot_bottom + radius <= runtime.origin_y + runtime.height_px


def test_elevation_profile_rejects_rectangle_too_small_for_visual_sizes() -> None:
    config = elevation_config(width=0.01, height=0.01, margin=0)

    with pytest.raises(ConfigError, match="无法容纳线宽、圆点大小"):
        build_runtime(config)


def test_elevation_curve_breaks_at_long_gap_and_dot_holds_last_curve_point() -> None:
    activity = altitude_activity(
        (0.0, 2.0, 10.0),
        (100.0, 120.0, 180.0),
    )
    runtime = build_runtime(elevation_config(), activity)

    assert len(runtime.segments) == 2
    held = elevation_point_at(runtime, 6.0)
    resumed = elevation_point_at(runtime, 10.0)
    assert held == runtime.points[1]
    assert resumed == runtime.points[2]


def test_elevation_dot_interpolates_on_continuous_curve() -> None:
    runtime = build_runtime(elevation_config())

    sample = elevation_point_at(runtime, 2.5)

    assert sample.altitude_m == pytest.approx(150)
    assert sample.x == pytest.approx((runtime.points[0].x + runtime.points[1].x) / 2)
    assert sample.y == pytest.approx((runtime.points[0].y + runtime.points[1].y) / 2)


def test_curve_smoothing_rounds_peak_and_keeps_dot_on_rendered_curve() -> None:
    raw = build_runtime(elevation_config(curve_smoothing=0))
    smoothed = build_runtime(elevation_config(curve_smoothing=1))

    assert len(smoothed.points) > len(raw.points)
    assert smoothed.points[0] == raw.points[0]
    assert smoothed.points[-1] == raw.points[-1]
    assert elevation_point_at(raw, 5).altitude_m == pytest.approx(200)

    peak = elevation_point_at(smoothed, 5)
    assert 150 < peak.altitude_m < 200
    assert peak.y > smoothed.plot_top


def test_curve_smoothing_does_not_bridge_elevation_gaps() -> None:
    activity = altitude_activity(
        (0.0, 2.0, 10.0),
        (100.0, 120.0, 180.0),
    )
    runtime = build_runtime(elevation_config(curve_smoothing=1), activity)

    assert len(runtime.segments) == 2
    assert elevation_point_at(runtime, 6.0) == runtime.segments[0][-1]


def test_curve_smoothing_uses_visible_time_scale_low_pass_filter() -> None:
    times = tuple(float(index) for index in range(101))
    values = tuple(100.0 if index % 2 == 0 else 120.0 for index in range(101))
    activity = altitude_activity(times, values, duration=100)
    config = elevation_config(curve_smoothing=1)
    report = type("Report", (), {"details": {}})()

    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 100),
        report=report,
    )[0]

    assert isinstance(runtime, ElevationProfileRuntime)
    filtered = elevation_point_at(runtime, 50).altitude_m
    assert abs(filtered - 110) < abs(values[50] - 110)
    details = report.details["dashboards"][0]
    assert details["curve_smoothing_window_seconds"] == pytest.approx(2)
    assert details["curve_smoothing_algorithm"].startswith("centered_moving_average")


def test_elevation_sampling_uses_dashboard_update_interval() -> None:
    config = elevation_config(update_interval_ms=1000)
    runtime = build_runtime(config)

    values = sample_dashboard_texts([runtime], ClipRange(0, 10), 2.75)

    assert values == (2.0,)


def test_flat_elevation_profile_gets_visible_vertical_range() -> None:
    activity = altitude_activity(values=(123.0, 123.0, 123.0))
    runtime = build_runtime(elevation_config(), activity)

    assert runtime.elevation_max_m == pytest.approx(123)
    assert runtime.y_axis_min_m == pytest.approx(122)
    assert all(point.y == pytest.approx(runtime.plot_top) for point in runtime.points)


def test_elevation_renderer_draws_curve_vertical_line_and_dot(tmp_path) -> None:
    del tmp_path
    runtime = build_runtime(elevation_config())
    renderer = bare_renderer()
    frame = Image.new("RGBA", (320, 180), (0, 0, 0, 0))

    renderer._render_elevation_profile(frame, runtime, 5.0)

    dot_x = round(runtime.points[1].x)
    dot_y = round(runtime.points[1].y)
    vertical_y = round((runtime.points[1].y + runtime.plot_bottom) / 2)
    assert frame.getpixel((dot_x, dot_y))[:3] == (0, 255, 0)
    assert frame.getpixel((dot_x, vertical_y))[:3] == (0, 0, 255)
    assert frame.getchannel("A").getbbox() is not None
    assert any(0 < alpha < 255 for alpha in frame.getchannel("A").get_flattened_data())


@pytest.mark.parametrize("line_width", [1, 2, 3, 4])
def test_elevation_vertical_line_uses_exact_output_pixel_width(line_width: int) -> None:
    runtime = build_runtime(
        elevation_config(
            vertical_line_width=line_width,
            vertical_line_color="#0000FFFF",
        )
    )
    renderer = bare_renderer()

    for time_seconds in (0.37, 2.13, 4.81, 7.29, 9.63):
        frame = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
        renderer._render_elevation_profile(frame, runtime, time_seconds)
        row_y = round(runtime.plot_bottom) - 1
        blue_columns = [
            x
            for x in range(frame.width)
            if frame.getpixel((x, row_y)) == (0, 0, 255, 255)
        ]

        assert len(blue_columns) == line_width
        assert blue_columns == list(
            range(blue_columns[0], blue_columns[0] + line_width)
        )


def test_optional_background_uses_outer_dashboard_rectangle() -> None:
    runtime = build_runtime(elevation_config(background_color="#11223380", margin=0.1))
    renderer = bare_renderer()
    frame = Image.new("RGBA", (320, 180), (0, 0, 0, 0))

    renderer._render_elevation_profile(frame, runtime, 5.0)

    assert frame.getpixel((5, 5)) == (17, 34, 51, 128)
    assert renderer.dashboard_bounds(runtime, None) == pytest.approx((0, 0, 160, 90))


def test_missing_or_insufficient_altitude_skips_dashboard() -> None:
    config = elevation_config()
    missing = ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={},
        record_count=2,
    )
    one_point = altitude_activity((0.0,), (100.0,))

    assert build_dashboard_runtimes(config, missing, ClipRange(0, 10)) == []
    assert build_dashboard_runtimes(config, one_point, ClipRange(0, 10)) == []


def test_dense_elevation_data_is_reduced_without_losing_extrema() -> None:
    times = tuple(index / 10 for index in range(101))
    values = tuple(200.0 if index == 50 else 100.0 for index in range(101))
    activity = altitude_activity(times, values)
    config = elevation_config(width=0.1)
    report = type("Report", (), {"details": {}})()

    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 10),
        report=report,
    )[0]

    assert isinstance(runtime, ElevationProfileRuntime)
    assert report.details["dashboards"][0]["render_point_count"] < len(times)
    assert any(point.altitude_m == 200 for segment in runtime.segments for point in segment)
