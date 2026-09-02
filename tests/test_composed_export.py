from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import ride_overlay_export as export_module
from ride_overlay_export import ExportError, export_composed_video, timeline_frame_count
from ride_overlay_video import VideoInfo, VideoSegment, VideoTimeline, probe_video


class MarkerRenderer:
    def render_overlay(self, *_args, **_kwargs) -> Image.Image:
        image = Image.new("RGBA", (160, 90), (0, 0, 0, 0))
        for x in range(70, 91):
            for y in range(35, 56):
                image.putpixel((x, y), (0, 255, 0, 255))
        return image


def test_timeline_frame_count_uses_per_segment_nearest_rounding() -> None:
    durations = (900.0, 900.0, 900.0, 900.0, 900.0, 119.266667)
    segments = []
    cursor = 0.0
    for index, duration in enumerate(durations):
        info = VideoInfo(Path(f"{index}.mp4"), duration, 2560, 1440, 30.0, True)
        segments.append(VideoSegment(info, cursor, cursor + duration))
        cursor += duration
    timeline = VideoTimeline(tuple(segments), cursor)

    assert timeline.duration_seconds * 30 == pytest.approx(138578.00001)
    assert timeline_frame_count(timeline, 30) == 138578


class _FakePipe:
    def __init__(self, successful_writes: int) -> None:
        self.successful_writes = successful_writes
        self.writes = 0

    def write(self, _data: bytes) -> int:
        if self.writes >= self.successful_writes:
            raise BrokenPipeError(32, "Broken pipe")
        self.writes += 1
        return 1

    def close(self) -> None:
        pass


class _FakeStderr:
    def read(self) -> bytes:
        return b""


class _FakeFFmpegProcess:
    def __init__(self, successful_writes: int) -> None:
        self.stdin = _FakePipe(successful_writes)
        self.stderr = _FakeStderr()

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


def _mock_export_process(monkeypatch, successful_writes: int) -> None:
    process = _FakeFFmpegProcess(successful_writes)
    monkeypatch.setattr(export_module, "_check_export_inputs", lambda _timeline: ("ffmpeg", False))
    monkeypatch.setattr(
        export_module,
        "select_video_encoder",
        lambda *_args: SimpleNamespace(
            codec=SimpleNamespace(value="h264"),
            name="libx264",
            hardware_accelerated=False,
            arguments=(),
        ),
    )
    monkeypatch.setattr(export_module, "prevent_macos_sleep", lambda _process: None)
    monkeypatch.setattr(export_module.subprocess, "Popen", lambda *_args, **_kwargs: process)


def _fake_timeline(duration_seconds: float) -> VideoTimeline:
    info = VideoInfo(Path("source.mp4"), duration_seconds, 160, 90, 5.0, False)
    return VideoTimeline((VideoSegment(info, 0.0, duration_seconds),), duration_seconds)


def _fake_export_config() -> SimpleNamespace:
    return SimpleNamespace(
        output=SimpleNamespace(
            width=160,
            height=90,
            fps=5,
            bitrate_mbps=1,
            video_codec="h264",
            video_encoder="software",
        ),
        timeline=SimpleNamespace(activity_start_offset_frames=0),
    )


def test_composed_export_accepts_normal_pipe_close_within_tail_tolerance(
    tmp_path: Path, monkeypatch
) -> None:
    _mock_export_process(monkeypatch, successful_writes=2)
    target = tmp_path / "rendered.mp4"

    result = export_composed_video(
        _fake_export_config(),
        SimpleNamespace(output=target),
        10,
        [],
        MarkerRenderer(),
        _fake_timeline(0.6),
    )

    assert result["frame_count"] == 3
    assert result["frames_submitted"] == 2
    assert result["pipe_closed_at_timeline_end"] is True
    assert target.is_file()


def test_composed_export_reports_diagnostics_for_premature_pipe_close(
    tmp_path: Path, monkeypatch
) -> None:
    _mock_export_process(monkeypatch, successful_writes=0)

    with pytest.raises(ExportError, match="已提交 0/10 帧") as captured:
        export_composed_video(
            _fake_export_config(),
            SimpleNamespace(output=tmp_path / "rendered.mp4"),
            10,
            [],
            MarkerRenderer(),
            _fake_timeline(2.0),
        )

    assert captured.value.details["ffmpeg_return_code"] == 0
    assert captured.value.details["remaining_frames"] == 10


def _make_clip(path: Path, color: str, frequency: int, size: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r=5:d=0.4",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def _make_silent_clip(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x90:r=5:d=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_composed_export_concatenates_video_and_keeps_audio(tmp_path: Path) -> None:
    first = tmp_path / "001.mp4"
    second = tmp_path / "002.mp4"
    _make_clip(first, "red", 440, "160x90")
    _make_clip(second, "blue", 660, "120x90")
    timeline = VideoTimeline.from_paths((first, second))
    target = tmp_path / "rendered.mp4"
    config = SimpleNamespace(
        output=SimpleNamespace(
            width=160,
            height=90,
            fps=5,
            bitrate_mbps=1,
            video_codec="h264",
            video_encoder="software",
        ),
        timeline=SimpleNamespace(activity_start_offset_frames=0),
    )

    result = export_composed_video(
        config,
        SimpleNamespace(output=target),
        10,
        [],
        MarkerRenderer(),
        timeline,
    )

    preview = tmp_path / "preview.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0.1",
            "-i",
            str(target),
            "-frames:v",
            "1",
            str(preview),
        ],
        check=True,
    )

    output = probe_video(target)
    with Image.open(preview) as frame:
        red, green, blue = frame.convert("RGB").getpixel((80, 45))
    assert result["video_count"] == 2
    assert result["audio_included"] is True
    assert result["encoder"] == "libx264"
    assert output.has_audio is True
    assert (output.width, output.height) == (160, 90)
    assert output.duration_seconds == pytest.approx(0.8, abs=0.08)
    assert green > 180 and green > red * 2 and green > blue * 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_composed_export_trims_video_and_audio_at_join(tmp_path: Path) -> None:
    first = tmp_path / "001.mp4"
    second = tmp_path / "002.mp4"
    _make_clip(first, "red", 440, "160x90")
    _make_clip(second, "blue", 660, "160x90")
    timeline = VideoTimeline.from_paths((first, second), overlap_frames=(1,))
    target = tmp_path / "rendered.mp4"
    config = SimpleNamespace(
        output=SimpleNamespace(
            width=160,
            height=90,
            fps=5,
            bitrate_mbps=1,
            video_codec="h264",
            video_encoder="software",
        ),
        timeline=SimpleNamespace(activity_start_offset_frames=0),
    )

    result = export_composed_video(
        config,
        SimpleNamespace(output=target),
        10,
        [],
        MarkerRenderer(),
        timeline,
    )

    output = probe_video(target)
    assert result["video_join_overlap_frames"] == [1]
    assert result["audio_included"] is True
    assert output.has_audio is True
    assert output.duration_seconds == pytest.approx(0.6, abs=0.08)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_composed_export_handles_video_only_segments(tmp_path: Path) -> None:
    first = tmp_path / "001.mp4"
    second = tmp_path / "002.mp4"
    _make_silent_clip(first, "red")
    _make_silent_clip(second, "blue")
    timeline = VideoTimeline.from_paths((first, second), overlap_frames=(1,))
    target = tmp_path / "rendered.mp4"
    config = SimpleNamespace(
        output=SimpleNamespace(
            width=160,
            height=90,
            fps=5,
            bitrate_mbps=1,
            video_codec="h264",
            video_encoder="software",
        ),
        timeline=SimpleNamespace(activity_start_offset_frames=0),
    )

    result = export_composed_video(
        config,
        SimpleNamespace(output=target),
        10,
        [],
        MarkerRenderer(),
        timeline,
    )

    output = probe_video(target)
    assert result["audio_included"] is False
    assert output.has_audio is False
    assert output.duration_seconds == pytest.approx(0.6, abs=0.08)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_composed_export_handles_single_segment(tmp_path: Path) -> None:
    source = tmp_path / "001.mp4"
    _make_clip(source, "red", 440, "160x90")
    timeline = VideoTimeline.from_paths((source,))
    target = tmp_path / "rendered.mp4"
    config = SimpleNamespace(
        output=SimpleNamespace(
            width=160,
            height=90,
            fps=5,
            bitrate_mbps=1,
            video_codec="h264",
            video_encoder="software",
        ),
        timeline=SimpleNamespace(activity_start_offset_frames=0),
    )

    result = export_composed_video(
        config,
        SimpleNamespace(output=target),
        10,
        [],
        MarkerRenderer(),
        timeline,
    )

    output = probe_video(target)
    assert result["video_count"] == 1
    assert output.has_audio is True
    assert output.duration_seconds == pytest.approx(0.4, abs=0.08)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_composed_export_supports_h265(tmp_path: Path) -> None:
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        check=False,
        text=True,
    )
    if "libx265" not in encoders.stdout + encoders.stderr:
        pytest.skip("FFmpeg does not include libx265")
    source = tmp_path / "001.mp4"
    _make_silent_clip(source, "red")
    timeline = VideoTimeline.from_paths((source,))
    target = tmp_path / "rendered-hevc.mp4"
    config = SimpleNamespace(
        output=SimpleNamespace(
            width=160,
            height=90,
            fps=5,
            bitrate_mbps=1,
            video_codec="h265",
            video_encoder="software",
        ),
        timeline=SimpleNamespace(activity_start_offset_frames=0),
    )

    result = export_composed_video(
        config,
        SimpleNamespace(output=target),
        10,
        [],
        MarkerRenderer(),
        timeline,
    )
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,codec_tag_string",
            "-of",
            "default=noprint_wrappers=1",
            str(target),
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result["video_codec"] == "h265"
    assert result["encoder"] == "libx265"
    assert "codec_name=hevc" in probe.stdout
    assert "codec_tag_string=hvc1" in probe.stdout
