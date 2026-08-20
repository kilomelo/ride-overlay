from __future__ import annotations

import json
from pathlib import Path

import pytest

import ride_overlay_video_analysis as analysis_module
from ride_overlay_video import VideoInfo
from ride_overlay_video_analysis import (
    VideoJoinAnalysis,
    _apply_conservative_group_fallback,
    analyze_frame_windows,
    prepare_editor_video_configuration,
)


def _frame(value: int) -> bytes:
    return bytes([value, value + 1, value + 2, value + 3])


def _minimal_config() -> dict:
    return {
        "schema_version": 1,
        "inputs": {
            "activity_file": "ride.fit",
            "font_file": "font.ttf",
        },
        "output": {
            "width": 320,
            "height": 180,
            "fps": 30,
            "background": {"mode": "transparent"},
        },
        "dashboards": [
            {
                "id": "speed",
                "source": "speed",
                "font_size": 30,
                "anchor": {"x": 0.5, "y": 0.5},
            }
        ],
    }


def test_video_frame_analysis_finds_unique_exact_overlap() -> None:
    previous = [_frame(value) for value in (10, 30, 50, 70, 90)]
    next_ = [_frame(value) for value in (70, 90, 120, 140)]

    result = analyze_frame_windows("a.mp4", "b.mp4", previous, next_, (2, 2))

    assert result.detected_overlap_frames == 2
    assert result.applied_overlap_frames == 2
    assert result.method == "exact_video_frames"
    assert result.confidence >= 0.98


def test_video_frame_analysis_does_not_force_an_approximate_mismatch() -> None:
    previous = [_frame(value) for value in (5, 30, 60, 90)]
    next_ = [_frame(value) for value in (150, 180, 210)]

    result = analyze_frame_windows("a.mp4", "b.mp4", previous, next_, (2, 2))

    assert result.applied_overlap_frames == 0
    assert result.method == "no_reliable_video_match"


def test_group_fallback_only_corrects_low_confidence_nonzero_outlier() -> None:
    values = [
        VideoJoinAnalysis("a", "b", 3, 3, 1.0, "exact"),
        VideoJoinAnalysis("b", "c", 3, 3, 1.0, "exact"),
        VideoJoinAnalysis("c", "d", 4, 4, 1.0, "exact"),
        VideoJoinAnalysis("d", "e", 26, 26, 0.56, "approximate"),
        VideoJoinAnalysis("e", "f", 0, 0, 0.1, "no_match"),
    ]

    result = _apply_conservative_group_fallback(values)

    assert [item.applied_overlap_frames for item in result] == [3, 3, 4, 3, 0]
    assert result[3].fallback_reason is not None
    assert result[4].fallback_reason is None


def test_editor_preparation_persists_playlist_and_missing_joins_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ride.fit").touch()
    (tmp_path / "font.ttf").touch()
    first = tmp_path / "001.mp4"
    second = tmp_path / "002.mp4"
    first.touch()
    second.touch()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_minimal_config()), encoding="utf-8")

    def fake_probe(path: Path) -> VideoInfo:
        return VideoInfo(path.resolve(), 10.0, 320, 180, 30.0, True)

    calls: list[tuple[str, str]] = []

    def fake_analysis(
        project: Path, previous: VideoInfo, next_: VideoInfo
    ) -> VideoJoinAnalysis:
        del project
        calls.append((previous.path.name, next_.path.name))
        return VideoJoinAnalysis(previous.path.name, next_.path.name, 3, 3, 1.0, "exact")

    monkeypatch.setattr(analysis_module, "probe_video", fake_probe)
    monkeypatch.setattr(analysis_module, "analyze_video_join", fake_analysis)

    result = prepare_editor_video_configuration(tmp_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert result.config_changed is True
    assert calls == [("001.mp4", "002.mp4")]
    assert saved["inputs"]["video_files"] == ["001.mp4", "002.mp4"]
    assert saved["timeline"]["video_joins"] == [
        {
            "previous_file": "001.mp4",
            "next_file": "002.mp4",
            "overlap_frames": 3,
        }
    ]
    assert result.report_path.is_file()

    saved["timeline"]["video_joins"][0]["overlap_frames"] = 7
    config_path.write_text(json.dumps(saved), encoding="utf-8")
    calls.clear()
    prepare_editor_video_configuration(tmp_path)
    reloaded = json.loads(config_path.read_text(encoding="utf-8"))

    assert calls == []
    assert reloaded["timeline"]["video_joins"][0]["overlap_frames"] == 7
