"""Background Qt worker for full-video export."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal, Slot

from ride_overlay import RESULT_LOG_FILENAME, RunReport, file_details
from ride_overlay_dashboard import ClipRange, FrameRenderer, build_dashboard_runtimes
from ride_overlay_data import activity_details
from ride_overlay_export import ExportCancelled, export_composed_video
from ride_overlay_gui.project import EditorProject


class ExportWorker(QObject):
    progressChanged = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    completed = Signal()

    def __init__(self, model: EditorProject) -> None:
        super().__init__()
        self.model = model
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        """Request cancellation without depending on the worker event loop."""
        self._cancel.set()

    @Slot()
    def cancel(self) -> None:
        self.request_cancel()

    @Slot()
    def run(self) -> None:
        report = RunReport(
            mode="editor_export",
            project=self.model.project_dir,
            command=["ride-overlay-editor", str(self.model.project_dir), "export"],
        )
        report.result_path = self.model.paths.export_dir / RESULT_LOG_FILENAME
        report.details["config"] = self.model.config.model_dump(mode="json")
        report.details["activity"] = activity_details(self.model.activity)
        report.details["videos"] = [
            {
                **file_details(segment.info.path),
                "duration_seconds": segment.duration_seconds,
                "global_start_seconds": segment.start_seconds,
                "global_end_seconds": segment.end_seconds,
                "width": segment.info.width,
                "height": segment.info.height,
                "fps": segment.info.fps,
                "has_audio": segment.info.has_audio,
            }
            for segment in self.model.video_timeline.segments
        ]
        try:
            with report.stage("检查完整运动数据和仪表盘"):
                clip = ClipRange(0.0, self.model.activity.duration_seconds)
                runtimes = build_dashboard_runtimes(
                    self.model.config,
                    self.model.activity,
                    clip,
                    report=report,
                    paths=self.model.paths,
                )
            with report.stage("渲染仪表盘并合成完整视频"):
                renderer = FrameRenderer(self.model.config, self.model.paths)
                result = export_composed_video(
                    self.model.config,
                    self.model.paths,
                    self.model.activity.duration_seconds,
                    runtimes,
                    renderer,
                    self.model.video_timeline,
                    progress=self.progressChanged.emit,
                    cancellation=self._cancel,
                )
                report.details["result"] = result
        except ExportCancelled:
            report.finish("CANCELLED", 130)
            report.write()
            self.cancelled.emit()
        except Exception as exc:
            report.details["error"] = str(exc)
            report.finish("FAILED", 4)
            report.write()
            self.failed.emit(str(exc))
        else:
            report.finish("SUCCESS", 0)
            report.write()
            self.succeeded.emit(result)
        finally:
            self.completed.emit()
