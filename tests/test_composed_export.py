from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ride_overlay_export import export_composed_video
from ride_overlay_video import VideoTimeline, probe_video


class MarkerRenderer:
    def render_overlay(self, *_args, **_kwargs) -> Image.Image:
        image = Image.new("RGBA", (160, 90), (0, 0, 0, 0))
        for x in range(70, 91):
            for y in range(35, 56):
                image.putpixel((x, y), (0, 255, 0, 255))
        return image


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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_composed_export_concatenates_video_and_keeps_audio(tmp_path: Path) -> None:
    first = tmp_path / "001.mp4"
    second = tmp_path / "002.mp4"
    _make_clip(first, "red", 440, "160x90")
    _make_clip(second, "blue", 660, "120x90")
    timeline = VideoTimeline.from_paths((first, second))
    target = tmp_path / "rendered.mp4"
    config = SimpleNamespace(
        output=SimpleNamespace(width=160, height=90, fps=5, bitrate_mbps=1),
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
    assert output.has_audio is True
    assert (output.width, output.height) == (160, 90)
    assert output.duration_seconds == pytest.approx(0.8, abs=0.08)
    assert green > 180 and green > red * 2 and green > blue * 2
