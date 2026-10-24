"""Offline validation for explicitly manifested Adaptive QoS campaigns."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
from typing import Any

from integration.evidence_analysis import (
    EXPECTED_FLOWS,
    TRIAL_MATRIX_DURATION_SECONDS,
    TRIAL_MATRIX_OFFERED_RATE_MBPS,
    _is_finite_number,
    analyze_manifest,
)


CAMPAIGN_SCHEMA_VERSION = 1
WILLIAMS_DESIGN = "WILLIAMS_4"
WILLIAMS_DESIGN_VERSION = 1
MINIMUM_COMPLETE_CYCLES = 3
STRATA = {
    "baseline": {"condition": "baseline", "profile": "BASELINE"},
    "qos_low": {"condition": "qos", "profile": "LOW"},
    "qos_medium": {"condition": "qos", "profile": "MEDIUM"},
    "qos_high": {"condition": "qos", "profile": "HIGH"},
}
SEQUENCES = {
    "A": ("baseline", "qos_low", "qos_high", "qos_medium"),
    "B": ("qos_low", "qos_medium", "baseline", "qos_high"),
    "C": ("qos_medium", "qos_high", "qos_low", "baseline"),
    "D": ("qos_high", "baseline", "qos_medium", "qos_low"),
}
UTC_FIELDS = (
    "trial_start_utc",
    "trial_finish_utc",
    "window_start_utc",
    "window_finish_utc",
)
MONOTONIC_FIELDS = (
    "trial_start_monotonic_seconds",
    "trial_finish_monotonic_seconds",
    "window_start_monotonic_seconds",
    "window_finish_monotonic_seconds",
)
COUNTER_GROUPS = {
    "interface": ("tx_bytes", "rx_bytes"),
    "server": ("rx_bytes", "tx_bytes"),
    "tc": ("qdisc_packets", "class_bytes", "filter_packets"),
}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is missing, unreadable, or malformed: {exc}") from exc


def _resolve_path(root: Path, value: Any, label: str) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty relative path"
    # Treat backslashes as separators too, even when validation runs on POSIX.
    candidate = Path(value.replace("\\", "/"))
    windows_path = PureWindowsPath(value)
    if candidate.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        return None, f"{label} must be relative to the campaign directory"
    try:
        resolved_root = root.resolve()
        resolved = (resolved_root / candidate).resolve()
        resolved.relative_to(resolved_root)
    except ValueError:
        return None, f"{label} resolves outside the campaign directory"
    except (OSError, RuntimeError) as exc:
        return None, f"{label} cannot be safely resolved: {exc}"
    return resolved, None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _same_identity(document: Mapping[str, Any], trial: Mapping[str, Any]) -> list[str]:
    errors = []
    for field in ("trial_id", "experiment_id"):
        if document.get(field) != trial.get(field):
            errors.append(f"{field} does not match campaign trial")
    return errors


def _validate_timestamps(trial: Mapping[str, Any], context: str) -> list[str]:
    errors = []
    timestamps = trial.get("timestamps")
    if not isinstance(timestamps, Mapping):
        return [f"{context}: timestamps object is required"]
    parsed: dict[str, datetime] = {}
    for field in UTC_FIELDS:
        value = _aware_datetime(timestamps.get(field))
        if value is None:
            errors.append(f"{context}: timestamps.{field} must be timezone-aware ISO-8601")
        else:
            parsed[field] = value
    monotonic: dict[str, float] = {}
    for field in MONOTONIC_FIELDS:
        value = timestamps.get(field)
        if not _is_finite_number(value):
            errors.append(f"{context}: timestamps.{field} must be a finite number")
        else:
            monotonic[field] = float(value)
    if len(parsed) == len(UTC_FIELDS):
        if parsed["trial_finish_utc"] <= parsed["trial_start_utc"]:
            errors.append(f"{context}: UTC trial finish must be after trial start")
        if parsed["window_finish_utc"] <= parsed["window_start_utc"]:
            errors.append(f"{context}: UTC measurement window must have positive duration")
        if not (
            parsed["trial_start_utc"] <= parsed["window_start_utc"]
            < parsed["window_finish_utc"] <= parsed["trial_finish_utc"]
        ):
            errors.append(f"{context}: UTC measurement window must be inside the trial")
        elif not math.isclose(
            (
                parsed["window_finish_utc"] - parsed["window_start_utc"]
            ).total_seconds(),
            TRIAL_MATRIX_DURATION_SECONDS,
            rel_tol=0,
            abs_tol=0.001,
        ):
            errors.append(f"{context}: UTC measurement window must be 20 seconds")
    if len(monotonic) == len(MONOTONIC_FIELDS):
        if monotonic["trial_finish_monotonic_seconds"] <= monotonic["trial_start_monotonic_seconds"]:
            errors.append(f"{context}: monotonic trial finish must be after trial start")
        if monotonic["window_finish_monotonic_seconds"] <= monotonic["window_start_monotonic_seconds"]:
            errors.append(f"{context}: monotonic measurement window must have positive duration")
        if not (
            monotonic["trial_start_monotonic_seconds"]
            <= monotonic["window_start_monotonic_seconds"]
            < monotonic["window_finish_monotonic_seconds"]
            <= monotonic["trial_finish_monotonic_seconds"]
        ):
            errors.append(f"{context}: monotonic measurement window must be inside the trial")
        if not math.isclose(
            monotonic["window_finish_monotonic_seconds"]
            - monotonic["window_start_monotonic_seconds"],
            TRIAL_MATRIX_DURATION_SECONDS,
            rel_tol=0,
            abs_tol=0.001,
        ):
            errors.append(f"{context}: monotonic measurement window must be 20 seconds")
    if not _nonempty_string(timestamps.get("clock_id")):
        errors.append(f"{context}: timestamps.clock_id must be a non-empty string")
    return errors


def _validate_json_artifact(
    root: Path,
    value: Any,
    label: str,
    trial: Mapping[str, Any],
    trial_directory: Path,
) -> tuple[Mapping[str, Any] | None, list[str]]:
    path, path_error = _resolve_path(root, value, label)
    if path_error:
        return None, [path_error]
    assert path is not None
    try:
        path.relative_to(trial_directory.resolve())
    except ValueError:
        return None, [f"{label} must remain inside its trial evidence directory"]
    if not path.is_file():
        return None, [f"{label} file is missing"]
    try:
        document = _load_json(path, label)
    except ValueError as exc:
        return None, [str(exc)]
    if not isinstance(document, Mapping):
        return None, [f"{label} must contain a JSON object"]
    return document, _same_identity(document, trial)


def _validate_counter_evidence(
    evidence: Mapping[str, Any],
    context: str,
) -> list[str]:
    errors = []
    for group, names in COUNTER_GROUPS.items():
        values = evidence.get(group)
        if not isinstance(values, Mapping):
            errors.append(f"{context}: {group} counter object is required")
            continue
        for name in names:
            before_key = f"{name}_before"
            after_key = f"{name}_after"
            delta_key = f"{name}_delta"
            before = values.get(before_key)
            after = values.get(after_key)
            delta = values.get(delta_key)
            for key, value in (
                (before_key, before),
                (after_key, after),
                (delta_key, delta),
            ):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    errors.append(f"{context}: {group}.{key} must be a nonnegative integer")
            if (
                isinstance(before, int)
                and not isinstance(before, bool)
                and isinstance(after, int)
                and not isinstance(after, bool)
                and isinstance(delta, int)
                and not isinstance(delta, bool)
                and delta != after - before
            ):
                errors.append(f"{context}: {group}.{delta_key} must equal after minus before")
    return errors


def _validate_trial_evidence(
    root: Path,
    trial: Mapping[str, Any],
) -> dict[str, Any]:
    trial_id = trial.get("trial_id")
    context = f"trial {trial_id!r}"
    errors: list[str] = []
    trial_manifest_path, path_error = _resolve_path(
        root,
        trial.get("trial_manifest_path"),
        f"{context} trial_manifest_path",
    )
    if path_error:
        return {
            "trial_id": trial_id,
            "status": "failed",
            "alignment_status": "unavailable",
            "errors": [path_error],
        }
    assert trial_manifest_path is not None
    trial_directory = trial_manifest_path.parent
    if trial_directory == root.resolve():
        return {
            "trial_id": trial_id,
            "status": "failed",
            "alignment_status": "unavailable",
            "errors": [f"{context}: trial manifest must be inside a dedicated trial directory"],
        }
    if not trial_manifest_path.is_file():
        return {
            "trial_id": trial_id,
            "status": "failed",
            "alignment_status": "unavailable",
            "errors": [f"{context}: trial manifest file is missing"],
        }
    try:
        trial_manifest = _load_json(trial_manifest_path, f"{context} trial manifest")
    except ValueError as exc:
        return {
            "trial_id": trial_id,
            "status": "failed",
            "alignment_status": "unavailable",
            "errors": [str(exc)],
        }
    if not isinstance(trial_manifest, Mapping):
        return {
            "trial_id": trial_id,
            "status": "failed",
            "alignment_status": "unavailable",
            "errors": [f"{context}: trial manifest must be a JSON object"],
        }
    trial_specs = trial_manifest.get("trials")
    if not isinstance(trial_specs, list) or len(trial_specs) != 1 or not isinstance(
        trial_specs[0], Mapping
    ):
        return {
            "trial_id": trial_id,
            "status": "failed",
            "alignment_status": "unavailable",
            "errors": [f"{context}: per-trial manifest must contain exactly one trial"],
        }
    trial_spec = trial_specs[0]
    errors.extend(_same_identity(trial_spec, trial))
    for field in ("condition", "profile", "repetition_id", "duration_seconds"):
        if trial_spec.get(field) != trial.get(field):
            errors.append(f"{context}: trial manifest {field} does not match campaign")
    if (
        trial_spec.get("duration_seconds") != TRIAL_MATRIX_DURATION_SECONDS
        or trial_manifest.get("offered_rate_mbps") != TRIAL_MATRIX_OFFERED_RATE_MBPS
    ):
        errors.append(f"{context}: trial manifest must declare 20 seconds and 4 Mbps per flow")
    flows = trial_spec.get("flows")
    if not isinstance(flows, list):
        errors.append(f"{context}: trial manifest flows must be an array")
        flows = []
    flow_entries = {
        entry.get("flow_id"): entry
        for entry in flows
        if isinstance(entry, Mapping) and isinstance(entry.get("flow_id"), str)
    }
    if len(flows) != len(EXPECTED_FLOWS) or set(flow_entries) != set(EXPECTED_FLOWS):
        errors.append(f"{context}: trial manifest must explicitly list all four required flows")
    for flow_id, port in EXPECTED_FLOWS.items():
        entry = flow_entries.get(flow_id)
        if entry is None or entry.get("port") != port:
            errors.append(f"{context}: flow {flow_id} must be mapped to UDP port {port}")
            continue
        evidence_path, evidence_path_error = _resolve_path(
            trial_manifest_path.parent,
            entry.get("evidence_path"),
            f"{context} flow {flow_id} evidence_path",
        )
        if evidence_path_error:
            errors.append(evidence_path_error)
        elif evidence_path is not None:
            try:
                evidence_path.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{context}: flow {flow_id} evidence escapes campaign directory")
    analyzer_report = analyze_manifest(trial_manifest_path)
    analyzed_trials = analyzer_report.get("trials", [])
    analyzed = next(
        (item for item in analyzed_trials if item.get("trial_id") == trial_id),
        None,
    )
    if analyzed is None:
        errors.append(f"{context}: analyzer did not return the campaign trial")
        analyzer_status = "failed"
        alignment_status = "unavailable"
        alignment_reason = "trial is absent from analyzer report"
    else:
        analyzer_status = analyzed.get("status", "failed")
        aggregate = analyzed.get("receiver_window_aggregate", {})
        alignment_status = (
            "ineligible"
            if isinstance(aggregate, Mapping) and aggregate.get("status") == "contract_valid"
            else "unavailable"
        )
        alignment_reason = aggregate.get("reason") if isinstance(aggregate, Mapping) else None
        if analyzer_status != "complete":
            errors.extend(
                f"{context}: analyzer: {message}"
                for message in analyzed.get("errors", [])
            )
    if analyzer_report.get("manifest_errors"):
        errors.extend(
            f"{context}: analyzer manifest: {message}"
            for message in analyzer_report["manifest_errors"]
        )

    status_document, status_errors = _validate_json_artifact(
        root,
        trial.get("trial_status_path"),
        f"{context} trial_status_path",
        trial,
        trial_directory,
    )
    errors.extend(status_errors)
    if status_document is not None:
        claimed_trial_status = status_document.get("status")
        normalized_status = (
            {
                "completed": "complete",
                "complete": "complete",
                "failed": "failed",
            }.get(claimed_trial_status)
            if isinstance(claimed_trial_status, str)
            else None
        )
        if normalized_status is None:
            errors.append(f"{context}: trial_status.json status must be completed or failed")
        elif normalized_status != analyzer_status:
            errors.append(f"{context}: trial_status.json disagrees with analyzer status")

    provenance, provenance_errors = _validate_json_artifact(
        root,
        trial.get("aggregate_provenance_path"),
        f"{context} aggregate_provenance_path",
        trial,
        trial_directory,
    )
    errors.extend(provenance_errors)
    if provenance is not None:
        window = trial_spec.get("declared_window")
        if not isinstance(window, Mapping):
            errors.append(f"{context}: declared_window is required for aggregate provenance")
        else:
            for field in ("clock_id", "start_seconds", "end_seconds"):
                if provenance.get(field) != window.get(field):
                    errors.append(
                        f"{context}: aggregate provenance {field} does not match declared_window"
                    )
        flow_ids = provenance.get("flow_ids")
        if (
            not isinstance(flow_ids, list)
            or len(flow_ids) != len(EXPECTED_FLOWS)
            or any(not isinstance(flow_id, str) for flow_id in flow_ids)
            or set(flow_ids) != set(EXPECTED_FLOWS)
        ):
            errors.append(f"{context}: aggregate provenance must identify all four flows")
        source_paths = provenance.get("source_evidence_paths")
        if not isinstance(source_paths, Mapping):
            errors.append(f"{context}: aggregate provenance source_evidence_paths is required")
        else:
            expected_paths = {
                flow_id: entry.get("evidence_path")
                for flow_id, entry in flow_entries.items()
                if isinstance(entry, Mapping)
            }
            if source_paths != expected_paths:
                errors.append(
                    f"{context}: aggregate provenance source paths do not match "
                    "the per-flow manifest"
                )
        if (
            not _is_finite_number(provenance.get("aggregate_throughput_mbps"))
            or provenance["aggregate_throughput_mbps"] < 0
        ):
            errors.append(
                f"{context}: aggregate provenance aggregate_throughput_mbps "
                "must be finite and nonnegative"
            )
        if not _nonempty_string(provenance.get("aggregation_method")):
            errors.append(f"{context}: aggregate provenance aggregation_method is required")

    raw_reports = trial.get("raw_reports")
    if not isinstance(raw_reports, list) or not raw_reports:
        errors.append(f"{context}: raw_reports must list at least one hashed report")
    else:
        for index, item in enumerate(raw_reports):
            label = f"{context} raw_reports[{index}]"
            if not isinstance(item, Mapping):
                errors.append(f"{label} must be an object")
                continue
            raw_path, raw_path_error = _resolve_path(root, item.get("path"), f"{label}.path")
            if raw_path_error:
                errors.append(raw_path_error)
                continue
            digest = item.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest
            ):
                errors.append(f"{label}.sha256 must be a lowercase SHA-256 digest")
                continue
            assert raw_path is not None
            try:
                raw_path.relative_to(trial_directory.resolve())
            except ValueError:
                errors.append(f"{label} must remain inside its trial evidence directory")
                continue
            if not raw_path.is_file():
                errors.append(f"{label} file is missing")
            else:
                try:
                    actual_digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
                except OSError as exc:
                    errors.append(f"{label} cannot be read: {exc}")
                else:
                    if actual_digest != digest:
                        errors.append(f"{label} SHA-256 does not match file contents")

    declared_window = trial_spec.get("declared_window")
    timestamp_errors = _validate_timestamps(trial, context)
    errors.extend(timestamp_errors)
    if timestamp_errors:
        alignment_status = "unavailable"
    timestamps = trial.get("timestamps")
    if isinstance(timestamps, Mapping):
        expected_clock = (
            declared_window.get("clock_id")
            if isinstance(declared_window, Mapping)
            else None
        )
        if timestamps.get("clock_id") != expected_clock:
            errors.append(
                f"{context}: timestamp clock_id does not match declared receiver window"
            )
            alignment_status = "unavailable"
        if (
            isinstance(declared_window, Mapping)
            and (
                timestamps.get("window_start_monotonic_seconds")
                != declared_window.get("start_seconds")
                or timestamps.get("window_finish_monotonic_seconds")
                != declared_window.get("end_seconds")
            )
        ):
            errors.append(f"{context}: monotonic timestamps do not match declared_window"            )
            alignment_status = "unavailable"

    tc_paths = trial.get("traffic_control")
    if not isinstance(tc_paths, Mapping):
        errors.append(f"{context}: traffic_control evidence object is required")
        tc_paths = {}
    for phase in ("before", "after"):
        snapshot, snapshot_errors = _validate_json_artifact(
            root,
            tc_paths.get(f"{phase}_snapshot_path"),
            f"{context} traffic_control.{phase}_snapshot_path",
            trial,
            trial_directory,
        )
        errors.extend(snapshot_errors)
        if snapshot is not None:
            for field in ("qdiscs", "classes", "filters"):
                if not isinstance(snapshot.get(field), list):
                    errors.append(
                        f"{context}: {phase} traffic-control snapshot {field} must be an array"
                    )
                elif field in {"qdiscs", "classes"} and not snapshot[field]:
                    errors.append(
                        f"{context}: {phase} traffic-control snapshot {field} "
                        "must not be empty"
                    )
            if _aware_datetime(snapshot.get("captured_at_utc")) is None:
                errors.append(
                    f"{context}: {phase} traffic-control snapshot captured_at_utc "
                    "must be timezone-aware ISO-8601"
                )
            else:
                capture_time = _aware_datetime(snapshot.get("captured_at_utc"))
                trial_timestamps = trial.get("timestamps")
                if isinstance(trial_timestamps, Mapping):
                    trial_start = _aware_datetime(trial_timestamps.get("trial_start_utc"))
                    trial_finish = _aware_datetime(trial_timestamps.get("trial_finish_utc"))
                    if (
                        phase == "before"
                        and trial_start is not None
                        and capture_time is not None
                        and capture_time >= trial_start
                    ):
                        errors.append(
                            f"{context}: before traffic-control snapshot must precede trial start"
                        )
                    if (
                        phase == "after"
                        and trial_finish is not None
                        and capture_time is not None
                        and capture_time <= trial_finish
                    ):
                        errors.append(
                            f"{context}: after traffic-control snapshot must follow trial finish"
                        )
    counters, counter_errors = _validate_json_artifact(
        root,
        tc_paths.get("counter_evidence_path"),
        f"{context} traffic_control.counter_evidence_path",
        trial,
        trial_directory,
    )
    errors.extend(counter_errors)
    if counters is not None:
        errors.extend(_validate_counter_evidence(counters, context))
        before_time = _aware_datetime(counters.get("before_captured_at_utc"))
        after_time = _aware_datetime(counters.get("after_captured_at_utc"))
        trial_timestamps = trial.get("timestamps")
        trial_start = _aware_datetime(trial_timestamps.get("trial_start_utc")) if isinstance(
            trial_timestamps, Mapping
        ) else None
        trial_finish = _aware_datetime(trial_timestamps.get("trial_finish_utc")) if isinstance(
            trial_timestamps, Mapping
        ) else None
        if before_time is None or trial_start is None or before_time >= trial_start:
            errors.append(
                f"{context}: counter before_captured_at_utc must precede trial start"
            )
        if after_time is None or trial_finish is None or after_time <= trial_finish:
            errors.append(
                f"{context}: counter after_captured_at_utc must follow trial finish"
            )

    for field, label in (
        ("connectivity_verification_path", "connectivity"),
        ("restoration_verification_path", "restoration"),
    ):
        document, artifact_errors = _validate_json_artifact(
            root,
            trial.get(field),
            f"{context} {field}",
            trial,
            trial_directory,
        )
        errors.extend(artifact_errors)
        if document is not None and document.get("status") != "verified":
            errors.append(f"{context}: {label} verification status must be verified")

    return {
        "trial_id": trial_id,
        "status": "complete" if not errors else "failed",
        "analyzer_status": analyzer_status,
        "alignment_status": alignment_status,
        "receiver_alignment_status": alignment_status,
        "sender_receiver_temporal_comparison": {
            "status": "unavailable",
            "reason": (
                "sender-to-receiver clock mapping is not implemented or "
                "independently validated"
            ),
        },
        "alignment_reason": alignment_reason,
        "errors": errors,
    }


def _validate_campaign_structure(manifest: Any) -> tuple[list[str], list[Mapping[str, Any]]]:
    errors: list[str] = []
    if not isinstance(manifest, Mapping):
        return ["campaign manifest root must be an object"], []
    if (
        isinstance(manifest.get("schema_version"), bool)
        or manifest.get("schema_version") != CAMPAIGN_SCHEMA_VERSION
    ):
        errors.append(f"schema_version must be {CAMPAIGN_SCHEMA_VERSION}")
    if not _nonempty_string(manifest.get("campaign_id")):
        errors.append("campaign_id must be a non-empty string")
    design = manifest.get("design")
    if not isinstance(design, Mapping):
        errors.append("design object is required")
    else:
        if design.get("id") != WILLIAMS_DESIGN:
            errors.append(f"design.id must be {WILLIAMS_DESIGN}")
        if (
            isinstance(design.get("version"), bool)
            or design.get("version") != WILLIAMS_DESIGN_VERSION
        ):
            errors.append(f"design.version must be {WILLIAMS_DESIGN_VERSION}")
        if design.get("strata") != STRATA:
            errors.append("design.strata must match the four approved condition/profile mappings")
        expected_sequences = {
            key: list(value) for key, value in SEQUENCES.items()
        }
        if design.get("sequences") != expected_sequences:
            errors.append("design.sequences do not match the approved WILLIAMS_4 definitions")
    if manifest.get("minimum_complete_cycles") != MINIMUM_COMPLETE_CYCLES:
        errors.append(f"minimum_complete_cycles must be {MINIMUM_COMPLETE_CYCLES}")
    if manifest.get("offered_rate_mbps_per_flow") != TRIAL_MATRIX_OFFERED_RATE_MBPS:
        errors.append("offered_rate_mbps_per_flow must be 4")
    if manifest.get("duration_seconds_per_flow") != TRIAL_MATRIX_DURATION_SECONDS:
        errors.append("duration_seconds_per_flow must be 20")
    trials_value = manifest.get("trials")
    if not isinstance(trials_value, list):
        errors.append("trials must be an array")
        return errors, []
    trials = [trial for trial in trials_value if isinstance(trial, Mapping)]
    if len(trials) != len(trials_value):
        errors.append("every campaign trial must be an object")
    if not trials:
        errors.append("campaign must contain trials")

    for field in ("trial_id", "repetition_id", "experiment_id"):
        values = [trial.get(field) for trial in trials]
        if any(not _nonempty_string(value) for value in values):
            errors.append(f"every trial requires a non-empty {field}")
        valid_values = [value for value in values if _nonempty_string(value)]
        duplicates = [
            value for value, count in Counter(valid_values).items() if count > 1
        ]
        if duplicates:
            errors.append(f"{field} values must be globally unique: {duplicates!r}")

    orders = [trial.get("execution_order") for trial in trials]
    if any(
        isinstance(order, bool) or not isinstance(order, int) or order < 1
        for order in orders
    ):
        errors.append("execution_order values must be positive integers")
    elif sorted(orders) != list(range(1, len(trials) + 1)):
        errors.append("execution_order values must be unique and contiguous from 1")

    cycle_groups: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for trial in trials:
        context = f"trial {trial.get('trial_id')!r}"
        if not isinstance(trial.get("stratum_id"), str) or trial["stratum_id"] not in STRATA:
            errors.append(f"{context}: stratum_id is not one of the four approved strata")
        else:
            expected = STRATA[trial["stratum_id"]]
            if trial.get("condition") != expected["condition"] or trial.get("profile") != expected["profile"]:
                errors.append(f"{context}: condition/profile does not match stratum_id")
        cycle = trial.get("cycle")
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
            errors.append(f"{context}: cycle must be a positive integer")
        else:
            cycle_groups[cycle].append(trial)
        if not isinstance(trial.get("sequence_id"), str) or trial["sequence_id"] not in SEQUENCES:
            errors.append(f"{context}: sequence_id must be A, B, C, or D")

    if cycle_groups:
        cycle_numbers = sorted(cycle_groups)
        if cycle_numbers != list(range(1, max(cycle_numbers) + 1)):
            errors.append("cycle numbers must be contiguous from 1")
        complete_cycle_count = 0
        expected_execution: list[tuple[int, str, str]] = []
        for cycle in cycle_numbers:
            group = cycle_groups[cycle]
            by_sequence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for trial in group:
                if (
                    isinstance(trial.get("sequence_id"), str)
                    and trial.get("sequence_id") in SEQUENCES
                ):
                    by_sequence[trial["sequence_id"]].append(trial)
            cycle_complete = set(by_sequence) == set(SEQUENCES) and all(
                len(by_sequence[sequence]) == 4 for sequence in SEQUENCES
            )
            if not cycle_complete:
                errors.append(f"cycle {cycle} must contain sequences A, B, C, and D once each")
            else:
                complete_cycle_count += 1
            for sequence, sequence_strata in SEQUENCES.items():
                block = by_sequence.get(sequence, [])
                block_trials = sorted(
                    block,
                    key=lambda item: (
                        item.get("execution_order")
                        if isinstance(item.get("execution_order"), int)
                        and not isinstance(item.get("execution_order"), bool)
                        else math.inf
                    ),
                )
                actual_strata = [item.get("stratum_id") for item in block_trials]
                if len(block) != 4 or actual_strata != list(sequence_strata):
                    errors.append(
                        f"cycle {cycle} sequence {sequence} must contain its four strata "
                        "exactly once in the approved sequence order"
                    )
                expected_execution.extend(
                    (cycle, sequence, stratum)
                    for stratum in sequence_strata
                )
        if complete_cycle_count < MINIMUM_COMPLETE_CYCLES:
            errors.append(
                f"campaign requires at least {MINIMUM_COMPLETE_CYCLES} complete "
                f"WILLIAMS_4 cycles (16 trials each)"
            )
        if all(
            isinstance(order, int) and not isinstance(order, bool)
            for order in orders
        ):
            ordered_trials = sorted(trials, key=lambda item: item["execution_order"])
            actual_execution = [
                (trial.get("cycle"), trial.get("sequence_id"), trial.get("stratum_id"))
                for trial in ordered_trials
            ]
            if actual_execution != expected_execution:
                errors.append(
                    "global execution_order must follow cycle order, A/B/C/D block "
                    "order, and each prescribed sequence"
                )
    else:
        errors.append("campaign has no valid cycles")

    claim = manifest.get("claimed_status")
    if claim not in ("complete", "incomplete"):
        errors.append("claimed_status must be complete or incomplete")
    return errors, trials


def _count_complete_cycles(trials: list[Mapping[str, Any]]) -> int:
    cycle_groups: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for trial in trials:
        cycle = trial.get("cycle")
        if isinstance(cycle, int) and not isinstance(cycle, bool) and cycle > 0:
            cycle_groups[cycle].append(trial)
    complete = 0
    for group in cycle_groups.values():
        by_sequence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for trial in group:
            sequence = trial.get("sequence_id")
            if isinstance(sequence, str) and sequence in SEQUENCES:
                by_sequence[sequence].append(trial)
        if set(by_sequence) != set(SEQUENCES):
            continue
        if all(
            len(by_sequence[sequence]) == 4
            and [
                trial.get("stratum_id")
                for trial in sorted(
                    by_sequence[sequence],
                    key=lambda item: (
                        item.get("execution_order")
                        if isinstance(item.get("execution_order"), int)
                        and not isinstance(item.get("execution_order"), bool)
                        else math.inf
                    ),
                )
            ] == list(SEQUENCES[sequence])
            for sequence in SEQUENCES
        ):
            complete += 1
    return complete


def _duplicate_trial_references(
    root: Path,
    trials: list[Mapping[str, Any]],
) -> dict[str, list[str]]:
    references: dict[Path, list[str]] = defaultdict(list)
    for trial in trials:
        trial_id = trial.get("trial_id")
        refs: list[tuple[Path, Any, str]] = []
        for field in (
            "trial_manifest_path",
            "trial_status_path",
            "aggregate_provenance_path",
            "connectivity_verification_path",
            "restoration_verification_path",
        ):
            refs.append((root, trial.get(field), field))
        traffic_control = trial.get("traffic_control")
        if isinstance(traffic_control, Mapping):
            for field, value in traffic_control.items():
                refs.append((root, value, f"traffic_control.{field}"))
        raw_reports = trial.get("raw_reports")
        if isinstance(raw_reports, list):
            for index, item in enumerate(raw_reports):
                if isinstance(item, Mapping):
                    refs.append((root, item.get("path"), f"raw_reports[{index}]"))
        for base, value, label in refs:
            path, error = _resolve_path(base, value, f"trial {trial_id!r} {label}")
            if path is not None and error is None:
                references[path].append(str(trial_id))
        trial_manifest, _ = _resolve_path(
            root,
            trial.get("trial_manifest_path"),
            f"trial {trial_id!r} trial_manifest_path",
        )
        if trial_manifest is None or not trial_manifest.is_file():
            continue
        try:
            document = _load_json(trial_manifest, "trial manifest")
        except ValueError:
            continue
        if not isinstance(document, Mapping):
            continue
        specs = document.get("trials")
        if not isinstance(specs, list) or len(specs) != 1 or not isinstance(specs[0], Mapping):
            continue
        spec = specs[0]
        flows = spec.get("flows")
        if isinstance(flows, list):
            for entry in flows:
                if isinstance(entry, Mapping):
                    path, error = _resolve_path(
                        trial_manifest.parent,
                        entry.get("evidence_path"),
                        f"trial {trial_id!r} flow evidence_path",
                    )
                    if path is not None and error is None:
                        references[path].append(str(trial_id))
        mapping = spec.get("clock_mapping")
        if isinstance(mapping, Mapping):
            path, error = _resolve_path(
                trial_manifest.parent,
                mapping.get("evidence_path"),
                f"trial {trial_id!r} clock mapping evidence_path",
            )
            if path is not None and error is None:
                references[path].append(str(trial_id))
    duplicates: dict[str, list[str]] = defaultdict(list)
    for path, trial_ids in references.items():
        if len(trial_ids) > 1:
            message = f"evidence path is shared or duplicated: {path}"
            for trial_id in set(trial_ids):
                duplicates[trial_id].append(message)
    return duplicates


def _execution_time_errors(
    trials: list[Mapping[str, Any]],
) -> dict[str, list[str]]:
    ordered = sorted(
        (
            trial for trial in trials
            if isinstance(trial.get("execution_order"), int)
            and not isinstance(trial.get("execution_order"), bool)
        ),
        key=lambda trial: trial["execution_order"],
    )
    errors: dict[str, list[str]] = defaultdict(list)
    previous_finish: datetime | None = None
    for trial in ordered:
        trial_id = str(trial.get("trial_id"))
        timestamps = trial.get("timestamps")
        start = _aware_datetime(timestamps.get("trial_start_utc")) if isinstance(
            timestamps, Mapping
        ) else None
        finish = _aware_datetime(timestamps.get("trial_finish_utc")) if isinstance(
            timestamps, Mapping
        ) else None
        if start is None or finish is None:
            continue
        if previous_finish is not None and start < previous_finish:
            errors[trial_id].append(
                "UTC trial timestamps overlap or contradict execution_order"
            )
        previous_finish = max(previous_finish, finish) if previous_finish else finish
    return errors


def analyze_campaign(manifest_path: str | Path) -> dict[str, Any]:
    """Validate campaign structure and explicitly referenced trial evidence offline."""
    manifest_file = Path(manifest_path).expanduser().resolve()
    try:
        manifest = _load_json(manifest_file, "campaign manifest")
    except ValueError as exc:
        return {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "status": "invalid_manifest",
            "campaign_status": "incomplete",
            "campaign_id": None,
            "manifest_path": str(manifest_file),
            "campaign_errors": [str(exc)],
            "trials": [],
            "comparison_ready": False,
            "live_operations_supported": False,
            "live_readiness": "not_established",
        }

    structural_errors, trial_specs = _validate_campaign_structure(manifest)
    cycle_count = _count_complete_cycles(trial_specs)
    campaign_structure_status = (
        "complete" if not structural_errors else "incomplete"
    )
    trial_reports = [
        _validate_trial_evidence(manifest_file.parent, trial)
        for trial in trial_specs
    ]
    duplicate_references = _duplicate_trial_references(
        manifest_file.parent,
        trial_specs,
    )
    for trial_report in trial_reports:
        duplicate_errors = duplicate_references.get(str(trial_report.get("trial_id")), [])
        if duplicate_errors:
            trial_report["errors"].extend(duplicate_errors)
            trial_report["status"] = "failed"
        time_errors = _execution_time_errors(trial_specs).get(
            str(trial_report.get("trial_id")),
            [],
        )
        if time_errors:
            trial_report["errors"].extend(time_errors)
            trial_report["status"] = "failed"
    evidence_errors = [
        f"trial {trial.get('trial_id')!r}: {error}"
        for trial in trial_reports
        for error in trial["errors"]
    ]
    computed_status = (
        "complete"
        if campaign_structure_status == "complete"
        and all(trial.get("status") == "complete" for trial in trial_reports)
        else "incomplete"
    )
    campaign_errors = list(structural_errors) + evidence_errors
    if isinstance(manifest, Mapping) and manifest.get("claimed_status") != computed_status:
        campaign_errors.append(
            f"claimed_status {manifest.get('claimed_status')!r} does not match "
            f"computed status {computed_status!r}"
        )
        computed_status = "incomplete"
    alignment_complete = bool(trial_reports) and all(
        trial.get("alignment_status") == "eligible" for trial in trial_reports
    )
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_id": manifest.get("campaign_id") if isinstance(manifest, Mapping) else None,
        "manifest_path": str(manifest_file),
        "status": computed_status,
        "campaign_status": campaign_structure_status,
        "complete_cycle_count": cycle_count,
        "minimum_complete_cycles": MINIMUM_COMPLETE_CYCLES,
        "alignment_status": "eligible" if alignment_complete else "blocked",
        "receiver_alignment_status": (
            "eligible" if alignment_complete else "blocked"
        ),
        "measurement_alignment_eligibility": (
            "eligible" if alignment_complete else "ineligible"
        ),
        "sender_receiver_temporal_comparison": {
            "status": "unavailable",
            "reason": (
                "sender-to-receiver clock mapping is not implemented or "
                "independently validated"
            ),
        },
        "comparison_ready": computed_status == "complete" and alignment_complete,
        "live_operations_supported": False,
        "live_readiness": "not_established",
        "campaign_errors": campaign_errors,
        "trials": trial_reports,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an explicitly manifested Adaptive QoS campaign offline."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    report = analyze_campaign(args.manifest)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
