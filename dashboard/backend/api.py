"""Adaptive QoS dashboard API with allowlisted, guarded traffic execution."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai.prediction.telemetry import adapt_person1_telemetry
from dashboard.backend.decision_loop import decision_loop


API_VERSION = "0.1.0"
MAX_REPORT_BYTES = 5 * 1024 * 1024
MAX_TELEMETRY_BYTES = 5 * 1024 * 1024
MAX_TELEMETRY_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
MAX_TELEMETRY_HISTORY_RECORDS = 500
MAX_TELEMETRY_HISTORY_RUNS = 100
MAX_TELEMETRY_HISTORY_SCAN_RECORDS = 20_000
MAX_TELEMETRY_HISTORY_BYTES = 25 * 1024 * 1024
EXPERIMENT_RUN_ROOT = Path(__file__).resolve().parents[2].parent / ".adaptive-qos-runs"
REPORT_DIRECTORY_ENV = "ADAPTIVE_QOS_REPORT_DIR"
TELEMETRY_FILE_ENV = "ADAPTIVE_QOS_TELEMETRY_FILE"
QOS_APPLICATION_ENV = "ADAPTIVE_QOS_ENABLE_APPLICATION"
REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
EXPERIMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"
    service: str = "adaptive-qos-dashboard-api"
    api_version: str = API_VERSION
    live_network_operations: Literal[True] = True


class ExperimentStartRequest(APIModel):
    scenario: Literal["normal", "moderate", "congestion", "burst"]
    source_host: Literal["h1", "h2", "h3", "h4", "all"] | None = None
    rate_mbps: float | None = Field(default=None, ge=0.1, le=10)
    duration_seconds: int | None = Field(default=None, ge=1, le=60)

    @model_validator(mode="after")
    def validate_scenario_parameters(self):
        if self.source_host == "all" and self.scenario != "congestion":
            raise ValueError("All flows are supported only for the congestion scenario.")
        if self.rate_mbps is not None and abs(self.rate_mbps * 10 - round(self.rate_mbps * 10)) > 1e-8:
            raise ValueError("Rate must use 0.1 Mbps increments.")
        if self.scenario == "burst":
            if self.rate_mbps not in (None, 8, 8.0) or self.duration_seconds not in (None, 23):
                raise ValueError("Burst uses a fixed 8 Mbps flow schedule lasting 23 seconds (five 3-second bursts with 2-second gaps).")
        return self


class ExperimentResponse(APIModel):
    experiment_id: str
    scenario: Literal["normal", "moderate", "congestion", "burst"]
    source_host: Literal["h1", "h2", "h3", "h4", "all"] | None = None
    status: Literal["starting", "running", "stopping", "completed", "failed", "stopped"]
    flows: list[str]
    target_rate_mbps: float
    duration_seconds: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float
    process_alive: bool
    can_stop: bool
    process_exit_code: int | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_execution_sources(self):
        if self.source_host is None:
            return self
        expected_flows = (
            ["h1", "h2", "h3", "h4"]
            if self.source_host == "all" and self.scenario == "congestion"
            else [self.source_host]
        )
        if self.flows != expected_flows:
            raise ValueError("Execution source host and planned flows do not match.")
        return self


class ExperimentStartResponse(ExperimentResponse):
    pass


class TopologyHost(APIModel):
    id: str
    role: Literal["client", "server"]
    service: str | None = None
    udp_destination_port: int | None = None
    ipv4_address: str | None = None


class TopologyLink(APIModel):
    source: str
    target: str
    bandwidth_mbps: float | None = None
    delay: str | None = None


class TopologyResponse(APIModel):
    status: Literal["configured"] = "configured"
    source: str = "repository topology and traffic configuration"
    live_connectivity_verified: Literal[False] = False
    bottleneck_interface: str = "s1-eth5"
    hosts: list[TopologyHost]
    switches: list[str]
    links: list[TopologyLink]


class TelemetryRecordResponse(APIModel):
    timestamp: str
    source: str
    scenario: str
    throughput_mbps: float
    latency_ms: float | None
    jitter_ms: float | None
    packet_loss_percent: float | None
    queue_size: int | None
    queue_drops: int | None
    measurement_direction: str | None
    interface: str = "s1-eth5"
    bandwidth_mbps: float | None = None
    utilization_percent: float | None = None
    active_flows: int | None = None


class LatestTelemetryResponse(APIModel):
    status: Literal["available", "stale", "unavailable"]
    source: Literal["configured_person1_jsonl", "completed_experiment_person1_jsonl", "none"]
    measurement_age_seconds: float | None = None
    stale_after_seconds: int = MAX_TELEMETRY_AGE_SECONDS
    message: str
    record: TelemetryRecordResponse | None = None


class TelemetryHistoryRecordResponse(APIModel):
    timestamp: str
    experiment_id: str
    scenario: str
    source: str
    destination: str
    traffic_type: str
    requested_mbps: float
    throughput_mbps: float
    latency_ms: float | None
    jitter_ms: float | None
    packet_loss_percent: float | None
    lost_packets: int
    total_packets: int
    bandwidth_mbps: float
    utilization_percent: float
    active_flows: int
    queue_size: int | None
    queue_drops: int | None
    measurement_direction: str | None = None
    interface: str = "s1-eth5"
    provenance: Literal["configured_person1_jsonl", "completed_experiment_person1_jsonl"]


class TelemetryHistoryResponse(APIModel):
    status: Literal["available", "unavailable"]
    message: str
    source_kinds: list[Literal["configured_person1_jsonl", "completed_experiment_person1_jsonl"]]
    host_filter: str | None = None
    scenario_filter: str | None = None
    limit: int
    total_records: int
    truncated: bool
    rejected_records: int
    records: list[TelemetryHistoryRecordResponse]


class DryRunDecisionResponse(APIModel):
    status: Literal["ready", "stale", "unavailable"]
    dry_run: Literal[True] = True
    applied: Literal[False] = False
    execution_state: Literal["dry_run"] = "dry_run"
    application_enabled: bool = False
    telemetry_status: Literal["available", "stale", "unavailable"]
    telemetry_source: Literal["configured_person1_jsonl", "completed_experiment_person1_jsonl", "none"]
    measurement_age_seconds: float | None = None
    measurement: dict[str, Any] | None = None
    classifier: Literal["existing_rule_based_thresholds"]
    classification: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    score: int | None = None
    selected_profile: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    candidate_profile: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    transition: str
    hysteresis_samples_required: int
    hysteresis_samples_observed: int
    cooldown_seconds: float
    cooldown_remaining_seconds: float
    reasons: list[str]
    proposed_policy: dict[str, Any] | None = None
    tc_commands: list[str]
    history: list[dict[str, Any]]
    message: str


class QosApplyRequest(APIModel):
    profile: Literal["LOW", "MEDIUM", "HIGH"]
    measurement_timestamp: str = Field(min_length=1, max_length=64)
    authorization: Literal["APPLY_QOS_PROFILE"]


class QosApplyResponse(APIModel):
    status: Literal["applied_verified"]
    dry_run: Literal[False] = False
    applied: Literal[True] = True
    profile: Literal["LOW", "MEDIUM", "HIGH"]
    interface: Literal["s1-eth5"]
    measurement_timestamp: str
    verification: Literal["kernel_readback_verified"]
    message: str


class QosApplicationStatusResponse(APIModel):
    status: Literal["verified", "unrecognized", "unavailable"]
    applied_profile: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    interface: Literal["s1-eth5"] = "s1-eth5"
    message: str


class ReportSummary(APIModel):
    report_id: str
    report_type: Literal[
        "experiment-runner", "evidence-analysis", "campaign-validation", "unknown"
    ]
    status: str
    campaign_status: str | None = None
    alignment_eligibility: str
    comparison_ready: bool


class ReportsResponse(APIModel):
    status: Literal["available", "unavailable"]
    message: str
    reports: list[ReportSummary] = Field(default_factory=list)


class MetricSummary(APIModel):
    throughput_mbps: float | None = None
    latency_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_percent: float | None = None


class RunnerPlanRecord(APIModel):
    line: int
    status: str
    experiment_id: str | None = None
    scenario: str | None = None
    source: str | None = None
    timestamp: str | None = None
    freshness_status: str
    freshness_age_seconds: float | None = None
    prediction: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    confidence: float | None = None
    input_features: dict[str, float] = Field(default_factory=dict)
    measured: MetricSummary
    service_allocations_mbps: dict[str, float] = Field(default_factory=dict)
    flow_allocations_mbps: dict[str, float] = Field(default_factory=dict)
    planning_only: Literal[True] = True


class ReportFlow(APIModel):
    flow_id: str
    status: str
    metrics: MetricSummary


class ReportTrial(APIModel):
    trial_id: str
    condition: str
    profile: str
    repetition_id: str
    status: str
    alignment_status: str
    boundary_metadata_status: str
    boundary_evidence_integrity_status: str
    producer_provenance_status: str
    clock_mapping_status: str
    flows: list[ReportFlow] = Field(default_factory=list)


class ReportDetailResponse(APIModel):
    report_id: str
    report_type: Literal["experiment-runner", "evidence-analysis", "campaign-validation"]
    created_at: str | None = None
    status: str
    planning_only: bool
    campaign_status: str | None = None
    complete_cycle_count: int | None = None
    minimum_complete_cycles: int | None = None
    boundary_metadata_status: str
    boundary_evidence_integrity_status: str
    producer_provenance_status: str
    clock_mapping_status: str
    alignment_eligibility: str
    comparison_ready: bool
    runner_records: list[RunnerPlanRecord] = Field(default_factory=list)
    trials: list[ReportTrial] = Field(default_factory=list)


class ExperimentsStatusResponse(APIModel):
    status: Literal["available", "unavailable"]
    report_id: str | None = None
    campaign_completeness: str
    complete_cycle_count: int | None = None
    minimum_complete_cycles: int | None = None
    boundary_metadata_status: str
    boundary_evidence_integrity_status: str
    producer_provenance_status: str
    clock_mapping_status: str
    measurement_alignment_eligibility: str
    comparison_ready: bool
    message: str


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _safe_text(value: Any, *, max_length: int = 120) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if "/" in value or "\\" in value or "\x00" in value:
        return None
    return value


def _safe_id(value: Any) -> str | None:
    text = _safe_text(value)
    return text if text is not None and REPORT_ID_PATTERN.fullmatch(text) else None


def _json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not allowed")


def _read_json_file(path: Path, root: Path, *, max_bytes: int) -> Any:
    try:
        resolved = path.resolve(strict=True)
        if resolved.parent != root:
            raise FileNotFoundError
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise FileNotFoundError
        if before.st_size > max_bytes:
            raise HTTPException(status_code=413, detail="Configured JSON file exceeds the size limit.")

        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved, flags)
        try:
            opened = os.fstat(descriptor)
            after = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(after.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise FileNotFoundError
            if opened.st_size > max_bytes:
                raise HTTPException(status_code=413, detail="Configured JSON file exceeds the size limit.")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                content = stream.read(max_bytes + 1)
            if len(content.encode("utf-8")) > max_bytes:
                raise HTTPException(status_code=413, detail="Configured JSON file exceeds the size limit.")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Configured report was not found.") from exc
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Configured report was not found.") from exc
    try:
        return json.loads(content, parse_constant=_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Configured JSON report is malformed.") from exc


def _read_configured_telemetry_text(path: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise FileNotFoundError
        if before.st_size > MAX_TELEMETRY_BYTES:
            raise ValueError
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved, flags)
        try:
            opened = os.fstat(descriptor)
            after = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(after.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size > MAX_TELEMETRY_BYTES
            ):
                raise FileNotFoundError
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(MAX_TELEMETRY_BYTES + 1)
            if len(content) > MAX_TELEMETRY_BYTES:
                raise ValueError
            return content.decode("utf-8")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ValueError("configured telemetry is unavailable or unsafe") from exc


def _telemetry_timestamp(payload: Any) -> datetime:
    """Validate a Person-1 row and return its real measurement timestamp."""
    adapt_person1_telemetry(payload, interface="s1-eth5")
    value = payload["timestamp"]
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("telemetry timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _completed_experiment_telemetry(
    *,
    max_run_directories: int | None = None,
    max_total_bytes: int | None = None,
    max_records: int | None = None,
    rejected_record_counter: list[int] | None = None,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Read valid records only from successful, completed guarded runs.

    Paths are derived from this repository's fixed run root; output_directory
    stored in WSL state is intentionally never treated as a Windows path.
    """
    root = EXPERIMENT_RUN_ROOT
    if root.is_symlink() or not root.is_dir():
        return []
    try:
        run_directories = list(root.iterdir())
    except OSError:
        return []

    run_directories = [
        path for path in run_directories
        if EXPERIMENT_ID_PATTERN.fullmatch(path.name) and not path.is_symlink() and path.is_dir()
    ]
    if max_run_directories is not None and len(run_directories) > max_run_directories:
        raise HTTPException(status_code=503, detail="Telemetry history exceeds the supported experiment-directory scan limit.")

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    total_bytes = 0
    for run_dir in run_directories:
        state_path = run_dir / "state.json"
        output_dir = run_dir / "output"
        try:
            if state_path.is_symlink() or not state_path.is_file():
                continue
            state_content = _read_configured_telemetry_text(state_path)
            total_bytes += len(state_content.encode("utf-8"))
            if max_total_bytes is not None and total_bytes > max_total_bytes:
                raise HTTPException(status_code=503, detail="Telemetry history exceeds the supported read-size limit.")
            state = json.loads(state_content)
            if (
                not isinstance(state, dict)
                or state.get("experiment_id") != run_dir.name
                or state.get("status") != "completed"
                or state.get("process_alive") is not False
                or state.get("process_exit_code") != 0
                or state.get("scenario") not in {"normal", "moderate", "congestion", "burst"}
                or output_dir.is_symlink()
                or not output_dir.is_dir()
            ):
                continue
            telemetry_path = output_dir / f"{state['scenario']}.jsonl"
            if telemetry_path.is_symlink() or not telemetry_path.is_file():
                continue
            content = _read_configured_telemetry_text(telemetry_path)
            total_bytes += len(content.encode("utf-8"))
            if max_total_bytes is not None and total_bytes > max_total_bytes:
                raise HTTPException(status_code=503, detail="Telemetry history exceeds the supported read-size limit.")
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            continue

        # A final unterminated line may be a concurrent/partial write. Completed
        # producer files end each JSONL row with a newline; ignore that tail.
        complete_content = content[: content.rfind("\n") + 1]
        for line in complete_content.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line, parse_constant=_json_constant)
                timestamp = _telemetry_timestamp(payload)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                if rejected_record_counter is not None:
                    rejected_record_counter[0] += 1
                continue
            candidates.append((timestamp, payload))
            if max_records is not None and len(candidates) > max_records:
                raise HTTPException(status_code=503, detail="Telemetry history exceeds the supported record-scan limit.")
    return candidates


def _complete_jsonl_records(content: str) -> tuple[list[tuple[datetime, dict[str, Any]]], int]:
    """Validate complete JSONL lines and ignore an unterminated concurrent-write tail."""
    complete_content = content[: content.rfind("\n") + 1]
    rejected = 0
    records: list[tuple[datetime, dict[str, Any]]] = []
    future_limit = datetime.now(timezone.utc).timestamp() + MAX_FUTURE_SKEW_SECONDS
    for line in complete_content.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line, parse_constant=_json_constant)
            timestamp = _telemetry_timestamp(payload)
            if timestamp.timestamp() > future_limit:
                raise ValueError("telemetry timestamp is too far in the future")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            rejected += 1
            continue
        records.append((timestamp, payload))
        if len(records) > MAX_TELEMETRY_HISTORY_SCAN_RECORDS:
            raise HTTPException(status_code=503, detail="Telemetry history exceeds the supported record-scan limit.")
    return records, rejected


def _history_record(payload: dict[str, Any], provenance: str) -> TelemetryHistoryRecordResponse:
    """Project a validated Person-1 row without deriving or filling measurements."""
    return TelemetryHistoryRecordResponse(
        timestamp=payload["timestamp"],
        experiment_id=payload["experiment_id"],
        scenario=payload["scenario"],
        source=payload["source"],
        destination=payload["destination"],
        traffic_type=payload["traffic_type"],
        requested_mbps=payload["requested_mbps"],
        throughput_mbps=payload["throughput_mbps"],
        latency_ms=payload["latency_ms"],
        jitter_ms=payload["jitter_ms"],
        packet_loss_percent=payload["packet_loss_percent"],
        lost_packets=payload["lost_packets"],
        total_packets=payload["total_packets"],
        bandwidth_mbps=payload["bandwidth_mbps"],
        utilization_percent=payload["utilization_percent"],
        active_flows=payload["active_flows"],
        queue_size=payload["queue_size"],
        queue_drops=payload["queue_drops"],
        measurement_direction=payload.get("measurement_direction"),
        interface="s1-eth5",
        provenance=provenance,
    )


def _configured_report_root() -> Path | None:
    configured = os.environ.get(REPORT_DIRECTORY_ENV)
    if not configured or not configured.strip():
        return None
    try:
        root = Path(configured).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Configured report directory is unavailable.") from exc
    if not root.is_dir():
        raise HTTPException(status_code=503, detail="Configured report directory is unavailable.")
    return root


def _classify_report(report: Any) -> str | None:
    if not isinstance(report, dict):
        return None
    if (
        report.get("schema_version") == 1
        and report.get("planning_only") is True
        and report.get("dry_run") is True
        and report.get("experiment_executed") is False
        and report.get("measurements_collected") is False
        and report.get("live_qos_applied") is False
        and report.get("tc_commands_executed") is False
        and report.get("mode") in {"adaptive", "both", "baseline"}
        and isinstance(report.get("results"), list)
    ):
        return "experiment-runner"
    if (
        report.get("offline_only") is True
        and report.get("live_operations_supported") is False
        and isinstance(report.get("trials"), list)
    ):
        return "evidence-analysis"
    if (
        report.get("live_operations_supported") is False
        and isinstance(report.get("trials"), list)
        and ("campaign_status" in report or "campaign_id" in report)
    ):
        return "campaign-validation"
    return None


def _report_path(report_id: str, root: Path) -> Path:
    if not REPORT_ID_PATTERN.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="Report was not found.")
    path = root / f"{report_id}.json"
    try:
        resolved = path.resolve(strict=True)
        if resolved.parent != root:
            raise FileNotFoundError
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise FileNotFoundError
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="Report was not found.") from exc
    return path


def _metric_summary(value: Any) -> MetricSummary:
    record = value if isinstance(value, dict) else {}
    return MetricSummary(
        throughput_mbps=_finite(record.get("throughput_mbps")),
        latency_ms=_finite(record.get("latency_ms")),
        jitter_ms=_finite(record.get("jitter_ms")),
        packet_loss_percent=_finite(
            record.get("packet_loss_percent", record.get("packet_loss_pct"))
        ),
    )


def _recognized_status(value: Any, default: str = "unknown") -> str:
    text = _safe_text(value, max_length=80)
    return text if text is not None else default


def _report_summary(report_id: str, document: Any) -> ReportSummary | None:
    report_type = _classify_report(document)
    if report_type is None:
        return None
    status = _recognized_status(document.get("status"))
    campaign_status = (
        _recognized_status(document.get("campaign_status"))
        if report_type == "campaign-validation"
        else None
    )
    return ReportSummary(
        report_id=report_id,
        report_type=report_type,  # type: ignore[arg-type]
        status=status,
        campaign_status=campaign_status,
        alignment_eligibility="ineligible",
        comparison_ready=False,
    )


def _runner_records(document: dict[str, Any]) -> list[RunnerPlanRecord]:
    if document.get("mode") not in {"adaptive", "both"}:
        return []
    records: list[RunnerPlanRecord] = []
    for index, item in enumerate(document["results"], start=1):
        if not isinstance(item, dict):
            continue
        modes = item.get("modes")
        modes = modes if isinstance(modes, dict) else {}
        adaptive = modes.get("adaptive")
        adaptive = adaptive if isinstance(adaptive, dict) else {}
        prediction = adaptive.get("prediction")
        prediction = prediction if isinstance(prediction, dict) else {}
        freshness = item.get("freshness")
        freshness = freshness if isinstance(freshness, dict) else {}
        window = item.get("window")
        window = window if isinstance(window, dict) else {}

        def allocation_map(value: Any, expected: set[str]) -> dict[str, float]:
            if not isinstance(value, dict):
                return {}
            result = {
                key: _finite(rate)
                for key, rate in value.items()
                if key in expected and _finite(rate) is not None
            }
            return {key: rate for key, rate in result.items() if rate is not None}

        input_features = allocation_map(
            adaptive.get("input_features"),
            {
                "utilization", "throughput_mbps", "latency_ms",
                "jitter_ms", "packet_loss_pct", "active_flows",
            },
        )
        congestion = prediction.get("congestion")
        confidence = _finite(prediction.get("confidence"))
        records.append(RunnerPlanRecord(
            line=int(_finite(item.get("line")) or index),
            status=_recognized_status(adaptive.get("status", item.get("status"))),
            experiment_id=_safe_id(item.get("experiment_id")),
            scenario=_safe_text(item.get("scenario"), max_length=40),
            source=_safe_text(item.get("source"), max_length=20),
            timestamp=_safe_text(window.get("timestamp"), max_length=64),
            freshness_status=_recognized_status(freshness.get("status"), "not evaluated"),
            freshness_age_seconds=_finite(freshness.get("age_seconds")),
            prediction=congestion if congestion in {"LOW", "MEDIUM", "HIGH"} else None,
            confidence=confidence if confidence is not None and 0 <= confidence <= 1 else None,
            input_features=input_features,
            measured=_metric_summary(item.get("measured")),
            service_allocations_mbps=allocation_map(
                adaptive.get("service_class_allocation_mbps"),
                {"gaming", "video", "web", "bulk"},
            ),
            flow_allocations_mbps=allocation_map(
                adaptive.get("flow_allocations_mbps"),
                {"5201", "5202", "5203", "5204"},
            ),
        ))
    return records


def _evidence_trials(document: dict[str, Any], report_type: str) -> list[ReportTrial]:
    trials: list[ReportTrial] = []
    for index, item in enumerate(document["trials"], start=1):
        if not isinstance(item, dict):
            continue
        alignment_value = (
            item.get("measurement_alignment_eligibility")
            if report_type == "evidence-analysis"
            else item.get("alignment_status")
        )
        alignment = _recognized_status(alignment_value, "ineligible")
        flows: list[ReportFlow] = []
        flow_data = item.get("flows")
        if isinstance(flow_data, dict):
            for flow_id, flow in flow_data.items():
                if flow_id not in {"h1", "h2", "h3", "h4"} or not isinstance(flow, dict):
                    continue
                receiver = flow.get("receiver")
                receiver = receiver if isinstance(receiver, dict) else {}
                flows.append(ReportFlow(
                    flow_id=flow_id,
                    status=_recognized_status(flow.get("status")),
                    metrics=_metric_summary(receiver.get("metrics")),
                ))
        aggregate = item.get("receiver_window_aggregate")
        aggregate = aggregate if isinstance(aggregate, dict) else {}
        trials.append(ReportTrial(
            trial_id=_safe_id(item.get("trial_id")) or f"trial-{index}",
            condition=_recognized_status(item.get("condition")),
            profile=_recognized_status(item.get("profile")),
            repetition_id=_safe_id(item.get("repetition_id")) or "unknown",
            status=_recognized_status(item.get("status")),
            alignment_status=alignment,
            boundary_metadata_status=_recognized_status(
                item.get("boundary_metadata_status"), "invalid_or_incomplete"
            ),
            boundary_evidence_integrity_status=_recognized_status(
                item.get("boundary_evidence_integrity_status"), "not_verified"
            ),
            producer_provenance_status="not_independently_validated",
            clock_mapping_status="not_independently_validated",
            flows=flows,
        ))
    return trials


def _build_report_detail(report_id: str, document: Any) -> ReportDetailResponse:
    report_type = _classify_report(document)
    if report_type is None:
        raise HTTPException(status_code=422, detail="Report does not match a supported offline report schema.")
    assert isinstance(document, dict)
    summary = _report_summary(report_id, document)
    assert summary is not None
    if report_type == "experiment-runner":
        return ReportDetailResponse(
            report_id=report_id,
            report_type=report_type,  # type: ignore[arg-type]
            created_at=_safe_text(document.get("created_at"), max_length=64),
            status=summary.status,
            planning_only=True,
            boundary_metadata_status="not_applicable",
            boundary_evidence_integrity_status="not_applicable",
            producer_provenance_status="not_applicable",
            clock_mapping_status="not_applicable",
            alignment_eligibility="not_applicable",
            comparison_ready=False,
            runner_records=_runner_records(document),
        )

    trials = _evidence_trials(document, report_type)
    complete_cycles = _finite(document.get("complete_cycle_count"))
    minimum_cycles = _finite(document.get("minimum_complete_cycles"))
    campaign_status = (
        _recognized_status(document.get("campaign_status"))
        if report_type == "campaign-validation"
        else None
    )
    boundary_metadata = (
        "schema_valid"
        if trials and all(t.boundary_metadata_status == "schema_valid" for t in trials)
        else "invalid_or_incomplete"
    )
    artifact_integrity = (
        "verified"
        if trials and all(t.boundary_evidence_integrity_status == "verified" for t in trials)
        else "not_verified"
    )
    return ReportDetailResponse(
        report_id=report_id,
        report_type=report_type,  # type: ignore[arg-type]
        created_at=None,
        status=summary.status,
        planning_only=True,
        campaign_status=campaign_status,
        complete_cycle_count=int(complete_cycles) if complete_cycles is not None else None,
        minimum_complete_cycles=int(minimum_cycles) if minimum_cycles is not None else None,
        boundary_metadata_status=boundary_metadata,
        boundary_evidence_integrity_status=artifact_integrity,
        producer_provenance_status="not_independently_validated",
        clock_mapping_status="not_independently_validated",
        alignment_eligibility="ineligible",
        comparison_ready=False,
        trials=trials,
    )


def _latest_experiment_status(reports: list[ReportSummary], root: Path) -> ExperimentsStatusResponse:
    candidates = [
        report for report in reports
        if report.report_type in {"campaign-validation", "evidence-analysis"}
    ]
    if not candidates:
        return ExperimentsStatusResponse(
            status="unavailable",
            campaign_completeness="unavailable",
            boundary_metadata_status="unavailable",
            boundary_evidence_integrity_status="unavailable",
            producer_provenance_status="not_independently_validated",
            clock_mapping_status="not_independently_validated",
            measurement_alignment_eligibility="ineligible",
            comparison_ready=False,
            message="No supported evidence-analysis or campaign report is available.",
        )
    try:
        latest = max(
            candidates,
            key=lambda item: (root / f"{item.report_id}.json").stat().st_mtime,
        )
        path = _report_path(latest.report_id, root)
        report = _read_json_file(path, root, max_bytes=MAX_REPORT_BYTES)
    except (OSError, HTTPException) as exc:
        raise HTTPException(
            status_code=503,
            detail="Configured report changed or became unavailable during inspection.",
        ) from exc
    detail = _build_report_detail(latest.report_id, report)
    return ExperimentsStatusResponse(
        status="available",
        report_id=latest.report_id,
        campaign_completeness=(
            detail.campaign_status
            if detail.campaign_status is not None
            else detail.status
        ),
        complete_cycle_count=detail.complete_cycle_count,
        minimum_complete_cycles=detail.minimum_complete_cycles,
        boundary_metadata_status=detail.boundary_metadata_status,
        boundary_evidence_integrity_status=detail.boundary_evidence_integrity_status,
        producer_provenance_status=detail.producer_provenance_status,
        clock_mapping_status=detail.clock_mapping_status,
        measurement_alignment_eligibility=detail.alignment_eligibility,
        comparison_ready=False,
        message=(
            "Campaign completeness is reported separately. Measurement alignment "
            "remains ineligible because producer provenance and clock validation "
            "are not independently validated."
        ),
    )


app = FastAPI(
    title="Adaptive QoS Dashboard API",
    version=API_VERSION,
    description="Read-only telemetry and reports plus explicitly authorized local QoS application.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Accept", "Content-Type"],
)


_LOCAL_ORIGINS = {
    "http://127.0.0.1:5173", "http://localhost:5173",
    "http://127.0.0.1:5174", "http://localhost:5174",
}
_LOCAL_HOSTS = {"127.0.0.1:8000", "localhost:8000"}


def _require_local_mutation(request: Request) -> None:
    # Browser mutations require an exact local Vite Origin and a loopback API Host.
    # This rejects cross-site browser requests and requests sent through a LAN host.
    client_host = request.client.host if request.client else ""
    try:
        client_is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        client_is_loopback = False
    if (
        request.headers.get("origin") not in _LOCAL_ORIGINS
        or request.headers.get("host", "").lower() not in _LOCAL_HOSTS
        or not client_is_loopback
    ):
        raise HTTPException(status_code=403, detail="Network changes are restricted to the local dashboard origin.")


def _public_experiment(state: dict[str, Any]) -> ExperimentResponse:
    # Deliberately omit WSL PIDs, filesystem paths, command arguments, and logs.
    try:
        return ExperimentResponse(
            experiment_id=state["experiment_id"], scenario=state["scenario"], source_host=state.get("source_host"), status=state["status"],
            flows=state["flows"], target_rate_mbps=state["target_rate_mbps"],
            duration_seconds=state["duration_seconds"], created_at=state["created_at"],
            started_at=state.get("started_at"), finished_at=state.get("finished_at"),
            elapsed_seconds=max(0.0, float(state.get("elapsed_seconds", 0))),
            process_alive=bool(state.get("process_alive")), can_stop=bool(state.get("can_stop")),
            process_exit_code=state.get("process_exit_code"), error=state.get("error"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="The execution supervisor returned invalid state.") from exc


def _execution_call(
    action: str,
    experiment_id: str | None = None,
    scenario: str | None = None,
    rate_mbps: float | None = None,
    duration_seconds: int | None = None,
    source_host: str | None = None,
) -> dict[str, Any]:
    from dashboard.backend.execution_manager import (
        ExecutionNotFound, ExecutionUnavailable, TopologyUnavailable, invoke_supervisor,
    )

    try:
        if action == "start":
            return invoke_supervisor(action, experiment_id, scenario, rate_mbps, duration_seconds, source_host)
        return invoke_supervisor(action, experiment_id, scenario)
    except TopologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ExecutionNotFound as exc:
        raise HTTPException(status_code=404, detail="Experiment was not found.") from exc
    except ExecutionUnavailable as exc:
        status_code = 409 if "already active" in str(exc).lower() else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/api/experiments/start", response_model=ExperimentStartResponse)
def start_traffic(request: Request, body: ExperimentStartRequest) -> ExperimentStartResponse:
    _require_local_mutation(request)
    from uuid import uuid4

    defaults = {
        "normal": (3.0, 20),
        "moderate": (6.0, 20),
        "congestion": (4.0, 20),
        "burst": (8.0, 23),
    }[body.scenario]
    rate_mbps = defaults[0] if body.rate_mbps is None else body.rate_mbps
    duration_seconds = defaults[1] if body.duration_seconds is None else body.duration_seconds
    source_host = body.source_host or ("all" if body.scenario == "congestion" else "h1")
    state = _execution_call(
        "start", uuid4().hex, body.scenario, rate_mbps, duration_seconds, source_host
    )
    return ExperimentStartResponse(**_public_experiment(state).model_dump())


@app.get("/api/execution/active", response_model=ExperimentResponse | None)
def get_active_traffic_execution() -> ExperimentResponse | None:
    state = _execution_call("active")
    return _public_experiment(state) if state is not None else None


@app.get("/api/execution/{experiment_id}", response_model=ExperimentResponse)
def get_traffic_execution(experiment_id: str) -> ExperimentResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", experiment_id):
        raise HTTPException(status_code=404, detail="Experiment was not found.")
    state = _execution_call("status", experiment_id)
    return _public_experiment(state)


@app.post("/api/experiments/{experiment_id}/stop", response_model=ExperimentResponse)
def stop_traffic(request: Request, experiment_id: str) -> ExperimentResponse:
    _require_local_mutation(request)
    if not re.fullmatch(r"[0-9a-f]{32}", experiment_id):
        raise HTTPException(status_code=404, detail="Experiment was not found.")
    return _public_experiment(_execution_call("stop", experiment_id))


@app.get("/api/topology", response_model=TopologyResponse)
def topology() -> TopologyResponse:
    return TopologyResponse(
        hosts=[
            TopologyHost(id="h1", role="client", service="Gaming", udp_destination_port=5201),
            TopologyHost(id="h2", role="client", service="Video Streaming", udp_destination_port=5202),
            TopologyHost(id="h3", role="client", service="Web", udp_destination_port=5203),
            TopologyHost(id="h4", role="client", service="Bulk Transfer", udp_destination_port=5204),
            TopologyHost(id="server", role="server", ipv4_address="10.0.0.5"),
        ],
        switches=["s1", "s2"],
        links=[
            TopologyLink(source="h1", target="s1"),
            TopologyLink(source="h2", target="s1"),
            TopologyLink(source="h3", target="s1"),
            TopologyLink(source="h4", target="s1"),
            TopologyLink(source="s1", target="s2", bandwidth_mbps=10.0, delay="10ms"),
            TopologyLink(source="s2", target="server"),
        ],
    )


@app.get("/api/telemetry/latest", response_model=LatestTelemetryResponse)
def latest_telemetry() -> LatestTelemetryResponse:
    configured = os.environ.get(TELEMETRY_FILE_ENV)
    candidates: list[tuple[datetime, dict[str, Any], str]] = []
    if configured and configured.strip():
        try:
            content = _read_configured_telemetry_text(Path(configured).expanduser())
            record_data: dict[str, Any] | None = None
            for line in content.splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line, parse_constant=_json_constant)
                _telemetry_timestamp(payload)
                record_data = payload
            if record_data is None:
                raise ValueError("configured telemetry has no complete rows")
            candidates.append((
                _telemetry_timestamp(record_data),
                record_data,
                "configured_person1_jsonl",
            ))
        except (OSError, RuntimeError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass

    candidates.extend(
        (timestamp, payload, "completed_experiment_person1_jsonl")
        for timestamp, payload in _completed_experiment_telemetry()
    )
    if not candidates:
        if configured and configured.strip():
            source = "configured_person1_jsonl"
            message = "The configured telemetry file is missing, unsafe, empty, or invalid; no valid completed experiment measurement was found."
        else:
            source = "none"
            message = (
                "No telemetry file is configured and no valid measurement from a completed experiment was found. "
                "Set ADAPTIVE_QOS_TELEMETRY_FILE to an explicitly selected Person-1 JSONL file."
            )
        return LatestTelemetryResponse(status="unavailable", source=source, message=message)

    parsed_time, record_data, source = max(candidates, key=lambda candidate: candidate[0])

    now = datetime.now(timezone.utc)
    age = (now - parsed_time).total_seconds()
    if age > MAX_TELEMETRY_AGE_SECONDS:
        status: Literal["available", "stale", "unavailable"] = "stale"
        message = "Latest validated measurement is stale."
    elif age < -MAX_FUTURE_SKEW_SECONDS:
        status = "unavailable"
        message = "Latest measurement timestamp is too far in the future."
    else:
        status = "available"
        if source == "configured_person1_jsonl":
            message = "Latest validated record from the explicitly configured Person-1 JSONL file."
        else:
            message = "Latest validated record from a successfully completed guarded experiment output."

    return LatestTelemetryResponse(
        status=status,
        source=source,
        measurement_age_seconds=age,
        message=message,
        record=TelemetryRecordResponse(
            timestamp=record_data["timestamp"],
            source=record_data["source"],
            scenario=record_data["scenario"],
            throughput_mbps=record_data["throughput_mbps"],
            latency_ms=record_data["latency_ms"],
            jitter_ms=record_data["jitter_ms"],
            packet_loss_percent=record_data["packet_loss_percent"],
            queue_size=record_data["queue_size"],
            queue_drops=record_data["queue_drops"],
            measurement_direction=record_data.get("measurement_direction"),
            bandwidth_mbps=record_data.get("bandwidth_mbps"),
            utilization_percent=record_data.get("utilization_percent"),
            active_flows=record_data.get("active_flows"),
        ),
    )


@app.get("/api/telemetry/history", response_model=TelemetryHistoryResponse)
def telemetry_history(
    request: Request,
    host: Literal["h1", "h2", "h3", "h4"] | None = Query(default=None),
    scenario: Literal["normal", "moderate", "congestion", "burst"] | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=MAX_TELEMETRY_HISTORY_RECORDS),
) -> TelemetryHistoryResponse:
    """Return bounded, validated recorded telemetry; this endpoint never reads request paths."""
    unsupported_parameters = set(request.query_params) - {"host", "scenario", "limit"}
    if unsupported_parameters:
        raise HTTPException(status_code=422, detail="Telemetry history accepts only host, scenario, and limit filters.")

    candidates: list[tuple[datetime, dict[str, Any], str]] = []
    source_kinds: set[str] = set()
    rejected_records = [0]
    total_bytes = 0

    configured = os.environ.get(TELEMETRY_FILE_ENV)
    if configured and configured.strip():
        source_kinds.add("configured_person1_jsonl")
        try:
            content = _read_configured_telemetry_text(Path(configured).expanduser())
            total_bytes += len(content.encode("utf-8"))
            rows, rejected = _complete_jsonl_records(content)
            rejected_records[0] += rejected
            candidates.extend((timestamp, payload, "configured_person1_jsonl") for timestamp, payload in rows)
        except (OSError, RuntimeError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass

    completed_records = _completed_experiment_telemetry(
        max_run_directories=MAX_TELEMETRY_HISTORY_RUNS,
        max_total_bytes=max(0, MAX_TELEMETRY_HISTORY_BYTES - total_bytes),
        max_records=MAX_TELEMETRY_HISTORY_SCAN_RECORDS,
        rejected_record_counter=rejected_records,
    )
    future_limit = datetime.now(timezone.utc).timestamp() + MAX_FUTURE_SKEW_SECONDS
    for timestamp, payload in completed_records:
        source_kinds.add("completed_experiment_person1_jsonl")
        if timestamp.timestamp() > future_limit:
            rejected_records[0] += 1
            continue
        candidates.append((timestamp, payload, "completed_experiment_person1_jsonl"))

    # A configured file may also be a completed-run output. Keep one copy and
    # prefer the explicitly configured provenance, matching /latest behavior.
    deduplicated: dict[tuple[str, str, str, str], tuple[datetime, dict[str, Any], str]] = {}
    for item in candidates:
        timestamp, payload, provenance = item
        key = (payload["experiment_id"], payload["timestamp"], payload["source"], payload["scenario"])
        current = deduplicated.get(key)
        if current is None or provenance == "configured_person1_jsonl":
            deduplicated[key] = (timestamp, payload, provenance)

    filtered = [
        item for item in deduplicated.values()
        if (host is None or item[1]["source"] == host)
        and (scenario is None or item[1]["scenario"] == scenario)
    ]
    filtered.sort(key=lambda item: item[0])
    total_records = len(filtered)
    truncated = total_records > limit
    selected_records = filtered[-limit:]

    if selected_records:
        message = "Validated timestamped records from recorded JSONL sources; these are historical measurements, not live telemetry."
        status: Literal["available", "unavailable"] = "available"
    elif candidates:
        message = "No recorded telemetry matches the requested host and scenario filters."
        status = "unavailable"
    else:
        message = "No valid records were found in the configured or completed-experiment telemetry sources."
        status = "unavailable"

    return TelemetryHistoryResponse(
        status=status,
        message=message,
        source_kinds=sorted(source_kinds),
        host_filter=host,
        scenario_filter=scenario,
        limit=limit,
        total_records=total_records,
        truncated=truncated,
        rejected_records=rejected_records[0],
        records=[_history_record(payload, provenance) for _, payload, provenance in selected_records],
    )


@app.get("/api/qos/decision", response_model=DryRunDecisionResponse)
def qos_dry_run_decision() -> DryRunDecisionResponse:
    """Return a validated, rule-based QoS proposal without applying it."""
    result = decision_loop.evaluate(latest_telemetry())
    result["application_enabled"] = os.environ.get(QOS_APPLICATION_ENV) == "1"
    return DryRunDecisionResponse(**result)


@app.get("/api/qos/status", response_model=QosApplicationStatusResponse)
def qos_application_status() -> QosApplicationStatusResponse:
    from dashboard.backend.execution_manager import (
        ExecutionUnavailable,
        TopologyUnavailable,
        inspect_qos_profile,
    )

    try:
        result = inspect_qos_profile()
    except (TopologyUnavailable, ExecutionUnavailable) as exc:
        return QosApplicationStatusResponse(
            status="unavailable",
            message=f"Applied QoS state could not be read: {exc}",
        )
    if (
        not isinstance(result, dict)
        or result.get("interface") != "s1-eth5"
        or result.get("status") not in {"verified", "unrecognized"}
    ):
        raise HTTPException(status_code=502, detail="The QoS supervisor returned invalid readback state.")
    profile = result.get("profile")
    if profile not in {None, "LOW", "MEDIUM", "HIGH"}:
        raise HTTPException(status_code=502, detail="The QoS supervisor returned an unsupported profile.")
    if (result["status"] == "verified") != (profile is not None):
        raise HTTPException(status_code=502, detail="The QoS supervisor returned inconsistent profile state.")
    return QosApplicationStatusResponse(
        status=result["status"],
        applied_profile=profile,
        message=(
            "Current managed QoS profile confirmed by kernel readback."
            if profile is not None
            else "No recognized managed profile is confirmed; existing state is not inferred."
        ),
    )


@app.post("/api/qos/apply", response_model=QosApplyResponse)
def apply_qos_profile(request: Request, body: QosApplyRequest) -> QosApplyResponse:
    _require_local_mutation(request)
    if os.environ.get(QOS_APPLICATION_ENV) != "1":
        raise HTTPException(
            status_code=403,
            detail="QoS application is disabled; explicitly enable it for a trusted local lab.",
        )

    decision = decision_loop.evaluate(latest_telemetry())
    measurement = decision.get("measurement")
    proposed_policy = decision.get("proposed_policy")
    if (
        decision.get("status") != "ready"
        or decision.get("telemetry_status") != "available"
        or not isinstance(measurement, dict)
        or not isinstance(proposed_policy, dict)
        or decision.get("selected_profile") != body.profile
        or measurement.get("timestamp") != body.measurement_timestamp
        or decision.get("candidate_profile") is not None
        or proposed_policy.get("interface") != "s1-eth5"
    ):
        raise HTTPException(
            status_code=409,
            detail="The fresh, stable QoS proposal changed or is unavailable; refresh the decision before applying.",
        )

    from dashboard.backend.execution_manager import (
        ExecutionUnavailable,
        QosVerificationFailed,
        TopologyUnavailable,
        invoke_qos_profile,
    )

    try:
        result = invoke_qos_profile(body.profile)
    except QosVerificationFailed as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "state": "verification_failed",
                "message": "QoS application did not pass kernel readback verification.",
            },
        ) from exc
    except (TopologyUnavailable, ExecutionUnavailable) as exc:
        raise HTTPException(
            status_code=503,
            detail={"state": "failed", "message": str(exc)},
        ) from exc

    if (
        not isinstance(result, dict)
        or result.get("status") != "applied_verified"
        or result.get("profile") != body.profile
        or result.get("interface") != "s1-eth5"
        or result.get("kernel_readback_verified") is not True
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "state": "verification_failed",
                "message": "QoS readback result was incomplete or mismatched.",
            },
        )
    return QosApplyResponse(
        status="applied_verified",
        profile=body.profile,
        interface="s1-eth5",
        measurement_timestamp=body.measurement_timestamp,
        verification="kernel_readback_verified",
        message="The selected profile was applied by the WSL QoS controller and confirmed by kernel readback.",
    )


def _list_report_summaries(root: Path) -> list[ReportSummary]:
    summaries: list[ReportSummary] = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Configured report directory is unavailable.") from exc
    for path in entries:
        if path.suffix.lower() != ".json" or not REPORT_ID_PATTERN.fullmatch(path.stem):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            document = _read_json_file(path, root, max_bytes=MAX_REPORT_BYTES)
            summary = _report_summary(path.stem, document)
        except HTTPException:
            summaries.append(ReportSummary(
                report_id=path.stem,
                report_type="unknown",
                status="invalid",
                alignment_eligibility="ineligible",
                comparison_ready=False,
            ))
            continue
        if summary is None:
            summaries.append(ReportSummary(
                report_id=path.stem,
                report_type="unknown",
                status="invalid",
                alignment_eligibility="ineligible",
                comparison_ready=False,
            ))
        else:
            summaries.append(summary)
    return sorted(summaries, key=lambda item: item.report_id)


@app.get("/api/reports", response_model=ReportsResponse)
def list_reports() -> ReportsResponse:
    root = _configured_report_root()
    if root is None:
        return ReportsResponse(
            status="unavailable",
            message=f"Set {REPORT_DIRECTORY_ENV} to an explicitly approved report directory.",
        )
    return ReportsResponse(
        status="available",
        message="Only recognized JSON reports from the configured directory are listed.",
        reports=_list_report_summaries(root),
    )


@app.get("/api/reports/{report_id}", response_model=ReportDetailResponse)
def get_report(report_id: str) -> ReportDetailResponse:
    root = _configured_report_root()
    if root is None:
        raise HTTPException(status_code=503, detail="Report directory is not configured.")
    path = _report_path(report_id, root)
    document = _read_json_file(path, root, max_bytes=MAX_REPORT_BYTES)
    return _build_report_detail(report_id, document)


@app.get("/api/experiments/status", response_model=ExperimentsStatusResponse)
def experiments_status() -> ExperimentsStatusResponse:
    root = _configured_report_root()
    if root is None:
        return ExperimentsStatusResponse(
            status="unavailable",
            campaign_completeness="unavailable",
            boundary_metadata_status="unavailable",
            boundary_evidence_integrity_status="unavailable",
            producer_provenance_status="not_independently_validated",
            clock_mapping_status="not_independently_validated",
            measurement_alignment_eligibility="ineligible",
            comparison_ready=False,
            message=f"Set {REPORT_DIRECTORY_ENV} to inspect offline campaign reports.",
        )
    return _latest_experiment_status(_list_report_summaries(root), root)
