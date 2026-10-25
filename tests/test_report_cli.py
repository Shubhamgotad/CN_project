import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import report
from dashboard.backend import api


def _record(experiment_id, scenario, timestamp=None):
    return {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "experiment_id": experiment_id,
        "scenario": scenario,
        "source": "h1",
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": 3,
        "throughput_mbps": 3,
        "latency_ms": 10,
        "jitter_ms": 1,
        "packet_loss_percent": 0,
        "lost_packets": 0,
        "total_packets": 100,
        "bandwidth_mbps": 10,
        "utilization_percent": 30,
        "active_flows": 1,
        "queue_size": 0,
        "queue_drops": 0,
    }


def _run(root, experiment_id, scenario, finished, *, telemetry=True, valid_state=True):
    directory = root / experiment_id
    output = directory / "output"
    output.mkdir(parents=True)
    finish_time = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    state = {
        "experiment_id": experiment_id,
        "execution_contract_version": 3,
        "scenario": scenario,
        "status": "completed",
        "process_alive": False,
        "process_exit_code": 0,
        "output_directory": str(output),
    }
    if not valid_state:
        state["process_alive"] = True
    state["source_host"] = "all" if scenario == "congestion" else "h1"
    state["flows"] = ["h1", "h2", "h3", "h4"] if scenario == "congestion" else ["h1"]
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    path = output / f"{scenario}.jsonl"
    if telemetry:
        row = _record(experiment_id, scenario)
        row_time = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        finish_time = max(finish_time, row_time + timedelta(seconds=2))
    state["created_at"] = (finish_time - timedelta(minutes=3)).isoformat()
    state["started_at"] = (finish_time - timedelta(minutes=2)).isoformat()
    state["finished_at"] = finish_time.isoformat()
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return path


def test_selects_newest_completed_run_by_state_and_matching_output(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    old = "1" * 32
    newest = "2" * 32
    _run(tmp_path, old, "normal", (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat())
    _run(tmp_path, newest, "burst", datetime.now(timezone.utc).isoformat())
    state, path = report._newest_completed_run()
    assert state["experiment_id"] == newest
    assert state["scenario"] == "burst"
    assert path.name == "burst.jsonl"


def test_older_invalid_run_does_not_displace_a_newer_valid_run(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    older = _run(tmp_path, "1" * 32, "normal", (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat())
    older.write_text("{broken\n", encoding="utf-8")
    newest = "2" * 32
    _run(tmp_path, newest, "burst", datetime.now(timezone.utc).isoformat())
    state, path = report._newest_completed_run()
    assert state["experiment_id"] == newest
    assert path.name == "burst.jsonl"


def test_no_completed_run_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    with pytest.raises(ValueError, match="No completed Traffic Lab run"):
        report._newest_completed_run()


def test_rejects_incomplete_telemetry_instead_of_falling_back(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    _run(tmp_path, "1" * 32, "normal", datetime.now(timezone.utc).isoformat())
    newest_path = _run(tmp_path, "2" * 32, "congestion", datetime.now(timezone.utc).isoformat())
    newest_path.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Newest completed run.*malformed JSON"):
        report._newest_completed_run()


@pytest.mark.parametrize("timestamp, message", [
    ("2026-01-01T00:00:00", "no timezone"),
    ("not-a-timestamp", "invalid timestamp"),
])
def test_rejects_invalid_or_naive_timestamps(tmp_path, monkeypatch, timestamp, message):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    path = _run(tmp_path, "3" * 32, "normal", datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(_record("3" * 32, "normal", timestamp)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        report._newest_completed_run()


def test_rejects_run_id_and_source_host_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    path = _run(tmp_path, "4" * 32, "normal", datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(_record("5" * 32, "normal")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="experiment identity mismatch"):
        report._newest_completed_run()
    path.write_text(json.dumps({**_record("4" * 32, "normal"), "source": "h2"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source host"):
        report._newest_completed_run()


def test_real_legacy_contract_v2_identity_mismatch_is_diagnosed_without_rewriting(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    state_id = "f64f425c68cf4105b906da6f980651b0"
    producer_id = "f9a93200-79d9-45d1-8385-1a333190738c"
    path = _run(tmp_path, state_id, "normal", datetime.now(timezone.utc).isoformat())
    row = _record(producer_id, "normal")
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    state_path = tmp_path / state_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["execution_contract_version"] = 2
    state["source_host"] = None
    state_path.write_text(json.dumps(state), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(ValueError) as error:
        report._newest_completed_run()

    message = str(error.value)
    assert state_id in message
    assert "normal.jsonl" in message
    assert f"expected experiment_id='{state_id}'" in message
    assert f"observed experiment_id='{producer_id}'" in message
    assert path.read_bytes() == original


def test_rejects_scenario_mismatch_with_observed_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    path = _run(tmp_path, "e" * 32, "normal", datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(_record("e" * 32, "moderate")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="observed experiment_id='eeee.*scenario='moderate'"):
        report._newest_completed_run()


@pytest.mark.parametrize("finished", [None, "not-a-time", "2026-10-25T10:00:00"])
def test_rejects_missing_invalid_or_naive_finished_at(tmp_path, monkeypatch, finished):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    _run(tmp_path, "f" * 32, "normal", datetime.now(timezone.utc).isoformat())
    state_path = tmp_path / ("f" * 32) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if finished is None:
        state.pop("finished_at")
    else:
        state["finished_at"] = finished
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="finished_at"):
        report._newest_completed_run()


def test_rejects_measurement_after_authoritative_finished_at(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    path = _run(tmp_path, "8" * 32, "normal", datetime.now(timezone.utc).isoformat())
    row = _record("8" * 32, "normal", (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat())
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="after authoritative finished_at"):
        report._newest_completed_run()


def test_scenario_telemetry_symlink_or_outside_output_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    path = _run(tmp_path, "a" * 32, "normal", datetime.now(timezone.utc).isoformat())
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda candidate: True if candidate == path else original_is_symlink(candidate))
    with pytest.raises(ValueError, match="scenario telemetry.*unsafe|resolves outside"):
        report._newest_completed_run()


def test_multiple_records_are_all_validated_and_newest_timestamp_is_reported(tmp_path):
    run_id = "7" * 32
    old = datetime.now(timezone.utc) - timedelta(seconds=15)
    new = old + timedelta(seconds=3)
    path = tmp_path / "normal.jsonl"
    path.write_text(
        json.dumps(_record(run_id, "normal", old.isoformat())) + "\n"
        + json.dumps(_record(run_id, "normal", new.isoformat())) + "\n",
        encoding="utf-8",
    )
    count, latest = report._validated_records(path, run_id, "normal", {"h1"}, check_freshness=True)
    assert count == 2
    assert latest == new

    path.write_text(
        json.dumps(_record(run_id, "normal", old.isoformat())) + "\n"
        + json.dumps(_record("wrong-run", "normal", new.isoformat())) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="record 2.*observed experiment_id='wrong-run'"):
        report._validated_records(path, run_id, "normal", {"h1"})


def test_rejects_state_output_directory_outside_its_run(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    _run(tmp_path, "b" * 32, "normal", datetime.now(timezone.utc).isoformat())
    state_path = tmp_path / ("b" * 32) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["output_directory"] = str(tmp_path / "other" / "output")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="output_directory"):
        report._newest_completed_run()


def test_rejects_completed_but_invalid_latest_state(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    _run(tmp_path, "1" * 32, "normal", (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    _run(tmp_path, "2" * 32, "burst", datetime.now(timezone.utc).isoformat(), valid_state=False)
    with pytest.raises(ValueError, match="Newest completed run.*rejected"):
        report._newest_completed_run()


def test_missing_required_measurement_field_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    path = _run(tmp_path, "9" * 32, "normal", datetime.now(timezone.utc).isoformat())
    row = _record("9" * 32, "normal")
    row.pop("queue_drops")
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required ML fields: queue_drops"):
        report._newest_completed_run()


def test_invalid_measurement_ranges_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    path = _run(tmp_path, "d" * 32, "normal", datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps({**_record("d" * 32, "normal"), "throughput_mbps": -1}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="incompatible Person 1 ML measurement schema"):
        report._newest_completed_run()


def test_cli_refuses_missing_report_directory_and_model(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(report.REPORT_DIRECTORY_ENV, raising=False)
    assert report.main() != 0
    assert report.REPORT_DIRECTORY_ENV in capsys.readouterr().err
    output = tmp_path / "reports"
    output.mkdir()
    monkeypatch.setenv(report.REPORT_DIRECTORY_ENV, str(output))
    monkeypatch.setattr(report, "MODEL", tmp_path / "missing-model.joblib")
    assert report.main() != 0
    assert "Saved ML model not found" in capsys.readouterr().err


def test_cli_generates_real_model_report_and_api_preserves_identity(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    report_dir = tmp_path / "approved-reports"
    report_dir.mkdir()
    monkeypatch.setattr(report, "RUNS_DIR", runs)
    monkeypatch.setattr(report, "MODEL", report.ROOT / "ai" / "prediction" / "model.joblib")
    monkeypatch.setenv(report.REPORT_DIRECTORY_ENV, str(report_dir))
    monkeypatch.delenv(report.TELEMETRY_FILE_ENV, raising=False)
    experiment_id = "6" * 32
    telemetry = _run(runs, experiment_id, "normal", datetime.now(timezone.utc).isoformat())
    telemetry.write_text(json.dumps(_record(experiment_id, "normal")) + "\n", encoding="utf-8")
    assert report.main() == 0
    output = next(report_dir.glob("*.json"))
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["status"] == "planned"
    assert document["experiment"]["experiment_id"] == experiment_id
    assert document["results"][0]["experiment_id"] == experiment_id
    assert document["results"][0]["scenario"] == "normal"
    assert api._classify_report(document) == "experiment-runner"
    detail = api._build_report_detail(output.stem, document)
    assert detail.runner_records[0].experiment_id == experiment_id
    assert detail.runner_records[0].scenario == "normal"
    assert detail.runner_records[0].timestamp == document["experiment"]["latest_measurement_timestamp"]
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(report_dir))
    listing = api.list_reports()
    assert listing.status == "available"
    assert listing.reports[0].report_id == output.stem
    assert listing.reports[0].input_record_count == 1


def test_cli_creates_unique_report_files_without_overwriting(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    report_dir = tmp_path / "approved-reports"
    report_dir.mkdir()
    monkeypatch.setattr(report, "RUNS_DIR", runs)
    monkeypatch.setattr(report, "MODEL", report.ROOT / "ai" / "prediction" / "model.joblib")
    monkeypatch.setenv(report.REPORT_DIRECTORY_ENV, str(report_dir))
    monkeypatch.delenv(report.TELEMETRY_FILE_ENV, raising=False)
    _run(runs, "c" * 32, "normal", datetime.now(timezone.utc).isoformat())

    assert report.main() == 0
    first = next(report_dir.glob("*.json"))
    first_content = first.read_bytes()
    assert report.main() == 0
    outputs = sorted(report_dir.glob("*.json"))

    assert len(outputs) == 2
    assert outputs[0] != outputs[1]
    assert first.read_bytes() == first_content


def test_configured_telemetry_override_must_match_selected_experiment(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs"
    monkeypatch.setattr(report, "RUNS_DIR", runs)
    state_timestamp = datetime.now(timezone.utc).isoformat()
    _run(runs, "7" * 32, "normal", state_timestamp)
    override = tmp_path / "selected.jsonl"
    override.write_text(json.dumps(_record("8" * 32, "normal")) + "\n", encoding="utf-8")
    monkeypatch.setenv(report.TELEMETRY_FILE_ENV, str(override))
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    monkeypatch.setenv(report.REPORT_DIRECTORY_ENV, str(output_dir))
    assert report.main() == 2
    message = capsys.readouterr().err
    assert "Selected experiment 77777777777777777777777777777777" in message
    assert "expected experiment_id='77777777777777777777777777777777'" in message
    assert "observed experiment_id='88888888888888888888888888888888'" in message


def test_future_measurement_and_model_inference_errors_fail_without_success(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs"
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    monkeypatch.setattr(report, "RUNS_DIR", runs)
    monkeypatch.setattr(report, "MODEL", report.ROOT / "ai" / "prediction" / "model.joblib")
    monkeypatch.setenv(report.REPORT_DIRECTORY_ENV, str(output_dir))
    monkeypatch.delenv(report.TELEMETRY_FILE_ENV, raising=False)
    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    path = _run(runs, "b" * 32, "normal", datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(_record("b" * 32, "normal", future)) + "\n", encoding="utf-8")
    assert report.main() == 2
    assert "after authoritative finished_at" in capsys.readouterr().err

    path.write_text(json.dumps(_record("b" * 32, "normal")) + "\n", encoding="utf-8")
    monkeypatch.setattr(report, "run_experiment", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("model inference failed")))
    assert report.main() == 2
    assert "model/report generation failed" in capsys.readouterr().err


def test_stale_telemetry_and_output_collision_fail_without_overwrite(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    monkeypatch.setattr(report, "RUNS_DIR", runs)
    monkeypatch.setattr(report, "MODEL", report.ROOT / "ai" / "prediction" / "model.joblib")
    monkeypatch.setenv(report.REPORT_DIRECTORY_ENV, str(output_dir))
    old = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
    path = _run(runs, "a" * 32, "normal", datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(_record("a" * 32, "normal", old)) + "\n", encoding="utf-8")
    assert report.main() == 2
    assert list(output_dir.iterdir()) == []

    current = datetime.now(timezone.utc)
    path.write_text(json.dumps(_record("a" * 32, "normal", current.isoformat())) + "\n", encoding="utf-8")
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)
    monkeypatch.setattr(report, "datetime", FrozenDateTime)
    stamp = current.strftime("%Y%m%dT%H%M%S%fZ")
    collision = output_dir / f"report_normal_{'a' * 32}_{stamp}.json"
    collision.write_text("preserve", encoding="utf-8")
    assert report.main() == 2
    assert collision.read_text(encoding="utf-8") == "preserve"
