import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from dashboard.backend import api


client = TestClient(api.app)


def write_report(directory: Path, report_id: str, payload: dict) -> Path:
    path = directory / f"{report_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def runner_report() -> dict:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "created_at": "2026-10-20T10:00:00+00:00",
        "mode": "adaptive",
        "status": "planned",
        "planning_only": True,
        "dry_run": True,
        "experiment_executed": False,
        "measurements_collected": False,
        "live_qos_applied": False,
        "tc_commands_executed": False,
        "model_path": r"D:\private\model.joblib",
        "counts": {"records_processed": 1},
        "results": [{
            "line": 1,
            "status": "planned",
            "experiment_id": "a" * 32,
            "scenario": "congestion",
            "source": "h1",
            "window": {"timestamp": "2026-10-20T10:00:00+00:00"},
            "freshness": {"status": "fresh", "age_seconds": 12},
            "queue_measurement_quality": {"status": "ready"},
            "measured": {
                "throughput_mbps": 3.2,
                "latency_ms": 20.0,
                "jitter_ms": 0.2,
                "packet_loss_percent": 0.0,
            },
            "modes": {
                "adaptive": {
                    "status": "planned",
                    "prediction": {"congestion": "MEDIUM", "confidence": 0.8},
                    "input_features": {"utilization": 32.0},
                    "service_class_allocation_mbps": {"gaming": 3.6},
                    "flow_allocations_mbps": {"5201": 3.6, "5202": 2.7, "5203": 1.8, "5204": 0.9},
                    "unmatched_leaf": {"rate_mbps": 1.0},
                }
            },
        }],
    }


def test_report_budget_provenance_is_exposed_and_inconsistent_budget_rejected(tmp_path, monkeypatch):
    report = runner_report()
    adaptive = report["results"][0]["modes"]["adaptive"]
    adaptive.update(
        total_bandwidth_mbps=10.0,
        policy_percentages={"gaming": 40, "video": 30, "web": 20, "bulk": 10},
        service_class_allocation_mbps={"gaming": 3.6, "video": 2.7, "web": 1.8, "bulk": 0.9},
    )
    directory = tmp_path / "reports"
    directory.mkdir()
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(directory))
    path = write_report(directory, "budget-report", report)
    assert api._classify_report(report) == "experiment-runner"
    records = api._runner_records(report)
    assert records[0].total_bandwidth_mbps == 10
    assert records[0].policy_percentages["gaming"] == 40

    adaptive["total_bandwidth_mbps"] = 11
    assert api._classify_report(report) is None
    assert path.exists()


@pytest.fixture(autouse=True)
def clear_api_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv(api.REPORT_DIRECTORY_ENV, raising=False)
    monkeypatch.delenv(api.TELEMETRY_FILE_ENV, raising=False)
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", tmp_path / "isolated-experiment-runs")


def test_health_and_topology_are_typed_configured_not_live_claims():
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "adaptive-qos-dashboard-api",
        "api_version": api.API_VERSION,
        "live_network_operations": True,
    }

    topology = client.get("/api/topology")
    assert topology.status_code == 200
    body = topology.json()
    assert body["status"] == "configured"
    assert body["live_connectivity_verified"] is False
    assert body["bottleneck_interface"] == "s1-eth5"
    assert body["hosts"][-1]["ipv4_address"] == "10.0.0.5"
    assert next(link for link in body["links"] if link["source"] == "s1") == {
        "source": "s1",
        "target": "s2",
        "bandwidth_mbps": 10.0,
        "delay": "10ms",
    }


def test_api_has_no_network_changing_http_operations():
    for path in (
        "/api/health",
        "/api/topology",
        "/api/telemetry/latest",
        "/api/telemetry/history",
        "/api/reports",
        "/api/experiments/status",
    ):
        response = client.post(path)
        assert response.status_code == 405


def test_latest_telemetry_is_explicitly_unavailable_without_source():
    response = client.get("/api/telemetry/latest")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["record"] is None
    assert "ADAPTIVE_QOS_TELEMETRY_FILE" in response.json()["message"]


def person1_telemetry(timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "experiment_id": "run-1",
        "scenario": "normal",
        "source": "h1",
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": 3.0,
        "throughput_mbps": 2.9,
        "latency_ms": 20.0,
        "jitter_ms": 0.5,
        "packet_loss_percent": 0.0,
        "lost_packets": 0,
        "total_packets": 100,
        "bandwidth_mbps": 10.0,
        "utilization_percent": 29.0,
        "active_flows": 1,
        "queue_size": None,
        "queue_drops": None,
    }


def test_latest_telemetry_reads_only_explicit_validated_person1_file(tmp_path, monkeypatch):
    source = tmp_path / "telemetry.jsonl"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    source.write_text(json.dumps(person1_telemetry(now)) + "\n", encoding="utf-8")
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))

    response = client.get("/api/telemetry/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["source"] == "configured_person1_jsonl"
    assert body["record"]["source"] == "h1"
    assert 0 <= body["measurement_age_seconds"] <= api.MAX_TELEMETRY_AGE_SECONDS
    assert body["record"]["throughput_mbps"] == 2.9
    assert body["record"]["queue_size"] is None


def test_stale_and_malformed_telemetry_are_not_reported_as_current(tmp_path, monkeypatch):
    source = tmp_path / "telemetry.jsonl"
    source.write_text(
        json.dumps(person1_telemetry("2020-01-01T00:00:00Z")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))
    stale = client.get("/api/telemetry/latest").json()
    assert stale["status"] == "stale"
    assert stale["record"]["source"] == "h1"
    assert stale["measurement_age_seconds"] > api.MAX_TELEMETRY_AGE_SECONDS
    assert "ADAPTIVE_QOS_TELEMETRY_FILE" in stale["message"]
    assert "not mixed" in stale["message"]

    source.write_text('{"timestamp":"not a telemetry row"}\n', encoding="utf-8")
    body = client.get("/api/telemetry/latest").json()
    assert body["status"] == "unavailable"
    assert body["record"] is None


def write_completed_run(root: Path, run_id: str, scenario: str, rows: list[str], *, status="completed", exit_code=0, alive=False, source_host=None):
    run_dir = root / run_id
    output = run_dir / "output"
    output.mkdir(parents=True)
    fixture_sources = set()
    for line in rows:
        try:
            row = json.loads(line)
            if row.get("source") in {"h1", "h2", "h3", "h4"}:
                fixture_sources.add(row["source"])
        except (json.JSONDecodeError, AttributeError):
            pass
    if source_host is None:
        source_host = (next(iter(fixture_sources)) if len(fixture_sources) == 1 else "all") if scenario == "congestion" else (next(iter(fixture_sources)) if len(fixture_sources) == 1 else "h1")
    finished_at = None
    for line in rows:
        try:
            value = json.loads(line).get("timestamp")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and (finished_at is None or parsed > finished_at):
                finished_at = parsed
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            continue
    if finished_at is None:
        finished_at = datetime.now(timezone.utc)
    (run_dir / "state.json").write_text(json.dumps({
        "experiment_id": run_id,
        "scenario": scenario,
        "source_host": source_host,
        "flows": sorted(fixture_sources) if source_host == "all" else [source_host],
        "status": status,
        "process_alive": alive,
        "process_exit_code": exit_code,
        "finished_at": finished_at.isoformat(),
        "output_directory": "/ignored/WSL/path/output",
    }), encoding="utf-8")
    fixture_rows = []
    for line in rows:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            fixture_rows.append(line)
            continue
        # person1_telemetry() is a shared schema fixture; bind its default
        # metadata to the run represented by this completed-run fixture.
        if payload.get("experiment_id") == "run-1":
            payload["experiment_id"] = run_id
            payload["scenario"] = scenario
        fixture_rows.append(json.dumps(payload) + "\n")
    (output / f"{scenario}.jsonl").write_text("".join(fixture_rows), encoding="utf-8")
    return run_dir


def test_latest_telemetry_discovers_fresh_completed_run_output(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = person1_telemetry(timestamp)
    record["experiment_id"] = "d535edb82d3d4e3aa6ab19ac919cb205"
    record["throughput_mbps"] = 2.997
    write_completed_run(run_root, "d535edb82d3d4e3aa6ab19ac919cb205", "normal", [json.dumps(record) + "\n"])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    response = client.get("/api/telemetry/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["source"] == "completed_experiment_person1_jsonl"
    assert body["record"]["experiment_id"] == "d535edb82d3d4e3aa6ab19ac919cb205"
    assert body["record"]["throughput_mbps"] == 2.997
    assert body["record"]["scenario"] == "normal"
    assert 0 <= body["measurement_age_seconds"] <= api.MAX_TELEMETRY_AGE_SECONDS


def test_latest_telemetry_filters_completed_run_by_host_before_selecting_newest(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    run_id = "44444444444444444444444444444444"
    now = time.time()
    hosts = ("h1", "h2", "h3", "h4")
    rows = []
    expected = {}
    for index, host in enumerate(hosts):
        timestamp = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(now - len(hosts) + index),
        )
        payload = person1_telemetry(timestamp)
        payload.update(
            experiment_id=run_id,
            scenario="congestion",
            source=host,
            throughput_mbps=1.25 + index,
            active_flows=4,
        )
        expected[host] = payload
        rows.append(json.dumps(payload) + "\n")
    write_completed_run(run_root, run_id, "congestion", rows)
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    for host, payload in expected.items():
        response = client.get(
            "/api/telemetry/latest",
            params={"host": host, "scenario": "congestion"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "available"
        assert body["source"] == "completed_experiment_person1_jsonl"
        assert body["record"]["experiment_id"] == run_id
        assert body["record"]["source"] == host
        assert body["record"]["scenario"] == "congestion"
        assert body["record"]["timestamp"] == payload["timestamp"]
        assert body["record"]["throughput_mbps"] == payload["throughput_mbps"]
        assert body["record"]["active_flows"] == 4


def test_latest_telemetry_filters_never_fall_back_to_another_host_or_scenario(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    run_id = "55555555555555555555555555555555"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = person1_telemetry(timestamp)
    payload.update(experiment_id=run_id, scenario="congestion", source="h1")
    write_completed_run(run_root, run_id, "congestion", [json.dumps(payload) + "\n"])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    for filters in (
        {"host": "h2", "scenario": "congestion"},
        {"host": "h1", "scenario": "burst"},
    ):
        response = client.get("/api/telemetry/latest", params=filters)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["record"] is None


def test_telemetry_history_returns_chronological_filtered_records_with_provenance(tmp_path, monkeypatch):
    source = tmp_path / "selected-person1.jsonl"
    rows = []
    for source_host, timestamp in [
        ("h2", "2026-01-01T10:02:00Z"),
        ("h1", "2026-01-01T10:01:00Z"),
        ("h1", "2026-01-01T10:03:00Z"),
    ]:
        payload = person1_telemetry(timestamp)
        payload["source"] = source_host
        payload["experiment_id"] = f"experiment-{source_host}-{timestamp[-9:-1]}"
        payload["scenario"] = "normal" if source_host == "h1" else "congestion"
        rows.append(json.dumps(payload) + "\n")
    source.write_text("".join(rows), encoding="utf-8")
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))

    response = client.get("/api/telemetry/history?host=h1&scenario=normal")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["host_filter"] == "h1"
    assert body["scenario_filter"] == "normal"
    assert [record["timestamp"] for record in body["records"]] == [
        "2026-01-01T10:01:00Z",
        "2026-01-01T10:03:00Z",
    ]
    assert all(record["source"] == "h1" for record in body["records"])
    assert all(record["provenance"] == "configured_person1_jsonl" for record in body["records"])
    assert body["records"][0]["interface"] == "s1-eth5"
    assert body["records"][0]["experiment_id"].startswith("experiment-h1")


def test_telemetry_history_preserves_nullable_measurements_and_rejects_malformed_lines(tmp_path, monkeypatch):
    source = tmp_path / "history.jsonl"
    payload = person1_telemetry("2026-01-01T10:00:00Z")
    payload.update(queue_size=None, queue_drops=None)
    missing_fields = json.dumps({"timestamp": "2026-01-01T10:01:00Z"})
    invalid_timestamp = person1_telemetry("2026-01-01T10:02:00")
    source.write_text(
        json.dumps(payload) + "\n{malformed json}\n" + missing_fields + "\n"
        + json.dumps(invalid_timestamp) + "\n{\"timestamp\":\"partial",
        encoding="utf-8",
    )
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))

    response = client.get("/api/telemetry/history")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert len(body["records"]) == 1
    assert body["records"][0]["queue_size"] is None
    assert body["records"][0]["queue_drops"] is None
    assert body["records"][0]["measurement_direction"] is None
    assert body["rejected_records"] == 3


def test_telemetry_history_discovers_only_successfully_completed_experiment_outputs(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    payload = person1_telemetry("2026-01-01T10:00:00Z")
    payload.update(experiment_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", source="h3", scenario="moderate")
    write_completed_run(
        run_root,
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "moderate",
        [json.dumps(payload) + "\n"],
        source_host="h3",
    )
    write_completed_run(
        run_root,
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "normal",
        [json.dumps(person1_telemetry("2026-01-01T10:01:00Z")) + "\n"],
        status="running",
        alive=True,
        exit_code=None,
    )
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    response = client.get("/api/telemetry/history?host=h3&scenario=moderate")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["source_kinds"] == ["completed_experiment_person1_jsonl"]
    assert body["records"][0]["provenance"] == "completed_experiment_person1_jsonl"
    assert body["records"][0]["source"] == "h3"
    assert body["records"][0]["scenario"] == "moderate"


def test_telemetry_history_reports_empty_history_and_rejects_invalid_filters_and_paths(tmp_path, monkeypatch):
    source = tmp_path / "empty.jsonl"
    source.write_text("", encoding="utf-8")
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))

    empty = client.get("/api/telemetry/history")
    assert empty.status_code == 200
    assert empty.json()["status"] == "unavailable"
    assert empty.json()["records"] == []

    assert client.get("/api/telemetry/history?host=h5").status_code == 422
    assert client.get("/api/telemetry/history?scenario=unknown").status_code == 422
    assert client.get("/api/telemetry/history?path=C%3A%5Csecret.jsonl").status_code == 422


def test_telemetry_history_limit_is_bounded_and_keeps_latest_matching_rows(tmp_path, monkeypatch):
    source = tmp_path / "history.jsonl"
    rows = []
    for minute in range(4):
        payload = person1_telemetry(f"2026-01-01T10:0{minute}:00Z")
        payload["experiment_id"] = f"experiment-{minute}"
        rows.append(json.dumps(payload) + "\n")
    source.write_text("".join(rows), encoding="utf-8")
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))

    limited = client.get("/api/telemetry/history?limit=2")
    assert limited.status_code == 200
    body = limited.json()
    assert body["limit"] == 2
    assert body["total_records"] == 4
    assert body["truncated"] is True
    assert [record["timestamp"] for record in body["records"]] == [
        "2026-01-01T10:02:00Z",
        "2026-01-01T10:03:00Z",
    ]
    assert client.get(f"/api/telemetry/history?limit={api.MAX_TELEMETRY_HISTORY_RECORDS + 1}").status_code == 422


def test_completed_run_stale_measurement_remains_stale(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    row = json.dumps(person1_telemetry("2020-01-01T00:00:00Z")) + "\n"
    write_completed_run(run_root, "11111111111111111111111111111111", "normal", [row])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    body = client.get("/api/telemetry/latest").json()
    assert body["status"] == "stale"
    assert body["record"] is not None
    assert body["measurement_age_seconds"] > api.MAX_TELEMETRY_AGE_SECONDS


def test_missing_and_malformed_completed_run_output_is_unavailable(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    write_completed_run(run_root, "22222222222222222222222222222222", "normal", ["{broken json}\n"])
    write_completed_run(run_root, "33333333333333333333333333333333", "normal", [], status="running", alive=True, exit_code=None)
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    body = client.get("/api/telemetry/latest").json()
    assert body["status"] == "unavailable"
    assert body["source"] == "none"
    assert body["record"] is None


def test_completed_run_telemetry_identity_must_match_authoritative_state(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    run_id = "77777777777777777777777777777777"
    payload = person1_telemetry(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    payload["experiment_id"] = "88888888888888888888888888888888"
    write_completed_run(run_root, run_id, "normal", [json.dumps(payload) + "\n"])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)
    response = client.get("/api/telemetry/latest").json()
    assert response["status"] == "unavailable"
    assert response["record"] is None


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "2026-10-25T09:00:00"])
def test_completed_run_rejects_invalid_or_timezone_naive_measurement_timestamps(tmp_path, monkeypatch, timestamp):
    run_root = tmp_path / ".adaptive-qos-runs"
    run_id = "9" * 32
    payload = person1_telemetry(timestamp)
    payload["experiment_id"] = run_id
    write_completed_run(run_root, run_id, "normal", [json.dumps(payload) + "\n"])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    response = client.get("/api/telemetry/latest").json()
    assert response["status"] == "unavailable"
    assert response["record"] is None


def test_multiple_runs_choose_newest_valid_complete_measurement(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    older = json.dumps(person1_telemetry("2020-01-01T10:00:00Z")) + "\n"
    newer_payload = person1_telemetry("2020-01-01T10:01:00Z")
    newer_payload["throughput_mbps"] = 2.997
    # A partial tail is ignored; the last complete valid row remains selectable.
    write_completed_run(run_root, "44444444444444444444444444444444", "normal", [older])
    write_completed_run(run_root, "55555555555555555555555555555555", "normal", [json.dumps(newer_payload) + "\n", "{partial"])
    write_completed_run(run_root, "66666666666666666666666666666666", "normal", [json.dumps(person1_telemetry("2020-01-01T10:02:00Z")) + "\n"], status="failed")
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    body = client.get("/api/telemetry/latest").json()
    assert body["status"] == "stale"  # historical fixture timestamps are never made fresh
    assert body["record"]["throughput_mbps"] == 2.997
    assert body["record"]["timestamp"] == "2020-01-01T10:01:00Z"


def test_fresh_auto_discovery_chooses_newest_metadata_bound_run_and_pinned_stale_file_is_not_mixed(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    now = datetime.now(timezone.utc)
    older_id = "a" * 32
    newer_id = "b" * 32
    older_record = person1_telemetry((now - timedelta(seconds=90)).isoformat())
    older_record["experiment_id"] = older_id
    newer_record = person1_telemetry((now - timedelta(seconds=10)).isoformat())
    newer_record.update(experiment_id=newer_id, throughput_mbps=2.997)
    write_completed_run(run_root, older_id, "normal", [json.dumps(older_record) + "\n"])
    write_completed_run(run_root, newer_id, "normal", [json.dumps(newer_record) + "\n"])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    pinned = tmp_path / "pinned.jsonl"
    pinned.write_text(json.dumps(person1_telemetry("2026-10-20T12:47:37.243232+00:00")) + "\n", encoding="utf-8")
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(pinned))
    response = client.get("/api/telemetry/latest").json()
    assert response["status"] == "stale"
    assert response["record"]["experiment_id"] == "run-1"
    assert "Clear that setting" in response["message"]

    monkeypatch.delenv(api.TELEMETRY_FILE_ENV)
    response = client.get("/api/telemetry/latest").json()
    assert response["status"] == "available"
    assert response["record"]["experiment_id"] == newer_id
    assert response["record"]["timestamp"] == newer_record["timestamp"]


def test_completed_run_rejects_measurement_after_authoritative_finish(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    run_id = "c" * 32
    payload = person1_telemetry(datetime.now(timezone.utc).isoformat())
    payload["experiment_id"] = run_id
    run_dir = write_completed_run(run_root, run_id, "normal", [json.dumps(payload) + "\n"])
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["finished_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    response = client.get("/api/telemetry/latest").json()
    assert response["status"] == "unavailable"
    assert response["record"] is None


def test_telemetry_symlink_is_rejected(tmp_path, monkeypatch):
    target = tmp_path / "target.jsonl"
    target.write_text(json.dumps(person1_telemetry("2026-10-20T10:00:00Z")) + "\n")
    source = tmp_path / "linked.jsonl"
    source.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    original_lstat = Path.lstat

    def lstat_with_symlink(path: Path):
        if path == source:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_size=source.stat().st_size)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_symlink)
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))
    assert client.get("/api/telemetry/latest").json()["status"] == "unavailable"


def test_report_listing_and_detail_return_sanitized_runner_projection(tmp_path, monkeypatch):
    write_report(tmp_path, "runner-1", runner_report())
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(tmp_path))

    listing = client.get("/api/reports")
    assert listing.status_code == 200
    assert listing.json()["status"] == "available"
    assert listing.json()["reports"][0]["report_type"] == "experiment-runner"
    assert listing.json()["reports"][0]["alignment_eligibility"] == "ineligible"

    response = client.get("/api/reports/runner-1")
    assert response.status_code == 200
    body = response.json()
    assert body["runner_records"][0]["prediction"] == "MEDIUM"
    assert body["runner_records"][0]["confidence"] == 0.8
    assert body["runner_records"][0]["freshness_status"] == "stale"
    assert body["runner_records"][0]["flow_allocations_mbps"] == {"5201": 3.6, "5202": 2.7, "5203": 1.8, "5204": 0.9}
    assert body["runner_records"][0]["unmatched_rate_mbps"] == 1.0
    assert body["comparison_ready"] is False
    assert "model_path" not in response.text
    assert "D:\\private" not in response.text


def test_invalid_ids_traversal_and_missing_reports_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(tmp_path))
    assert client.get("/api/reports/missing").status_code == 404
    assert client.get("/api/reports/%2e%2e").status_code == 404
    assert client.get("/api/reports/..%2Foutside").status_code == 404

    monkeypatch.delenv(api.REPORT_DIRECTORY_ENV)
    assert client.get("/api/reports").json()["status"] == "unavailable"
    assert client.get("/api/reports/missing").status_code == 503


def test_malformed_and_unsupported_reports_are_not_returned_as_valid(tmp_path, monkeypatch):
    (tmp_path / "malformed.json").write_text("{broken", encoding="utf-8")
    write_report(tmp_path, "unsupported", {"secret": "not exposed"})
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(tmp_path))

    listing = client.get("/api/reports").json()
    assert len(listing["reports"]) == 2
    assert all(item["status"] == "invalid" for item in listing["reports"])
    assert client.get("/api/reports/malformed").status_code == 422
    assert client.get("/api/reports/unsupported").status_code == 422


def test_report_symlink_escape_is_not_listed_or_opened(tmp_path, monkeypatch):
    report_root = tmp_path / "reports"
    report_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(runner_report()), encoding="utf-8")
    escaped = report_root / "escaped.json"
    escaped.write_text(json.dumps(runner_report()), encoding="utf-8")
    original_resolve = Path.resolve

    def resolve_escape(path: Path, *args, **kwargs):
        if path == escaped:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_escape)
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(report_root))

    listed = client.get("/api/reports").json()["reports"]
    assert listed == [{
        "report_id": "escaped",
        "created_at": None,
        "input_record_count": None,
        "freshness_status": "unknown",
        "report_type": "unknown",
        "status": "invalid",
        "campaign_status": None,
        "alignment_eligibility": "ineligible",
        "comparison_ready": False,
    }]
    assert client.get("/api/reports/escaped").status_code == 404


def test_campaign_completeness_never_implies_alignment_eligibility(tmp_path, monkeypatch):
    campaign = {
        "schema_version": 1,
        "status": "complete",
        "campaign_status": "complete",
        "campaign_id": "campaign-1",
        "complete_cycle_count": 3,
        "minimum_complete_cycles": 3,
        "alignment_status": "blocked",
        "measurement_alignment_eligibility": "ineligible",
        "comparison_ready": False,
        "live_operations_supported": False,
        "manifest_path": str(tmp_path / "private-campaign.json"),
        "trials": [{
            "trial_id": "trial-1",
            "status": "complete",
            "alignment_status": "ineligible",
            "errors": [str(tmp_path / "private-evidence.json")],
        }],
    }
    write_report(tmp_path, "campaign-1", campaign)
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(tmp_path))

    status = client.get("/api/experiments/status")
    assert status.status_code == 200
    body = status.json()
    assert body["campaign_completeness"] == "complete"
    assert body["measurement_alignment_eligibility"] == "ineligible"
    assert body["producer_provenance_status"] == "not_independently_validated"
    assert body["clock_mapping_status"] == "not_independently_validated"
    assert body["comparison_ready"] is False
    assert str(tmp_path) not in status.text

    detail = client.get("/api/reports/campaign-1")
    assert detail.json()["campaign_status"] == "complete"
    assert detail.json()["alignment_eligibility"] == "ineligible"
    assert detail.json()["comparison_ready"] is False


def test_missing_report_directory_is_explicitly_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(tmp_path / "missing"))
    body = client.get("/api/reports").json()
    assert body["status"] == "unavailable"
    assert body["directory_status"] == "invalid"
    assert "does not exist" in body["message"]


def test_report_directory_states_are_explicit_and_env_is_read_per_request(tmp_path, monkeypatch):
    monkeypatch.delenv(api.REPORT_DIRECTORY_ENV, raising=False)
    unset = client.get("/api/reports").json()
    assert unset["directory_status"] == "unset"
    assert "unset" in unset["message"]

    directory = tmp_path / "approved"
    directory.mkdir()
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(directory))
    empty = client.get("/api/reports").json()
    assert empty["directory_status"] == "valid"
    assert empty["reports"] == []
    assert "contains no compatible reports" in empty["message"]

    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(api.Path(__file__).resolve().parents[1]))
    rejected = client.get("/api/reports").json()
    assert rejected["directory_status"] == "invalid"
    assert "outside the repository" in rejected["message"]
    assert str(api.Path(__file__).resolve().parents[1]) not in rejected["message"]


def test_inaccessible_report_directory_has_distinct_status(tmp_path, monkeypatch):
    directory = tmp_path / "no-read"
    directory.mkdir()
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(directory))
    original_access = api.os.access
    monkeypatch.setattr(api.os, "access", lambda path, mode: False if Path(path) == directory else original_access(path, mode))
    response = client.get("/api/reports").json()
    assert response["directory_status"] == "inaccessible"
    assert response["reports"] == []


def test_invalid_configured_telemetry_does_not_mix_completed_run_discovery(tmp_path, monkeypatch):
    configured = tmp_path / "broken.jsonl"
    configured.write_text("{broken\n", encoding="utf-8")
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(configured))
    monkeypatch.setattr(api, "_completed_experiment_telemetry", lambda **_kwargs: [
        (api.datetime.now(api.timezone.utc), {
            "timestamp": api.datetime.now(api.timezone.utc).isoformat(),
            "experiment_id": "a" * 32, "scenario": "normal", "source": "h1",
        })
    ])
    response = client.get("/api/telemetry/latest").json()
    assert response["status"] == "unavailable"
    assert response["source"] == "configured_person1_jsonl"
    assert "not mixed" in response["message"]


def test_listing_never_exposes_json_payload_or_absolute_paths(tmp_path, monkeypatch):
    payload = runner_report()
    payload["internal_error"] = str(Path.cwd() / "credentials.txt")
    write_report(tmp_path, "safe-report", payload)
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(tmp_path))

    response = client.get("/api/reports")
    assert str(Path.cwd()) not in response.text
    assert "internal_error" not in response.text
