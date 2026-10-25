"""Read-only rule-based QoS decision planning with profile stabilization."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Any

from ai.prediction.labeling import DEFAULT_THRESHOLDS, congestion_score, label_congestion
from ai.prediction.telemetry import TelemetryRecord, validate_telemetry
from qos.allocator import plan_managed_htb
from qos.tc_controller import (
    FLOW_PORTS,
    NETEM_DELAY,
    NETEM_LIMIT,
    SERVICE_CLASS_IDS,
    UNMATCHED_CLASS_ID,
    UNMATCHED_NETEM_HANDLE,
    generate_managed_htb_commands,
)


HYSTERESIS_SAMPLES = 2
COOLDOWN_SECONDS = 30.0
HISTORY_LIMIT = 20


class DecisionLoop:
    """Stateful proposal selector. This module has no network command runner."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._selected_profile: str | None = None
        self._candidate_profile: str | None = None
        self._candidate_samples = 0
        self._seen_sample_keys: set[str] = set()
        self._seen_sample_order: deque[str] = deque()
        self._last_change_monotonic: float | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)

    def evaluate(self, telemetry: Any, *, now_monotonic: float | None = None) -> dict[str, Any]:
        with self._lock:
            if telemetry.status != "available" or telemetry.record is None:
                return self._unavailable(telemetry)

            record_data = telemetry.record.model_dump()
            if record_data.get("interface") != "s1-eth5":
                return self._unavailable(
                    telemetry,
                    message="Telemetry interface is not supported by the existing managed QoS profiles.",
                )
            required = ("bandwidth_mbps", "utilization_percent", "active_flows")
            if any(record_data.get(field) is None for field in required):
                return self._unavailable(
                    telemetry,
                    message="Validated telemetry is missing fields required for congestion classification.",
                )

            try:
                record = validate_telemetry({
                    "timestamp": record_data["timestamp"],
                    "interface": record_data["interface"],
                    "throughput_mbps": record_data["throughput_mbps"],
                    "bandwidth_mbps": record_data["bandwidth_mbps"],
                    "utilization": record_data["utilization_percent"],
                    "latency_ms": record_data["latency_ms"],
                    "jitter_ms": record_data["jitter_ms"],
                    "packet_loss_pct": record_data["packet_loss_percent"],
                    "active_flows": record_data["active_flows"],
                })
            except (TypeError, ValueError, KeyError):
                return self._unavailable(telemetry, message="Telemetry is invalid for congestion classification.")

            classification = label_congestion(record)
            score = congestion_score(record)
            sample_key = f"{record_data['timestamp']}|{record_data['source']}|{record_data['scenario']}"
            is_new_sample = sample_key not in self._seen_sample_keys
            if is_new_sample:
                if len(self._seen_sample_order) >= 256:
                    self._seen_sample_keys.discard(self._seen_sample_order.popleft())
                self._seen_sample_order.append(sample_key)
                self._seen_sample_keys.add(sample_key)
            current_monotonic = monotonic() if now_monotonic is None else now_monotonic
            transition = "stable"
            cooldown_remaining = 0.0
            if self._selected_profile is None:
                self._selected_profile = classification
                self._last_change_monotonic = current_monotonic
                transition = "initial_profile_selected"
            elif classification != self._selected_profile:
                if classification == self._candidate_profile:
                    if is_new_sample:
                        self._candidate_samples += 1
                else:
                    self._candidate_profile = classification
                    self._candidate_samples = 1 if is_new_sample else 0
                last_change = self._last_change_monotonic
                elapsed = current_monotonic - (
                    current_monotonic if last_change is None else last_change
                )
                cooldown_remaining = max(0.0, COOLDOWN_SECONDS - elapsed)
                if self._candidate_samples < HYSTERESIS_SAMPLES:
                    transition = "awaiting_hysteresis"
                else:
                    if cooldown_remaining > 0:
                        transition = "cooldown"
                    elif is_new_sample:
                        self._selected_profile = classification
                        self._last_change_monotonic = current_monotonic
                        self._candidate_profile = None
                        self._candidate_samples = 0
                        transition = "profile_changed"
                    else:
                        transition = "awaiting_fresh_sample"
            else:
                self._candidate_profile = None
                self._candidate_samples = 0
            config = plan_managed_htb(self._selected_profile, interface=record.interface)
            commands = generate_managed_htb_commands(config)
            reasons = self._reasons(record)
            result = {
                "status": "ready",
                "dry_run": True,
                "applied": False,
                "telemetry_status": telemetry.status,
                "telemetry_source": telemetry.source,
                "measurement_age_seconds": telemetry.measurement_age_seconds,
                "measurement": {
                    "timestamp": record_data["timestamp"],
                    "experiment_id": record_data.get("experiment_id"),
                    "source": record_data["source"],
                    "scenario": record_data["scenario"],
                    "interface": record.interface,
                    "throughput_mbps": record.throughput_mbps,
                    "bandwidth_mbps": record.bandwidth_mbps,
                    "utilization_percent": record.utilization,
                    "latency_ms": record.latency_ms,
                    "jitter_ms": record.jitter_ms,
                    "packet_loss_percent": record.packet_loss_pct,
                    "active_flows": record.active_flows,
                    "queue_size": record_data.get("queue_size"),
                    "queue_drops": record_data.get("queue_drops"),
                },
                "classifier": "existing_rule_based_thresholds",
                "classification": classification,
                "score": score,
                "selected_profile": self._selected_profile,
                "candidate_profile": self._candidate_profile,
                "transition": transition,
                "hysteresis_samples_required": HYSTERESIS_SAMPLES,
                "hysteresis_samples_observed": self._candidate_samples,
                "cooldown_seconds": COOLDOWN_SECONDS,
                "cooldown_remaining_seconds": round(cooldown_remaining, 3),
                "reasons": reasons,
                "thresholds": {
                    "utilization_medium_percent": DEFAULT_THRESHOLDS.utilization_medium,
                    "utilization_high_percent": DEFAULT_THRESHOLDS.utilization_high,
                    "latency_medium_ms": DEFAULT_THRESHOLDS.latency_medium_ms,
                    "latency_high_ms": DEFAULT_THRESHOLDS.latency_high_ms,
                    "packet_loss_medium_percent": DEFAULT_THRESHOLDS.packet_loss_medium_pct,
                    "packet_loss_high_percent": DEFAULT_THRESHOLDS.packet_loss_high_pct,
                },
                "proposed_policy": {
                    "interface": config.interface,
                    "total_bandwidth_mbps": config.total_bandwidth_mbps,
                    "service_allocations_mbps": dict(config.service_allocations_mbps),
                    "service_ceilings_mbps": dict(config.service_allocations_mbps),
                    "service_class_ids": dict(SERVICE_CLASS_IDS),
                    "flow_mappings": [
                        {
                            "port": port,
                            "service": service,
                            "class_id": SERVICE_CLASS_IDS[service],
                            "priority": config.filter_priorities[port],
                        }
                        for port, service in zip(FLOW_PORTS, SERVICE_CLASS_IDS)
                    ],
                    "unmatched_rate_mbps": config.unmatched_rate_mbps,
                    "unmatched_ceil_mbps": config.unmatched_ceil_mbps,
                    "default_class_id": config.default_class_id,
                    "unmatched_class_id": UNMATCHED_CLASS_ID,
                    "filter_priorities": dict(config.filter_priorities),
                    "netem_delay": NETEM_DELAY,
                    "netem_limit": NETEM_LIMIT,
                    "unmatched_netem_handle": UNMATCHED_NETEM_HANDLE,
                },
                "tc_commands": commands,
                "history": list(self._history),
                "message": "Policy is a dry-run proposal only; no network configuration was changed.",
            }
            self._history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "measurement_timestamp": record_data["timestamp"],
                "classification": classification,
                "selected_profile": self._selected_profile,
                "transition": transition,
            })
            result["history"] = list(self._history)
            return result

    @staticmethod
    def _reasons(record: TelemetryRecord) -> list[str]:
        thresholds = DEFAULT_THRESHOLDS
        return [
            f"Utilization {record.utilization:.2f}%: medium threshold {thresholds.utilization_medium:.1f}%, high threshold {thresholds.utilization_high:.1f}%.",
            f"Latency {record.latency_ms:.4f} ms: medium threshold {thresholds.latency_medium_ms:.1f} ms, high threshold {thresholds.latency_high_ms:.1f} ms.",
            f"Packet loss {record.packet_loss_pct:.4f}%: medium threshold {thresholds.packet_loss_medium_pct:.1f}%, high threshold {thresholds.packet_loss_high_pct:.1f}%.",
        ]

    @staticmethod
    def _unavailable(telemetry: Any, *, message: str | None = None) -> dict[str, Any]:
        return {
            "status": telemetry.status if telemetry.status in {"stale", "unavailable"} else "unavailable",
            "dry_run": True,
            "applied": False,
            "telemetry_status": telemetry.status,
            "telemetry_source": telemetry.source,
            "measurement_age_seconds": telemetry.measurement_age_seconds,
            "measurement": None,
            "classifier": "existing_rule_based_thresholds",
            "classification": None,
            "score": None,
            "selected_profile": None,
            "candidate_profile": None,
            "transition": "no_decision",
            "hysteresis_samples_required": HYSTERESIS_SAMPLES,
            "hysteresis_samples_observed": 0,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "cooldown_remaining_seconds": 0.0,
            "reasons": [],
            "proposed_policy": None,
            "tc_commands": [],
            "history": [],
            "message": message or telemetry.message,
        }


decision_loop = DecisionLoop()
