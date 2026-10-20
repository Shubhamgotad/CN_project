from dashboard.backend.api import LatestTelemetryResponse, TelemetryRecordResponse
from dashboard.backend.decision_loop import DecisionLoop


def latest(*, status="available", utilization=30.0, latency=20.0, timestamp="2026-10-20T10:00:00+00:00", source="h1"):
    return LatestTelemetryResponse(
        status=status,
        source="configured_person1_jsonl" if status != "unavailable" else "none",
        measurement_age_seconds=2.0 if status == "available" else 400.0,
        message="measurement status",
        record=None if status == "unavailable" else TelemetryRecordResponse(
            timestamp=timestamp,
            source=source,
            scenario="normal",
            throughput_mbps=3.0,
            latency_ms=latency,
            jitter_ms=0.1,
            packet_loss_percent=0.0,
            queue_size=4,
            queue_drops=0,
            measurement_direction="receiver",
            interface="s1-eth5",
            bandwidth_mbps=10.0,
            utilization_percent=utilization,
            active_flows=1,
        ),
    )


def test_fresh_telemetry_classifies_and_returns_existing_profile_as_dry_run_only():
    result = DecisionLoop().evaluate(latest(), now_monotonic=100.0)

    assert result["status"] == "ready"
    assert result["classification"] == "LOW"
    assert result["selected_profile"] == "LOW"
    assert result["measurement"]["throughput_mbps"] == 3.0
    assert result["proposed_policy"]["service_allocations_mbps"] == {
        "gaming": 2.7, "video": 2.7, "web": 2.25, "bulk": 1.35
    }
    assert result["tc_commands"]
    assert result["dry_run"] is True and result["applied"] is False


def test_fresh_high_congestion_selects_existing_high_profile():
    result = DecisionLoop().evaluate(
        latest(utilization=90.0, latency=120.0), now_monotonic=100.0
    )

    assert result["classification"] == "HIGH"
    assert result["selected_profile"] == "HIGH"
    assert result["proposed_policy"]["service_allocations_mbps"] == {
        "gaming": 4.5, "video": 2.25, "web": 1.35, "bulk": 0.9
    }


def test_stale_and_unavailable_telemetry_produce_no_decision():
    loop = DecisionLoop()
    stale = loop.evaluate(latest(status="stale"), now_monotonic=100.0)
    missing = loop.evaluate(latest(status="unavailable"), now_monotonic=101.0)

    assert stale["status"] == "stale" and stale["classification"] is None
    assert stale["proposed_policy"] is None and stale["tc_commands"] == []
    assert missing["status"] == "unavailable" and missing["selected_profile"] is None


def test_missing_classifier_fields_fail_closed():
    telemetry = latest()
    telemetry.record.utilization_percent = None
    result = DecisionLoop().evaluate(telemetry, now_monotonic=10.0)
    assert result["status"] == "unavailable"
    assert result["proposed_policy"] is None


def test_malformed_or_unsupported_measurements_fail_closed():
    malformed = DecisionLoop().evaluate(latest(utilization=101.0), now_monotonic=10.0)
    unsupported = latest()
    unsupported.record.interface = "eth0"
    unsupported_result = DecisionLoop().evaluate(unsupported, now_monotonic=11.0)

    assert malformed["status"] == "unavailable"
    assert malformed["classification"] is None and malformed["tc_commands"] == []
    assert unsupported_result["status"] == "unavailable"
    assert unsupported_result["proposed_policy"] is None


def test_congestion_classification_hysteresis_and_cooldown_use_distinct_records():
    loop = DecisionLoop()
    first = loop.evaluate(latest(utilization=30.0), now_monotonic=0.0)
    assert first["selected_profile"] == "LOW"

    medium_1 = loop.evaluate(latest(utilization=70.0, latency=55.0, timestamp="2026-10-20T10:00:01+00:00"), now_monotonic=1.0)
    repeated = loop.evaluate(latest(utilization=70.0, latency=55.0, timestamp="2026-10-20T10:00:01+00:00"), now_monotonic=2.0)
    medium_2 = loop.evaluate(latest(utilization=70.0, latency=55.0, timestamp="2026-10-20T10:00:02+00:00"), now_monotonic=3.0)
    assert medium_1["classification"] == "MEDIUM"
    assert medium_1["selected_profile"] == "LOW"
    assert medium_1["candidate_profile"] == "MEDIUM"
    assert medium_1["hysteresis_samples_observed"] == 1
    assert medium_1["hysteresis_samples_required"] == 2
    assert medium_1["cooldown_remaining_seconds"] == 29.0
    assert medium_1["transition"] == "awaiting_hysteresis"
    assert repeated["hysteresis_samples_observed"] == 1
    assert repeated["candidate_profile"] == "MEDIUM"
    assert medium_2["transition"] == "cooldown"
    assert medium_2["selected_profile"] == "LOW"

    after_cooldown_same_sample = loop.evaluate(latest(utilization=70.0, latency=55.0, timestamp="2026-10-20T10:00:02+00:00"), now_monotonic=31.0)
    assert after_cooldown_same_sample["transition"] == "awaiting_fresh_sample"
    after_cooldown = loop.evaluate(latest(utilization=70.0, latency=55.0, timestamp="2026-10-20T10:00:03+00:00"), now_monotonic=32.0)
    assert after_cooldown["selected_profile"] == "MEDIUM"
    assert after_cooldown["transition"] == "profile_changed"
    assert after_cooldown["candidate_profile"] is None


def test_revisited_old_record_does_not_advance_hysteresis_or_mismatch_candidate():
    loop = DecisionLoop()
    loop.evaluate(latest(utilization=30.0), now_monotonic=0.0)
    old_high = latest(
        utilization=90.0,
        latency=120.0,
        timestamp="2026-10-20T10:00:01+00:00",
    )
    first_high = loop.evaluate(old_high, now_monotonic=1.0)
    medium = loop.evaluate(
        latest(utilization=70.0, latency=55.0, timestamp="2026-10-20T10:00:02+00:00"),
        now_monotonic=2.0,
    )
    revisited_high = loop.evaluate(old_high, now_monotonic=3.0)

    assert first_high["candidate_profile"] == "HIGH"
    assert medium["candidate_profile"] == "MEDIUM"
    assert revisited_high["classification"] == "HIGH"
    assert revisited_high["candidate_profile"] == "HIGH"
    assert revisited_high["hysteresis_samples_observed"] == 0
    assert revisited_high["transition"] == "awaiting_hysteresis"


def test_dry_run_decision_only_builds_commands_and_never_calls_apply(monkeypatch):
    import qos.tc_controller as controller

    def forbidden(*args, **kwargs):
        raise AssertionError("live apply must not be called")

    monkeypatch.setattr(controller, "apply_managed_htb_policy", forbidden)
    result = DecisionLoop().evaluate(latest(), now_monotonic=1.0)
    assert result["tc_commands"]
    assert result["applied"] is False
