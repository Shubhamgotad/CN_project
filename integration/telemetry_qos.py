"""Configurable Person 1 telemetry adapter and Stage 1/2 integration boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from ai.prediction.congestion_predictor import CongestionPredictor
from ai.prediction.telemetry import TelemetryRecord, validate_telemetry
from qos.policy import build_qos_decision_from_prediction
from qos.tc_controller import apply_tc_commands, generate_tc_commands
from qos.validator import validate_interface, validate_total_bandwidth

PERSON1_METADATA_FIELDS = (
    "experiment_id",
    "scenario",
    "source",
    "destination",
    "traffic_type",
    "requested_mbps",
    "lost_packets",
    "total_packets",
    "queue_size",
    "queue_drops",
)

PERSON1_REQUIRED_FIELDS = (
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


@dataclass(frozen=True)
class AdaptedTelemetry:
    """Validated Stage 1 telemetry plus Person 1 metadata kept out of ML features."""

    record: TelemetryRecord
    metadata: dict[str, Any]


@dataclass(frozen=True)
class QoSIntegrationConfig:
    """External settings; neutral flows are not translated to QoS classes."""

    interface: str
    total_bandwidth_mbps: float
    traffic_filters: Mapping[str, Mapping[str, Any] | Sequence[Mapping[str, Any]]] = field(
        default_factory=dict
    )
    neutral_flow_filters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    dry_run: bool = True

    def __post_init__(self) -> None:
        validate_interface(self.interface)
        validate_total_bandwidth(self.total_bandwidth_mbps)
        if not isinstance(self.dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        for flow_id, filter_config in self.neutral_flow_filters.items():
            if not isinstance(flow_id, str) or not flow_id:
                raise ValueError("neutral flow identifiers must be non-empty strings")
            if not isinstance(filter_config, Mapping):
                raise ValueError(f"filter for {flow_id} must be a mapping")


def _timestamp_to_epoch(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"malformed ISO-8601 timestamp: {value!r}") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return timestamp.timestamp()


def adapt_person1_telemetry(
    telemetry: Mapping[str, Any],
    *,
    interface: str,
) -> AdaptedTelemetry:
    """Adapt one documented Person 1 record without assigning application classes."""
    if not isinstance(telemetry, Mapping):
        raise TypeError("Person 1 telemetry must be a mapping")
    missing = [field for field in PERSON1_REQUIRED_FIELDS if field not in telemetry]
    if missing:
        raise ValueError("missing Person 1 telemetry fields: " + ", ".join(missing))
    validate_interface(interface)

    adapted = {
        "timestamp": _timestamp_to_epoch(telemetry["timestamp"]),
        "interface": interface,
        "throughput_mbps": telemetry["throughput_mbps"],
        "bandwidth_mbps": telemetry["bandwidth_mbps"],
        "utilization": telemetry["utilization_percent"],
        "latency_ms": telemetry["latency_ms"],
        "jitter_ms": telemetry["jitter_ms"],
        "packet_loss_pct": telemetry["packet_loss_percent"],
        "active_flows": telemetry["active_flows"],
    }
    record = validate_telemetry(adapted)
    metadata = {field: telemetry[field] for field in PERSON1_METADATA_FIELDS}
    return AdaptedTelemetry(record=record, metadata=metadata)


def read_person1_jsonl(
    source: str | Path,
    *,
    interface: str,
) -> list[AdaptedTelemetry]:
    """Read JSON Lines and report malformed records with their source line number."""
    path = Path(source)
    results = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                results.append(adapt_person1_telemetry(payload, interface=interface))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid Person 1 telemetry at line {line_number}: {exc}"
                ) from exc
    return results


def build_qos_integration(
    telemetry: Mapping[str, Any],
    *,
    model_path: str | Path,
    config: QoSIntegrationConfig,
) -> dict[str, Any]:
    """Build a decision and commands without executing them.

    ``neutral_flow_filters`` remain separate metadata. They are never translated
    into ``gaming``, ``video``, ``web``, or ``bulk`` tc filters without an
    explicit caller-provided application-class mapping.
    """
    adapted = adapt_person1_telemetry(telemetry, interface=config.interface)
    prediction = CongestionPredictor(model_path).predict(adapted.record)
    decision = build_qos_decision_from_prediction(
        prediction, config.total_bandwidth_mbps
    )
    commands = generate_tc_commands(
        config.interface,
        config.total_bandwidth_mbps,
        decision["allocation_mbps"],
        config.traffic_filters,
    )
    return {
        "adapted": adapted,
        "prediction": prediction,
        "qos_decision": decision,
        "tc_commands": commands,
        "dry_run": config.dry_run,
        "neutral_flow_filters": dict(config.neutral_flow_filters),
    }


def apply_integration_commands(
    integration_result: Mapping[str, Any],
    *,
    runner: Any = None,
) -> list[Any]:
    """Explicitly apply prepared commands; dry-run results cannot execute.

    This is the only integration entry point that can invoke a command runner.
    """
    if integration_result.get("dry_run", True):
        raise RuntimeError("cannot apply tc commands while dry_run is enabled")
    commands = integration_result.get("tc_commands")
    if not isinstance(commands, Sequence):
        raise ValueError("integration result does not contain tc commands")
    return apply_tc_commands(commands, runner=runner)
