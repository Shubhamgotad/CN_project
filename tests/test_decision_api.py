import json
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from dashboard.backend import api
from dashboard.backend.decision_loop import DecisionLoop


client = TestClient(api.app)


def telemetry(status="available"):
    return api.LatestTelemetryResponse(
        status=status,
        source="configured_person1_jsonl" if status != "unavailable" else "none",
        measurement_age_seconds=3.0 if status == "available" else 400.0,
        message="sample telemetry status",
        record=api.TelemetryRecordResponse(
            timestamp="2026-10-20T10:00:00+00:00",
            source="h1",
            scenario="normal",
            throughput_mbps=2.997,
            latency_ms=20.63,
            jitter_ms=0.03,
            packet_loss_percent=0.0,
            queue_size=4,
            queue_drops=0,
            measurement_direction="receiver",
            interface="s1-eth5",
            bandwidth_mbps=10.0,
            utilization_percent=29.97,
            active_flows=1,
        ) if status != "unavailable" else None,
    )


def write_congestion_run(root: Path, run_id: str, rows: list[dict]) -> Path:
    run_dir = root / run_id
    output = run_dir / "output"
    output.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({
        "experiment_id": run_id,
        "scenario": "congestion",
        "status": "completed",
        "process_alive": False,
        "process_exit_code": 0,
    }), encoding="utf-8")
    (output / "congestion.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return run_dir


def decision_telemetry(host: str, timestamp: str, *, utilization: float, latency: float, loss: float) -> dict:
    return {
        "timestamp": timestamp,
        "experiment_id": "66666666666666666666666666666666",
        "scenario": "congestion",
        "source": host,
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": 3.0,
        "throughput_mbps": utilization / 100 * 10,
        "latency_ms": latency,
        "jitter_ms": 0.5,
        "packet_loss_percent": loss,
        "lost_packets": 0,
        "total_packets": 100,
        "bandwidth_mbps": 10.0,
        "utilization_percent": utilization,
        "active_flows": 4,
        "queue_size": 100,
        "queue_drops": 0,
    }


def test_decision_api_returns_real_input_and_non_applied_proposal(monkeypatch):
    monkeypatch.setattr(api, "latest_telemetry", lambda host=None, scenario=None: telemetry())
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(api.tc_controller, "LIVE_TC_OWNERSHIP_GUARANTEED", False)

    response = client.get("/api/qos/decision")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["measurement"]["throughput_mbps"] == 2.997
    assert body["measurement"]["utilization_percent"] == 29.97
    assert body["classification"] == "LOW"
    assert body["selected_profile"] == "LOW"
    assert body["candidate_profile"] is None
    assert body["proposed_policy"]["interface"] == "s1-eth5"
    assert body["thresholds"] == {
        "utilization_medium_percent": 60.0,
        "utilization_high_percent": 80.0,
        "latency_medium_ms": 50.0,
        "latency_high_ms": 100.0,
        "packet_loss_medium_percent": 0.5,
        "packet_loss_high_percent": 2.0,
    }
    assert body["proposed_policy"]["flow_mappings"] == [
        {"port": 5201, "service": "gaming", "class_id": "1:11", "priority": 10},
        {"port": 5202, "service": "video", "class_id": "1:12", "priority": 20},
        {"port": 5203, "service": "web", "class_id": "1:13", "priority": 30},
        {"port": 5204, "service": "bulk", "class_id": "1:14", "priority": 40},
    ]
    assert body["proposed_policy"]["service_ceilings_mbps"] == {
        "gaming": 2.7,
        "video": 2.7,
        "web": 2.25,
        "bulk": 1.35,
    }
    assert body["proposed_policy"]["netem_delay"] == "10ms"
    assert body["proposed_policy"]["netem_limit"] == 1000
    assert body["tc_commands"]
    assert body["dry_run"] is True and body["applied"] is False
    assert body["execution_state"] == "dry_run"
    assert body["application_enabled"] is False


def test_decision_api_filters_each_host_before_classification_and_profile_planning(tmp_path, monkeypatch):
    run_id = "66666666666666666666666666666666"
    run_root = tmp_path / ".adaptive-qos-runs"
    base = datetime.now(timezone.utc)
    samples = {
        "h1": (20.0, 20.0, 0.0, "LOW"),
        "h2": (65.0, 60.0, 0.0, "MEDIUM"),
        "h3": (90.0, 120.0, 3.0, "HIGH"),
        "h4": (75.0, 20.0, 0.0, "LOW"),
    }
    expected = {}
    rows = []
    for index, (host, (utilization, latency, loss, classification)) in enumerate(samples.items()):
        timestamp = (base + timedelta(milliseconds=index)).isoformat()
        row = decision_telemetry(
            host,
            timestamp,
            utilization=utilization,
            latency=latency,
            loss=loss,
        )
        expected[host] = (row, classification)
        rows.append(row)
    write_congestion_run(run_root, run_id, rows)
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)
    monkeypatch.delenv(api.TELEMETRY_FILE_ENV, raising=False)
    monkeypatch.delenv(api.QOS_APPLICATION_ENV, raising=False)

    for host, (row, classification) in expected.items():
        api._scoped_decision_loops.pop((host, "congestion"), None)
        response = client.get(
            "/api/qos/decision",
            params={"host": host, "scenario": "congestion"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["telemetry_status"] == "available"
        assert body["measurement"]["source"] == host
        assert body["measurement"]["scenario"] == "congestion"
        assert body["measurement"]["experiment_id"] == run_id
        assert body["measurement"]["timestamp"] == row["timestamp"]
        assert body["measurement"]["utilization_percent"] == row["utilization_percent"]
        assert body["classification"] == classification
        assert body["selected_profile"] == classification
        assert body["proposed_policy"]["service_allocations_mbps"]
        assert body["dry_run"] is True
        assert body["applied"] is False
        assert body["application_enabled"] is False


def test_decision_api_missing_or_mismatched_host_and_scenario_have_no_proposal(tmp_path, monkeypatch):
    run_id = "77777777777777777777777777777777"
    run_root = tmp_path / ".adaptive-qos-runs"
    timestamp = datetime.now(timezone.utc).isoformat()
    row = decision_telemetry("h1", timestamp, utilization=85.0, latency=120.0, loss=3.0)
    write_congestion_run(run_root, run_id, [row])
    monkeypatch.setattr(api, "EXPERIMENT_RUN_ROOT", run_root)
    monkeypatch.delenv(api.TELEMETRY_FILE_ENV, raising=False)

    for filters in (
        {"host": "h2", "scenario": "congestion"},
        {"host": "h1", "scenario": "burst"},
    ):
        response = client.get("/api/qos/decision", params=filters)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["measurement"] is None
        assert body["classification"] is None
        assert body["selected_profile"] is None
        assert body["proposed_policy"] is None
        assert body["tc_commands"] == []


def test_decision_api_rejects_stale_and_missing_telemetry(monkeypatch):
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.setattr(api, "latest_telemetry", lambda host=None, scenario=None: telemetry("stale"))
    stale = client.get("/api/qos/decision").json()
    assert stale["status"] == "stale"
    assert stale["proposed_policy"] is None and stale["tc_commands"] == []

    monkeypatch.setattr(api, "latest_telemetry", lambda host=None, scenario=None: telemetry("unavailable"))
    missing = client.get("/api/qos/decision").json()
    assert missing["status"] == "unavailable"
    assert missing["classification"] is None


def test_decision_api_reports_high_candidate_separately_from_low_selection(monkeypatch):
    from dashboard.backend import decision_loop as decision_module

    monkeypatch.setattr(decision_module, "monotonic", lambda: 1.0)
    loop = DecisionLoop()
    loop.evaluate(telemetry(), now_monotonic=0.0)
    high = telemetry()
    high.record.timestamp = "2026-10-20T10:00:01+00:00"
    high.record.throughput_mbps = 9.692
    high.record.utilization_percent = 96.92
    high.record.latency_ms = 376.341
    high.record.jitter_ms = 0.03
    high.record.packet_loss_percent = 0.0
    monkeypatch.setattr(api, "latest_telemetry", lambda host=None, scenario=None: high)
    monkeypatch.setattr(api, "decision_loop", loop)

    response = client.get("/api/qos/decision")

    assert response.status_code == 200
    body = response.json()
    assert body["classification"] == "HIGH"
    assert body["score"] == 4
    assert body["selected_profile"] == "LOW"
    assert body["candidate_profile"] == "HIGH"
    assert body["transition"] == "awaiting_hysteresis"
    assert body["hysteresis_samples_observed"] == 1
    assert body["hysteresis_samples_required"] == 2
    assert body["cooldown_remaining_seconds"] == 29.0
    assert body["applied"] is False


def test_qos_apply_requires_local_authorization_and_opt_in(monkeypatch):
    monkeypatch.setattr(api, "_require_local_mutation", lambda _request: None)
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry())
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.delenv(api.QOS_APPLICATION_ENV, raising=False)

    disabled = client.post(
        "/api/qos/apply",
        json={
            "profile": "LOW",
            "measurement_timestamp": "2026-10-20T10:00:00+00:00",
            "authorization": "APPLY_QOS_PROFILE",
        },
    )
    unauthorized = client.post(
        "/api/qos/apply",
        json={
            "profile": "LOW",
            "measurement_timestamp": "2026-10-20T10:00:00+00:00",
        },
    )
    assert disabled.status_code == 403
    assert unauthorized.status_code == 422


def test_qos_apply_rejects_nonlocal_browser_request_before_telemetry(monkeypatch):
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(
        api,
        "latest_telemetry",
        lambda: pytest.fail("non-local request must not reach telemetry or controller"),
    )
    response = client.post(
        "/api/qos/apply",
        json={
            "profile": "LOW",
            "measurement_timestamp": "2026-10-20T10:00:00+00:00",
            "authorization": "APPLY_QOS_PROFILE",
        },
    )
    assert response.status_code == 403


def test_qos_apply_revalidates_timestamp_profile_and_telemetry(monkeypatch):
    monkeypatch.setattr(api, "_require_local_mutation", lambda _request: None)
    monkeypatch.setattr(api.tc_controller, "LIVE_TC_OWNERSHIP_GUARANTEED", True)
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(api, "latest_telemetry", lambda host=None, scenario=None: telemetry("stale"))
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    body = {
        "profile": "LOW",
        "measurement_timestamp": "2026-10-20T10:00:00+00:00",
        "authorization": "APPLY_QOS_PROFILE",
    }

    stale = client.post("/api/qos/apply", json=body)
    invalid_profile = client.post("/api/qos/apply", json={**body, "profile": "CUSTOM"})
    invalid_interface = client.post("/api/qos/apply", json={**body, "interface": "eth0"})
    assert stale.status_code == 409
    assert invalid_profile.status_code == 422
    assert invalid_interface.status_code == 422

    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.setattr(api, "latest_telemetry", lambda host=None, scenario=None: telemetry("unavailable"))
    missing = client.post("/api/qos/apply", json=body)
    assert missing.status_code == 409


def test_qos_apply_calls_supervisor_and_requires_verified_readback(monkeypatch):
    from dashboard.backend import execution_manager

    monkeypatch.setattr(api, "_require_local_mutation", lambda _request: None)
    monkeypatch.setattr(api.tc_controller, "LIVE_TC_OWNERSHIP_GUARANTEED", True)
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry())
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    calls = []
    monkeypatch.setattr(
        execution_manager,
        "invoke_qos_profile",
        lambda profile: calls.append(profile) or {
            "status": "applied_verified",
            "profile": profile,
            "interface": "s1-eth5",
            "kernel_readback_verified": True,
        },
    )
    response = client.post(
        "/api/qos/apply",
        json={
            "profile": "LOW",
            "measurement_timestamp": "2026-10-20T10:00:00+00:00",
            "authorization": "APPLY_QOS_PROFILE",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied_verified"
    assert response.json()["verification"] == "kernel_readback_verified"
    assert calls == ["LOW"]


def test_qos_apply_reports_verification_failure_without_success_status(monkeypatch):
    from dashboard.backend import execution_manager

    monkeypatch.setattr(api, "_require_local_mutation", lambda _request: None)
    monkeypatch.setattr(api.tc_controller, "LIVE_TC_OWNERSHIP_GUARANTEED", True)
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry())
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.setattr(
        execution_manager,
        "invoke_qos_profile",
        lambda _profile: (_ for _ in ()).throw(
            execution_manager.QosVerificationFailed("readback mismatch")
        ),
    )
    response = client.post(
        "/api/qos/apply",
        json={
            "profile": "LOW",
            "measurement_timestamp": "2026-10-20T10:00:00+00:00",
            "authorization": "APPLY_QOS_PROFILE",
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"]["state"] == "verification_failed"


def test_qos_apply_reports_controller_failure_without_success_status(monkeypatch):
    from dashboard.backend import execution_manager

    monkeypatch.setattr(api, "_require_local_mutation", lambda _request: None)
    monkeypatch.setattr(api.tc_controller, "LIVE_TC_OWNERSHIP_GUARANTEED", True)
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry())
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.setattr(
        execution_manager,
        "invoke_qos_profile",
        lambda _profile: (_ for _ in ()).throw(
            execution_manager.ExecutionUnavailable("Controller rejected the existing tc state.")
        ),
    )
    response = client.post(
        "/api/qos/apply",
        json={
            "profile": "LOW",
            "measurement_timestamp": "2026-10-20T10:00:00+00:00",
            "authorization": "APPLY_QOS_PROFILE",
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["state"] == "failed"


def test_qos_status_reports_only_supervisor_readback(monkeypatch):
    from dashboard.backend import execution_manager

    monkeypatch.setattr(
        execution_manager,
        "inspect_qos_profile",
        lambda: {
            "status": "verified",
            "profile": "MEDIUM",
            "interface": "s1-eth5",
            "readback_at": "2026-10-25T00:00:00+00:00",
            "qdiscs": ["qdisc htb 5: root"],
            "classes": ["class htb 5:1 root rate 10Mbit ceil 10Mbit"],
            "filters": ["filter protocol ip pref 10 u32"],
        },
    )
    response = client.get("/api/qos/status")
    assert response.status_code == 200
    assert response.json()["applied_profile"] == "MEDIUM"
    assert response.json()["status"] == "verified"
    assert response.json()["readback_at"] == "2026-10-25T00:00:00+00:00"
    assert response.json()["qdiscs"] == ["qdisc htb 5: root"]
    assert response.json()["classes"] == ["class htb 5:1 root rate 10Mbit ceil 10Mbit"]
    assert response.json()["filters"] == ["filter protocol ip pref 10 u32"]


def test_qos_status_rejects_malformed_kernel_snapshot(monkeypatch):
    from dashboard.backend import execution_manager

    monkeypatch.setattr(
        execution_manager,
        "inspect_qos_profile",
        lambda: {
            "status": "verified",
            "profile": "LOW",
            "interface": "s1-eth5",
            "classes": "not-a-list",
        },
    )
    response = client.get("/api/qos/status")
    assert response.status_code == 502
