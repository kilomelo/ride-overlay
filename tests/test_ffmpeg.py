from __future__ import annotations

import subprocess

import pytest

import ride_overlay_ffmpeg as ffmpeg_helpers
from ride_overlay_dashboard import VideoCodec, VideoEncoder
from ride_overlay_ffmpeg import (
    FFmpegCapabilityError,
    prevent_macos_sleep,
    select_h264_encoder,
    select_video_encoder,
)


def test_auto_encoder_prefers_usable_videotoolbox(monkeypatch) -> None:
    monkeypatch.setattr(
        ffmpeg_helpers, "_videotoolbox_usable", lambda _ffmpeg, _codec: True
    )

    plan = select_h264_encoder("ffmpeg", VideoEncoder.AUTO, 16)

    assert plan.name == "h264_videotoolbox"
    assert plan.hardware_accelerated is True
    assert "16M" in plan.arguments


def test_software_encoder_uses_libx264(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_helpers, "_available_encoders", lambda _ffmpeg: "libx264")

    plan = select_h264_encoder("ffmpeg", VideoEncoder.SOFTWARE, 12)

    assert plan.name == "libx264"
    assert plan.hardware_accelerated is False
    assert "12M" in plan.arguments


def test_forced_videotoolbox_fails_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        ffmpeg_helpers, "_videotoolbox_usable", lambda _ffmpeg, _codec: False
    )

    with pytest.raises(FFmpegCapabilityError, match="h264_videotoolbox"):
        select_h264_encoder("ffmpeg", VideoEncoder.VIDEOTOOLBOX, 16)


def test_h265_videotoolbox_uses_hevc_and_hvc1_tag(monkeypatch) -> None:
    monkeypatch.setattr(
        ffmpeg_helpers, "_videotoolbox_usable", lambda _ffmpeg, _codec: True
    )

    plan = select_video_encoder("ffmpeg", VideoCodec.H265, VideoEncoder.AUTO, 12)

    assert plan.codec == VideoCodec.H265
    assert plan.name == "hevc_videotoolbox"
    assert plan.hardware_accelerated is True
    assert "12M" in plan.arguments
    assert "hvc1" in plan.arguments


def test_h265_software_encoder_uses_libx265(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_helpers, "_available_encoders", lambda _ffmpeg: "libx265")

    plan = select_video_encoder("ffmpeg", "h265", "software", 8)

    assert plan.name == "libx265"
    assert plan.hardware_accelerated is False
    assert "hvc1" in plan.arguments


def test_macos_sleep_prevention_tracks_export_process(monkeypatch) -> None:
    launched: list[tuple[list[str], object, object]] = []
    marker = object()

    def fake_popen(command, *, stdout, stderr):
        launched.append((command, stdout, stderr))
        return marker

    monkeypatch.setattr(ffmpeg_helpers.sys, "platform", "darwin")
    monkeypatch.setattr(ffmpeg_helpers.shutil, "which", lambda _name: "/usr/bin/caffeinate")
    monkeypatch.setattr(ffmpeg_helpers.subprocess, "Popen", fake_popen)

    result = prevent_macos_sleep(type("Process", (), {"pid": 1234})())

    assert result is marker
    assert launched == [
        (["/usr/bin/caffeinate", "-i", "-w", "1234"], subprocess.DEVNULL, subprocess.DEVNULL)
    ]
