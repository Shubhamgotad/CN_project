"""Shared feature extraction for training and runtime prediction."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .telemetry import TelemetryRecord, validate_telemetry


FEATURE_COLUMNS = (
    "utilization",
    "throughput_mbps",
    "latency_ms",
    "jitter_ms",
    "packet_loss_pct",
    "active_flows",
)


def extract_features(record: TelemetryRecord | Mapping[str, object]) -> list[float]:
    """Return features in the one canonical order used by the model."""
    validated = record if isinstance(record, TelemetryRecord) else validate_telemetry(record)
    return [float(getattr(validated, field)) for field in FEATURE_COLUMNS]


def records_to_features(records: list[TelemetryRecord]) -> pd.DataFrame:
    return pd.DataFrame(
        [extract_features(record) for record in records],
        columns=FEATURE_COLUMNS,
    )
