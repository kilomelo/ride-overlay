"""Activity parsing, cleaning, interpolation, and metric derivation."""

from __future__ import annotations

import bisect
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import fitdecode
import gpxpy

LOGGER = logging.getLogger("ride-overlay")
MAX_INTERPOLATION_GAP_SECONDS = 5.0
ALTITUDE_SMOOTHING_WINDOW_SECONDS = 5.0
ASCENT_NOISE_THRESHOLD_METERS = 1.0
GRADE_DISTANCE_WINDOW_METERS = 20.0
MIN_GRADE_DISTANCE_METERS = 5.0
MAX_TRAJECTORY_GAP_SECONDS = 5.0
MAX_TRAJECTORY_SPEED_MPS = 60.0
EARTH_RADIUS_METERS = 6_371_008.8


class RideOverlayError(Exception):
    """Base error with a user-facing message."""


class ActivityError(RideOverlayError):
    """Invalid or insufficient activity data."""


class MetricSource(StrEnum):
    SPEED = "speed"
    DISTANCE = "distance"
    ELAPSED_TIME = "elapsed_time"
    CURRENT_TIME = "current_time"
    ALTITUDE = "altitude"
    TEMPERATURE = "temperature"
    PRESSURE = "pressure"
    CADENCE = "cadence"
    HEART_RATE = "heart_rate"
    POWER = "power"
    GRADE = "grade"
    TOTAL_ASCENT = "total_ascent"
    CALORIES = "calories"
    AVERAGE_SPEED = "average_speed"
    AVERAGE_HEART_RATE = "average_heart_rate"
    AVERAGE_CADENCE = "average_cadence"
    AVERAGE_POWER = "average_power"


class GapStrategy(StrEnum):
    MISSING = "missing"
    HOLD = "hold"


@dataclass(frozen=True)
class RawPoint:
    timestamp: datetime
    segment: int
    speed_mps: float | None = None
    distance_m: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    temperature_c: float | None = None
    pressure_pa: float | None = None
    cadence_rpm: float | None = None
    heart_rate_bpm: float | None = None
    power_w: float | None = None
    grade_percent: float | None = None
    calories_kcal: float | None = None
    total_ascent_m: float | None = None


@dataclass(frozen=True)
class TimeSeries:
    times: tuple[float, ...]
    values: tuple[float, ...]
    cumulative: bool = False
    interpolation_gap_seconds: float | None = MAX_INTERPOLATION_GAP_SECONDS
    gap_strategy: GapStrategy = GapStrategy.MISSING

    def value_at(
        self,
        time_seconds: float,
        max_gap_seconds: float | None | Literal["series"] = "series",
    ) -> float | None:
        gap_limit = (
            self.interpolation_gap_seconds if max_gap_seconds == "series" else max_gap_seconds
        )
        if not self.times:
            return None
        index = bisect.bisect_left(self.times, time_seconds)
        if index < len(self.times) and math.isclose(self.times[index], time_seconds, abs_tol=1e-9):
            return self.values[index]
        if index == 0:
            gap = self.times[0] - time_seconds
            return self.values[0] if gap_limit is None or gap <= gap_limit else None
        if index == len(self.times):
            gap = time_seconds - self.times[-1]
            if gap_limit is not None and gap > gap_limit:
                return self.values[-1] if self.gap_strategy == GapStrategy.HOLD else None
            return self.values[-1]
        left_time, right_time = self.times[index - 1], self.times[index]
        if gap_limit is not None and right_time - left_time > gap_limit:
            return self.values[index - 1] if self.gap_strategy == GapStrategy.HOLD else None
        ratio = (time_seconds - left_time) / (right_time - left_time)
        left_value, right_value = self.values[index - 1], self.values[index]
        return left_value + (right_value - left_value) * ratio

    def moving_average(self, window_seconds: float) -> TimeSeries:
        half_window = window_seconds / 2
        prefix = [0.0]
        for value in self.values:
            prefix.append(prefix[-1] + value)
        averaged: list[float] = []
        for time_seconds in self.times:
            left = bisect.bisect_left(self.times, time_seconds - half_window)
            right = bisect.bisect_right(self.times, time_seconds + half_window)
            averaged.append((prefix[right] - prefix[left]) / (right - left))
        return TimeSeries(
            self.times,
            tuple(averaged),
            cumulative=self.cumulative,
            interpolation_gap_seconds=self.interpolation_gap_seconds,
            gap_strategy=self.gap_strategy,
        )


@dataclass(frozen=True)
class TrajectoryPoint:
    time_seconds: float
    latitude: float
    longitude: float
    segment: int


@dataclass(frozen=True)
class TrajectoryBreak:
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    reason: str


@dataclass(frozen=True)
class TrajectoryData:
    points: tuple[TrajectoryPoint, ...]
    input_record_count: int
    missing_position_count: int
    invalid_position_count: int
    filtered_spike_count: int
    breaks: tuple[TrajectoryBreak, ...]

    @property
    def segment_count(self) -> int:
        return len({point.segment for point in self.points})


@dataclass(frozen=True)
class ProjectedTrajectoryPoint:
    time_seconds: float
    x_m: float
    y_m: float
    segment: int


@dataclass(frozen=True)
class TrajectorySample:
    x_m: float
    y_m: float
    heading_degrees: float


@dataclass(frozen=True)
class ProjectedTrajectory:
    points: tuple[ProjectedTrajectoryPoint, ...]
    min_x_m: float
    max_x_m: float
    min_y_m: float
    max_y_m: float
    projection: str = "local_equirectangular"

    @property
    def width_m(self) -> float:
        return self.max_x_m - self.min_x_m

    @property
    def height_m(self) -> float:
        return self.max_y_m - self.min_y_m

    def sample_at(self, time_seconds: float) -> TrajectorySample | None:
        if not self.points or time_seconds < self.points[0].time_seconds:
            return None
        times = [point.time_seconds for point in self.points]
        index = bisect.bisect_right(times, time_seconds) - 1
        current = self.points[index]
        x_m, y_m = current.x_m, current.y_m
        if index + 1 < len(self.points):
            following = self.points[index + 1]
            if following.segment == current.segment and time_seconds < following.time_seconds:
                ratio = (time_seconds - current.time_seconds) / (
                    following.time_seconds - current.time_seconds
                )
                x_m += (following.x_m - current.x_m) * ratio
                y_m += (following.y_m - current.y_m) * ratio
                if math.isclose(following.x_m, current.x_m, abs_tol=1e-6) and math.isclose(
                    following.y_m, current.y_m, abs_tol=1e-6
                ):
                    distinct = _next_distinct_projected_point(self.points, index)
                    if distinct is not None:
                        heading = _projected_heading(current, distinct)
                    else:
                        previous = _previous_distinct_projected_point(self.points, index)
                        heading = (
                            _projected_heading(previous, current) if previous is not None else 0.0
                        )
                else:
                    heading = _projected_heading(current, following)
                return TrajectorySample(x_m, y_m, heading)

        following = _next_distinct_projected_point(self.points, index)
        if following is not None:
            heading = _projected_heading(current, following)
        else:
            previous = _previous_distinct_projected_point(self.points, index)
            heading = _projected_heading(previous, current) if previous is not None else 0.0
        return TrajectorySample(x_m, y_m, heading)


@dataclass(frozen=True)
class ActivityData:
    start_time: datetime
    duration_seconds: float
    metrics: dict[MetricSource, TimeSeries]
    record_count: int
    metric_origins: dict[MetricSource, str] = dataclass_field(default_factory=dict)
    trajectory: TrajectoryData | None = None


def series_gap_details(
    series: TimeSeries,
    start_seconds: float,
    end_seconds: float,
) -> list[dict[str, float]]:
    threshold = series.interpolation_gap_seconds
    if threshold is None:
        return []
    return [
        {
            "start_seconds": left,
            "end_seconds": right,
            "duration_seconds": right - left,
        }
        for left, right in zip(series.times, series.times[1:], strict=False)
        if left < end_seconds and right > start_seconds and right - left > threshold
    ]


def activity_details(activity: ActivityData) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for source, series in activity.metrics.items():
        gaps = series_gap_details(series, 0.0, activity.duration_seconds)
        origin = activity.metric_origins.get(source, "unknown")
        metric_details: dict[str, Any] = {
            "origin": origin,
            "sample_count": len(series.times),
            "first_sample_seconds": series.times[0],
            "last_sample_seconds": series.times[-1],
            "minimum_value": min(series.values),
            "maximum_value": max(series.values),
            "cumulative": series.cumulative,
            "interpolation_gap_seconds": series.interpolation_gap_seconds,
            "gap_strategy": series.gap_strategy.value,
            "long_gap_count": len(gaps),
            "long_gaps": gaps,
        }
        if origin == "activity_file" and source in {
            MetricSource.SPEED,
            MetricSource.DISTANCE,
            MetricSource.ALTITUDE,
            MetricSource.TEMPERATURE,
            MetricSource.PRESSURE,
            MetricSource.CADENCE,
            MetricSource.HEART_RATE,
            MetricSource.POWER,
            MetricSource.GRADE,
        }:
            missing_count = max(0, activity.record_count - len(series.times))
            metric_details["missing_sample_count"] = missing_count
            metric_details["sample_coverage_percent"] = (
                len(series.times) / activity.record_count * 100 if activity.record_count else 0.0
            )
        metrics[source.value] = metric_details
    end_time = activity.start_time + timedelta(seconds=activity.duration_seconds)
    trajectory_details: dict[str, Any]
    if activity.trajectory is None:
        trajectory_details = {
            "available": False,
            "input_record_count": activity.record_count,
            "valid_point_count": 0,
        }
    else:
        trajectory = activity.trajectory
        trajectory_details = {
            "available": len(trajectory.points) >= 2,
            "origin": "activity_file_position_records",
            "input_record_count": trajectory.input_record_count,
            "valid_point_count": len(trajectory.points),
            "missing_position_count": trajectory.missing_position_count,
            "invalid_position_count": trajectory.invalid_position_count,
            "filtered_spike_count": trajectory.filtered_spike_count,
            "segment_count": trajectory.segment_count,
            "break_count": len(trajectory.breaks),
            "breaks": [
                {
                    "start_seconds": item.start_seconds,
                    "end_seconds": item.end_seconds,
                    "duration_seconds": item.duration_seconds,
                    "reason": item.reason,
                }
                for item in trajectory.breaks
            ],
        }
    return {
        "record_count": activity.record_count,
        "duration_seconds": activity.duration_seconds,
        "start_time_utc": activity.start_time.isoformat(timespec="milliseconds"),
        "end_time_utc": end_time.isoformat(timespec="milliseconds"),
        "start_time_local": activity.start_time.astimezone().isoformat(timespec="milliseconds"),
        "end_time_local": end_time.astimezone().isoformat(timespec="milliseconds"),
        "available_metric_count": len(activity.metrics),
        "metrics": metrics,
        "trajectory": trajectory_details,
    }


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _as_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fit_degrees(value: Any) -> float | None:
    numeric = _as_float(value)
    if numeric is None:
        return None
    if abs(numeric) > 180:
        numeric = numeric * 180.0 / (2**31)
    return numeric


def _fit_fields(frame: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for fit_field in frame.fields:
        if fit_field.name and fit_field.value is not None and fit_field.name not in values:
            values[fit_field.name] = fit_field.value
    return values


def _fit_field_value(frame: Any, *names: str) -> tuple[Any, str | None]:
    for name in names:
        for fit_field in frame.fields:
            if fit_field.name == name and fit_field.value is not None:
                return fit_field.value, fit_field.units
    return None, None


def _normalize_pressure_pa(value: Any, units: str | None = None) -> float | None:
    pressure = _as_float(value)
    if pressure is None or pressure < 0:
        return None
    normalized_units = (units or "").casefold()
    if normalized_units in {"hpa", "mbar"}:
        return pressure * 100
    if normalized_units == "kpa":
        return pressure * 1000
    if normalized_units == "bar":
        return pressure * 100_000
    if normalized_units == "mmhg":
        return pressure * 133.322368421
    if not normalized_units and pressure < 2_000:
        return pressure * 100
    return pressure


def _gpx_extension_values(point: Any) -> dict[str, float]:
    values: dict[str, float] = {}
    for extension in getattr(point, "extensions", ()):
        for element in extension.iter():
            if element.text is None:
                continue
            value = _as_float_from_text(element.text)
            if value is None:
                continue
            local_name = element.tag.rsplit("}", 1)[-1].casefold()
            normalized_name = local_name.replace("_", "").replace("-", "")
            values[normalized_name] = value
    return values


def _as_float_from_text(value: str) -> float | None:
    try:
        result = float(value.strip())
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _first_extension_value(values: dict[str, float], *names: str) -> float | None:
    for name in names:
        if name in values:
            return values[name]
    return None


def read_fit(path: Path) -> list[RawPoint]:
    points: list[RawPoint] = []
    lap_summaries: list[tuple[datetime, float | None, float | None]] = []
    session_summaries: list[tuple[datetime, float | None, float | None]] = []
    try:
        with fitdecode.FitReader(path) as fit_file:
            for frame in fit_file:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                fields = _fit_fields(frame)
                timestamp = _as_utc_datetime(fields.get("timestamp"))
                if timestamp is None:
                    continue
                if frame.name in {"lap", "session"}:
                    calories = _as_float(fields.get("total_calories"))
                    ascent = _as_float(fields.get("total_ascent"))
                    if calories is not None or ascent is not None:
                        summary = (timestamp, calories, ascent)
                        if frame.name == "lap":
                            lap_summaries.append(summary)
                        else:
                            session_summaries.append(summary)
                    continue
                if frame.name != "record":
                    continue
                speed = _as_float(fields.get("enhanced_speed"))
                if speed is None:
                    speed = _as_float(fields.get("speed"))
                distance = _as_float(fields.get("distance"))
                altitude = _as_float(fields.get("enhanced_altitude"))
                if altitude is None:
                    altitude = _as_float(fields.get("altitude"))
                cadence = _as_float(fields.get("cadence"))
                fractional_cadence = _as_float(fields.get("fractional_cadence"))
                if cadence is not None and fractional_cadence is not None:
                    cadence += fractional_cadence
                pressure_value, pressure_units = _fit_field_value(
                    frame, "absolute_pressure", "ambient_pressure", "pressure"
                )
                points.append(
                    RawPoint(
                        timestamp=timestamp,
                        segment=0,
                        speed_mps=speed if speed is None or speed >= 0 else None,
                        distance_m=distance if distance is None or distance >= 0 else None,
                        latitude=_fit_degrees(fields.get("position_lat")),
                        longitude=_fit_degrees(fields.get("position_long")),
                        altitude_m=altitude,
                        temperature_c=_as_float(fields.get("temperature")),
                        pressure_pa=_normalize_pressure_pa(pressure_value, pressure_units),
                        cadence_rpm=cadence if cadence is None or cadence >= 0 else None,
                        heart_rate_bpm=_as_float(fields.get("heart_rate")),
                        power_w=_as_float(fields.get("power")),
                        grade_percent=_as_float(fields.get("grade")),
                        calories_kcal=_as_float(
                            fields.get("total_calories", fields.get("calories"))
                        ),
                        total_ascent_m=_as_float(fields.get("total_ascent")),
                    )
                )
    except Exception as exc:
        raise ActivityError(f"无法解析 FIT 文件 {path.name}: {exc}") from exc
    if points:
        first_timestamp = min(point.timestamp for point in points)
        last_timestamp = max(point.timestamp for point in points)

        def valid_summary_time(timestamp: datetime) -> bool:
            return first_timestamp <= timestamp <= last_timestamp + timedelta(minutes=5)

        cumulative_calories = 0.0
        cumulative_ascent = 0.0
        for timestamp, calories, ascent in sorted(lap_summaries, key=lambda item: item[0]):
            if not valid_summary_time(timestamp):
                continue
            cumulative_calories += calories or 0.0
            cumulative_ascent += ascent or 0.0
            points.append(
                RawPoint(
                    timestamp=min(timestamp, last_timestamp),
                    segment=0,
                    calories_kcal=cumulative_calories if calories is not None else None,
                    total_ascent_m=cumulative_ascent if ascent is not None else None,
                )
            )

        cumulative_calories = 0.0
        cumulative_ascent = 0.0
        for timestamp, calories, ascent in sorted(session_summaries, key=lambda item: item[0]):
            if not valid_summary_time(timestamp):
                continue
            cumulative_calories += calories or 0.0
            cumulative_ascent += ascent or 0.0
            points.append(
                RawPoint(
                    timestamp=min(timestamp, last_timestamp),
                    segment=0,
                    calories_kcal=cumulative_calories if calories is not None else None,
                    total_ascent_m=cumulative_ascent if ascent is not None else None,
                )
            )
    return points


def read_gpx(path: Path) -> list[RawPoint]:
    points: list[RawPoint] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            gpx = gpxpy.parse(handle)
        segment_number = 0
        for track in gpx.tracks:
            for segment in track.segments:
                for point in segment.points:
                    timestamp = _as_utc_datetime(point.time)
                    if timestamp is None:
                        continue
                    speed = _as_float(getattr(point, "speed", None))
                    extensions = _gpx_extension_values(point)
                    pressure = _first_extension_value(
                        extensions,
                        "absolutepressure",
                        "ambientpressure",
                        "airpressure",
                        "pressure",
                    )
                    points.append(
                        RawPoint(
                            timestamp=timestamp,
                            segment=segment_number,
                            speed_mps=speed if speed is None or speed >= 0 else None,
                            latitude=_as_float(point.latitude),
                            longitude=_as_float(point.longitude),
                            altitude_m=_as_float(point.elevation),
                            temperature_c=_first_extension_value(
                                extensions, "atemp", "temperature", "temp"
                            ),
                            pressure_pa=_normalize_pressure_pa(pressure),
                            cadence_rpm=_first_extension_value(extensions, "cad", "cadence"),
                            heart_rate_bpm=_first_extension_value(extensions, "hr", "heartrate"),
                            power_w=_first_extension_value(extensions, "power", "watts"),
                            grade_percent=_first_extension_value(extensions, "grade", "slope"),
                            calories_kcal=_first_extension_value(
                                extensions, "totalcalories", "calories"
                            ),
                            total_ascent_m=_first_extension_value(extensions, "totalascent"),
                        )
                    )
                segment_number += 1
    except Exception as exc:
        raise ActivityError(f"无法解析 GPX 文件 {path.name}: {exc}") from exc
    return points


def _merge_points(points: Iterable[RawPoint]) -> list[RawPoint]:
    merged: list[RawPoint] = []
    for point in sorted(points, key=lambda item: item.timestamp):
        if merged and merged[-1].timestamp == point.timestamp:
            previous = merged[-1]
            merged[-1] = RawPoint(
                timestamp=point.timestamp,
                segment=point.segment,
                speed_mps=point.speed_mps if point.speed_mps is not None else previous.speed_mps,
                distance_m=(
                    point.distance_m if point.distance_m is not None else previous.distance_m
                ),
                latitude=point.latitude if point.latitude is not None else previous.latitude,
                longitude=point.longitude if point.longitude is not None else previous.longitude,
                altitude_m=(
                    point.altitude_m if point.altitude_m is not None else previous.altitude_m
                ),
                temperature_c=(
                    point.temperature_c
                    if point.temperature_c is not None
                    else previous.temperature_c
                ),
                pressure_pa=(
                    point.pressure_pa if point.pressure_pa is not None else previous.pressure_pa
                ),
                cadence_rpm=(
                    point.cadence_rpm if point.cadence_rpm is not None else previous.cadence_rpm
                ),
                heart_rate_bpm=(
                    point.heart_rate_bpm
                    if point.heart_rate_bpm is not None
                    else previous.heart_rate_bpm
                ),
                power_w=point.power_w if point.power_w is not None else previous.power_w,
                grade_percent=(
                    point.grade_percent
                    if point.grade_percent is not None
                    else previous.grade_percent
                ),
                calories_kcal=(
                    point.calories_kcal
                    if point.calories_kcal is not None
                    else previous.calories_kcal
                ),
                total_ascent_m=(
                    point.total_ascent_m
                    if point.total_ascent_m is not None
                    else previous.total_ascent_m
                ),
            )
        else:
            merged.append(point)
    return merged


def _haversine_meters(left: RawPoint, right: RawPoint) -> float | None:
    if None in (left.latitude, left.longitude, right.latitude, right.longitude):
        return None
    assert left.latitude is not None and left.longitude is not None
    assert right.latitude is not None and right.longitude is not None
    lat1, lon1 = math.radians(left.latitude), math.radians(left.longitude)
    lat2, lon2 = math.radians(right.latitude), math.radians(right.longitude)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    a = min(1.0, max(0.0, a))
    return EARTH_RADIUS_METERS * 2 * math.asin(math.sqrt(a))


def _trajectory_speed_mps(left: RawPoint, right: RawPoint) -> float | None:
    duration = (right.timestamp - left.timestamp).total_seconds()
    if duration <= 0:
        return None
    distance = _haversine_meters(left, right)
    return distance / duration if distance is not None else None


def _is_isolated_position_spike(
    previous: RawPoint,
    current: RawPoint,
    following: RawPoint,
) -> bool:
    if not (previous.segment == current.segment == following.segment):
        return False
    before_seconds = (current.timestamp - previous.timestamp).total_seconds()
    after_seconds = (following.timestamp - current.timestamp).total_seconds()
    if not (
        0 < before_seconds <= MAX_TRAJECTORY_GAP_SECONDS
        and 0 < after_seconds <= MAX_TRAJECTORY_GAP_SECONDS
    ):
        return False
    incoming = _trajectory_speed_mps(previous, current)
    outgoing = _trajectory_speed_mps(current, following)
    bypass = _trajectory_speed_mps(previous, following)
    return (
        incoming is not None
        and outgoing is not None
        and bypass is not None
        and incoming > MAX_TRAJECTORY_SPEED_MPS
        and outgoing > MAX_TRAJECTORY_SPEED_MPS
        and bypass <= MAX_TRAJECTORY_SPEED_MPS
    )


def build_trajectory(points: list[RawPoint], start: datetime) -> TrajectoryData | None:
    located: list[RawPoint] = []
    missing_count = 0
    invalid_count = 0
    for point in points:
        latitude = _as_float(point.latitude)
        longitude = _as_float(point.longitude)
        if latitude is None or longitude is None:
            missing_count += 1
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            invalid_count += 1
            continue
        located.append(point)

    if not located:
        return None

    spike_indexes = {
        index
        for index in range(1, len(located) - 1)
        if _is_isolated_position_spike(
            located[index - 1],
            located[index],
            located[index + 1],
        )
    }
    reliable = [point for index, point in enumerate(located) if index not in spike_indexes]
    trajectory_points: list[TrajectoryPoint] = []
    breaks: list[TrajectoryBreak] = []
    output_segment = 0
    previous: RawPoint | None = None
    for point in reliable:
        if previous is not None:
            duration = (point.timestamp - previous.timestamp).total_seconds()
            reason: str | None = None
            if point.segment != previous.segment:
                reason = "activity_segment"
            elif duration > MAX_TRAJECTORY_GAP_SECONDS:
                reason = "position_gap"
            else:
                speed = _trajectory_speed_mps(previous, point)
                if speed is not None and speed > MAX_TRAJECTORY_SPEED_MPS:
                    reason = "implausible_jump"
            if reason is not None:
                output_segment += 1
                breaks.append(
                    TrajectoryBreak(
                        start_seconds=(previous.timestamp - start).total_seconds(),
                        end_seconds=(point.timestamp - start).total_seconds(),
                        duration_seconds=duration,
                        reason=reason,
                    )
                )
        assert point.latitude is not None and point.longitude is not None
        trajectory_points.append(
            TrajectoryPoint(
                time_seconds=(point.timestamp - start).total_seconds(),
                latitude=float(point.latitude),
                longitude=float(point.longitude),
                segment=output_segment,
            )
        )
        previous = point
    return TrajectoryData(
        points=tuple(trajectory_points),
        input_record_count=len(points),
        missing_position_count=missing_count,
        invalid_position_count=invalid_count,
        filtered_spike_count=len(spike_indexes),
        breaks=tuple(breaks),
    )


def project_trajectory(trajectory: TrajectoryData) -> ProjectedTrajectory:
    if not trajectory.points:
        raise ActivityError("轨迹没有可投影的位置点")
    reference_latitude = sum(point.latitude for point in trajectory.points) / len(trajectory.points)
    longitude_scale = max(1e-9, math.cos(math.radians(reference_latitude)))
    previous_longitude = trajectory.points[0].longitude
    unwrapped_longitude = previous_longitude
    reference_longitude = unwrapped_longitude
    projected: list[ProjectedTrajectoryPoint] = []
    for point in trajectory.points:
        delta_longitude = (point.longitude - previous_longitude + 180.0) % 360.0 - 180.0
        unwrapped_longitude += delta_longitude
        previous_longitude = point.longitude
        x_m = (
            EARTH_RADIUS_METERS
            * math.radians(unwrapped_longitude - reference_longitude)
            * longitude_scale
        )
        y_m = EARTH_RADIUS_METERS * math.radians(point.latitude - reference_latitude)
        projected.append(
            ProjectedTrajectoryPoint(
                time_seconds=point.time_seconds,
                x_m=x_m,
                y_m=y_m,
                segment=point.segment,
            )
        )
    x_values = [point.x_m for point in projected]
    y_values = [point.y_m for point in projected]
    return ProjectedTrajectory(
        points=tuple(projected),
        min_x_m=min(x_values),
        max_x_m=max(x_values),
        min_y_m=min(y_values),
        max_y_m=max(y_values),
    )


def _projected_heading(
    left: ProjectedTrajectoryPoint,
    right: ProjectedTrajectoryPoint,
) -> float:
    return math.degrees(math.atan2(right.x_m - left.x_m, right.y_m - left.y_m)) % 360


def _next_distinct_projected_point(
    points: tuple[ProjectedTrajectoryPoint, ...],
    index: int,
) -> ProjectedTrajectoryPoint | None:
    current = points[index]
    for candidate in points[index + 1 :]:
        if candidate.segment != current.segment:
            break
        if not (
            math.isclose(candidate.x_m, current.x_m, abs_tol=1e-6)
            and math.isclose(candidate.y_m, current.y_m, abs_tol=1e-6)
        ):
            return candidate
    return None


def _previous_distinct_projected_point(
    points: tuple[ProjectedTrajectoryPoint, ...],
    index: int,
) -> ProjectedTrajectoryPoint | None:
    current = points[index]
    for candidate in reversed(points[:index]):
        if candidate.segment != current.segment:
            break
        if not (
            math.isclose(candidate.x_m, current.x_m, abs_tol=1e-6)
            and math.isclose(candidate.y_m, current.y_m, abs_tol=1e-6)
        ):
            return candidate
    return None


def _direct_distance_series(points: list[RawPoint], start: datetime) -> TimeSeries | None:
    samples = [(point, point.distance_m) for point in points if point.distance_m is not None]
    if len(samples) < 2:
        return None
    times: list[float] = []
    values: list[float] = []
    last_value = 0.0
    reset_count = 0
    for point, raw_value in samples:
        assert raw_value is not None
        value = raw_value
        if values and value < last_value:
            value = last_value
            reset_count += 1
        times.append((point.timestamp - start).total_seconds())
        values.append(value)
        last_value = value
    if reset_count:
        LOGGER.warning("距离字段出现 %d 次回退，已保持累计距离单调不减", reset_count)
    return TimeSeries(
        tuple(times),
        tuple(values),
        cumulative=True,
        gap_strategy=GapStrategy.HOLD,
    )


def _derived_distance_series(points: list[RawPoint], start: datetime) -> TimeSeries | None:
    located = [
        point for point in points if point.latitude is not None and point.longitude is not None
    ]
    if len(located) < 2:
        return None
    times: list[float] = []
    values: list[float] = []
    cumulative = 0.0
    previous: RawPoint | None = None
    for point in located:
        if previous is not None and point.segment == previous.segment:
            distance = _haversine_meters(previous, point)
            if distance is not None:
                cumulative += distance
        times.append((point.timestamp - start).total_seconds())
        values.append(cumulative)
        previous = point
    return TimeSeries(
        tuple(times),
        tuple(values),
        cumulative=True,
        gap_strategy=GapStrategy.HOLD,
    )


def _direct_speed_series(points: list[RawPoint], start: datetime) -> TimeSeries | None:
    samples = [(point, point.speed_mps) for point in points if point.speed_mps is not None]
    if len(samples) < 2:
        return None
    return TimeSeries(
        tuple((point.timestamp - start).total_seconds() for point, _ in samples),
        tuple(value for _, value in samples if value is not None),
    )


def _derived_speed_series(points: list[RawPoint], start: datetime) -> TimeSeries | None:
    values_by_time: dict[float, float] = {}
    previous: RawPoint | None = None
    for point in points:
        if point.latitude is None or point.longitude is None:
            continue
        if previous is not None and point.segment == previous.segment:
            delta = (point.timestamp - previous.timestamp).total_seconds()
            distance = _haversine_meters(previous, point)
            if delta > 0 and distance is not None:
                speed = distance / delta
                previous_time = (previous.timestamp - start).total_seconds()
                current_time = (point.timestamp - start).total_seconds()
                values_by_time.setdefault(previous_time, speed)
                values_by_time[current_time] = speed
        previous = point
    if len(values_by_time) < 2:
        return None
    samples = sorted(values_by_time.items())
    return TimeSeries(tuple(item[0] for item in samples), tuple(item[1] for item in samples))


def _point_series(
    points: list[RawPoint],
    start: datetime,
    attribute: str,
    *,
    non_negative: bool = False,
) -> TimeSeries | None:
    samples: list[tuple[float, float]] = []
    for point in points:
        value = _as_float(getattr(point, attribute))
        if value is None or (non_negative and value < 0):
            continue
        samples.append(((point.timestamp - start).total_seconds(), value))
    if not samples:
        return None
    return TimeSeries(
        tuple(time_seconds for time_seconds, _ in samples),
        tuple(value for _, value in samples),
    )


def _cumulative_point_series(
    points: list[RawPoint],
    start: datetime,
    attribute: str,
    *,
    minimum_raw_samples: int = 1,
) -> TimeSeries | None:
    samples: list[tuple[float, float]] = []
    for point in points:
        value = _as_float(getattr(point, attribute))
        if value is None or value < 0:
            continue
        samples.append(((point.timestamp - start).total_seconds(), value))
    if len(samples) < minimum_raw_samples:
        return None
    times: list[float] = []
    values: list[float] = []
    if samples[0][0] > 0 or samples[0][1] > 0:
        times.append(0.0)
        values.append(0.0)
    previous = 0.0
    for time_seconds, value in samples:
        value = max(previous, value)
        times.append(time_seconds)
        values.append(value)
        previous = value
    return TimeSeries(
        tuple(times),
        tuple(values),
        cumulative=True,
        interpolation_gap_seconds=None,
    )


def _segment_by_time(points: list[RawPoint], start: datetime) -> dict[float, int]:
    return {
        (point.timestamp - start).total_seconds(): point.segment
        for point in points
        if point.altitude_m is not None
    }


def _smooth_altitude_by_segment(
    altitude: TimeSeries,
    segments: dict[float, int],
) -> TimeSeries:
    grouped: dict[int, list[tuple[float, float]]] = {}
    for time_seconds, value in zip(altitude.times, altitude.values, strict=True):
        segment = segments.get(time_seconds, 0)
        grouped.setdefault(segment, []).append((time_seconds, value))
    smoothed: list[tuple[float, float]] = []
    for samples in grouped.values():
        series = TimeSeries(
            tuple(time_seconds for time_seconds, _ in samples),
            tuple(value for _, value in samples),
        ).moving_average(ALTITUDE_SMOOTHING_WINDOW_SECONDS)
        smoothed.extend(zip(series.times, series.values, strict=True))
    smoothed.sort(key=lambda item: item[0])
    return TimeSeries(
        tuple(time_seconds for time_seconds, _ in smoothed),
        tuple(value for _, value in smoothed),
    )


def _derived_ascent_series(
    altitude: TimeSeries,
    segments: dict[float, int],
) -> TimeSeries | None:
    if len(altitude.times) < 2:
        return None
    values: list[float] = []
    total_ascent = 0.0
    reference_altitude: float | None = None
    previous_segment: int | None = None
    for time_seconds, current_altitude in zip(altitude.times, altitude.values, strict=True):
        segment = segments.get(time_seconds, 0)
        if reference_altitude is None or segment != previous_segment:
            reference_altitude = current_altitude
        else:
            difference = current_altitude - reference_altitude
            if difference >= ASCENT_NOISE_THRESHOLD_METERS:
                total_ascent += difference
                reference_altitude = current_altitude
            elif difference <= -ASCENT_NOISE_THRESHOLD_METERS:
                reference_altitude = current_altitude
        values.append(total_ascent)
        previous_segment = segment
    return TimeSeries(
        altitude.times,
        tuple(values),
        cumulative=True,
        gap_strategy=GapStrategy.HOLD,
    )


def _derived_grade_series(
    altitude: TimeSeries,
    distance: TimeSeries,
    segments: dict[float, int],
) -> TimeSeries | None:
    grouped: dict[int, list[tuple[float, float, float]]] = {}
    for time_seconds, altitude_m in zip(altitude.times, altitude.values, strict=True):
        distance_m = distance.value_at(time_seconds)
        if distance_m is None:
            continue
        segment = segments.get(time_seconds, 0)
        grouped.setdefault(segment, []).append((time_seconds, distance_m, altitude_m))

    grades: list[tuple[float, float]] = []
    half_window = GRADE_DISTANCE_WINDOW_METERS / 2
    for samples in grouped.values():
        if len(samples) < 2:
            continue
        distances = [sample[1] for sample in samples]
        for index, (time_seconds, distance_m, _) in enumerate(samples):
            left = max(0, bisect.bisect_right(distances, distance_m - half_window) - 1)
            right = min(
                len(samples) - 1,
                bisect.bisect_left(distances, distance_m + half_window),
            )
            if left == right:
                if index > 0:
                    left = index - 1
                elif index + 1 < len(samples):
                    right = index + 1
            horizontal_distance = samples[right][1] - samples[left][1]
            if horizontal_distance < MIN_GRADE_DISTANCE_METERS:
                continue
            elevation_change = samples[right][2] - samples[left][2]
            grade = elevation_change / horizontal_distance * 100
            if math.isfinite(grade) and abs(grade) <= 100:
                grades.append((time_seconds, grade))
    if not grades:
        return None
    grades.sort(key=lambda item: item[0])
    return TimeSeries(
        tuple(time_seconds for time_seconds, _ in grades),
        tuple(value for _, value in grades),
    )


def _running_average_series(series: TimeSeries) -> TimeSeries:
    if len(series.times) == 1:
        return TimeSeries(
            series.times,
            series.values,
            gap_strategy=GapStrategy.HOLD,
        )
    averages = [series.values[0]]
    accumulated_value_time = 0.0
    accumulated_time = 0.0
    for index in range(1, len(series.times)):
        duration = series.times[index] - series.times[index - 1]
        if 0 < duration <= MAX_INTERPOLATION_GAP_SECONDS:
            accumulated_value_time += (
                (series.values[index - 1] + series.values[index]) / 2 * duration
            )
            accumulated_time += duration
        average = (
            accumulated_value_time / accumulated_time if accumulated_time > 0 else averages[-1]
        )
        averages.append(average)
    return TimeSeries(
        series.times,
        tuple(averages),
        interpolation_gap_seconds=series.interpolation_gap_seconds,
        gap_strategy=GapStrategy.HOLD,
    )


def build_activity(points: list[RawPoint]) -> ActivityData:
    points = _merge_points(points)
    if len(points) < 2:
        raise ActivityError("运动数据至少需要两个不同时间戳的有效记录")
    start = points[0].timestamp
    duration = (points[-1].timestamp - start).total_seconds()
    if duration <= 0:
        raise ActivityError("运动数据总时长必须大于 0 秒")

    direct_distance = _direct_distance_series(points, start)
    distance = direct_distance or _derived_distance_series(points, start)
    direct_speed = _direct_speed_series(points, start)
    speed = direct_speed or _derived_speed_series(points, start)
    altitude = _point_series(points, start, "altitude_m")
    temperature = _point_series(points, start, "temperature_c")
    pressure = _point_series(points, start, "pressure_pa", non_negative=True)
    cadence = _point_series(points, start, "cadence_rpm", non_negative=True)
    heart_rate = _point_series(points, start, "heart_rate_bpm", non_negative=True)
    power = _point_series(points, start, "power_w", non_negative=True)
    direct_grade = _point_series(points, start, "grade_percent")
    calories = _cumulative_point_series(points, start, "calories_kcal")

    metrics: dict[MetricSource, TimeSeries] = {}
    origins: dict[MetricSource, str] = {}
    if speed is not None:
        metrics[MetricSource.SPEED] = speed
        origins[MetricSource.SPEED] = (
            "activity_file" if direct_speed is not None else "derived_from_gps"
        )
    if distance is not None:
        metrics[MetricSource.DISTANCE] = distance
        origins[MetricSource.DISTANCE] = (
            "activity_file" if direct_distance is not None else "derived_from_gps"
        )
    metrics[MetricSource.ELAPSED_TIME] = TimeSeries(
        (0.0, duration),
        (0.0, duration),
        cumulative=True,
        interpolation_gap_seconds=None,
    )
    origins[MetricSource.ELAPSED_TIME] = "derived_from_record_timestamps"
    local_start = start.astimezone()
    clock_start = (
        local_start.hour * 3600
        + local_start.minute * 60
        + local_start.second
        + local_start.microsecond / 1_000_000
    )
    metrics[MetricSource.CURRENT_TIME] = TimeSeries(
        (0.0, duration),
        (clock_start, clock_start + duration),
        interpolation_gap_seconds=None,
    )
    origins[MetricSource.CURRENT_TIME] = "derived_from_record_timestamps_and_local_timezone"
    if altitude is not None:
        metrics[MetricSource.ALTITUDE] = altitude
        origins[MetricSource.ALTITUDE] = "activity_file"
    if temperature is not None:
        metrics[MetricSource.TEMPERATURE] = temperature
        origins[MetricSource.TEMPERATURE] = "activity_file"
    if pressure is not None:
        metrics[MetricSource.PRESSURE] = pressure
        origins[MetricSource.PRESSURE] = "activity_file"
    if cadence is not None:
        metrics[MetricSource.CADENCE] = cadence
        origins[MetricSource.CADENCE] = "activity_file"
    if heart_rate is not None:
        metrics[MetricSource.HEART_RATE] = heart_rate
        origins[MetricSource.HEART_RATE] = "activity_file"
    if power is not None:
        metrics[MetricSource.POWER] = power
        origins[MetricSource.POWER] = "activity_file"

    segments = _segment_by_time(points, start)
    smoothed_altitude = (
        _smooth_altitude_by_segment(altitude, segments) if altitude is not None else None
    )
    grade = direct_grade
    if grade is None and smoothed_altitude is not None and distance is not None:
        grade = _derived_grade_series(smoothed_altitude, distance, segments)
    if grade is not None:
        metrics[MetricSource.GRADE] = grade
        origins[MetricSource.GRADE] = (
            "activity_file"
            if direct_grade is not None
            else "derived_from_smoothed_altitude_and_distance"
        )

    direct_ascent = _cumulative_point_series(points, start, "total_ascent_m", minimum_raw_samples=2)
    derived_ascent = (
        _derived_ascent_series(smoothed_altitude, segments)
        if smoothed_altitude is not None
        else None
    )
    sparse_ascent = _cumulative_point_series(points, start, "total_ascent_m")
    total_ascent = direct_ascent or derived_ascent or sparse_ascent
    if total_ascent is not None:
        metrics[MetricSource.TOTAL_ASCENT] = total_ascent
        if direct_ascent is not None:
            origins[MetricSource.TOTAL_ASCENT] = "activity_file_continuous"
        elif derived_ascent is not None:
            origins[MetricSource.TOTAL_ASCENT] = "derived_from_smoothed_altitude"
        else:
            origins[MetricSource.TOTAL_ASCENT] = "activity_file_summary"
    if calories is not None:
        metrics[MetricSource.CALORIES] = calories
        origins[MetricSource.CALORIES] = "activity_file_cumulative_or_summary"

    average_sources = (
        (MetricSource.AVERAGE_SPEED, speed),
        (MetricSource.AVERAGE_HEART_RATE, heart_rate),
        (MetricSource.AVERAGE_CADENCE, cadence),
        (MetricSource.AVERAGE_POWER, power),
    )
    for metric, source_series in average_sources:
        if source_series is not None:
            metrics[metric] = _running_average_series(source_series)
            origins[metric] = "derived_time_weighted_running_average"
    trajectory = build_trajectory(points, start)
    if trajectory is not None:
        if trajectory.invalid_position_count:
            LOGGER.warning(
                "位置数据中有 %d 条越界坐标，已从轨迹中移除",
                trajectory.invalid_position_count,
            )
        if trajectory.filtered_spike_count:
            LOGGER.warning(
                "位置数据中有 %d 个孤立 GPS 飘点，已从轨迹中移除",
                trajectory.filtered_spike_count,
            )
        jump_count = sum(item.reason == "implausible_jump" for item in trajectory.breaks)
        if jump_count:
            LOGGER.warning(
                "轨迹中检测到 %d 次不可信的位置跳跃，已断开对应线段",
                jump_count,
            )
    return ActivityData(start, duration, metrics, len(points), origins, trajectory)


def read_activity(path: Path) -> ActivityData:
    LOGGER.info("读取运动数据: %s", path.name)
    points = read_fit(path) if path.suffix.lower() == ".fit" else read_gpx(path)
    activity = build_activity(points)
    available = ", ".join(metric.value for metric in activity.metrics) or "无"
    LOGGER.info(
        "运动数据记录 %d 条，时长 %.3f 秒，可用指标: %s",
        activity.record_count,
        activity.duration_seconds,
        available,
    )
    for source, series in activity.metrics.items():
        LOGGER.debug(
            "指标处理明细: source=%s origin=%s samples=%d first=%.3fs last=%.3fs",
            source.value,
            activity.metric_origins.get(source, "unknown"),
            len(series.times),
            series.times[0],
            series.times[-1],
        )
    if activity.trajectory is not None:
        LOGGER.info(
            "轨迹位置点 %d 条，分为 %d 段，断点 %d 个",
            len(activity.trajectory.points),
            activity.trajectory.segment_count,
            len(activity.trajectory.breaks),
        )
        LOGGER.debug(
            "轨迹处理明细: input=%d valid=%d missing=%d invalid=%d filtered_spikes=%d",
            activity.trajectory.input_record_count,
            len(activity.trajectory.points),
            activity.trajectory.missing_position_count,
            activity.trajectory.invalid_position_count,
            activity.trajectory.filtered_spike_count,
        )
    return activity
