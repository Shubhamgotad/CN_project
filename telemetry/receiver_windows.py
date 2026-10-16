"""Validation and aggregation of explicitly aligned receiver interval data."""

from __future__ import annotations

import math
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path


def synchronized_receiver_aggregate(
    intervals: Iterable[Mapping], expected_flows: Sequence[str]
) -> dict:
    """Return receiver aggregate over the exact shared time window.

    Every record must carry a common clock_id and absolute receiver window.
    Relative iperf offsets and callback observation times are deliberately
    insufficient. Throughput is treated as the mean rate for its interval.
    """
    expected = tuple(expected_flows)
    if not expected or len(set(expected)) != len(expected):
        raise ValueError("expected_flows must be a non-empty unique sequence")
    grouped: dict[str, list[tuple[float, float, float]]] = {flow: [] for flow in expected}
    clock_ids = set()
    seen = set()
    for row in intervals:
        if not isinstance(row, Mapping):
            raise ValueError("receiver interval must be an object")
        flow = row.get("flow_id")
        if flow not in grouped:
            raise ValueError(f"unexpected flow {flow!r}")
        if row.get("measurement_direction") != "receiver":
            raise ValueError(f"flow {flow} interval is not receiver-side")
        clock_id = row.get("clock_id")
        if not isinstance(clock_id, str) or not clock_id:
            raise ValueError(f"flow {flow} interval is missing clock_id")
        clock_ids.add(clock_id)
        start, end, rate = (
            row.get("absolute_start_seconds"),
            row.get("absolute_end_seconds"),
            row.get("throughput_mbps"),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (start, end, rate)
        ):
            raise ValueError(f"flow {flow} interval has malformed timing or throughput")
        if end <= start or rate < 0:
            raise ValueError(f"flow {flow} interval has invalid boundaries or throughput")
        identity = (flow, start, end)
        if identity in seen:
            raise ValueError(f"duplicate interval for flow {flow}: {start}-{end}")
        seen.add(identity)
        grouped[flow].append((float(start), float(end), float(rate)))

    if len(clock_ids) != 1:
        raise ValueError("receiver windows do not share one verified clock_id")
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
        "clock_id": next(iter(clock_ids)),
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
        or not math.isfinite(offered_rate)
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
