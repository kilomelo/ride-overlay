from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QMessageBox, QProgressDialog

import ride_overlay_gui.export_worker as export_worker_module
import ride_overlay_gui.window as window_module
from ride_overlay_data import ActivityData
from ride_overlay_export import ExportError
from ride_overlay_gui.export_worker import ExportWorker
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


def test_export_failure_report_captures_error_event_and_ffmpeg_details(
    tmp_path: Path, monkeypatch
) -> None:
    export_dir = tmp_path / "export"
    model = SimpleNamespace(
        project_dir=tmp_path,
        config=SimpleNamespace(model_dump=lambda **_kwargs: {}),
        activity=ActivityData(datetime(2026, 1, 1, tzinfo=UTC), 1.0, {}, 1),
        paths=SimpleNamespace(export_dir=export_dir),
        video_timeline=SimpleNamespace(segments=()),
    )
    monkeypatch.setattr(export_worker_module, "build_dashboard_runtimes", lambda *_a, **_k: [])
    monkeypatch.setattr(export_worker_module, "FrameRenderer", lambda *_a, **_k: object())

    def fail_export(*_args, **_kwargs):
        raise ExportError(
            "模拟 FFmpeg 失败",
            details={"ffmpeg_return_code": -9, "frames_submitted": 123},
        )

    monkeypatch.setattr(export_worker_module, "export_composed_video", fail_export)

    ExportWorker(model).run()

    content = (export_dir / "result.log").read_text(encoding="utf-8")
    assert "error_count: 2" in content
    assert "完整视频导出失败: 模拟 FFmpeg 失败" in content
    assert '"ffmpeg_return_code": -9' in content
    assert '"frames_submitted": 123' in content


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
    assert window.preview_scale_button.text() == "适应窗口"
    window.preview_scale_button.click()
    assert window.preview_scale_button.isChecked() is True
    assert window.preview_scale_button.text() == "原始分辨率"
    assert window.canvas.fit_to_view is False
    assert window.canvas.transform().m11() == pytest.approx(1)
    window.preview_scale_button.click()
    assert window.canvas.fit_to_view is True
    assert window.playback_rate_button.text() == "1 X"
    for expected in ("2 X", "3 X", "5 X", "10 X", "0.5 X", "1 X"):
        window.playback_rate_button.click()
        assert window.playback_rate_button.text() == expected
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
