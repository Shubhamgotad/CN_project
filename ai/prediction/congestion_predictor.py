"""Runtime congestion prediction using a previously trained Random Forest."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pandas as pd

from .features import extract_features
from .telemetry import TelemetryRecord, validate_telemetry
from .training import load_model


class CongestionPredictor:
    def __init__(self, model_path: str | Path):
        self._artifact = load_model(model_path)
        self._model = self._artifact["model"]

    def predict(
        self, record: TelemetryRecord | Mapping[str, object]
    ) -> dict[str, float | str]:
        validated = record if isinstance(record, TelemetryRecord) else validate_telemetry(record)
        features = pd.DataFrame([extract_features(validated)], columns=self._artifact["feature_columns"])
        probabilities = self._model.predict_proba(features)[0]
        index = int(probabilities.argmax())
        return {
            "congestion": str(self._model.classes_[index]),
            "confidence": max(0.0, min(1.0, float(probabilities[index]))),
        }


def predict(
    model_path: str | Path, record: TelemetryRecord | Mapping[str, object]
) -> dict[str, float | str]:
    """Load an existing model and predict one validated telemetry record."""
    return CongestionPredictor(model_path).predict(record)