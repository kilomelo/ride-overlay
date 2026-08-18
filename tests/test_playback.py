from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer

from ride_overlay_gui.playback import VirtualPlaybackController
from ride_overlay_video import VideoInfo, VideoSegment, VideoTimeline


class FakeFrame:
    def isValid(self) -> bool:
        return True


class FakeVideoSink(QObject):
    videoFrameChanged = Signal(object)


class FakeVideoOutput(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.sink = FakeVideoSink(self)

    def videoSink(self) -> FakeVideoSink:
        return self.sink


class FakeMediaPlayer(QObject):
    positionChanged = Signal(int)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    seekableChanged = Signal(bool)
    errorOccurred = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self._position = 0
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._status = QMediaPlayer.MediaStatus.NoMedia
        self._seekable = False
        self.pause_count = 0
        self.play_count = 0

    def setVideoOutput(self, _output: QObject) -> None:
        pass

    def setSource(self, _url: QUrl) -> None:
        self._position = 0
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._status = QMediaPlayer.MediaStatus.LoadingMedia
        self._seekable = False
        self.playbackStateChanged.emit(self._state)
        self.positionChanged.emit(0)
        self.mediaStatusChanged.emit(self._status)

    def stop(self) -> None:
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self.playbackStateChanged.emit(self._state)

    def play(self) -> None:
        self.play_count += 1
        self._state = QMediaPlayer.PlaybackState.PlayingState
        self.playbackStateChanged.emit(self._state)

    def pause(self) -> None:
        self.pause_count += 1
        self._state = QMediaPlayer.PlaybackState.PausedState
        self.playbackStateChanged.emit(self._state)

    def setPosition(self, value: int) -> None:
        self._position = value
        self.positionChanged.emit(value)

    def position(self) -> int:
        return self._position

    def playbackState(self) -> QMediaPlayer.PlaybackState:
        return self._state

    def mediaStatus(self) -> QMediaPlayer.MediaStatus:
        return self._status

    def isSeekable(self) -> bool:
        return self._seekable

    def complete_load(self) -> None:
        self._seekable = True
        self.seekableChanged.emit(True)
        self._status = QMediaPlayer.MediaStatus.LoadedMedia
        self.mediaStatusChanged.emit(self._status)

    def stale_end_of_media(self) -> None:
        self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)

    def late_source_position_reset(self) -> None:
        self._position = 0
        self.positionChanged.emit(0)


def _timeline() -> VideoTimeline:
    first_info = VideoInfo(Path("a.mp4"), 10.0, 1920, 1080, 30.0, True)
    second_info = VideoInfo(Path("b.mp4"), 10.0, 1920, 1080, 30.0, True)
    return VideoTimeline(
        (
            VideoSegment(first_info, 0.0, 10.0),
            VideoSegment(second_info, 10.0, 20.0),
        ),
        20.0,
    )


def _loaded_controller(
    qtbot,
) -> tuple[VirtualPlaybackController, FakeMediaPlayer, FakeVideoOutput]:
    del qtbot
    player = FakeMediaPlayer()
    output = FakeVideoOutput()
    controller = VirtualPlaybackController(output, _player=player)  # type: ignore[arg-type]
    controller.set_timeline(_timeline())
    player.complete_load()
    output.sink.videoFrameChanged.emit(FakeFrame())
    return controller, player, output


def test_paused_cross_segment_seek_stays_paused_and_ignores_source_position_zero(qtbot) -> None:
    controller, player, output = _loaded_controller(qtbot)
    positions: list[float] = []
    states: list[bool] = []
    controller.positionChanged.connect(positions.append)
    controller.playingChanged.connect(states.append)
    controller.play()
    controller.pause()
    positions.clear()
    states.clear()

    controller.seek(14.25)

    assert controller.is_playing is False
    assert controller.current_position() == pytest.approx(14.25)
    assert positions == [pytest.approx(14.25)]
    assert states == []

    player.complete_load()
    assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    assert controller.is_playing is False
    player.late_source_position_reset()
    assert all(position == pytest.approx(14.25) for position in positions)
    output.sink.videoFrameChanged.emit(FakeFrame())
    output.sink.videoFrameChanged.emit(FakeFrame())

    assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    assert controller.is_playing is False
    assert controller.current_position() == pytest.approx(14.25)
    assert all(position == pytest.approx(14.25) for position in positions)
    assert states == []


def test_playing_cross_segment_seek_keeps_playing_and_ignores_stale_end(qtbot) -> None:
    controller, player, output = _loaded_controller(qtbot)
    positions: list[float] = []
    states: list[bool] = []
    controller.positionChanged.connect(positions.append)
    controller.playingChanged.connect(states.append)
    controller.play()
    positions.clear()
    states.clear()

    controller.seek(13.5)
    player.stale_end_of_media()

    assert controller.segment_index == 1
    assert controller.is_playing is True
    assert positions == [pytest.approx(13.5)]
    assert states == []

    player.complete_load()
    player.late_source_position_reset()
    assert all(position == pytest.approx(13.5) for position in positions)
    output.sink.videoFrameChanged.emit(FakeFrame())
    output.sink.videoFrameChanged.emit(FakeFrame())

    assert controller.segment_index == 1
    assert controller.is_playing is True
    assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    assert all(position == pytest.approx(13.5) for position in positions)
    assert states == []
