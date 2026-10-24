#!/usr/bin/env python3

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from telemetry.receiver_windows import validate_receiver_interval_record


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

OPTIONAL_INTERVAL_FIELDS = (
    "observation_timestamp",
    "interval_start_seconds",
    "interval_end_seconds",
    "interval_duration_seconds",
    "interval_id",
    "measurement_direction",
    "receiver_interval_schema_version",
    "boundary_source",
    "clock_domain",
    "clock_id",
    "clock_unit",
    "clock_semantics",
    "boundary_semantics",
    "host_id",
    "boot_id",
    "absolute_start_seconds",
    "absolute_end_seconds",
    "start_boundary_uncertainty_seconds",
    "end_boundary_uncertainty_seconds",
    "boundary_uncertainty_evidence",
    "producer_name",
    "producer_version",
    "producer_build_identity",
    "producer_binary_sha256",
    "producer_process_instance_id",
    "producer_pid",
    "producer_process_start_ticks",
    "boundary_metadata_status",
    "boundary_evidence_integrity_status",
    "producer_capability_validation_status",
    "clock_validation_status",
    "measurement_alignment_eligibility",
    "receiver_observation_role",
    "receiver_observation_timestamp",
    "receiver_observation_monotonic_seconds",
    "receiver_interval_start_seconds",
    "receiver_interval_end_seconds",
    "receiver_interval_duration_seconds",
    "client_observation_timestamp",
    "sender_interval_start_seconds",
    "sender_interval_end_seconds",
    "sender_interval_duration_seconds",
    "sender_throughput_mbps",
    "sender_measurement_source",
    "latency_measurement_timestamp",
    "queue_measurement_timestamp",
    "latency_measurement_age_seconds",
    "queue_measurement_age_seconds",
    "throughput_measurement_source",
    "sender_measurement_source",
)

STRING_FIELDS = (
    "timestamp",
    "experiment_id",
    "scenario",
    "source",
    "destination",
    "traffic_type",
    "measurement_direction",
    "throughput_measurement_source",
    "sender_measurement_source",
    "boundary_source",
    "clock_domain",
    "clock_id",
    "clock_unit",
    "clock_semantics",
    "boundary_semantics",
    "host_id",
    "boot_id",
    "producer_name",
    "producer_version",
    "producer_build_identity",
    "producer_binary_sha256",
    "producer_process_instance_id",
    "producer_process_start_ticks",
    "boundary_metadata_status",
    "boundary_evidence_integrity_status",
    "producer_capability_validation_status",
    "clock_validation_status",
    "measurement_alignment_eligibility",
    "receiver_observation_role",
    "receiver_observation_timestamp",
    "client_observation_timestamp",
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
    "receiver_interval_schema_version",
    "producer_pid",
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
    "receiver_interval_schema_version",
    "producer_pid",
}

THROUGHPUT_MEASUREMENT_SOURCES = {
    "client",
    "receiver",
    "synthetic_receiver",
    "iperf3_client_json_stream",
    "iperf3_server_json_stream",
}

SENDER_MEASUREMENT_SOURCES = {
    "iperf3_client_json_stream",
}

SENDER_INTERVAL_FIELDS = {
    "client_observation_timestamp",
    "sender_interval_start_seconds",
    "sender_interval_end_seconds",
    "sender_interval_duration_seconds",
    "sender_throughput_mbps",
    "sender_measurement_source",
}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_record(record):
    errors = []

    if not isinstance(record, dict):
        return ["record must be a JSON object"]

    missing = [field for field in REQUIRED_FIELDS if field not in record]
    allowed_fields = set(REQUIRED_FIELDS) | set(OPTIONAL_INTERVAL_FIELDS)
    extra = [field for field in record if field not in allowed_fields]
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected fields: {', '.join(extra)}")

    for field in STRING_FIELDS:
        if field in record and not isinstance(record[field], str):
            errors.append(f"{field} must be a string")

    for field in (
        "observation_timestamp",
        "receiver_observation_timestamp",
        "client_observation_timestamp",
        "latency_measurement_timestamp",
        "queue_measurement_timestamp",
        "interval_id",
    ):
        if field in record and not isinstance(record[field], str):
            errors.append(f"{field} must be a string")

    throughput_source = record.get("throughput_measurement_source")
    if (
        throughput_source is not None
        and throughput_source not in THROUGHPUT_MEASUREMENT_SOURCES
    ):
        errors.append("throughput_measurement_source is unsupported")
    sender_source = record.get("sender_measurement_source")
    if sender_source is not None and sender_source not in SENDER_MEASUREMENT_SOURCES:
        errors.append("sender_measurement_source is unsupported")
    sender_fields_present = SENDER_INTERVAL_FIELDS.intersection(record)
    if sender_fields_present and sender_fields_present != SENDER_INTERVAL_FIELDS:
        missing_sender_fields = sorted(SENDER_INTERVAL_FIELDS - sender_fields_present)
        errors.append(
            "sender interval metadata must be complete; missing: "
            + ", ".join(missing_sender_fields)
        )
    if throughput_source == "receiver" and sender_fields_present:
        errors.append(
            "receiver-only interval must not include sender interval metadata"
        )
    if sender_fields_present and throughput_source is None:
        errors.append(
            "sender interval metadata requires a throughput measurement source"
        )
    if sender_fields_present and throughput_source not in (
        None,
        "iperf3_server_json_stream",
    ):
        errors.append(
            "sender interval metadata conflicts with throughput measurement source"
        )
    if throughput_source == "iperf3_server_json_stream":
        if sender_fields_present != SENDER_INTERVAL_FIELDS:
            errors.append(
                "matched iperf3 server interval must include "
                "complete sender interval metadata"
            )
        for field in ("interval_start_seconds", "interval_end_seconds"):
            if field not in record:
                errors.append(
                    "matched iperf3 server interval requires "
                    "iperf-relative " + field
                )
        if (
            _is_number(record.get("interval_start_seconds"))
            and math.isfinite(record["interval_start_seconds"])
            and _is_number(record.get("interval_end_seconds"))
            and math.isfinite(record["interval_end_seconds"])
            and record["interval_end_seconds"] <= record["interval_start_seconds"]
        ):
            errors.append(
                "iperf-relative interval_end_seconds must be after "
                "interval_start_seconds"
            )

    for timestamp_field in (
        "timestamp",
        "observation_timestamp",
        "receiver_observation_timestamp",
        "client_observation_timestamp",
        "latency_measurement_timestamp",
        "queue_measurement_timestamp",
    ):
        timestamp = record.get(timestamp_field)
        if timestamp is None and timestamp_field != "timestamp":
            continue
        if isinstance(timestamp, str):
            try:
                parsed_timestamp = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )
            except ValueError:
                errors.append(
                    f"{timestamp_field} must be a valid ISO-8601 datetime"
                )
            else:
                if (
                    parsed_timestamp.tzinfo is None
                    or parsed_timestamp.utcoffset() != timezone.utc.utcoffset(None)
                ):
                    errors.append(
                        f"{timestamp_field} must include a UTC timezone"
                    )
        elif timestamp is not None:
            errors.append(f"{timestamp_field} must be a string")

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

    for field in (
        "interval_start_seconds",
        "interval_end_seconds",
        "interval_duration_seconds",
        "absolute_start_seconds",
        "absolute_end_seconds",
        "start_boundary_uncertainty_seconds",
        "end_boundary_uncertainty_seconds",
        "receiver_observation_monotonic_seconds",
        "receiver_interval_start_seconds",
        "receiver_interval_end_seconds",
        "receiver_interval_duration_seconds",
        "sender_interval_start_seconds",
        "sender_interval_end_seconds",
        "sender_interval_duration_seconds",
        "sender_throughput_mbps",
        "latency_measurement_age_seconds",
        "queue_measurement_age_seconds",
    ):
        if field in record:
            value = record[field]
            if not _is_number(value) or not math.isfinite(value):
                errors.append(f"{field} must be a finite number")
            elif value < 0:
                errors.append(f"{field} must be non-negative")

    active_flows = record.get("active_flows")
    if isinstance(active_flows, int) and active_flows < 0:
        errors.append("active_flows must be a non-negative integer")

    receiver_boundary_fields = {
        "receiver_interval_schema_version",
        "boundary_source",
        "clock_domain",
        "clock_id",
        "clock_unit",
        "clock_semantics",
        "boundary_semantics",
        "host_id",
        "boot_id",
        "absolute_start_seconds",
        "absolute_end_seconds",
        "start_boundary_uncertainty_seconds",
        "end_boundary_uncertainty_seconds",
        "boundary_uncertainty_evidence",
        "producer_name",
        "producer_version",
        "producer_build_identity",
        "producer_binary_sha256",
        "producer_process_instance_id",
        "producer_pid",
        "producer_process_start_ticks",
        "boundary_metadata_status",
        "boundary_evidence_integrity_status",
        "producer_capability_validation_status",
        "clock_validation_status",
        "measurement_alignment_eligibility",
    }
    if receiver_boundary_fields.intersection(record):
        try:
            validate_receiver_interval_record(record)
        except ValueError as exc:
            errors.append(str(exc))
    if (
        (
            "receiver_observation_monotonic_seconds" in record
            or "receiver_observation_timestamp" in record
        )
        and record.get("receiver_observation_role") != "diagnostic_only"
    ):
        errors.append(
            "receiver_observation_monotonic_seconds must be marked diagnostic_only"
        )

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
                    schema = set(record) - set(OPTIONAL_INTERVAL_FIELDS)
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
