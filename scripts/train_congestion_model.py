"""Train the congestion classification model using telemetry records."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

# Ensure the project root directory is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.prediction.labeling import label_congestion
from ai.prediction.training import train_random_forest
from integration.telemetry_qos import read_person1_jsonl

DEFAULT_DATASET_PATH = PROJECT_ROOT / "telemetry" / "datasets" / "combined.jsonl"
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "congestion_model.joblib"


def train_model(
    dataset_path: Path = DEFAULT_DATASET_PATH,
    model_path: Path = DEFAULT_MODEL_PATH,
    interface: str = "training-bottleneck",
    random_state: int = 42,
) -> dict[str, object]:
    """Load adapted telemetry records, train Random Forest, and persist model."""
    dataset_path = Path(dataset_path)
    model_path = Path(model_path)

    if not dataset_path.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")

    adapted_records = read_person1_jsonl(
        dataset_path,
        interface=interface,
    )
    records = [item.record for item in adapted_records]
    total_records = len(records)
    print(f"Loaded {total_records} telemetry records from {dataset_path}")

    # Compute and display label distribution
    labels = [label_congestion(record) for record in records]
    distribution = Counter(labels)
    print("\nCongestion label distribution:")
    for label in ("LOW", "MEDIUM", "HIGH"):
        print(f"  {label}: {distribution.get(label, 0)}")

    model_path.parent.mkdir(parents=True, exist_ok=True)

    result = train_random_forest(
        records=records,
        model_path=model_path,
        random_state=random_state,
    )

    print("\nTraining completed successfully.")
    print(f"Saved model path: {model_path}")

    evaluation = result["evaluation"]
    print("\nEvaluation metrics:")
    print(f"  Accuracy:         {evaluation['accuracy']:.4f}")
    print(f"  Precision (wt):   {evaluation['precision']:.4f}")
    print(f"  Recall (wt):      {evaluation['recall']:.4f}")
    print(f"  F1 Score (wt):    {evaluation['f1']:.4f}")
    print(f"  Confusion Matrix: {evaluation['confusion_matrix']}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train Random Forest congestion model from telemetry dataset."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help="Path to input telemetry jsonl dataset",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=DEFAULT_MODEL_PATH,
        help="Path where trained model will be saved",
    )
    parser.add_argument(
        "--random-state",
        type=int,
        default=42,
        help="Random state for reproducibility",
    )
    args = parser.parse_args()

    train_model(
        dataset_path=args.dataset,
        model_path=args.model_path,
        random_state=args.random_state,
    )


if __name__ == "__main__":
    main()
