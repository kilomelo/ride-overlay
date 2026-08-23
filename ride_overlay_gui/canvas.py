"""Video canvas, dashboard overlay, selection, move, and resize gestures."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
)

from ride_overlay_dashboard import DashboardConfig, ElevationProfileDashboardConfig
from ride_overlay_gui.assets import image_asset
from ride_overlay_gui.project import EditorFrame, EditorProject

GEOMETRY_RENDER_DEBOUNCE_MS = 150


class EditorCanvas(QGraphicsView):
    dashboardSelected = Signal(object)
    geometryChanged = Signal(str, object)
    geometryCommitted = Signal()

    def __init__(self, model: EditorProject, parent=None) -> None:
        super().__init__(parent)
        self.model = model
        self.current_time_seconds = 0.0
        self.selected_id: str | None = None
        self.editing_enabled = True
        self._fit_to_view = True
        self._frame: EditorFrame | None = None
        self._drag_mode: str | None = None
        self._drag_start_scene = QPointF()
        self._drag_start_anchor = QPointF()
        self._drag_start_distance = 0.0
        self._drag_start_size = 0.0
        self._drag_start_bounds: tuple[float, float, float, float] | None = None
        self._drag_dashboard_id: str | None = None
        self._pending_geometry_patch: tuple[str, dict[str, float | int]] | None = None
        self._geometry_revision = 0
        self._rendered_geometry_revision = 0
        self._geometry_render_timer = QTimer(self)
        self._geometry_render_timer.setSingleShot(True)
        self._geometry_render_timer.setInterval(GEOMETRY_RENDER_DEBOUNCE_MS)
        self._geometry_render_timer.timeout.connect(self._apply_pending_geometry_update)

        self.scene_object = QGraphicsScene(self)
        self.setScene(self.scene_object)
        self.setBackgroundBrush(QColor("black"))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )

        self.video_item = QGraphicsVideoItem()
        self.video_item.setZValue(0)
        self.scene_object.addItem(self.video_item)

        self.overlay_item = QGraphicsPixmapItem()
        self.overlay_item.setZValue(10)
        self.scene_object.addItem(self.overlay_item)

        self.selection_item = QGraphicsRectItem()
        selection_pen = QPen(QColor("#A855F7"), 2, Qt.PenStyle.DashLine)
        selection_pen.setCosmetic(True)
        self.selection_item.setPen(selection_pen)
        self.selection_item.setBrush(Qt.BrushStyle.NoBrush)
        self.selection_item.setZValue(20)
        self.selection_item.hide()
        self.scene_object.addItem(self.selection_item)

        move_pixmap = QPixmap(str(image_asset("move_widget.png"))).scaled(
            36,
            36,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.move_item = QGraphicsPixmapItem(move_pixmap)
        self.move_item.setOffset(-move_pixmap.width() / 2, -move_pixmap.height() / 2)
        self.move_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.move_item.setZValue(21)
        self.move_item.hide()
        self.scene_object.addItem(self.move_item)
        self.set_model(model)

    def set_model(self, model: EditorProject) -> None:
        self._geometry_render_timer.stop()
        self._pending_geometry_patch = None
        self._drag_dashboard_id = None
        self._geometry_revision = self._rendered_geometry_revision
        previous_scene = self.scene_object.sceneRect()
        previous_center = (
            self.mapToScene(self.viewport().rect().center())
            if previous_scene.isValid() and not previous_scene.isEmpty()
            else None
        )
        self.model = model
        width = model.config.output.width
        height = model.config.output.height
        self.scene_object.setSceneRect(0, 0, width, height)
        self.video_item.setSize(self.scene_object.sceneRect().size())
        self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._apply_preview_scale(previous_center)
        if self.selected_id and not any(
            item.id == self.selected_id for item in model.config.dashboards
        ):
            self.selected_id = None
        self.refresh(self.current_time_seconds)

    @property
    def fit_to_view(self) -> bool:
        return self._fit_to_view

    def set_fit_to_view(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._fit_to_view:
            return
        current_center = self.mapToScene(self.viewport().rect().center())
        self._fit_to_view = enabled
        self._apply_preview_scale(current_center)

    def _apply_preview_scale(self, center: QPointF | None = None) -> None:
        scene_rect = self.scene_object.sceneRect()
        self.resetTransform()
        if self._fit_to_view:
            self.fitInView(scene_rect, Qt.AspectRatioMode.KeepAspectRatio)
            return
        self.centerOn(center if center is not None else scene_rect.center())

    @staticmethod
    def _to_pixmap(frame: EditorFrame) -> QPixmap:
        image = frame.image
        data = image.tobytes("raw", "RGBA")
        qimage = QImage(
            data,
            image.width,
            image.height,
            image.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
        return QPixmap.fromImage(qimage)

    def refresh(self, video_time_seconds: float) -> None:
        self.current_time_seconds = video_time_seconds
        self._frame = self.model.frame_at(video_time_seconds)
        self.overlay_item.setPixmap(self._to_pixmap(self._frame))
        self._update_selection_items()

    def select_dashboard(self, dashboard_id: str | None) -> None:
        if self._pending_geometry_patch is not None:
            self._geometry_render_timer.stop()
            self._apply_pending_geometry_update()
        self.selected_id = dashboard_id
        self._drag_mode = None
        self._drag_dashboard_id = None
        self._update_selection_items()

    def _schedule_geometry_update(
        self,
        dashboard_id: str,
        patch: dict[str, float | int],
    ) -> None:
        self._pending_geometry_patch = (dashboard_id, patch)
        self._geometry_revision += 1
        self._geometry_render_timer.start()

    def _apply_pending_geometry_update(self) -> None:
        pending = self._pending_geometry_patch
        if pending is not None:
            dashboard_id, patch = pending
            self.geometryChanged.emit(dashboard_id, patch)
            self._pending_geometry_patch = None
        if self._rendered_geometry_revision == self._geometry_revision:
            return
        dashboard_id = self._drag_dashboard_id
        if dashboard_id is not None:
            dashboard = self.model.dashboard(dashboard_id)
            if not isinstance(dashboard, DashboardConfig):
                self.model.rebuild_dashboard_runtime()
        self.refresh(self.current_time_seconds)
        self._rendered_geometry_revision = self._geometry_revision

    def _update_drag_feedback(self) -> None:
        if (
            self._drag_mode is None
            or self._drag_dashboard_id is None
            or self._drag_start_bounds is None
        ):
            return
        dashboard = self.model.dashboard(self._drag_dashboard_id)
        output = self.model.config.output
        anchor_x = dashboard.anchor.x * output.width
        anchor_y = dashboard.anchor.y * output.height
        left, top, right, bottom = self._drag_start_bounds
        if self._drag_mode == "move":
            start_anchor_x = self._drag_start_anchor.x() * output.width
            start_anchor_y = self._drag_start_anchor.y() * output.height
            delta_x = anchor_x - start_anchor_x
            delta_y = anchor_y - start_anchor_y
            left += delta_x
            right += delta_x
            top += delta_y
            bottom += delta_y
        else:
            current_size = (
                float(dashboard.font_size)
                if isinstance(dashboard, DashboardConfig)
                else float(dashboard.width)
            )
            scale = current_size / self._drag_start_size
            left = anchor_x + (left - anchor_x) * scale
            right = anchor_x + (right - anchor_x) * scale
            top = anchor_y + (top - anchor_y) * scale
            bottom = anchor_y + (bottom - anchor_y) * scale
        self.selection_item.setRect(QRectF(left, top, right - left, bottom - top))
        self.move_item.setPos(anchor_x, anchor_y)
        self.selection_item.show()
        self.move_item.show()

    def _update_selection_items(self) -> None:
        if not self.selected_id or self._frame is None:
            self.selection_item.hide()
            self.move_item.hide()
            return
        bounds = self._frame.bounds.get(self.selected_id)
        if bounds is None:
            self.selection_item.hide()
            self.move_item.hide()
            return
        left, top, right, bottom = bounds
        self.selection_item.setRect(QRectF(left, top, right - left, bottom - top))
        dashboard = self.model.dashboard(self.selected_id)
        self.move_item.setPos(
            dashboard.anchor.x * self.model.config.output.width,
            dashboard.anchor.y * self.model.config.output.height,
        )
        self.selection_item.show()
        self.move_item.show()

    def _hit_dashboard(self, point: QPointF) -> str | None:
        if self._frame is None:
            return None
        for dashboard in self.model.config.dashboards:
            bounds = self._frame.bounds.get(dashboard.id)
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            if left <= point.x() <= right and top <= point.y() <= bottom:
                return dashboard.id
        return None

    def _move_widget_hit(self, event: QMouseEvent) -> bool:
        if not self.selected_id or not self.move_item.isVisible():
            return False
        dashboard = self.model.dashboard(self.selected_id)
        anchor = self.mapFromScene(
            dashboard.anchor.x * self.model.config.output.width,
            dashboard.anchor.y * self.model.config.output.height,
        )
        point = event.position()
        return abs(point.x() - anchor.x()) <= 22 and abs(point.y() - anchor.y()) <= 22

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.editing_enabled:
            super().mousePressEvent(event)
            return
        scene_point = self.mapToScene(event.position().toPoint())
        if self._move_widget_hit(event):
            dashboard = self.model.dashboard(self.selected_id)  # type: ignore[arg-type]
            self._drag_mode = "move"
            self._drag_dashboard_id = self.selected_id
            self._drag_start_bounds = (
                self._frame.bounds.get(self.selected_id) if self._frame is not None else None
            )
            self._drag_start_scene = scene_point
            self._drag_start_anchor = QPointF(dashboard.anchor.x, dashboard.anchor.y)
            event.accept()
            return

        hit = self._hit_dashboard(scene_point)
        if hit != self.selected_id:
            self.dashboardSelected.emit(hit)
            event.accept()
            return
        if hit is None:
            self.dashboardSelected.emit(None)
            event.accept()
            return

        dashboard = self.model.dashboard(hit)
        anchor_scene = QPointF(
            dashboard.anchor.x * self.model.config.output.width,
            dashboard.anchor.y * self.model.config.output.height,
        )
        distance = math.hypot(
            scene_point.x() - anchor_scene.x(),
            scene_point.y() - anchor_scene.y(),
        )
        if distance < 1.0:
            event.accept()
            return
        self._drag_mode = "resize"
        self._drag_dashboard_id = hit
        self._drag_start_bounds = (
            self._frame.bounds.get(hit) if self._frame is not None else None
        )
        self._drag_start_scene = scene_point
        self._drag_start_distance = distance
        self._drag_start_size = (
            float(dashboard.font_size)
            if isinstance(dashboard, DashboardConfig)
            else float(dashboard.width)
        )
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode is None or not self.selected_id:
            super().mouseMoveEvent(event)
            return
        scene_point = self.mapToScene(event.position().toPoint())
        dashboard = self.model.dashboard(self.selected_id)
        patch: dict[str, float | int] = {}
        if self._drag_mode == "move":
            delta = scene_point - self._drag_start_scene
            anchor_x = self._drag_start_anchor.x() + delta.x() / self.model.config.output.width
            anchor_y = self._drag_start_anchor.y() + delta.y() / self.model.config.output.height
            self.model.update_dashboard_geometry(
                self.selected_id,
                anchor_x=anchor_x,
                anchor_y=anchor_y,
                rebuild_runtime=False,
            )
            patch = {
                "anchor_x": dashboard.anchor.x,
                "anchor_y": dashboard.anchor.y,
            }
        else:
            anchor_x = dashboard.anchor.x * self.model.config.output.width
            anchor_y = dashboard.anchor.y * self.model.config.output.height
            distance = math.hypot(scene_point.x() - anchor_x, scene_point.y() - anchor_y)
            size = self._drag_start_size * distance / self._drag_start_distance
            self.model.update_dashboard_geometry(
                self.selected_id,
                size=size,
                rebuild_runtime=False,
            )
            if isinstance(dashboard, DashboardConfig):
                patch = {"font_size": dashboard.font_size}
            elif isinstance(dashboard, ElevationProfileDashboardConfig):
                patch = {"width": dashboard.width, "height": dashboard.height}
            else:
                patch = {"width": dashboard.width}
        self._update_drag_feedback()
        self._schedule_geometry_update(self.selected_id, patch)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_mode is not None:
            self._geometry_render_timer.stop()
            self._apply_pending_geometry_update()
            self._drag_mode = None
            self._drag_dashboard_id = None
            self._drag_start_bounds = None
            self.geometryCommitted.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_to_view:
            self._apply_preview_scale()
