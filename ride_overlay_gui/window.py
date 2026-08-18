"""Main window for the ride-overlay graphical editor."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QThread, Slot
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap, QTransform
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ride_overlay_gui.assets import image_asset
from ride_overlay_gui.canvas import EditorCanvas
from ride_overlay_gui.config_sync import ConfigSynchronizer
from ride_overlay_gui.export_worker import ExportWorker
from ride_overlay_gui.playback import VirtualPlaybackController
from ride_overlay_gui.project import EditorProject
from ride_overlay_gui.timeline import (
    RepeatStepButton,
    TimelineWidget,
    format_clock,
    format_offset_frames,
)


class EditorWindow(QMainWindow):
    def __init__(self, model: EditorProject) -> None:
        super().__init__()
        self.model = model
        self.current_time_seconds = 0.0
        self._export_thread: QThread | None = None
        self._export_worker: ExportWorker | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._config_valid = True

        self.setWindowTitle(f"ride-overlay 编辑器 — {model.project_dir.name}")
        self.resize(1280, 900)
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self.setCentralWidget(root)

        self.canvas = EditorCanvas(model, self)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.canvas, 1)

        alignment_row = QHBoxLayout()
        alignment_row.addStretch(1)
        self.offset_label = QLabel()
        alignment_row.addWidget(self.offset_label)
        self.offset_left = RepeatStepButton(-1, self)
        self.offset_right = RepeatStepButton(1, self)
        align_pixmap = QPixmap(str(image_asset("align_btn.png")))
        self.offset_left.setIcon(QIcon(align_pixmap))
        self.offset_right.setIcon(QIcon(align_pixmap.transformed(QTransform().scale(-1, 1))))
        for button in (self.offset_left, self.offset_right):
            button.setFixedSize(34, 34)
            button.setIconSize(button.size() * 0.65)
        alignment_row.addWidget(self.offset_left)
        alignment_row.addWidget(self.offset_right)
        alignment_row.addStretch(1)
        layout.addLayout(alignment_row)

        self.timeline_widget = TimelineWidget(self)
        layout.addWidget(self.timeline_widget)

        transport = QHBoxLayout()
        self.play_button = QToolButton(self)
        self.play_button.setFixedSize(62, 62)
        self.play_button.setIconSize(self.play_button.size())
        transport.addWidget(self.play_button)
        self.time_label = QLabel()
        transport.addWidget(self.time_label)
        transport.addStretch(1)
        self.export_button = QPushButton("导出完整视频", self)
        self.export_button.setMinimumHeight(38)
        transport.addWidget(self.export_button)
        layout.addLayout(transport)

        self.playback = VirtualPlaybackController(self.canvas.video_item, self)
        self.playback.set_timeline(model.video_timeline)
        self.config_sync = ConfigSynchronizer(model.project_dir, self)
        self._connect_signals()
        self._refresh_timeline_model()
        self._set_playing_ui(False)
        self._on_position_changed(0.0)
        if not model.video_timeline.segments:
            self.statusBar().showMessage("未找到视频：仍可在黑色画布上调整仪表盘，但不能导出成片")

    def _connect_signals(self) -> None:
        self.play_button.clicked.connect(self.playback.toggle)
        self.playback.positionChanged.connect(self._on_position_changed)
        self.playback.playingChanged.connect(self._set_playing_ui)
        self.playback.errorOccurred.connect(self._show_error)
        self.timeline_widget.seekRequested.connect(self._seek)
        self.offset_left.stepRequested.connect(self._change_offset)
        self.offset_right.stepRequested.connect(self._change_offset)
        self.canvas.dashboardSelected.connect(self._select_dashboard)
        self.canvas.geometryChanged.connect(self._save_geometry)
        self.canvas.geometryCommitted.connect(self.config_sync.flush)
        self.config_sync.configChanged.connect(self._reload_external_config)
        self.config_sync.configError.connect(self._config_error)
        self.config_sync.writeError.connect(self._show_error)
        self.export_button.clicked.connect(self._start_export)

    def _refresh_timeline_model(self) -> None:
        self.timeline_widget.set_timeline(
            total_seconds=self.model.display_duration_seconds,
            activity_duration_seconds=self.model.activity.duration_seconds,
            offset_frames=self.model.offset_frames,
            fps=self.model.config.output.fps,
            join_times=self.model.video_timeline.join_times,
        )
        self.offset_label.setText(
            "运动数据对齐偏移 "
            + format_offset_frames(self.model.offset_frames, self.model.config.output.fps)
        )

    def _on_position_changed(self, seconds: float) -> None:
        self.current_time_seconds = min(max(seconds, 0.0), self.model.display_duration_seconds)
        self.timeline_widget.set_current_time(self.current_time_seconds)
        self.canvas.refresh(self.current_time_seconds)
        self.time_label.setText(
            f"当前时间 {format_clock(self.current_time_seconds)} / "
            f"视频总时长 {format_clock(self.model.display_duration_seconds)}"
        )

    def _set_playing_ui(self, playing: bool) -> None:
        asset = "pause_btn.png" if playing else "play_btn.png"
        self.play_button.setIcon(QIcon(str(image_asset(asset))))
        if playing:
            self.canvas.select_dashboard(None)

    def _seek(self, seconds: float) -> None:
        if self.model.video_timeline.segments:
            self.playback.seek(seconds)
        else:
            self._on_position_changed(seconds)

    def _select_dashboard(self, dashboard_id: str | None) -> None:
        if dashboard_id is not None:
            self.playback.pause()
        self.canvas.select_dashboard(dashboard_id)

    def _save_geometry(self, dashboard_id: str, patch: dict[str, float | int]) -> None:
        self.config_sync.update_dashboard_geometry(dashboard_id, **patch)

    def _change_offset(self, direction: int) -> None:
        value = self.model.offset_frames + direction
        self.model.set_offset_frames(value)
        self.config_sync.update_offset_frames(value)
        self._refresh_timeline_model()
        self.canvas.refresh(self.current_time_seconds)

    def _reload_external_config(self) -> None:
        was_playing = self.playback.is_playing
        previous_time = self.current_time_seconds
        previous_paths = self.model.video_timeline.paths
        try:
            model = EditorProject.load(self.model.project_dir, previous=self.model)
        except Exception as exc:
            self._config_error(str(exc))
            return
        self.model = model
        self.canvas.set_model(model)
        if model.video_timeline.paths != previous_paths:
            self.playback.set_timeline(model.video_timeline)
        self._refresh_timeline_model()
        self._seek(min(previous_time, model.display_duration_seconds))
        if was_playing:
            self.playback.play()
        self._config_valid = True
        self._set_export_controls_enabled(self._export_thread is None)
        self.statusBar().showMessage("已载入 config.json 的外部修改", 3000)

    def _config_error(self, message: str) -> None:
        self._config_valid = False
        self._set_export_controls_enabled(False)
        self.statusBar().showMessage(message)

    @Slot(str)
    def _show_error(self, message: str) -> None:
        self.statusBar().showMessage(message)
        QMessageBox.critical(self, "ride-overlay", message)

    def _set_export_controls_enabled(self, enabled: bool) -> None:
        self.export_button.setEnabled(enabled)
        self.canvas.editing_enabled = enabled
        self.offset_left.setEnabled(enabled)
        self.offset_right.setEnabled(enabled)

    def _start_export(self) -> None:
        if not self.model.video_timeline.segments:
            self._show_error("没有源视频，无法导出完整视频")
            return
        target = self.model.paths.output
        if target.exists():
            answer = QMessageBox.question(
                self,
                "覆盖已有文件",
                f"导出将替换已有文件：\n{target}\n\n是否继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.playback.pause()
        self.canvas.select_dashboard(None)
        self.config_sync.flush()
        self._set_export_controls_enabled(False)
        frame_count = math.ceil(
            self.model.video_timeline.duration_seconds * self.model.config.output.fps
        )
        progress = QProgressDialog("正在渲染并合成完整视频…", "取消", 0, frame_count, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        # Completion is handled explicitly after the worker has reported its
        # outcome.  In particular, do not let setValue(maximum) implicitly
        # reset/close the native macOS dialog.
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        self._progress_dialog = progress

        thread = QThread(self)
        worker = ExportWorker(self.model)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        queued = Qt.ConnectionType.QueuedConnection
        # These signals originate in the export thread.  They must terminate
        # at QObject slots owned by the GUI thread; a Python lambda has no Qt
        # thread affinity and can otherwise update Cocoa windows off-thread.
        worker.progressChanged.connect(self._update_export_progress, queued)
        worker.succeeded.connect(self._export_succeeded, queued)
        worker.failed.connect(self._export_failed, queued)
        worker.cancelled.connect(self._export_cancelled, queued)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(self._export_finished)
        thread.finished.connect(thread.deleteLater)
        progress.canceled.connect(self._request_export_cancel)
        self._export_thread = thread
        self._export_worker = worker
        thread.start()

    @Slot(int, int)
    def _update_export_progress(self, current: int, total: int) -> None:
        progress = self._progress_dialog
        if progress is None:
            return
        if total > 0 and progress.maximum() != total:
            progress.setMaximum(total)
        progress.setValue(current)

    @Slot()
    def _request_export_cancel(self) -> None:
        worker = self._export_worker
        if worker is not None:
            # request_cancel only sets a threading.Event, so it does not rely
            # on the busy worker thread's Qt event loop.
            worker.request_cancel()
        if self._progress_dialog is not None:
            self._progress_dialog.setLabelText("正在取消导出…")

    def _dismiss_export_progress(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None

    @Slot(object)
    def _export_succeeded(self, result: dict[str, object]) -> None:
        self._dismiss_export_progress()
        QMessageBox.information(
            self,
            "导出完成",
            f"完整视频已生成：\n{result['path']}",
        )

    @Slot(str)
    def _export_failed(self, message: str) -> None:
        self._dismiss_export_progress()
        self._show_error(message)

    @Slot()
    def _export_cancelled(self) -> None:
        self._dismiss_export_progress()
        self.statusBar().showMessage("导出已取消", 3000)

    @Slot()
    def _export_finished(self) -> None:
        self._dismiss_export_progress()
        self._export_worker = None
        self._export_thread = None
        self._set_export_controls_enabled(self._config_valid)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._export_thread is not None and self._export_thread.isRunning():
            QMessageBox.warning(self, "正在导出", "请先取消导出并等待任务结束。")
            event.ignore()
            return
        self.config_sync.flush()
        super().closeEvent(event)
