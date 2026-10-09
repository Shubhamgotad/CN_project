"""Reusable validation for QoS decisions and tc configuration."""

from __future__ import annotations

from collections.abc import Mapping
import math
import re

from .policy import CONGESTION_STATES, TRAFFIC_CLASSES, validate_policy


def validate_congestion_state(state: str) -> str:
    if state not in CONGESTION_STATES:
        raise ValueError(f"invalid congestion state: {state!r}")
    return state


def validate_total_bandwidth(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("total bandwidth must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("total bandwidth must be greater than zero")
    return value


def validate_allocations(
    allocations: Mapping[str, float], total_bandwidth_mbps: float
) -> dict[str, float]:
    """Validate allocations; unused capacity is intentionally allowed."""
    total = validate_total_bandwidth(total_bandwidth_mbps)
    if set(allocations) != set(TRAFFIC_CLASSES):
        raise ValueError("allocations must contain exactly: " + ", ".join(TRAFFIC_CLASSES))
    result = {}
    for traffic_class in TRAFFIC_CLASSES:
        value = allocations[traffic_class]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"allocation for {traffic_class} must be numeric")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"allocation for {traffic_class} cannot be negative")
        result[traffic_class] = value
    if sum(result.values()) > total + 1e-9:
        raise ValueError("allocations cannot exceed total bandwidth")
    return result


def validate_interface(interface: str) -> str:
    if not isinstance(interface, str) or not interface.strip():
        raise ValueError("interface name cannot be empty")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", interface):
        raise ValueError("interface name contains unsupported characters")
    return interface


def validate_tc_configuration(
    interface: str,
    total_bandwidth_mbps: float,
    allocations: Mapping[str, float],
) -> None:
    validate_interface(interface)
    validate_allocations(allocations, total_bandwidth_mbps)
