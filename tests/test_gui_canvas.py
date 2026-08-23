from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from ride_overlay import AppConfig
from ride_overlay_gui.canvas import EditorCanvas
from ride_overlay_gui.project import EditorFrame


class CanvasModel:
    def __init__(self, width: int = 1600, height: int = 900) -> None:
        self.config = SimpleNamespace(
            output=SimpleNamespace(width=width, height=height),
            dashboards=[],
        )

    def frame_at(self, _video_time_seconds: float) -> EditorFrame:
        size = (self.config.output.width, self.config.output.height)
        return EditorFrame(Image.new("RGBA", size), (), {}, 0.0)


class DebounceCanvasModel:
    def __init__(self) -> None:
        self.config = AppConfig.model_validate(
            {
                "schema_version": 2,
                "inputs": {},
                "output": {
                    "width": 160,
                    "height": 90,
                    "fps": 30,
                    "background": {"mode": "transparent"},
                },
                "dashboards": [
                    {
                        "type": "heartbeat",
                        "id": "heart",
                        "width": 0.1,
                        "anchor": {"x": 0.5, "y": 0.5},
                    }
                ],
            }
        )
        self.frame_calls = 0
        self.rebuild_calls = 0

    def frame_at(self, _video_time_seconds: float) -> EditorFrame:
        self.frame_calls += 1
        size = (self.config.output.width, self.config.output.height)
        return EditorFrame(
            Image.new("RGBA", size),
            (),
            {"heart": (60.0, 35.0, 100.0, 55.0)},
            0.0,
        )

    def dashboard(self, dashboard_id: str):
        return next(item for item in self.config.dashboards if item.id == dashboard_id)

    def rebuild_dashboard_runtime(self) -> None:
        self.rebuild_calls += 1


def test_canvas_toggles_between_fit_and_original_resolution(qtbot) -> None:
    canvas = EditorCanvas(CanvasModel())  # type: ignore[arg-type]
    qtbot.addWidget(canvas)
    canvas.resize(QSize(800, 600))
    canvas.show()
    QApplication.processEvents()

    assert canvas.fit_to_view is True
    assert canvas.transform().m11() < 1

    canvas.set_fit_to_view(False)

    assert canvas.fit_to_view is False
    assert canvas.transform().m11() == pytest.approx(1)
    assert canvas.horizontalScrollBar().maximum() > 0
    assert canvas.verticalScrollBar().maximum() > 0

    canvas.resize(QSize(640, 480))
    QApplication.processEvents()
    assert canvas.transform().m11() == pytest.approx(1)

    canvas.set_fit_to_view(True)

    assert canvas.fit_to_view is True
    assert canvas.transform().m11() < 1


def test_drag_feedback_is_immediate_but_rendering_is_debounced(qtbot) -> None:
    model = DebounceCanvasModel()
    canvas = EditorCanvas(model)  # type: ignore[arg-type]
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.select_dashboard("heart")
    initial_frame_calls = model.frame_calls
    changes: list[tuple[str, dict[str, float | int]]] = []
    canvas.geometryChanged.connect(
        lambda dashboard_id, patch: changes.append((dashboard_id, patch))
    )

    dashboard = model.dashboard("heart")
    canvas._drag_mode = "move"
    canvas._drag_dashboard_id = "heart"
    canvas._drag_start_anchor.setX(0.5)
    canvas._drag_start_anchor.setY(0.5)
    canvas._drag_start_bounds = (60.0, 35.0, 100.0, 55.0)
    dashboard.anchor.x = 0.6
    canvas._update_drag_feedback()
    canvas._schedule_geometry_update("heart", {"anchor_x": 0.55, "anchor_y": 0.5})
    canvas._schedule_geometry_update("heart", {"anchor_x": 0.6, "anchor_y": 0.5})

    assert canvas.selection_item.rect().left() == pytest.approx(76)
    assert canvas.move_item.pos().x() == pytest.approx(96)
    assert model.frame_calls == initial_frame_calls
    assert model.rebuild_calls == 0
    assert changes == []

    qtbot.waitUntil(lambda: model.frame_calls == initial_frame_calls + 1, timeout=1000)

    assert model.rebuild_calls == 1
    assert changes == [("heart", {"anchor_x": 0.6, "anchor_y": 0.5})]
