"""Construction of supervised training datasets from telemetry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

import pandas as pd

from .features import FEATURE_COLUMNS, records_to_features
from .labeling import CongestionThresholds, DEFAULT_THRESHOLDS, label_congestion
from .telemetry import TelemetryRecord, load_telemetry_json, validate_telemetry


def create_dataset(
    records: Iterable[TelemetryRecord | Mapping[str, object]] | str | Path,
    thresholds: CongestionThresholds = DEFAULT_THRESHOLDS,
) -> pd.DataFrame:
    if isinstance(records, (str, Path)):
        records = load_telemetry_json(records)
    validated = [
        record if isinstance(record, TelemetryRecord) else validate_telemetry(record)
        for record in records
    ]
    features = records_to_features(validated)
    features["congestion"] = [
        label_congestion(record, thresholds) for record in validated
    ]
    return features


def split_features_and_labels(dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    missing = [column for column in (*FEATURE_COLUMNS, "congestion") if column not in dataset]
    if missing:
        raise ValueError("dataset is missing columns: " + ", ".join(missing))
    return dataset.loc[:, FEATURE_COLUMNS], dataset["congestion"]
