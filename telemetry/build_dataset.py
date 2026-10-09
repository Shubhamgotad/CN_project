#!/usr/bin/env python3

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from validate_telemetry import REQUIRED_FIELDS, _validate_record


DATASET_DIR = Path(__file__).parent / "datasets"
INPUT_FILES = (
    DATASET_DIR / "normal.jsonl",
    DATASET_DIR / "congestion.jsonl",
    DATASET_DIR / "burst.jsonl",
)
JSONL_OUTPUT = DATASET_DIR / "combined.jsonl"
CSV_OUTPUT = DATASET_DIR / "combined.csv"
AVERAGE_FIELDS = (
    "throughput_mbps",
    "latency_ms",
    "jitter_ms",
    "packet_loss_percent",
    "utilization_percent",
)


def _timestamp_key(record):
    timestamp = record["timestamp"].replace("Z", "+00:00")
    return datetime.fromisoformat(timestamp)


def _load_valid_records(path):
    records = []
    invalid = 0

    if not path.exists():
        print(f"[DATASET] Missing input, skipped: {path}")
        return records, invalid

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                print(
                    f"[DATASET] Invalid JSON skipped: "
                    f"{path}:{line_number}: {error.msg}"
                )
                invalid += 1
                continue

            errors = _validate_record(record)
            if errors:
                print(
                    f"[DATASET] Invalid record skipped: "
                    f"{path}:{line_number}: {'; '.join(errors)}"
                )
                invalid += 1
                continue

            records.append(record)

    return records, invalid


def _average(records, field):
    values = [
        record[field]
        for record in records
        if isinstance(record[field], (int, float))
        and not isinstance(record[field], bool)
    ]
    if not values:
        return None
    return sum(values) / len(values)


def build_dataset():
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    invalid_records = 0

    for path in INPUT_FILES:
        file_records, invalid = _load_valid_records(path)
        records.extend(file_records)
        invalid_records += invalid

    unique_records = {}
    for record in records:
        identity = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        unique_records[identity] = record

    combined = sorted(unique_records.values(), key=_timestamp_key)

    with JSONL_OUTPUT.open("w", encoding="utf-8", newline="") as file:
        for record in combined:
            ordered_record = {
                field: record[field] for field in REQUIRED_FIELDS
            }
            file.write(json.dumps(ordered_record, allow_nan=False) + "\n")

    with CSV_OUTPUT.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REQUIRED_FIELDS)
        writer.writeheader()
        for record in combined:
            writer.writerow(
                {field: record[field] for field in REQUIRED_FIELDS}
            )

    scenarios = Counter(record["scenario"] for record in combined)
    print(f"Total records: {len(combined)}")
    for scenario in sorted(scenarios):
        print(f"Records for {scenario}: {scenarios[scenario]}")
    for field in AVERAGE_FIELDS:
        average = _average(combined, field)
        display = f"{average:.2f}" if average is not None else "n/a"
        print(f"Average {field}: {display}")
    print(f"Invalid input records skipped: {invalid_records}")
    print(f"Wrote: {JSONL_OUTPUT}")
    print(f"Wrote: {CSV_OUTPUT}")


if __name__ == "__main__":
    build_dataset()
