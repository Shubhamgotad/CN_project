"""Deterministic, configurable congestion labeling rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .telemetry import TelemetryRecord, validate_telemetry


@dataclass(frozen=True)
class CongestionThresholds:
    utilization_medium: float = 60.0
    utilization_high: float = 80.0
    latency_medium_ms: float = 50.0
    latency_high_ms: float = 100.0
    packet_loss_medium_pct: float = 0.5
    packet_loss_high_pct: float = 2.0


DEFAULT_THRESHOLDS = CongestionThresholds()


def congestion_score(
    record: TelemetryRecord | Mapping[str, object],
    thresholds: CongestionThresholds = DEFAULT_THRESHOLDS,
) -> int:
    """Return a transparent severity score from utilization, latency, and loss."""
    telemetry = record if isinstance(record, TelemetryRecord) else validate_telemetry(record)
    score = 0
    score += 2 if telemetry.utilization >= thresholds.utilization_high else (
        1 if telemetry.utilization >= thresholds.utilization_medium else 0
    )
    score += 2 if telemetry.latency_ms >= thresholds.latency_high_ms else (
        1 if telemetry.latency_ms >= thresholds.latency_medium_ms else 0
    )
    score += 2 if telemetry.packet_loss_pct >= thresholds.packet_loss_high_pct else (
        1 if telemetry.packet_loss_pct >= thresholds.packet_loss_medium_pct else 0
    )
    return score


def label_congestion(
    record: TelemetryRecord | Mapping[str, object],
    thresholds: CongestionThresholds = DEFAULT_THRESHOLDS,
) -> str:
    score = congestion_score(record, thresholds)
    if score >= 4:
        return "HIGH"
    if score >= 2:
        return "MEDIUM"
    return "LOW"
