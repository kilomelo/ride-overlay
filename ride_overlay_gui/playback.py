"""QMediaPlayer adapter that exposes several source clips as one timeline."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ride_overlay_video import VideoTimeline


@dataclass
class _PendingSeek:
    local_ms: int
    load_started: bool = False
    applied: bool = False


class VirtualPlaybackController(QObject):
    positionChanged = Signal(float)
    playingChanged = Signal(bool)
    errorOccurred = Signal(str)
    segmentChanged = Signal(int)

    def __init__(
        self,
        video_output: QObject,
        parent: QObject | None = None,
        *,
        _player: QMediaPlayer | None = None,
    ) -> None:
        super().__init__(parent)
        self.player = _player or QMediaPlayer(self)
        self.audio_output: QAudioOutput | None = None
        if _player is None:
            self.audio_output = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(video_output)
        self.timeline = VideoTimeline((), 0.0)
        self._segment_index = -1
        self._pending_seek: _PendingSeek | None = None
        self._audio_suppressed_for_priming = False
        # This is the state requested by the user. The backend temporarily
        # enters Stopped while changing sources, and briefly enters Playing to
        # decode a still frame for a paused cross-segment seek; neither should
        # change the play/pause button.
        self._desired_playing = False
        self._video_sink = None
        sink_getter = getattr(video_output, "videoSink", None)
        if callable(sink_getter):
            self._video_sink = sink_getter()
            self._video_sink.videoFrameChanged.connect(self._on_video_frame_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.seekableChanged.connect(self._on_seekable_changed)
        self.player.errorOccurred.connect(self._on_error)

    @property
    def is_playing(self) -> bool:
        return self._desired_playing

    @property
    def segment_index(self) -> int:
        return self._segment_index

    def _set_desired_playing(self, playing: bool) -> None:
        playing = bool(playing)
        if self._desired_playing == playing:
            return
        self._desired_playing = playing
        self.playingChanged.emit(playing)

    def _suppress_audio_for_priming(self, suppressed: bool) -> None:
        suppressed = bool(suppressed)
        if self._audio_suppressed_for_priming == suppressed:
            return
        self._audio_suppressed_for_priming = suppressed
        if self.audio_output is not None:
            self.audio_output.setMuted(suppressed)

    def set_timeline(self, timeline: VideoTimeline) -> None:
        self._set_desired_playing(False)
        self._suppress_audio_for_priming(False)
        self.player.stop()
        self.timeline = timeline
        self._segment_index = -1
        self._pending_seek = None
        if timeline.segments:
            self._switch_segment(0, 0)
        else:
            self.player.setSource(QUrl())
            self.positionChanged.emit(0.0)

    def _switch_segment(self, index: int, local_ms: int) -> None:
        self._segment_index = index
        # Install the guard before setSource: Qt normally emits position=0
        # synchronously while replacing the source.
        self._pending_seek = _PendingSeek(max(0, local_ms))
        source = self.timeline.segments[index].info.path
        self.player.setSource(QUrl.fromLocalFile(str(source)))
        self.segmentChanged.emit(index)

    def seek(self, global_seconds: float) -> None:
        if not self.timeline.segments:
            self.positionChanged.emit(min(max(global_seconds, 0.0), self.timeline.duration_seconds))
            return
        target = self.timeline.locate(global_seconds)
        local_ms = round(target.local_seconds * 1000)
        if target.index == self._segment_index:
            if self._pending_seek is not None:
                self._pending_seek.local_ms = local_ms
                self._pending_seek.applied = False
                self._try_apply_pending_seek()
            else:
                self.player.setPosition(local_ms)
                if self._desired_playing and self.player.playbackState() != (
                    QMediaPlayer.PlaybackState.PlayingState
                ):
                    self.player.play()
        else:
            self._switch_segment(target.index, local_ms)
        # Keep the editor and dashboards responsive immediately. Backend
        # position=0 events are suppressed until the requested frame arrives.
        self.positionChanged.emit(target.segment.start_seconds + target.local_seconds)

    def play(self) -> None:
        if not self.timeline.segments:
            return
        if self.current_position() >= self.timeline.duration_seconds - 0.001:
            self.seek(0.0)
        self._set_desired_playing(True)
        self._suppress_audio_for_priming(False)
        if self._pending_seek is not None:
            self._try_apply_pending_seek()
        else:
            self.player.play()

    def pause(self) -> None:
        self._set_desired_playing(False)
        if self._pending_seek is None:
            self.player.pause()
        else:
            self._suppress_audio_for_priming(True)
        # During a cross-segment seek the backend may need to play briefly to
        # decode the target frame. _finish_pending_seek pauses it once that
        # frame reaches the video sink.

    def toggle(self) -> None:
        if self._desired_playing:
            self.pause()
        else:
            self.play()

    def current_position(self) -> float:
        if self._segment_index < 0 or not self.timeline.segments:
            return 0.0
        segment = self.timeline.segments[self._segment_index]
        if self._pending_seek is not None:
            local_seconds = self._pending_seek.local_ms / 1000
        else:
            local_seconds = self.player.position() / 1000
        timeline_seconds = segment.start_seconds + local_seconds - segment.source_start_seconds
        return min(self.timeline.duration_seconds, timeline_seconds)

    def _try_apply_pending_seek(self) -> None:
        pending = self._pending_seek
        if pending is None or pending.applied:
            return
        # setSource can deliver late Buffered/Loaded events from the previous
        # source before the new source reports LoadingMedia. Applying the seek
        # in that window targets the old decoder and leaves the new clip at 0.
        if not pending.load_started:
            return
        ready = {
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        }
        if self.player.mediaStatus() not in ready:
            return
        if pending.local_ms > 0 and not self.player.isSeekable():
            return
        pending.applied = True
        self.player.setPosition(pending.local_ms)
        # Playing is also required when the logical state is paused: a newly
        # loaded source otherwise does not necessarily decode a visible frame.
        self._suppress_audio_for_priming(not self._desired_playing)
        self.player.play()

    @staticmethod
    def _frame_is_valid(frame: object) -> bool:
        checker = getattr(frame, "isValid", None)
        return not callable(checker) or bool(checker())

    def _on_video_frame_changed(self, frame: object) -> None:
        pending = self._pending_seek
        if pending is None or not pending.applied or not self._frame_is_valid(frame):
            return
        actual_ms = self.player.position()
        if abs(actual_ms - pending.local_ms) > 100:
            # Some backends deliver the source's first frame before applying a
            # non-zero seek. Reapply it and keep suppressing transient position
            # updates rather than exposing the segment start to the UI.
            self.player.setPosition(pending.local_ms)
            return
        self._finish_pending_seek()

    def _finish_pending_seek(self) -> None:
        pending = self._pending_seek
        if pending is None:
            return
        segment = self.timeline.segments[self._segment_index]
        self._pending_seek = None
        self.positionChanged.emit(
            segment.start_seconds + pending.local_ms / 1000 - segment.source_start_seconds
        )
        if not self._desired_playing:
            self.player.pause()
        self._suppress_audio_for_priming(False)

    def _on_position_changed(self, local_ms: int) -> None:
        if self._segment_index < 0 or self._pending_seek is not None:
            return
        segment = self.timeline.segments[self._segment_index]
        next_index = self._segment_index + 1
        is_trimmed_join = (
            next_index < len(self.timeline.segments)
            and segment.effective_source_end_seconds < segment.info.duration_seconds
        )
        frame_seconds = 1.0 / segment.info.fps if segment.info.fps else 0.0
        switch_threshold_ms = round(
            (segment.effective_source_end_seconds - frame_seconds / 2) * 1000
        )
        if self._desired_playing and is_trimmed_join and local_ms >= switch_threshold_ms:
            self.positionChanged.emit(segment.end_seconds)
            self._switch_segment(next_index, 0)
            return
        timeline_seconds = (
            segment.start_seconds + local_ms / 1000 - segment.source_start_seconds
        )
        self.positionChanged.emit(min(timeline_seconds, segment.end_seconds))

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        # Ignore state changes caused by setSource and paused-frame priming.
        # The UI reflects _desired_playing, which only user actions, final EOF,
        # and actual playback errors are allowed to change.
        if (
            state == QMediaPlayer.PlaybackState.PlayingState
            and not self._desired_playing
            and self._pending_seek is None
        ):
            self.player.pause()

    def _on_seekable_changed(self, _seekable: bool) -> None:
        self._try_apply_pending_seek()

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if self._pending_seek is not None:
            if status == QMediaPlayer.MediaStatus.LoadingMedia:
                self._pending_seek.load_started = True
                self._pending_seek.applied = False
                return
            if status in {
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            }:
                self._try_apply_pending_seek()
            # In particular, ignore a delayed EndOfMedia from the old source.
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.timeline.segments:
            next_index = self._segment_index + 1
            if next_index < len(self.timeline.segments):
                self._switch_segment(next_index, 0)
            else:
                self.positionChanged.emit(self.timeline.duration_seconds)
                self._set_desired_playing(False)
                self.player.pause()

    def _on_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        self._pending_seek = None
        self._suppress_audio_for_priming(False)
        self._set_desired_playing(False)
        self.errorOccurred.emit(message or "视频播放失败")
