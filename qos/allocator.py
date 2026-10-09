"""Deterministic bandwidth allocation from a QoS policy."""

from __future__ import annotations

from collections.abc import Mapping

from .policy import get_policy, validate_policy
from .validator import validate_total_bandwidth


def allocate_bandwidth(
    total_bandwidth_mbps: float,
    congestion: str | None = None,
    policy: Mapping[str, float] | None = None,
) -> dict[str, float]:
    if (congestion is None) == (policy is None):
        raise ValueError("provide exactly one of congestion or policy")
    total = validate_total_bandwidth(total_bandwidth_mbps)
    selected = get_policy(congestion) if congestion is not None else validate_policy(policy)
    allocation = {
        traffic_class: total * percentage / 100.0
        for traffic_class, percentage in selected.items()
    }
    difference = total - sum(allocation.values())
    allocation["bulk"] += difference
    return allocation
