from fastapi.testclient import TestClient

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


def test_decision_api_returns_real_input_and_non_applied_proposal(monkeypatch):
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry())
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.delenv(api.QOS_APPLICATION_ENV, raising=False)

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
    assert body["tc_commands"]
    assert body["dry_run"] is True and body["applied"] is False
    assert body["execution_state"] == "dry_run"
    assert body["application_enabled"] is False


def test_decision_api_rejects_stale_and_missing_telemetry(monkeypatch):
    monkeypatch.setattr(api, "decision_loop", DecisionLoop())
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry("stale"))
    stale = client.get("/api/qos/decision").json()
    assert stale["status"] == "stale"
    assert stale["proposed_policy"] is None and stale["tc_commands"] == []

    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry("unavailable"))
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
    monkeypatch.setattr(api, "latest_telemetry", lambda: high)
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
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry("stale"))
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
    monkeypatch.setattr(api, "latest_telemetry", lambda: telemetry("unavailable"))
    missing = client.post("/api/qos/apply", json=body)
    assert missing.status_code == 409


def test_qos_apply_calls_supervisor_and_requires_verified_readback(monkeypatch):
    from dashboard.backend import execution_manager

    monkeypatch.setattr(api, "_require_local_mutation", lambda _request: None)
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
        },
    )
    response = client.get("/api/qos/status")
    assert response.status_code == 200
    assert response.json()["applied_profile"] == "MEDIUM"
    assert response.json()["status"] == "verified"
