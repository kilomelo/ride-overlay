from __future__ import annotations

from pathlib import Path

import pytest

import ride_overlay_video
from ride_overlay_video import (
    VideoInfo,
    VideoTimeline,
    discover_video_files,
)


def test_discovered_videos_use_natural_filename_order_and_exclusions(tmp_path: Path) -> None:
    for name in ("clip10.mp4", "clip2.mp4", "clip1.mp4", "overlay.mp4", "notes.txt"):
        (tmp_path / name).touch()

    videos = discover_video_files(tmp_path, excluded=(tmp_path / "clip2.mp4",))

    assert [path.name for path in videos] == ["clip1.mp4", "clip10.mp4"]


def test_explicit_video_list_preserves_order(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").touch()
    (tmp_path / "b.mp4").touch()

    videos = discover_video_files(tmp_path, ["b.mp4", "a.mp4"])

    assert [path.name for path in videos] == ["b.mp4", "a.mp4"]


def test_virtual_timeline_maps_join_to_the_later_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    durations = {paths[0]: 3.0, paths[1]: 2.0}

    def fake_probe(path: Path) -> VideoInfo:
        return VideoInfo(path, durations[path], 1920, 1080, 30.0, True)

    monkeypatch.setattr(ride_overlay_video, "probe_video", fake_probe)
    timeline = VideoTimeline.from_paths(paths)

    assert timeline.duration_seconds == pytest.approx(5.0)
    assert timeline.join_times == (3.0,)
    assert timeline.locate(2.999).index == 0
    at_join = timeline.locate(3.0)
    assert at_join.index == 1
    assert at_join.local_seconds == pytest.approx(0.0)
    assert timeline.locate(99).local_seconds == pytest.approx(2.0)
