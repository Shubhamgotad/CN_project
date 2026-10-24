"""Validation and aggregation of explicitly aligned receiver interval data."""

from __future__ import annotations

import math
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path


RECEIVER_INTERVAL_SCHEMA_VERSION = 1


def _is_finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_receiver_interval_record(row: Mapping) -> None:
    """Validate versioned evidence for actual receiver-domain interval edges."""
    schema_version = row.get("receiver_interval_schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RECEIVER_INTERVAL_SCHEMA_VERSION
    ):
        raise ValueError("receiver interval schema version is missing or unsupported")
    if row.get("boundary_source") != "receiver_measurement":
        raise ValueError("receiver interval boundaries must come from measurement instrumentation")
    if row.get("clock_domain") != "receiver_monotonic":
        raise ValueError("receiver interval clock_domain must be receiver_monotonic")
    if not isinstance(row.get("clock_id"), str) or not row["clock_id"].strip():
        raise ValueError("receiver interval clock_id is required")
    if not isinstance(row.get("host_id"), str) or not row["host_id"].strip():
        raise ValueError("receiver interval host_id is required")
    boot_id = row.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id.strip():
        raise ValueError("receiver interval boot_id is required")
    for field in (
        "clock_unit",
        "clock_semantics",
        "boundary_semantics",
        "producer_name",
        "producer_version",
        "producer_build_identity",
        "producer_binary_sha256",
        "producer_process_instance_id",
        "producer_process_start_ticks",
    ):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"receiver interval {field} is required")
    pid = row.get("producer_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("receiver interval producer_pid must be a positive integer")
    binary_digest = row.get("producer_binary_sha256")
    if (
        not isinstance(binary_digest, str)
        or len(binary_digest) != 64
        or any(character not in "0123456789abcdef" for character in binary_digest)
    ):
        raise ValueError("receiver interval producer_binary_sha256 must be lowercase SHA-256")
    if row.get("clock_unit") != "seconds":
        raise ValueError("receiver interval clock_unit must be seconds")
    if row.get("boundary_metadata_status") != "schema_valid":
        raise ValueError("receiver interval boundary metadata status is not schema_valid")
    if row.get("boundary_evidence_integrity_status") != "not_checked":
        raise ValueError("receiver interval boundary evidence integrity must be not_checked")
    if row.get("producer_capability_validation_status") != "not_independently_validated":
        raise ValueError("receiver interval producer capability must remain unvalidated")
    if row.get("clock_validation_status") != "not_independently_validated":
        raise ValueError("receiver interval clock must remain independently unvalidated")
    if row.get("measurement_alignment_eligibility") != "ineligible":
        raise ValueError("receiver interval alignment must remain ineligible")
    for field in (
        "absolute_start_seconds",
        "absolute_end_seconds",
        "start_boundary_uncertainty_seconds",
        "end_boundary_uncertainty_seconds",
    ):
        if not _is_finite_number(row.get(field)):
            raise ValueError(f"receiver interval {field} must be finite")
    if row["absolute_end_seconds"] <= row["absolute_start_seconds"]:
        raise ValueError("receiver interval end must be after start")
    if (
        row["start_boundary_uncertainty_seconds"] < 0
        or row["end_boundary_uncertainty_seconds"] < 0
    ):
        raise ValueError("receiver interval boundary uncertainty must be nonnegative")
    evidence = row.get("boundary_uncertainty_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("receiver interval boundary uncertainty evidence is required")
    for field in ("method", "evidence_path"):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError(
                f"receiver interval boundary uncertainty evidence {field} is required"
            )
    digest = evidence.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(
            "receiver interval boundary uncertainty evidence sha256 must be lowercase SHA-256"
        )
    for field in (
        "receiver_observation_monotonic_seconds",
        "receiver_observation_timestamp",
    ):
        if field in row and row[field] is not None:
            if field.endswith("_seconds") and not _is_finite_number(row[field]):
                raise ValueError(f"{field} must be finite when present")
            if field.endswith("_timestamp") and not isinstance(row[field], str):
                raise ValueError(f"{field} must be a string when present")


def synchronized_receiver_aggregate(
    intervals: Iterable[Mapping], expected_flows: Sequence[str]
) -> dict:
    """Return receiver aggregate over the exact shared time window.

    Every record must satisfy the versioned receiver-domain interval contract.
    Relative iperf offsets and callback observation times are deliberately
    insufficient. Throughput is treated as the mean rate for its interval;
    only the interior guaranteed by each boundary's uncertainty is aggregated.
    """
    expected = tuple(expected_flows)
    if not expected or len(set(expected)) != len(expected):
        raise ValueError("expected_flows must be a non-empty unique sequence")
    grouped: dict[str, list[tuple[float, float, float]]] = {flow: [] for flow in expected}
    clock_ids = set()
    host_ids = set()
    boot_ids = set()
    seen = set()
    for row in intervals:
        if not isinstance(row, Mapping):
            raise ValueError("receiver interval must be an object")
        flow = row.get("flow_id")
        if flow not in grouped:
            raise ValueError(f"unexpected flow {flow!r}")
        if row.get("measurement_direction") != "receiver":
            raise ValueError(f"flow {flow} interval is not receiver-side")
        try:
            validate_receiver_interval_record(row)
        except ValueError as exc:
            raise ValueError(f"flow {flow}: {exc}") from exc
        clock_ids.add(row["clock_id"])
        host_ids.add(row["host_id"])
        boot_ids.add(row.get("boot_id"))
        start = row["absolute_start_seconds"]
        end = row["absolute_end_seconds"]
        rate = row.get("throughput_mbps")
        if not _is_finite_number(rate) or rate < 0:
            raise ValueError(f"flow {flow} interval throughput must be finite and nonnegative")
        identity = (flow, start, end)
        if identity in seen:
            raise ValueError(f"duplicate interval for flow {flow}: {start}-{end}")
        seen.add(identity)
        guaranteed_start = start + row["start_boundary_uncertainty_seconds"]
        guaranteed_end = end - row["end_boundary_uncertainty_seconds"]
        if guaranteed_end <= guaranteed_start:
            raise ValueError(
                f"flow {flow} interval has no guaranteed coverage after boundary uncertainty"
            )
        grouped[flow].append((guaranteed_start, guaranteed_end, float(rate)))

    if len(clock_ids) != 1 or len(host_ids) != 1 or len(boot_ids) != 1:
        raise ValueError("receiver intervals do not share clock, host, and boot identities")
    missing = [flow for flow, rows in grouped.items() if not rows]
    if missing:
        raise ValueError(f"incomplete run; missing receiver flows: {missing}")
    for flow, rows in grouped.items():
        rows.sort()
        for previous, current in zip(rows, rows[1:]):
            if current[0] < previous[1] - 1e-9:
                raise ValueError(f"overlapping/duplicated intervals for flow {flow}")

    window_start = max(rows[0][0] for rows in grouped.values())
    window_end = min(rows[-1][1] for rows in grouped.values())
    if window_end <= window_start:
        raise ValueError("receiver flows have no shared measurement window")
    boundaries = {window_start, window_end}
    for rows in grouped.values():
        for start, end, _ in rows:
            if window_start < start < window_end:
                boundaries.add(start)
            if window_start < end < window_end:
                boundaries.add(end)
    ordered = sorted(boundaries)
    aggregate_bit_seconds = 0.0
    per_flow_bit_seconds = {flow: 0.0 for flow in expected}
    for left, right in zip(ordered, ordered[1:]):
        midpoint = (left + right) / 2
        for flow, rows in grouped.items():
            matches = [rate for start, end, rate in rows if start <= midpoint < end]
            if len(matches) != 1:
                raise ValueError(f"flow {flow} has a gap in the shared receiver window")
            bit_seconds = matches[0] * (right - left)
            per_flow_bit_seconds[flow] += bit_seconds
            aggregate_bit_seconds += bit_seconds
    duration = window_end - window_start
    return {
        "status": "contract_valid",
        "boundary_metadata_status": "schema_valid",
        "boundary_evidence_integrity_status": "not_checked",
        "producer_provenance_status": "not_independently_validated",
        "clock_mapping_status": "not_independently_validated",
        "measurement_alignment_eligibility": "ineligible",
        "clock_id": next(iter(clock_ids)),
        "host_id": next(iter(host_ids)),
        "boot_id": next(iter(boot_ids)),
        "window_start_seconds": window_start,
        "window_end_seconds": window_end,
        "window_duration_seconds": duration,
        "aggregate_throughput_mbps": aggregate_bit_seconds / duration,
        "per_flow_average_mbps": {
            flow: value / duration for flow, value in per_flow_bit_seconds.items()
        },
    }


def validate_run_manifest(metadata: Mapping, expected_flows: Mapping[str, int]) -> None:
    """Fail closed when run metadata is malformed or has missing/duplicate flows."""
    if not isinstance(metadata, Mapping):
        raise ValueError("run metadata must be an object")
    offered_rate = metadata.get("offered_rate_mbps")
    if (
        isinstance(offered_rate, bool)
        or not isinstance(offered_rate, (int, float))
        or not _is_finite_number(offered_rate)
        or offered_rate <= 0
    ):
        raise ValueError("run metadata offered_rate_mbps is malformed")
    rows = metadata.get("flows")
    if not isinstance(rows, list):
        raise ValueError("run metadata flows must be an array")
    found = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("run metadata flow entry must be an object")
        host, port = row.get("host"), row.get("port")
        if host not in expected_flows or expected_flows[host] != port:
            raise ValueError(f"unexpected or malformed flow entry: {row!r}")
        if host in found:
            raise ValueError(f"duplicate flow entry: {host}")
        rate = row.get("rate_mbps")
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or not math.isfinite(rate)
            or rate != offered_rate
        ):
            raise ValueError(f"flow {host} offered rate disagrees with metadata")
        found[host] = row
    missing = set(expected_flows) - set(found)
    if missing:
        raise ValueError(f"run metadata is incomplete; missing flows: {sorted(missing)}")
    duration = metadata.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise ValueError("run metadata duration_seconds is malformed")


def load_run_manifest(path: str | Path, expected_flows: Mapping[str, int]) -> dict:
    """Load and validate JSON metadata without repairing malformed evidence."""
    try:
        metadata = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"run metadata is unreadable or invalid JSON: {exc}") from exc
    validate_run_manifest(metadata, expected_flows)
    return metadata
