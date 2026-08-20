"""Dual activity/video timeline and frame-repeat alignment controls."""

from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QToolButton, QWidget


def format_clock(seconds: float) -> str:
    total = max(0, math.floor(seconds + 1e-9))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_timecode(seconds: float, fps: float) -> str:
    """Format an elapsed position as HH:MM:SS:FF using the output frame rate."""

    value = max(0.0, seconds)
    total_seconds = math.floor(value + 1e-9)
    nominal_fps = max(1, round(fps))
    frame = round((value - total_seconds) * fps)
    if frame >= nominal_fps:
        carry, frame = divmod(frame, nominal_fps)
        total_seconds += carry
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    frame_width = max(2, len(str(nominal_fps - 1)))
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frame:0{frame_width}d}"


def frame_step_target(
    current_seconds: float,
    total_seconds: float,
    fps: float,
    direction: int,
) -> float:
    """Return the nearest output-frame position one step left or right."""

    duration = max(0.0, total_seconds)
    current = min(max(current_seconds, 0.0), duration)
    current_frame = round(current * fps)
    target_frame = max(0, current_frame + (-1 if direction < 0 else 1))
    return min(duration, target_frame / fps)


def format_offset_frames(frames: int, fps: float) -> str:
    sign = "+" if frames >= 0 else "-"
    absolute = abs(frames)
    if math.isclose(fps, round(fps), abs_tol=1e-9):
        nominal = round(fps)
        total_seconds, frame = divmod(absolute, nominal)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}:{frame:02d}"
    seconds_value = absolute / fps
    total_milliseconds = round(seconds_value * 1000)
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d} ({absolute} 帧, NDF)"


class RepeatStepButton(QToolButton):
    stepRequested = Signal(int)

    def __init__(self, direction: int, parent=None) -> None:
        super().__init__(parent)
        self.direction = -1 if direction < 0 else 1
        self._elapsed = QElapsedTimer()
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(400)
        self._delay.timeout.connect(self._start_repeat)
        self._repeat = QTimer(self)
        self._repeat.setInterval(67)
        self._repeat.timeout.connect(self._repeat_step)
        self.pressed.connect(self._press)
        self.released.connect(self._release)

    def _press(self) -> None:
        self._elapsed.start()
        self.stepRequested.emit(self.direction)
        self._delay.start()

    def _release(self) -> None:
        self._delay.stop()
        self._repeat.stop()

    def _start_repeat(self) -> None:
        self._repeat.setInterval(67)
        self._repeat.start()

    def _repeat_step(self) -> None:
        if self._elapsed.elapsed() >= 2400 and self._repeat.interval() != 17:
            self._repeat.setInterval(17)
        self.stepRequested.emit(self.direction)


class TimelineWidget(QWidget):
    seekRequested = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.total_seconds = 0.0
        self.current_seconds = 0.0
        self.activity_duration_seconds = 0.0
        self.offset_frames = 0
        self.fps = 30.0
        self.join_times: tuple[float, ...] = ()
        self.setMinimumHeight(82)

    def set_timeline(
        self,
        *,
        total_seconds: float,
        activity_duration_seconds: float,
        offset_frames: int,
        fps: float,
        join_times: tuple[float, ...],
    ) -> None:
        self.total_seconds = max(0.0, total_seconds)
        self.activity_duration_seconds = max(0.0, activity_duration_seconds)
        self.offset_frames = offset_frames
        self.fps = fps
        self.join_times = join_times
        self.current_seconds = min(self.current_seconds, self.total_seconds)
        self.update()

    def set_current_time(self, seconds: float) -> None:
        self.current_seconds = min(max(seconds, 0.0), self.total_seconds)
        self.update()

    def _track_rect(self) -> QRectF:
        return QRectF(12, 8, max(1, self.width() - 24), max(1, self.height() - 16))

    def _x_at(self, seconds: float, track: QRectF) -> float:
        if self.total_seconds <= 0:
            return track.left()
        return track.left() + seconds / self.total_seconds * track.width()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = self._track_rect()
        activity_track = QRectF(outer.left(), outer.top() + 5, outer.width(), 20)
        video_track = QRectF(outer.left(), outer.top() + 43, outer.width(), 20)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#4D6B9C"))
        painter.drawRoundedRect(activity_track, 10, 10)
        painter.drawRoundedRect(video_track, 10, 10)
        painter.setBrush(QColor("#2F80ED"))
        painter.drawRoundedRect(video_track, 10, 10)

        if self.total_seconds > 0 and self.activity_duration_seconds > 0:
            start = self.offset_frames / self.fps
            end = start + self.activity_duration_seconds
            clipped_start = min(max(start, 0.0), self.total_seconds)
            clipped_end = min(max(end, 0.0), self.total_seconds)
            if clipped_end > clipped_start:
                green = QRectF(
                    self._x_at(clipped_start, activity_track),
                    activity_track.top(),
                    self._x_at(clipped_end, activity_track)
                    - self._x_at(clipped_start, activity_track),
                    activity_track.height(),
                )
                painter.setBrush(QColor("#45C875"))
                painter.drawRoundedRect(green, 10, 10)

        painter.setBrush(QColor("#F4C542"))
        for join_time in self.join_times:
            x = self._x_at(join_time, video_track)
            painter.drawEllipse(QRectF(x - 6, video_track.center().y() - 6, 12, 12))

        x = self._x_at(self.current_seconds, outer)
        marker_pen = QPen(QColor("#FFFFFF"), 4)
        marker_pen.setCosmetic(True)
        painter.setPen(marker_pen)
        painter.drawLine(round(x), round(outer.top()), round(x), round(outer.bottom()))

    def _seek_from_event(self, event: QMouseEvent) -> None:
        if self.total_seconds <= 0:
            return
        track = self._track_rect()
        ratio = (event.position().x() - track.left()) / track.width()
        self.seekRequested.emit(min(max(ratio, 0.0), 1.0) * self.total_seconds)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._seek_from_event(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._seek_from_event(event)
            event.accept()
            return
        super().mouseMoveEvent(event)
