"""Telemetry-based congestion prediction pipeline."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .congestion_predictor import CongestionPredictor, predict
    from .dataset import create_dataset, load_person1_dataset
    from .features import FEATURE_COLUMNS, extract_features
    from .labeling import CongestionThresholds, congestion_score, label_congestion
    from .telemetry import TelemetryRecord, load_telemetry_json, validate_telemetry
    from .training import load_model, train_candidate_model, train_random_forest

_EXPORTS = {
    "CongestionPredictor": "congestion_predictor",
    "CongestionThresholds": "labeling",
    "FEATURE_COLUMNS": "features",
    "TelemetryRecord": "telemetry",
    "congestion_score": "labeling",
    "create_dataset": "dataset",
    "extract_features": "features",
    "label_congestion": "labeling",
    "load_person1_dataset": "dataset",
    "load_model": "training",
    "load_telemetry_json": "telemetry",
    "predict": "congestion_predictor",
    "train_candidate_model": "training",
    "train_random_forest": "training",
    "validate_telemetry": "telemetry",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value
