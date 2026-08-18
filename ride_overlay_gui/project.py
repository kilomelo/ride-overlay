"""Pure project state shared by the Qt widgets and export worker."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ride_overlay import AppConfig, ResolvedPaths, load_config, resolve_paths
from ride_overlay_dashboard import (
    ClipRange,
    DashboardConfig,
    FrameRenderer,
    HeartbeatDashboardConfig,
    RuntimeDefinition,
    TrajectoryDashboardConfig,
    build_dashboard_runtimes,
    sample_dashboard_texts,
)
from ride_overlay_data import ActivityData, read_activity
from ride_overlay_video import VideoTimeline


@dataclass(frozen=True)
class EditorFrame:
    image: Image.Image
    values: tuple[str | float | None, ...]
    bounds: dict[str, tuple[float, float, float, float]]
    activity_time_seconds: float | None


@dataclass
class EditorProject:
    project_dir: Path
    config: AppConfig
    paths: ResolvedPaths
    activity: ActivityData
    clip: ClipRange
    runtimes: list[RuntimeDefinition]
    renderer: FrameRenderer
    video_timeline: VideoTimeline

    @classmethod
    def load(
        cls,
        project_dir: Path,
        previous: EditorProject | None = None,
    ) -> EditorProject:
        project = project_dir.expanduser().resolve()
        config = load_config(project)
        paths = resolve_paths(project, config)
        activity = (
            previous.activity
            if previous is not None and previous.paths.activity == paths.activity
            else read_activity(paths.activity)
        )
        clip = ClipRange(0.0, activity.duration_seconds)
        runtimes = build_dashboard_runtimes(config, activity, clip, paths=paths)
        renderer = FrameRenderer(config, paths)
        timeline = (
            previous.video_timeline
            if previous is not None and previous.video_timeline.paths == paths.videos
            else VideoTimeline.from_paths(paths.videos)
        )
        return cls(project, config, paths, activity, clip, runtimes, renderer, timeline)

    @property
    def display_duration_seconds(self) -> float:
        return self.video_timeline.duration_seconds or self.activity.duration_seconds

    @property
    def offset_frames(self) -> int:
        return self.config.timeline.activity_start_offset_frames

    def activity_time_at(self, video_time_seconds: float) -> float:
        return video_time_seconds - self.offset_frames / self.config.output.fps

    def frame_at(self, video_time_seconds: float) -> EditorFrame:
        activity_time = self.activity_time_at(video_time_seconds)
        if not 0.0 <= activity_time <= self.activity.duration_seconds:
            image = Image.new(
                "RGBA",
                (self.config.output.width, self.config.output.height),
                (0, 0, 0, 0),
            )
            return EditorFrame(image, tuple(None for _ in self.runtimes), {}, None)
        values = sample_dashboard_texts(self.runtimes, self.clip, activity_time)
        image = self.renderer.render_overlay(self.runtimes, values)
        bounds = self.renderer.dashboard_bounds_by_id(self.runtimes, values)
        return EditorFrame(image, values, bounds, activity_time)

    def dashboard(self, dashboard_id: str):
        return next(item for item in self.config.dashboards if item.id == dashboard_id)

    def update_dashboard_geometry(
        self,
        dashboard_id: str,
        *,
        anchor_x: float | None = None,
        anchor_y: float | None = None,
        size: float | int | None = None,
    ) -> None:
        dashboard = self.dashboard(dashboard_id)
        self.config.schema_version = 2
        if anchor_x is not None:
            dashboard.anchor.x = min(1.0, max(0.0, round(anchor_x, 3)))
        if anchor_y is not None:
            dashboard.anchor.y = min(1.0, max(0.0, round(anchor_y, 3)))
        if size is not None:
            if isinstance(dashboard, DashboardConfig):
                dashboard.font_size = min(2048, max(1, round(size)))
            elif isinstance(dashboard, (TrajectoryDashboardConfig, HeartbeatDashboardConfig)):
                dashboard.width = min(1.0, max(0.001, round(float(size), 3)))
        if not isinstance(dashboard, DashboardConfig):
            self.rebuild_dashboard_runtime()

    def set_offset_frames(self, value: int) -> None:
        self.config.schema_version = 2
        self.config.timeline.activity_start_offset_frames = int(value)

    def rebuild_dashboard_runtime(self) -> None:
        self.runtimes = build_dashboard_runtimes(
            self.config,
            self.activity,
            self.clip,
            paths=self.paths,
        )
        self.renderer = FrameRenderer(self.config, self.paths)
