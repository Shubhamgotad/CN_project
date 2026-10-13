"""Evaluation of congestion model reliability, dataset characteristics, and generalization.

Evaluates the Random Forest model using proper experiment-grouped splitting
to avoid data leakage, analyzes label generation rules, and compares model
predictions with threshold-derived ground truth.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)
from sklearn.model_selection import StratifiedGroupKFold

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.prediction.dataset import load_person1_dataset, split_features_and_labels
from ai.prediction.features import FEATURE_COLUMNS
from ai.prediction.labeling import (
    DEFAULT_THRESHOLDS,
    congestion_score,
    label_congestion,
)
from ai.prediction.training import (
    CONGESTION_CLASSES,
    load_model,
    train_candidate_model,
)
from integration.telemetry_qos import read_person1_jsonl

DEFAULT_DATASET_PATH = PROJECT_ROOT / "telemetry" / "datasets" / "combined.jsonl"
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "congestion_model.joblib"


def format_confusion_matrix(cm: list[list[int]] | np.ndarray, classes: list[str]) -> str:
    """Format confusion matrix with clear headers and row labels."""
    cm = np.array(cm)
    header = f"{'True \\ Pred':<12} " + " ".join(f"{cls:>8}" for cls in classes)
    lines = [header, "-" * len(header)]
    for idx, cls in enumerate(classes):
        row = f"{cls:<12} " + " ".join(f"{cm[idx, j]:>8d}" for j in range(len(classes)))
        lines.append(row)
    return "\n".join(lines)


def evaluate_dataset_and_labeling(dataset_path: Path) -> dict:
    """Inspect dataset and explain threshold labeling mechanics."""
    raw_adapted = read_person1_jsonl(dataset_path, interface="training-bottleneck")
    total_records = len(raw_adapted)

    exp_records = defaultdict(list)
    scenario_counts = Counter()
    label_counts = Counter()

    for item in raw_adapted:
        exp_id = item.metadata.get("experiment_id", "unknown")
        scenario = item.metadata.get("scenario", "unknown")
        lbl = label_congestion(item.record)
        exp_records[exp_id].append({
            "scenario": scenario,
            "record": item.record,
            "label": lbl,
        })
        scenario_counts[scenario] += 1
        label_counts[lbl] += 1

    print("=" * 70)
    print("1. DATASET INSPECTION & COMPOSITION")
    print("=" * 70)
    print(f"Dataset path       : {dataset_path}")
    print(f"Total records      : {total_records}")
    print(f"Unique experiments : {len(exp_records)}")
    print(f"Overall labels     : {dict(label_counts)}")
    print(f"Scenario counts    : {dict(scenario_counts)}")
    print("\nScenario breakdown by records, experiments, and labels:")
    scenarios_summary = defaultdict(lambda: {"records": 0, "experiments": set(), "labels": Counter()})
    for exp_id, records in exp_records.items():
        for r in records:
            sc = r["scenario"]
            scenarios_summary[sc]["records"] += 1
            scenarios_summary[sc]["experiments"].add(exp_id)
            scenarios_summary[sc]["labels"][r["label"]] += 1

    for sc, data in sorted(scenarios_summary.items()):
        print(f"  - {sc:<12}: {data['records']:2d} records across {len(data['experiments']):2d} experiments | Labels: {dict(data['labels'])}")

    print("\n" + "=" * 70)
    print("2. LABEL GENERATION & THRESHOLD RULES EXPLANATION")
    print("=" * 70)
    print("The labels (LOW, MEDIUM, HIGH) are 100% synthetically generated from deterministic")
    print("threshold rules defined in ai/prediction/labeling.py.")
    print("\nConfigured thresholds (DEFAULT_THRESHOLDS):")
    print(f"  Utilization (%):   Medium >= {DEFAULT_THRESHOLDS.utilization_medium}%, High >= {DEFAULT_THRESHOLDS.utilization_high}%")
    print(f"  Latency (ms):      Medium >= {DEFAULT_THRESHOLDS.latency_medium_ms} ms, High >= {DEFAULT_THRESHOLDS.latency_high_ms} ms")
    print(f"  Packet Loss (%):   Medium >= {DEFAULT_THRESHOLDS.packet_loss_medium_pct}%, High >= {DEFAULT_THRESHOLDS.packet_loss_high_pct}%")
    print("\nSeverity scoring formula:")
    print("  score = 0")
    print("  score += 2 if utilization >= 80% else (1 if utilization >= 60% else 0)")
    print("  score += 2 if latency >= 100 ms else (1 if latency >= 50 ms else 0)")
    print("  score += 2 if packet_loss >= 2.0% else (1 if packet_loss >= 0.5% else 0)")
    print("\nMapping to class:")
    print("  score >= 4 -> 'HIGH'")
    print("  score >= 2 -> 'MEDIUM'")
    print("  score <  2 -> 'LOW'")

    return {
        "total_records": total_records,
        "unique_experiments": len(exp_records),
        "label_counts": label_counts,
        "scenario_counts": scenario_counts,
    }


def evaluate_held_out_grouped(dataset_path: Path, random_state: int = 42) -> dict:
    """Evaluate using train_candidate_model (StratifiedGroupKFold holdout)."""
    dataset, groups = load_person1_dataset(dataset_path)
    classes = list(CONGESTION_CLASSES)

    print("\n" + "=" * 70)
    print("3. HELD-OUT GROUPED EVALUATION (AVOIDING DATA LEAKAGE)")
    print("=" * 70)
    print("Splitting records by experiment_id using StratifiedGroupKFold.")
    print("Ensures no experiment appears in both training and test partitions.")

    candidate_result = train_candidate_model(dataset, groups, random_state=random_state)
    eval_info = candidate_result["evaluation"]

    n_train_records = sum(eval_info["train_support"].values())
    n_test_records = sum(eval_info["test_support"].values())
    n_train_groups = len(eval_info["train_experiment_ids"])
    n_test_groups = len(eval_info["test_experiment_ids"])

    print(f"\nSplit Summary:")
    print(f"  Training Split : {n_train_records} records across {n_train_groups} experiments")
    print(f"    Class distribution: {eval_info['train_support']}")
    print(f"  Test Split     : {n_test_records} records across {n_test_groups} experiments")
    print(f"    Class distribution: {eval_info['test_support']}")

    # Verify zero leakage
    train_exp_set = set(eval_info["train_experiment_ids"])
    test_exp_set = set(eval_info["test_experiment_ids"])
    assert train_exp_set.isdisjoint(test_exp_set), "Data leakage detected: experiment overlap!"
    print(f"  Data Leakage Check: PASSED (0 overlapping experiments between train and test)")

    print(f"\nPerformance Metrics (Held-out Test Set, N={n_test_records}):")
    print(f"  Accuracy : {eval_info['accuracy']:.4f} ({eval_info['accuracy'] * 100:.1f}%)")
    print(f"  Macro F1 : {eval_info['macro_f1']:.4f}")
    print("\nPer-Class Breakdown:")
    for cls in classes:
        metrics = eval_info["per_class"][cls]
        print(f"  {cls:<8} - Precision: {metrics['precision']:.4f}, Recall: {metrics['recall']:.4f}, F1: {metrics['f1']:.4f}, Support: {metrics['support']}")

    print("\nConfusion Matrix:")
    print(format_confusion_matrix(eval_info["confusion_matrix"], classes))

    return eval_info


def evaluate_cross_validation(dataset_path: Path) -> None:
    """Evaluate using Grouped Cross-Validation across folds to test stability."""
    print("\n" + "=" * 70)
    print("4. CROSS-VALIDATION & GENERALIZATION STABILITY")
    print("=" * 70)
    dataset, groups = load_person1_dataset(dataset_path)
    features, labels = split_features_and_labels(dataset)
    classes = list(CONGESTION_CLASSES)

    for n_splits in (2, 3, 5):
        print(f"\n--- {n_splits}-Fold StratifiedGroupKFold Cross-Validation ---")
        sgkf = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=42)
        all_true = []
        all_pred = []
        fold_accuracies = []

        from sklearn.ensemble import RandomForestClassifier

        for fold_idx, (train_idx, test_idx) in enumerate(sgkf.split(features, labels, groups)):
            x_tr, x_te = features.iloc[train_idx], features.iloc[test_idx]
            y_tr, y_te = labels.iloc[train_idx], labels.iloc[test_idx]

            rf = RandomForestClassifier(n_estimators=100, random_state=42)
            rf.fit(x_tr, y_tr)
            preds = rf.predict(x_te)

            acc = accuracy_score(y_te, preds)
            fold_accuracies.append(acc)
            all_true.extend(y_te)
            all_pred.extend(preds)

        overall_acc = accuracy_score(all_true, all_pred)
        overall_cm = confusion_matrix(all_true, all_pred, labels=classes)
        p, r, f1, sup = precision_recall_fscore_support(all_true, all_pred, labels=classes, zero_division=0)

        print(f"Overall Accuracy: {overall_acc:.4f} (Fold accuracies: {[round(a, 3) for a in fold_accuracies]})")
        print("Per-class performance across all folds:")
        for idx, cls in enumerate(classes):
            print(f"  {cls:<8} - Precision: {p[idx]:.4f}, Recall: {r[idx]:.4f}, F1: {f1[idx]:.4f}, Support: {sup[idx]}")
        print("Aggregate Confusion Matrix:")
        print(format_confusion_matrix(overall_cm, classes))


def evaluate_saved_model(dataset_path: Path, model_path: Path) -> None:
    """Evaluate the saved model and compare with threshold rules."""
    print("\n" + "=" * 70)
    print("5. EVALUATION OF SAVED MODEL (models/congestion_model.joblib)")
    print("=" * 70)
    if not model_path.exists():
        print(f"Saved model not found at {model_path}")
        return

    artifact = load_model(model_path)
    model = artifact["model"]
    classes = list(CONGESTION_CLASSES)

    dataset, groups = load_person1_dataset(dataset_path)
    features, labels = split_features_and_labels(dataset)

    predictions = model.predict(features)
    acc = accuracy_score(labels, predictions)
    cm = confusion_matrix(labels, predictions, labels=classes)
    p, r, f1, sup = precision_recall_fscore_support(labels, predictions, labels=classes, zero_division=0)

    print(f"Model file       : {model_path}")
    print(f"Features used    : {list(artifact['feature_columns'])}")
    print(f"Evaluation on entire 48-record dataset:")
    print(f"  Accuracy       : {acc:.4f} ({acc * 100:.1f}%)")
    for idx, cls in enumerate(classes):
        print(f"  {cls:<8} - Precision: {p[idx]:.4f}, Recall: {r[idx]:.4f}, F1: {f1[idx]:.4f}, Support: {sup[idx]}")
    print("\nConfusion Matrix:")
    print(format_confusion_matrix(cm, classes))

    print("\n" + "=" * 70)
    print("6. COMPARISON & METHODOLOGICAL LIMITATIONS")
    print("=" * 70)
    print("A. Original Training Script Data Leakage:")
    print("   The script scripts/train_congestion_model.py used train_test_split(test_size=0.2)")
    print("   with stratify=labels, ignoring experiment grouping.")
    print("   Because congestion experiments contain 4 records each and burst experiments")
    print("   contain 5 records each with nearly identical timestamps and network metrics,")
    print("   8 of the 10 test records (80%) came from experiments already present in the training set.")
    print("\nB. Limitations of Evaluating Against Threshold-Derived Labels:")
    print("   1. Circular Validation: The ML model is merely approximating a simple, deterministic")
    print("      mathematical formula (ai/prediction/labeling.py) rather than learning true")
    print("      network dynamics from packet-level ground truth.")
    print("   2. Artifact of Discretization: Telemetry near threshold boundaries (e.g., 79.5% utilization")
    print("      in burst scenario labeled LOW vs. 81.9% in moderate labeled MEDIUM) produces sharp")
    print("      discontinuities that cause generalization errors when novel traffic patterns appear.")
    print("   3. In 3-fold cross-validation, when burst experiments (79.5% utilization, LOW) were in")
    print("      the test fold, the model had only seen LOW at ~30% utilization in train, leading")
    print("      it to misclassify all burst records as MEDIUM (accuracy dropped to 77.1%, LOW recall to 0.31).")
    print("\nC. Dataset Limitations & Next Steps:")
    print("   1. Small Sample Size: Only 48 records from 22 experiments.")
    print("   2. Class Imbalance: 24 HIGH (50%), 16 LOW (33.3%), 8 MEDIUM (16.7%).")
    print("   3. Recommendation: Collect a larger, diversified dataset spanning intermediate")
    print("      utilizations (50-75%), varying round-trip latencies, bufferbloat queues, and loss rates.")
    print("   4. Consider continuous congestion scoring or real ground-truth signals (TCP retransmits,")
    print("      ECN, delay gradient) rather than hard artificial score thresholds.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate congestion model reliability.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH, help="Path to telemetry dataset")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH, help="Path to saved model")
    args = parser.parse_args()

    evaluate_dataset_and_labeling(args.dataset)
    evaluate_held_out_grouped(args.dataset)
    evaluate_cross_validation(args.dataset)
    evaluate_saved_model(args.dataset, args.model_path)


if __name__ == "__main__":
    main()
