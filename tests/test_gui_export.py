from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QMessageBox, QProgressDialog

import ride_overlay_gui.window as window_module
from ride_overlay_gui.project import EditorProject
from ride_overlay_gui.window import EditorWindow


def _system_font() -> Path | None:
    candidates = (
        Path("/System/Library/Fonts/SFNSMono.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    )
    return next((path for path in candidates if path.is_file()), None)


def _make_clip(path: Path) -> None:
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
            "color=c=blue:s=160x90:r=5:d=0.4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.4",
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


class OneFrameExportWorker(QObject):
    progressChanged = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    completed = Signal()

    def __init__(self, _model: EditorProject) -> None:
        super().__init__()

    def request_cancel(self) -> None:
        pass

    @Slot()
    def run(self) -> None:
        # Reproduce the exact transition from the crash: setValue(maximum)
        # from a worker-thread signal, then finish the task.
        self.progressChanged.emit(1, 1)
        QThread.msleep(25)
        self.succeeded.emit({"path": "test.mp4"})
        self.completed.emit()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_gui_export_updates_native_dialog_only_on_gui_thread(
    tmp_path: Path, qtbot, monkeypatch
) -> None:
    font = _system_font()
    if font is None:
        pytest.skip("A usable system font is required")
    local_font = tmp_path / f"font{font.suffix}"
    shutil.copyfile(font, local_font)
    (tmp_path / "activity.gpx").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="31.0" lon="121.0"><time>2026-01-01T00:00:00Z</time></trkpt>
    <trkpt lat="31.0001" lon="121.0"><time>2026-01-01T00:00:01Z</time></trkpt>
  </trkseg></trk>
</gpx>
""",
        encoding="utf-8",
    )
    _make_clip(tmp_path / "clip.mp4")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "inputs": {
                    "activity_file": "activity.gpx",
                    "font_file": local_font.name,
                    "video_files": ["clip.mp4"],
                },
                "timeline": {"activity_start_offset_frames": 0},
                "output": {
                    "filename": "gui-export.mp4",
                    "width": 160,
                    "height": 90,
                    "fps": 5,
                    "bitrate_mbps": 1,
                    "background": {"mode": "chroma_key"},
                },
                "dashboards": [
                    {
                        "id": "elapsed",
                        "source": "elapsed_time",
                        "font_size": 16,
                        "anchor": {"x": 0.5, "y": 0.5},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(window_module, "ExportWorker", OneFrameExportWorker)
    progress_thread_ids: list[int] = []
    original_set_value = QProgressDialog.setValue

    def checked_set_value(dialog: QProgressDialog, value: int) -> None:
        progress_thread_ids.append(threading.get_ident())
        original_set_value(dialog, value)

    monkeypatch.setattr(QProgressDialog, "setValue", checked_set_value)

    window = EditorWindow(EditorProject.load(tmp_path))
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.canvas.setFocus()
    qtbot.waitUntil(window.isActiveWindow)
    qtbot.keyClick(window.canvas.viewport(), Qt.Key.Key_Space)
    assert window.playback.is_playing is True
    qtbot.keyClick(window.canvas.viewport(), Qt.Key.Key_Right)
    assert window.playback.is_playing is False
    assert window.current_time_seconds == pytest.approx(0.2)
    assert "00:00:00:01" in window.time_label.text()
    gui_thread_id = threading.get_ident()
    window._start_export()
    qtbot.waitUntil(lambda: window._export_thread is None, timeout=15_000)

    assert len(progress_thread_ids) >= 2
    assert set(progress_thread_ids) == {gui_thread_id}
