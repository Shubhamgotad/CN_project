"""Congestion-aware QoS policy selection."""

from __future__ import annotations

from collections.abc import Mapping
import math
from types import MappingProxyType
from typing import Any

TRAFFIC_CLASSES = ("gaming", "video", "web", "bulk")
CONGESTION_STATES = ("LOW", "MEDIUM", "HIGH")

POLICY_PERCENTAGES = MappingProxyType({
    "LOW": MappingProxyType({"gaming": 30.0, "video": 30.0, "web": 25.0, "bulk": 15.0}),
    "MEDIUM": MappingProxyType({"gaming": 40.0, "video": 30.0, "web": 20.0, "bulk": 10.0}),
    "HIGH": MappingProxyType({"gaming": 50.0, "video": 25.0, "web": 15.0, "bulk": 10.0}),
})


def validate_policy(policy: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(policy, Mapping):
        raise ValueError("policy must be a mapping")
    if set(policy) != set(TRAFFIC_CLASSES):
        raise ValueError("policy must contain exactly: " + ", ".join(TRAFFIC_CLASSES))
    result = {}
    for traffic_class in TRAFFIC_CLASSES:
        value = policy[traffic_class]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"policy percentage for {traffic_class} must be numeric")
        if not math.isfinite(value):
            raise ValueError(f"policy percentage for {traffic_class} must be finite")
        if value < 0 or value > 100:
            raise ValueError(f"policy percentage for {traffic_class} must be between 0 and 100")
        result[traffic_class] = float(value)
    if sum(result.values()) != 100.0:
        raise ValueError("policy percentages must sum to exactly 100")
    return result


def get_policy(congestion: str, custom_policies: Mapping[str, Mapping[str, float]] | None = None) -> dict[str, float]:
    policies = custom_policies if custom_policies is not None else POLICY_PERCENTAGES
    if congestion not in policies:
        raise ValueError(f"invalid congestion state: {congestion!r}")
    return validate_policy(policies[congestion])


def build_qos_decision(
    congestion: str,
    total_bandwidth_mbps: float,
    custom_policies: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    from .allocator import allocate_bandwidth

    policy = get_policy(congestion, custom_policies)
    allocation = allocate_bandwidth(total_bandwidth_mbps, policy=policy)
    return {
        "congestion": congestion,
        "policy": policy,
        "allocation_mbps": allocation,
    }


def build_qos_decision_from_prediction(
    prediction: Mapping[str, Any],
    total_bandwidth_mbps: float,
    custom_policies: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    if "congestion" not in prediction:
        raise ValueError("prediction must contain a congestion state")
    return build_qos_decision(
        prediction["congestion"], total_bandwidth_mbps, custom_policies
    )
