#!/usr/bin/env python3

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_FIELDS = (
    "timestamp",
    "experiment_id",
    "scenario",
    "source",
    "destination",
    "traffic_type",
    "requested_mbps",
    "throughput_mbps",
    "latency_ms",
    "jitter_ms",
    "packet_loss_percent",
    "lost_packets",
    "total_packets",
    "bandwidth_mbps",
    "utilization_percent",
    "active_flows",
    "queue_size",
    "queue_drops",
)

STRING_FIELDS = (
    "timestamp",
    "experiment_id",
    "scenario",
    "source",
    "destination",
    "traffic_type",
)

NUMERIC_FIELDS = (
    "requested_mbps",
    "throughput_mbps",
    "latency_ms",
    "jitter_ms",
    "packet_loss_percent",
    "lost_packets",
    "total_packets",
    "bandwidth_mbps",
    "utilization_percent",
    "active_flows",
    "queue_size",
    "queue_drops",
)

NULLABLE_NUMERIC_FIELDS = {
    "requested_mbps",
    "latency_ms",
    "jitter_ms",
    "packet_loss_percent",
    "lost_packets",
    "total_packets",
    "bandwidth_mbps",
    "utilization_percent",
    "queue_size",
    "queue_drops",
}

INTEGER_FIELDS = {
    "lost_packets",
    "total_packets",
    "active_flows",
    "queue_size",
    "queue_drops",
}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_record(record):
    errors = []

    if not isinstance(record, dict):
        return ["record must be a JSON object"]

    missing = [field for field in REQUIRED_FIELDS if field not in record]
    extra = [field for field in record if field not in REQUIRED_FIELDS]
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected fields: {', '.join(extra)}")

    for field in STRING_FIELDS:
        if field in record and not isinstance(record[field], str):
            errors.append(f"{field} must be a string")

    timestamp = record.get("timestamp")
    if isinstance(timestamp, str):
        try:
            parsed_timestamp = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
        except ValueError:
            errors.append("timestamp must be a valid ISO-8601 datetime")
        else:
            if (
                parsed_timestamp.tzinfo is None
                or parsed_timestamp.utcoffset() != timezone.utc.utcoffset(None)
            ):
                errors.append("timestamp must include a UTC timezone")

    for field in NUMERIC_FIELDS:
        if field not in record:
            continue
        value = record[field]
        if value is None and field in NULLABLE_NUMERIC_FIELDS:
            continue
        if not _is_number(value) or not math.isfinite(value):
            errors.append(f"{field} must be a finite number")
            continue
        if field in INTEGER_FIELDS and not isinstance(value, int):
            errors.append(f"{field} must be an integer")
            continue
        if field in {
            "requested_mbps",
            "throughput_mbps",
            "latency_ms",
            "jitter_ms",
            "lost_packets",
            "total_packets",
            "bandwidth_mbps",
            "active_flows",
            "queue_size",
            "queue_drops",
        } and value < 0:
            errors.append(f"{field} must be non-negative")
        if field in {"packet_loss_percent", "utilization_percent"} and not (
            0 <= value <= 100
        ):
            errors.append(f"{field} must be between 0 and 100")

    active_flows = record.get("active_flows")
    if isinstance(active_flows, int) and active_flows < 0:
        errors.append("active_flows must be a non-negative integer")

    return errors


def validate_file(path):
    records = 0
    valid_records = 0
    invalid_records = 0
    scenarios = set()
    expected_schema = None
    errors_found = []

    try:
        file = path.open("r", encoding="utf-8")
    except OSError as error:
        return {
            "records": 0,
            "valid": 0,
            "invalid": 0,
            "scenarios": scenarios,
            "errors": [f"{path}: {error}"],
        }

    with file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            records += 1
            line_errors = []
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                record = None
                line_errors.append(f"invalid JSON: {error.msg}")
            else:
                if isinstance(record, dict):
                    if isinstance(record.get("scenario"), str):
                        scenarios.add(record["scenario"])
                    schema = set(record)
                    if expected_schema is None:
                        expected_schema = schema
                    elif schema != expected_schema:
                        missing = sorted(expected_schema - schema)
                        extra = sorted(schema - expected_schema)
                        details = []
                        if missing:
                            details.append(f"missing {', '.join(missing)}")
                        if extra:
                            details.append(f"extra {', '.join(extra)}")
                        line_errors.append(
                            "inconsistent schema: " + "; ".join(details)
                        )
                line_errors.extend(_validate_record(record))

            if line_errors:
                invalid_records += 1
                errors_found.extend(
                    f"{path}:{line_number}: {error}" for error in line_errors
                )
            else:
                valid_records += 1

    return {
        "records": records,
        "valid": valid_records,
        "invalid": invalid_records,
        "scenarios": scenarios,
        "errors": errors_found,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Validate Person 1 JSONL telemetry datasets."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="JSONL dataset file(s); defaults to telemetry/datasets/*.jsonl",
    )
    args = parser.parse_args()

    files = args.files
    if not files:
        files = sorted((Path(__file__).parent / "datasets").glob("*.jsonl"))
        if not files:
            parser.error("no dataset files supplied or found in telemetry/datasets")

    total_records = 0
    total_valid = 0
    total_invalid = 0
    all_scenarios = set()
    failed = False

    for path in files:
        result = validate_file(path)
        total_records += result["records"]
        total_valid += result["valid"]
        total_invalid += result["invalid"]
        all_scenarios.update(result["scenarios"])
        print(f"File: {path}")
        print(f"  Records: {result['records']}")
        print(f"  Valid: {result['valid']}")
        print(f"  Invalid: {result['invalid']}")
        print(f"  Scenarios: {', '.join(sorted(result['scenarios'])) or '(none)'}")
        if result["errors"]:
            failed = True
            for error in result["errors"]:
                print(f"  ERROR: {error}")

    print("Summary:")
    print(f"  Records: {total_records}")
    print(f"  Valid: {total_valid}")
    print(f"  Invalid: {total_invalid}")
    print(f"  Scenarios: {', '.join(sorted(all_scenarios)) or '(none)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
