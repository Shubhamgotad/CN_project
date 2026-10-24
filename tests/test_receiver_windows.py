import pytest

from telemetry.receiver_windows import (
    RECEIVER_INTERVAL_SCHEMA_VERSION,
    load_run_manifest,
    synchronized_receiver_aggregate,
    validate_run_manifest,
)
from telemetry.validate_telemetry import _validate_record


def interval(flow, start, end, rate, **extra):
    return {
        "flow_id": flow,
        "measurement_direction": "receiver",
        "receiver_interval_schema_version": RECEIVER_INTERVAL_SCHEMA_VERSION,
        "boundary_source": "receiver_measurement",
        "clock_domain": "receiver_monotonic",
        "clock_id": "run-monotonic-1",
        "clock_unit": "seconds",
        "clock_semantics": "synthetic fixture clock semantics",
        "boundary_semantics": "[start,end); synthetic interval edges",
        "host_id": "server-host",
        "boot_id": "server-boot",
        "producer_name": "synthetic-fixture",
        "producer_version": "test-only",
        "producer_build_identity": "synthetic-build-identity",
        "producer_binary_sha256": "b" * 64,
        "producer_process_instance_id": "server-boot:123:456",
        "producer_pid": 123,
        "producer_process_start_ticks": "456",
        "absolute_start_seconds": start,
        "absolute_end_seconds": end,
        "start_boundary_uncertainty_seconds": 0,
        "end_boundary_uncertainty_seconds": 0,
        "boundary_uncertainty_evidence": {
            "method": "synthetic exact fixture",
            "evidence_path": "boundary.json",
            "sha256": "0" * 64,
        },
        "boundary_metadata_status": "schema_valid",
        "boundary_evidence_integrity_status": "not_checked",
        "producer_capability_validation_status": "not_independently_validated",
        "clock_validation_status": "not_independently_validated",
        "measurement_alignment_eligibility": "ineligible",
        "throughput_mbps": rate,
        **extra,
    }


def test_aggregate_uses_exact_shared_window_and_time_weighted_receiver_rates():
    rows = [
        interval("h1", 0, 2, 2), interval("h1", 2, 4, 4),
        interval("h2", 1, 3, 3), interval("h2", 3, 5, 5),
    ]
    result = synchronized_receiver_aggregate(rows, ("h1", "h2"))
    assert result["window_start_seconds"] == 1
    assert result["window_end_seconds"] == 4
    assert result["window_duration_seconds"] == 3
    assert result["per_flow_average_mbps"] == {"h1": 10 / 3, "h2": 11 / 3}
    assert result["aggregate_throughput_mbps"] == pytest.approx(7.0)
    assert result["status"] == "contract_valid"
    assert result["boundary_metadata_status"] == "schema_valid"
    assert result["boundary_evidence_integrity_status"] == "not_checked"
    assert result["producer_provenance_status"] == "not_independently_validated"
    assert result["measurement_alignment_eligibility"] == "ineligible"


def test_relative_offsets_or_observation_times_do_not_establish_alignment():
    rows = [
        interval("h1", 0, 1, 2, interval_start_seconds=0,
                 receiver_observation_monotonic_seconds=100.1),
        interval("h2", 0, 1, 3, interval_start_seconds=0,
                 receiver_observation_monotonic_seconds=100.2,
                 clock_id="different-run-clock"),
    ]
    with pytest.raises(ValueError, match="clock, host, and boot identities"):
        synchronized_receiver_aggregate(rows, ("h1", "h2"))


@pytest.mark.parametrize(
    "mutation",
    ["missing_boundaries", "callback_only", "relative_only"],
)
def test_unversioned_or_callback_timestamps_do_not_establish_receiver_boundaries(
    mutation,
):
    row = interval("h1", 0, 1, 2)
    for field in (
        "receiver_interval_schema_version",
        "boundary_source",
        "clock_domain",
        "host_id",
        "boot_id",
        "absolute_start_seconds",
        "absolute_end_seconds",
        "start_boundary_uncertainty_seconds",
        "end_boundary_uncertainty_seconds",
        "boundary_uncertainty_evidence",
    ):
        row.pop(field, None)
    if mutation == "callback_only":
        row["receiver_observation_monotonic_seconds"] = 100.0
        row["receiver_observation_role"] = "diagnostic_only"
    elif mutation == "relative_only":
        row["interval_start_seconds"] = 0
        row["interval_end_seconds"] = 1

    with pytest.raises(ValueError, match="schema version is missing or unsupported"):
        synchronized_receiver_aggregate([row], ("h1",))


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("start_boundary_uncertainty_seconds", -0.1, "must be nonnegative"),
        ("end_boundary_uncertainty_seconds", float("nan"), "must be finite"),
        ("boundary_source", "callback_observation", "must come from measurement"),
        ("clock_domain", "iperf_relative", "must be receiver_monotonic"),
        ("host_id", "different-host", "host, and boot identities"),
        ("boot_id", "different-boot", "host, and boot identities"),
    ],
)
def test_receiver_identity_and_uncertainty_contract_fails_closed(
    field, value, error
):
    rows = [interval("h1", 0, 1, 1), interval("h2", 0, 1, 1)]
    rows[1][field] = value
    with pytest.raises(ValueError, match=error):
        synchronized_receiver_aggregate(rows, ("h1", "h2"))


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("clock_id", "clock_id is required"),
        ("host_id", "host_id is required"),
        ("boot_id", "boot_id is required"),
    ],
)
def test_receiver_clock_identity_fields_are_required(field, error):
    row = interval("h1", 0, 1, 1)
    row.pop(field)

    with pytest.raises(ValueError, match=error):
        synchronized_receiver_aggregate([row], ("h1",))


@pytest.mark.parametrize(
    ("evidence", "error"),
    [
        (None, "uncertainty evidence is required"),
        (
            {"method": "test", "evidence_path": "boundary.json", "sha256": "z" * 64},
            "lowercase SHA-256",
        ),
        (
            {"method": "test", "evidence_path": "", "sha256": "0" * 64},
            "evidence_path is required",
        ),
    ],
)
def test_invalid_boundary_evidence_blocks_aligned_aggregation(evidence, error):
    row = interval("h1", 0, 1, 1)
    row["boundary_uncertainty_evidence"] = evidence

    with pytest.raises(ValueError, match=error):
        synchronized_receiver_aggregate([row], ("h1",))


def test_uncertain_edges_shrink_coverage_to_guaranteed_interval_interior():
    rows = [
        interval(
            "h1", 0, 2, 2,
            start_boundary_uncertainty_seconds=0.25,
            end_boundary_uncertainty_seconds=0.5,
        ),
        interval("h2", 0, 2, 3),
    ]

    result = synchronized_receiver_aggregate(rows, ("h1", "h2"))

    assert result["window_start_seconds"] == 0.25
    assert result["window_end_seconds"] == 1.5
    assert result["window_duration_seconds"] == 1.25
    assert result["aggregate_throughput_mbps"] == pytest.approx(5)


def test_missing_and_duplicate_flows_are_rejected():
    with pytest.raises(ValueError, match="missing receiver flows"):
        synchronized_receiver_aggregate([interval("h1", 0, 1, 1)], ("h1", "h2"))
    rows = [interval("h1", 0, 1, 1), interval("h1", 0, 1, 1)]
    with pytest.raises(ValueError, match="duplicate interval"):
        synchronized_receiver_aggregate(rows, ("h1",))


def test_incomplete_coverage_or_sender_side_data_is_rejected():
    gap = [
        interval("h1", 0, 1, 1), interval("h1", 2, 3, 1),
        interval("h2", 0, 3, 1),
    ]
    with pytest.raises(ValueError, match="gap in the shared receiver window"):
        synchronized_receiver_aggregate(gap, ("h1", "h2"))
    sender = [dict(interval("h1", 0, 1, 1), measurement_direction="sender")]
    with pytest.raises(ValueError, match="not receiver-side"):
        synchronized_receiver_aggregate(sender, ("h1",))


def test_uncertainty_induced_gap_and_unresolved_overlap_are_rejected():
    gap = [
        interval(
            "h1", 0, 1, 1,
            end_boundary_uncertainty_seconds=0.2,
        ),
        interval(
            "h1", 1, 2, 1,
            start_boundary_uncertainty_seconds=0.2,
        ),
        interval("h2", 0, 2, 1),
    ]
    with pytest.raises(ValueError, match="gap in the shared receiver window"):
        synchronized_receiver_aggregate(gap, ("h1", "h2"))

    overlap = [
        interval("h1", 0, 1.2, 1),
        interval("h1", 1, 2, 1),
        interval("h2", 0, 2, 1),
    ]
    with pytest.raises(ValueError, match="overlapping/duplicated intervals"):
        synchronized_receiver_aggregate(overlap, ("h1", "h2"))


def test_telemetry_schema_accepts_receiver_contract_and_marks_callback_diagnostic():
    record = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "experiment_id": "synthetic",
        "scenario": "test",
        "source": "h1",
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": 4,
        "throughput_mbps": 3,
        "latency_ms": 1,
        "jitter_ms": 0,
        "packet_loss_percent": 0,
        "lost_packets": 0,
        "total_packets": 1,
        "bandwidth_mbps": 10,
        "utilization_percent": 30,
        "active_flows": 1,
        "queue_size": None,
        "queue_drops": None,
        **interval("h1", 0, 1, 3),
        "receiver_observation_monotonic_seconds": 5.0,
        "receiver_observation_role": "diagnostic_only",
    }
    record.pop("flow_id")

    assert _validate_record(record) == []

    record["receiver_observation_role"] = "measurement_boundary"
    assert any(
        "must be marked diagnostic_only" in error
        for error in _validate_record(record)
    )


def test_manifest_rejects_missing_duplicate_and_malformed_flow_metadata():
    expected = {"h1": 5201, "h2": 5202}
    valid = {
        "duration_seconds": 5,
        "offered_rate_mbps": 4,
        "flows": [
            {"host": "h1", "port": 5201, "rate_mbps": 4},
            {"host": "h2", "port": 5202, "rate_mbps": 4},
        ],
    }
    validate_run_manifest(valid, expected)
    with pytest.raises(ValueError, match="missing flows"):
        validate_run_manifest({**valid, "flows": valid["flows"][:1]}, expected)
    with pytest.raises(ValueError, match="duplicate flow"):
        validate_run_manifest({**valid, "flows": valid["flows"] + valid["flows"][:1]}, expected)
    with pytest.raises(ValueError, match="offered_rate_mbps is malformed"):
        validate_run_manifest({**valid, "offered_rate_mbps": "4"}, expected)


@pytest.mark.parametrize(
    "duration",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "5"],
)
def test_manifest_rejects_invalid_durations(duration):
    valid = {
        "duration_seconds": 5,
        "offered_rate_mbps": 4,
        "flows": [
            {"host": "h1", "port": 5201, "rate_mbps": 4},
        ],
    }
    with pytest.raises(ValueError, match="duration_seconds is malformed"):
        validate_run_manifest({**valid, "duration_seconds": duration}, {"h1": 5201})


@pytest.mark.parametrize("duration", [0.001, 5, 12.5])
def test_manifest_accepts_finite_positive_durations(duration):
    valid = {
        "duration_seconds": duration,
        "offered_rate_mbps": 4,
        "flows": [
            {"host": "h1", "port": 5201, "rate_mbps": 4},
        ],
    }
    validate_run_manifest(valid, {"h1": 5201})


def test_manifest_loader_rejects_invalid_json_without_repairing_it(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text('{"flows":[]} trailing', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_run_manifest(path, {"h1": 5201})
    assert path.read_text(encoding="utf-8") == '{"flows":[]} trailing'
