import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from ai.prediction import train_random_forest
from integration import experiment_runner
from integration import telemetry_qos
from integration.telemetry_qos import QoSIntegrationConfig
from qos import tc_controller


def telemetry_record(
    *,
    timestamp="2026-10-09T00:00:00Z",
    experiment_id="run-1",
    queue_size=None,
    queue_drops=None,
):
    return {
        "timestamp": timestamp,
        "experiment_id": experiment_id,
        "scenario": "normal",
        "source": "h1",
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": 3.0,
        "throughput_mbps": 3.0,
        "latency_ms": 20.26,
        "jitter_ms": 0.014,
        "packet_loss_percent": 0.0,
        "lost_packets": 0,
        "total_packets": 428,
        "bandwidth_mbps": 10.0,
        "utilization_percent": 30.0,
        "active_flows": 1,
        "queue_size": queue_size,
        "queue_drops": queue_drops,
    }


@pytest.fixture(scope="module")
def model_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("experiment-model") / "model.joblib"
    records = [
        {"timestamp": i, "interface": "test0", "throughput_mbps": rate,
         "bandwidth_mbps": 10, "utilization": util, "latency_ms": latency,
         "jitter_ms": 1, "packet_loss_pct": loss, "active_flows": 1}
        for i, (rate, util, latency, loss) in enumerate((
            (1, 10, 10, 0), (2, 20, 20, 0), (3, 30, 30, 0.1),
            (6, 60, 60, 0.6), (6.5, 65, 70, 0.8),
            (7, 70, 80, 0.7), (8, 80, 100, 2),
            (9, 90, 120, 3), (9.5, 95, 150, 5),
        ))
    ]
    train_random_forest(records, path, random_state=11)
    return path


@pytest.fixture
def qos_config():
    return QoSIntegrationConfig(
        interface="s1-eth5",
        total_bandwidth_mbps=10.0,
        flow_policy_classes={
            5201: "gaming", 5202: "video", 5203: "web", 5204: "bulk"
        },
        filter_priorities={5201: 10, 5202: 20, 5203: 30, 5204: 40},
        dry_run=True,
    )


def write_jsonl(path: Path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_adaptive_runner_uses_existing_prediction_and_allocation_path(
    tmp_path, model_path, qos_config
):
    source = tmp_path / "telemetry.jsonl"
    output = tmp_path / "result.json"
    write_jsonl(source, [telemetry_record(queue_size=None, queue_drops=None)])

    report = experiment_runner.run_experiment(
        source,
        output,
        mode="adaptive",
        model_path=model_path,
        qos_config=qos_config,
    )

    result = report["results"][0]
    adaptive = result["modes"]["adaptive"]
    assert report["counts"] == {
        "records": 1, "valid_records": 1, "invalid_records": 0,
        "records_processed": 1, "invalid_telemetry_records": 0,
        "baseline_plans_generated": 0, "adaptive_plans_generated": 1,
        "adaptive_decision_failures": 0, "decision_errors": 0,
        "records_blocked_by_strict_quality_or_freshness": 0,
    }
    assert report["status"] == "planned"
    assert adaptive["status"] == "planned"
    assert adaptive["prediction"]["congestion"] in {"LOW", "MEDIUM", "HIGH"}
    assert 0 <= adaptive["prediction"]["confidence"] <= 1
    assert set(adaptive["input_features"]) == {
        "utilization", "throughput_mbps", "latency_ms", "jitter_ms",
        "packet_loss_pct", "active_flows",
    }
    assert sum(adaptive["flow_allocations_mbps"].values()) == pytest.approx(9)
    assert adaptive["unmatched_leaf"] == {
        "classid": "1:15", "rate_mbps": 1.0, "ceil_mbps": 1.0,
    }
    assert result["measured"]["throughput_mbps"] == 3.0
    assert result["measured"]["queue_size"] is None
    assert result["measured"]["queue_drops"] is None
    assert adaptive["planning_only"] is True
    assert report["planning_only"] is True
    assert report["experiment_executed"] is False
    assert report["measurements_collected"] is False
    assert report["live_qos_applied"] is False
    assert report["tc_commands_executed"] is False
    assert adaptive["tc_commands_executed"] is False
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["run_id"] == report["run_id"]
    assert written["counts"] == report["counts"]
    assert written["results"][0]["modes"]["adaptive"]["prediction"] == adaptive["prediction"]


def test_both_modes_pair_each_result_to_same_record_window(
    tmp_path, model_path, qos_config
):
    source = tmp_path / "telemetry.jsonl"
    output = tmp_path / "both.json"
    inputs = [
        telemetry_record(experiment_id="first"),
        telemetry_record(
            timestamp="2026-10-09T00:00:01Z",
            experiment_id="second",
        ),
    ]
    write_jsonl(source, inputs)

    report = experiment_runner.run_experiment(
        source,
        output,
        mode="both",
        model_path=model_path,
        qos_config=qos_config,
    )

    assert report["counts"]["valid_records"] == 2
    for input_record, result in zip(inputs, report["results"]):
        assert result["window"]["timestamp"] == input_record["timestamp"]
        assert result["modes"]["baseline"]["status"] == "planned"
        assert result["modes"]["baseline"]["prediction_used_for_policy"] is False
        assert result["modes"]["baseline"]["allocation"]["flow_allocations_mbps"] is None
        assert result["modes"]["baseline"]["policy"]["aggregate_class"]["rate_mbps"] == 10
        assert result["modes"]["adaptive"]["status"] == "planned"
        assert result["modes"]["adaptive"]["prediction"]["congestion"] in {
            "LOW", "MEDIUM", "HIGH"
        }


def test_invalid_json_and_invalid_record_are_reported_per_line(tmp_path):
    source = tmp_path / "bad.jsonl"
    output = tmp_path / "report.json"
    valid = telemetry_record()
    missing = telemetry_record(experiment_id="missing-latency")
    del missing["latency_ms"]
    source.write_text(
        json.dumps(valid) + "\nnot-json\n" + json.dumps(missing)
        + '\n{"throughput_mbps":NaN}\n',
        encoding="utf-8",
    )

    report = experiment_runner.run_experiment(source, output, mode="baseline")

    assert report["counts"]["records"] == 4
    assert report["counts"]["valid_records"] == 1
    assert report["counts"]["invalid_records"] == 3
    assert report["status"] == "incomplete"
    malformed, invalid, nonstandard = report["results"][1:]
    assert malformed["errors"][0]["type"] == "JSONDecodeError"
    assert malformed["experiment_id"] is None
    assert invalid["experiment_id"] == "missing-latency"
    assert "latency_ms" in invalid["errors"][0]["message"]
    assert invalid["measured"]["latency_ms"] is None
    assert "non-standard JSON numeric constant" in nonstandard["errors"][0]["message"]


def test_empty_input_writes_empty_report(tmp_path):
    source = tmp_path / "empty.jsonl"
    output = tmp_path / "empty-result.json"
    source.write_text("\n \n", encoding="utf-8")

    report = experiment_runner.run_experiment(source, output, mode="baseline")

    assert report["status"] == "incomplete"
    assert report["counts"]["records_processed"] == 0
    assert report["counts"]["invalid_telemetry_records"] == 0
    assert report["results"] == []


@pytest.mark.parametrize(
    ("contents", "expected_status"),
    [
        (" \n\t\n", "incomplete"),
        ("not-json\nalso-not-json\n", "invalid_input"),
    ],
)
def test_cli_returns_nonzero_and_preserves_report_for_empty_or_all_invalid_input(
    tmp_path, contents, expected_status, capsys
):
    source = tmp_path / "input.jsonl"
    output = tmp_path / "report.json"
    source.write_text(contents, encoding="utf-8")

    result = experiment_runner.main([
        "--input", str(source),
        "--output", str(output),
        "--mode", "baseline",
    ])

    assert result == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == expected_status
    assert len(report["results"]) == report["counts"]["records_processed"]
    assert report["planning_only"] is True
    assert report["experiment_executed"] is False
    assert report["measurements_collected"] is False
    assert report["live_qos_applied"] is False
    assert report["tc_commands_executed"] is False
    assert report["dry_run"] is True
    assert result == (0 if report["status"] == "planned" else 1)
    assert capsys.readouterr().out


def test_mixed_validity_preserves_diagnostics_and_plans_valid_rows(tmp_path):
    source = tmp_path / "mixed.jsonl"
    output = tmp_path / "mixed-report.json"
    valid = telemetry_record(queue_size=None, queue_drops=None)
    source.write_text(
        json.dumps(valid) + "\nnot-json\n",
        encoding="utf-8",
    )

    report = experiment_runner.run_experiment(source, output, mode="baseline")

    assert report["status"] == "incomplete"
    assert report["counts"]["records_processed"] == 2
    assert report["counts"]["invalid_telemetry_records"] == 1
    assert report["counts"]["baseline_plans_generated"] == 1
    assert report["results"][1]["errors"][0]["type"] == "JSONDecodeError"
    assert report["results"][0]["queue_measurement_quality"]["status"] == "not_required_missing"


@pytest.mark.parametrize("mode", ["adaptive", "both"])
def test_all_adaptive_decisions_failing_are_not_reported_as_successful(
    tmp_path, model_path, qos_config, monkeypatch, mode
):
    source = tmp_path / f"all-fail-{mode}.jsonl"
    output = tmp_path / f"all-fail-{mode}.json"
    write_jsonl(source, [telemetry_record()])

    def fail(*args, **kwargs):
        raise ValueError("intentional planning failure")

    monkeypatch.setattr(telemetry_qos.CongestionPredictor, "predict", fail)
    report = experiment_runner.run_experiment(
        source,
        output,
        mode=mode,
        model_path=model_path,
        qos_config=qos_config,
    )

    assert report["status"] == ("planning_failed" if mode == "adaptive" else "incomplete")
    assert report["counts"]["adaptive_plans_generated"] == 0
    assert report["counts"]["adaptive_decision_failures"] == 1
    assert report["counts"]["baseline_plans_generated"] == (1 if mode == "both" else 0)
    if mode == "both":
        assert report["results"][0]["modes"]["baseline"]["status"] == "planned"
        assert report["results"][0]["modes"]["adaptive"]["status"] == "planning_failed"


def test_partial_adaptive_failure_keeps_plans_and_record_error(
    tmp_path, model_path, qos_config, monkeypatch, capsys
):
    source = tmp_path / "partial.jsonl"
    output = tmp_path / "partial.json"
    write_jsonl(source, [
        telemetry_record(experiment_id="good"),
        telemetry_record(
            timestamp="2026-10-09T00:00:01Z", experiment_id="bad"
        ),
    ])
    original = experiment_runner.build_qos_integration

    def fail_one(record, **kwargs):
        if record["experiment_id"] == "bad":
            raise ValueError("bad adaptive decision")
        return original(record, **kwargs)

    monkeypatch.setattr(experiment_runner, "build_qos_integration", fail_one)
    report = experiment_runner.run_experiment(
        source,
        output,
        mode="adaptive",
        model_path=model_path,
        qos_config=qos_config,
    )

    assert report["status"] == "incomplete"
    assert report["counts"]["adaptive_plans_generated"] == 1
    assert report["counts"]["adaptive_decision_failures"] == 1
    assert report["results"][0]["modes"]["adaptive"]["status"] == "planned"
    assert report["results"][1]["modes"]["adaptive"]["error"]["message"] == "bad adaptive decision"

    cli_output = tmp_path / "partial-cli.json"
    exit_code = experiment_runner.main([
        "--input", str(source),
        "--output", str(cli_output),
        "--mode", "adaptive",
        "--model", str(model_path),
        "--flow-policy-classes",
        '{"5201":"gaming","5202":"video","5203":"web","5204":"bulk"}',
        "--filter-priorities",
        '{"5201":10,"5202":20,"5203":30,"5204":40}',
    ])
    cli_report = json.loads(cli_output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert cli_report["status"] == "incomplete"
    assert cli_report["counts"]["adaptive_plans_generated"] == 1
    assert cli_report["counts"]["adaptive_decision_failures"] == 1
    capsys.readouterr()


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_baseline_plans"),
    [
        ("adaptive", "planning_failed", 0),
        ("both", "incomplete", 1),
    ],
)
def test_cli_returns_nonzero_when_every_adaptive_decision_fails(
    tmp_path, model_path, monkeypatch, capsys,
    mode, expected_status, expected_baseline_plans,
):
    source = tmp_path / f"adaptive-fails-{mode}.jsonl"
    output = tmp_path / f"adaptive-fails-{mode}.json"
    write_jsonl(source, [telemetry_record()])

    def fail(*args, **kwargs):
        raise ValueError("intentional inference failure")

    monkeypatch.setattr(telemetry_qos.CongestionPredictor, "predict", fail)
    result = experiment_runner.main([
        "--input", str(source),
        "--output", str(output),
        "--mode", mode,
        "--model", str(model_path),
        "--flow-policy-classes",
        '{"5201":"gaming","5202":"video","5203":"web","5204":"bulk"}',
        "--filter-priorities",
        '{"5201":10,"5202":20,"5203":30,"5204":40}',
    ])

    assert result == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == expected_status
    assert report["counts"]["baseline_plans_generated"] == expected_baseline_plans
    assert report["counts"]["adaptive_decision_failures"] == 1
    assert report["results"][0]["modes"]["adaptive"]["error"]["message"] == (
        "intentional inference failure"
    )
    assert report["planning_only"] is True
    assert report["experiment_executed"] is False
    assert report["measurements_collected"] is False
    assert report["live_qos_applied"] is False
    assert report["tc_commands_executed"] is False
    assert report["dry_run"] is True
    assert result == (0 if report["status"] == "planned" else 1)
    assert capsys.readouterr().out


@pytest.mark.parametrize(
    ("timestamp", "expected", "message"),
    [
        ("2026-10-17T11:59:00Z", "fresh", None),
        ("2026-10-17T11:54:59Z", "stale", "maximum allowed age"),
        ("2026-10-17T12:00:31Z", "future", "maximum allowed clock skew"),
        ("not-a-timestamp", "invalid", "malformed"),
        ("2026-10-17T11:59:00", "invalid", "timezone offset"),
    ],
)
def test_strict_freshness_policy_uses_fixed_clock(
    tmp_path, timestamp, expected, message
):
    source = tmp_path / "freshness.jsonl"
    output = tmp_path / f"freshness-{expected}.json"
    write_jsonl(source, [telemetry_record(timestamp=timestamp, queue_size=2, queue_drops=0)])

    report = experiment_runner.run_experiment(
        source,
        output,
        mode="baseline",
        enforce_freshness=True,
        now=datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
    )
    record = report["results"][0]
    assert record["freshness"]["status"] == expected
    if expected == "fresh":
        assert report["counts"]["baseline_plans_generated"] == 1
    else:
        assert report["counts"]["records_blocked_by_strict_quality_or_freshness"] == 1
        assert message in record["freshness"]["errors"][0]
        if expected in {"stale", "future"}:
            assert record["modes"]["baseline"]["status"] == "blocked"
        else:
            assert record["status"] == "invalid"


def test_old_timestamp_remains_usable_without_freshness_opt_in(tmp_path):
    source = tmp_path / "historical.jsonl"
    output = tmp_path / "historical.json"
    write_jsonl(source, [telemetry_record(timestamp="2001-01-01T00:00:00Z")])

    report = experiment_runner.run_experiment(source, output, mode="baseline")

    assert report["status"] == "planned"
    assert report["counts"]["baseline_plans_generated"] == 1
    assert report["results"][0]["freshness"]["status"] == "not_enforced"


@pytest.mark.parametrize(
    ("field", "value", "remove"),
    [
        ("queue_size", None, False),
        ("queue_size", None, True),
        ("queue_drops", True, False),
        ("queue_size", "3", False),
        ("queue_size", -1, False),
        ("queue_size", float("nan"), False),
        ("queue_size", float("inf"), False),
        ("queue_size", float("-inf"), False),
    ],
)
def test_strict_queue_quality_blocks_invalid_queue_measurements(
    tmp_path, field, value, remove
):
    source = tmp_path / "queue-invalid.jsonl"
    output = tmp_path / f"queue-invalid-{field}-{remove}-{value}.json"
    record = telemetry_record(queue_size=1, queue_drops=2)
    if remove:
        record.pop(field)
    else:
        record[field] = value
    write_jsonl(source, [record])

    report = experiment_runner.run_experiment(
        source, output, mode="baseline", require_queue_measurements=True
    )

    assert report["counts"]["baseline_plans_generated"] == 0
    assert report["counts"]["records_blocked_by_strict_quality_or_freshness"] == 1
    assert report["results"][0]["queue_measurement_quality"]["status"] == "blocked"
    assert report["results"][0]["modes"]["baseline"]["status"] == "blocked"
    assert report["results"][0]["status"] == "blocked"
    assert report["results"][0]["queue_measurement_quality"]["errors"]


def test_valid_queue_measurements_pass_strict_quality_policy(tmp_path):
    source = tmp_path / "queue-valid.jsonl"
    output = tmp_path / "queue-valid.json"
    write_jsonl(source, [telemetry_record(queue_size=0, queue_drops=3.5)])

    report = experiment_runner.run_experiment(
        source, output, mode="baseline", require_queue_measurements=True
    )

    assert report["status"] == "planned"
    assert report["counts"]["baseline_plans_generated"] == 1
    assert report["results"][0]["queue_measurement_quality"]["status"] == "ready"
    assert report["results"][0]["queue_measurement_quality"]["ready"] is True


def test_baseline_only_planning_does_not_require_a_model(tmp_path):
    source = tmp_path / "baseline-only.jsonl"
    output = tmp_path / "baseline-only.json"
    write_jsonl(source, [telemetry_record(queue_size=1, queue_drops=2)])

    report = experiment_runner.run_experiment(source, output, mode="baseline")

    assert report["status"] == "planned"
    assert report["model_path"] is None
    assert report["counts"]["baseline_plans_generated"] == 1
    assert report["counts"]["adaptive_plans_generated"] == 0
    quality = report["results"][0]["queue_measurement_quality"]
    assert quality["status"] == "ready"
    assert quality["enforced"] is False


@pytest.mark.parametrize("missing_field", ["queue_size", "queue_drops"])
def test_missing_queue_metadata_remains_permissive_by_default(
    tmp_path, missing_field
):
    source = tmp_path / "queue-missing.jsonl"
    output = tmp_path / "queue-missing.json"
    record = telemetry_record(queue_size=4, queue_drops=0)
    record.pop(missing_field)
    write_jsonl(source, [record])

    report = experiment_runner.run_experiment(source, output, mode="baseline")

    assert report["status"] == "planned"
    assert report["counts"]["baseline_plans_generated"] == 1
    assert report["results"][0]["measured"][missing_field] is None
    assert report["results"][0]["queue_measurement_quality"]["status"] == "not_required_missing"


def test_output_path_must_not_overwrite_input_or_existing_output(tmp_path):
    source = tmp_path / "input.jsonl"
    write_jsonl(source, [telemetry_record()])

    with pytest.raises(ValueError, match="must not overwrite"):
        experiment_runner.run_experiment(source, source, mode="baseline")

    existing = tmp_path / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        experiment_runner.run_experiment(source, existing, mode="baseline")
    assert existing.read_text(encoding="utf-8") == "keep"

    with pytest.raises(FileNotFoundError, match="output directory"):
        experiment_runner.run_experiment(
            source, tmp_path / "missing-dir" / "result.json", mode="baseline"
        )


@pytest.mark.parametrize(
    ("failure_target", "failure_message"),
    [
        ("inference", "model prediction failed"),
        ("allocation", "policy allocation failed"),
    ],
)
def test_adaptive_pipeline_errors_are_reported_without_fabricated_result(
    tmp_path, model_path, qos_config, monkeypatch,
    failure_target, failure_message,
):
    source = tmp_path / "telemetry.jsonl"
    output = tmp_path / "error.json"
    write_jsonl(source, [telemetry_record()])

    def fail(*args, **kwargs):
        raise ValueError(failure_message)

    if failure_target == "inference":
        monkeypatch.setattr(telemetry_qos.CongestionPredictor, "predict", fail)
    else:
        monkeypatch.setattr(
            telemetry_qos, "build_qos_decision_from_prediction", fail
        )
    report = experiment_runner.run_experiment(
        source,
        output,
        mode="adaptive",
        model_path=model_path,
        qos_config=qos_config,
    )
    adaptive = report["results"][0]["modes"]["adaptive"]
    assert report["counts"]["valid_records"] == 1
    assert report["counts"]["decision_errors"] == 1
    assert report["status"] == "planning_failed"
    assert adaptive["status"] == "planning_failed"
    assert adaptive["error"] == {
        "type": "ValueError",
        "message": failure_message,
    }
    assert "prediction" not in adaptive
    assert "flow_allocations_mbps" not in adaptive


def test_runner_never_invokes_live_apply_or_network_subprocess(
    tmp_path, model_path, qos_config, monkeypatch
):
    source = tmp_path / "telemetry.jsonl"
    output = tmp_path / "dry-run.json"
    write_jsonl(source, [telemetry_record()])

    def forbidden(*args, **kwargs):
        raise AssertionError("live execution path must not be called")

    monkeypatch.setattr(experiment_runner, "subprocess", None, raising=False)
    monkeypatch.setattr(tc_controller, "apply_tc_policy", forbidden)
    monkeypatch.setattr(tc_controller, "apply_managed_htb_policy", forbidden)
    monkeypatch.setattr(tc_controller, "restore_managed_htb_hierarchy", forbidden)
    monkeypatch.setattr(tc_controller, "restore_original_hierarchy", forbidden)
    monkeypatch.setattr(tc_controller, "apply_tc_commands", forbidden)
    monkeypatch.setattr(telemetry_qos, "apply_managed_htb_policy", forbidden)
    monkeypatch.setattr(telemetry_qos, "apply_integration_commands", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    report = experiment_runner.run_experiment(
        source,
        output,
        mode="adaptive",
        model_path=model_path,
        qos_config=qos_config,
    )

    assert report["dry_run"] is True
    assert report["results"][0]["modes"]["adaptive"]["tc_commands_executed"] is False
    assert report["experiment_executed"] is False
    assert report["measurements_collected"] is False
