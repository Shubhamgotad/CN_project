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
