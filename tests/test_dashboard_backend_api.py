import json
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
        "results": [{
            "line": 1,
            "status": "planned",
            "experiment_id": "exp-1",
            "scenario": "congestion",
            "source": "h1",
            "window": {"timestamp": "2026-10-20T10:00:00+00:00"},
            "freshness": {"status": "fresh", "age_seconds": 12},
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
                    "flow_allocations_mbps": {"5201": 3.6},
                }
            },
        }],
    }


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

    source.write_text('{"timestamp":"not a telemetry row"}\n', encoding="utf-8")
    body = client.get("/api/telemetry/latest").json()
    assert body["status"] == "unavailable"
    assert body["record"] is None


def write_completed_run(root: Path, run_id: str, scenario: str, rows: list[str], *, status="completed", exit_code=0, alive=False):
    run_dir = root / run_id
    output = run_dir / "output"
    output.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({
        "experiment_id": run_id,
        "scenario": scenario,
        "status": status,
        "process_alive": alive,
        "process_exit_code": exit_code,
        "output_directory": "/ignored/WSL/path/output",
    }), encoding="utf-8")
    (output / f"{scenario}.jsonl").write_text("".join(rows), encoding="utf-8")
    return run_dir


def test_latest_telemetry_discovers_fresh_completed_run_output(tmp_path, monkeypatch):
    run_root = tmp_path / ".adaptive-qos-runs"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = person1_telemetry(timestamp)
    record["experiment_id"] = "producer-generated-id"
    record["throughput_mbps"] = 2.997
    write_completed_run(run_root, "d535edb82d3d4e3aa6ab19ac919cb205", "normal", [json.dumps(record) + "\n"])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)

    response = client.get("/api/telemetry/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["source"] == "completed_experiment_person1_jsonl"
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
    payload.update(experiment_id="producer-id", source="h3", scenario="moderate")
    write_completed_run(
        run_root,
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "moderate",
        [json.dumps(payload) + "\n"],
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
    assert body["runner_records"][0]["flow_allocations_mbps"] == {"5201": 3.6}
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
    assert client.get("/api/reports").status_code == 503
    assert client.get("/api/experiments/status").status_code == 503


def test_listing_never_exposes_json_payload_or_absolute_paths(tmp_path, monkeypatch):
    payload = runner_report()
    payload["internal_error"] = str(Path.cwd() / "credentials.txt")
    write_report(tmp_path, "safe-report", payload)
    monkeypatch.setenv(api.REPORT_DIRECTORY_ENV, str(tmp_path))

    response = client.get("/api/reports")
    assert str(Path.cwd()) not in response.text
    assert "internal_error" not in response.text
