"""Bidirectional, debounced synchronization with a human-edited config.json."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import QFileSystemWatcher, QIODevice, QObject, QSaveFile, QTimer, Signal

from ride_overlay import CONFIG_FILENAME, AppConfig


class ConfigSynchronizer(QObject):
    configChanged = Signal()
    configError = Signal(str)
    writeError = Signal(str)

    def __init__(self, project_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = project_dir / CONFIG_FILENAME
        self._raw = self._read_raw()
        self._last_seen_hash = self._hash(self.path.read_bytes())
        self._dirty = False
        self._last_self_hash: str | None = None
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._schedule_external_read)
        self._watcher.directoryChanged.connect(self._schedule_external_read)
        self._watcher.addPath(str(self.path.parent))
        self._ensure_file_watch()
        self._read_timer = QTimer(self)
        self._read_timer.setSingleShot(True)
        self._read_timer.setInterval(200)
        self._read_timer.timeout.connect(self._read_external)
        self._write_timer = QTimer(self)
        self._write_timer.setSingleShot(True)
        self._write_timer.setInterval(80)
        self._write_timer.timeout.connect(self.flush)

    @staticmethod
    def _hash(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def _read_raw(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        AppConfig.model_validate(raw)
        return raw

    def _ensure_file_watch(self) -> None:
        path = str(self.path)
        if self.path.is_file() and path not in self._watcher.files():
            self._watcher.addPath(path)

    def _schedule_external_read(self, _path: str) -> None:
        self._ensure_file_watch()
        self._read_timer.start()

    def _read_external(self) -> None:
        self._ensure_file_watch()
        try:
            payload = self.path.read_bytes()
            content_hash = self._hash(payload)
            if content_hash == self._last_self_hash:
                self._last_self_hash = None
                return
            if content_hash == self._last_seen_hash:
                return
            raw = json.loads(payload.decode("utf-8"))
            AppConfig.model_validate(raw)
        except json.JSONDecodeError as exc:
            self.configError.emit(
                f"config.json JSON 无效：第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}"
            )
            return
        except (OSError, UnicodeDecodeError, ValidationError) as exc:
            self.configError.emit(f"config.json 暂时无法使用：{exc}")
            return
        self._write_timer.stop()
        self._dirty = False
        self._last_self_hash = None
        self._last_seen_hash = content_hash
        self._raw = raw
        self.configChanged.emit()

    def _schedule_write(self, raw: dict[str, Any]) -> None:
        raw["schema_version"] = 2
        try:
            AppConfig.model_validate(raw)
        except ValidationError as exc:
            self.writeError.emit(f"拒绝写入无效配置：{exc}")
            return
        self._raw = raw
        self._dirty = True
        self._write_timer.start()

    def update_dashboard_geometry(
        self,
        dashboard_id: str,
        *,
        anchor_x: float | None = None,
        anchor_y: float | None = None,
        font_size: int | None = None,
        width: float | None = None,
    ) -> None:
        raw = deepcopy(self._raw)
        dashboard = next(
            (item for item in raw.get("dashboards", []) if item.get("id") == dashboard_id),
            None,
        )
        if dashboard is None:
            self.writeError.emit(f"config.json 中找不到仪表盘: {dashboard_id}")
            return
        anchor = dashboard.setdefault("anchor", {})
        if anchor_x is not None:
            anchor["x"] = round(float(anchor_x), 3)
        if anchor_y is not None:
            anchor["y"] = round(float(anchor_y), 3)
        if font_size is not None:
            dashboard["font_size"] = int(font_size)
        if width is not None:
            dashboard["width"] = round(float(width), 3)
        self._schedule_write(raw)

    def update_offset_frames(self, value: int) -> None:
        raw = deepcopy(self._raw)
        timeline = raw.setdefault("timeline", {})
        timeline["activity_start_offset_frames"] = int(value)
        self._schedule_write(raw)

    def flush(self) -> None:
        if not self._dirty:
            return
        self._write_timer.stop()
        payload = json.dumps(self._raw, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        target = QSaveFile(str(self.path))
        if not target.open(QIODevice.OpenModeFlag.WriteOnly):
            self.writeError.emit(f"无法写入 config.json：{target.errorString()}")
            return
        if target.write(payload) != len(payload):
            target.cancelWriting()
            self.writeError.emit(f"无法完整写入 config.json：{target.errorString()}")
            return
        if not target.commit():
            self.writeError.emit(f"无法提交 config.json：{target.errorString()}")
            return
        self._dirty = False
        self._last_self_hash = self._hash(payload)
        self._last_seen_hash = self._last_self_hash
        self._ensure_file_watch()
