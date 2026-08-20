#!/usr/bin/env python3
"""Render FIT/GPX activity metrics as a dashboard overlay video."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from PIL import Image
from pydantic import Field, ValidationError, field_validator, model_validator

# Re-export the established public surface while implementation lives in focused modules.
from ride_overlay_dashboard import (
    Align,
    AnchorConfig,
    BackgroundConfig,
    BackgroundMode,
    ClipRange,
    ConfigError,
    CumulativeOrigin,
    DashboardConfig,
    DashboardDefinition,
    DashboardRuntime,
    FrameRenderer,
    HeartbeatAnimationState,
    HeartbeatDashboardConfig,
    HeartbeatRuntime,
    OutputConfig,
    SmoothingConfig,
    SmoothingMethod,
    StrictModel,
    TrajectoryDashboardConfig,
    TrajectoryOverlapBlendMode,
    TrajectoryRuntime,
    blank_to_none,
    build_dashboard_runtimes,
    convert_value,
    format_current_time,
    format_dashboard_value,
    format_elapsed_time,
    format_value,
    sample_dashboard_texts,
)
from ride_overlay_data import (
    ActivityData,
    ActivityError,
    GapStrategy,
    MetricSource,
    ProjectedTrajectory,
    RawPoint,
    RideOverlayError,
    TimeSeries,
    TrajectoryData,
    TrajectoryPoint,
    TrajectorySample,
    activity_details,
    build_activity,
    build_trajectory,
    project_trajectory,
    read_activity,
    read_fit,
    read_gpx,
    series_gap_details,
)
from ride_overlay_video import (
    VideoError,
    discover_video_files,
    probe_video,
)

_blank_to_none = blank_to_none

__all__ = [
    "ActivityData",
    "ActivityError",
    "Align",
    "AnchorConfig",
    "AppConfig",
    "BackgroundConfig",
    "BackgroundMode",
    "ClipRange",
    "ConfigError",
    "CumulativeOrigin",
    "DashboardConfig",
    "DashboardDefinition",
    "DashboardRuntime",
    "FrameRenderer",
    "GapStrategy",
    "HeartbeatAnimationState",
    "HeartbeatDashboardConfig",
    "HeartbeatRuntime",
    "MetricSource",
    "OutputConfig",
    "RawPoint",
    "ProjectedTrajectory",
    "RideOverlayError",
    "SmoothingConfig",
    "SmoothingMethod",
    "TimeSeries",
    "TimelineConfig",
    "ToolError",
    "TrajectoryDashboardConfig",
    "TrajectoryData",
    "TrajectoryOverlapBlendMode",
    "TrajectoryPoint",
    "TrajectoryRuntime",
    "TrajectorySample",
    "VideoJoinConfig",
    "activity_details",
    "build_activity",
    "build_dashboard_runtimes",
    "build_trajectory",
    "convert_value",
    "format_current_time",
    "format_dashboard_value",
    "format_elapsed_time",
    "format_value",
    "main",
    "project_trajectory",
    "read_activity",
    "read_fit",
    "read_gpx",
    "resolve_clip",
    "resolve_paths",
    "sample_dashboard_texts",
    "series_gap_details",
]

LOGGER = logging.getLogger("ride-overlay")
CONFIG_FILENAME = "config.json"
EXPORT_DIRNAME = "export"
PREVIEW_FILENAME = "preview.png"
RESULT_LOG_FILENAME = "result.log"
APP_VERSION = "0.2.0"
ACTIVITY_EXTENSIONS = {".fit", ".gpx"}
FONT_EXTENSIONS = {".otf", ".ttf", ".ttc"}
DEFAULT_ARROW_RELATIVE_PATH = Path("assets/images/arrow.png")
DEFAULT_HEART_RELATIVE_PATH = Path("assets/images/heart.png")


class ToolError(RideOverlayError):
    """An external video tool failed."""


@dataclass
class StageResult:
    name: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    status: str = "RUNNING"


@dataclass
class LogEvent:
    timestamp: datetime
    level: str
    message: str


@dataclass
class RunReport:
    mode: str
    project: Path
    command: list[str]
    run_id: str = dataclass_field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: datetime = dataclass_field(default_factory=lambda: datetime.now().astimezone())
    started_monotonic: float = dataclass_field(default_factory=time.perf_counter)
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    status: str = "RUNNING"
    exit_code: int | None = None
    result_path: Path | None = None
    stages: list[StageResult] = dataclass_field(default_factory=list)
    events: list[LogEvent] = dataclass_field(default_factory=list)
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    @contextmanager
    def stage(self, name: str):
        stage = StageResult(name=name, started_at=datetime.now().astimezone())
        self.stages.append(stage)
        started = time.perf_counter()
        LOGGER.info("阶段开始: %s", name)
        try:
            yield
        except BaseException:
            stage.status = "FAILED"
            LOGGER.error("阶段失败: %s", name)
            raise
        else:
            stage.status = "SUCCESS"
        finally:
            stage.finished_at = datetime.now().astimezone()
            stage.duration_seconds = time.perf_counter() - started
            if stage.status == "SUCCESS":
                LOGGER.info("阶段完成: %s，耗时 %.3fs", name, stage.duration_seconds)

    def finish(self, status: str, exit_code: int) -> None:
        self.status = status
        self.exit_code = exit_code
        self.finished_at = datetime.now().astimezone()
        self.duration_seconds = time.perf_counter() - self.started_monotonic

    def render(self) -> str:
        warning_count = sum(event.level == "WARNING" for event in self.events)
        error_count = sum(event.level in {"ERROR", "CRITICAL"} for event in self.events)
        finished_at = self.finished_at or datetime.now().astimezone()
        duration = self.duration_seconds or (time.perf_counter() - self.started_monotonic)
        lines = [
            "ride-overlay 工作结果日志",
            "=" * 72,
            f"run_id: {self.run_id}",
            f"version: {APP_VERSION}",
            f"status: {self.status}",
            f"exit_code: {self.exit_code if self.exit_code is not None else '-'}",
            f"mode: {self.mode}",
            f"started_at: {self.started_at.isoformat(timespec='milliseconds')}",
            f"finished_at: {finished_at.isoformat(timespec='milliseconds')}",
            f"duration_seconds: {duration:.3f}",
            f"warning_count: {warning_count}",
            f"error_count: {error_count}",
            f"project: {self.project}",
            f"command: {shlex.join(self.command)}",
            f"python: {sys.version.split()[0]}",
            f"platform: {platform.platform()}",
            "",
            "[任务阶段]",
        ]
        if self.stages:
            for index, stage in enumerate(self.stages, start=1):
                finished = (
                    stage.finished_at.isoformat(timespec="milliseconds")
                    if stage.finished_at
                    else "-"
                )
                stage_duration = (
                    f"{stage.duration_seconds:.3f}s" if stage.duration_seconds is not None else "-"
                )
                lines.append(
                    f"{index:02d}. {stage.name} | {stage.status} | "
                    f"start={stage.started_at.isoformat(timespec='milliseconds')} | "
                    f"end={finished} | duration={stage_duration}"
                )
        else:
            lines.append("（尚未进入处理阶段）")

        notable_events = [
            event for event in self.events if event.level in {"WARNING", "ERROR", "CRITICAL"}
        ]
        lines.extend(["", "[警告与错误摘要]"])
        if notable_events:
            for event in notable_events:
                message = event.message.replace("\n", "\n    ")
                lines.append(
                    f"{event.timestamp.isoformat(timespec='milliseconds')} "
                    f"{event.level:<8} {message}"
                )
        else:
            lines.append("（无警告或错误）")

        lines.extend(["", "[任务数据]", json.dumps(self.details, ensure_ascii=False, indent=2)])
        lines.extend(["", "[流程与异常事件]"])
        if self.events:
            for event in self.events:
                message = event.message.replace("\n", "\n    ")
                lines.append(
                    f"{event.timestamp.isoformat(timespec='milliseconds')} "
                    f"{event.level:<8} {message}"
                )
        else:
            lines.append("（无事件）")
        lines.append("")
        return "\n".join(lines)

    def write(self, path: Path | None = None) -> Path:
        target = (path or self.result_path or (self.project / RESULT_LOG_FILENAME)).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_output(target)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.render())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self.result_path = target
        return target


class RunReportHandler(logging.Handler):
    def __init__(self, report: RunReport) -> None:
        super().__init__(level=logging.DEBUG)
        self.report = report

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.report.events.append(
            LogEvent(
                timestamp=datetime.fromtimestamp(record.created).astimezone(),
                level=record.levelname,
                message=message,
            )
        )


class InputsConfig(StrictModel):
    activity_file: str | None = None
    font_file: str | None = None
    background_image_file: str | None = None
    video_files: list[str] | None = None

    _normalize_blanks = field_validator(
        "activity_file", "font_file", "background_image_file", mode="before"
    )(_blank_to_none)

    @field_validator("video_files", mode="before")
    @classmethod
    def normalize_video_files(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, list):
            normalized = [item.strip() if isinstance(item, str) else item for item in value]
            return [item for item in normalized if item != ""] or None
        return value


class ClipConfig(StrictModel):
    start_seconds: Annotated[float, Field(ge=0)] | None = None
    end_seconds: Annotated[float, Field(ge=0)] | None = None
    cumulative_origin: CumulativeOrigin = CumulativeOrigin.ACTIVITY_START


class VideoJoinConfig(StrictModel):
    previous_file: Annotated[str, Field(min_length=1)]
    next_file: Annotated[str, Field(min_length=1)]
    overlap_frames: Annotated[int, Field(ge=0)] = 0

    _normalize_files = field_validator("previous_file", "next_file", mode="before")(
        lambda value: value.strip() if isinstance(value, str) else value
    )


class TimelineConfig(StrictModel):
    activity_start_offset_frames: int = 0
    video_joins: list[VideoJoinConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_video_joins(self) -> TimelineConfig:
        pairs = [(item.previous_file, item.next_file) for item in self.video_joins]
        duplicates = sorted({pair for pair in pairs if pairs.count(pair) > 1})
        if duplicates:
            formatted = ", ".join(f"{previous} -> {next_}" for previous, next_ in duplicates)
            raise ValueError(f"timeline.video_joins 不得包含重复连接: {formatted}")
        return self


class AppConfig(StrictModel):
    schema_version: Literal[1, 2]
    opacity: Annotated[float, Field(ge=0, le=1)] = 1.0
    inputs: InputsConfig = Field(default_factory=InputsConfig)
    clip: ClipConfig = Field(default_factory=ClipConfig)
    timeline: TimelineConfig = Field(default_factory=TimelineConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    dashboards: Annotated[list[DashboardDefinition], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_dashboard_ids(self) -> AppConfig:
        ids = [dashboard.id for dashboard in self.dashboards]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"dashboard id 不得重复: {', '.join(duplicates)}")
        return self


@dataclass(frozen=True)
class ResolvedPaths:
    project: Path
    export_dir: Path
    activity: Path
    font: Path
    background_image: Path | None
    output: Path
    preview: Path
    videos: tuple[Path, ...] = ()
    trajectory_markers: dict[str, Path] = dataclass_field(default_factory=dict)
    heartbeat_images: dict[str, Path] = dataclass_field(default_factory=dict)


def load_config(project: Path) -> AppConfig:
    config_path = project / CONFIG_FILENAME
    if not config_path.is_file():
        raise ConfigError(f"项目目录中缺少 {CONFIG_FILENAME}: {config_path}")
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{CONFIG_FILENAME} 不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取 {config_path}: {exc}") from exc
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        lines = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(item) for item in error["loc"])
            lines.append(f"  - {location or '<root>'}: {error['msg']}")
        raise ConfigError("配置校验失败:\n" + "\n".join(lines)) from exc


def file_details(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime)
        .astimezone()
        .isoformat(timespec="milliseconds"),
    }


def _safe_project_file(project: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ConfigError(f"{label} 必须是项目目录内的相对路径: {value}")
    candidate = (project / relative).resolve()
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise ConfigError(f"{label} 不得指向项目目录外: {value}") from exc
    if not candidate.is_file():
        raise ConfigError(f"找不到{label}: {candidate}")
    return candidate


def _discover_first(project: Path, extensions: set[str], label: str) -> Path:
    candidates = sorted(
        (
            path
            for path in project.iterdir()
            if path.is_file() and path.suffix.lower() in extensions
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )
    if not candidates:
        expected = ", ".join(sorted(extensions))
        raise ConfigError(f"项目目录中没有可用的{label}（支持 {expected}）")
    if len(candidates) > 1:
        LOGGER.warning(
            "找到 %d 个%s，按文件名选择第一个: %s",
            len(candidates),
            label,
            candidates[0].name,
        )
    return candidates[0].resolve()


def _default_image_asset(relative_path: Path, label: str) -> Path:
    candidates = (
        Path(__file__).resolve().parent / relative_path,
        Path(sys.prefix) / "share" / "ride-overlay" / relative_path,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise ConfigError(f"找不到默认{label} {relative_path.name}；已检查: {searched}")


def resolve_paths(project_dir: Path, config: AppConfig) -> ResolvedPaths:
    project = project_dir.expanduser().resolve()
    if not project.is_dir():
        raise ConfigError(f"项目目录不存在或不是目录: {project}")
    export_dir = project / EXPORT_DIRNAME
    export_dir.mkdir(parents=True, exist_ok=True)

    activity = (
        _safe_project_file(project, config.inputs.activity_file, "运动数据文件")
        if config.inputs.activity_file
        else _discover_first(project, ACTIVITY_EXTENSIONS, "运动数据文件")
    )
    if activity.suffix.lower() not in ACTIVITY_EXTENSIONS:
        raise ConfigError(f"不支持的运动数据格式: {activity.suffix}")

    font = (
        _safe_project_file(project, config.inputs.font_file, "字体文件")
        if config.inputs.font_file
        else _discover_first(project, FONT_EXTENSIONS, "字体文件")
    )
    if font.suffix.lower() not in FONT_EXTENSIONS:
        raise ConfigError(f"不支持的字体格式: {font.suffix}")

    background_image = None
    if config.inputs.background_image_file:
        background_image = _safe_project_file(
            project, config.inputs.background_image_file, "仪表盘背景图片"
        )
        if background_image.suffix.lower() != ".png":
            raise ConfigError("仪表盘背景图片目前只支持 PNG")

    trajectory_markers: dict[str, Path] = {}
    for dashboard in config.dashboards:
        if not isinstance(dashboard, TrajectoryDashboardConfig):
            continue
        marker = (
            _safe_project_file(
                project,
                dashboard.marker_image_file,
                f"轨迹仪表盘 {dashboard.id} 的当前位置图片",
            )
            if dashboard.marker_image_file
            else _default_image_asset(DEFAULT_ARROW_RELATIVE_PATH, "轨迹箭头图片")
        )
        if marker.suffix.lower() != ".png":
            raise ConfigError(f"轨迹仪表盘 {dashboard.id} 的当前位置图片目前只支持 PNG")
        trajectory_markers[dashboard.id] = marker

    heartbeat_images: dict[str, Path] = {}
    for dashboard in config.dashboards:
        if not isinstance(dashboard, HeartbeatDashboardConfig):
            continue
        heart_image = (
            _safe_project_file(
                project,
                dashboard.heart_image_file,
                f"心跳动画仪表盘 {dashboard.id} 的心脏图片",
            )
            if dashboard.heart_image_file
            else _default_image_asset(DEFAULT_HEART_RELATIVE_PATH, "心脏图片")
        )
        if heart_image.suffix.lower() != ".png":
            raise ConfigError(f"心跳动画仪表盘 {dashboard.id} 的心脏图片目前只支持 PNG")
        heartbeat_images[dashboard.id] = heart_image

    default_name = (
        "overlay.mov"
        if config.output.background.mode == BackgroundMode.TRANSPARENT
        else "overlay.mp4"
    )
    output_value = config.output.filename or default_name
    output_relative = Path(output_value)
    if output_relative.is_absolute():
        raise ConfigError("output.filename 必须是项目目录内的相对路径")
    output = (export_dir / output_relative).resolve()
    try:
        output.relative_to(export_dir)
    except ValueError as exc:
        raise ConfigError("output.filename 不得指向 export 目录外") from exc
    output.parent.mkdir(parents=True, exist_ok=True)

    expected_suffix = (
        ".mov" if config.output.background.mode == BackgroundMode.TRANSPARENT else ".mp4"
    )
    if output.suffix.lower() != expected_suffix:
        raise ConfigError(
            f"{config.output.background.mode.value} 模式必须输出 {expected_suffix} 文件，"
            f"当前为 {output.name}"
        )
    if output in (
        activity,
        font,
        background_image,
        *trajectory_markers.values(),
        *heartbeat_images.values(),
    ):
        raise ConfigError("输出文件不得覆盖输入文件")

    try:
        videos = discover_video_files(
            project,
            config.inputs.video_files,
            excluded=(
                output,
                project / output_relative,
                project / PREVIEW_FILENAME,
                export_dir / PREVIEW_FILENAME,
            ),
        )
    except VideoError as exc:
        raise ConfigError(str(exc)) from exc

    return ResolvedPaths(
        project=project,
        export_dir=export_dir,
        activity=activity,
        font=font,
        background_image=background_image,
        output=output,
        preview=export_dir / PREVIEW_FILENAME,
        videos=videos,
        trajectory_markers=trajectory_markers,
        heartbeat_images=heartbeat_images,
    )


def resolve_clip(config: ClipConfig, duration: float) -> ClipRange:
    start = config.start_seconds if config.start_seconds is not None else 0.0
    end = config.end_seconds if config.end_seconds is not None else duration
    tolerance = 1e-6
    if start > duration + tolerance:
        raise ConfigError(f"clip.start_seconds={start} 超过运动总时长 {duration:.3f}")
    if end > duration + tolerance:
        raise ConfigError(f"clip.end_seconds={end} 超过运动总时长 {duration:.3f}")
    start = min(start, duration)
    end = min(end, duration)
    if end <= start:
        raise ConfigError("正式渲染和预览都要求 clip.end_seconds 大于 start_seconds")
    return ClipRange(start, end)


def _run_checked(command: list[str], description: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:
        raise ToolError(f"无法执行 {description}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"{description}失败: {detail or f'退出码 {result.returncode}'}")
    return result


def find_preview_video(paths: ResolvedPaths) -> Path | None:
    if not paths.videos:
        return None
    if len(paths.videos) > 1:
        LOGGER.warning(
            "找到 %d 个预览视频，使用视频时间轴中的第一段: %s",
            len(paths.videos),
            paths.videos[0].name,
        )
    return paths.videos[0]


def probe_video_duration(video: Path) -> float:
    try:
        return probe_video(video).duration_seconds
    except VideoError as exc:
        raise ToolError(str(exc)) from exc


def extract_video_frame(video: Path, time_seconds: float, width: int, height: int) -> Image.Image:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ToolError("找不到 ffmpeg，请先安装 FFmpeg 并确保它位于 PATH")
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    )
    result = _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{time_seconds:.6f}",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            video_filter,
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        f"提取视频预览帧 {video.name}",
    )
    try:
        import io

        with Image.open(io.BytesIO(result.stdout)) as image:
            return image.convert("RGBA")
    except Exception as exc:
        raise ToolError(f"FFmpeg 未返回有效的预览图片: {video.name}") from exc


def _temporary_output(target: Path) -> Path:
    descriptor, value = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent
    )
    os.close(descriptor)
    return Path(value)


def save_image_atomic(image: Image.Image, target: Path) -> None:
    temporary = _temporary_output(target)
    try:
        image.save(temporary, format="PNG")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def render_preview(
    config: AppConfig,
    paths: ResolvedPaths,
    clip: ClipRange,
    runtimes: list[DashboardRuntime | TrajectoryRuntime | HeartbeatRuntime],
    renderer: FrameRenderer,
) -> dict[str, Any]:
    preview_video = find_preview_video(paths)
    bottom_image = None
    source_details: dict[str, Any] | None = None
    if preview_video is not None:
        video_duration = probe_video_duration(preview_video)
        video_time = video_duration / 2
        data_time = clip.start + video_time
        if data_time > clip.end:
            LOGGER.warning(
                "预览视频中点 %.3fs 超出运动截取范围，运动数据时刻已限制到 %.3fs",
                video_time,
                clip.end,
            )
            data_time = clip.end
        bottom_image = extract_video_frame(
            preview_video, video_time, config.output.width, config.output.height
        )
        LOGGER.info(
            "预览底图来自 %s 的 %.3fs，运动数据时刻 %.3fs",
            preview_video.name,
            video_time,
            data_time,
        )
        source_details = {
            **file_details(preview_video),
            "duration_seconds": video_duration,
            "sample_time_seconds": video_time,
            "activity_time_seconds": data_time,
        }
    else:
        data_time = (clip.start + clip.end) / 2
        LOGGER.info("未找到视频，预览使用运动数据中点 %.3fs", data_time)
    texts = sample_dashboard_texts(runtimes, clip, data_time)
    image = renderer.render(runtimes, texts, bottom_image=bottom_image, preview=True)
    save_image_atomic(image, paths.preview)
    LOGGER.info("预览图片已生成: %s", paths.preview)
    return {
        "artifact": file_details(paths.preview),
        "preview_video": source_details,
        "activity_time_seconds": data_time,
        "rendered_dashboard_count": len(runtimes),
    }


def check_encoder(mode: BackgroundMode) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ToolError("找不到 ffmpeg，请先安装 FFmpeg 并确保它位于 PATH")
    result = _run_checked([ffmpeg, "-hide_banner", "-encoders"], "检查 FFmpeg 编码器")
    encoders = result.stdout.decode("utf-8", errors="replace")
    required = "prores_ks" if mode == BackgroundMode.TRANSPARENT else "libx264"
    if required not in encoders:
        raise ToolError(f"当前 FFmpeg 不包含所需编码器: {required}")
    return ffmpeg


def render_video(
    config: AppConfig,
    paths: ResolvedPaths,
    clip: ClipRange,
    runtimes: list[DashboardRuntime | TrajectoryRuntime | HeartbeatRuntime],
    renderer: FrameRenderer,
) -> dict[str, Any]:
    ffmpeg = check_encoder(config.output.background.mode)
    temporary = _temporary_output(paths.output)
    fps = config.output.fps
    frame_count = math.ceil(clip.duration * fps)
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgba",
        "-video_size",
        f"{config.output.width}x{config.output.height}",
        "-framerate",
        f"{fps:g}",
        "-i",
        "pipe:0",
        "-an",
    ]
    if config.output.background.mode == BackgroundMode.TRANSPARENT:
        command.extend(
            [
                "-c:v",
                "prores_ks",
                "-profile:v",
                "4444",
                "-alpha_bits",
                "16",
                "-pix_fmt",
                "yuva444p10le",
            ]
        )
    else:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                f"{config.output.bitrate_mbps:g}M",
                "-movflags",
                "+faststart",
            ]
        )
    command.append(str(temporary))

    LOGGER.info(
        "开始渲染 %d 帧（%.3f 秒，%g FPS）到 %s",
        frame_count,
        clip.duration,
        fps,
        paths.output.name,
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdin is not None and process.stderr is not None
        last_texts: tuple[str | float | None, ...] | None = None
        last_frame: bytes | None = None
        progress_interval = max(1, round(fps * 10))
        for frame_index in range(frame_count):
            time_seconds = clip.start + frame_index / fps
            texts = sample_dashboard_texts(runtimes, clip, time_seconds)
            if texts != last_texts or last_frame is None:
                last_frame = renderer.render(runtimes, texts).tobytes("raw", "RGBA")
                last_texts = texts
            process.stdin.write(last_frame)
            if frame_index and frame_index % progress_interval == 0:
                LOGGER.info("渲染进度 %.1f%%", frame_index / frame_count * 100)
        process.stdin.close()
        error_output = process.stderr.read().decode("utf-8", errors="replace").strip()
        return_code = process.wait()
        if return_code:
            raise ToolError(f"FFmpeg 视频编码失败: {error_output or f'退出码 {return_code}'}")
        os.replace(temporary, paths.output)
    except BrokenPipeError as exc:
        detail = ""
        if process is not None and process.stderr is not None:
            detail = process.stderr.read().decode("utf-8", errors="replace").strip()
        raise ToolError(f"FFmpeg 提前结束: {detail or exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        temporary.unlink(missing_ok=True)
    LOGGER.info("视频已生成: %s", paths.output)
    return {
        "artifact": file_details(paths.output),
        "encoder": (
            "prores_ks"
            if config.output.background.mode == BackgroundMode.TRANSPARENT
            else "libx264"
        ),
        "frame_count": frame_count,
        "duration_seconds": clip.duration,
        "fps": fps,
        "width": config.output.width,
        "height": config.output.height,
        "rendered_dashboard_count": len(runtimes),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ride-overlay",
        description="将 FIT/GPX 运动数据渲染为仪表盘叠加视频",
    )
    parser.add_argument("project_dir", type=Path, help="包含 config.json 和素材的项目目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="只生成静态预览图片")
    mode.add_argument("--editor", action="store_true", help="打开图形界面编辑器")
    parser.add_argument("--verbose", action="store_true", help="输出更详细的日志")
    return parser


def configure_logging(report: RunReport, verbose: bool) -> None:
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    LOGGER.addHandler(console)

    report_handler = RunReportHandler(report)
    report_handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(report_handler)


def run(args: argparse.Namespace, report: RunReport) -> None:
    project = args.project_dir.expanduser().resolve()
    with report.stage("读取并校验配置"):
        if not project.is_dir():
            raise ConfigError(f"项目目录不存在或不是目录: {project}")
        config = load_config(project)
        report.details["config"] = config.model_dump(mode="json")

    with report.stage("解析输入与输出路径"):
        paths = resolve_paths(project, config)
        report.result_path = paths.export_dir / RESULT_LOG_FILENAME
        report.details["inputs"] = {
            "activity": file_details(paths.activity),
            "font": file_details(paths.font),
            "background_image": (
                file_details(paths.background_image) if paths.background_image else None
            ),
            "trajectory_marker_images": {
                dashboard_id: file_details(path)
                for dashboard_id, path in paths.trajectory_markers.items()
            },
            "heartbeat_images": {
                dashboard_id: file_details(path)
                for dashboard_id, path in paths.heartbeat_images.items()
            },
            "videos": [file_details(path) for path in paths.videos],
        }
        report.details["output_plan"] = {
            "video": str(paths.output),
            "preview": str(paths.preview),
            "result_log": str(report.result_path),
            "background_mode": config.output.background.mode.value,
            "resolution": f"{config.output.width}x{config.output.height}",
            "fps": config.output.fps,
            "bitrate_mbps": config.output.bitrate_mbps,
        }
    LOGGER.info("运动文件: %s", paths.activity.name)
    LOGGER.info("字体文件: %s", paths.font.name)
    if paths.background_image:
        LOGGER.info("仪表盘背景: %s", paths.background_image.name)

    with report.stage("读取并处理运动数据"):
        activity = read_activity(paths.activity)
        report.details["activity"] = activity_details(activity)

    with report.stage("解析截取范围并检查仪表盘"):
        clip = resolve_clip(config.clip, activity.duration_seconds)
        report.details["clip"] = {
            "start_seconds": clip.start,
            "end_seconds": clip.end,
            "duration_seconds": clip.duration,
        }
        LOGGER.info("运动数据范围: %.3fs - %.3fs", clip.start, clip.end)
        runtimes = build_dashboard_runtimes(config, activity, clip, report=report, paths=paths)
        report.details["dashboard_summary"] = {
            "configured_count": len(config.dashboards),
            "active_count": len(runtimes),
            "skipped_count": len(config.dashboards) - len(runtimes),
        }

    with report.stage("初始化画面渲染器"):
        renderer = FrameRenderer(config, paths)

    if args.preview:
        with report.stage("生成静态预览"):
            report.details["result"] = render_preview(config, paths, clip, runtimes, renderer)
    else:
        with report.stage("渲染并编码视频"):
            report.details["result"] = render_video(config, paths, clip, runtimes, renderer)


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.editor:
        try:
            from ride_overlay_gui.main import run_editor
        except ImportError as exc:
            parser.error("图形界面依赖尚未安装，请执行: python -m pip install -e '.[gui]'")
            raise AssertionError from exc
        return run_editor(args.project_dir)
    command_args = list(argv) if argv is not None else sys.argv[1:]
    project = args.project_dir.expanduser().resolve()
    report = RunReport(
        mode="preview" if args.preview else "video",
        project=project,
        command=[sys.argv[0], *command_args],
    )
    configure_logging(report, args.verbose)
    exit_code = 0
    status = "SUCCESS"
    try:
        run(args, report)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        exit_code = 2
        status = "FAILED"
    except ActivityError as exc:
        LOGGER.error("%s", exc)
        exit_code = 3
        status = "FAILED"
    except ToolError as exc:
        LOGGER.error("%s", exc)
        exit_code = 4
        status = "FAILED"
    except KeyboardInterrupt:
        LOGGER.error("操作已取消")
        exit_code = 130
        status = "CANCELLED"
    except Exception:
        LOGGER.exception("发生未处理异常")
        exit_code = 1
        status = "FAILED"
    finally:
        report.finish(status, exit_code)
        LOGGER.info(
            "任务结束: status=%s exit_code=%d duration=%.3fs",
            status,
            exit_code,
            report.duration_seconds,
        )
        if project.is_dir():
            target = report.result_path or (project / EXPORT_DIRNAME / RESULT_LOG_FILENAME)
            LOGGER.info("工作结果日志: %s", target)
            try:
                report.write(target)
            except OSError as exc:
                LOGGER.error("无法写入工作结果日志 %s: %s", target, exc)
                if exit_code == 0:
                    exit_code = 4
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
