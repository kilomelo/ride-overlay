"""Render dashboards onto a complete multi-clip source video with audio."""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from PIL import Image

from ride_overlay_dashboard import (
    ClipRange,
    FrameRenderer,
    RuntimeDefinition,
    sample_dashboard_texts,
)
from ride_overlay_ffmpeg import (
    FFmpegCapabilityError,
    prevent_macos_sleep,
    select_video_encoder,
)
from ride_overlay_video import VideoTimeline

LOGGER = logging.getLogger("ride-overlay")


class ExportConfigLike(Protocol):
    output: object
    timeline: object


class ExportPathsLike(Protocol):
    output: Path


class CancellationLike(Protocol):
    def is_set(self) -> bool: ...


class ExportError(Exception):
    """The composed-video export could not complete."""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ExportCancelled(ExportError):
    """The user cancelled an in-progress export."""


def timeline_frame_count(timeline: VideoTimeline, fps: float) -> int:
    """Match FFmpeg's per-segment nearest-frame rounding without duration ceil drift."""

    return sum(
        max(1, math.floor(segment.duration_seconds * fps + 0.5))
        for segment in timeline.segments
    )


def _temporary_output(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, value = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent
    )
    os.close(descriptor)
    return Path(value)


def _check_export_inputs(timeline: VideoTimeline) -> tuple[str, bool]:
    if not timeline.segments:
        raise ExportError("项目目录中没有可用于成片导出的视频")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ExportError("找不到 ffmpeg，请先安装 FFmpeg 并确保它位于 PATH")
    audio_states = {segment.info.has_audio for segment in timeline.segments}
    if len(audio_states) > 1:
        raise ExportError("部分视频包含音频、部分视频没有音频，暂时无法可靠拼接音轨")
    return ffmpeg, audio_states == {True}


def export_composed_video(
    config: ExportConfigLike,
    paths: ExportPathsLike,
    activity_duration_seconds: float,
    runtimes: list[RuntimeDefinition],
    renderer: FrameRenderer,
    video_timeline: VideoTimeline,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancellation: CancellationLike | None = None,
) -> dict[str, object]:
    """Export the whole virtual video timeline with dashboards and source audio."""

    ffmpeg, has_audio = _check_export_inputs(video_timeline)
    output = config.output
    width = int(output.width)
    height = int(output.height)
    fps = float(output.fps)
    bitrate = float(output.bitrate_mbps)
    try:
        encoder = select_video_encoder(
            ffmpeg,
            getattr(output, "video_codec", "h264"),
            getattr(output, "video_encoder", getattr(output, "h264_encoder", "auto")),
            bitrate,
        )
    except FFmpegCapabilityError as exc:
        raise ExportError(str(exc)) from exc
    offset_frames = int(config.timeline.activity_start_offset_frames)
    frame_count = timeline_frame_count(video_timeline, fps)
    full_activity = ClipRange(0.0, activity_duration_seconds)
    transparent_frame = Image.new("RGBA", (width, height), (0, 0, 0, 0)).tobytes("raw", "RGBA")
    temporary = _temporary_output(paths.output)
    filter_parts: list[str] = []
    for index, segment in enumerate(video_timeline.segments):
        source_start = segment.source_start_seconds
        source_end = segment.effective_source_end_seconds
        filter_parts.append(
            f"[{index}:v]trim=start={source_start:.9f}:end={source_end:.9f},"
            "setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps:g},format=yuv420p[v{index}]"
        )
        if has_audio:
            filter_parts.append(
                f"[{index}:a]atrim=start={source_start:.9f}:end={source_end:.9f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
    if len(video_timeline.segments) == 1:
        filter_parts.append("[v0]null[base]")
        if has_audio:
            filter_parts.append("[a0]anull[outa]")
    elif has_audio:
        concat_inputs = "".join(
            f"[v{index}][a{index}]" for index in range(len(video_timeline.segments))
        )
        filter_parts.append(
            f"{concat_inputs}concat=n={len(video_timeline.segments)}:v=1:a=1[base][outa]"
        )
    else:
        concat_inputs = "".join(
            f"[v{index}]" for index in range(len(video_timeline.segments))
        )
        filter_parts.append(
            f"{concat_inputs}concat=n={len(video_timeline.segments)}:v=1:a=0[base]"
        )
    dashboard_input_index = len(video_timeline.segments)
    filter_parts.append(
        f"[base][{dashboard_input_index}:v]"
        "overlay=0:0:format=yuv420:shortest=1[outv]"
    )
    filter_graph = ";".join(filter_parts)
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    for segment in video_timeline.segments:
        command.extend(["-i", str(segment.info.path)])
    command.extend(
        [
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgba",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            f"{fps:g}",
            "-i",
            "pipe:0",
            "-filter_complex",
            filter_graph,
            "-map",
            "[outv]",
        ]
    )
    if has_audio:
        command.extend(["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"])
    command.extend(encoder.arguments)
    command.extend(["-movflags", "+faststart", "-shortest", str(temporary)])

    process: subprocess.Popen[bytes] | None = None
    sleep_preventer: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    frames_submitted = 0
    pipe_error: BrokenPipeError | None = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        sleep_preventer = prevent_macos_sleep(process)
        assert process.stdin is not None and process.stderr is not None
        last_values: tuple[str | float | None, ...] | None = None
        last_frame: bytes | None = None
        progress_interval = max(1, round(fps / 10))
        for frame_index in range(frame_count):
            if cancellation is not None and cancellation.is_set():
                raise ExportCancelled("导出已取消")
            video_time = frame_index / fps
            activity_time = video_time - offset_frames / fps
            if 0.0 <= activity_time <= activity_duration_seconds:
                values = sample_dashboard_texts(runtimes, full_activity, activity_time)
                if values != last_values or last_frame is None:
                    last_frame = renderer.render_overlay(runtimes, values).tobytes("raw", "RGBA")
                    last_values = values
                frame_bytes = last_frame
            else:
                frame_bytes = transparent_frame
                last_values = None
                last_frame = None
            try:
                process.stdin.write(frame_bytes)
            except BrokenPipeError as exc:
                pipe_error = exc
                break
            frames_submitted += 1
            if progress is not None and (
                (frame_index + 1) % progress_interval == 0 or frame_index + 1 == frame_count
            ):
                progress(frame_index + 1, frame_count)
        try:
            process.stdin.close()
        except BrokenPipeError as exc:
            pipe_error = pipe_error or exc
        error_output = process.stderr.read().decode("utf-8", errors="replace").strip()
        return_code = process.wait()
        if return_code:
            return_code_text = (
                f"被信号 {-return_code} 终止" if return_code < 0 else f"退出码 {return_code}"
            )
            message = f"FFmpeg 成片导出失败（{return_code_text}）"
            if error_output:
                message += f": {error_output}"
            elif pipe_error is not None:
                message += f": {pipe_error}"
            raise ExportError(
                message,
                details={
                    "encoder": encoder.name,
                    "hardware_encoding": encoder.hardware_accelerated,
                    "ffmpeg_return_code": return_code,
                    "ffmpeg_stderr": error_output,
                    "pipe_error": str(pipe_error) if pipe_error is not None else None,
                    "planned_frame_count": frame_count,
                    "frames_submitted": frames_submitted,
                    "remaining_frames": frame_count - frames_submitted,
                    "elapsed_seconds": time.monotonic() - started,
                    "temporary_output_size_bytes": (
                        temporary.stat().st_size if temporary.exists() else None
                    ),
                    "available_disk_bytes": shutil.disk_usage(temporary.parent).free,
                    "ffmpeg_command": command,
                },
            )
        remaining_frames = frame_count - frames_submitted
        pipe_tolerance_frames = max(2, math.ceil(fps * 0.1))
        if pipe_error is not None and remaining_frames > pipe_tolerance_frames:
            raise ExportError(
                "FFmpeg 过早关闭了仪表盘输入管道"
                f"（已提交 {frames_submitted}/{frame_count} 帧，退出码 0）",
                details={
                    "encoder": encoder.name,
                    "hardware_encoding": encoder.hardware_accelerated,
                    "ffmpeg_return_code": return_code,
                    "ffmpeg_stderr": error_output,
                    "pipe_error": str(pipe_error),
                    "planned_frame_count": frame_count,
                    "frames_submitted": frames_submitted,
                    "remaining_frames": remaining_frames,
                    "pipe_tolerance_frames": pipe_tolerance_frames,
                    "elapsed_seconds": time.monotonic() - started,
                    "temporary_output_size_bytes": (
                        temporary.stat().st_size if temporary.exists() else None
                    ),
                    "available_disk_bytes": shutil.disk_usage(temporary.parent).free,
                    "ffmpeg_command": command,
                },
            )
        if pipe_error is not None:
            LOGGER.info(
                "FFmpeg 在时间轴尾部提前关闭输入管道，容许 %d 帧时长舍入差异"
                "（已提交 %d/%d 帧）",
                remaining_frames,
                frames_submitted,
                frame_count,
            )
        os.replace(temporary, paths.output)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if sleep_preventer is not None and sleep_preventer.poll() is None:
            sleep_preventer.terminate()
            sleep_preventer.wait()
        temporary.unlink(missing_ok=True)

    elapsed_seconds = time.monotonic() - started
    return {
        "path": str(paths.output),
        "frame_count": frame_count,
        "frames_submitted": frames_submitted,
        "pipe_closed_at_timeline_end": pipe_error is not None,
        "duration_seconds": video_timeline.duration_seconds,
        "width": width,
        "height": height,
        "fps": fps,
        "bitrate_mbps": bitrate,
        "video_codec": encoder.codec.value,
        "encoder": encoder.name,
        "hardware_encoding": encoder.hardware_accelerated,
        "sleep_prevention": sleep_preventer is not None,
        "export_elapsed_seconds": elapsed_seconds,
        "export_speed_ratio": (
            video_timeline.duration_seconds / elapsed_seconds if elapsed_seconds > 0 else None
        ),
        "audio_included": has_audio,
        "video_count": len(video_timeline.segments),
        "video_join_overlap_frames": list(video_timeline.overlap_frames),
        "activity_start_offset_frames": offset_frames,
    }
