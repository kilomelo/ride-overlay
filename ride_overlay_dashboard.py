"""Dashboard configuration, sampling, formatting, and Pillow rendering."""

from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from PIL import Image, ImageColor, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ride_overlay_data import (
    ActivityData,
    GapStrategy,
    MetricSource,
    ProjectedTrajectory,
    RideOverlayError,
    TimeSeries,
    TrajectorySample,
    project_trajectory,
    series_gap_details,
)

LOGGER = logging.getLogger("ride-overlay")
MILES_PER_METER = 0.000621371192237334
MIN_TRAJECTORY_BBOX_WIDTH_METERS = 1.0


class ConfigError(RideOverlayError):
    """Invalid project or configuration."""


class SmoothingMethod(StrEnum):
    NONE = "none"
    MOVING_AVERAGE = "moving_average"


class Align(StrEnum):
    TOP_LEFT = "top_left"
    TOP_CENTER = "top_center"
    TOP_RIGHT = "top_right"
    MIDDLE_LEFT = "middle_left"
    CENTER = "center"
    MIDDLE_RIGHT = "middle_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_CENTER = "bottom_center"
    BOTTOM_RIGHT = "bottom_right"


class BackgroundMode(StrEnum):
    TRANSPARENT = "transparent"
    CHROMA_KEY = "chroma_key"


class CumulativeOrigin(StrEnum):
    ACTIVITY_START = "activity_start"
    CLIP_START = "clip_start"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def validate_color(value: str) -> str:
    if not isinstance(value, str) or len(value) not in (7, 9) or not value.startswith("#"):
        raise ValueError("颜色必须使用 #RRGGBB 或 #RRGGBBAA 格式")
    try:
        int(value[1:], 16)
    except ValueError as exc:
        raise ValueError("颜色必须使用十六进制字符") from exc
    return value.upper()


class BackgroundConfig(StrictModel):
    mode: BackgroundMode = BackgroundMode.CHROMA_KEY
    chroma_key_color: str = "#00FF00"

    _color = field_validator("chroma_key_color")(validate_color)


class OutputConfig(StrictModel):
    filename: str | None = None
    width: Annotated[int, Field(ge=16, le=8192)] = 1920
    height: Annotated[int, Field(ge=16, le=8192)] = 1080
    fps: Annotated[float, Field(gt=0, le=240)] = 30
    background: BackgroundConfig = Field(default_factory=BackgroundConfig)
    bitrate_mbps: Annotated[float, Field(gt=0, le=1000)] = 16

    _normalize_filename = field_validator("filename", mode="before")(blank_to_none)

    @model_validator(mode="after")
    def validate_dimensions(self) -> OutputConfig:
        if self.background.mode == BackgroundMode.CHROMA_KEY and (
            self.width % 2 or self.height % 2
        ):
            raise ValueError("绿幕 H.264 输出的 width 和 height 必须是偶数")
        return self


class SmoothingConfig(StrictModel):
    method: SmoothingMethod = SmoothingMethod.NONE
    window_seconds: Annotated[float, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def validate_window(self) -> SmoothingConfig:
        if self.method == SmoothingMethod.MOVING_AVERAGE and self.window_seconds is None:
            raise ValueError("moving_average 必须设置 window_seconds")
        if self.method == SmoothingMethod.NONE and self.window_seconds is not None:
            raise ValueError("method=none 时不得设置 window_seconds")
        return self


class AnchorConfig(StrictModel):
    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]


UNIT_OPTIONS: dict[MetricSource, set[str]] = {
    MetricSource.SPEED: {"km/h", "m/s", "mph"},
    MetricSource.DISTANCE: {"km", "m", "mi"},
    MetricSource.ELAPSED_TIME: {"hms"},
    MetricSource.CURRENT_TIME: {"hms"},
    MetricSource.ALTITUDE: {"m", "ft"},
    MetricSource.TEMPERATURE: {"C", "F"},
    MetricSource.PRESSURE: {"Pa", "hPa", "kPa", "mmHg"},
    MetricSource.CADENCE: {"rpm"},
    MetricSource.HEART_RATE: {"bpm"},
    MetricSource.POWER: {"W"},
    MetricSource.GRADE: {"%"},
    MetricSource.TOTAL_ASCENT: {"m", "ft"},
    MetricSource.CALORIES: {"kcal"},
    MetricSource.AVERAGE_SPEED: {"km/h", "m/s", "mph"},
    MetricSource.AVERAGE_HEART_RATE: {"bpm"},
    MetricSource.AVERAGE_CADENCE: {"rpm"},
    MetricSource.AVERAGE_POWER: {"W"},
}
DEFAULT_UNITS = {
    MetricSource.SPEED: "km/h",
    MetricSource.DISTANCE: "km",
    MetricSource.ELAPSED_TIME: "hms",
    MetricSource.CURRENT_TIME: "hms",
    MetricSource.ALTITUDE: "m",
    MetricSource.TEMPERATURE: "C",
    MetricSource.PRESSURE: "hPa",
    MetricSource.CADENCE: "rpm",
    MetricSource.HEART_RATE: "bpm",
    MetricSource.POWER: "W",
    MetricSource.GRADE: "%",
    MetricSource.TOTAL_ASCENT: "m",
    MetricSource.CALORIES: "kcal",
    MetricSource.AVERAGE_SPEED: "km/h",
    MetricSource.AVERAGE_HEART_RATE: "bpm",
    MetricSource.AVERAGE_CADENCE: "rpm",
    MetricSource.AVERAGE_POWER: "W",
}
CUMULATIVE_SOURCES = {
    MetricSource.DISTANCE,
    MetricSource.ELAPSED_TIME,
    MetricSource.TOTAL_ASCENT,
    MetricSource.CALORIES,
}
AVERAGE_SOURCES = {
    MetricSource.AVERAGE_SPEED,
    MetricSource.AVERAGE_HEART_RATE,
    MetricSource.AVERAGE_CADENCE,
    MetricSource.AVERAGE_POWER,
}
NON_SMOOTHABLE_SOURCES = CUMULATIVE_SOURCES | AVERAGE_SOURCES | {MetricSource.CURRENT_TIME}


class DashboardConfig(StrictModel):
    type: Literal["numeric"] = "numeric"
    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    source: MetricSource
    unit: str | None = None
    smoothing: SmoothingConfig = Field(default_factory=SmoothingConfig)
    precision: Annotated[int, Field(ge=0, le=6)] = 0
    pad_zeros: bool = True
    update_interval_ms: Annotated[int, Field(gt=0, le=3_600_000)] = 100
    font_size: Annotated[int, Field(gt=0, le=2048)]
    anchor: AnchorConfig
    align: Align = Align.CENTER
    color: str = "#FFFFFFFF"
    stroke_width: Annotated[int, Field(ge=0, le=100)] = 0
    stroke_color: str = "#000000FF"

    _text_color = field_validator("color", "stroke_color")(validate_color)
    _normalize_unit = field_validator("unit", mode="before")(blank_to_none)

    @model_validator(mode="after")
    def validate_metric_options(self) -> DashboardConfig:
        if self.source in NON_SMOOTHABLE_SOURCES and self.smoothing.method != SmoothingMethod.NONE:
            raise ValueError(f"{self.source.value} 指标不支持平滑")
        options = UNIT_OPTIONS.get(self.source)
        if self.unit is None and self.source in DEFAULT_UNITS:
            self.unit = DEFAULT_UNITS[self.source]
        if options is not None and self.unit not in options:
            allowed = ", ".join(sorted(options))
            raise ValueError(f"{self.source.value} 的 unit 必须是: {allowed}")
        return self


class TrajectoryDashboardConfig(StrictModel):
    type: Literal["trajectory"]
    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    width: Annotated[float, Field(gt=0, le=1)]
    anchor: AnchorConfig
    align: Align = Align.CENTER
    update_interval_ms: Annotated[int, Field(gt=0, le=3_600_000)] = 200
    line_width: Annotated[int, Field(gt=0, le=200)] = 8
    remaining_color: str = "#FFFFFF66"
    completed_color: str = "#00E676CC"
    marker_image_file: str | None = None
    marker_scale: Annotated[float, Field(gt=0, le=100)] = 2.0

    _line_colors = field_validator("remaining_color", "completed_color")(validate_color)
    _normalize_marker = field_validator("marker_image_file", mode="before")(blank_to_none)


DashboardDefinition = DashboardConfig | TrajectoryDashboardConfig


@dataclass(frozen=True)
class ClipRange:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class DashboardRuntime:
    config: DashboardConfig
    series: TimeSeries
    cumulative_offset: float = 0.0


@dataclass(frozen=True)
class PixelTrajectoryPoint:
    time_seconds: float
    x: float
    y: float
    segment: int


@dataclass(frozen=True)
class TrajectoryRuntime:
    config: TrajectoryDashboardConfig
    projected: ProjectedTrajectory
    marker_path: Path
    points: tuple[PixelTrajectoryPoint, ...]
    segments: tuple[tuple[PixelTrajectoryPoint, ...], ...]
    origin_x: float
    origin_y: float
    width_px: float
    height_px: float
    scale_px_per_meter: float


RuntimeDefinition = DashboardRuntime | TrajectoryRuntime


class ClipConfigLike(Protocol):
    cumulative_origin: CumulativeOrigin


class DashboardAppConfigLike(Protocol):
    output: OutputConfig
    clip: ClipConfigLike
    dashboards: list[DashboardDefinition]


class RenderPathsLike(Protocol):
    font: Path
    background_image: Path | None
    trajectory_markers: dict[str, Path]


class ReportLike(Protocol):
    details: dict[str, Any]


ALIGN_TO_PIL = {
    Align.TOP_LEFT: "lt",
    Align.TOP_CENTER: "mt",
    Align.TOP_RIGHT: "rt",
    Align.MIDDLE_LEFT: "lm",
    Align.CENTER: "mm",
    Align.MIDDLE_RIGHT: "rm",
    Align.BOTTOM_LEFT: "lb",
    Align.BOTTOM_CENTER: "mb",
    Align.BOTTOM_RIGHT: "rb",
}


def aligned_origin(
    anchor: AnchorConfig,
    align: Align,
    item_width: float,
    item_height: float,
    frame_width: int,
    frame_height: int,
) -> tuple[float, float]:
    anchor_x = anchor.x * frame_width
    anchor_y = anchor.y * frame_height
    horizontal = {
        Align.TOP_LEFT: 0.0,
        Align.MIDDLE_LEFT: 0.0,
        Align.BOTTOM_LEFT: 0.0,
        Align.TOP_CENTER: 0.5,
        Align.CENTER: 0.5,
        Align.BOTTOM_CENTER: 0.5,
        Align.TOP_RIGHT: 1.0,
        Align.MIDDLE_RIGHT: 1.0,
        Align.BOTTOM_RIGHT: 1.0,
    }[align]
    vertical = {
        Align.TOP_LEFT: 0.0,
        Align.TOP_CENTER: 0.0,
        Align.TOP_RIGHT: 0.0,
        Align.MIDDLE_LEFT: 0.5,
        Align.CENTER: 0.5,
        Align.MIDDLE_RIGHT: 0.5,
        Align.BOTTOM_LEFT: 1.0,
        Align.BOTTOM_CENTER: 1.0,
        Align.BOTTOM_RIGHT: 1.0,
    }[align]
    return anchor_x - item_width * horizontal, anchor_y - item_height * vertical


def _build_trajectory_runtime(
    dashboard: TrajectoryDashboardConfig,
    config: DashboardAppConfigLike,
    activity: ActivityData,
    paths: RenderPathsLike | None,
) -> tuple[TrajectoryRuntime | None, dict[str, Any]]:
    trajectory = activity.trajectory
    if trajectory is None or len(trajectory.points) < 2:
        return None, {
            "id": dashboard.id,
            "type": dashboard.type,
            "source": "position",
            "status": "SKIPPED",
            "reason": "运动文件无法提供至少两个有效位置点",
        }
    if paths is None or dashboard.id not in paths.trajectory_markers:
        raise ConfigError(f"轨迹仪表盘 {dashboard.id} 缺少已解析的当前位置图片")

    projected = project_trajectory(trajectory)
    requested_width_px = dashboard.width * config.output.width
    bbox_width_for_scale = max(projected.width_m, MIN_TRAJECTORY_BBOX_WIDTH_METERS)
    scale = requested_width_px / bbox_width_for_scale
    height_px = max(1.0, projected.height_m * scale)
    origin_x, origin_y = aligned_origin(
        dashboard.anchor,
        dashboard.align,
        requested_width_px,
        height_px,
        config.output.width,
        config.output.height,
    )

    if projected.width_m < MIN_TRAJECTORY_BBOX_WIDTH_METERS:
        LOGGER.warning(
            "轨迹仪表盘 %s 的东西向范围小于 %.1fm，缩放计算使用 %.1fm 最小宽度",
            dashboard.id,
            MIN_TRAJECTORY_BBOX_WIDTH_METERS,
            MIN_TRAJECTORY_BBOX_WIDTH_METERS,
        )
    overflow_edges: list[str] = []
    if origin_x < 0:
        overflow_edges.append("left")
    if origin_x + requested_width_px > config.output.width:
        overflow_edges.append("right")
    if origin_y < 0:
        overflow_edges.append("top")
    if origin_y + height_px > config.output.height:
        overflow_edges.append("bottom")
    if overflow_edges:
        LOGGER.warning(
            "轨迹仪表盘 %s 超出画面边界（%s），将保持配置缩放比例，不自动缩小",
            dashboard.id,
            ", ".join(overflow_edges),
        )

    points: list[PixelTrajectoryPoint] = []
    for point in projected.points:
        if projected.width_m < 1e-9:
            x = origin_x + requested_width_px / 2
        else:
            x = origin_x + (point.x_m - projected.min_x_m) * scale
        if projected.height_m < 1e-9:
            y = origin_y + height_px / 2
        else:
            y = origin_y + (projected.max_y_m - point.y_m) * scale
        points.append(PixelTrajectoryPoint(point.time_seconds, x, y, point.segment))

    segments: list[list[PixelTrajectoryPoint]] = []
    for point in points:
        if not segments or point.segment != segments[-1][-1].segment:
            segments.append([])
        segments[-1].append(point)

    runtime = TrajectoryRuntime(
        config=dashboard,
        projected=projected,
        marker_path=paths.trajectory_markers[dashboard.id],
        points=tuple(points),
        segments=tuple(tuple(segment) for segment in segments),
        origin_x=origin_x,
        origin_y=origin_y,
        width_px=requested_width_px,
        height_px=height_px,
        scale_px_per_meter=scale,
    )
    details = {
        "id": dashboard.id,
        "type": dashboard.type,
        "source": "position",
        "status": "ACTIVE",
        "update_interval_ms": dashboard.update_interval_ms,
        "valid_point_count": len(trajectory.points),
        "segment_count": trajectory.segment_count,
        "position_break_count": len(trajectory.breaks),
        "position_breaks": [
            {
                "start_seconds": item.start_seconds,
                "end_seconds": item.end_seconds,
                "duration_seconds": item.duration_seconds,
                "reason": item.reason,
            }
            for item in trajectory.breaks
        ],
        "gap_action": "当前位置保持最后一个可靠位置，轨迹线段断开",
        "projection": projected.projection,
        "bounding_box_width_m": projected.width_m,
        "bounding_box_height_m": projected.height_m,
        "requested_width_ratio": dashboard.width,
        "scale_px_per_meter": scale,
        "render_rectangle": {
            "x": origin_x,
            "y": origin_y,
            "width": requested_width_px,
            "height": height_px,
        },
        "overflow_edges": overflow_edges,
        "marker_image": str(paths.trajectory_markers[dashboard.id]),
        "marker_scale": dashboard.marker_scale,
    }
    return runtime, details


def build_dashboard_runtimes(
    config: DashboardAppConfigLike,
    activity: ActivityData,
    clip: ClipRange,
    report: ReportLike | None = None,
    paths: RenderPathsLike | None = None,
) -> list[RuntimeDefinition]:
    runtimes: list[RuntimeDefinition] = []
    dashboard_reports: list[dict[str, Any]] = []
    if report is not None:
        report.details["dashboards"] = dashboard_reports
    frame_ms = 1000 / config.output.fps
    for dashboard in config.dashboards:
        if dashboard.update_interval_ms < frame_ms:
            LOGGER.warning(
                "仪表盘 %s 的刷新间隔 %dms 小于单帧 %.2fms，可见刷新率将受 FPS 限制",
                dashboard.id,
                dashboard.update_interval_ms,
                frame_ms,
            )
        if isinstance(dashboard, TrajectoryDashboardConfig):
            runtime, details = _build_trajectory_runtime(dashboard, config, activity, paths)
            dashboard_reports.append(details)
            if runtime is None:
                LOGGER.warning(
                    "轨迹仪表盘 %s 已跳过: 运动文件无法提供至少两个有效位置点",
                    dashboard.id,
                )
                continue
            clip_breaks = [
                item
                for item in activity.trajectory.breaks  # type: ignore[union-attr]
                if item.start_seconds < clip.end and item.end_seconds > clip.start
            ]
            if clip_breaks:
                LOGGER.warning(
                    "轨迹仪表盘 %s 在截取范围内有 %d 个位置断点，空洞期间箭头将保持最后位置",
                    dashboard.id,
                    len(clip_breaks),
                )
            runtimes.append(runtime)
            LOGGER.info("轨迹仪表盘 %s 可用: position", dashboard.id)
            continue
        series = activity.metrics.get(dashboard.source)
        if series is None:
            dashboard_reports.append(
                {
                    "id": dashboard.id,
                    "type": dashboard.type,
                    "source": dashboard.source.value,
                    "status": "SKIPPED",
                    "reason": "运动文件无法提供该指标",
                }
            )
            LOGGER.warning(
                "仪表盘 %s 已跳过: 运动文件无法提供 %s",
                dashboard.id,
                dashboard.source.value,
            )
            continue
        if dashboard.smoothing.method == SmoothingMethod.MOVING_AVERAGE:
            assert dashboard.smoothing.window_seconds is not None
            series = series.moving_average(dashboard.smoothing.window_seconds)
        first_in_clip = bisect.bisect_left(series.times, clip.start)
        last_in_clip = bisect.bisect_right(series.times, clip.end)
        has_clip_data = (
            first_in_clip < last_in_clip
            or series.value_at(clip.start) is not None
            or series.value_at(clip.end) is not None
        )
        if not has_clip_data:
            LOGGER.warning(
                "仪表盘 %s 的 %s 在截取范围内没有可用数据，将显示 -",
                dashboard.id,
                dashboard.source.value,
            )
        long_gap_details = series_gap_details(series, clip.start, clip.end)
        long_gaps = len(long_gap_details)
        gap_action = "保持最后一个有效值" if series.gap_strategy == GapStrategy.HOLD else "显示 -"
        if long_gaps:
            LOGGER.warning(
                "仪表盘 %s 在截取范围内有 %d 个超过 %.1fs 的数据空洞，空洞期间将%s",
                dashboard.id,
                long_gaps,
                series.interpolation_gap_seconds,
                gap_action,
            )
            for gap in long_gap_details:
                LOGGER.debug(
                    "数据空洞明细: dashboard=%s source=%s start=%.3fs end=%.3fs "
                    "duration=%.3fs strategy=%s",
                    dashboard.id,
                    dashboard.source.value,
                    gap["start_seconds"],
                    gap["end_seconds"],
                    gap["duration_seconds"],
                    gap_action,
                )
        offset = 0.0
        if (
            dashboard.source == MetricSource.DISTANCE
            and config.clip.cumulative_origin == CumulativeOrigin.CLIP_START
        ):
            value = series.value_at(clip.start, max_gap_seconds=None)
            offset = value or 0.0
        runtimes.append(DashboardRuntime(dashboard, series, offset))
        dashboard_reports.append(
            {
                "id": dashboard.id,
                "type": dashboard.type,
                "source": dashboard.source.value,
                "status": "ACTIVE" if has_clip_data else "NO_DATA_IN_CLIP",
                "unit": dashboard.unit,
                "sample_count": len(series.times),
                "smoothing": dashboard.smoothing.model_dump(mode="json"),
                "update_interval_ms": dashboard.update_interval_ms,
                "gap_strategy": series.gap_strategy.value,
                "long_gap_count": long_gaps,
                "long_gaps": long_gap_details,
                "gap_action": gap_action if long_gaps else None,
            }
        )
        LOGGER.info("仪表盘 %s 可用: %s", dashboard.id, dashboard.source.value)
    return runtimes


def convert_value(source: MetricSource, value: float, unit: str | None) -> float:
    if source in {MetricSource.SPEED, MetricSource.AVERAGE_SPEED}:
        if unit == "km/h":
            return value * 3.6
        if unit == "mph":
            return value * 3.6 * 0.621371192237334
        return value
    if source == MetricSource.DISTANCE:
        if unit == "km":
            return value / 1000
        if unit == "mi":
            return value * MILES_PER_METER
        return value
    if source in {MetricSource.ALTITUDE, MetricSource.TOTAL_ASCENT}:
        return value * 3.280839895013123 if unit == "ft" else value
    if source == MetricSource.TEMPERATURE:
        return value * 9 / 5 + 32 if unit == "F" else value
    if source == MetricSource.PRESSURE:
        if unit == "hPa":
            return value / 100
        if unit == "kPa":
            return value / 1000
        if unit == "mmHg":
            return value / 133.322368421
        return value
    return value


def format_value(value: float, precision: int, pad_zeros: bool) -> str:
    zero_threshold = 0.5 * 10 ** (-precision) if precision else 0.5
    if abs(value) < zero_threshold:
        value = 0.0
    rendered = f"{value:.{precision}f}"
    if not pad_zeros and "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def format_elapsed_time(value: float) -> str:
    total_seconds = max(0, math.floor(value + 1e-9))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_current_time(value: float) -> str:
    total_seconds = max(0, math.floor(value + 1e-9)) % (24 * 60 * 60)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_dashboard_value(config: DashboardConfig, value: float) -> str:
    if config.source == MetricSource.ELAPSED_TIME:
        return format_elapsed_time(value)
    if config.source == MetricSource.CURRENT_TIME:
        return format_current_time(value)
    converted = convert_value(config.source, value, config.unit)
    return format_value(converted, config.precision, config.pad_zeros)


def sample_dashboard_texts(
    runtimes: list[RuntimeDefinition], clip: ClipRange, time_seconds: float
) -> tuple[str | float | None, ...]:
    results: list[str | float | None] = []
    relative_ms = max(0.0, (time_seconds - clip.start) * 1000)
    for runtime in runtimes:
        interval = runtime.config.update_interval_ms
        display_time = clip.start + math.floor((relative_ms + 1e-7) / interval) * interval / 1000
        display_time = min(display_time, clip.end)
        if isinstance(runtime, TrajectoryRuntime):
            results.append(display_time)
            continue
        value = runtime.series.value_at(display_time)
        if value is None:
            results.append("-")
            continue
        value -= runtime.cumulative_offset
        results.append(format_dashboard_value(runtime.config, value))
    return tuple(results)


def rgba_color(value: str) -> tuple[int, int, int, int]:
    return ImageColor.getcolor(value, "RGBA")


def _interpolate_pixel_point(
    left: PixelTrajectoryPoint,
    right: PixelTrajectoryPoint,
    time_seconds: float,
) -> PixelTrajectoryPoint:
    ratio = (time_seconds - left.time_seconds) / (right.time_seconds - left.time_seconds)
    return PixelTrajectoryPoint(
        time_seconds=time_seconds,
        x=left.x + (right.x - left.x) * ratio,
        y=left.y + (right.y - left.y) * ratio,
        segment=left.segment,
    )


def trajectory_paths_at(
    runtime: TrajectoryRuntime,
    time_seconds: float,
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    completed: list[list[tuple[float, float]]] = []
    remaining: list[list[tuple[float, float]]] = []
    for segment in runtime.segments:
        if time_seconds < segment[0].time_seconds:
            remaining.append([(point.x, point.y) for point in segment])
            continue
        if time_seconds >= segment[-1].time_seconds:
            completed.append([(point.x, point.y) for point in segment])
            continue
        times = [point.time_seconds for point in segment]
        index = bisect.bisect_right(times, time_seconds) - 1
        left = segment[index]
        if math.isclose(left.time_seconds, time_seconds, abs_tol=1e-9):
            split = left
            completed_points = segment[: index + 1]
            remaining_points = segment[index:]
        else:
            split = _interpolate_pixel_point(left, segment[index + 1], time_seconds)
            completed_points = (*segment[: index + 1], split)
            remaining_points = (split, *segment[index + 1 :])
        completed.append([(point.x, point.y) for point in completed_points])
        remaining.append([(point.x, point.y) for point in remaining_points])
    return completed, remaining


def _draw_rounded_paths(
    size: tuple[int, int],
    paths: list[list[tuple[float, float]]],
    line_width: int,
) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    radius = line_width / 2
    for path in paths:
        if len(path) < 2:
            continue
        draw.line(path, fill=255, width=line_width, joint="curve")
        for x, y in (path[0], path[-1]):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return mask


def _colorize_mask(mask: Image.Image, color: str) -> Image.Image:
    red, green, blue, alpha = rgba_color(color)
    layer = Image.new("RGBA", mask.size, (red, green, blue, 0))
    layer.putalpha(mask.point(lambda value: value * alpha // 255))
    return layer


class FrameRenderer:
    def __init__(self, config: DashboardAppConfigLike, paths: RenderPathsLike) -> None:
        self.width = config.output.width
        self.height = config.output.height
        self.font_path = paths.font
        self.fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self.marker_images: dict[tuple[Path, float], Image.Image] = {}
        self.trajectory_layers: dict[str, tuple[float, Image.Image, tuple[int, int]]] = {}
        if config.output.background.mode == BackgroundMode.TRANSPARENT:
            self.base = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        else:
            self.base = Image.new(
                "RGBA",
                (self.width, self.height),
                rgba_color(config.output.background.chroma_key_color),
            )
        self.dashboard_background: Image.Image | None = None
        if paths.background_image is not None:
            try:
                image = Image.open(paths.background_image).convert("RGBA")
            except Exception as exc:
                raise ConfigError(f"无法读取仪表盘背景图片: {exc}") from exc
            expected_ratio = self.width / self.height
            actual_ratio = image.width / image.height
            if not math.isclose(expected_ratio, actual_ratio, rel_tol=0.001):
                raise ConfigError(
                    "仪表盘背景图片宽高比与输出分辨率不同: "
                    f"{image.width}x{image.height} vs {self.width}x{self.height}"
                )
            if image.size != (self.width, self.height):
                image = image.resize((self.width, self.height), Image.Resampling.LANCZOS)
            self.dashboard_background = image
        try:
            ImageFont.truetype(str(self.font_path), size=16)
        except Exception as exc:
            raise ConfigError(f"无法加载字体 {self.font_path.name}: {exc}") from exc

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(str(self.font_path), size=size)
        return self.fonts[size]

    def _marker(self, runtime: TrajectoryRuntime) -> Image.Image:
        key = (runtime.marker_path, runtime.config.marker_scale)
        if key not in self.marker_images:
            try:
                marker = Image.open(runtime.marker_path).convert("RGBA")
            except Exception as exc:
                raise ConfigError(
                    f"无法读取轨迹仪表盘 {runtime.config.id} 的当前位置图片: {exc}"
                ) from exc
            width = max(1, round(marker.width * runtime.config.marker_scale))
            height = max(1, round(marker.height * runtime.config.marker_scale))
            self.marker_images[key] = marker.resize((width, height), Image.Resampling.LANCZOS)
        return self.marker_images[key]

    def _trajectory_xy(
        self,
        runtime: TrajectoryRuntime,
        sample: TrajectorySample,
    ) -> tuple[float, float]:
        if runtime.projected.width_m < 1e-9:
            x = runtime.origin_x + runtime.width_px / 2
        else:
            x = runtime.origin_x + (sample.x_m - runtime.projected.min_x_m) * (
                runtime.scale_px_per_meter
            )
        if runtime.projected.height_m < 1e-9:
            y = runtime.origin_y + runtime.height_px / 2
        else:
            y = runtime.origin_y + (runtime.projected.max_y_m - sample.y_m) * (
                runtime.scale_px_per_meter
            )
        return x, y

    def _render_trajectory(
        self,
        frame: Image.Image,
        runtime: TrajectoryRuntime,
        time_seconds: float,
    ) -> None:
        cached = self.trajectory_layers.get(runtime.config.id)
        if cached is not None and math.isclose(cached[0], time_seconds, abs_tol=1e-9):
            frame.alpha_composite(cached[1], dest=cached[2])
            return

        sample = runtime.projected.sample_at(time_seconds)
        marker: Image.Image | None = None
        marker_center: tuple[float, float] | None = None
        if sample is not None:
            marker = self._marker(runtime).rotate(
                -sample.heading_degrees,
                resample=Image.Resampling.BICUBIC,
                expand=True,
            )
            marker_center = self._trajectory_xy(runtime, sample)

        marker_padding = (
            math.hypot(marker.width, marker.height) / 2 + 2 if marker is not None else 0
        )
        padding = max(runtime.config.line_width / 2 + 2, marker_padding)
        left = max(0, math.floor(runtime.origin_x - padding))
        top = max(0, math.floor(runtime.origin_y - padding))
        right = min(self.width, math.ceil(runtime.origin_x + runtime.width_px + padding))
        bottom = min(self.height, math.ceil(runtime.origin_y + runtime.height_px + padding))
        if right <= left or bottom <= top:
            return

        completed, remaining = trajectory_paths_at(runtime, time_seconds)
        local_completed = [[(x - left, y - top) for x, y in path] for path in completed]
        local_remaining = [[(x - left, y - top) for x, y in path] for path in remaining]
        layer_size = (right - left, bottom - top)
        remaining_mask = _draw_rounded_paths(layer_size, local_remaining, runtime.config.line_width)
        completed_mask = _draw_rounded_paths(layer_size, local_completed, runtime.config.line_width)
        layer = _colorize_mask(remaining_mask, runtime.config.remaining_color)
        layer.alpha_composite(_colorize_mask(completed_mask, runtime.config.completed_color))
        if marker is not None and marker_center is not None:
            marker_x = round(marker_center[0] - marker.width / 2 - left)
            marker_y = round(marker_center[1] - marker.height / 2 - top)
            source_left = max(0, -marker_x)
            source_top = max(0, -marker_y)
            source_right = min(marker.width, layer.width - marker_x)
            source_bottom = min(marker.height, layer.height - marker_y)
            if source_right > source_left and source_bottom > source_top:
                clipped_marker = marker.crop((source_left, source_top, source_right, source_bottom))
                layer.alpha_composite(
                    clipped_marker,
                    dest=(max(0, marker_x), max(0, marker_y)),
                )
        destination = (left, top)
        self.trajectory_layers[runtime.config.id] = (time_seconds, layer, destination)
        frame.alpha_composite(layer, dest=destination)

    def render(
        self,
        runtimes: list[RuntimeDefinition],
        texts: tuple[str | float | None, ...],
        bottom_image: Image.Image | None = None,
    ) -> Image.Image:
        frame = (
            bottom_image.convert("RGBA").copy() if bottom_image is not None else self.base.copy()
        )
        if frame.size != (self.width, self.height):
            frame = frame.resize((self.width, self.height), Image.Resampling.LANCZOS)
        if self.dashboard_background is not None:
            frame.alpha_composite(self.dashboard_background)
        draw = ImageDraw.Draw(frame)
        for runtime, text in zip(runtimes, texts, strict=True):
            if text is None:
                continue
            if isinstance(runtime, TrajectoryRuntime):
                assert isinstance(text, float)
                self._render_trajectory(frame, runtime, text)
                continue
            assert isinstance(text, str)
            dashboard = runtime.config
            xy = (dashboard.anchor.x * self.width, dashboard.anchor.y * self.height)
            draw.text(
                xy,
                text,
                font=self._font(dashboard.font_size),
                fill=rgba_color(dashboard.color),
                anchor=ALIGN_TO_PIL[dashboard.align],
                stroke_width=dashboard.stroke_width,
                stroke_fill=rgba_color(dashboard.stroke_color),
            )
        return frame
