from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

import integration.campaign_validation as campaign_validation
from integration.campaign_validation import (
    MINIMUM_COMPLETE_CYCLES,
    SEQUENCES,
    STRATA,
    analyze_campaign,
)
from integration.evidence_analysis import EXPECTED_FLOWS


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _write_trial_package(root: Path, trial: dict, execution_order: int) -> None:
    trial_dir = root / f"trials/{trial['trial_id']}"
    trial_dir.mkdir(parents=True)
    start = 1000.0 + execution_order * 30.0
    finish = start + 30.0
    window_start = start + 5.0
    window_finish = window_start + 20.0
    clock_id = "synthetic-clock"
    flows = []
    for flow_id, port in EXPECTED_FLOWS.items():
        evidence_path = f"{flow_id}.jsonl"
        boundary_evidence_path = f"{flow_id}-boundary-evidence.json"
        boundary_evidence = {
            "schema_version": 1,
            "method": "synthetic exact boundary fixture",
            "start_boundary_uncertainty_seconds": 0.0,
            "end_boundary_uncertainty_seconds": 0.0,
        }
        boundary_evidence_bytes = (
            json.dumps(boundary_evidence, sort_keys=True) + "\n"
        ).encode("utf-8")
        (trial_dir / boundary_evidence_path).write_bytes(boundary_evidence_bytes)
        row = {
            "source": flow_id,
            "experiment_id": trial["experiment_id"],
            "requested_mbps": 4.0,
            "measurement_direction": "receiver",
            "throughput_measurement_source": "synthetic_receiver",
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
            "absolute_start_seconds": window_start,
            "absolute_end_seconds": window_finish,
            "clock_id": clock_id,
            "host_id": "synthetic-server-host",
            "boot_id": "synthetic-server-boot",
            "producer_name": "synthetic-fixture",
            "producer_version": "test-only",
            "producer_build_identity": "synthetic-build-identity",
            "producer_binary_sha256": "b" * 64,
            "producer_process_instance_id": "synthetic-server-boot:123:456",
            "producer_pid": 123,
            "producer_process_start_ticks": "456",
            "start_boundary_uncertainty_seconds": 0.0,
            "end_boundary_uncertainty_seconds": 0.0,
            "boundary_uncertainty_evidence": {
                "method": boundary_evidence["method"],
                "evidence_path": boundary_evidence_path,
                "sha256": hashlib.sha256(boundary_evidence_bytes).hexdigest(),
            },
            "boundary_metadata_status": "schema_valid",
            "boundary_evidence_integrity_status": "not_checked",
            "producer_capability_validation_status": "not_independently_validated",
            "clock_validation_status": "not_independently_validated",
            "measurement_alignment_eligibility": "ineligible",
        }
        (trial_dir / evidence_path).write_text(
            json.dumps(row) + "\n",
            encoding="utf-8",
        )
        flows.append({
            "flow_id": flow_id,
            "port": port,
            "evidence_path": evidence_path,
        })
    trial_manifest = {
        "schema_version": 1,
        "offered_rate_mbps": 4,
        "duration_confirmation": {"confirmed": False},
        "trials": [{
        "trial_id": trial["trial_id"],
        "experiment_id": trial["experiment_id"],
        "condition": trial["condition"],
        "profile": trial["profile"],
        "repetition_id": trial["repetition_id"],
        "duration_seconds": 20,
        "offered_rate_mbps": 4,
        "trial_status": "completed",
        "declared_window": {
            "clock_id": clock_id,
            "start_seconds": window_start,
            "end_seconds": window_finish,
        },
        "flows": flows,
        }],
    }
    _write_json(trial_dir / "trial-manifest.json", trial_manifest)
    _write_json(trial_dir / "trial_status.json", {
        "trial_id": trial["trial_id"],
        "experiment_id": trial["experiment_id"],
        "status": "completed",
    })
    _write_json(trial_dir / "aggregate-provenance.json", {
        "trial_id": trial["trial_id"],
        "experiment_id": trial["experiment_id"],
        "clock_id": clock_id,
        "start_seconds": window_start,
        "end_seconds": window_finish,
        "flow_ids": list(EXPECTED_FLOWS),
        "source_evidence_paths": {
            entry["flow_id"]: entry["evidence_path"] for entry in flows
        },
        "aggregate_throughput_mbps": 12.0,
        "aggregation_method": "receiver interval weighted mean",
    })
    raw_path = trial_dir / "raw-report.json"
    raw_path.write_text(
        json.dumps({"trial_id": trial["trial_id"], "raw": True}) + "\n",
        encoding="utf-8",
    )
    raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()

    trial_start_utc = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=execution_order * 30
    )
    trial["timestamps"] = {
        "trial_start_utc": trial_start_utc.isoformat(),
        "trial_finish_utc": (trial_start_utc + timedelta(seconds=30)).isoformat(),
        "window_start_utc": (trial_start_utc + timedelta(seconds=5)).isoformat(),
        "window_finish_utc": (trial_start_utc + timedelta(seconds=25)).isoformat(),
        "trial_start_monotonic_seconds": start,
        "trial_finish_monotonic_seconds": finish,
        "window_start_monotonic_seconds": window_start,
        "window_finish_monotonic_seconds": window_finish,
        "clock_id": clock_id,
    }
    trial["trial_manifest_path"] = f"trials/{trial['trial_id']}/trial-manifest.json"
    trial["trial_status_path"] = f"trials/{trial['trial_id']}/trial_status.json"
    trial["aggregate_provenance_path"] = (
        f"trials/{trial['trial_id']}/aggregate-provenance.json"
    )
    trial["raw_reports"] = [{
        "path": f"trials/{trial['trial_id']}/raw-report.json",
        "sha256": raw_hash,
    }]
    trial["traffic_control"] = {
        "before_snapshot_path": f"trials/{trial['trial_id']}/tc-before.json",
        "after_snapshot_path": f"trials/{trial['trial_id']}/tc-after.json",
        "counter_evidence_path": f"trials/{trial['trial_id']}/counters.json",
    }
    for phase in ("before", "after"):
        _write_json(trial_dir / f"tc-{phase}.json", {
            "trial_id": trial["trial_id"],
            "experiment_id": trial["experiment_id"],
            "captured_at_utc": (
                trial_start_utc - timedelta(seconds=1)
                if phase == "before"
                else trial_start_utc + timedelta(seconds=31)
            ).isoformat(),
            "qdiscs": [{"handle": "root"}],
            "classes": [{"classid": "root"}],
            "filters": [],
        })
    counter_data = {
        "trial_id": trial["trial_id"],
        "experiment_id": trial["experiment_id"],
        "before_captured_at_utc": (
            trial_start_utc - timedelta(seconds=1)
        ).isoformat(),
        "after_captured_at_utc": (
            trial_start_utc + timedelta(seconds=31)
        ).isoformat(),
    }
    for group, names in campaign_validation.COUNTER_GROUPS.items():
        counter_data[group] = {}
        for name in names:
            counter_data[group][f"{name}_before"] = 10
            counter_data[group][f"{name}_after"] = 15
            counter_data[group][f"{name}_delta"] = 5
    _write_json(trial_dir / "counters.json", counter_data)
    for key, status in (
        ("connectivity_verification_path", "verified"),
        ("restoration_verification_path", "verified"),
    ):
        name = key.removesuffix("_path").replace("_", "-") + ".json"
        trial[key] = f"trials/{trial['trial_id']}/{name}"
        _write_json(trial_dir / name, {
            "trial_id": trial["trial_id"],
            "experiment_id": trial["experiment_id"],
            "status": status,
        })


def make_campaign(root: Path, *, write_evidence: bool = False) -> dict:
    trials = []
    order = 0
    for cycle in range(1, MINIMUM_COMPLETE_CYCLES + 1):
        for sequence_id, strata in SEQUENCES.items():
            for stratum_id in strata:
                order += 1
                stratum = STRATA[stratum_id]
                trial = {
                    "trial_id": f"trial-{order:02d}",
                    "repetition_id": f"rep-{order:02d}",
                    "experiment_id": f"experiment-{order:02d}",
                    "condition": stratum["condition"],
                    "profile": stratum["profile"],
                    "stratum_id": stratum_id,
                    "cycle": cycle,
                    "sequence_id": sequence_id,
                    "execution_order": order,
                    "duration_seconds": 20,
                }
                if write_evidence:
                    _write_trial_package(root, trial, order)
                else:
                    trial.update({
                        "trial_manifest_path": f"trials/{trial['trial_id']}/trial-manifest.json",
                        "trial_status_path": f"trials/{trial['trial_id']}/trial_status.json",
                        "aggregate_provenance_path": (
                            f"trials/{trial['trial_id']}/aggregate-provenance.json"
                        ),
                        "raw_reports": [{"path": "raw.json", "sha256": "0" * 64}],
                        "timestamps": {},
                        "traffic_control": {},
                        "connectivity_verification_path": "connectivity.json",
                        "restoration_verification_path": "restoration.json",
                    })
                trials.append(trial)
    return {
        "schema_version": 1,
        "campaign_id": "synthetic-campaign",
        "claimed_status": "complete",
        "minimum_complete_cycles": 3,
        "offered_rate_mbps_per_flow": 4,
        "duration_seconds_per_flow": 20,
        "design": {
            "id": "WILLIAMS_4",
            "version": 1,
            "strata": STRATA,
            "sequences": {name: list(sequence) for name, sequence in SEQUENCES.items()},
        },
        "trials": trials,
    }


def write_campaign(root: Path, manifest: dict) -> Path:
    path = root / "campaign.json"
    _write_json(path, manifest)
    return path


def test_three_complete_williams_cycles_validate_offline(tmp_path):
    manifest = make_campaign(tmp_path, write_evidence=True)
    report = analyze_campaign(write_campaign(tmp_path, manifest))

    assert report["status"] == "complete"
    assert report["campaign_status"] == "complete"
    assert report["complete_cycle_count"] == 3
    assert len(report["trials"]) == 48
    assert report["alignment_status"] == "blocked"
    assert report["receiver_alignment_status"] == "blocked"
    assert report["measurement_alignment_eligibility"] == "ineligible"
    assert report["comparison_ready"] is False
    assert report["live_operations_supported"] is False
    assert all(trial["status"] == "complete" for trial in report["trials"])


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("two_cycles", "at least 3 complete"),
        ("duplicate_trial_id", "trial_id values must be globally unique"),
        ("duplicate_repetition_id", "repetition_id values must be globally unique"),
        ("duplicate_experiment_id", "experiment_id values must be globally unique"),
        ("missing_stratum", "sequence A must contain its four strata"),
        ("duplicate_stratum", "sequence A must contain its four strata"),
        ("invalid_sequence", "design.sequences do not match"),
        ("sequence_order", "sequence A must contain its four strata"),
        ("wrong_order", "execution_order values must be positive integers"),
        ("duplicate_order", "execution_order values must be unique and contiguous"),
        ("gapped_order", "execution_order values must be unique and contiguous"),
        ("missing_block", "cycle 1 must contain sequences A, B, C, and D"),
        ("three_sequence_subset", "at least 3 complete WILLIAMS_4 cycles"),
        ("missing_cycle_sequence", "must contain sequences A, B, C, and D"),
        ("global_order_mismatch", "global execution_order must follow"),
        ("incorrect_claim", "claimed_status"),
    ],
)
def test_campaign_schedule_contract_rejects_invalid_campaigns(
    tmp_path, monkeypatch, mutation, expected_error
):
    manifest = make_campaign(tmp_path)
    trials = manifest["trials"]
    if mutation == "two_cycles":
        manifest["trials"] = [trial for trial in trials if trial["cycle"] < 3]
        manifest["claimed_status"] = "incomplete"
    elif mutation == "duplicate_trial_id":
        trials[1]["trial_id"] = trials[0]["trial_id"]
    elif mutation == "duplicate_repetition_id":
        trials[1]["repetition_id"] = trials[0]["repetition_id"]
    elif mutation == "duplicate_experiment_id":
        trials[1]["experiment_id"] = trials[0]["experiment_id"]
    elif mutation == "missing_stratum":
        manifest["trials"] = [
            trial for trial in trials
            if not (
                trial["cycle"] == 1
                and trial["sequence_id"] == "A"
                and trial["stratum_id"] == "qos_low"
            )
        ]
    elif mutation == "duplicate_stratum":
        trials[1]["stratum_id"] = trials[0]["stratum_id"]
        trials[1]["condition"] = trials[0]["condition"]
        trials[1]["profile"] = trials[0]["profile"]
    elif mutation == "invalid_sequence":
        manifest["design"]["sequences"]["A"][0] = "qos_low"
    elif mutation == "sequence_order":
        trials[0]["stratum_id"], trials[1]["stratum_id"] = (
            trials[1]["stratum_id"],
            trials[0]["stratum_id"],
        )
    elif mutation == "wrong_order":
        trials[0]["execution_order"] = 0
    elif mutation == "duplicate_order":
        trials[1]["execution_order"] = trials[0]["execution_order"]
    elif mutation == "gapped_order":
        trials[-1]["execution_order"] += 1
    elif mutation == "missing_block":
        manifest["trials"] = [
            trial for trial in trials
            if not (trial["cycle"] == 1 and trial["sequence_id"] == "D")
        ]
    elif mutation == "three_sequence_subset":
        manifest["trials"] = [
            trial for trial in trials if trial["sequence_id"] != "D"
        ]
    elif mutation == "missing_cycle_sequence":
        manifest["trials"] = [
            trial for trial in trials
            if not (trial["cycle"] == 2 and trial["sequence_id"] == "C")
        ]
    elif mutation == "global_order_mismatch":
        trials[0]["cycle"] = 2
    elif mutation == "incorrect_claim":
        manifest["claimed_status"] = "incomplete"

    monkeypatch.setattr(
        campaign_validation,
        "_validate_trial_evidence",
        lambda _root, trial: {
            "trial_id": trial.get("trial_id"),
            "status": "complete",
            "analyzer_status": "complete",
            "alignment_status": "verified",
            "errors": [],
        },
    )
    monkeypatch.setattr(
        campaign_validation,
        "_duplicate_trial_references",
        lambda _root, _trials: {},
    )
    path = write_campaign(tmp_path, manifest)
    report = analyze_campaign(path)
    assert report["status"] == "incomplete"
    assert any(expected_error in error for error in report["campaign_errors"])
    if mutation == "three_sequence_subset":
        assert report["campaign_status"] == "incomplete"
        assert report["complete_cycle_count"] == 0
        assert len(report["trials"]) == 36


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing_trial_manifest", "trial_manifest_path must be a non-empty relative path"),
        ("path_traversal", "resolves outside the campaign directory"),
        ("missing_hash", "SHA-256 does not match"),
        ("missing_throughput", "receiver throughput_mbps is required"),
        ("null_throughput", "receiver throughput_mbps is required"),
        ("status_disagreement", "trial_status.json disagrees with analyzer status"),
        ("missing_absolute_boundaries", "absolute_start_seconds must be finite"),
        ("relative_only", "absolute_start_seconds must be finite"),
        ("empty_tc_snapshot", "traffic-control snapshot classes must not be empty"),
        ("snapshot_at_trial_start", "before traffic-control snapshot must precede trial start"),
        ("snapshot_at_trial_finish", "after traffic-control snapshot must follow trial finish"),
        ("counter_missing_timestamp", "counter before_captured_at_utc must precede trial start"),
    ],
)
def test_campaign_trial_evidence_and_alignment_gates(
    tmp_path, mutation, expected_error
):
    manifest = make_campaign(tmp_path, write_evidence=True)
    target = manifest["trials"][0]
    trial_manifest_path = tmp_path / target["trial_manifest_path"]
    trial_manifest = json.loads(trial_manifest_path.read_text(encoding="utf-8"))
    trial_spec = trial_manifest["trials"][0]
    if mutation == "missing_trial_manifest":
        target["trial_manifest_path"] = ""
    elif mutation == "path_traversal":
        target["trial_manifest_path"] = "..\\outside.json"
    elif mutation == "missing_hash":
        target["raw_reports"][0]["sha256"] = "0" * 64
    elif mutation in ("missing_throughput", "null_throughput", "missing_absolute_boundaries", "relative_only"):
        flow_path = trial_manifest_path.parent / trial_spec["flows"][0]["evidence_path"]
        row = json.loads(flow_path.read_text(encoding="utf-8"))
        if mutation == "missing_throughput":
            row.pop("throughput_mbps")
        elif mutation == "null_throughput":
            row["throughput_mbps"] = None
        elif mutation == "missing_absolute_boundaries":
            row.pop("absolute_start_seconds")
        else:
            row.pop("absolute_start_seconds")
            row.pop("absolute_end_seconds")
            row["interval_start_seconds"] = 0
            row["interval_end_seconds"] = 20
        flow_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    elif mutation == "status_disagreement":
        status_path = tmp_path / target["trial_status_path"]
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["status"] = "failed"
        _write_json(status_path, status)
    elif mutation in {
        "empty_tc_snapshot",
        "snapshot_at_trial_start",
        "snapshot_at_trial_finish",
    }:
        phase = "after" if mutation == "snapshot_at_trial_finish" else "before"
        snapshot_path = tmp_path / target["traffic_control"][f"{phase}_snapshot_path"]
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if mutation == "empty_tc_snapshot":
            snapshot["classes"] = []
        elif phase == "before":
            snapshot["captured_at_utc"] = target["timestamps"]["trial_start_utc"]
        else:
            snapshot["captured_at_utc"] = target["timestamps"]["trial_finish_utc"]
        _write_json(snapshot_path, snapshot)
    elif mutation == "counter_missing_timestamp":
        counter_path = tmp_path / target["traffic_control"]["counter_evidence_path"]
        counter_evidence = json.loads(counter_path.read_text(encoding="utf-8"))
        counter_evidence.pop("before_captured_at_utc")
        _write_json(counter_path, counter_evidence)

    report = analyze_campaign(write_campaign(tmp_path, manifest))
    target_report = next(
        trial for trial in report["trials"] if trial["trial_id"] == target["trial_id"]
    )
    assert report["status"] == "incomplete"
    assert any(expected_error in error for error in target_report["errors"])
    if mutation in {"missing_absolute_boundaries", "relative_only"}:
        assert target_report["alignment_status"] != "verified"


def test_campaign_completeness_is_distinct_from_receiver_alignment(tmp_path):
    manifest = make_campaign(tmp_path, write_evidence=True)
    target = manifest["trials"][0]
    trial_manifest_path = tmp_path / target["trial_manifest_path"]
    trial_manifest = json.loads(trial_manifest_path.read_text(encoding="utf-8"))
    flow_path = (
        trial_manifest_path.parent
        / trial_manifest["trials"][0]["flows"][0]["evidence_path"]
    )
    row = json.loads(flow_path.read_text(encoding="utf-8"))
    row.pop("absolute_start_seconds")
    flow_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    _write_json(trial_manifest_path, trial_manifest)

    report = analyze_campaign(write_campaign(tmp_path, manifest))

    assert report["campaign_status"] == "complete"
    assert report["alignment_status"] == "blocked"
    assert report["comparison_ready"] is False
    assert report["status"] == "incomplete"


def test_sender_temporal_comparison_stays_unavailable_without_mapping(tmp_path):
    manifest = make_campaign(tmp_path, write_evidence=True)
    target = manifest["trials"][0]

    report = analyze_campaign(write_campaign(tmp_path, manifest))
    trial_report = next(
        item for item in report["trials"] if item["trial_id"] == target["trial_id"]
    )

    assert report["campaign_status"] == "complete"
    assert report["receiver_alignment_status"] == "blocked"
    assert trial_report["receiver_alignment_status"] == "ineligible"
    assert trial_report["alignment_status"] == "ineligible"
    assert report["sender_receiver_temporal_comparison"]["status"] == "unavailable"
    assert trial_report["sender_receiver_temporal_comparison"]["status"] == "unavailable"


def test_campaign_validation_does_not_modify_evidence(tmp_path):
    manifest = make_campaign(tmp_path, write_evidence=True)
    campaign_path = write_campaign(tmp_path, manifest)
    evidence_file = tmp_path / manifest["trials"][0]["raw_reports"][0]["path"]
    before = evidence_file.read_bytes()

    analyze_campaign(campaign_path)

    assert evidence_file.read_bytes() == before


def test_campaign_cli_persists_unique_json_report_outside_evidence_without_overwrite(tmp_path, capsys):
    manifest = make_campaign(tmp_path / "campaign", write_evidence=False)
    campaign_path = write_campaign(tmp_path / "campaign", manifest)
    output_dir = tmp_path / "approved-reports"
    output_dir.mkdir()
    output_path = output_dir / "campaign-report.json"

    result = campaign_validation.main([
        "--manifest", str(campaign_path), "--json-output", str(output_path),
    ])

    assert result == 1  # this fixture is intentionally incomplete but produces a valid diagnostic report
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["campaign_id"] == manifest["campaign_id"]
    assert payload["status"] == "incomplete"
    assert "Campaign report written" in capsys.readouterr().out
    output_path.write_text("user data", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        campaign_validation.main([
            "--manifest", str(campaign_path), "--json-output", str(output_path),
        ])
    assert error.value.code == 2
    assert output_path.read_text(encoding="utf-8") == "user data"
