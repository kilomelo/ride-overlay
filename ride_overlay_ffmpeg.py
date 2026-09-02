"""Shared FFmpeg encoder selection and long-running process helpers."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache

from ride_overlay_dashboard import VideoCodec, VideoEncoder


class FFmpegCapabilityError(RuntimeError):
    """The requested FFmpeg capability is unavailable."""


@dataclass(frozen=True)
class VideoEncoderPlan:
    codec: VideoCodec
    name: str
    hardware_accelerated: bool
    arguments: tuple[str, ...]


# Backward-compatible public name used by earlier releases.
H264EncoderPlan = VideoEncoderPlan


@lru_cache(maxsize=4)
def _available_encoders(ffmpeg: str) -> str:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        check=False,
    )
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


@lru_cache(maxsize=8)
def _videotoolbox_usable(ffmpeg: str, codec: VideoCodec) -> bool:
    encoder_name = {
        VideoCodec.H264: "h264_videotoolbox",
        VideoCodec.H265: "hevc_videotoolbox",
    }[codec]
    if sys.platform != "darwin" or encoder_name not in _available_encoders(ffmpeg):
        return False
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:r=1:d=1",
                "-frames:v",
                "1",
                "-an",
                "-c:v",
                encoder_name,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def select_video_encoder(
    ffmpeg: str,
    codec: VideoCodec | str,
    configured: VideoEncoder | str,
    bitrate_mbps: float,
) -> VideoEncoderPlan:
    """Resolve an H.264/H.265 encoder, preferring VideoToolbox on supported Macs."""

    try:
        requested_codec = VideoCodec(codec)
    except ValueError as exc:
        raise FFmpegCapabilityError(f"不支持的视频编码格式: {codec}") from exc
    try:
        requested = VideoEncoder(configured)
    except ValueError as exc:
        raise FFmpegCapabilityError(f"不支持的视频编码器设置: {configured}") from exc

    hardware_name = {
        VideoCodec.H264: "h264_videotoolbox",
        VideoCodec.H265: "hevc_videotoolbox",
    }[requested_codec]
    software_name = {
        VideoCodec.H264: "libx264",
        VideoCodec.H265: "libx265",
    }[requested_codec]

    if requested in {VideoEncoder.AUTO, VideoEncoder.VIDEOTOOLBOX}:
        if _videotoolbox_usable(ffmpeg, requested_codec):
            arguments = [
                "-c:v",
                hardware_name,
                "-profile:v",
                "high" if requested_codec == VideoCodec.H264 else "main",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                f"{bitrate_mbps:g}M",
            ]
            if requested_codec == VideoCodec.H265:
                arguments.extend(["-tag:v", "hvc1"])
            return VideoEncoderPlan(
                codec=requested_codec,
                name=hardware_name,
                hardware_accelerated=True,
                arguments=tuple(arguments),
            )
        if requested == VideoEncoder.VIDEOTOOLBOX:
            raise FFmpegCapabilityError(
                f"当前设备/FFmpeg 无法使用 {hardware_name} 硬件编码器"
            )

    if software_name not in _available_encoders(ffmpeg):
        raise FFmpegCapabilityError(f"当前 FFmpeg 不包含 {software_name} 编码器")
    arguments = [
        "-c:v",
        software_name,
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{bitrate_mbps:g}M",
    ]
    if requested_codec == VideoCodec.H265:
        arguments.extend(["-tag:v", "hvc1"])
    return VideoEncoderPlan(
        codec=requested_codec,
        name=software_name,
        hardware_accelerated=False,
        arguments=tuple(arguments),
    )


def select_h264_encoder(
    ffmpeg: str,
    configured: VideoEncoder | str,
    bitrate_mbps: float,
) -> VideoEncoderPlan:
    """Backward-compatible H.264-only wrapper."""

    return select_video_encoder(ffmpeg, VideoCodec.H264, configured, bitrate_mbps)


def prevent_macos_sleep(process: subprocess.Popen[bytes]) -> subprocess.Popen[bytes] | None:
    """Keep macOS awake while the child process is alive; no-op elsewhere."""

    if sys.platform != "darwin":
        return None
    caffeinate = shutil.which("caffeinate")
    if caffeinate is None:
        return None
    try:
        return subprocess.Popen(
            [caffeinate, "-i", "-w", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
