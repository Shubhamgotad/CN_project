#!/usr/bin/env python3

import argparse
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.prediction.dataset import load_person1_dataset
from telemetry.validate_telemetry import validate_file


def report_labels(path: Path) -> int:
    validation = validate_file(path)
    if validation["errors"]:
        for error in validation["errors"]:
            print(f"ERROR: {error}")
        return 1

    dataset, experiment_ids = load_person1_dataset(path)
    labels = Counter(dataset["congestion"])
    class_order = ("LOW", "MEDIUM", "HIGH")

    print(f"File: {path}")
    print(f"Validated records: {len(dataset)}")
    print(f"Independent experiment IDs: {experiment_ids.nunique()}")
    print("Label distribution:")
    for label in class_order:
        print(f"  {label}: {labels.get(label, 0)}")
    if any(labels.get(label, 0) == 0 for label in class_order):
        print("WARNING: at least one congestion class has no measured examples.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate telemetry and count labels from existing thresholds."
    )
    parser.add_argument(
        "file",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "datasets" / "combined.jsonl",
        help="validated telemetry JSONL (default: telemetry/datasets/combined.jsonl)",
    )
    args = parser.parse_args()
    return report_labels(args.file)


if __name__ == "__main__":
    raise SystemExit(main())
