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
def clear_api_configuration(monkeypatch):
    monkeypatch.delenv(api.REPORT_DIRECTORY_ENV, raising=False)
    monkeypatch.delenv(api.TELEMETRY_FILE_ENV, raising=False)


def test_health_and_topology_are_typed_configured_not_live_claims():
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "adaptive-qos-readonly-api",
        "api_version": api.API_VERSION,
        "live_network_operations": False,
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
    assert body["record"]["throughput_mbps"] == 2.9
    assert body["record"]["queue_size"] is None


def test_stale_and_malformed_telemetry_are_not_reported_as_current(tmp_path, monkeypatch):
    source = tmp_path / "telemetry.jsonl"
    source.write_text(
        json.dumps(person1_telemetry("2020-01-01T00:00:00Z")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(api.TELEMETRY_FILE_ENV, str(source))
    assert client.get("/api/telemetry/latest").json()["status"] == "stale"

    source.write_text('{"timestamp":"not a telemetry row"}\n', encoding="utf-8")
    body = client.get("/api/telemetry/latest").json()
    assert body["status"] == "unavailable"
    assert body["record"] is None


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
