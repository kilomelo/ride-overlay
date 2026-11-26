from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import fitdecode

from ride_overlay import load_config, read_activity

REPOSITORY = Path(__file__).resolve().parents[1]
EXAMPLE_PROJECT = REPOSITORY / "example_project"


def test_example_project_is_self_contained_and_starts_without_video_metadata() -> None:
    config = load_config(EXAMPLE_PROJECT)

    assert config.inputs.activity_file == "example_activity.fit"
    assert config.inputs.auto_fix_startup_altitude is True
    assert config.inputs.font_file == "Exo2-BoldItalic.ttf"
    assert config.inputs.background_image_file == "images/dashboard-background.png"
    assert config.inputs.foreground_image_file == "images/dashboard-foreground.png"
    assert config.inputs.video_files is None
    assert config.timeline.video_joins == []
    assert config.timeline.activity_start_offset_frames == 0
    assert (EXAMPLE_PROJECT / config.inputs.activity_file).is_file()
    assert (EXAMPLE_PROJECT / config.inputs.font_file).is_file()
    assert (EXAMPLE_PROJECT / config.inputs.background_image_file).is_file()
    assert (EXAMPLE_PROJECT / config.inputs.foreground_image_file).is_file()


def test_example_fit_remains_useful_but_contains_only_synthetic_location_and_time() -> None:
    fit_path = EXAMPLE_PROJECT / "example_activity.fit"
    activity = read_activity(fit_path)

    assert activity.start_time == datetime(2025, 1, 1, tzinfo=UTC)
    assert activity.record_count == 1844
    assert activity.duration_seconds == 3625
    assert activity.trajectory is not None
    assert all(9.9 < point.latitude < 10.1 for point in activity.trajectory.points)
    assert all(-30.1 < point.longitude < -29.9 for point in activity.trajectory.points)

    serial_numbers: list[int] = []
    timestamps: list[datetime] = []
    activity_names: list[str] = []
    with fitdecode.FitReader(fit_path) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            for field in frame.fields:
                if field.name == "serial_number" and field.value is not None:
                    serial_numbers.append(field.value)
                if isinstance(field.value, datetime):
                    timestamps.append(field.value)
                if field.name == "sport_profile_name" and field.value:
                    activity_names.append(field.value)

    assert serial_numbers == []
    assert timestamps and all(timestamp.year == 2025 for timestamp in timestamps)
    assert activity_names == ["Example Ride"]
