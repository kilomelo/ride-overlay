from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import ride_overlay_gui.project as project_module
from ride_overlay import load_config
from ride_overlay_ffmpeg import FFmpegCapabilityError
from ride_overlay_gui.project import ensure_editor_video_codec


def _write_h265_config(project: Path) -> None:
    (project / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "inputs": {},
                "output": {
                    "width": 320,
                    "height": 180,
                    "fps": 30,
                    "bitrate_mbps": 12,
                    "video_codec": "h265",
                    "video_encoder": "auto",
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
        ),
        encoding="utf-8",
    )


def test_editor_keeps_h265_when_probe_succeeds(tmp_path: Path, monkeypatch) -> None:
    _write_h265_config(tmp_path)
    monkeypatch.setattr(project_module.shutil, "which", lambda _name: "/opt/ffmpeg")
    monkeypatch.setattr(
        project_module,
        "select_video_encoder",
        lambda *_args: SimpleNamespace(name="hevc_videotoolbox"),
    )

    result = ensure_editor_video_codec(tmp_path, load_config(tmp_path))

    assert result.output.video_codec.value == "h265"
    assert result.output.bitrate_mbps == 12


def test_editor_persists_h264_fallback_when_h265_probe_fails(
    tmp_path: Path, monkeypatch
) -> None:
    _write_h265_config(tmp_path)
    monkeypatch.setattr(project_module.shutil, "which", lambda _name: "/opt/ffmpeg")

    def fail_probe(*_args):
        raise FFmpegCapabilityError("HEVC unavailable")

    monkeypatch.setattr(project_module, "select_video_encoder", fail_probe)

    result = ensure_editor_video_codec(tmp_path, load_config(tmp_path))
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))

    assert result.output.video_codec.value == "h264"
    assert result.output.video_encoder.value == "auto"
    assert result.output.bitrate_mbps == 16
    assert persisted["output"]["video_codec"] == "h264"
    assert persisted["output"]["video_encoder"] == "auto"
    assert persisted["output"]["bitrate_mbps"] == 16
