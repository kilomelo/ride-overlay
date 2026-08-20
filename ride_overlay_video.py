"""Video discovery, metadata probing, and virtual multi-clip timelines."""

from __future__ import annotations

import bisect
import json
import math
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}


class VideoError(Exception):
    """A video input cannot be discovered or inspected."""


def natural_sort_key(path: Path) -> tuple[object, ...]:
    """Sort timestamp and numbered filenames predictably across platforms."""

    parts = re.split(r"(\d+)", path.name.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _resolve_project_video(project: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise VideoError(f"视频文件必须使用项目目录内的相对路径: {value}")
    candidate = (project / relative).resolve()
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise VideoError(f"视频文件不得指向项目目录外: {value}") from exc
    if not candidate.is_file():
        raise VideoError(f"找不到视频文件: {candidate}")
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        raise VideoError(f"不支持的视频格式: {candidate.name}")
    return candidate


def discover_video_files(
    project: Path,
    configured_files: list[str] | None = None,
    *,
    excluded: Iterable[Path] = (),
) -> tuple[Path, ...]:
    """Resolve an explicit playlist or discover project-root videos."""

    project = project.resolve()
    if configured_files:
        videos = tuple(_resolve_project_video(project, value) for value in configured_files)
        duplicates = sorted(
            {path.name for path in videos if videos.count(path) > 1},
            key=str.casefold,
        )
        if duplicates:
            raise VideoError(f"inputs.video_files 不得包含重复文件: {', '.join(duplicates)}")
        return videos

    excluded_paths = {path.resolve() for path in excluded}
    candidates = [
        path.resolve()
        for path in project.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and path.resolve() not in excluded_paths
        and not path.name.casefold().startswith("overlay.")
        and not path.name.casefold().startswith("rendered.")
    ]
    return tuple(sorted(candidates, key=natural_sort_key))


def _parse_rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: float | None
    has_audio: bool


def probe_video(path: Path) -> VideoInfo:
    """Read the metadata needed by playback and export using ffprobe."""

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise VideoError("找不到 ffprobe，请先安装 FFmpeg 并确保它位于 PATH")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise VideoError(f"无法执行 ffprobe: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise VideoError(f"读取视频信息失败 {path.name}: {detail or result.returncode}")
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        streams = payload["streams"]
        video_stream = next(item for item in streams if item.get("codec_type") == "video")
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except (KeyError, TypeError, ValueError, StopIteration, json.JSONDecodeError) as exc:
        raise VideoError(f"无法确定视频时长或分辨率: {path.name}") from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise VideoError(f"视频元数据无效: {path.name}")
    fps = _parse_rate(video_stream.get("avg_frame_rate")) or _parse_rate(
        video_stream.get("r_frame_rate")
    )
    return VideoInfo(
        path=path.resolve(),
        duration_seconds=duration,
        width=width,
        height=height,
        fps=fps,
        has_audio=any(item.get("codec_type") == "audio" for item in streams),
    )


@dataclass(frozen=True)
class VideoSegment:
    info: VideoInfo
    start_seconds: float
    end_seconds: float
    source_start_seconds: float = 0.0
    source_end_seconds: float | None = None

    @property
    def effective_source_end_seconds(self) -> float:
        return (
            self.info.duration_seconds
            if self.source_end_seconds is None
            else self.source_end_seconds
        )

    @property
    def duration_seconds(self) -> float:
        return self.effective_source_end_seconds - self.source_start_seconds


@dataclass(frozen=True)
class SegmentPosition:
    index: int
    segment: VideoSegment
    local_seconds: float


@dataclass(frozen=True)
class VideoTimeline:
    segments: tuple[VideoSegment, ...]
    duration_seconds: float

    @classmethod
    def from_paths(
        cls,
        paths: Iterable[Path],
        overlap_frames: Iterable[int] | None = None,
    ) -> VideoTimeline:
        path_list = list(paths)
        overlaps = list(overlap_frames or ())
        expected_overlap_count = max(0, len(path_list) - 1)
        if overlap_frames is None:
            overlaps = [0] * expected_overlap_count
        elif len(overlaps) != expected_overlap_count:
            raise VideoError(
                f"视频连接重叠帧数量应为 {expected_overlap_count} 项，实际为 {len(overlaps)} 项"
            )
        invalid_overlap = any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in overlaps
        )
        if invalid_overlap:
            raise VideoError("视频连接重叠帧数必须是非负整数")

        segments: list[VideoSegment] = []
        cursor = 0.0
        for index, path in enumerate(path_list):
            info = probe_video(path)
            overlap = overlaps[index] if index < len(overlaps) else 0
            if overlap and info.fps is None:
                raise VideoError(f"无法按帧裁切 {info.path.name}：视频帧率未知")
            overlap_seconds = overlap / info.fps if info.fps is not None else 0.0
            source_end = info.duration_seconds - overlap_seconds
            if source_end <= 0:
                raise VideoError(
                    f"{info.path.name} 的 overlap_frames={overlap} 超过或等于视频总时长"
                )
            end = cursor + source_end
            segments.append(
                VideoSegment(
                    info=info,
                    start_seconds=cursor,
                    end_seconds=end,
                    source_end_seconds=source_end,
                )
            )
            cursor = end
        return cls(tuple(segments), cursor)

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(segment.info.path for segment in self.segments)

    @property
    def join_times(self) -> tuple[float, ...]:
        return tuple(segment.start_seconds for segment in self.segments[1:])

    @property
    def overlap_frames(self) -> tuple[int, ...]:
        values: list[int] = []
        for segment in self.segments[:-1]:
            trimmed_seconds = segment.info.duration_seconds - segment.effective_source_end_seconds
            values.append(round(trimmed_seconds * (segment.info.fps or 0.0)))
        return tuple(values)

    def locate(self, global_seconds: float) -> SegmentPosition:
        if not self.segments:
            raise VideoError("视频时间轴为空")
        clamped = min(max(global_seconds, 0.0), self.duration_seconds)
        starts = [segment.start_seconds for segment in self.segments]
        index = min(bisect.bisect_right(starts, clamped) - 1, len(self.segments) - 1)
        segment = self.segments[index]
        local = min(
            max(clamped - segment.start_seconds + segment.source_start_seconds, 0.0),
            segment.effective_source_end_seconds,
        )
        return SegmentPosition(index=index, segment=segment, local_seconds=local)
