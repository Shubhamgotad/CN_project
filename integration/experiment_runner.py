"""Offline baseline/adaptive comparison runner for Person 1 telemetry JSONL.

This module generates analysis and allocation plans only. It never applies TC
commands or invokes network/system utilities.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import uuid
from typing import Any

from ai.prediction.features import FEATURE_COLUMNS, extract_features
from integration.telemetry_qos import (
    QoSIntegrationConfig,
    adapt_person1_telemetry,
    build_qos_integration,
)
from integration.safe_outputs import validate_new_output_paths, write_exclusive_text


BASELINE_POLICY = {
    "name": "existing-static-baseline",
    "interface": "s1-eth5",
    "root_qdisc": "htb 5:",
    "aggregate_class": {
        "classid": "5:1",
        "rate_mbps": 10.0,
        "ceil_mbps": 10.0,
    },
    "leaf_qdisc": {
        "qdisc": "netem",
        "handle": "10:",
        "delay": "10ms",
        "limit_packets": 1000,
    },
    "flow_specific_allocations_mbps": None,
}

MEASURED_FIELDS = (
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
REQUEST_FIELDS = ("requested_mbps",)
WINDOW_FIELDS = (
    "interval_start_seconds",
    "interval_end_seconds",
    "interval_duration_seconds",
)
MAX_MEASUREMENT_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
QUEUE_MEASUREMENT_FIELDS = ("queue_size", "queue_drops")


class _NonStandardJSONNumber(str):
    pass


class _OverflowingJSONFloat(str):
    pass


def _error(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _reject_nonstandard_json_number(value: str) -> _NonStandardJSONNumber:
    return _NonStandardJSONNumber(value)


def _parse_finite_json_float(value: str) -> float | _OverflowingJSONFloat:
    parsed = float(value)
    if not math.isfinite(parsed):
        return _OverflowingJSONFloat(value)
    return parsed


def _contains_overflowing_json_float(value: Any) -> bool:
    if isinstance(value, _OverflowingJSONFloat):
        return True
    if isinstance(value, Mapping):
        return any(_contains_overflowing_json_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_overflowing_json_float(item) for item in value)
    return False


def _find_nonstandard_json_number(value: Any) -> str | None:
    if isinstance(value, _NonStandardJSONNumber):
        return str(value)
    if isinstance(value, Mapping):
        for child in value.values():
            found = _find_nonstandard_json_number(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nonstandard_json_number(child)
            if found is not None:
                return found
    return None


def _report_value(value: Any) -> Any:
    if isinstance(value, _NonStandardJSONNumber):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, Mapping):
        return {key: _report_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_report_value(item) for item in value]
    return value


def _queue_quality(
    record: Mapping[str, Any],
    *,
    strict: bool,
) -> dict[str, Any]:
    errors: list[str] = []
    fields: dict[str, dict[str, Any]] = {}
    for field in QUEUE_MEASUREMENT_FIELDS:
        if field not in record:
            error = f"{field} is missing; include a measured nonnegative number"
            fields[field] = {"status": "missing", "value": None}
            errors.append(error)
            continue
        value = record[field]
        if value is None:
            fields[field] = {"status": "missing", "value": None}
            errors.append(f"{field} is null; provide a measured nonnegative number")
            continue
        if isinstance(value, _OverflowingJSONFloat):
            fields[field] = {"status": "invalid", "value": str(value)}
            errors.append(f"{field} must be finite; overflowing JSON numbers are invalid")
            continue
        if isinstance(value, _NonStandardJSONNumber):
            fields[field] = {"status": "invalid", "value": str(value)}
            errors.append(
                f"{field} must be finite; JSON NaN and infinities are not valid measurements"
            )
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fields[field] = {"status": "invalid", "value": _report_value(value)}
            errors.append(f"{field} must be a nonnegative finite number, not {type(value).__name__}")
            continue
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            fields[field] = {"status": "invalid", "value": str(value)}
            errors.append(f"{field} must be finite; NaN and infinity are not valid measurements")
            continue
        if value < 0:
            fields[field] = {"status": "invalid", "value": value}
            errors.append(f"{field} cannot be negative")
            continue
        fields[field] = {"status": "valid", "value": value}

    ready = not errors
    if ready:
        status = "ready"
    elif strict:
        status = "blocked"
    elif any(item["status"] == "invalid" for item in fields.values()):
        status = "not_enforced_invalid"
    else:
        status = "not_required_missing"
    return {
        "status": status,
        "ready": ready,
        "enforced": strict,
        "fields": fields,
        "errors": errors,
    }


def _freshness_status(
    timestamp_value: Any,
    *,
    strict: bool,
    now: datetime | None,
) -> dict[str, Any]:
    if not strict:
        return {"status": "not_enforced", "enforced": False, "errors": []}
    if not isinstance(timestamp_value, str):
        error = "timestamp must be an ISO-8601 measurement time with a timezone offset"
        return {"status": "invalid", "enforced": True, "errors": [error]}
    try:
        timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
    except ValueError:
        error = f"timestamp {timestamp_value!r} is malformed; use ISO-8601 with a timezone offset"
        return {"status": "invalid", "enforced": True, "errors": [error]}
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        error = "timestamp must include a timezone offset; timezone-naive times cannot be freshness-checked"
        return {"status": "invalid", "enforced": True, "errors": [error]}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware when freshness enforcement is enabled")
    age_seconds = (current.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age_seconds > MAX_MEASUREMENT_AGE_SECONDS:
        error = (
            f"measurement timestamp is stale by {age_seconds:.1f}s; "
            f"maximum allowed age is {MAX_MEASUREMENT_AGE_SECONDS}s"
        )
        return {
            "status": "stale",
            "enforced": True,
            "age_seconds": age_seconds,
            "errors": [error],
        }
    if age_seconds < -MAX_FUTURE_SKEW_SECONDS:
        error = (
            f"measurement timestamp is {-age_seconds:.1f}s in the future; "
            f"maximum allowed clock skew is {MAX_FUTURE_SKEW_SECONDS}s"
        )
        return {
            "status": "future",
            "enforced": True,
            "age_seconds": age_seconds,
            "errors": [error],
        }
    return {
        "status": "fresh",
        "enforced": True,
        "age_seconds": age_seconds,
        "errors": [],
    }


def _record_window(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "telemetry-record",
        "timestamp": _report_value(record.get("timestamp")),
        **{field: _report_value(record.get(field)) for field in WINDOW_FIELDS},
    }


def _baseline_result(prediction: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "status": "planned",
        "policy": BASELINE_POLICY,
        "allocation": {
            "status": "not-applicable",
            "reason": "the existing baseline shares the aggregate 10 Mbps class",
            "flow_allocations_mbps": None,
        },
        "congestion_prediction": prediction,
        "prediction_used_for_policy": False,
        "planning_only": True,
        "experiment_executed": False,
        "measurements_collected": False,
        "live_qos_applied": False,
        "tc_commands_executed": False,
    }


def _adaptive_result(
    integration_result: Mapping[str, Any],
    input_features: Mapping[str, float],
) -> dict[str, Any]:
    decision = integration_result["qos_decision"]
    managed = integration_result["managed_htb_config"]
    return {
        "status": "planned",
        "prediction": dict(integration_result["prediction"]),
        "input_features": dict(input_features),
        "total_bandwidth_mbps": float(integration_result["total_bandwidth_mbps"]),
        "policy_percentages": dict(decision["policy"]),
        "service_class_allocation_mbps": dict(managed["service_allocations_mbps"]),
        "unmatched_leaf": {
            "classid": managed["default_class_id"],
            "rate_mbps": managed["unmatched_rate_mbps"],
            "ceil_mbps": managed["unmatched_ceil_mbps"],
        },
        "flow_policy_classes": dict(integration_result["flow_policy_classes"]),
        "flow_allocations_mbps": dict(integration_result["flow_allocations_mbps"]),
        "filter_priorities": dict(managed["filter_priorities"]),
        "planning_only": True,
        "experiment_executed": False,
        "measurements_collected": False,
        "live_qos_applied": False,
        "tc_commands_executed": False,
    }


def run_experiment(
    input_path: str | Path,
    output_path: str | Path,
    *,
    mode: str,
    model_path: str | Path | None = None,
    qos_config: QoSIntegrationConfig | None = None,
    enforce_freshness: bool = False,
    require_queue_measurements: bool = False,
    now: datetime | None = None,
    experiment_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create offline plans from Person 1 JSONL without applying QoS or measuring.

    In ``both`` mode baseline and adaptive outputs are paired within each
    telemetry record. The source contains aggregate samples, not a verified
    synchronized multi-flow interval window; no cross-record aggregation is
    inferred here.

    Report status is ``planned`` only when all applicable plans were generated
    without invalid or blocked records; ``incomplete`` means some plans exist
    alongside errors or blocked records; ``planning_failed`` means usable
    telemetry produced no plan; ``invalid_input`` means the input is empty or
    every nonblank record failed telemetry validation. Historical replay is
    permissive by default. Strict freshness uses a 300-second maximum age and
    30-second future clock-skew allowance on the Person 1 ``timestamp``
    freshness reference. The parser can set that field when it observes an
    interval report; it is not a verified interval-start measurement time.
    Strict queue quality requires both queue measurements to be present,
    finite, numeric, and nonnegative. These checks never turn the runner into
    a measurement or live-application tool.
    """
    if not isinstance(mode, str) or mode not in {"baseline", "adaptive", "both"}:
        raise ValueError("mode must be 'baseline', 'adaptive', or 'both'")
    include_adaptive = mode in {"adaptive", "both"}
    if include_adaptive:
        if model_path is None:
            raise ValueError("adaptive mode requires an existing model_path")
        if not isinstance(qos_config, QoSIntegrationConfig):
            raise ValueError("adaptive mode requires an explicit QoSIntegrationConfig")
        if not qos_config.dry_run:
            raise ValueError("the experiment runner only accepts dry-run QoS configuration")
        qos_config.require_complete_policy_mapping()
    if not isinstance(enforce_freshness, bool):
        raise ValueError("enforce_freshness must be a boolean")
    if not isinstance(require_queue_measurements, bool):
        raise ValueError("require_queue_measurements must be a boolean")
    if experiment_metadata is not None and not isinstance(experiment_metadata, Mapping):
        raise ValueError("experiment_metadata must be a mapping")
    if enforce_freshness and now is not None and (
        now.tzinfo is None or now.utcoffset() is None
    ):
        raise ValueError("now must be timezone-aware when freshness enforcement is enabled")

    source = Path(input_path).expanduser().resolve(strict=True)
    output_candidate = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("input_path must be a JSONL file")
    if source == output_candidate:
        raise ValueError("output_path must not overwrite the input telemetry file")
    destination, = validate_new_output_paths((output_path,))

    modes = ("baseline", "adaptive") if mode == "both" else (mode,)
    records: list[dict[str, Any]] = []
    records_processed = 0
    valid_records = 0
    invalid_records = 0
    decision_errors = 0
    baseline_plans = 0
    adaptive_plans = 0
    blocked_records = 0

    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            records_processed += 1
            raw: Any = None
            queue_quality: dict[str, Any] = {
                "status": "unavailable",
                "ready": False,
                "enforced": require_queue_measurements,
                "fields": {},
                "errors": [],
            }
            freshness: dict[str, Any] = {
                "status": "not_enforced",
                "enforced": False,
                "errors": [],
            }
            try:
                raw = json.loads(
                    line,
                    parse_float=_parse_finite_json_float,
                    parse_constant=_reject_nonstandard_json_number,
                )
                if not isinstance(raw, Mapping):
                    raise ValueError("telemetry JSONL record must be an object")
                queue_quality = _queue_quality(
                    raw, strict=require_queue_measurements
                )
                freshness = _freshness_status(
                    raw.get("timestamp"),
                    strict=enforce_freshness,
                    now=now,
                )
                nonstandard = _find_nonstandard_json_number(raw)
                queue_nonstandard = any(
                    isinstance(raw.get(field), _NonStandardJSONNumber)
                    for field in QUEUE_MEASUREMENT_FIELDS
                )
                non_queue_overflow = any(
                    _contains_overflowing_json_float(value)
                    for field, value in raw.items()
                    if field not in QUEUE_MEASUREMENT_FIELDS
                )
                if non_queue_overflow:
                    raise ValueError(
                        "JSON floating-point value is not finite"
                    )
                if nonstandard is not None and not (
                    require_queue_measurements and queue_nonstandard
                ):
                    raise ValueError(
                        f"non-standard JSON numeric constant {nonstandard!r}"
                    )
                adaptation_input = dict(raw)
                for field in QUEUE_MEASUREMENT_FIELDS:
                    adaptation_input.setdefault(field, None)
                adapted = adapt_person1_telemetry(
                    adaptation_input,
                    interface=qos_config.interface if qos_config else "s1-eth5",
                )
            except (json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
                invalid_records += 1
                strict_block = (
                    (require_queue_measurements and not queue_quality["ready"])
                    or (enforce_freshness and freshness["status"] != "fresh")
                )
                if strict_block:
                    blocked_records += 1
                errors = [_error(exc)]
                errors.extend(
                    {"type": "QueueMeasurementQualityError", "message": message}
                    for message in queue_quality["errors"]
                    if require_queue_measurements
                )
                errors.extend(
                    {"type": "TelemetryFreshnessError", "message": message}
                    for message in freshness["errors"]
                )
                records.append({
                    "line": line_number,
                    "status": "invalid",
                    "experiment_id": (
                        _report_value(raw.get("experiment_id"))
                        if isinstance(raw, Mapping) else None
                    ),
                    "window": _record_window(raw) if isinstance(raw, Mapping) else None,
                    "measured": {
                        field: _report_value(raw.get(field)) if isinstance(raw, Mapping) else None
                        for field in MEASURED_FIELDS
                    },
                    "freshness": freshness,
                    "queue_measurement_quality": queue_quality,
                    "errors": errors,
                })
                continue

            valid_records += 1
            strict_blocked = (
                (require_queue_measurements and not queue_quality["ready"])
                or (enforce_freshness and freshness["status"] != "fresh")
            )
            if strict_blocked:
                blocked_records += 1
            input_features = dict(zip(
                FEATURE_COLUMNS, extract_features(adapted.record)
            ))
            measured = {
                field: _report_value(raw.get(field)) for field in MEASURED_FIELDS
            }
            mode_results: dict[str, Any] = {}
            adaptive_integration: Mapping[str, Any] | None = None
            adaptive_failure: Exception | None = None
            if include_adaptive and not strict_blocked:
                try:
                    adaptive_integration = build_qos_integration(
                        adaptation_input,
                        model_path=model_path,
                        config=qos_config,
                    )
                except Exception as exc:
                    adaptive_failure = exc
                    decision_errors += 1

            if "baseline" in modes:
                if strict_blocked:
                    mode_results["baseline"] = {
                        "status": "blocked",
                        "errors": queue_quality["errors"] + freshness["errors"],
                        "planning_only": True,
                        "experiment_executed": False,
                        "measurements_collected": False,
                        "live_qos_applied": False,
                        "tc_commands_executed": False,
                    }
                else:
                    prediction = (
                        adaptive_integration["prediction"]
                        if adaptive_integration is not None
                        else None
                    )
                    mode_results["baseline"] = _baseline_result(prediction)
                    baseline_plans += 1
            if "adaptive" in modes:
                if strict_blocked:
                    mode_results["adaptive"] = {
                        "status": "blocked",
                        "errors": queue_quality["errors"] + freshness["errors"],
                        "planning_only": True,
                        "experiment_executed": False,
                        "measurements_collected": False,
                        "live_qos_applied": False,
                        "tc_commands_executed": False,
                    }
                elif adaptive_integration is None:
                    mode_results["adaptive"] = {
                        "status": "planning_failed",
                        "error": _error(adaptive_failure or RuntimeError(
                            "adaptive decision did not produce a result"
                        )),
                        "planning_only": True,
                        "experiment_executed": False,
                        "measurements_collected": False,
                        "live_qos_applied": False,
                        "tc_commands_executed": False,
                    }
                else:
                    mode_results["adaptive"] = _adaptive_result(
                        adaptive_integration, input_features
                    )
                    adaptive_plans += 1

            plan_count = sum(
                result.get("status") == "planned"
                for result in mode_results.values()
            )
            expected_plan_count = len(modes)
            record_status = (
                "planned" if plan_count == expected_plan_count
                else "incomplete" if plan_count
                else "blocked" if strict_blocked
                else "planning_failed"
            )

            records.append({
                "line": line_number,
                "status": record_status,
                "experiment_id": adapted.metadata.get("experiment_id"),
                "scenario": adapted.metadata.get("scenario"),
                "source": adapted.metadata.get("source"),
                "destination": adapted.metadata.get("destination"),
                "traffic_type": adapted.metadata.get("traffic_type"),
                "request": {
                    field: _report_value(raw.get(field)) for field in REQUEST_FIELDS
                },
                "window": _record_window(raw),
                "measured": measured,
                "freshness": freshness,
                "queue_measurement_quality": queue_quality,
                "measurement_scope": {
                    "queue_size": "scenario-wide",
                    "queue_drops": "scenario-wide",
                },
                "modes": mode_results,
            })

    if records_processed == 0:
        report_status = "incomplete"
    elif invalid_records == records_processed:
        report_status = "invalid_input"
    elif baseline_plans + adaptive_plans == 0:
        report_status = "planning_failed"
    else:
        expected_baseline = valid_records - blocked_records if "baseline" in modes else 0
        expected_adaptive = valid_records - blocked_records if "adaptive" in modes else 0
        report_status = (
            "planned"
            if (
                invalid_records == 0
                and blocked_records == 0
                and decision_errors == 0
                and baseline_plans == expected_baseline
                and adaptive_plans == expected_adaptive
            )
            else "incomplete"
        )

    report = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "status": report_status,
        "planning_only": True,
        "experiment_executed": False,
        "measurements_collected": False,
        "live_qos_applied": False,
        "tc_commands_executed": False,
        "dry_run": True,
        "freshness_policy": {
            "enforced": enforce_freshness,
            "timestamp_field": "timestamp",
            "timestamp_semantics": (
                "record freshness reference; parser-generated interval records "
                "use report observation time, not interval start"
            ),
            "maximum_measurement_age_seconds": MAX_MEASUREMENT_AGE_SECONDS,
            "maximum_future_clock_skew_seconds": MAX_FUTURE_SKEW_SECONDS,
        },
        "queue_measurement_policy": {
            "strict": require_queue_measurements,
            "permissive_missing_values_allowed": not require_queue_measurements,
        },
        "input_source": str(source),
        "experiment": dict(experiment_metadata) if experiment_metadata is not None else None,
        "model_path": str(Path(model_path).expanduser().resolve()) if model_path else None,
        "counts": {
            "records": records_processed,
            "records_processed": records_processed,
            "valid_records": valid_records,
            "invalid_records": invalid_records,
            "invalid_telemetry_records": invalid_records,
            "baseline_plans_generated": baseline_plans,
            "adaptive_plans_generated": adaptive_plans,
            "adaptive_decision_failures": decision_errors,
            "decision_errors": decision_errors,
            "records_blocked_by_strict_quality_or_freshness": blocked_records,
        },
        "results": records,
    }
    write_exclusive_text(destination, json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


def _json_object_argument(value: str, argument: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"{argument} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError(f"{argument} must be a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a dry-run baseline/adaptive QoS comparison report."
    )
    parser.add_argument("--input", required=True, type=Path, help="Person 1 telemetry JSONL")
    parser.add_argument("--output", required=True, type=Path, help="New JSON report path")
    parser.add_argument(
        "--mode", required=True, choices=("baseline", "adaptive", "both")
    )
    parser.add_argument("--model", type=Path, help="Existing trained model for adaptive mode")
    parser.add_argument(
        "--flow-policy-classes",
        help='JSON mapping, e.g. {"5201":"gaming","5202":"video","5203":"web","5204":"bulk"}',
    )
    parser.add_argument(
        "--filter-priorities",
        help='JSON mapping, e.g. {"5201":10,"5202":20,"5203":30,"5204":40}',
    )
    parser.add_argument(
        "--strict-freshness",
        action="store_true",
        help=(
            "require measurement timestamps within 300 seconds and no more "
            "than 30 seconds in the future"
        ),
    )
    parser.add_argument(
        "--strict-queue-quality",
        action="store_true",
        help="require finite, nonnegative queue_size and queue_drops measurements",
    )
    args = parser.parse_args(argv)

    qos_config = None
    if args.mode in {"adaptive", "both"}:
        if args.model is None:
            parser.error("--model is required for adaptive and both modes")
        if args.flow_policy_classes is None or args.filter_priorities is None:
            parser.error(
                "adaptive modes require explicit flow-policy classes and filter priorities"
            )
        try:
            classes = _json_object_argument(
                args.flow_policy_classes, "--flow-policy-classes"
            )
            priorities = _json_object_argument(
                args.filter_priorities, "--filter-priorities"
            )
            qos_config = QoSIntegrationConfig(
                interface="s1-eth5",
                total_bandwidth_mbps=10.0,
                flow_policy_classes={int(port): value for port, value in classes.items()},
                filter_priorities={int(port): value for port, value in priorities.items()},
                dry_run=True,
            )
        except (ValueError, TypeError, argparse.ArgumentTypeError) as exc:
            parser.error(str(exc))
    try:
        report = run_experiment(
            args.input,
            args.output,
            mode=args.mode,
            model_path=args.model,
            qos_config=qos_config,
            enforce_freshness=args.strict_freshness,
            require_queue_measurements=args.strict_queue_quality,
        )
    except (OSError, ValueError, TypeError, FileExistsError) as exc:
        print(f"experiment runner failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "run_id": report["run_id"],
        "output": str(args.output.resolve()),
        "status": report["status"],
        **report["counts"],
    }))
    return 0 if report["status"] == "planned" else 1


if __name__ == "__main__":
    raise SystemExit(main())
