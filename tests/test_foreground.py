from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from ride_overlay import AppConfig, ConfigError, FrameRenderer, resolve_paths


def foreground_config(value: str | None) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 2,
            "opacity": 0.5,
            "inputs": {"foreground_image_file": value},
            "clip": {},
            "output": {
                "width": 100,
                "height": 50,
                "fps": 10,
                "background": {"mode": "transparent"},
            },
            "dashboards": [
                {
                    "id": "speed",
                    "source": "speed",
                    "font_size": 20,
                    "anchor": {"x": 0.5, "y": 0.5},
                }
            ],
        }
    )


def create_project(project: Path) -> None:
    project.mkdir()
    (project / "activity.gpx").touch()
    (project / "font.ttf").touch()


def test_foreground_path_is_resolved_inside_project(tmp_path: Path) -> None:
    create_project(tmp_path / "project")
    project = tmp_path / "project"
    Image.new("RGBA", (100, 50), (0, 0, 0, 0)).save(project / "frame.png")

    paths = resolve_paths(project, foreground_config("frame.png"))

    assert paths.foreground_image == (project / "frame.png").resolve()


def test_foreground_is_only_accepted_inside_inputs() -> None:
    raw = foreground_config(None).model_dump(mode="json")
    raw["foreground_image_file"] = "frame.png"

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        AppConfig.model_validate(raw)


def test_foreground_path_cannot_escape_project_and_requires_png(tmp_path: Path) -> None:
    create_project(tmp_path / "project")
    project = tmp_path / "project"
    Image.new("RGBA", (100, 50), (0, 0, 0, 0)).save(tmp_path / "outside.png")
    (project / "frame.jpg").touch()

    with pytest.raises(ConfigError, match="不得指向项目目录外"):
        resolve_paths(project, foreground_config("../outside.png"))
    with pytest.raises(ConfigError, match="前景图片目前只支持 PNG"):
        resolve_paths(project, foreground_config("frame.jpg"))


def test_foreground_renders_above_dashboards_and_global_opacity_applies() -> None:
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.width = 3
    renderer.height = 1
    renderer.opacity = 0.5
    renderer.dashboard_background = Image.new("RGBA", (3, 1), (255, 0, 0, 255))
    renderer.dashboard_foreground = Image.new("RGBA", (3, 1), (0, 0, 0, 0))
    renderer.dashboard_foreground.putpixel((2, 0), (0, 0, 255, 255))

    def draw_dashboard(frame, _runtimes, _texts, *, preview):
        assert preview is False
        frame.putpixel((1, 0), (0, 255, 0, 255))
        frame.putpixel((2, 0), (0, 255, 0, 255))

    renderer._draw_dashboards = draw_dashboard

    overlay = renderer.render_overlay([], ())

    assert overlay.getpixel((0, 0)) == (255, 0, 0, 128)
    assert overlay.getpixel((1, 0)) == (0, 255, 0, 128)
    assert overlay.getpixel((2, 0)) == (0, 0, 255, 128)


def test_foreground_must_match_output_aspect_ratio(tmp_path: Path) -> None:
    path = tmp_path / "square.png"
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(path)
    renderer = FrameRenderer.__new__(FrameRenderer)
    renderer.width = 100
    renderer.height = 50

    with pytest.raises(ConfigError, match="前景图片宽高比"):
        renderer._load_full_frame_image(path, "仪表盘前景图片")
