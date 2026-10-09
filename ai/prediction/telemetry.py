"""Validation and loading for the shared telemetry record schema."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


REQUIRED_FIELDS = (
    "timestamp",
    "interface",
    "throughput_mbps",
    "bandwidth_mbps",
    "utilization",
    "latency_ms",
    "jitter_ms",
    "packet_loss_pct",
    "active_flows",
)


@dataclass(frozen=True)
class TelemetryRecord:
    timestamp: Any
    interface: str
    throughput_mbps: float
    bandwidth_mbps: float
    utilization: float
    latency_ms: float
    jitter_ms: float
    packet_loss_pct: float
    active_flows: int

    @classmethod
    def from_mapping(cls, record: Mapping[str, Any]) -> "TelemetryRecord":
        if not isinstance(record, Mapping):
            raise TypeError("telemetry record must be a mapping")

        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            raise ValueError("missing required telemetry fields: " + ", ".join(missing))

        interface = record["interface"]
        if not isinstance(interface, str) or not interface.strip():
            raise ValueError("interface must be a non-empty string")

        values = {}
        for field in (
            "throughput_mbps",
            "bandwidth_mbps",
            "utilization",
            "latency_ms",
            "jitter_ms",
            "packet_loss_pct",
        ):
            value = record[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field} must be numeric")
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"{field} must be finite")
            values[field] = value

        active_flows = record["active_flows"]
        if isinstance(active_flows, bool) or not isinstance(active_flows, int):
            raise ValueError("active_flows must be a non-negative integer")

        if values["throughput_mbps"] < 0:
            raise ValueError("throughput_mbps cannot be negative")
        if values["bandwidth_mbps"] < 0:
            raise ValueError("bandwidth_mbps cannot be negative")
        if values["utilization"] < 0 or values["utilization"] > 100:
            raise ValueError("utilization must be between 0 and 100")
        if values["latency_ms"] < 0:
            raise ValueError("latency_ms cannot be negative")
        if values["jitter_ms"] < 0:
            raise ValueError("jitter_ms cannot be negative")
        if values["packet_loss_pct"] < 0 or values["packet_loss_pct"] > 100:
            raise ValueError("packet_loss_pct must be between 0 and 100")
        if active_flows < 0:
            raise ValueError("active_flows cannot be negative")

        return cls(
            timestamp=record["timestamp"],
            interface=interface,
            active_flows=active_flows,
            **values,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in REQUIRED_FIELDS
        }


def validate_telemetry(record: Mapping[str, Any]) -> TelemetryRecord:
    """Validate one record and return its typed representation."""
    return TelemetryRecord.from_mapping(record)


def load_telemetry_json(source: str | Path | Sequence[Mapping[str, Any]]) -> list[TelemetryRecord]:
    """Load and validate a JSON object, JSON array, JSON file, or record sequence."""
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray, Path)):
        payload = list(source)
    else:
        source_text = str(source)
        try:
            path = Path(source_text)
            is_file = path.exists()
        except OSError:
            is_file = False
        if is_file:
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = json.loads(source_text)

    if isinstance(payload, Mapping):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("telemetry JSON must contain an object or array of objects")
    return [validate_telemetry(record) for record in payload]
