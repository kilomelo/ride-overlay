"""Render dashboards onto a complete multi-clip source video with audio."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
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
from ride_overlay_video import VideoTimeline


class ExportConfigLike(Protocol):
    output: object
    timeline: object


class ExportPathsLike(Protocol):
    output: Path


class CancellationLike(Protocol):
    def is_set(self) -> bool: ...


class ExportError(Exception):
    """The composed-video export could not complete."""


class ExportCancelled(ExportError):
    """The user cancelled an in-progress export."""


def _temporary_output(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, value = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent
    )
    os.close(descriptor)
    return Path(value)


def _concat_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def _write_concat_file(timeline: VideoTimeline, directory: Path) -> Path:
    descriptor, value = tempfile.mkstemp(prefix=".ride-overlay-", suffix=".ffconcat", dir=directory)
    path = Path(value)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("ffconcat version 1.0\n")
            for segment in timeline.segments:
                handle.write(f"file {_concat_quote(segment.info.path)}\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


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
    offset_frames = int(config.timeline.activity_start_offset_frames)
    frame_count = math.ceil(video_timeline.duration_seconds * fps)
    full_activity = ClipRange(0.0, activity_duration_seconds)
    transparent_frame = Image.new("RGBA", (width, height), (0, 0, 0, 0)).tobytes("raw", "RGBA")

    temporary = _temporary_output(paths.output)
    concat_file = _write_concat_file(video_timeline, paths.output.parent)
    filter_graph = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps:g},format=rgba[base];"
        "[base][1:v]overlay=0:0:format=auto:shortest=1,format=yuv420p[outv]"
    )
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
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
    if has_audio:
        command.extend(["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{bitrate:g}M",
            "-movflags",
            "+faststart",
            "-shortest",
            str(temporary),
        ]
    )

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdin is not None and process.stderr is not None
        last_values: tuple[str | float | None, ...] | None = None
        last_frame: bytes | None = None
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
            process.stdin.write(frame_bytes)
            if progress is not None:
                progress(frame_index + 1, frame_count)
        process.stdin.close()
        error_output = process.stderr.read().decode("utf-8", errors="replace").strip()
        return_code = process.wait()
        if return_code:
            raise ExportError(f"FFmpeg 成片导出失败: {error_output or f'退出码 {return_code}'}")
        os.replace(temporary, paths.output)
    except BrokenPipeError as exc:
        detail = ""
        if process is not None and process.stderr is not None:
            detail = process.stderr.read().decode("utf-8", errors="replace").strip()
        raise ExportError(f"FFmpeg 提前结束: {detail or exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        concat_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)

    return {
        "path": str(paths.output),
        "frame_count": frame_count,
        "duration_seconds": video_timeline.duration_seconds,
        "width": width,
        "height": height,
        "fps": fps,
        "bitrate_mbps": bitrate,
        "audio_included": has_audio,
        "video_count": len(video_timeline.segments),
        "activity_start_offset_frames": offset_frames,
    }
