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
from qos.allocator import plan_managed_htb
from qos.policy import build_qos_decision_from_prediction
from qos.tc_controller import (
    FLOW_PORTS,
    MANAGED_FILTER_PRIORITIES,
    ManagedHTBConfig,
    apply_managed_htb_policy,
    generate_managed_htb_commands,
)
from qos.policy import TRAFFIC_CLASSES
from qos.validator import validate_interface, validate_total_bandwidth

@dataclass(frozen=True)
class QoSIntegrationConfig:
    """Caller-approved mapping from four UDP flows to four policy classes."""

    interface: str
    total_bandwidth_mbps: float
    flow_policy_classes: Mapping[int, str] | None = None
    filter_priorities: Mapping[int, int] | None = None
    neutral_flow_filters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    dry_run: bool = True

    def __post_init__(self) -> None:
        validate_interface(self.interface)
        if self.interface != "s1-eth5":
            raise ValueError("client-to-server adaptive QoS must target s1-eth5 egress")
        validate_total_bandwidth(self.total_bandwidth_mbps)
        if self.total_bandwidth_mbps != 10:
            raise ValueError("the existing aggregate bottleneck must remain 10 Mbps")
        if not isinstance(self.dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        for flow_id, filter_config in self.neutral_flow_filters.items():
            if not isinstance(flow_id, str) or not flow_id:
                raise ValueError("neutral flow identifiers must be non-empty strings")
            if not isinstance(filter_config, Mapping):
                raise ValueError(f"filter for {flow_id} must be a mapping")
        if self.flow_policy_classes is not None:
            self._validate_flow_policy_classes()
        if self.filter_priorities is not None:
            self._validate_filter_priorities()
    def _validate_flow_policy_classes(self) -> None:
        mapping = self.flow_policy_classes
        if not isinstance(mapping, Mapping) or set(mapping) != set(FLOW_PORTS):
            raise ValueError("flow_policy_classes must map every port 5201-5204")
        if any(not isinstance(value, str) for value in mapping.values()):
            raise ValueError("flow policy class names must be strings")
        expected = dict(zip(FLOW_PORTS, TRAFFIC_CLASSES))
        if dict(mapping) != expected:
            raise ValueError(
                "flow_policy_classes must map 5201-5204 to gaming, video, web, and bulk"
            )

    def _validate_filter_priorities(self) -> None:
        priorities = self.filter_priorities
        if not isinstance(priorities, Mapping) or set(priorities) != set(FLOW_PORTS):
            raise ValueError("filter_priorities must explicitly cover ports 5201-5204")
        values = list(priorities.values())
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in values)
            or dict(priorities) != MANAGED_FILTER_PRIORITIES
        ):
            raise ValueError("filter priorities must be 10, 20, 30, and 40 for ports 5201-5204")

    def require_complete_policy_mapping(self) -> None:
        if self.flow_policy_classes is None:
            raise ValueError("explicit flow-to-policy-class mapping is required")
        if self.filter_priorities is None:
            raise ValueError("explicit per-flow filter priorities are required")


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
    config.require_complete_policy_mapping()
    adapted = adapt_person1_telemetry(telemetry, interface=config.interface)
    prediction = CongestionPredictor(model_path).predict(adapted.record)
    decision = build_qos_decision_from_prediction(
        prediction, config.total_bandwidth_mbps
    )
    managed_config = plan_managed_htb(
        prediction["congestion"], interface=config.interface
    )
    flow_allocations = {
        port: managed_config.service_allocations_mbps[config.flow_policy_classes[port]]
        for port in FLOW_PORTS
    }
    commands = generate_managed_htb_commands(managed_config)
    return {
        "adapted": adapted,
        "prediction": prediction,
        "qos_decision": decision,
        "tc_commands": commands,
        "dry_run": config.dry_run,
        "interface": config.interface,
        "total_bandwidth_mbps": config.total_bandwidth_mbps,
        "managed_htb_config": {
            "service_allocations_mbps": dict(managed_config.service_allocations_mbps),
            "unmatched_rate_mbps": managed_config.unmatched_rate_mbps,
            "unmatched_ceil_mbps": managed_config.unmatched_ceil_mbps,
            "default_class_id": managed_config.default_class_id,
            "filter_priorities": dict(managed_config.filter_priorities),
        },
        "_managed_htb_config": managed_config,
        "flow_policy_classes": dict(config.flow_policy_classes),
        "flow_allocations_mbps": flow_allocations,
        "filter_priorities": dict(managed_config.filter_priorities),
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
    config = integration_result.get("_managed_htb_config")
    if not isinstance(config, ManagedHTBConfig):
        raise ValueError("integration result is missing validated hierarchy settings")
    if list(commands) != generate_managed_htb_commands(config):
        raise ValueError("integration commands do not match the validated managed HTB plan")
    return apply_managed_htb_policy(config, runner=runner)
