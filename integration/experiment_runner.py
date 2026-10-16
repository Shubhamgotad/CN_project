"""Offline baseline/adaptive comparison runner for Person 1 telemetry JSONL.

This module generates analysis and allocation plans only. It never applies TC
commands or invokes network/system utilities.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
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


def _error(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _reject_nonstandard_json_number(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant {value!r}")


def _record_window(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "telemetry-record",
        "timestamp": record.get("timestamp"),
        **{field: record.get(field) for field in WINDOW_FIELDS},
    }


def _baseline_result(prediction: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "status": "completed",
        "policy": BASELINE_POLICY,
        "allocation": {
            "status": "not-applicable",
            "reason": "the existing baseline shares the aggregate 10 Mbps class",
            "flow_allocations_mbps": None,
        },
        "congestion_prediction": prediction,
        "prediction_used_for_policy": False,
    }


def _adaptive_result(
    integration_result: Mapping[str, Any],
    input_features: Mapping[str, float],
) -> dict[str, Any]:
    decision = integration_result["qos_decision"]
    return {
        "status": "completed",
        "prediction": dict(integration_result["prediction"]),
        "input_features": dict(input_features),
        "policy_percentages": dict(decision["policy"]),
        "service_class_allocation_mbps": dict(decision["allocation_mbps"]),
        "flow_policy_classes": dict(integration_result["flow_policy_classes"]),
        "flow_allocations_mbps": dict(integration_result["flow_allocations_mbps"]),
        "planning_only": True,
        "tc_commands_executed": False,
    }


def run_experiment(
    input_path: str | Path,
    output_path: str | Path,
    *,
    mode: str,
    model_path: str | Path | None = None,
    qos_config: QoSIntegrationConfig | None = None,
) -> dict[str, Any]:
    """Read Person 1 JSONL, validate each record, and write a dry-run report.

    In ``both`` mode baseline and adaptive outputs are paired within each
    telemetry record. The source contains aggregate samples, not a verified
    synchronized multi-flow interval window; no cross-record aggregation is
    inferred here.
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

    source = Path(input_path).expanduser().resolve(strict=True)
    destination = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("input_path must be a JSONL file")
    if source == destination:
        raise ValueError("output_path must not overwrite the input telemetry file")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"output directory does not exist: {destination.parent}"
        )

    modes = ("baseline", "adaptive") if mode == "both" else (mode,)
    records: list[dict[str, Any]] = []
    valid_records = 0
    invalid_records = 0
    decision_errors = 0

    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            raw: Any = None
            try:
                raw = json.loads(
                    line, parse_constant=_reject_nonstandard_json_number
                )
                if not isinstance(raw, Mapping):
                    raise ValueError("telemetry JSONL record must be an object")
                adapted = adapt_person1_telemetry(
                    raw,
                    interface=qos_config.interface if qos_config else "s1-eth5",
                )
            except (json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
                invalid_records += 1
                records.append({
                    "line": line_number,
                    "status": "invalid",
                    "experiment_id": raw.get("experiment_id") if isinstance(raw, Mapping) else None,
                    "window": _record_window(raw) if isinstance(raw, Mapping) else None,
                    "measured": {
                        field: raw.get(field) if isinstance(raw, Mapping) else None
                        for field in MEASURED_FIELDS
                    },
                    "errors": [_error(exc)],
                })
                continue

            valid_records += 1
            input_features = dict(zip(
                FEATURE_COLUMNS, extract_features(adapted.record)
            ))
            measured = {field: raw.get(field) for field in MEASURED_FIELDS}
            mode_results: dict[str, Any] = {}
            adaptive_integration: Mapping[str, Any] | None = None
            adaptive_failure: Exception | None = None
            if include_adaptive:
                try:
                    adaptive_integration = build_qos_integration(
                        raw,
                        model_path=model_path,
                        config=qos_config,
                    )
                except Exception as exc:
                    adaptive_failure = exc
                    decision_errors += 1

            if "baseline" in modes:
                prediction = (
                    adaptive_integration["prediction"]
                    if adaptive_integration is not None
                    else None
                )
                mode_results["baseline"] = _baseline_result(prediction)
            if "adaptive" in modes:
                if adaptive_integration is None:
                    mode_results["adaptive"] = {
                        "status": "error",
                        "error": _error(adaptive_failure or RuntimeError(
                            "adaptive decision did not produce a result"
                        )),
                        "planning_only": True,
                        "tc_commands_executed": False,
                    }
                else:
                    mode_results["adaptive"] = _adaptive_result(
                        adaptive_integration, input_features
                    )

            records.append({
                "line": line_number,
                "status": "valid",
                "experiment_id": adapted.metadata.get("experiment_id"),
                "scenario": adapted.metadata.get("scenario"),
                "source": adapted.metadata.get("source"),
                "destination": adapted.metadata.get("destination"),
                "traffic_type": adapted.metadata.get("traffic_type"),
                "request": {field: raw.get(field) for field in REQUEST_FIELDS},
                "window": _record_window(raw),
                "measured": measured,
                "measurement_scope": {
                    "queue_size": "scenario-wide",
                    "queue_drops": "scenario-wide",
                },
                "modes": mode_results,
            })

    report = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "input_source": str(source),
        "model_path": str(Path(model_path).expanduser().resolve()) if model_path else None,
        "counts": {
            "records": valid_records + invalid_records,
            "valid_records": valid_records,
            "invalid_records": invalid_records,
            "decision_errors": decision_errors,
        },
        "dry_run": True,
        "results": records,
    }
    with destination.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
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
    parser.add_argument("--default-port", type=int)
    args = parser.parse_args(argv)

    qos_config = None
    if args.mode in {"adaptive", "both"}:
        if args.model is None:
            parser.error("--model is required for adaptive and both modes")
        if (
            args.flow_policy_classes is None
            or args.filter_priorities is None
            or args.default_port is None
        ):
            parser.error(
                "adaptive modes require explicit flow-policy classes, filter priorities, "
                "and default port"
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
                default_port=args.default_port,
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
        )
    except (OSError, ValueError, TypeError, FileExistsError) as exc:
        print(f"experiment runner failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "run_id": report["run_id"],
        "output": str(args.output.resolve()),
        **report["counts"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
