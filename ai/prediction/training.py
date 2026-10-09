"""Random Forest training, evaluation, and model persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split

from .dataset import create_dataset, split_features_and_labels
from .features import FEATURE_COLUMNS
from .labeling import CongestionThresholds, DEFAULT_THRESHOLDS
from .telemetry import TelemetryRecord


def train_random_forest(
    records: Iterable[TelemetryRecord | Mapping[str, object]],
    model_path: str | Path,
    *,
    test_size: float = 0.2,
    random_state: int = 42,
    thresholds: CongestionThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, object]:
    dataset = create_dataset(records, thresholds)
    features, labels = split_features_and_labels(dataset)
    class_count = labels.nunique()
    if class_count < 2:
        raise ValueError("training requires at least two congestion classes")
    if isinstance(test_size, float):
        test_size = max(test_size, class_count / len(labels))
    x_train, x_test, y_train, y_test = train_test_split(
        features,
        labels,
        test_size=test_size,
        random_state=random_state,
        stratify=labels,
    )
    model = RandomForestClassifier(n_estimators=100, random_state=random_state)
    model.fit(x_train, y_train)
    predictions = model.predict(x_test)
    classes = ["LOW", "MEDIUM", "HIGH"]
    evaluation = {
        "accuracy": accuracy_score(y_test, predictions),
        "precision": precision_score(y_test, predictions, labels=classes, average="weighted", zero_division=0),
        "recall": recall_score(y_test, predictions, labels=classes, average="weighted", zero_division=0),
        "f1": f1_score(y_test, predictions, labels=classes, average="weighted", zero_division=0),
        "confusion_matrix": confusion_matrix(y_test, predictions, labels=classes).tolist(),
    }
    artifact = {"model": model, "feature_columns": FEATURE_COLUMNS, "classes": classes}
    path = Path(model_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, path)
    return {"model": model, "dataset": dataset, "evaluation": evaluation, "model_path": path}


def load_model(model_path: str | Path) -> dict[str, object]:
    artifact = joblib.load(model_path)
    if not isinstance(artifact, dict) or "model" not in artifact:
        raise ValueError("saved model has an invalid format")
    if tuple(artifact.get("feature_columns", ())) != FEATURE_COLUMNS:
        raise ValueError("saved model feature ordering does not match the current schema")
    return artifact
