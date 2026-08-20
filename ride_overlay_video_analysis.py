"""Video-only overlap analysis and editor configuration preparation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

from ride_overlay import (
    APP_VERSION,
    CONFIG_FILENAME,
    AppConfig,
    ConfigError,
    load_config,
    resolve_paths,
)
from ride_overlay_video import VideoInfo, probe_video

ANALYSIS_VERSION = 1
ANALYSIS_WIDTH = 160
MAX_SEARCH_SECONDS = 3.0
MAX_SEARCH_FRAMES = 120
VIDEO_ANALYSIS_REPORT_FILENAME = "video-analysis.log"
VIDEO_ANALYSIS_CACHE_FILENAME = ".video-analysis-cache.json"


class VideoAnalysisError(ConfigError):
    """Video overlap analysis could not be completed."""


@dataclass(frozen=True)
class VideoJoinAnalysis:
    previous_file: str
    next_file: str
    detected_overlap_frames: int
    applied_overlap_frames: int
    confidence: float
    method: str
    mean_similarity: float | None = None
    worst_similarity: float | None = None
    peak_margin: float | None = None
    exact_candidate_count: int = 0
    visual_activity: float | None = None
    warning: str | None = None
    fallback_reason: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> VideoJoinAnalysis:
        names = cls.__dataclass_fields__.keys()
        return cls(**{name: raw[name] for name in names if name in raw})


@dataclass(frozen=True)
class VideoPreparationResult:
    config_changed: bool
    report_path: Path
    cache_path: Path
    analyses: tuple[VideoJoinAnalysis, ...]


def _relative_name(project: Path, path: Path) -> str:
    return path.resolve().relative_to(project.resolve()).as_posix()


def _file_signature(project: Path, info: VideoInfo) -> dict[str, object]:
    stat = info.path.stat()
    return {
        "file": _relative_name(project, info.path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration_seconds": round(info.duration_seconds, 6),
        "width": info.width,
        "height": info.height,
        "fps": round(info.fps, 9) if info.fps is not None else None,
    }


def _atomic_write_json(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _analysis_dimensions(info: VideoInfo) -> tuple[int, int]:
    height = max(2, round(info.height * ANALYSIS_WIDTH / info.width / 2) * 2)
    return ANALYSIS_WIDTH, height


def _decode_analysis_frames(info: VideoInfo, *, tail: bool) -> list[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoAnalysisError("找不到 ffmpeg，无法分析视频拼接处的重复帧")
    fps = info.fps or 30.0
    requested = min(MAX_SEARCH_FRAMES, max(2, math.ceil(MAX_SEARCH_SECONDS * fps)))
    seconds = requested / fps + 0.75
    width, height = _analysis_dimensions(info)
    command = [ffmpeg, "-v", "error"]
    if tail:
        command.extend(["-sseof", f"-{min(info.duration_seconds, seconds):.6f}"])
    command.extend(["-i", str(info.path)])
    if not tail:
        command.extend(["-t", f"{min(info.duration_seconds, seconds):.6f}"])
    command.extend(
        [
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"scale={width}:{height}:flags=area,format=gray",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise VideoAnalysisError(
            f"读取视频拼接分析帧失败 {info.path.name}: {detail or result.returncode}"
        )
    frame_size = width * height
    frames = [
        result.stdout[offset : offset + frame_size]
        for offset in range(0, len(result.stdout) - frame_size + 1, frame_size)
    ]
    if not frames:
        raise VideoAnalysisError(f"视频没有可用于拼接分析的画面: {info.path.name}")
    return frames[-requested:] if tail else frames[:requested]


def _frame_similarity(first: bytes, second: bytes, size: tuple[int, int]) -> float:
    difference = ImageChops.difference(
        Image.frombytes("L", size, first),
        Image.frombytes("L", size, second),
    )
    histogram = difference.histogram()
    mae = sum(value * count for value, count in enumerate(histogram)) / len(first)
    return max(0.0, 1.0 - mae / 255.0)


def _mean_visual_activity(frames: list[bytes], size: tuple[int, int]) -> float:
    if len(frames) < 2:
        return 0.0
    differences = [
        1.0 - _frame_similarity(first, second, size)
        for first, second in zip(frames, frames[1:], strict=False)
    ]
    return sum(differences) / len(differences)


def analyze_frame_windows(
    previous_file: str,
    next_file: str,
    previous_frames: list[bytes],
    next_frames: list[bytes],
    size: tuple[int, int],
) -> VideoJoinAnalysis:
    """Match the previous tail against the next head without using audio."""

    limit = min(len(previous_frames), len(next_frames))
    if limit == 0:
        raise VideoAnalysisError(f"拼接处没有足够的视频帧: {previous_file} -> {next_file}")
    previous_frames = previous_frames[-limit:]
    next_frames = next_frames[:limit]
    hashes_previous = [hashlib.sha256(frame).digest() for frame in previous_frames]
    hashes_next = [hashlib.sha256(frame).digest() for frame in next_frames]
    exact_candidates = [
        count
        for count in range(1, limit + 1)
        if hashes_previous[-count:] == hashes_next[:count]
    ]
    activity_frames = previous_frames[-min(30, limit) :] + next_frames[: min(30, limit)]
    visual_activity = _mean_visual_activity(activity_frames, size)
    if exact_candidates:
        detected = max(exact_candidates)
        ambiguous = len(exact_candidates) > 3 and max(exact_candidates) - min(exact_candidates) > 2
        if ambiguous:
            return VideoJoinAnalysis(
                previous_file,
                next_file,
                detected,
                0,
                0.2,
                "ambiguous_exact_static",
                1.0,
                1.0,
                0.0,
                len(exact_candidates),
                visual_activity,
                "连续静止或重复画面产生多个精确候选，未自动裁切",
            )
        confidence = min(1.0, 0.985 + min(visual_activity, 0.015))
        return VideoJoinAnalysis(
            previous_file,
            next_file,
            detected,
            detected,
            confidence,
            "exact_video_frames",
            1.0,
            1.0,
            None,
            len(exact_candidates),
            visual_activity,
        )

    first_similarities = [
        (count, _frame_similarity(previous_frames[-count], next_frames[0], size))
        for count in range(1, limit + 1)
    ]
    strongest = sorted(first_similarities, key=lambda item: item[1], reverse=True)[:12]
    candidates: list[tuple[int, float, float]] = []
    for count, _first_similarity in strongest:
        similarities = [
            _frame_similarity(first, second, size)
            for first, second in zip(
                previous_frames[-count:], next_frames[:count], strict=True
            )
        ]
        candidates.append((count, sum(similarities) / count, min(similarities)))
    candidates.sort(key=lambda item: (item[1], item[2], item[0]), reverse=True)
    detected, mean_similarity, worst_similarity = candidates[0]
    second_mean = candidates[1][1] if len(candidates) > 1 else 0.0
    peak_margin = mean_similarity - second_mean
    accepted = (
        detected >= 2
        and mean_similarity >= 0.995
        and worst_similarity >= 0.985
        and peak_margin >= 0.0005
    )
    if accepted:
        confidence = min(0.95, 0.55 + (mean_similarity - 0.995) * 40 + peak_margin * 40)
        return VideoJoinAnalysis(
            previous_file,
            next_file,
            detected,
            detected,
            confidence,
            "approximate_video_frames",
            mean_similarity,
            worst_similarity,
            peak_margin,
            0,
            visual_activity,
        )
    return VideoJoinAnalysis(
        previous_file,
        next_file,
        0,
        0,
        max(0.0, min(0.49, peak_margin * 40)),
        "no_reliable_video_match",
        mean_similarity,
        worst_similarity,
        peak_margin,
        0,
        visual_activity,
        f"最佳候选为 {detected} 帧，但画面证据不足，按 0 帧处理",
    )


def analyze_video_join(project: Path, previous: VideoInfo, next_: VideoInfo) -> VideoJoinAnalysis:
    previous_name = _relative_name(project, previous.path)
    next_name = _relative_name(project, next_.path)
    if previous.fps is None or next_.fps is None:
        return VideoJoinAnalysis(
            previous_name,
            next_name,
            0,
            0,
            0.0,
            "missing_frame_rate",
            warning="无法确定视频帧率，未自动检测重复帧",
        )
    if not math.isclose(previous.fps, next_.fps, rel_tol=0.001, abs_tol=0.001):
        return VideoJoinAnalysis(
            previous_name,
            next_name,
            0,
            0,
            0.0,
            "frame_rate_mismatch",
            warning=f"相邻视频帧率不同（{previous.fps:g} / {next_.fps:g}），未自动检测",
        )
    size = _analysis_dimensions(previous)
    if size != _analysis_dimensions(next_):
        return VideoJoinAnalysis(
            previous_name,
            next_name,
            0,
            0,
            0.0,
            "aspect_ratio_mismatch",
            warning="相邻视频画面比例不同，未自动检测重复帧",
        )
    return analyze_frame_windows(
        previous_name,
        next_name,
        _decode_analysis_frames(previous, tail=True),
        _decode_analysis_frames(next_, tail=False),
        size,
    )


def _apply_conservative_group_fallback(
    analyses: list[VideoJoinAnalysis],
) -> list[VideoJoinAnalysis]:
    reliable = sorted(
        item.applied_overlap_frames
        for item in analyses
        if item.applied_overlap_frames > 0 and item.confidence >= 0.8
    )
    if len(reliable) < 3:
        return analyses
    median = reliable[len(reliable) // 2]
    deviations = sorted(abs(value - median) for value in reliable)
    mad = deviations[len(deviations) // 2]
    cluster_radius = max(1, 2 * mad)
    cluster = [value for value in reliable if abs(value - median) <= cluster_radius]
    if len(cluster) < 3 or len(cluster) / len(reliable) < 0.75 or max(cluster) - min(cluster) > 3:
        return analyses
    replacement = round(sum(cluster) / len(cluster))
    result: list[VideoJoinAnalysis] = []
    for item in analyses:
        is_low_confidence_outlier = (
            item.applied_overlap_frames > 0
            and item.confidence < 0.65
            and abs(item.applied_overlap_frames - median) > max(4, 4 * mad)
        )
        if not is_low_confidence_outlier:
            result.append(item)
            continue
        raw = asdict(item)
        raw["applied_overlap_frames"] = replacement
        raw["fallback_reason"] = (
            f"低置信度非零结果明显偏离主簇，使用主簇平均值 {replacement} 帧"
        )
        result.append(VideoJoinAnalysis(**raw))
    return result


def _cache_matches(
    cache: dict[str, Any] | None,
    signatures: list[dict[str, object]],
) -> bool:
    return bool(
        cache
        and cache.get("analysis_version") == ANALYSIS_VERSION
        and cache.get("file_signatures") == signatures
        and isinstance(cache.get("analyses"), list)
    )


def _write_analysis_report(
    report_path: Path,
    project: Path,
    infos: list[VideoInfo],
    configured_joins: list[dict[str, Any]],
    analyses: list[VideoJoinAnalysis],
    sources: dict[tuple[str, str], str],
) -> None:
    analysis_by_pair = {(item.previous_file, item.next_file): item for item in analyses}
    lines = [
        "ride-overlay 视频拼接分析报告",
        "=" * 72,
        f"generated_at: {datetime.now().astimezone().isoformat(timespec='milliseconds')}",
        f"version: {APP_VERSION}",
        f"analysis_version: {ANALYSIS_VERSION}",
        f"project: {project}",
        "matching_basis: video_frames_only",
        "audio_matching: disabled",
        "trim_policy: 从每个连接处前一个视频的末尾裁掉 overlap_frames；音频同步裁切",
        "",
        "[视频列表]",
    ]
    for index, info in enumerate(infos, start=1):
        lines.append(
            f"{index:02d}. {_relative_name(project, info.path)} | "
            f"duration={info.duration_seconds:.6f}s | {info.width}x{info.height} | "
            f"fps={info.fps if info.fps is not None else '-'} | audio={info.has_audio}"
        )
    lines.extend(["", "[拼接结果]"])
    total_trim_seconds = 0.0
    for index, join in enumerate(configured_joins, start=1):
        pair = (join["previous_file"], join["next_file"])
        configured = int(join["overlap_frames"])
        previous_info = infos[index - 1]
        trim_seconds = configured / previous_info.fps if previous_info.fps else 0.0
        total_trim_seconds += trim_seconds
        lines.append(
            f"{index:02d}. {pair[0]} -> {pair[1]} | overlap_frames={configured} | "
            f"trim_seconds={trim_seconds:.6f} | value_source={sources.get(pair, 'config')}"
        )
        analysis = analysis_by_pair.get(pair)
        if analysis is None:
            lines.append("    analysis: 无缓存分析记录；当前值来自 config.json")
            continue
        lines.append(
            f"    analysis: method={analysis.method} | "
            f"detected={analysis.detected_overlap_frames} | "
            f"auto_applied={analysis.applied_overlap_frames} | "
            f"confidence={analysis.confidence:.4f} | exact_candidates="
            f"{analysis.exact_candidate_count}"
        )
        lines.append(
            "    metrics: "
            f"mean_similarity={analysis.mean_similarity} | "
            f"worst_similarity={analysis.worst_similarity} | "
            f"peak_margin={analysis.peak_margin} | "
            f"visual_activity={analysis.visual_activity}"
        )
        if analysis.warning:
            lines.append(f"    warning: {analysis.warning}")
        if analysis.fallback_reason:
            lines.append(f"    fallback: {analysis.fallback_reason}")
    raw_duration = sum(item.duration_seconds for item in infos)
    lines.extend(
        [
            "",
            "[汇总]",
            f"video_count: {len(infos)}",
            f"join_count: {len(configured_joins)}",
            f"raw_duration_seconds: {raw_duration:.6f}",
            f"trimmed_duplicate_seconds: {total_trim_seconds:.6f}",
            f"effective_duration_seconds: {raw_duration - total_trim_seconds:.6f}",
            "",
            "overlap_frames 可在 config.json 的 timeline.video_joins 中手动修改。",
            "修改后编辑器会重新读取，并立即重建预览时间轴；不会仅因手动修改而重新分析。",
            "",
        ]
    )
    _atomic_write_text(report_path, "\n".join(lines))


def prepare_editor_video_configuration(project_dir: Path) -> VideoPreparationResult:
    """Discover videos, fill missing joins, cache analysis, and write a report."""

    project = project_dir.expanduser().resolve()
    config_path = project / CONFIG_FILENAME
    raw = _read_json_object(config_path)
    if raw is None:
        # Produce the same actionable validation error as the regular loader.
        load_config(project)
        raise VideoAnalysisError(f"无法读取 {config_path}")
    config = load_config(project)
    paths = resolve_paths(project, config)
    infos = [probe_video(path) for path in paths.videos]
    names = [_relative_name(project, info.path) for info in infos]
    export_dir = paths.export_dir
    report_path = export_dir / VIDEO_ANALYSIS_REPORT_FILENAME
    cache_path = export_dir / VIDEO_ANALYSIS_CACHE_FILENAME
    signatures = [_file_signature(project, info) for info in infos]
    cache = _read_json_object(cache_path)
    cached_analyses: list[VideoJoinAnalysis] = []
    if _cache_matches(cache, signatures):
        try:
            cached_analyses = [VideoJoinAnalysis.from_dict(item) for item in cache["analyses"]]
        except (KeyError, TypeError, ValueError):
            cached_analyses = []
    cached_by_pair = {(item.previous_file, item.next_file): item for item in cached_analyses}

    inputs = raw.setdefault("inputs", {})
    timeline = raw.setdefault("timeline", {})
    old_video_files = inputs.get("video_files")
    old_joins = timeline.get("video_joins", [])
    existing_by_pair = {
        (item.get("previous_file"), item.get("next_file")): item
        for item in old_joins
        if isinstance(item, dict)
    }
    adjacent_pairs = list(zip(names, names[1:], strict=False))
    missing_pairs = [pair for pair in adjacent_pairs if pair not in existing_by_pair]

    analyses = list(cached_analyses)
    if missing_pairs:
        info_by_name = {name: info for name, info in zip(names, infos, strict=True)}
        for previous_name, next_name in missing_pairs:
            pair = (previous_name, next_name)
            if pair in cached_by_pair:
                continue
            try:
                result = analyze_video_join(
                    project, info_by_name[previous_name], info_by_name[next_name]
                )
            except (OSError, VideoAnalysisError) as exc:
                result = VideoJoinAnalysis(
                    previous_name,
                    next_name,
                    0,
                    0,
                    0.0,
                    "analysis_failed",
                    warning=f"自动分析失败，按 0 帧处理: {exc}",
                )
            analyses.append(result)
        analyses = _apply_conservative_group_fallback(analyses)
        cache_payload = {
            "analysis_version": ANALYSIS_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "file_signatures": signatures,
            "analyses": [asdict(item) for item in analyses],
        }
        _atomic_write_json(cache_path, cache_payload)
    analysis_by_pair = {(item.previous_file, item.next_file): item for item in analyses}

    configured_joins: list[dict[str, Any]] = []
    sources: dict[tuple[str, str], str] = {}
    for previous_name, next_name in adjacent_pairs:
        pair = (previous_name, next_name)
        existing = existing_by_pair.get(pair)
        if existing is not None:
            overlap = int(existing.get("overlap_frames", 0))
            sources[pair] = "config"
        else:
            overlap = analysis_by_pair.get(
                pair,
                VideoJoinAnalysis(previous_name, next_name, 0, 0, 0.0, "not_analyzed"),
            ).applied_overlap_frames
            sources[pair] = "automatic_analysis"
        configured_joins.append(
            {
                "previous_file": previous_name,
                "next_file": next_name,
                "overlap_frames": overlap,
            }
        )

    inputs["video_files"] = names
    timeline["video_joins"] = configured_joins
    raw["schema_version"] = 2
    try:
        AppConfig.model_validate(raw)
    except Exception as exc:
        raise VideoAnalysisError(f"自动生成的视频拼接配置无效: {exc}") from exc
    changed = (
        old_video_files != names
        or old_joins != configured_joins
        or config.schema_version != 2
    )
    if changed:
        _atomic_write_json(config_path, raw)
    _write_analysis_report(
        report_path,
        project,
        infos,
        configured_joins,
        analyses,
        sources,
    )
    return VideoPreparationResult(changed, report_path, cache_path, tuple(analyses))
