from __future__ import annotations

import math
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
from ride_overlay_dashboard import (
    PixelTrajectoryPoint,
    TrajectoryCoverageState,
    _colorize_mask,
    _draw_rounded_paths,
    build_trajectory_coverage_plan,
    render_accumulated_trajectory_layer,
    simplify_trajectory_segment,
    trajectory_paths_at,
)


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
        "marker_scale": 0.25,
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


def test_trajectory_mask_has_antialiased_edge_pixels() -> None:
    mask = _draw_rounded_paths((64, 64), [[(8, 52), (56, 12)]], line_width=14)

    assert any(count > 0 for count in mask.histogram()[1:255])


def test_render_simplification_removes_micro_jitter_but_preserves_reversals() -> None:
    noisy = tuple(
        PixelTrajectoryPoint(float(index), float(index), y, 0)
        for index, y in enumerate((0.0, 0.1, -0.1, 0.1, 0.0))
    )
    reversal = (
        PixelTrajectoryPoint(0.0, 0.0, 0.0, 0),
        PixelTrajectoryPoint(1.0, 10.0, 0.0, 0),
        PixelTrajectoryPoint(2.0, 0.0, 0.0, 0),
        PixelTrajectoryPoint(3.0, 10.0, 0.0, 0),
    )

    assert len(simplify_trajectory_segment(noisy, 0.5)) == 2
    assert simplify_trajectory_segment(reversal, 0.5) == reversal


def test_trajectory_rectangle_includes_stroke_marker_and_configured_margin() -> None:
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
    config = trajectory_config(line_width=20, margin=0.05)
    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 10),
        paths=marker_paths(config),
    )[0]
    assert isinstance(runtime, TrajectoryRuntime)
    marker_side = round(128 * runtime.config.marker_scale)
    visual_radius = max(runtime.config.line_width / 2, math.hypot(marker_side, marker_side) / 2 + 2)
    horizontal_margin = runtime.width_px * runtime.config.margin
    vertical_margin = runtime.height_px * runtime.config.margin

    for point in runtime.points:
        assert point.x - visual_radius >= runtime.origin_x + horizontal_margin - 1e-6
        assert point.x + visual_radius <= (
            runtime.origin_x + runtime.width_px - horizontal_margin + 1e-6
        )
        assert point.y - visual_radius >= runtime.origin_y + vertical_margin - 1e-6
        assert point.y + visual_radius <= (
            runtime.origin_y + runtime.height_px - vertical_margin + 1e-6
        )


def test_negative_trajectory_margin_allows_visuals_outside_rectangle() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = build_activity(
        [
            RawPoint(start, 0, latitude=30.0, longitude=120.0),
            RawPoint(
                start + timedelta(seconds=1),
                0,
                latitude=30.00001,
                longitude=120.00001,
            ),
        ]
    )
    config = trajectory_config(
        anchor={"x": 0.5, "y": 0.5},
        align="center",
        line_width=20,
        margin=-0.1,
        marker_scale=0.01,
    )
    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 1),
        paths=marker_paths(config),
    )[0]
    assert isinstance(runtime, TrajectoryRuntime)
    plan = build_trajectory_coverage_plan(runtime, 320, 180)
    assert plan is not None

    outer_left = math.floor(runtime.origin_x)
    assert plan.left < outer_left
    overflow_width = outer_left - plan.left
    assert any(
        alpha > 0
        for y in range(plan.height)
        for alpha in plan.route_mask.crop(
            (0, y, overflow_width, y + 1)
        ).get_flattened_data()
    )


def test_global_opacity_multiplies_background_and_dashboard_alpha() -> None:
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.width = 2
    renderer.height = 1
    renderer.opacity = 0.5
    renderer.dashboard_background = Image.new("RGBA", (2, 1), (10, 20, 30, 128))

    def draw_dashboard(frame, _runtimes, _texts, *, preview):
        assert preview is False
        frame.putpixel((1, 0), (200, 100, 50, 200))

    renderer._draw_dashboards = draw_dashboard
    overlay = renderer.render_overlay([], ())

    assert overlay.getpixel((0, 0)) == (10, 20, 30, 64)
    assert overlay.getpixel((1, 0)) == (200, 100, 50, 100)


def test_optional_rounded_background_uses_dashboard_rectangle() -> None:
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
    config = trajectory_config(
        overlap_blend_mode="accumulate",
        background_color="#11223380",
        background_corner_radius=20,
    )
    runtime = build_dashboard_runtimes(
        config,
        activity,
        ClipRange(0, 10),
        paths=marker_paths(config),
    )[0]
    assert isinstance(runtime, TrajectoryRuntime)
    plan = build_trajectory_coverage_plan(runtime, 320, 180)
    assert plan is not None

    background_pixels = [
        pixel
        for pixel, route_alpha in zip(
            plan.base_layer.get_flattened_data(),
            plan.route_mask.get_flattened_data(),
            strict=True,
        )
        if route_alpha == 0 and pixel[3] == 128
    ]
    assert background_pixels
    assert background_pixels[0][:3] == (17, 34, 51)
    assert plan.base_layer.getpixel((0, 0))[3] < 128


@pytest.mark.parametrize("overlap_blend_mode", ["uniform", "accumulate"])
def test_trajectory_layer_is_rendered_and_reused_for_same_update_time(
    overlap_blend_mode: str,
) -> None:
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
    config = trajectory_config(
        anchor={"x": 0.25, "y": 0.5},
        align="center",
        overlap_blend_mode=overlap_blend_mode,
    )
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


def _repeated_route_runtime() -> TrajectoryRuntime:
    dashboard = trajectory_config(
        width=1.0,
        line_width=6,
        remaining_color="#FFFFFFFF",
        completed_color="#FF000080",
        overlap_blend_mode="accumulate",
    ).dashboards[0]
    points = (
        PixelTrajectoryPoint(0.0, 10.0, 20.0, 0),
        PixelTrajectoryPoint(10.0, 50.0, 20.0, 0),
        PixelTrajectoryPoint(20.0, 10.0, 20.0, 0),
        PixelTrajectoryPoint(30.0, 50.0, 20.0, 0),
    )
    return TrajectoryRuntime(
        config=dashboard,
        projected=SimpleNamespace(),
        marker_path=Path("unused.png"),
        points=points,
        segments=(points,),
        origin_x=0.0,
        origin_y=0.0,
        width_px=64.0,
        height_px=40.0,
        scale_px_per_meter=1.0,
    )


def test_accumulated_overlap_deepens_after_each_distinct_pass() -> None:
    runtime = _repeated_route_runtime()
    plan = build_trajectory_coverage_plan(runtime, 64, 40)
    assert plan is not None
    state = TrajectoryCoverageState(bytearray(plan.width * plan.height))
    pixel_index = (20 - plan.top) * plan.width + (30 - plan.left)

    first = render_accumulated_trajectory_layer(
        plan, state, 6.0, runtime.config.completed_color
    )
    first_pixel = first.getpixel((30 - plan.left, 20 - plan.top))
    assert state.counts[pixel_index] == 1

    second = render_accumulated_trajectory_layer(
        plan, state, 16.0, runtime.config.completed_color
    )
    second_pixel = second.getpixel((30 - plan.left, 20 - plan.top))
    assert state.counts[pixel_index] == 2

    third = render_accumulated_trajectory_layer(
        plan, state, 26.0, runtime.config.completed_color
    )
    third_pixel = third.getpixel((30 - plan.left, 20 - plan.top))
    assert state.counts[pixel_index] == 3
    assert first_pixel[1] > second_pixel[1] > third_pixel[1]
    assert first_pixel[3] == second_pixel[3] == third_pixel[3] == 255


def test_accumulated_overlap_does_not_double_count_a_connected_joint() -> None:
    runtime = _repeated_route_runtime()
    points = (
        PixelTrajectoryPoint(0.0, 10.0, 30.0, 0),
        PixelTrajectoryPoint(10.0, 30.0, 30.0, 0),
        PixelTrajectoryPoint(20.0, 30.0, 10.0, 0),
    )
    runtime = TrajectoryRuntime(
        config=runtime.config,
        projected=runtime.projected,
        marker_path=runtime.marker_path,
        points=points,
        segments=(points,),
        origin_x=runtime.origin_x,
        origin_y=runtime.origin_y,
        width_px=runtime.width_px,
        height_px=runtime.height_px,
        scale_px_per_meter=runtime.scale_px_per_meter,
    )
    plan = build_trajectory_coverage_plan(runtime, 64, 40)
    assert plan is not None
    state = TrajectoryCoverageState(bytearray(plan.width * plan.height))

    render_accumulated_trajectory_layer(plan, state, 20.0, "#FF000080")

    pixel_index = (30 - plan.top) * plan.width + (30 - plan.left)
    assert state.counts[pixel_index] == 1


def test_accumulated_overlap_rebuilds_counts_after_backward_seek() -> None:
    runtime = _repeated_route_runtime()
    plan = build_trajectory_coverage_plan(runtime, 64, 40)
    assert plan is not None
    state = TrajectoryCoverageState(bytearray(plan.width * plan.height))
    pixel_index = (20 - plan.top) * plan.width + (30 - plan.left)

    render_accumulated_trajectory_layer(plan, state, 26.0, "#FF000080")
    assert state.counts[pixel_index] == 3

    render_accumulated_trajectory_layer(plan, state, 6.0, "#FF000080")
    assert state.counts[pixel_index] == 1


def test_uniform_mode_keeps_complete_route_beneath_completed_route() -> None:
    runtime = _repeated_route_runtime()
    dashboard = trajectory_config(
        width=1.0,
        line_width=6,
        remaining_color="#FFFFFF80",
        completed_color="#FF000080",
        overlap_blend_mode="uniform",
    ).dashboards[0]
    runtime = TrajectoryRuntime(
        config=dashboard,
        projected=SimpleNamespace(sample_at=lambda _seconds: None),
        marker_path=runtime.marker_path,
        points=runtime.points,
        segments=runtime.segments,
        origin_x=runtime.origin_x,
        origin_y=runtime.origin_y,
        width_px=runtime.width_px,
        height_px=runtime.height_px,
        scale_px_per_meter=runtime.scale_px_per_meter,
    )
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.width = 64
    renderer.height = 40
    renderer.marker_images = {}
    renderer.trajectory_layers = {}
    frame = Image.new("RGBA", (64, 40), (0, 0, 0, 0))

    renderer._render_trajectory(frame, runtime, 30.0)

    assert frame.getpixel((30, 20))[3] > 128


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
