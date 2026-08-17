from __future__ import annotations

from pathlib import Path

from ride_overlay import MetricSource, build_activity, read_gpx


def test_gpx_derives_speed_and_distance(tmp_path: Path) -> None:
    gpx_path = tmp_path / "track.gpx"
    gpx_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>test</name><trkseg>
    <trkpt lat="31.000000" lon="121.000000"><time>2026-01-01T00:00:00Z</time></trkpt>
    <trkpt lat="31.000100" lon="121.000000"><time>2026-01-01T00:00:01Z</time></trkpt>
    <trkpt lat="31.000200" lon="121.000000"><time>2026-01-01T00:00:02Z</time></trkpt>
  </trkseg></trk>
</gpx>
""",
        encoding="utf-8",
    )
    activity = build_activity(read_gpx(gpx_path))
    assert MetricSource.SPEED in activity.metrics
    assert MetricSource.DISTANCE in activity.metrics
    assert 21 < activity.metrics[MetricSource.DISTANCE].values[-1] < 23


def test_gpx_reads_common_sensor_extensions(tmp_path: Path) -> None:
    gpx_path = tmp_path / "sensors.gpx"
    gpx_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test"
 xmlns="http://www.topografix.com/GPX/1/1"
 xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk><trkseg>
    <trkpt lat="31.0" lon="121.0"><ele>10</ele><time>2026-01-01T00:00:00Z</time>
      <extensions><gpxtpx:TrackPointExtension><gpxtpx:atemp>20</gpxtpx:atemp>
      <gpxtpx:hr>120</gpxtpx:hr><gpxtpx:cad>80</gpxtpx:cad>
      <gpxtpx:power>200</gpxtpx:power><gpxtpx:pressure>1013</gpxtpx:pressure>
      </gpxtpx:TrackPointExtension></extensions></trkpt>
    <trkpt lat="31.0001" lon="121.0"><ele>12</ele><time>2026-01-01T00:00:05Z</time>
      <extensions><gpxtpx:TrackPointExtension><gpxtpx:atemp>21</gpxtpx:atemp>
      <gpxtpx:hr>125</gpxtpx:hr><gpxtpx:cad>85</gpxtpx:cad>
      <gpxtpx:power>210</gpxtpx:power><gpxtpx:pressure>1012</gpxtpx:pressure>
      </gpxtpx:TrackPointExtension></extensions></trkpt>
  </trkseg></trk>
</gpx>
""",
        encoding="utf-8",
    )
    activity = build_activity(read_gpx(gpx_path))
    for source in (
        MetricSource.ALTITUDE,
        MetricSource.TEMPERATURE,
        MetricSource.PRESSURE,
        MetricSource.CADENCE,
        MetricSource.HEART_RATE,
        MetricSource.POWER,
    ):
        assert source in activity.metrics
    assert activity.metrics[MetricSource.PRESSURE].values[0] == 101_300
