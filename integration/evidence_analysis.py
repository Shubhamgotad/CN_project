"""Offline analysis for explicitly manifested four-flow experiment evidence.

This module only reads files named in a manifest and writes JSON/CSV reports.
It never starts experiments, executes commands, or applies traffic control.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import ExitStack
from collections import defaultdict
from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any

from telemetry.receiver_windows import synchronized_receiver_aggregate
from telemetry.receiver_windows import (
    RECEIVER_INTERVAL_SCHEMA_VERSION,
    validate_receiver_interval_record,
)
from integration.safe_outputs import open_exclusive_text, validate_new_output_paths


EXPECTED_FLOWS = {"h1": 5201, "h2": 5202, "h3": 5203, "h4": 5204}
CONDITIONS = {"baseline", "qos"}
PROFILES = {"LOW", "MEDIUM", "HIGH"}
PROFILE_ALLOCATIONS_MBPS = {
    "LOW": {"gaming": 2.70, "video": 2.70, "web": 2.25, "bulk": 1.35},
    "MEDIUM": {"gaming": 3.60, "video": 2.70, "web": 1.80, "bulk": 0.90},
    "HIGH": {"gaming": 4.50, "video": 2.25, "web": 1.35, "bulk": 0.90},
}
BASELINE_ALLOCATIONS_MBPS = None
TRIAL_MATRIX_OFFERED_RATE_MBPS = 4.0
TRIAL_MATRIX_DURATION_SECONDS = 20.0


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


DOCUMENTED_SCENARIO_DURATION_SECONDS = 20.0
SUMMARY_METRICS = (
    "throughput_mbps",
    "latency_ms",
    "jitter_ms",
    "packet_loss_percent",
)
NONNEGATIVE_METRICS = (
    "throughput_mbps",
    "latency_ms",
    "jitter_ms",
    "packet_loss_percent",
    "lost_packets",
    "total_packets",
    "sender_throughput_mbps",
)


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    return value


def _load_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=float,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"{label} file is missing: {path}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is unreadable or malformed JSON: {exc}") from exc


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line, parse_constant=float)
                except (json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"{path} line {line_number}: malformed JSON: {exc}")
                    continue
                if not isinstance(value, Mapping):
                    errors.append(f"{path} line {line_number}: evidence row must be a JSON object")
                    continue
                rows.append(dict(value))
    except FileNotFoundError:
        errors.append(f"evidence file is missing: {path}")
    except (OSError, UnicodeError) as exc:
        errors.append(f"evidence file is unreadable: {path}: {exc}")
    return rows, errors


def _validate_metric(row: Mapping[str, Any], key: str, context: str) -> list[str]:
    if key not in row or row[key] is None:
        return []
    value = row[key]
    if not _is_finite_number(value):
        return [f"{context}: {key} must be a finite number or null"]
    if value < 0:
        return [f"{context}: {key} cannot be negative"]
    if key in {"lost_packets", "total_packets"} and not isinstance(value, int):
        return [f"{context}: {key} must be an integer"]
    if key == "packet_loss_percent" and value > 100:
        return [f"{context}: packet_loss_percent cannot exceed 100"]
    return []


def _load_flow_evidence(
    trial: Mapping[str, Any],
    flow_entry: Mapping[str, Any],
    manifest_dir: Path,
) -> tuple[list[dict[str, Any]], list[str], Path | None]:
    reference = flow_entry.get("evidence_path")
    flow = flow_entry.get("flow_id")
    if not isinstance(reference, str) or not reference.strip():
        return [], [f"flow {flow!r}: evidence_path is required"], None
    path, path_error = _resolve_evidence_path(reference, manifest_dir)
    if path_error:
        return [], [f"flow {flow}: {path_error}"], None
    assert path is not None
    if not path.is_file():
        return [], [f"flow {flow}: evidence file is missing: {reference}"], path
    rows, errors = _read_jsonl(path)
    for index, row in enumerate(rows, start=1):
        context = f"flow {flow} evidence row {index}"
        experiment_id = row.get("experiment_id")
        if experiment_id != trial.get("experiment_id"):
            errors.append(
                f"{context}: experiment_id {experiment_id!r} does not match "
                f"trial experiment_id {trial.get('experiment_id')!r}"
            )
        row_flow = row.get("flow_id", row.get("source"))
        if row_flow != flow:
            errors.append(
                f"{context}: flow identity {row_flow!r} does not match manifest flow {flow!r}"
            )
        direction = row.get("measurement_direction")
        source = row.get("throughput_measurement_source")
        if direction not in (None, "receiver", "sender"):
            errors.append(f"{context}: measurement_direction must be receiver or sender")
        if source is not None and not isinstance(source, str):
            errors.append(f"{context}: throughput_measurement_source must be a string")
        if (
            direction == "receiver"
            and isinstance(source, str)
            and ("client" in source.lower() or "sender" in source.lower())
        ):
            errors.append(
                f"{context}: receiver direction conflicts with sender-side throughput source"
            )
        if _receiver_provenance(row) and (
            row.get("receiver_interval_schema_version") is not None
            or "absolute_start_seconds" in row
            or "absolute_end_seconds" in row
        ):
            errors.extend(
                _receiver_boundary_evidence_errors(
                    row,
                    path,
                    manifest_dir,
                    context,
                )
            )
        if _receiver_provenance(row) and row.get("throughput_mbps") is None:
            errors.append(f"{context}: receiver throughput_mbps is required")
        for metric in NONNEGATIVE_METRICS:
            errors.extend(_validate_metric(row, metric, context))
        if "requested_mbps" not in row or row["requested_mbps"] is None:
            errors.append(f"{context}: requested_mbps is required")
        else:
            errors.extend(_validate_metric(row, "requested_mbps", context))
            requested_mbps = row["requested_mbps"]
            if (
                _is_finite_number(requested_mbps)
                and requested_mbps != TRIAL_MATRIX_OFFERED_RATE_MBPS
            ):
                errors.append(
                    f"{context}: requested_mbps must match the comparison-plan "
                    f"value {TRIAL_MATRIX_OFFERED_RATE_MBPS:g}"
                )
        lost = row.get("lost_packets")
        total = row.get("total_packets")
        if (
            _is_finite_number(lost)
            and _is_finite_number(total)
            and lost > total
        ):
            errors.append(f"{context}: lost_packets cannot exceed total_packets")
        if (
            "packet_loss_percent" in row
            and row["packet_loss_percent"] is not None
            and _is_finite_number(row["packet_loss_percent"])
            and row["packet_loss_percent"] > 100
        ):
            pass
        for key in (
            "absolute_start_seconds",
            "absolute_end_seconds",
            "interval_start_seconds",
            "interval_end_seconds",
        ):
            if key in row and row[key] is not None and not _is_finite_number(row[key]):
                errors.append(f"{context}: {key} must be a finite number")
        if (
            row.get("absolute_start_seconds") is not None
            and row.get("absolute_end_seconds") is not None
            and row["absolute_end_seconds"] <= row["absolute_start_seconds"]
        ):
            errors.append(f"{context}: absolute interval end must be after start")
        if (
            row.get("interval_start_seconds") is not None
            and row.get("interval_end_seconds") is not None
            and row["interval_end_seconds"] <= row["interval_start_seconds"]
        ):
            errors.append(f"{context}: interval end must be after start")
    return rows, errors, path


def _resolve_evidence_path(
    reference: str,
    manifest_dir: Path,
) -> tuple[Path | None, str | None]:
    # Treat backslashes as separators too, even when validation runs on POSIX.
    candidate = Path(reference.replace("\\", "/"))
    windows_candidate = PureWindowsPath(reference)
    if candidate.is_absolute() or windows_candidate.is_absolute() or windows_candidate.drive:
        return None, "evidence_path must be relative to the manifest directory"
    try:
        root = manifest_dir.resolve()
        resolved = (root / candidate).resolve()
        resolved.relative_to(root)
    except ValueError:
        return None, "evidence_path resolves outside the manifest directory"
    except (OSError, RuntimeError) as exc:
        return None, f"evidence_path cannot be safely resolved: {exc}"
    return resolved, None


def _receiver_provenance(row: Mapping[str, Any]) -> bool:
    direction = row.get("measurement_direction")
    if direction is not None:
        return direction == "receiver"
    source = row.get("throughput_measurement_source")
    return isinstance(source, str) and (
        "receiver" in source.lower() or "server" in source.lower()
    )


def _receiver_boundary_evidence_errors(
    row: Mapping[str, Any],
    interval_file: Path,
    evidence_root: Path,
    context: str,
) -> list[str]:
    if row.get("receiver_interval_schema_version") != RECEIVER_INTERVAL_SCHEMA_VERSION:
        return [f"{context}: versioned receiver measurement-boundary evidence is required"]
    try:
        validate_receiver_interval_record(row)
    except ValueError as exc:
        return [f"{context}: {exc}"]

    evidence = row["boundary_uncertainty_evidence"]
    evidence_path, path_error = _resolve_evidence_path(
        evidence["evidence_path"],
        interval_file.parent,
    )
    if path_error:
        return [f"{context}: boundary uncertainty evidence {path_error}"]
    assert evidence_path is not None
    try:
        evidence_path.relative_to(evidence_root.resolve())
    except ValueError:
        return [f"{context}: boundary uncertainty evidence escapes the trial evidence directory"]
    try:
        evidence_bytes = evidence_path.read_bytes()
    except OSError as exc:
        return [f"{context}: boundary uncertainty evidence cannot be read: {exc}"]
    if hashlib.sha256(evidence_bytes).hexdigest() != evidence["sha256"]:
        return [f"{context}: boundary uncertainty evidence SHA-256 does not match"]
    try:
        evidence_document = json.loads(
            evidence_bytes.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        return [f"{context}: boundary uncertainty evidence is malformed: {exc}"]
    if not isinstance(evidence_document, Mapping):
        return [f"{context}: boundary uncertainty evidence must contain an object"]
    for field, expected in (
        ("schema_version", 1),
        ("method", evidence["method"]),
        (
            "start_boundary_uncertainty_seconds",
            row["start_boundary_uncertainty_seconds"],
        ),
        (
            "end_boundary_uncertainty_seconds",
            row["end_boundary_uncertainty_seconds"],
        ),
    ):
        if evidence_document.get(field) != expected:
            return [
                f"{context}: boundary uncertainty evidence {field} "
                "does not match the interval record"
            ]
    return []


def _mean_available(rows: list[Mapping[str, Any]], key: str) -> float | None:
    values = [
        float(row[key]) for row in rows
        if (
            key in row
            and row[key] is not None
            and _is_finite_number(row[key])
            and row[key] >= 0
            and (key != "packet_loss_percent" or row[key] <= 100)
        )
    ]
    return sum(values) / len(values) if values else None


def _flow_summary(
    rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    receiver_rows = [row for row in rows if _receiver_provenance(row)]
    sender_rows = [
        row for row in rows
        if row.get("measurement_direction") == "sender"
        or row.get("sender_measurement_source") is not None
    ]
    receiver = {
        metric: _mean_available(receiver_rows, metric)
        for metric in SUMMARY_METRICS
    }
    sender = {
        "throughput_mbps": _mean_available(sender_rows, "sender_throughput_mbps"),
    }
    return {
        "receiver": {
            "status": "available" if receiver_rows else "unavailable",
            "sample_count": len(receiver_rows),
            "metrics": receiver,
        },
        "sender": {
            "status": "available" if sender_rows else "unavailable",
            "sample_count": len(sender_rows),
            "metrics": sender,
        },
    }


def _window_from_trial(trial: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    declared = trial.get("declared_window")
    if not isinstance(declared, Mapping):
        return None, "declared_window with clock_id/start_seconds/end_seconds is required"
    clock = declared.get("clock_id")
    start = declared.get("start_seconds")
    end = declared.get("end_seconds")
    if not isinstance(clock, str) or not clock.strip():
        return None, "declared_window.clock_id must be a non-empty string"
    if not _is_finite_number(start) or not _is_finite_number(end):
        return None, "declared_window start_seconds and end_seconds must be finite numbers"
    if end <= start:
        return None, "declared_window end_seconds must be after start_seconds"
    return {
        "clock_id": clock,
        "start_seconds": float(start),
        "end_seconds": float(end),
    }, None


def _contract_aggregate(
    trial: Mapping[str, Any],
    flow_rows: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, Any]:
    declared, error = _window_from_trial(trial)
    if error:
        return {
            "status": "unavailable",
            "reason": error,
            "_alignment_failure": True,
        }
    rows: list[dict[str, Any]] = []
    for flow in EXPECTED_FLOWS:
        for row in flow_rows.get(flow, []):
            if not _receiver_provenance(row):
                continue
            if row.get("experiment_id") != trial.get("experiment_id"):
                return {
                    "status": "unavailable",
                    "reason": f"flow {flow} interval experiment_id does not match this trial",
                    "_alignment_failure": True,
                }
            if row.get("flow_id", row.get("source")) != flow:
                return {
                    "status": "unavailable",
                    "reason": f"flow {flow} interval identity does not match this trial flow",
                    "_alignment_failure": True,
                }
            if row.get("measurement_direction") != "receiver":
                return {
                    "status": "unavailable",
                    "reason": f"flow {flow} interval lacks explicit measurement_direction=receiver",
                    "_alignment_failure": True,
                }
            if row.get("clock_id") != declared["clock_id"]:
                return {
                    "status": "unavailable",
                    "reason": (
                        f"flow {flow} interval clock_id does not match the "
                        "declared trial clock_id"
                    ),
                    "_alignment_failure": True,
                }
            if row.get("receiver_interval_schema_version") != RECEIVER_INTERVAL_SCHEMA_VERSION:
                return {
                    "status": "unavailable",
                    "reason": (
                        f"flow {flow} receiver interval lacks versioned "
                        "receiver measurement-boundary evidence"
                    ),
                    "_alignment_failure": True,
                }
            if row.get("throughput_mbps") is None:
                return {
                    "status": "unavailable",
                    "reason": f"flow {flow} receiver throughput is unavailable",
                }
            rows.append({
                **row,
                "flow_id": flow,
                "measurement_direction": "receiver",
            })
    try:
        aggregate = synchronized_receiver_aggregate(rows, tuple(EXPECTED_FLOWS))
    except (OverflowError, TypeError, ValueError) as exc:
        return {
            "status": "unavailable",
            "reason": str(exc),
            "_alignment_failure": True,
        }
    if (
        not math.isclose(
            aggregate["window_start_seconds"],
            declared["start_seconds"],
            rel_tol=0,
            abs_tol=1e-9,
        )
        or not math.isclose(
            aggregate["window_end_seconds"],
            declared["end_seconds"],
            rel_tol=0,
            abs_tol=1e-9,
        )
    ):
        return {
            "status": "unavailable",
            "reason": "receiver intervals do not cover the exact declared trial window",
            "_alignment_failure": True,
        }
    return {
        "status": "contract_valid",
        "boundary_metadata_status": "schema_valid",
        "boundary_evidence_integrity_status": "verified",
        "producer_provenance_status": "not_independently_validated",
        "clock_mapping_status": "not_independently_validated",
        "measurement_alignment_eligibility": "ineligible",
        "alignment_mode": "receiver_only_contract",
        **aggregate,
    }


def _manifest_errors(manifest: Any) -> list[str]:
    if not isinstance(manifest, Mapping):
        return ["manifest root must be a JSON object"]
    if manifest.get("schema_version") != 1:
        return ["schema_version must be 1"]
    trials = manifest.get("trials")
    if not isinstance(trials, list):
        return ["trials must be an array"]
    errors = []
    if not _is_finite_number(manifest.get("offered_rate_mbps")):
        errors.append("offered_rate_mbps must be a finite numeric manifest value")
    elif manifest["offered_rate_mbps"] != TRIAL_MATRIX_OFFERED_RATE_MBPS:
        errors.append(
            f"offered_rate_mbps must match the comparison-plan matrix value "
            f"{TRIAL_MATRIX_OFFERED_RATE_MBPS:g}"
        )
    return errors


def _trial_shape_errors(trial: Mapping[str, Any]) -> list[str]:
    errors = []
    required_strings = ("trial_id", "experiment_id")
    for field in required_strings:
        if not isinstance(trial.get(field), str) or not trial[field].strip():
            errors.append(f"{field} must be a non-empty string")
    condition = trial.get("condition")
    profile = trial.get("profile")
    if not isinstance(condition, str) or condition not in CONDITIONS:
        errors.append("condition must be baseline or qos")
    if condition == "baseline" and profile != "BASELINE":
        errors.append("baseline condition requires profile BASELINE")
    if condition == "qos" and (
        not isinstance(profile, str) or profile not in PROFILES
    ):
        errors.append("qos condition profile must be LOW, MEDIUM, or HIGH")
    repetition = trial.get("repetition_id")
    if isinstance(repetition, bool) or not isinstance(repetition, (int, str)) or not str(repetition).strip():
        errors.append("repetition_id must be a non-empty string or integer")
    duration = trial.get("duration_seconds")
    if not _is_finite_number(duration) or duration <= 0:
        errors.append("duration_seconds must be a positive finite number")
    declared_status = trial.get("trial_status", "completed")
    if not isinstance(declared_status, str) or declared_status not in {
        "completed",
        "failed",
    }:
        errors.append("trial_status must be completed or failed")
    if declared_status == "failed" and (
        not isinstance(trial.get("failure_reason"), str)
        or not trial["failure_reason"].strip()
    ):
        errors.append("failed trial requires a non-empty failure_reason")
    flows = trial.get("flows")
    if not isinstance(flows, list):
        errors.append("flows must be an array")
        return errors
    found: dict[str, Mapping[str, Any]] = {}
    for entry in flows:
        if not isinstance(entry, Mapping):
            errors.append("each flow entry must be an object")
            continue
        flow_id = entry.get("flow_id")
        if not isinstance(flow_id, str) or flow_id not in EXPECTED_FLOWS:
            errors.append(f"unexpected flow identity {flow_id!r}")
            continue
        if flow_id in found:
            errors.append(f"duplicate flow identity {flow_id!r}")
        found[flow_id] = entry
        if entry.get("port") != EXPECTED_FLOWS[flow_id]:
            errors.append(f"flow {flow_id} must use port {EXPECTED_FLOWS[flow_id]}")
        if not isinstance(entry.get("evidence_path"), str) or not entry["evidence_path"].strip():
            errors.append(f"flow {flow_id} evidence_path is required")
    missing = set(EXPECTED_FLOWS) - set(found)
    if missing:
        errors.append(f"required flows are missing: {sorted(missing)}")
    return errors


def _duration_confirmation(
    manifest: Mapping[str, Any],
    *,
    manifest_valid: bool = True,
) -> dict[str, Any]:
    confirmation = manifest.get("duration_confirmation")
    trials = manifest.get("trials", [])
    if not isinstance(trials, list):
        trials = []
    if (
        manifest_valid
        and isinstance(confirmation, Mapping)
        and bool(trials)
        and confirmation.get("confirmed") is True
        and isinstance(confirmation.get("operator"), str)
        and confirmation["operator"].strip()
        and isinstance(confirmation.get("note"), str)
        and confirmation["note"].strip()
        and _is_finite_number(confirmation.get("confirmed_duration_seconds"))
        and confirmation["confirmed_duration_seconds"] > 0
        and all(
            isinstance(trial, Mapping)
            and _is_finite_number(trial.get("duration_seconds"))
            and trial["duration_seconds"] > 0
            and trial["duration_seconds"] == confirmation["confirmed_duration_seconds"]
            for trial in trials
        )
    ):
        status = "confirmed_by_operator"
    else:
        status = "pending_operator_confirmation"
    return {
        "status": status,
        "live_use_blocked": status != "confirmed_by_operator",
        "matrix_duration_seconds": TRIAL_MATRIX_DURATION_SECONDS,
        "other_documented_scenario_duration_seconds": DOCUMENTED_SCENARIO_DURATION_SECONDS,
        "operator_confirmed_duration_seconds": (
            _json_safe_value(confirmation.get("confirmed_duration_seconds"))
            if isinstance(confirmation, Mapping)
            else None
        ),
        "trial_durations_seconds": sorted({
            trial.get("duration_seconds")
            for trial in trials
            if isinstance(trial, Mapping)
            and _is_finite_number(trial.get("duration_seconds"))
        }),
        "issue": (
            "The approved comparison duration is 20 seconds. Operator confirmation "
            "documents that duration only; it does not authorize live execution."
        ),
    }


def analyze_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Analyze only the explicit files listed in a per-trial JSON manifest."""
    manifest_file = Path(manifest_path).expanduser().resolve()
    try:
        manifest = _load_json(manifest_file, label="manifest")
    except ValueError as exc:
        return {
            "schema_version": 1,
            "status": "invalid_manifest",
            "manifest_path": str(manifest_file),
            "manifest_errors": [str(exc)],
            "trials": [],
            "offline_only": True,
            "live_operations_supported": False,
            "duration_confirmation": _duration_confirmation({}),
        }
    errors = _manifest_errors(manifest)
    if not isinstance(manifest, Mapping):
        return {
            "schema_version": 1,
            "status": "invalid_manifest",
            "manifest_path": str(manifest_file),
            "manifest_errors": errors,
            "trials": [],
            "offline_only": True,
            "live_operations_supported": False,
            "duration_confirmation": _duration_confirmation({}),
        }
    trial_specs = manifest.get("trials", [])
    if not isinstance(trial_specs, list):
        trial_specs = []
    trial_reports: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    seen_repetitions: dict[tuple[Any, Any, Any], int] = {}
    references: dict[Path, list[int]] = defaultdict(list)
    duplicated_ids: set[str] = set()
    duplicated_repetitions: set[tuple[Any, Any, Any]] = set()
    manifest_structure_valid = not errors
    for index, spec in enumerate(trial_specs):
        if not isinstance(spec, Mapping):
            manifest_structure_valid = False
            trial_reports.append({
                "trial_id": None,
                "status": "failed",
                "errors": ["trial entry must be an object"],
                "flows": {},
            })
            continue
        trial_id = spec.get("trial_id")
        trial_errors = _trial_shape_errors(spec)
        if trial_errors:
            manifest_structure_valid = False
        if isinstance(trial_id, str) and trial_id:
            if trial_id in seen_ids:
                manifest_structure_valid = False
                duplicated_ids.add(trial_id)
                trial_errors.append(f"duplicate trial_id {trial_id!r}")
            else:
                seen_ids[trial_id] = index
        condition = spec.get("condition")
        profile = spec.get("profile")
        repetition = spec.get("repetition_id")
        repetition_key = None
        if (
            isinstance(condition, str)
            and isinstance(profile, str)
            and not isinstance(repetition, bool)
            and isinstance(repetition, (int, str))
        ):
            repetition_key = (condition, profile, str(repetition))
            if repetition_key in seen_repetitions:
                manifest_structure_valid = False
                duplicated_repetitions.add(repetition_key)
                trial_errors.append(
                    "duplicate condition/profile repetition_id "
                    f"{repetition_key!r}"
                )
            else:
                seen_repetitions[repetition_key] = index
        for flow_entry in spec.get("flows", []) if isinstance(spec.get("flows"), list) else []:
            if isinstance(flow_entry, Mapping) and isinstance(flow_entry.get("evidence_path"), str):
                resolved_path, path_error = _resolve_evidence_path(
                    flow_entry["evidence_path"],
                    manifest_file.parent,
                )
                if resolved_path is not None:
                    references[resolved_path].append(index)
        trial_reports.append({
            "trial_id": trial_id,
            "condition": spec.get("condition"),
            "profile": spec.get("profile"),
            "repetition_id": spec.get("repetition_id"),
            "experiment_id": spec.get("experiment_id"),
            "duration_seconds": (
                spec.get("duration_seconds")
                if _is_finite_number(spec.get("duration_seconds"))
                else None
            ),
            "failure_reason": spec.get("failure_reason"),
            "status": "pending",
            "receiver_alignment_status": "unavailable",
            "boundary_metadata_status": "invalid_or_incomplete",
            "boundary_evidence_integrity_status": "not_verified",
            "producer_provenance_status": "not_independently_validated",
            "clock_mapping_status": "not_independently_validated",
            "measurement_alignment_eligibility": "ineligible",
            "sender_receiver_temporal_comparison": {
                "status": "unavailable",
                "reason": (
                    "sender-to-receiver clock mapping is not implemented or "
                    "independently validated"
                ),
            },
            "errors": trial_errors,
            "flows": {},
            "_spec": spec,
        })
        if spec.get("trial_status", "completed") == "failed" and isinstance(
            spec.get("failure_reason"), str
        ):
            trial_reports[-1]["errors"].append(
                f"trial was reported failed: {spec['failure_reason']}"
            )

    for trial_report in trial_reports:
        trial_id = trial_report.get("trial_id")
        if isinstance(trial_id, str) and trial_id in duplicated_ids:
            message = f"duplicate trial_id {trial_id!r}"
            if message not in trial_report["errors"]:
                trial_report["errors"].append(message)
        condition = trial_report.get("condition")
        profile = trial_report.get("profile")
        repetition = trial_report.get("repetition_id")
        repetition_key = (
            (condition, profile, str(repetition))
            if (
                isinstance(condition, str)
                and isinstance(profile, str)
                and not isinstance(repetition, bool)
                and isinstance(repetition, (int, str))
            )
            else None
        )
        if repetition_key is not None and repetition_key in duplicated_repetitions:
            message = f"duplicate condition/profile repetition_id {repetition_key!r}"
            if message not in trial_report["errors"]:
                trial_report["errors"].append(message)
        trial_report["errors"].extend(errors)

    for path, indices in references.items():
        if len(indices) > 1:
            manifest_structure_valid = False
            message = f"duplicate evidence reference: {path}"
            for index in indices:
                trial_reports[index]["errors"].append(message)

    for index, result in enumerate(trial_reports):
        spec = result.pop("_spec", None)
        if not isinstance(spec, Mapping):
            continue
        flow_rows: dict[str, list[Mapping[str, Any]]] = {}
        entries = {
            entry.get("flow_id"): entry
            for entry in spec.get("flows", [])
            if isinstance(entry, Mapping)
            and isinstance(entry.get("flow_id"), str)
            and entry.get("flow_id") in EXPECTED_FLOWS
        }
        for flow in EXPECTED_FLOWS:
            entry = entries.get(flow)
            if entry is None:
                result["flows"][flow] = {
                    "status": "unavailable",
                    "errors": [f"required flow {flow} evidence is not listed"],
                    "receiver": {"status": "unavailable", "sample_count": 0, "metrics": {key: None for key in SUMMARY_METRICS}},
                    "sender": {"status": "unavailable", "sample_count": 0, "metrics": {"throughput_mbps": None}},
                }
                flow_rows[flow] = []
                continue
            rows, load_errors, path = _load_flow_evidence(spec, entry, manifest_file.parent)
            result["errors"].extend(load_errors)
            flow_rows[flow] = rows
            flow_summary = _flow_summary(rows)
            flow_errors = list(load_errors)
            if rows and flow_summary["receiver"]["sample_count"] == 0:
                flow_errors.append(
                    f"flow {flow}: evidence has no explicitly identified receiver-side samples"
                )
                result["errors"].append(flow_errors[-1])
            result["flows"][flow] = {
                "status": "failed" if flow_errors else (
                    "available" if rows else "unavailable"
                ),
                "evidence_path": str(path) if path is not None else entry.get("evidence_path"),
                "errors": flow_errors,
                **flow_summary,
            }
            if not rows and not load_errors:
                message = f"flow {flow} evidence contains no records"
                result["errors"].append(message)
                result["flows"][flow]["status"] = "failed"
                result["flows"][flow]["errors"].append(message)

        aggregate = _contract_aggregate(spec, flow_rows)
        schema_status = (
            "schema_valid" if aggregate["status"] == "contract_valid"
            else "invalid_or_incomplete"
        )
        boundary_artifact_errors = [
            error
            for flow_report in result["flows"].values()
            for error in flow_report["errors"]
            if "boundary uncertainty evidence" in error
        ]
        if aggregate["status"] == "contract_valid" and boundary_artifact_errors:
            aggregate = {
                "status": "unavailable",
                "reason": (
                    "receiver boundary evidence artifacts failed integrity "
                    "validation"
                ),
                "_alignment_failure": True,
            }
        result["receiver_window_aggregate"] = aggregate
        aggregate_status = aggregate["status"]
        result["receiver_alignment_status"] = (
            "ineligible" if aggregate_status == "contract_valid" else "unavailable"
        )
        result["boundary_metadata_status"] = schema_status
        result["boundary_evidence_integrity_status"] = (
            "verified"
            if aggregate_status == "contract_valid" and not boundary_artifact_errors
            else "not_verified"
        )
        result["producer_provenance_status"] = "not_independently_validated"
        result["clock_mapping_status"] = "not_independently_validated"
        result["measurement_alignment_eligibility"] = "ineligible"
        alignment_failure = result["receiver_window_aggregate"].pop(
            "_alignment_failure",
            False,
        )
        if alignment_failure:
            result["errors"].append(
                "receiver intervals cannot be aligned: "
                + result["receiver_window_aggregate"]["reason"]
            )
        if aggregate_status != "contract_valid":
            result["unverified_arithmetic_throughput_summary"] = {
                "status": "unverified",
                "label": "unverified arithmetic sum of per-flow receiver means",
                "throughput_mbps": (
                    sum(
                        flow_summary["receiver"]["metrics"]["throughput_mbps"]
                        for flow_summary in result["flows"].values()
                        if flow_summary["receiver"]["metrics"].get("throughput_mbps") is not None
                    )
                    if any(
                        flow_summary["receiver"]["metrics"].get("throughput_mbps") is not None
                        for flow_summary in result["flows"].values()
                    )
                    else None
                ),
            }
        else:
            result["unverified_arithmetic_throughput_summary"] = {
                "status": "not_emitted",
                "label": None,
                "throughput_mbps": None,
            }
        result["status"] = "failed" if result["errors"] else "complete"

    status = "invalid_manifest" if errors else (
        "complete" if trial_reports and all(row.get("status") == "complete" for row in trial_reports)
        else "incomplete"
    )
    comparison_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for trial in trial_reports:
        condition = trial.get("condition")
        profile = trial.get("profile")
        if isinstance(condition, str) and condition in CONDITIONS and isinstance(profile, str):
            comparison_groups[(condition, profile)].append(trial)
    comparisons = [
        {
            "condition": condition,
            "profile": profile,
            "trials": [
                {
                    "trial_id": trial.get("trial_id"),
                    "repetition_id": trial.get("repetition_id"),
                    "status": trial.get("status"),
                }
                for trial in sorted(
                    group,
                    key=lambda item: str(item.get("repetition_id")),
                )
            ],
            "repetition_count": len(group),
            "complete_repetitions": sum(
                trial.get("status") == "complete" for trial in group
            ),
            "failed_repetitions": sum(
                trial.get("status") != "complete" for trial in group
            ),
        }
        for (condition, profile), group in sorted(comparison_groups.items())
    ]
    return {
        "schema_version": 1,
        "status": status,
        "manifest_path": str(manifest_file),
        "manifest_errors": errors,
        "offline_only": True,
        "live_operations_supported": False,
        "sender_receiver_temporal_comparison": {
            "status": "unavailable",
            "reason": (
                "sender-to-receiver clock mapping is not implemented or "
                "independently validated"
            ),
        },
        "duration_confirmation": _duration_confirmation(
            manifest,
            manifest_valid=manifest_structure_valid,
        ),
        "comparisons_by_repetition": comparisons,
        "trial_matrix": {
            "required_flows": [
                {"flow_id": flow, "port": port}
                for flow, port in EXPECTED_FLOWS.items()
            ],
            "offered_rate_mbps_per_flow": TRIAL_MATRIX_OFFERED_RATE_MBPS,
            "minimum_repetitions_per_condition_profile": 3,
            "profiles_mbps": PROFILE_ALLOCATIONS_MBPS,
            "baseline_profile": BASELINE_ALLOCATIONS_MBPS,
            "duration_seconds": TRIAL_MATRIX_DURATION_SECONDS,
        },
        "trials": _json_safe_value(trial_reports),
    }


def write_reports(
    report: Mapping[str, Any],
    json_output: str | Path,
    csv_output: str | Path,
) -> None:
    manifest_value = report.get("manifest_path")
    if not isinstance(manifest_value, (str, Path)) or not str(manifest_value).strip():
        raise ValueError("analysis report must include its manifest path")
    evidence_root = Path(manifest_value).expanduser().resolve().parent
    json_path, csv_path = validate_new_output_paths(
        (json_output, csv_output),
        protected_roots=(evidence_root,),
    )
    safe_report = _json_safe_value(report)
    fields = (
        "trial_id", "condition", "profile", "repetition_id", "experiment_id",
        "duration_seconds", "trial_status", "flow_id", "flow_status",
        "receiver_sample_count", "receiver_throughput_mbps",
        "receiver_latency_ms", "receiver_jitter_ms",
        "receiver_packet_loss_percent", "sender_sample_count",
        "sender_throughput_mbps", "receiver_window_contract_status",
        "receiver_window_contract_throughput_mbps",
        "unverified_arithmetic_throughput_mbps", "failure_reasons",
    )
    with ExitStack() as stack:
        json_stream = stack.enter_context(open_exclusive_text(json_path))
        csv_stream = stack.enter_context(open_exclusive_text(csv_path, newline=""))
        json_stream.write(json.dumps(safe_report, indent=2, allow_nan=False) + "\n")
        writer = csv.DictWriter(csv_stream, fieldnames=fields)
        writer.writeheader()
        for trial in safe_report.get("trials", []):
            flows = trial.get("flows", {})
            for flow_id in EXPECTED_FLOWS:
                flow = flows.get(flow_id, {})
                receiver = flow.get("receiver", {})
                receiver_metrics = receiver.get("metrics", {})
                sender = flow.get("sender", {})
                aggregate = trial.get("receiver_window_aggregate", {})
                unverified = trial.get("unverified_arithmetic_throughput_summary", {})
                writer.writerow({
                    "trial_id": trial.get("trial_id"),
                    "condition": trial.get("condition"),
                    "profile": trial.get("profile"),
                    "repetition_id": trial.get("repetition_id"),
                    "experiment_id": trial.get("experiment_id"),
                    "duration_seconds": trial.get("duration_seconds"),
                    "trial_status": trial.get("status"),
                    "flow_id": flow_id,
                    "flow_status": flow.get("status"),
                    "receiver_sample_count": receiver.get("sample_count", 0),
                    "receiver_throughput_mbps": receiver_metrics.get("throughput_mbps"),
                    "receiver_latency_ms": receiver_metrics.get("latency_ms"),
                    "receiver_jitter_ms": receiver_metrics.get("jitter_ms"),
                    "receiver_packet_loss_percent": receiver_metrics.get("packet_loss_percent"),
                    "sender_sample_count": sender.get("sample_count", 0),
                    "sender_throughput_mbps": sender.get("metrics", {}).get("throughput_mbps"),
                    "receiver_window_contract_status": aggregate.get("status"),
                    "receiver_window_contract_throughput_mbps": aggregate.get("aggregate_throughput_mbps"),
                    "unverified_arithmetic_throughput_mbps": unverified.get("throughput_mbps"),
                    "failure_reasons": " | ".join(trial.get("errors", [])),
                })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze explicitly manifested experiment evidence offline."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--csv-output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = analyze_manifest(args.manifest)
    try:
        write_reports(report, args.json_output, args.csv_output)
    except (OSError, TypeError, ValueError) as exc:
        parser.exit(2, f"could not write evidence analysis reports: {exc}\n")
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
