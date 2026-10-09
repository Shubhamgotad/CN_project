"""Random Forest training, evaluation, and model persistence."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
    precision_score,
    recall_score,
)
from sklearn.model_selection import StratifiedGroupKFold, train_test_split

from .dataset import create_dataset, split_features_and_labels
from .features import FEATURE_COLUMNS
from .labeling import CongestionThresholds, DEFAULT_THRESHOLDS
from .telemetry import TelemetryRecord


CONGESTION_CLASSES = ("LOW", "MEDIUM", "HIGH")


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
    classes = list(CONGESTION_CLASSES)
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


def train_candidate_model(
    dataset: pd.DataFrame,
    groups: Sequence[str],
    *,
    random_state: int = 42,
) -> dict[str, object]:
    """Fit and evaluate an in-memory candidate using experiment-grouped holdout."""
    features, labels = split_features_and_labels(dataset)
    if features.isna().any().any():
        raise ValueError("candidate features cannot contain missing values")
    try:
        feature_values = features.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate features must be numeric") from exc
    if not np.isfinite(feature_values).all():
        raise ValueError("candidate features must be finite")

    unknown_labels = set(labels) - set(CONGESTION_CLASSES)
    if unknown_labels:
        raise ValueError(
            "dataset contains unsupported congestion labels: "
            + ", ".join(sorted(map(str, unknown_labels)))
        )
    if isinstance(groups, (str, bytes)):
        raise ValueError("groups must contain one experiment ID per dataset row")
    group_values = list(groups)
    if len(group_values) != len(dataset):
        raise ValueError("groups must contain one experiment ID per dataset row")
    if any(not isinstance(group, str) or not group.strip() for group in group_values):
        raise ValueError("experiment IDs must be non-empty strings")
    if len(set(group_values)) < 2:
        raise ValueError("candidate evaluation requires at least two experiment groups")

    classes = list(CONGESTION_CLASSES)
    warnings_list = []
    class_group_support = {
        label: len(
            {
                group_values[index]
                for index, observed in enumerate(labels)
                if observed == label
            }
        )
        for label in classes
    }
    for label in classes:
        if 0 < class_group_support[label] < 2:
            warnings_list.append(
                f"{label} appears in fewer than two experiment groups; "
                "it cannot be supported in both train and test."
            )
    absent_classes = [label for label in classes if label not in set(labels)]
    if absent_classes:
        warnings_list.append(
            "dataset has no examples for: " + ", ".join(absent_classes)
        )

    splitter = StratifiedGroupKFold(
        n_splits=2,
        shuffle=True,
        random_state=random_state,
    )
    train_indices, test_indices = next(
        splitter.split(features, labels, groups=group_values)
    )
    x_train = features.iloc[train_indices]
    x_test = features.iloc[test_indices]
    y_train = labels.iloc[train_indices]
    y_test = labels.iloc[test_indices]
    train_groups = {group_values[index] for index in train_indices}
    test_groups = {group_values[index] for index in test_indices}
    if train_groups.intersection(test_groups):
        raise RuntimeError("experiment-group split placed a group in both partitions")
    test_support = {
        label: int((y_test == label).sum()) for label in classes
    }
    train_support = {
        label: int((y_train == label).sum()) for label in classes
    }
    for label in classes:
        if test_support[label] == 0:
            warnings_list.append(f"test partition has no {label} examples.")
    train_missing = [label for label in classes if train_support[label] == 0]
    if train_missing:
        warnings_list.append(
            "training partition has no examples for: " + ", ".join(train_missing)
        )
    for message in warnings_list:
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    if y_train.nunique() < 2:
        raise ValueError(
            "grouped training partition contains fewer than two classes; "
            "collect more independently grouped class examples"
        )

    model = RandomForestClassifier(n_estimators=100, random_state=random_state)
    model.fit(x_train, y_train)
    predictions = model.predict(x_test)
    precision, recall, f1, support = precision_recall_fscore_support(
        y_test,
        predictions,
        labels=classes,
        zero_division=0,
    )
    test_support = {label: int(value) for label, value in zip(classes, support)}

    evaluation = {
        "class_distribution": {
            label: int((labels == label).sum()) for label in classes
        },
        "class_group_support": class_group_support,
        "train_support": train_support,
        "test_support": test_support,
        "per_class": {
            label: {
                "precision": float(precision[index]),
                "recall": float(recall[index]),
                "f1": float(f1[index]),
                "support": test_support[label],
            }
            for index, label in enumerate(classes)
        },
        "accuracy": float(accuracy_score(y_test, predictions)),
        "macro_f1": float(
            f1_score(y_test, predictions, labels=classes, average="macro", zero_division=0)
        ),
        "confusion_matrix": confusion_matrix(
            y_test, predictions, labels=classes
        ).tolist(),
        "train_experiment_ids": sorted(train_groups),
        "test_experiment_ids": sorted(test_groups),
        "warnings": warnings_list,
    }
    return {
        "model": model,
        "dataset": dataset,
        "evaluation": evaluation,
    }


def load_model(model_path: str | Path) -> dict[str, object]:
    artifact = joblib.load(model_path)
    if not isinstance(artifact, dict) or "model" not in artifact:
        raise ValueError("saved model has an invalid format")
    if tuple(artifact.get("feature_columns", ())) != FEATURE_COLUMNS:
        raise ValueError("saved model feature ordering does not match the current schema")
    return artifact
