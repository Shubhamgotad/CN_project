import pytest

from telemetry.receiver_windows import (
    load_run_manifest,
    synchronized_receiver_aggregate,
    validate_run_manifest,
)


def interval(flow, start, end, rate, **extra):
    return {
        "flow_id": flow,
        "measurement_direction": "receiver",
        "clock_id": "run-monotonic-1",
        "absolute_start_seconds": start,
        "absolute_end_seconds": end,
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


def test_relative_offsets_or_observation_times_do_not_establish_alignment():
    rows = [
        interval("h1", 0, 1, 2, interval_start_seconds=0,
                 receiver_observation_monotonic_seconds=100.1),
        interval("h2", 0, 1, 3, interval_start_seconds=0,
                 receiver_observation_monotonic_seconds=100.2,
                 clock_id="different-run-clock"),
    ]
    with pytest.raises(ValueError, match="one verified clock_id"):
        synchronized_receiver_aggregate(rows, ("h1", "h2"))


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
