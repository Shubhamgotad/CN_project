import csv
import hashlib
import json
import math
from pathlib import Path

import pytest

import integration.evidence_analysis as evidence_analysis
from integration import safe_outputs
from integration.evidence_analysis import (
    EXPECTED_FLOWS,
    analyze_manifest,
    main,
    write_reports,
)


def receiver_row(flow, experiment_id="exp-1", *, start=100.0, end=120.0, **extra):
    return {
        "source": flow,
        "experiment_id": experiment_id,
        "requested_mbps": 4.0,
        "measurement_direction": "receiver",
        "throughput_measurement_source": "iperf3_server_json_stream",
        "throughput_mbps": 3.0,
        "latency_ms": 20.0,
        "jitter_ms": 0.5,
        "packet_loss_percent": 1.0,
        "lost_packets": 1,
        "total_packets": 100,
        "receiver_interval_schema_version": 1,
        "boundary_source": "receiver_measurement",
        "clock_domain": "receiver_monotonic",
        "clock_unit": "seconds",
        "clock_semantics": "synthetic fixture clock semantics",
        "boundary_semantics": "[start,end); synthetic interval edges",
        "absolute_start_seconds": start,
        "absolute_end_seconds": end,
        "clock_id": "clock-run-1",
        "host_id": "server-host",
        "boot_id": "server-boot",
        "producer_name": "synthetic-fixture",
        "producer_version": "test-only",
        "producer_build_identity": "synthetic-build-identity",
        "producer_binary_sha256": "b" * 64,
        "producer_process_instance_id": "server-boot:123:456",
        "producer_pid": 123,
        "producer_process_start_ticks": "456",
        "start_boundary_uncertainty_seconds": 0.0,
        "end_boundary_uncertainty_seconds": 0.0,
        "boundary_uncertainty_evidence": {
            "method": "synthetic exact boundary fixture",
            "evidence_path": "placeholder.json",
            "sha256": "0" * 64,
        },
        "boundary_metadata_status": "schema_valid",
        "boundary_evidence_integrity_status": "not_checked",
        "producer_capability_validation_status": "not_independently_validated",
        "clock_validation_status": "not_independently_validated",
        "measurement_alignment_eligibility": "ineligible",
        **extra,
    }


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_rows = []
    for index, row in enumerate(rows):
        normalized = dict(row)
        if normalized.get("receiver_interval_schema_version") == 1:
            evidence_path = path.parent / f"{path.stem}-boundary-{index}.json"
            evidence_document = {
                "schema_version": 1,
                "method": normalized["boundary_uncertainty_evidence"]["method"],
                "start_boundary_uncertainty_seconds": normalized[
                    "start_boundary_uncertainty_seconds"
                ],
                "end_boundary_uncertainty_seconds": normalized[
                    "end_boundary_uncertainty_seconds"
                ],
            }
            evidence_bytes = (
                json.dumps(evidence_document, sort_keys=True, allow_nan=False) + "\n"
            ).encode("utf-8")
            evidence_path.write_bytes(evidence_bytes)
            normalized["boundary_uncertainty_evidence"] = {
                "method": evidence_document["method"],
                "evidence_path": evidence_path.name,
                "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            }
        normalized_rows.append(normalized)
    path.write_text(
        "".join(json.dumps(row, allow_nan=True) + "\n" for row in normalized_rows),
        encoding="utf-8",
    )


def make_trial(root, *, trial_id="trial-1", condition="baseline", profile="BASELINE",
               repetition=1, experiment_id="exp-1", rows_by_flow=None,
               duration=20, window=None):
    rows_by_flow = rows_by_flow or {
        flow: [receiver_row(flow, experiment_id)]
        for flow in EXPECTED_FLOWS
    }
    flows = []
    for flow, port in EXPECTED_FLOWS.items():
        path = root / f"{trial_id}-{flow}.jsonl"
        if flow in rows_by_flow and rows_by_flow[flow] is not None:
            write_jsonl(path, rows_by_flow[flow])
        flows.append({
            "flow_id": flow,
            "port": port,
            "evidence_path": path.name,
        })
    return {
        "trial_id": trial_id,
        "condition": condition,
        "profile": profile,
        "repetition_id": repetition,
        "experiment_id": experiment_id,
        "duration_seconds": duration,
        "declared_window": window or {
            "clock_id": "clock-run-1",
            "start_seconds": 100.0,
            "end_seconds": 120.0,
        },
        "flows": flows,
    }


def write_manifest(root, trials, *, confirmation=None):
    path = root / "manifest.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "offered_rate_mbps": 4,
        "duration_confirmation": confirmation or {"confirmed": False},
        "trials": trials,
    }, indent=2), encoding="utf-8")
    return path


def report_dir_outside(evidence_dir):
    path = evidence_dir.parent / f"{evidence_dir.name}-reports"
    path.mkdir(exist_ok=True)
    return path


def test_valid_complete_trial_verifies_exact_window_and_summarizes_receiver(tmp_path):
    trial = make_trial(tmp_path)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))

    assert report["status"] == "complete"
    assert report["offline_only"] is True
    assert report["live_operations_supported"] is False
    analyzed = report["trials"][0]
    assert analyzed["status"] == "complete"
    assert analyzed["receiver_window_aggregate"]["status"] == "contract_valid"
    assert analyzed["receiver_alignment_status"] == "ineligible"
    assert analyzed["receiver_window_aggregate"]["alignment_mode"] == "receiver_only_contract"
    assert analyzed["receiver_window_aggregate"]["aggregate_throughput_mbps"] == 12
    assert analyzed["boundary_metadata_status"] == "schema_valid"
    assert analyzed["boundary_evidence_integrity_status"] == "verified"
    assert analyzed["producer_provenance_status"] == "not_independently_validated"
    assert analyzed["clock_mapping_status"] == "not_independently_validated"
    assert analyzed["measurement_alignment_eligibility"] == "ineligible"
    assert analyzed["sender_receiver_temporal_comparison"]["status"] == "unavailable"
    assert report["sender_receiver_temporal_comparison"]["status"] == "unavailable"
    assert analyzed["unverified_arithmetic_throughput_summary"]["status"] == "not_emitted"
    assert analyzed["flows"]["h1"]["receiver"]["metrics"]["latency_ms"] == 20
    assert report["comparisons_by_repetition"][0]["repetition_count"] == 1


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing_boundaries", "absolute_start_seconds must be finite"),
        ("callback_only", "absolute_start_seconds must be finite"),
        ("relative_only", "absolute_start_seconds must be finite"),
        ("wrong_boundary_source", "must come from measurement instrumentation"),
        ("invalid_uncertainty", "must be nonnegative"),
        ("host_mismatch", "clock, host, and boot identities"),
    ],
)
def test_receiver_only_alignment_requires_versioned_boundary_evidence(
    tmp_path, mutation, expected_error
):
    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    row = rows["h1"][0]
    if mutation == "missing_boundaries":
        row.pop("absolute_start_seconds")
    elif mutation == "callback_only":
        row.pop("absolute_start_seconds")
        row.pop("absolute_end_seconds")
        row["receiver_observation_monotonic_seconds"] = 120.0
        row["receiver_observation_role"] = "diagnostic_only"
    elif mutation == "relative_only":
        row.pop("absolute_start_seconds")
        row.pop("absolute_end_seconds")
        row["interval_start_seconds"] = 0.0
        row["interval_end_seconds"] = 20.0
    elif mutation == "wrong_boundary_source":
        row["boundary_source"] = "callback_observation"
    elif mutation == "invalid_uncertainty":
        row["start_boundary_uncertainty_seconds"] = -0.1
    else:
        rows["h2"][0]["host_id"] = "other-host"
    trial = make_trial(tmp_path, rows_by_flow=rows)

    result = analyze_manifest(write_manifest(tmp_path, [trial]))["trials"][0]

    assert result["status"] == "failed"
    assert result["receiver_alignment_status"] == "unavailable"
    assert any(expected_error in error for error in result["errors"])
    assert result["sender_receiver_temporal_comparison"]["status"] == "unavailable"


def test_boundary_uncertainty_supporting_file_must_match_hash_and_values(tmp_path):
    trial = make_trial(tmp_path)
    evidence_path = next(tmp_path.glob("*-boundary-0.json"))
    evidence_path.write_text(
        json.dumps({
            "schema_version": 1,
            "method": "synthetic exact boundary fixture",
            "start_boundary_uncertainty_seconds": 0.5,
            "end_boundary_uncertainty_seconds": 0.0,
        }),
        encoding="utf-8",
    )

    result = analyze_manifest(write_manifest(tmp_path, [trial]))["trials"][0]

    assert result["status"] == "failed"
    assert any(
        "boundary uncertainty evidence SHA-256 does not match" in error
        for error in result["errors"]
    )


@pytest.mark.parametrize("failure", ["fake_digest", "missing_artifact"])
def test_boundary_artifact_integrity_failure_blocks_contract_aggregate(
    tmp_path, failure
):
    trial = make_trial(tmp_path)
    flow_path = tmp_path / trial["flows"][0]["evidence_path"]
    row = json.loads(flow_path.read_text(encoding="utf-8").splitlines()[0])
    if failure == "fake_digest":
        row["boundary_uncertainty_evidence"]["sha256"] = "0" * 64
    else:
        row["boundary_uncertainty_evidence"]["evidence_path"] = "missing-boundary.json"
    flow_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = analyze_manifest(write_manifest(tmp_path, [trial]))["trials"][0]

    assert result["receiver_window_aggregate"]["status"] == "unavailable"
    assert result["boundary_metadata_status"] == "schema_valid"
    assert result["boundary_evidence_integrity_status"] == "not_verified"
    assert result["measurement_alignment_eligibility"] == "ineligible"
    expected = (
        "SHA-256 does not match"
        if failure == "fake_digest"
        else "cannot be read"
    )
    assert any(expected in error for error in result["errors"])


def test_missing_manifest_and_missing_evidence_are_actionable(tmp_path):
    absent = analyze_manifest(tmp_path / "absent.json")
    assert absent["status"] == "invalid_manifest"
    assert "manifest file is missing" in absent["manifest_errors"][0]

    trial = make_trial(tmp_path)
    missing_path = tmp_path / trial["flows"][0]["evidence_path"]
    missing_path.unlink()
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert report["trials"][0]["status"] == "failed"
    assert any("evidence file is missing" in error for error in report["trials"][0]["errors"])


def test_malformed_jsonl_row_is_reported_instead_of_silently_ignored(tmp_path):
    trial = make_trial(tmp_path)
    path = tmp_path / trial["flows"][0]["evidence_path"]
    path.write_text(
        json.dumps(receiver_row("h1")) + "\nnot-json\n",
        encoding="utf-8",
    )
    report = analyze_manifest(write_manifest(tmp_path, [trial]))

    assert report["trials"][0]["status"] == "failed"
    assert any("line 2: malformed JSON" in error for error in report["trials"][0]["errors"])
    assert report["trials"][0]["flows"]["h1"]["receiver"]["sample_count"] == 1


def test_duplicate_trial_ids_and_duplicate_evidence_references_fail_trials(tmp_path):
    first = make_trial(tmp_path, trial_id="duplicate", repetition=1)
    second = make_trial(tmp_path, trial_id="duplicate", repetition=2)
    second["flows"][0]["evidence_path"] = first["flows"][0]["evidence_path"]
    report = analyze_manifest(write_manifest(tmp_path, [first, second]))

    assert report["status"] == "incomplete"
    assert all(trial["status"] == "failed" for trial in report["trials"])
    assert all(any("duplicate trial_id" in error for error in trial["errors"]) for trial in report["trials"])
    assert all(any("duplicate evidence reference" in error for error in trial["errors"]) for trial in report["trials"])


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("throughput_mbps", "fast", "finite number"),
        ("throughput_mbps", -1, "cannot be negative"),
        ("jitter_ms", float("nan"), "finite number"),
        ("latency_ms", float("inf"), "finite number"),
        ("packet_loss_percent", 101, "cannot exceed 100"),
    ],
)
def test_invalid_metrics_fail_trial_without_silent_drop(tmp_path, key, value, expected):
    rows = {
        flow: [receiver_row(flow)]
        for flow in EXPECTED_FLOWS
    }
    rows["h1"][0][key] = value
    trial = make_trial(tmp_path, rows_by_flow=rows)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))

    result = report["trials"][0]
    assert result["status"] == "failed"
    assert any(expected in message for message in result["errors"])


def test_missing_required_flow_and_mismatched_experiment_id_fail(tmp_path):
    trial = make_trial(tmp_path)
    trial["flows"] = trial["flows"][:-1]
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert report["trials"][0]["status"] == "failed"
    assert any("required flows are missing" in error for error in report["trials"][0]["errors"])
    assert report["trials"][0]["flows"]["h4"]["status"] == "unavailable"

    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    rows["h2"] = [receiver_row("h2", "other-experiment")]
    trial = make_trial(tmp_path, trial_id="mismatch", rows_by_flow=rows)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert any("does not match trial experiment_id" in error for error in report["trials"][0]["errors"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("condition", [], "condition must be baseline or qos"),
        ("condition", {}, "condition must be baseline or qos"),
        ("condition", None, "condition must be baseline or qos"),
        ("trial_status", [], "trial_status must be completed or failed"),
        ("trial_status", {}, "trial_status must be completed or failed"),
        ("trial_status", None, "trial_status must be completed or failed"),
    ],
)
def test_unhashable_and_unexpected_trial_fields_are_reported(tmp_path, field, value, message):
    trial = make_trial(tmp_path)
    trial[field] = value
    report = analyze_manifest(write_manifest(tmp_path, [trial]))

    assert report["trials"][0]["status"] == "failed"
    assert any(message in error for error in report["trials"][0]["errors"])


@pytest.mark.parametrize("flow_id", [[], {}, None])
def test_unhashable_and_unexpected_flow_ids_are_reported(tmp_path, flow_id):
    trial = make_trial(tmp_path)
    trial["flows"][0]["flow_id"] = flow_id
    report = analyze_manifest(write_manifest(tmp_path, [trial]))

    assert report["trials"][0]["status"] == "failed"
    assert any("unexpected flow identity" in error for error in report["trials"][0]["errors"])
    assert any("required flows are missing" in error for error in report["trials"][0]["errors"])


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("clock", "clock_id"),
        ("overlap", "overlapping/duplicated intervals"),
        ("gap", "gap in the shared receiver window"),
        ("window", "exact declared trial window"),
    ],
)
def test_unverified_shared_window_is_never_claimed(tmp_path, mutation, reason):
    rows = {
        flow: [receiver_row(flow)]
        for flow in EXPECTED_FLOWS
    }
    if mutation == "clock":
        rows["h2"][0]["clock_id"] = "different-clock"
    elif mutation == "overlap":
        rows["h1"].append(receiver_row("h1", start=119, end=121))
    elif mutation == "gap":
        rows["h1"] = [
            receiver_row("h1", start=100, end=110),
            receiver_row("h1", start=111, end=120),
        ]
    else:
        rows["h1"][0]["absolute_end_seconds"] = 119.5
    trial = make_trial(tmp_path, trial_id=f"trial-{mutation}", rows_by_flow=rows)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    result = report["trials"][0]

    assert result["status"] == "failed"
    assert any("receiver intervals cannot be aligned" in error
               for error in result["errors"])
    assert result["receiver_window_aggregate"]["status"] == "unavailable"
    assert reason in result["receiver_window_aggregate"]["reason"]
    arithmetic = result["unverified_arithmetic_throughput_summary"]
    assert arithmetic["status"] == "unverified"
    assert "unverified" in arithmetic["label"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("reversed", "absolute interval end must be after start"),
        ("declared_start", "exact declared trial window"),
        (
            "missing_declared_window",
            "declared_window with clock_id/start_seconds/end_seconds is required",
        ),
    ],
)
def test_reversed_or_misaligned_declared_window_is_unavailable_via_analyzer(
    tmp_path, mutation, reason
):
    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    window = None
    if mutation == "reversed":
        rows["h1"][0]["absolute_end_seconds"] = 99.0
    elif mutation == "declared_start":
        window = {
            "clock_id": "clock-run-1",
            "start_seconds": 99.0,
            "end_seconds": 120.0,
        }
    trial = make_trial(
        tmp_path,
        trial_id=f"trial-{mutation}",
        rows_by_flow=rows,
        window=window,
    )
    if mutation == "missing_declared_window":
        trial.pop("declared_window")
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    result = report["trials"][0]

    assert result["status"] == "failed"
    assert result["receiver_window_aggregate"]["status"] == "unavailable"
    assert any("receiver intervals cannot be aligned" in error
               for error in result["errors"])
    if mutation == "reversed":
        assert any(reason in error for error in result["errors"])
    else:
        assert reason in result["receiver_window_aggregate"]["reason"]


def test_missing_per_flow_requested_rate_fails_trial(tmp_path):
    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    rows["h2"][0].pop("requested_mbps")
    trial = make_trial(tmp_path, rows_by_flow=rows)

    result = analyze_manifest(write_manifest(tmp_path, [trial]))["trials"][0]

    assert result["status"] == "failed"
    assert any(
        "requested_mbps is required" in error
        for error in result["errors"]
    )


@pytest.mark.parametrize("requested_mbps", [3.9, 4.1])
def test_per_flow_requested_rate_must_match_comparison_contract(
    tmp_path, requested_mbps
):
    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    rows["h3"][0]["requested_mbps"] = requested_mbps
    trial = make_trial(tmp_path, rows_by_flow=rows)

    result = analyze_manifest(write_manifest(tmp_path, [trial]))["trials"][0]

    assert result["status"] == "failed"
    assert any(
        "requested_mbps must match the comparison-plan value 4" in error
        for error in result["errors"]
    )


@pytest.mark.parametrize("requested_mbps", [True, -1, float("inf"), "4"])
def test_per_flow_requested_rate_must_be_finite_nonnegative_number(
    tmp_path, requested_mbps
):
    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    rows["h1"][0]["requested_mbps"] = requested_mbps
    trial = make_trial(tmp_path, rows_by_flow=rows)

    result = analyze_manifest(write_manifest(tmp_path, [trial]))["trials"][0]

    assert result["status"] == "failed"
    assert any("requested_mbps" in error for error in result["errors"])


@pytest.mark.parametrize("missing_value", ["absent", None])
def test_missing_required_receiver_throughput_fails_trial(tmp_path, missing_value):
    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    if missing_value == "absent":
        rows["h4"][0].pop("throughput_mbps")
    else:
        rows["h4"][0]["throughput_mbps"] = None
    trial = make_trial(tmp_path, rows_by_flow=rows)

    result = analyze_manifest(write_manifest(tmp_path, [trial]))["trials"][0]

    assert result["status"] == "failed"
    assert result["receiver_window_aggregate"]["status"] == "unavailable"
    assert result["flows"]["h4"]["receiver"]["metrics"]["throughput_mbps"] is None
    assert any("receiver throughput_mbps is required" in error for error in result["errors"])


@pytest.mark.parametrize(
    ("duration", "json_literal"),
    [
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
        (True, "true"),
        ("5", '"5"'),
        (None, "null"),
        (0, "0"),
        (-1, "-1"),
    ],
)
def test_invalid_duration_is_reported_safe_and_cannot_confirm_live_use(
    tmp_path, duration, json_literal
):
    trial = make_trial(tmp_path, duration=duration)
    confirmation = {
        "confirmed": True,
        "operator": "operator-1",
        "note": "Reviewed",
        "confirmed_duration_seconds": 20,
    }
    manifest = write_manifest(tmp_path, [trial], confirmation=confirmation)
    if json_literal == "Infinity":
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(
            '"duration_seconds": Infinity', '"duration_seconds": 1e999'
        ), encoding="utf-8")
    elif json_literal == "-Infinity":
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(
            '"duration_seconds": -Infinity', '"duration_seconds": -1e999'
        ), encoding="utf-8")
    report = analyze_manifest(manifest)
    result = report["trials"][0]
    assert result["status"] == "failed"
    assert any("duration_seconds must be a positive finite number" in error for error in result["errors"])
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or (
        isinstance(duration, float) and not math.isfinite(duration)
    ):
        assert result["duration_seconds"] is None
    else:
        assert result["duration_seconds"] == duration
    assert report["duration_confirmation"]["status"] == "pending_operator_confirmation"
    assert report["duration_confirmation"]["live_use_blocked"] is True
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize(
    "reference",
    [
        lambda root: str(root / "trial-1-h1.jsonl"),
        lambda root: "..\\outside.jsonl",
    ],
    ids=["absolute", "parent-traversal"],
)
def test_evidence_paths_must_remain_relative_to_manifest(tmp_path, monkeypatch, reference):
    trial = make_trial(tmp_path)
    escaped = reference(tmp_path)
    trial["flows"][0]["evidence_path"] = escaped
    escaped_resolved = Path(escaped).resolve() if Path(escaped).is_absolute() else (
        tmp_path / escaped
    ).resolve()
    read_jsonl = evidence_analysis._read_jsonl

    def forbidden_read(*args, **kwargs):
        if Path(args[0]).resolve() == escaped_resolved:
            raise AssertionError("escaped evidence path must not be opened")
        return read_jsonl(*args, **kwargs)

    monkeypatch.setattr(evidence_analysis, "_read_jsonl", forbidden_read)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert report["trials"][0]["status"] == "failed"
    assert any(
        "evidence_path must be relative" in error
        or "evidence_path resolves outside" in error
        for error in report["trials"][0]["errors"]
    )


def test_symlink_evidence_path_cannot_escape_manifest_directory(tmp_path, monkeypatch):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    write_jsonl(outside, [receiver_row("h1")])
    link = tmp_path / "escaped.jsonl"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    trial = make_trial(tmp_path)
    trial["flows"][0]["evidence_path"] = link.name
    read_jsonl = evidence_analysis._read_jsonl

    def forbidden_read(*args, **kwargs):
        if Path(args[0]).resolve() == outside.resolve():
            raise AssertionError("symlink-escaped evidence must not be opened")
        return read_jsonl(*args, **kwargs)

    monkeypatch.setattr(evidence_analysis, "_read_jsonl", forbidden_read)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert report["trials"][0]["status"] == "failed"
    assert any(
        "evidence_path resolves outside the manifest directory" in error
        for error in report["trials"][0]["errors"]
    )


def test_nonstandard_manifest_constant_returns_serializable_invalid_report(tmp_path):
    trial = make_trial(tmp_path, duration=float("nan"))
    manifest = write_manifest(
        tmp_path,
        [trial],
        confirmation={
            "confirmed": True,
            "operator": "operator-1",
            "note": "Reviewed",
            "confirmed_duration_seconds": 20,
        },
    )
    report = analyze_manifest(manifest)
    assert report["status"] == "incomplete"
    assert report["trials"][0]["duration_seconds"] is None
    assert any(
        "duration_seconds must be a positive finite number" in error
        for error in report["trials"][0]["errors"]
    )
    assert report["duration_confirmation"]["live_use_blocked"] is True
    json.dumps(report, allow_nan=False)


def test_relative_offsets_do_not_qualify_as_absolute_shared_window(tmp_path):
    rows = {
        flow: [{
            key: value
            for key, value in receiver_row(flow).items()
            if key not in {"absolute_start_seconds", "absolute_end_seconds"}
        } | {
            "interval_start_seconds": 0,
            "interval_end_seconds": 5,
        }]
        for flow in EXPECTED_FLOWS
    }
    trial = make_trial(tmp_path, rows_by_flow=rows)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    aggregate = report["trials"][0]["receiver_window_aggregate"]
    assert aggregate["status"] == "unavailable"
    assert "absolute_start_seconds must be finite" in aggregate["reason"]
    assert report["trials"][0]["status"] == "failed"
    assert any(
        "receiver intervals cannot be aligned" in error
        and "absolute_start_seconds must be finite" in error
        for error in report["trials"][0]["errors"]
    )


def test_sender_metrics_are_never_substituted_for_receiver(tmp_path):
    rows = {}
    for flow in EXPECTED_FLOWS:
        rows[flow] = [{
            "source": flow,
            "experiment_id": "exp-1",
            "measurement_direction": "sender",
            "sender_throughput_mbps": 7.5,
            "throughput_mbps": 7.5,
        }]
    trial = make_trial(tmp_path, rows_by_flow=rows)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    flow = report["trials"][0]["flows"]["h1"]

    assert flow["receiver"]["status"] == "unavailable"
    assert flow["receiver"]["metrics"]["throughput_mbps"] is None
    assert flow["sender"]["metrics"]["throughput_mbps"] == 7.5
    assert report["trials"][0]["receiver_window_aggregate"]["status"] == "unavailable"
    assert report["trials"][0]["unverified_arithmetic_throughput_summary"]["throughput_mbps"] is None
    assert report["trials"][0]["status"] == "failed"


def test_sender_tag_conflicting_with_receiver_direction_is_rejected(tmp_path):
    rows = {
        flow: [receiver_row(flow)]
        for flow in EXPECTED_FLOWS
    }
    rows["h1"][0]["throughput_measurement_source"] = "iperf3_client_json_stream"
    trial = make_trial(tmp_path, rows_by_flow=rows)
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert report["trials"][0]["status"] == "failed"
    assert any("conflicts with sender-side" in error for error in report["trials"][0]["errors"])


def test_failed_trial_remains_in_report_with_reason(tmp_path):
    trial = make_trial(tmp_path)
    trial["trial_status"] = "failed"
    trial["failure_reason"] = "listener startup failed"
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert report["trials"][0]["status"] == "failed"
    assert report["trials"][0]["failure_reason"] == "listener startup failed"
    assert "trial was reported failed: listener startup failed" in report["trials"][0]["errors"]


def test_repeated_conditions_remain_distinct_by_repetition_id(tmp_path):
    trials = [
        make_trial(tmp_path, trial_id="low-r1", condition="qos", profile="LOW", repetition="r1"),
        make_trial(tmp_path, trial_id="low-r2", condition="qos", profile="LOW", repetition="r2",
                   experiment_id="exp-2"),
    ]
    # Give the second trial its own files and consistent IDs.
    for flow in EXPECTED_FLOWS:
        write_jsonl(
            tmp_path / f"low-r2-{flow}.jsonl",
            [receiver_row(flow, "exp-2")],
        )
    report = analyze_manifest(write_manifest(tmp_path, trials))
    group = next(
        item for item in report["comparisons_by_repetition"]
        if item["condition"] == "qos" and item["profile"] == "LOW"
    )
    assert group["repetition_count"] == 2
    assert [item["repetition_id"] for item in group["trials"]] == ["r1", "r2"]
    assert len({item["trial_id"] for item in group["trials"]}) == 2


def test_report_outputs_json_and_csv_and_blocks_unconfirmed_duration(tmp_path):
    trial = make_trial(tmp_path)
    manifest = write_manifest(tmp_path, [trial])
    report = analyze_manifest(manifest)
    assert report["duration_confirmation"]["status"] == "pending_operator_confirmation"
    assert report["duration_confirmation"]["live_use_blocked"] is True
    assert report["duration_confirmation"]["matrix_duration_seconds"] == 20
    assert report["duration_confirmation"]["other_documented_scenario_duration_seconds"] == 20

    output_dir = report_dir_outside(tmp_path)
    json_path = output_dir / "analysis.json"
    csv_path = output_dir / "analysis.csv"
    write_reports(report, json_path, csv_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "complete"
    with csv_path.open(encoding="utf-8", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    assert len(csv_rows) == 4
    assert csv_rows[0]["receiver_window_contract_status"] == "contract_valid"


def test_duration_confirmation_requires_explicit_matching_operator_value(tmp_path):
    trial = make_trial(tmp_path, duration=20)
    manifest = write_manifest(
        tmp_path,
        [trial],
        confirmation={
            "confirmed": True,
            "operator": "operator-1",
            "note": "Confirmed after review",
            "confirmed_duration_seconds": 19,
        },
    )
    report = analyze_manifest(manifest)
    assert report["duration_confirmation"]["status"] == "pending_operator_confirmation"
    assert report["duration_confirmation"]["live_use_blocked"] is True

    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["duration_confirmation"]["confirmed_duration_seconds"] = 20
    manifest.write_text(json.dumps(data), encoding="utf-8")
    report = analyze_manifest(manifest)
    assert report["duration_confirmation"]["status"] == "confirmed_by_operator"
    assert report["duration_confirmation"]["live_use_blocked"] is False
    assert report["live_operations_supported"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 99),
        ("offered_rate_mbps", 5),
    ],
)
def test_manifest_level_validation_errors_block_duration_confirmation(
    tmp_path, field, value
):
    trial = make_trial(tmp_path)
    manifest = write_manifest(
        tmp_path,
        [trial],
        confirmation={
            "confirmed": True,
            "operator": "operator-1",
            "note": "Confirmed after review",
            "confirmed_duration_seconds": 20,
        },
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data[field] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")

    report = analyze_manifest(manifest)

    assert report["status"] == "invalid_manifest"
    assert report["duration_confirmation"]["status"] == "pending_operator_confirmation"
    assert report["duration_confirmation"]["live_use_blocked"] is True


def test_invalid_trial_structure_blocks_duration_confirmation(tmp_path):
    trial = make_trial(tmp_path, profile="UNKNOWN")
    manifest = write_manifest(
        tmp_path,
        [trial],
        confirmation={
            "confirmed": True,
            "operator": "operator-1",
            "note": "Confirmed after review",
            "confirmed_duration_seconds": 20,
        },
    )

    report = analyze_manifest(manifest)

    assert report["status"] == "incomplete"
    assert report["duration_confirmation"]["status"] == "pending_operator_confirmation"
    assert report["duration_confirmation"]["live_use_blocked"] is True


def test_manifest_rejects_invalid_condition_profile_and_duplicate_output_path(tmp_path):
    trial = make_trial(tmp_path, condition="qos", profile="UNKNOWN")
    report = analyze_manifest(write_manifest(tmp_path, [trial]))
    assert report["trials"][0]["status"] == "failed"
    assert any("profile must be LOW" in error for error in report["trials"][0]["errors"])

    output_dir = report_dir_outside(tmp_path)
    with pytest.raises(ValueError, match="must be different"):
        write_reports(report, output_dir / "same.json", output_dir / "same.json")


@pytest.mark.parametrize("inside_output", ["json", "csv", "root"])
def test_report_outputs_must_be_outside_evidence_root_without_modifying_evidence(
    tmp_path, inside_output
):
    trial = make_trial(tmp_path)
    manifest = write_manifest(tmp_path, [trial])
    report = analyze_manifest(manifest)
    evidence_path = tmp_path / trial["flows"][0]["evidence_path"]
    original_manifest = manifest.read_bytes()
    original_evidence = evidence_path.read_bytes()
    outside_dir = report_dir_outside(tmp_path)
    json_path = outside_dir / "analysis.json"
    csv_path = outside_dir / "analysis.csv"
    if inside_output == "json":
        json_path = tmp_path / "analysis.json"
    elif inside_output == "csv":
        csv_path = tmp_path / "analysis.csv"
    else:
        json_path = tmp_path

    with pytest.raises(ValueError, match="outside protected"):
        write_reports(report, json_path, csv_path)

    assert not (tmp_path / "analysis.json").exists()
    assert not (tmp_path / "analysis.csv").exists()
    assert not (outside_dir / "analysis.json").exists()
    assert not (outside_dir / "analysis.csv").exists()
    assert manifest.read_bytes() == original_manifest
    assert evidence_path.read_bytes() == original_evidence


def test_report_output_symlink_resolving_inside_evidence_is_rejected(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    trial = make_trial(evidence_dir)
    manifest = write_manifest(evidence_dir, [trial])
    report = analyze_manifest(manifest)
    evidence_path = evidence_dir / trial["flows"][0]["evidence_path"]
    original_manifest = manifest.read_bytes()
    original_evidence = evidence_path.read_bytes()
    output_alias = tmp_path / "evidence-alias"
    try:
        output_alias.symlink_to(evidence_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    output_dir = report_dir_outside(evidence_dir)
    json_path = output_alias / evidence_path.name
    csv_path = output_dir / "analysis.csv"

    with pytest.raises(ValueError, match="must not contain symlinks"):
        write_reports(report, json_path, csv_path)

    assert manifest.read_bytes() == original_manifest
    assert evidence_path.read_bytes() == original_evidence
    assert not csv_path.exists()


def test_report_output_symlink_resolving_outside_evidence_is_rejected(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    trial = make_trial(evidence_dir)
    manifest = write_manifest(evidence_dir, [trial])
    report = analyze_manifest(manifest)
    original_manifest = manifest.read_bytes()
    outside_dir = report_dir_outside(evidence_dir)
    output_alias = evidence_dir / "external-reports"
    try:
        output_alias.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    json_path = output_alias / "analysis.json"
    csv_path = output_alias / "analysis.csv"
    with pytest.raises(ValueError, match="must not contain symlinks"):
        write_reports(report, json_path, csv_path)

    assert not (outside_dir / "analysis.json").exists()
    assert not (outside_dir / "analysis.csv").exists()
    assert manifest.read_bytes() == original_manifest


@pytest.mark.parametrize("existing_output", ["json", "csv"])
def test_report_outputs_refuse_existing_files_without_partial_writes(
    tmp_path, existing_output
):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    trial = make_trial(evidence_dir)
    report = analyze_manifest(write_manifest(evidence_dir, [trial]))
    output_dir = report_dir_outside(evidence_dir)
    json_path = output_dir / "analysis.json"
    csv_path = output_dir / "analysis.csv"
    existing_path = json_path if existing_output == "json" else csv_path
    existing_path.write_text("preserve this output", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_reports(report, json_path, csv_path)

    assert existing_path.read_text(encoding="utf-8") == "preserve this output"
    other_path = csv_path if existing_output == "json" else json_path
    assert not other_path.exists()


def test_report_outputs_reject_project_paths_and_traversal(tmp_path, monkeypatch):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    report = analyze_manifest(write_manifest(evidence_dir, [make_trial(evidence_dir)]))
    project_root = tmp_path / "project"
    protected = project_root / "telemetry" / "datasets"
    protected.mkdir(parents=True)
    monkeypatch.setattr(safe_outputs, "PROJECT_ROOT", project_root)
    outside_dir = tmp_path / "reports"
    outside_dir.mkdir()

    unsafe_paths = (
        (protected / "analysis.json", outside_dir / "analysis.csv"),
        (
            outside_dir / ".." / "project" / "telemetry" / "datasets" / "analysis.json",
            outside_dir / "analysis.csv",
        ),
    )
    for json_path, csv_path in unsafe_paths:
        with pytest.raises(ValueError, match="outside protected"):
            write_reports(report, json_path, csv_path)
    assert not list(protected.iterdir())
    assert not list(outside_dir.iterdir())


def test_report_writer_does_not_overwrite_concurrently_created_output(
    tmp_path, monkeypatch
):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    report = analyze_manifest(write_manifest(evidence_dir, [make_trial(evidence_dir)]))
    output_dir = report_dir_outside(evidence_dir)
    json_path = output_dir / "analysis.json"
    csv_path = output_dir / "analysis.csv"
    original_open = safe_outputs.open_exclusive_text

    def concurrent_create(path, *, newline=None):
        if Path(path) == json_path:
            json_path.write_text("created concurrently", encoding="utf-8")
        return original_open(path, newline=newline)

    monkeypatch.setattr(evidence_analysis, "open_exclusive_text", concurrent_create)
    with pytest.raises(FileExistsError):
        write_reports(report, json_path, csv_path)
    assert json_path.read_text(encoding="utf-8") == "created concurrently"
    assert not csv_path.exists()


def test_oversized_interval_integer_fails_analysis_and_cli_without_exception(
    tmp_path
):
    rows = {flow: [receiver_row(flow)] for flow in EXPECTED_FLOWS}
    rows["h1"][0]["absolute_start_seconds"] = 10**400
    trial = make_trial(tmp_path, trial_id="oversized-interval", rows_by_flow=rows)
    manifest = write_manifest(tmp_path, [trial])

    report = analyze_manifest(manifest)
    result = report["trials"][0]
    assert result["status"] == "failed"
    assert any("absolute_start_seconds must be a finite number" in error
               for error in result["errors"])
    assert result["receiver_window_aggregate"]["status"] == "unavailable"

    output_dir = report_dir_outside(tmp_path)
    json_path = output_dir / "analysis.json"
    csv_path = output_dir / "analysis.csv"
    assert main([
        "--manifest", str(manifest),
        "--json-output", str(json_path),
        "--csv-output", str(csv_path),
    ]) == 1
    cli_report = json.loads(json_path.read_text(encoding="utf-8"))
    cli_trial = cli_report["trials"][0]
    assert cli_trial["status"] == "failed"
    assert cli_trial["receiver_window_aggregate"]["status"] == "unavailable"


def test_cli_remains_offline_and_writes_reports_for_failed_trial(tmp_path, monkeypatch):
    trial = make_trial(tmp_path)
    trial["flows"][0]["evidence_path"] = "missing.jsonl"
    manifest = write_manifest(tmp_path, [trial])

    def forbidden(*args, **kwargs):
        raise AssertionError("offline analyzer cannot execute external operations")

    import subprocess

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    output_dir = report_dir_outside(tmp_path)
    json_path = output_dir / "cli-report.json"
    csv_path = output_dir / "cli-report.csv"
    result = main([
        "--manifest", str(manifest),
        "--json-output", str(json_path),
        "--csv-output", str(csv_path),
    ])
    assert result == 1
    assert json_path.exists()
    assert csv_path.exists()
    assert json.loads(json_path.read_text(encoding="utf-8"))["trials"][0]["status"] == "failed"


def test_cli_writes_invalid_manifest_report_without_traceback(tmp_path, capsys):
    manifest = tmp_path / "invalid.json"
    manifest.write_text(
        '{"schema_version":1,"offered_rate_mbps":4,"trials":[{"duration_seconds":NaN}]}',
        encoding="utf-8",
    )
    output_dir = report_dir_outside(tmp_path)
    json_path = output_dir / "invalid-report.json"
    csv_path = output_dir / "invalid-report.csv"

    result = main([
        "--manifest", str(manifest),
        "--json-output", str(json_path),
        "--csv-output", str(csv_path),
    ])

    assert result == 1
    assert json_path.exists() and csv_path.exists()
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["status"] == "incomplete"
    assert report["duration_confirmation"]["live_use_blocked"] is True
    assert capsys.readouterr().err == ""
