"""Deterministic bandwidth allocation from a QoS policy."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .policy import get_policy, validate_policy
from .tc_controller import (
    MANAGED_FILTER_PRIORITIES,
    ROOT_INTERFACE,
    ManagedHTBConfig,
)
from .validator import validate_total_bandwidth


MANAGED_HTB_PROFILES = MappingProxyType({
    "LOW": MappingProxyType({
        "gaming": 2.70, "video": 2.70, "web": 2.25, "bulk": 1.35,
    }),
    "MEDIUM": MappingProxyType({
        "gaming": 3.60, "video": 2.70, "web": 1.80, "bulk": 0.90,
    }),
    "HIGH": MappingProxyType({
        "gaming": 4.50, "video": 2.25, "web": 1.35, "bulk": 0.90,
    }),
})


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


def plan_managed_htb(
    congestion: str,
    *,
    interface: str = ROOT_INTERFACE,
) -> ManagedHTBConfig:
    """Plan one approved five-leaf profile without changing standalone allocation."""
    if not isinstance(congestion, str) or congestion not in MANAGED_HTB_PROFILES:
        raise ValueError(f"invalid congestion state: {congestion!r}")
    return ManagedHTBConfig(
        interface=interface,
        total_bandwidth_mbps=10.0,
        service_allocations_mbps=MANAGED_HTB_PROFILES[congestion],
        filter_priorities=MANAGED_FILTER_PRIORITIES,
    )
