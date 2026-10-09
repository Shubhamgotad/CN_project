"""Configurable Person 1 telemetry adapter and Stage 1/2 integration boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai.prediction.congestion_predictor import CongestionPredictor
from ai.prediction.telemetry import (
    AdaptedTelemetry,
    PERSON1_METADATA_FIELDS,
    PERSON1_REQUIRED_FIELDS,
    TelemetryRecord,
    adapt_person1_telemetry as _adapt_person1_telemetry,
    read_person1_jsonl as _read_person1_jsonl,
)
from qos.policy import build_qos_decision_from_prediction
from qos.tc_controller import apply_tc_commands, generate_tc_commands
from qos.validator import validate_interface, validate_total_bandwidth

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


def adapt_person1_telemetry(
    telemetry: Mapping[str, Any],
    *,
    interface: str,
) -> AdaptedTelemetry:
    """Adapt one Person 1 record after validating its configured interface."""
    validate_interface(interface)
    return _adapt_person1_telemetry(telemetry, interface=interface)


def read_person1_jsonl(
    source: str | Path,
    *,
    interface: str,
) -> list[AdaptedTelemetry]:
    """Read Person 1 JSONL after validating the configured interface."""
    validate_interface(interface)
    return _read_person1_jsonl(source, interface=interface)


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
