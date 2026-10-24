"""Fail-closed adapter for instrumented iperf3 receiver interval edges.

The adapter never derives absolute boundaries from iperf-relative offsets or
collector callback time. The patched producer's self-reported capability and
alignment statuses are not trusted; this code always leaves those gates closed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping


_BOUNDARY_KEYS = {
    "receiver_interval_schema_version",
    "boundary_source",
    "clock_domain",
    "clock_id",
    "clock_unit",
    "clock_semantics",
    "absolute_start_seconds",
    "absolute_end_seconds",
    "boundary_semantics",
    "start_boundary_uncertainty_seconds",
    "end_boundary_uncertainty_seconds",
    "boundary_uncertainty_evidence",
    "host_id",
    "boot_id",
    "producer_name",
    "producer_version",
    "producer_build_identity",
    "producer_binary_sha256",
    "producer_process_instance_id",
    "producer_pid",
    "producer_process_start_ticks",
}

_IDENTITY_FIELDS = (
    "clock_id",
    "host_id",
    "boot_id",
    "producer_name",
    "producer_version",
    "producer_build_identity",
    "producer_binary_sha256",
    "producer_process_instance_id",
    "producer_pid",
    "producer_process_start_ticks",
)


def _finite_number(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def receiver_boundary_fields(streams) -> dict | None:
    """Extract one producer boundary object, or return ``None`` for stock data.

    The current Traffic Lab creates one iperf stream per producer process. A
    parallel stream array is rejected because its boundaries cannot safely be
    attributed to one configured flow by this adapter.
    """
    if not isinstance(streams, list):
        return None
    boundary_streams = [
        stream for stream in streams
        if isinstance(stream, Mapping) and _BOUNDARY_KEYS.intersection(stream)
    ]
    if not boundary_streams:
        return None
    if len(boundary_streams) != 1 or len(streams) != 1:
        raise ValueError("receiver boundary output must identify exactly one stream")

    stream = boundary_streams[0]
    missing = sorted(_BOUNDARY_KEYS.difference(stream))
    if missing:
        raise ValueError(f"receiver boundary output is missing fields: {missing}")
    if stream.get("sender") is not False:
        raise ValueError("receiver boundary output must be from the receiver stream")
    if stream["receiver_interval_schema_version"] != 1 or isinstance(
        stream["receiver_interval_schema_version"], bool
    ):
        raise ValueError("receiver boundary schema version is unsupported")
    if stream["boundary_source"] != "receiver_measurement":
        raise ValueError("receiver boundary source is unsupported")
    if stream["clock_domain"] != "receiver_monotonic":
        raise ValueError("receiver boundary clock domain is unsupported")
    if stream["clock_unit"] != "seconds":
        raise ValueError("receiver boundary clock unit must be seconds")
    if stream["boundary_semantics"] != (
        "[start,end); iperf receiver interval-accounting edges"
    ):
        raise ValueError("receiver boundary semantics are unsupported")
    if not isinstance(stream["clock_semantics"], str) or not stream["clock_semantics"].strip():
        raise ValueError("receiver boundary clock semantics are required")

    for field in _IDENTITY_FIELDS:
        value = stream[field]
        if field == "producer_pid":
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("receiver boundary producer_pid must be a positive integer")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"receiver boundary {field} is required")

    for field in ("absolute_start_seconds", "absolute_end_seconds"):
        if not _finite_number(stream[field]):
            raise ValueError(f"receiver boundary {field} must be finite")
    if stream["absolute_end_seconds"] <= stream["absolute_start_seconds"]:
        raise ValueError("receiver boundary end must be after start")
    binary_digest = stream["producer_binary_sha256"]
    if (
        not isinstance(binary_digest, str)
        or len(binary_digest) != 64
        or any(char not in "0123456789abcdef" for char in binary_digest)
    ):
        raise ValueError("receiver boundary producer_binary_sha256 must be lowercase SHA-256")

    evidence = stream["boundary_uncertainty_evidence"]
    if not isinstance(evidence, Mapping) or not {
        "method", "evidence_path", "sha256"
    }.issubset(evidence):
        raise ValueError("receiver boundary uncertainty evidence must include method, evidence_path, and sha256")

    uncertainty_values = (
        stream["start_boundary_uncertainty_seconds"],
        stream["end_boundary_uncertainty_seconds"],
    )
    evidence_values = (evidence["method"], evidence["evidence_path"], evidence["sha256"])
    complete_uncertainty = all(value is not None for value in uncertainty_values + evidence_values)
    if complete_uncertainty:
        for field, value in zip(
            ("start_boundary_uncertainty_seconds", "end_boundary_uncertainty_seconds"),
            uncertainty_values,
        ):
            if not _finite_number(value) or value < 0:
                raise ValueError(f"receiver boundary {field} must be finite and nonnegative")
        method, evidence_path, digest = evidence_values
        if not isinstance(method, str) or not method.strip():
            raise ValueError("receiver boundary uncertainty method is required")
        if not isinstance(evidence_path, str) or not evidence_path.strip():
            raise ValueError("receiver boundary uncertainty evidence_path is required")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("receiver boundary uncertainty sha256 must be lowercase SHA-256")

    # Copy only documented contract fields. Producer-provided status strings
    # cannot upgrade independent validation or comparison eligibility.
    result = {key: stream[key] for key in _BOUNDARY_KEYS}
    result["boundary_metadata_status"] = (
        "schema_valid" if complete_uncertainty else "invalid_or_incomplete"
    )
    result["boundary_evidence_integrity_status"] = "not_checked"
    result["producer_capability_validation_status"] = "not_independently_validated"
    result["clock_validation_status"] = "not_independently_validated"
    result["measurement_alignment_eligibility"] = "ineligible"
    return result

