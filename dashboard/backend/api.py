"""Read-only HTTP API for configured Adaptive QoS artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from ai.prediction.telemetry import adapt_person1_telemetry


API_VERSION = "0.1.0"
MAX_REPORT_BYTES = 5 * 1024 * 1024
MAX_TELEMETRY_BYTES = 5 * 1024 * 1024
MAX_TELEMETRY_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
REPORT_DIRECTORY_ENV = "ADAPTIVE_QOS_REPORT_DIR"
TELEMETRY_FILE_ENV = "ADAPTIVE_QOS_TELEMETRY_FILE"
REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"
    service: str = "adaptive-qos-readonly-api"
    api_version: str = API_VERSION
    live_network_operations: Literal[False] = False


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


class LatestTelemetryResponse(APIModel):
    status: Literal["available", "stale", "unavailable"]
    source: Literal["configured_person1_jsonl", "none"]
    measurement_age_seconds: float | None = None
    stale_after_seconds: int = MAX_TELEMETRY_AGE_SECONDS
    message: str
    record: TelemetryRecordResponse | None = None


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
    title="Adaptive QoS Read-only API",
    version=API_VERSION,
    description="Read-only topology, explicitly configured telemetry, and sanitized offline reports.",
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
    allow_methods=["GET"],
    allow_headers=["Accept"],
)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


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
    if not configured or not configured.strip():
        return LatestTelemetryResponse(
            status="unavailable",
            source="none",
            message=(
                "No telemetry file is configured. Set ADAPTIVE_QOS_TELEMETRY_FILE "
                "to an explicitly selected Person-1 JSONL file."
            ),
        )
    source_path = Path(configured).expanduser()
    try:
        content = _read_configured_telemetry_text(source_path)
        record_data: dict[str, Any] | None = None
        for line in content.splitlines():
            if not line.strip():
                continue
            payload = json.loads(line, parse_constant=_json_constant)
            adapt_person1_telemetry(payload, interface="s1-eth5")
            record_data = payload
        if record_data is None:
            raise ValueError
        parsed_time = datetime.fromisoformat(
            record_data["timestamp"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (OSError, RuntimeError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return LatestTelemetryResponse(
            status="unavailable",
            source="configured_person1_jsonl",
            message="The configured telemetry file is missing, unsafe, empty, or invalid.",
        )

    now = datetime.now(timezone.utc)
    age = (now - parsed_time).total_seconds()
    if age > MAX_TELEMETRY_AGE_SECONDS:
        status: Literal["available", "stale", "unavailable"] = "stale"
        message = "Latest configured measurement is stale."
    elif age < -MAX_FUTURE_SKEW_SECONDS:
        status = "unavailable"
        message = "Configured measurement timestamp is too far in the future."
    else:
        status = "available"
        message = "Latest validated record from the explicitly configured Person-1 JSONL file."

    return LatestTelemetryResponse(
        status=status,
        source="configured_person1_jsonl",
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
        ),
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