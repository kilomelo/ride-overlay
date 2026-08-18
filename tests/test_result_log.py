from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import ride_overlay
from ride_overlay import (
    LOGGER,
    ActivityData,
    MetricSource,
    RunReport,
    TimeSeries,
    activity_details,
    configure_logging,
    main,
)


def test_report_contains_stage_details_and_events(tmp_path: Path) -> None:
    report = RunReport(mode="preview", project=tmp_path, command=["ride-overlay", "--preview"])
    configure_logging(report, verbose=False)

    with report.stage("测试阶段"):
        report.details["records"] = {"count": 12, "duration_seconds": 3.5}
        LOGGER.warning("测试数据空洞: 1.000s - 2.000s")
    report.finish("SUCCESS", 0)
    target = report.write(tmp_path / "result.log")

    content = target.read_text(encoding="utf-8")
    assert "status: SUCCESS" in content
    assert "测试阶段 | SUCCESS" in content
    assert '"count": 12' in content
    assert "WARNING  测试数据空洞" in content


def test_failed_task_still_writes_result_log(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({}), encoding="utf-8")

    assert main([str(tmp_path)]) == 2

    content = (tmp_path / "export" / "result.log").read_text(encoding="utf-8")
    assert "status: FAILED" in content
    assert "exit_code: 2" in content
    assert "读取并校验配置 | FAILED" in content
    assert "配置校验失败" in content


def test_activity_details_include_missing_samples_and_gaps() -> None:
    activity = ActivityData(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        duration_seconds=10,
        metrics={MetricSource.CADENCE: TimeSeries((0.0, 10.0), (80.0, 90.0))},
        record_count=3,
        metric_origins={MetricSource.CADENCE: "activity_file"},
    )

    cadence = activity_details(activity)["metrics"]["cadence"]
    assert cadence["missing_sample_count"] == 1
    assert cadence["sample_coverage_percent"] == 2 / 3 * 100
    assert cadence["long_gaps"] == [
        {"start_seconds": 0.0, "end_seconds": 10.0, "duration_seconds": 10.0}
    ]


def test_result_log_uses_output_folder(tmp_path: Path, monkeypatch) -> None:
    output_folder = tmp_path / "outputs"
    output_folder.mkdir()

    def fake_run(args, report: RunReport) -> None:
        report.result_path = output_folder / "result.log"
        LOGGER.info("模拟成功任务")

    monkeypatch.setattr(ride_overlay, "run", fake_run)

    assert main([str(tmp_path), "--preview"]) == 0
    assert (output_folder / "result.log").is_file()
    assert not (tmp_path / "result.log").exists()
    assert "模拟成功任务" in (output_folder / "result.log").read_text(encoding="utf-8")


def teardown_module() -> None:
    LOGGER.handlers.clear()
    LOGGER.propagate = True
    LOGGER.setLevel(logging.NOTSET)
