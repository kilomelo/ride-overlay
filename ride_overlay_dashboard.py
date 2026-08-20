"""Dashboard configuration, sampling, formatting, and Pillow rendering."""

from __future__ import annotations

import bisect
import logging
import math
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont
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
DEFAULT_TRAJECTORY_MARGIN_RATIO = 0.02
TRAJECTORY_SIMPLIFY_LINE_WIDTH_RATIO = 0.1
TRAJECTORY_MASK_SUPERSAMPLE = 4
MAX_SUPERSAMPLED_MASK_PIXELS = 40_000_000
TRAJECTORY_ANTIALIAS_EVENT_PADDING = 1.0


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


class TrajectoryOverlapBlendMode(StrEnum):
    UNIFORM = "uniform"
    ACCUMULATE = "accumulate"


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


def validate_optional_color(value: str | None) -> str | None:
    return None if value is None else validate_color(value)


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
    margin: Annotated[float, Field(ge=-0.25, le=0.25)] = (
        DEFAULT_TRAJECTORY_MARGIN_RATIO
    )
    anchor: AnchorConfig
    align: Align = Align.CENTER
    update_interval_ms: Annotated[int, Field(gt=0, le=3_600_000)] = 200
    line_width: Annotated[int, Field(gt=0, le=200)] = 8
    remaining_color: str = "#FFFFFF66"
    completed_color: str = "#00E676CC"
    overlap_blend_mode: TrajectoryOverlapBlendMode = TrajectoryOverlapBlendMode.UNIFORM
    background_color: str | None = None
    background_corner_radius: Annotated[int, Field(ge=0, le=4096)] = 0
    marker_image_file: str | None = None
    marker_scale: Annotated[float, Field(gt=0, le=100)] = 2.0

    _line_colors = field_validator("remaining_color", "completed_color")(validate_color)
    _normalize_background = field_validator("background_color", mode="before")(blank_to_none)
    _background_color = field_validator("background_color")(validate_optional_color)
    _normalize_marker = field_validator("marker_image_file", mode="before")(blank_to_none)


class HeartbeatDashboardConfig(StrictModel):
    type: Literal["heartbeat"]
    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    width: Annotated[float, Field(gt=0, le=1)]
    anchor: AnchorConfig
    align: Align = Align.CENTER
    heart_image_file: str | None = None

    _normalize_heart_image = field_validator("heart_image_file", mode="before")(blank_to_none)


DashboardDefinition = DashboardConfig | TrajectoryDashboardConfig | HeartbeatDashboardConfig


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


@dataclass(frozen=True)
class TrajectoryCoveragePlan:
    left: int
    top: int
    width: int
    height: int
    base_layer: Image.Image
    route_mask: Image.Image
    events: tuple[tuple[float, int], ...]


@dataclass
class TrajectoryCoverageState:
    counts: bytearray
    next_event_index: int = 0
    last_time_seconds: float = -math.inf

    def reset(self) -> None:
        self.counts[:] = bytes(len(self.counts))
        self.next_event_index = 0
        self.last_time_seconds = -math.inf


@dataclass(frozen=True)
class HeartbeatRuntime:
    config: HeartbeatDashboardConfig
    series: TimeSeries
    image_path: Path
    origin_x: float
    origin_y: float
    width_px: int
    height_px: int
    animation_start_seconds: float


@dataclass
class HeartbeatAnimationState:
    cycle_start_seconds: float | None = None
    cycle_period_seconds: float | None = None
    current_bpm: float | None = None
    last_time_seconds: float | None = None

    def reset(self) -> None:
        self.cycle_start_seconds = None
        self.cycle_period_seconds = None
        self.current_bpm = None
        self.last_time_seconds = None

    @staticmethod
    def _bpm_at(series: TimeSeries, time_seconds: float) -> float | None:
        value = series.value_at(time_seconds)
        return value if value is not None and math.isfinite(value) and value > 0 else None

    def opacity_at(
        self,
        series: TimeSeries,
        time_seconds: float,
        animation_start_seconds: float,
    ) -> float:
        if self.last_time_seconds is not None and time_seconds < self.last_time_seconds - 1e-9:
            self.reset()
        if self.cycle_start_seconds is None:
            self.cycle_start_seconds = animation_start_seconds
            self.current_bpm = self._bpm_at(series, animation_start_seconds)
            if self.current_bpm is not None:
                self.cycle_period_seconds = 60.0 / self.current_bpm

        if self.cycle_period_seconds is None:
            self.current_bpm = self._bpm_at(series, time_seconds)
            if self.current_bpm is None:
                self.last_time_seconds = time_seconds
                return 1.0
            self.cycle_start_seconds = time_seconds
            self.cycle_period_seconds = 60.0 / self.current_bpm

        assert self.cycle_start_seconds is not None
        assert self.cycle_period_seconds is not None
        while time_seconds >= self.cycle_start_seconds + self.cycle_period_seconds - 1e-9:
            boundary = self.cycle_start_seconds + self.cycle_period_seconds
            next_bpm = self._bpm_at(series, boundary)
            if next_bpm is not None:
                self.current_bpm = next_bpm
                self.cycle_period_seconds = 60.0 / next_bpm
            self.cycle_start_seconds = boundary

        phase = max(
            0.0,
            (time_seconds - self.cycle_start_seconds) / self.cycle_period_seconds,
        )
        opacity = 0.5 + 0.5 * math.cos(2 * math.pi * phase)
        self.last_time_seconds = time_seconds
        return min(1.0, max(0.0, opacity))


RuntimeDefinition = DashboardRuntime | TrajectoryRuntime | HeartbeatRuntime


class ClipConfigLike(Protocol):
    cumulative_origin: CumulativeOrigin


class DashboardAppConfigLike(Protocol):
    opacity: float
    output: OutputConfig
    clip: ClipConfigLike
    dashboards: list[DashboardDefinition]


class RenderPathsLike(Protocol):
    font: Path
    background_image: Path | None
    trajectory_markers: dict[str, Path]
    heartbeat_images: dict[str, Path]


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


def _point_to_line_distance(
    point: PixelTrajectoryPoint,
    start: PixelTrajectoryPoint,
    end: PixelTrajectoryPoint,
) -> float:
    delta_x = end.x - start.x
    delta_y = end.y - start.y
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= 1e-12:
        return math.hypot(point.x - start.x, point.y - start.y)
    ratio = min(
        1.0,
        max(
            0.0,
            ((point.x - start.x) * delta_x + (point.y - start.y) * delta_y)
            / length_squared,
        ),
    )
    closest_x = start.x + delta_x * ratio
    closest_y = start.y + delta_y * ratio
    return math.hypot(point.x - closest_x, point.y - closest_y)


def _simplify_trajectory_chunk(
    points: tuple[PixelTrajectoryPoint, ...],
    tolerance: float,
) -> tuple[PixelTrajectoryPoint, ...]:
    if len(points) <= 2:
        return points
    kept = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        start_index, end_index = pending.pop()
        furthest_index = -1
        furthest_distance = tolerance
        for index in range(start_index + 1, end_index):
            distance = _point_to_line_distance(
                points[index],
                points[start_index],
                points[end_index],
            )
            if distance > furthest_distance:
                furthest_index = index
                furthest_distance = distance
        if furthest_index >= 0:
            kept.add(furthest_index)
            pending.append((start_index, furthest_index))
            pending.append((furthest_index, end_index))
    return tuple(points[index] for index in sorted(kept))


def simplify_trajectory_segment(
    points: tuple[PixelTrajectoryPoint, ...],
    tolerance: float,
) -> tuple[PixelTrajectoryPoint, ...]:
    """Simplify a rendered centerline while retaining every direction reversal."""

    if len(points) <= 2 or tolerance <= 0:
        return points
    boundaries = [0]
    for index in range(1, len(points) - 1):
        previous = points[index - 1]
        current = points[index]
        following = points[index + 1]
        incoming_x = current.x - previous.x
        incoming_y = current.y - previous.y
        outgoing_x = following.x - current.x
        outgoing_y = following.y - current.y
        incoming_length = math.hypot(incoming_x, incoming_y)
        outgoing_length = math.hypot(outgoing_x, outgoing_y)
        if incoming_length <= 1e-9 or outgoing_length <= 1e-9:
            continue
        if incoming_x * outgoing_x + incoming_y * outgoing_y < 0:
            boundaries.append(index)
    boundaries.append(len(points) - 1)

    simplified: list[PixelTrajectoryPoint] = []
    for start_index, end_index in zip(boundaries, boundaries[1:], strict=False):
        chunk = _simplify_trajectory_chunk(
            points[start_index : end_index + 1],
            tolerance,
        )
        simplified.extend(chunk if not simplified else chunk[1:])
    return tuple(simplified)


def _trajectory_visual_radius(
    dashboard: TrajectoryDashboardConfig,
    marker_path: Path,
) -> tuple[float, tuple[int, int]]:
    try:
        with Image.open(marker_path) as marker:
            source_width, source_height = marker.size
    except Exception as exc:
        raise ConfigError(
            f"无法读取轨迹仪表盘 {dashboard.id} 的当前位置图片: {exc}"
        ) from exc
    marker_width = max(1, round(source_width * dashboard.marker_scale))
    marker_height = max(1, round(source_height * dashboard.marker_scale))
    marker_radius = math.hypot(marker_width, marker_height) / 2 + 2
    return max(dashboard.line_width / 2, marker_radius), (marker_width, marker_height)


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
    marker_path = paths.trajectory_markers[dashboard.id]
    visual_radius, marker_size = _trajectory_visual_radius(dashboard, marker_path)
    horizontal_margin = requested_width_px * dashboard.margin
    available_centerline_width = requested_width_px - 2 * (
        horizontal_margin + visual_radius
    )
    if available_centerline_width <= 0:
        raise ConfigError(
            f"轨迹仪表盘 {dashboard.id} 的 width 太小，无法容纳 line_width="
            f"{dashboard.line_width}、当前位置图片和 margin="
            f"{dashboard.margin:g}"
        )
    bbox_width_for_scale = max(projected.width_m, MIN_TRAJECTORY_BBOX_WIDTH_METERS)
    scale = available_centerline_width / bbox_width_for_scale
    centerline_width = projected.width_m * scale
    centerline_height = projected.height_m * scale
    height_px = max(
        1.0,
        (centerline_height + 2 * visual_radius)
        / (1 - 2 * dashboard.margin),
    )
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
    centerline_left = origin_x + (requested_width_px - centerline_width) / 2
    centerline_top = origin_y + (height_px - centerline_height) / 2
    for point in projected.points:
        if projected.width_m < 1e-9:
            x = origin_x + requested_width_px / 2
        else:
            x = centerline_left + (point.x_m - projected.min_x_m) * scale
        if projected.height_m < 1e-9:
            y = origin_y + height_px / 2
        else:
            y = centerline_top + (projected.max_y_m - point.y_m) * scale
        points.append(PixelTrajectoryPoint(point.time_seconds, x, y, point.segment))

    segments: list[list[PixelTrajectoryPoint]] = []
    for point in points:
        if not segments or point.segment != segments[-1][-1].segment:
            segments.append([])
        segments[-1].append(point)

    simplification_tolerance = max(
        0.5,
        dashboard.line_width * TRAJECTORY_SIMPLIFY_LINE_WIDTH_RATIO,
    )
    render_segments = tuple(
        simplify_trajectory_segment(tuple(segment), simplification_tolerance)
        for segment in segments
    )
    runtime = TrajectoryRuntime(
        config=dashboard,
        projected=projected,
        marker_path=marker_path,
        points=tuple(points),
        segments=render_segments,
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
        "render_point_count": sum(len(segment) for segment in render_segments),
        "simplification_tolerance_px": simplification_tolerance,
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
        "margin_ratio": dashboard.margin,
        "line_and_marker_included_in_render_rectangle": True,
        "visual_radius_px": visual_radius,
        "overlap_blend_mode": dashboard.overlap_blend_mode.value,
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
        "rendered_marker_size": {"width": marker_size[0], "height": marker_size[1]},
        "background_color": dashboard.background_color,
        "background_corner_radius": dashboard.background_corner_radius,
    }
    return runtime, details


def _build_heartbeat_runtime(
    dashboard: HeartbeatDashboardConfig,
    config: DashboardAppConfigLike,
    activity: ActivityData,
    clip: ClipRange,
    paths: RenderPathsLike | None,
) -> tuple[HeartbeatRuntime | None, dict[str, Any]]:
    series = activity.metrics.get(MetricSource.HEART_RATE)
    if series is None:
        return None, {
            "id": dashboard.id,
            "type": dashboard.type,
            "source": MetricSource.HEART_RATE.value,
            "status": "SKIPPED",
            "reason": "运动文件无法提供 heart_rate",
        }
    if paths is None or dashboard.id not in paths.heartbeat_images:
        raise ConfigError(f"心跳动画仪表盘 {dashboard.id} 缺少已解析的心脏图片")
    image_path = paths.heartbeat_images[dashboard.id]
    try:
        with Image.open(image_path) as image:
            source_width, source_height = image.size
    except Exception as exc:
        raise ConfigError(f"无法读取心跳动画仪表盘 {dashboard.id} 的心脏图片: {exc}") from exc
    if source_width <= 0 or source_height <= 0:
        raise ConfigError(f"心跳动画仪表盘 {dashboard.id} 的心脏图片尺寸无效")

    width_px = max(1, round(dashboard.width * config.output.width))
    height_px = max(1, round(width_px * source_height / source_width))
    origin_x, origin_y = aligned_origin(
        dashboard.anchor,
        dashboard.align,
        width_px,
        height_px,
        config.output.width,
        config.output.height,
    )
    overflow_edges: list[str] = []
    if origin_x < 0:
        overflow_edges.append("left")
    if origin_x + width_px > config.output.width:
        overflow_edges.append("right")
    if origin_y < 0:
        overflow_edges.append("top")
    if origin_y + height_px > config.output.height:
        overflow_edges.append("bottom")
    if overflow_edges:
        LOGGER.warning(
            "心跳动画仪表盘 %s 超出画面边界（%s），将保持配置尺寸，不自动缩小",
            dashboard.id,
            ", ".join(overflow_edges),
        )

    first_in_clip = bisect.bisect_left(series.times, clip.start)
    last_in_clip = bisect.bisect_right(series.times, clip.end)
    has_clip_data = (
        first_in_clip < last_in_clip
        or series.value_at(clip.start) is not None
        or series.value_at(clip.end) is not None
    )
    if not has_clip_data:
        LOGGER.warning(
            "心跳动画仪表盘 %s 在截取范围内没有可用心率；视频中将保持完全不透明，"
            "直到首次获得有效心率",
            dashboard.id,
        )
    long_gap_details = series_gap_details(series, clip.start, clip.end)
    if long_gap_details:
        LOGGER.warning(
            "心跳动画仪表盘 %s 在截取范围内有 %d 个心率数据空洞；空洞期间将沿用上一完整循环的频率",
            dashboard.id,
            len(long_gap_details),
        )

    runtime = HeartbeatRuntime(
        config=dashboard,
        series=series,
        image_path=image_path,
        origin_x=origin_x,
        origin_y=origin_y,
        width_px=width_px,
        height_px=height_px,
        animation_start_seconds=clip.start,
    )
    details = {
        "id": dashboard.id,
        "type": dashboard.type,
        "source": MetricSource.HEART_RATE.value,
        "status": "ACTIVE" if has_clip_data else "NO_DATA_IN_CLIP",
        "sample_count": len(series.times),
        "image": str(image_path),
        "source_image_size": {"width": source_width, "height": source_height},
        "requested_width_ratio": dashboard.width,
        "render_rectangle": {
            "x": origin_x,
            "y": origin_y,
            "width": width_px,
            "height": height_px,
        },
        "overflow_edges": overflow_edges,
        "animation": {
            "opacity_curve": "cosine: 1 -> 0 -> 1",
            "period_seconds": "60 / current_bpm",
            "frequency_update": "at_cycle_boundary",
            "phase_origin": "clip_start",
            "preview_opacity": 1.0,
        },
        "long_gap_count": len(long_gap_details),
        "long_gaps": long_gap_details,
        "gap_action": "沿用上一完整循环的频率；首次有效心率前保持完全不透明",
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
        update_interval_ms = getattr(dashboard, "update_interval_ms", None)
        if update_interval_ms is not None and update_interval_ms < frame_ms:
            LOGGER.warning(
                "仪表盘 %s 的刷新间隔 %dms 小于单帧 %.2fms，可见刷新率将受 FPS 限制",
                dashboard.id,
                update_interval_ms,
                frame_ms,
            )
        if isinstance(dashboard, HeartbeatDashboardConfig):
            runtime, details = _build_heartbeat_runtime(
                dashboard,
                config,
                activity,
                clip,
                paths,
            )
            dashboard_reports.append(details)
            if runtime is None:
                LOGGER.warning(
                    "心跳动画仪表盘 %s 已跳过: 运动文件无法提供 heart_rate",
                    dashboard.id,
                )
                continue
            runtimes.append(runtime)
            LOGGER.info("心跳动画仪表盘 %s 可用: heart_rate", dashboard.id)
            continue
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
        if isinstance(runtime, HeartbeatRuntime):
            results.append(min(max(time_seconds, clip.start), clip.end))
            continue
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
    pixel_count = max(1, size[0] * size[1])
    max_factor = max(1, math.floor(math.sqrt(MAX_SUPERSAMPLED_MASK_PIXELS / pixel_count)))
    factor = min(TRAJECTORY_MASK_SUPERSAMPLE, max_factor)
    high_resolution_size = (size[0] * factor, size[1] * factor)
    mask = Image.new("L", high_resolution_size, 0)
    draw = ImageDraw.Draw(mask)
    scaled_line_width = max(1, round(line_width * factor))
    radius = scaled_line_width / 2
    for path in paths:
        if len(path) < 2:
            continue
        scaled_path = [(x * factor, y * factor) for x, y in path]
        draw.line(scaled_path, fill=255, width=scaled_line_width, joint="curve")
        for x, y in (scaled_path[0], scaled_path[-1]):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    if factor == 1:
        return mask
    return mask.resize(size, Image.Resampling.LANCZOS)


def _colorize_mask(mask: Image.Image, color: str) -> Image.Image:
    red, green, blue, alpha = rgba_color(color)
    layer = Image.new("RGBA", mask.size, (red, green, blue, 0))
    layer.putalpha(mask.point(lambda value: value * alpha // 255))
    return layer


def _draw_rounded_rectangle_mask(
    size: tuple[int, int],
    box: tuple[float, float, float, float],
    radius: int,
) -> Image.Image:
    pixel_count = max(1, size[0] * size[1])
    max_factor = max(1, math.floor(math.sqrt(MAX_SUPERSAMPLED_MASK_PIXELS / pixel_count)))
    factor = min(TRAJECTORY_MASK_SUPERSAMPLE, max_factor)
    high_resolution_size = (size[0] * factor, size[1] * factor)
    mask = Image.new("L", high_resolution_size, 0)
    draw = ImageDraw.Draw(mask)
    scaled_box = tuple(value * factor for value in box)
    maximum_radius = max(0.0, min(box[2] - box[0], box[3] - box[1]) / 2)
    draw.rounded_rectangle(
        scaled_box,
        radius=min(radius, maximum_radius) * factor,
        fill=255,
    )
    if factor == 1:
        return mask
    return mask.resize(size, Image.Resampling.LANCZOS)


def _trajectory_background_layer(
    size: tuple[int, int],
    runtime: TrajectoryRuntime,
    left: int,
    top: int,
) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    if runtime.config.background_color is None:
        return layer
    box = (
        runtime.origin_x - left,
        runtime.origin_y - top,
        runtime.origin_x + runtime.width_px - left,
        runtime.origin_y + runtime.height_px - top,
    )
    mask = _draw_rounded_rectangle_mask(
        size,
        box,
        runtime.config.background_corner_radius,
    )
    return _colorize_mask(mask, runtime.config.background_color)


def _trajectory_visual_bounds(
    runtime: TrajectoryRuntime,
    frame_width: int,
    frame_height: int,
    padding: float,
) -> tuple[int, int, int, int]:
    """Return bounds containing the dashboard rectangle and overflowing visuals."""

    point_x = [point.x for point in runtime.points]
    point_y = [point.y for point in runtime.points]
    left = max(0, math.floor(min(runtime.origin_x, min(point_x) - padding)))
    top = max(0, math.floor(min(runtime.origin_y, min(point_y) - padding)))
    right = min(
        frame_width,
        math.ceil(
            max(runtime.origin_x + runtime.width_px, max(point_x) + padding)
        ),
    )
    bottom = min(
        frame_height,
        math.ceil(
            max(runtime.origin_y + runtime.height_px, max(point_y) + padding)
        ),
    )
    return left, top, right, bottom


def _capsule_pixel_intervals(
    size: tuple[int, int],
    start: tuple[float, float],
    end: tuple[float, float],
    start_time: float,
    end_time: float,
    line_width: int,
) -> list[tuple[int, float, float]]:
    """Rasterize an edge and return when its moving round cap covers each pixel."""

    width, height = size
    start_x, start_y = start
    end_x, end_y = end
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    length_squared = delta_x * delta_x + delta_y * delta_y
    # Include the antialiased fringe; the final completed alpha is still
    # clipped by route_mask, so this only prevents the base color leaking out
    # as a one-pixel halo around completed accumulated paths.
    radius = line_width / 2 + TRAJECTORY_ANTIALIAS_EVENT_PADDING
    radius_squared = radius * radius
    left = max(0, math.floor(min(start_x, end_x) - radius))
    top = max(0, math.floor(min(start_y, end_y) - radius))
    right = min(width, math.ceil(max(start_x, end_x) + radius + 1))
    bottom = min(height, math.ceil(max(start_y, end_y) + radius + 1))
    pixels: list[tuple[int, float, float]] = []
    for y in range(top, bottom):
        pixel_y = y + 0.5
        for x in range(left, right):
            pixel_x = x + 0.5
            if length_squared <= 1e-12:
                distance_squared = (pixel_x - start_x) ** 2 + (pixel_y - start_y) ** 2
                if distance_squared <= radius_squared:
                    pixels.append((y * width + x, start_time, end_time))
                continue

            offset_x = start_x - pixel_x
            offset_y = start_y - pixel_y
            linear = 2 * (offset_x * delta_x + offset_y * delta_y)
            constant = offset_x * offset_x + offset_y * offset_y - radius_squared
            discriminant = linear * linear - 4 * length_squared * constant
            if discriminant < 0:
                continue
            root = math.sqrt(max(0.0, discriminant))
            entry_ratio = max(0.0, (-linear - root) / (2 * length_squared))
            exit_ratio = min(1.0, (-linear + root) / (2 * length_squared))
            if entry_ratio > exit_ratio:
                continue
            entry_time = start_time + (end_time - start_time) * entry_ratio
            exit_time = start_time + (end_time - start_time) * exit_ratio
            pixels.append((y * width + x, entry_time, exit_time))
    return pixels


def build_trajectory_coverage_plan(
    runtime: TrajectoryRuntime,
    frame_width: int,
    frame_height: int,
) -> TrajectoryCoveragePlan | None:
    """Precompute temporal visits for pixels covered by the visible trajectory stroke."""

    started_at = time.perf_counter()
    left, top, right, bottom = _trajectory_visual_bounds(
        runtime,
        frame_width,
        frame_height,
        runtime.config.line_width / 2 + 2,
    )
    if right <= left or bottom <= top:
        return None
    size = (right - left, bottom - top)
    local_paths = [
        [(point.x - left, point.y - top) for point in segment]
        for segment in runtime.segments
    ]
    base_mask = _draw_rounded_paths(size, local_paths, runtime.config.line_width)
    base_layer = _trajectory_background_layer(size, runtime, left, top)
    base_layer.alpha_composite(_colorize_mask(base_mask, runtime.config.remaining_color))

    pixel_count = size[0] * size[1]
    route_coverage = base_mask.tobytes()
    last_seen_segments = [-1] * pixel_count
    last_exit_times = [-math.inf] * pixel_count
    visit_counts = bytearray(pixel_count)
    events: list[tuple[float, int]] = []
    for segment_number, segment in enumerate(runtime.segments):
        for start_point, end_point in zip(segment, segment[1:], strict=False):
            edge_pixels = _capsule_pixel_intervals(
                size,
                (start_point.x - left, start_point.y - top),
                (end_point.x - left, end_point.y - top),
                start_point.time_seconds,
                end_point.time_seconds,
                runtime.config.line_width,
            )
            new_events: list[tuple[float, int]] = []
            for pixel_index, entry_time, exit_time in edge_pixels:
                if route_coverage[pixel_index] == 0:
                    continue
                same_continuous_visit = (
                    last_seen_segments[pixel_index] == segment_number
                    and entry_time <= last_exit_times[pixel_index] + 1e-9
                )
                if not same_continuous_visit:
                    new_events.append((entry_time, pixel_index))
                    visit_counts[pixel_index] = min(
                        255, visit_counts[pixel_index] + 1
                    )
                last_seen_segments[pixel_index] = segment_number
                last_exit_times[pixel_index] = max(
                    last_exit_times[pixel_index], exit_time
                )
            new_events.sort(key=lambda item: item[0])
            events.extend(new_events)
    plan = TrajectoryCoveragePlan(
        left=left,
        top=top,
        width=size[0],
        height=size[1],
        base_layer=base_layer,
        route_mask=base_mask,
        events=tuple(events),
    )
    LOGGER.info(
        "轨迹仪表盘 %s 累计重叠预计算完成: region=%dx%d route_pixels=%d "
        "repeated_pixels=%d max_visits=%d events=%d elapsed=%.3fs",
        runtime.config.id,
        plan.width,
        plan.height,
        pixel_count - base_mask.histogram()[0],
        sum(count > 1 for count in visit_counts),
        max(visit_counts, default=0),
        len(plan.events),
        time.perf_counter() - started_at,
    )
    return plan


def render_accumulated_trajectory_layer(
    plan: TrajectoryCoveragePlan,
    state: TrajectoryCoverageState,
    time_seconds: float,
    completed_color: str,
) -> Image.Image:
    """Render the complete route base plus completed color for every distinct visit."""

    if time_seconds < state.last_time_seconds - 1e-9:
        state.reset()
    while state.next_event_index < len(plan.events):
        event_time, pixel_index = plan.events[state.next_event_index]
        if event_time > time_seconds + 1e-9:
            break
        state.counts[pixel_index] = min(255, state.counts[pixel_index] + 1)
        state.next_event_index += 1
    state.last_time_seconds = time_seconds

    red, green, blue, alpha = rgba_color(completed_color)
    opacity = alpha / 255
    alpha_lookup = [
        round(255 * (1 - (1 - opacity) ** visit_count))
        for visit_count in range(256)
    ]
    count_image = Image.frombytes("L", (plan.width, plan.height), bytes(state.counts))
    completed_alpha = ImageChops.multiply(
        count_image.point(alpha_lookup),
        plan.route_mask,
    )
    completed_layer = Image.new("RGBA", (plan.width, plan.height), (red, green, blue, 0))
    completed_layer.putalpha(completed_alpha)
    layer = plan.base_layer.copy()
    layer.alpha_composite(completed_layer)
    return layer


class FrameRenderer:
    def __init__(self, config: DashboardAppConfigLike, paths: RenderPathsLike) -> None:
        self.width = config.output.width
        self.height = config.output.height
        self.opacity = config.opacity
        self.font_path = paths.font
        self.fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self.marker_images: dict[tuple[Path, float], Image.Image] = {}
        self.trajectory_layers: dict[str, tuple[float, Image.Image, tuple[int, int]]] = {}
        self.trajectory_coverage_plans: dict[str, TrajectoryCoveragePlan] = {}
        self.trajectory_coverage_states: dict[str, TrajectoryCoverageState] = {}
        self.heart_images: dict[tuple[Path, int, int], Image.Image] = {}
        self.heartbeat_states: dict[str, HeartbeatAnimationState] = {}
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

    def _heart_image(self, runtime: HeartbeatRuntime) -> Image.Image:
        key = (runtime.image_path, runtime.width_px, runtime.height_px)
        if key not in self.heart_images:
            try:
                image = Image.open(runtime.image_path).convert("RGBA")
            except Exception as exc:
                raise ConfigError(
                    f"无法读取心跳动画仪表盘 {runtime.config.id} 的心脏图片: {exc}"
                ) from exc
            self.heart_images[key] = image.resize(
                (runtime.width_px, runtime.height_px),
                Image.Resampling.LANCZOS,
            )
        return self.heart_images[key]

    @staticmethod
    def _alpha_composite_clipped(
        frame: Image.Image,
        image: Image.Image,
        destination_x: int,
        destination_y: int,
    ) -> None:
        source_left = max(0, -destination_x)
        source_top = max(0, -destination_y)
        source_right = min(image.width, frame.width - destination_x)
        source_bottom = min(image.height, frame.height - destination_y)
        if source_right <= source_left or source_bottom <= source_top:
            return
        clipped = image.crop((source_left, source_top, source_right, source_bottom))
        frame.alpha_composite(
            clipped,
            dest=(max(0, destination_x), max(0, destination_y)),
        )

    def _render_heartbeat(
        self,
        frame: Image.Image,
        runtime: HeartbeatRuntime,
        time_seconds: float,
        *,
        preview: bool,
    ) -> None:
        opacity = 1.0
        if not preview:
            state = self.heartbeat_states.setdefault(
                runtime.config.id,
                HeartbeatAnimationState(),
            )
            opacity = state.opacity_at(
                runtime.series,
                time_seconds,
                runtime.animation_start_seconds,
            )
        if opacity <= 0:
            return
        image = self._heart_image(runtime)
        if opacity < 1:
            image = image.copy()
            image.putalpha(image.getchannel("A").point(lambda value: round(value * opacity)))
        self._alpha_composite_clipped(
            frame,
            image,
            round(runtime.origin_x),
            round(runtime.origin_y),
        )

    def _trajectory_xy(
        self,
        runtime: TrajectoryRuntime,
        sample: TrajectorySample,
    ) -> tuple[float, float]:
        if runtime.projected.width_m < 1e-9:
            x = runtime.origin_x + runtime.width_px / 2
        else:
            centerline_width = runtime.projected.width_m * runtime.scale_px_per_meter
            centerline_left = runtime.origin_x + (runtime.width_px - centerline_width) / 2
            x = centerline_left + (sample.x_m - runtime.projected.min_x_m) * (
                runtime.scale_px_per_meter
            )
        if runtime.projected.height_m < 1e-9:
            y = runtime.origin_y + runtime.height_px / 2
        else:
            centerline_height = runtime.projected.height_m * runtime.scale_px_per_meter
            centerline_top = runtime.origin_y + (runtime.height_px - centerline_height) / 2
            y = centerline_top + (runtime.projected.max_y_m - sample.y_m) * (
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

        if runtime.config.overlap_blend_mode == TrajectoryOverlapBlendMode.ACCUMULATE:
            plans = getattr(self, "trajectory_coverage_plans", None)
            if plans is None:
                plans = {}
                self.trajectory_coverage_plans = plans
            plan = plans.get(runtime.config.id)
            if plan is None:
                plan = build_trajectory_coverage_plan(runtime, self.width, self.height)
                if plan is None:
                    return
                plans[runtime.config.id] = plan

            states = getattr(self, "trajectory_coverage_states", None)
            if states is None:
                states = {}
                self.trajectory_coverage_states = states
            state = states.get(runtime.config.id)
            if state is None or len(state.counts) != plan.width * plan.height:
                state = TrajectoryCoverageState(bytearray(plan.width * plan.height))
                states[runtime.config.id] = state
            route_layer = render_accumulated_trajectory_layer(
                plan,
                state,
                time_seconds,
                runtime.config.completed_color,
            )
            rendered = self._compose_trajectory_layer(
                route_layer,
                (plan.left, plan.top),
                marker,
                marker_center,
            )
            if rendered is None:
                return
            layer, destination = rendered
            self.trajectory_layers[runtime.config.id] = (
                time_seconds,
                layer,
                destination,
            )
            frame.alpha_composite(layer, dest=destination)
            return

        marker_padding = (
            math.hypot(marker.width, marker.height) / 2 + 2 if marker is not None else 0
        )
        padding = max(runtime.config.line_width / 2 + 2, marker_padding)
        left, top, right, bottom = _trajectory_visual_bounds(
            runtime,
            self.width,
            self.height,
            padding,
        )
        if right <= left or bottom <= top:
            return

        completed, _remaining = trajectory_paths_at(runtime, time_seconds)
        full_route = [
            [(point.x, point.y) for point in segment]
            for segment in runtime.segments
        ]
        local_completed = [[(x - left, y - top) for x, y in path] for path in completed]
        local_full_route = [[(x - left, y - top) for x, y in path] for path in full_route]
        layer_size = (right - left, bottom - top)
        full_route_mask = _draw_rounded_paths(
            layer_size,
            local_full_route,
            runtime.config.line_width,
        )
        completed_mask = _draw_rounded_paths(layer_size, local_completed, runtime.config.line_width)
        layer = _trajectory_background_layer(layer_size, runtime, left, top)
        layer.alpha_composite(
            _colorize_mask(full_route_mask, runtime.config.remaining_color)
        )
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

    def _compose_trajectory_layer(
        self,
        route_layer: Image.Image,
        route_destination: tuple[int, int],
        marker: Image.Image | None,
        marker_center: tuple[float, float] | None,
    ) -> tuple[Image.Image, tuple[int, int]] | None:
        """Combine a clipped route layer and its marker into one cacheable image."""

        route_left, route_top = route_destination
        left = route_left
        top = route_top
        right = route_left + route_layer.width
        bottom = route_top + route_layer.height
        marker_left = marker_top = 0
        if marker is not None and marker_center is not None:
            marker_left = round(marker_center[0] - marker.width / 2)
            marker_top = round(marker_center[1] - marker.height / 2)
            left = min(left, marker_left)
            top = min(top, marker_top)
            right = max(right, marker_left + marker.width)
            bottom = max(bottom, marker_top + marker.height)

        left = max(0, left)
        top = max(0, top)
        right = min(self.width, right)
        bottom = min(self.height, bottom)
        if right <= left or bottom <= top:
            return None

        layer = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
        self._alpha_composite_clipped(
            layer,
            route_layer,
            route_left - left,
            route_top - top,
        )
        if marker is not None and marker_center is not None:
            self._alpha_composite_clipped(
                layer,
                marker,
                marker_left - left,
                marker_top - top,
            )
        return layer, (left, top)

    def dashboard_bounds(
        self,
        runtime: RuntimeDefinition,
        value: str | float | None,
    ) -> tuple[float, float, float, float] | None:
        """Return the current rendered rectangle in output-frame coordinates."""

        if value is None:
            return None
        if isinstance(runtime, (TrajectoryRuntime, HeartbeatRuntime)):
            return (
                runtime.origin_x,
                runtime.origin_y,
                runtime.origin_x + runtime.width_px,
                runtime.origin_y + runtime.height_px,
            )
        assert isinstance(value, str)
        dashboard = runtime.config
        anchor = (dashboard.anchor.x * self.width, dashboard.anchor.y * self.height)
        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        left, top, right, bottom = draw.textbbox(
            anchor,
            value,
            font=self._font(dashboard.font_size),
            anchor=ALIGN_TO_PIL[dashboard.align],
            stroke_width=dashboard.stroke_width,
        )
        return float(left), float(top), float(right), float(bottom)

    def dashboard_bounds_by_id(
        self,
        runtimes: list[RuntimeDefinition],
        values: tuple[str | float | None, ...],
    ) -> dict[str, tuple[float, float, float, float]]:
        bounds: dict[str, tuple[float, float, float, float]] = {}
        for runtime, value in zip(runtimes, values, strict=True):
            rectangle = self.dashboard_bounds(runtime, value)
            if rectangle is not None:
                bounds[runtime.config.id] = rectangle
        return bounds

    def _draw_dashboards(
        self,
        frame: Image.Image,
        runtimes: list[RuntimeDefinition],
        texts: tuple[str | float | None, ...],
        *,
        preview: bool,
    ) -> None:
        draw = ImageDraw.Draw(frame)
        # Earlier configuration entries are visually on top, matching editor hit priority.
        pairs = list(zip(runtimes, texts, strict=True))
        for runtime, text in reversed(pairs):
            if text is None:
                continue
            if isinstance(runtime, HeartbeatRuntime):
                assert isinstance(text, float)
                self._render_heartbeat(frame, runtime, text, preview=preview)
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

    def render_overlay(
        self,
        runtimes: list[RuntimeDefinition],
        texts: tuple[str | float | None, ...],
        *,
        preview: bool = False,
    ) -> Image.Image:
        """Render only dashboard content for compositing over source video."""

        frame = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        if self.dashboard_background is not None:
            frame.alpha_composite(self.dashboard_background)
        self._draw_dashboards(frame, runtimes, texts, preview=preview)
        if self.opacity < 1:
            alpha = frame.getchannel("A")
            frame.putalpha(alpha.point(lambda value: round(value * self.opacity)))
        return frame

    def render(
        self,
        runtimes: list[RuntimeDefinition],
        texts: tuple[str | float | None, ...],
        bottom_image: Image.Image | None = None,
        *,
        preview: bool = False,
    ) -> Image.Image:
        frame = (
            bottom_image.convert("RGBA").copy() if bottom_image is not None else self.base.copy()
        )
        if frame.size != (self.width, self.height):
            frame = frame.resize((self.width, self.height), Image.Resampling.LANCZOS)
        overlay = self.render_overlay(runtimes, texts, preview=preview)
        frame.alpha_composite(overlay)
        return frame
