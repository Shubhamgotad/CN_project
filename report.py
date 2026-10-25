"""Generate a saved-model plan from the newest validated Traffic Lab run.

This command is offline only: it reads completed evidence and never starts
traffic, invokes tc, or changes network configuration.
"""
from __future__ import annotations

import json
import ntpath
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from ai.prediction.telemetry import PERSON1_REQUIRED_FIELDS, adapt_person1_telemetry
from integration.experiment_runner import run_experiment
from integration.telemetry_qos import QoSIntegrationConfig

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT.parent / ".adaptive-qos-runs"
REPORT_DIRECTORY_ENV = "ADAPTIVE_QOS_REPORT_DIR"
TELEMETRY_FILE_ENV = "ADAPTIVE_QOS_TELEMETRY_FILE"
MODEL = ROOT / "ai" / "prediction" / "model.joblib"
MAX_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
SCENARIOS = {"normal", "moderate", "congestion", "burst"}
FLOW_CLASSES = {"5201": "gaming", "5202": "video", "5203": "web", "5204": "bulk"}
FILTER_PRIORITIES = {"5201": 10, "5202": 20, "5203": 30, "5204": 40}


def _aware_timestamp(value: object, *, line: int) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"record {line}: timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"record {line}: invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"record {line}: timestamp has no timezone offset")
    return parsed.astimezone(timezone.utc)


def _validated_records(
    path: Path,
    experiment_id: str,
    scenario: str,
    allowed_sources: set[str],
    *,
    finished_at: datetime | None = None,
    check_freshness: bool = False,
    now: datetime | None = None,
) -> tuple[int, datetime]:
    latest: datetime | None = None
    count = 0
    reference_now = now or datetime.now(timezone.utc)
    try:
        with path.open("r", encoding="utf-8") as stream:
            lines = list(stream)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"telemetry {path.name!r} cannot be read: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                f"telemetry {path.name!r} record {line_number}: malformed JSON: {exc}; "
                f"expected experiment_id={experiment_id!r}, scenario={scenario!r}; observed identity unavailable"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"telemetry {path.name!r} record {line_number}: expected a JSON object; "
                f"expected experiment_id={experiment_id!r}, scenario={scenario!r}; observed identity unavailable"
            )
        observed_id = record.get("experiment_id")
        observed_scenario = record.get("scenario")
        identity_context = (
            f"expected experiment_id={experiment_id!r}, scenario={scenario!r}; "
            f"observed experiment_id={observed_id!r}, scenario={observed_scenario!r}"
        )
        if observed_id != experiment_id or observed_scenario != scenario:
            raise ValueError(
                f"telemetry {path.name!r} record {line_number}: experiment identity mismatch; {identity_context}. "
                "The file may be stale or associated with another run."
            )
        missing = [field for field in PERSON1_REQUIRED_FIELDS if field not in record]
        if missing:
            raise ValueError(
                f"telemetry {path.name!r} record {line_number}: missing required ML fields: "
                f"{', '.join(missing)}; {identity_context}"
            )
        if record.get("source") not in allowed_sources:
            raise ValueError(
                f"telemetry {path.name!r} record {line_number}: source host {record.get('source')!r} "
                f"is outside run flows {sorted(allowed_sources)!r}; {identity_context}"
            )
        try:
            timestamp = _aware_timestamp(record.get("timestamp"), line=line_number)
        except ValueError as exc:
            raise ValueError(f"telemetry {path.name!r}: {exc}; {identity_context}") from exc
        if finished_at is not None and timestamp > finished_at:
            raise ValueError(
                f"telemetry {path.name!r} record {line_number}: measurement timestamp {timestamp.isoformat()} "
                f"is after authoritative finished_at {finished_at.isoformat()}; {identity_context}"
            )
        if check_freshness:
            age = (reference_now - timestamp).total_seconds()
            if age > MAX_AGE_SECONDS:
                raise ValueError(
                    f"telemetry {path.name!r} record {line_number}: measurement is stale ({age:.1f}s; "
                    f"limit {MAX_AGE_SECONDS}s); {identity_context}"
                )
            if age < -MAX_FUTURE_SKEW_SECONDS:
                raise ValueError(
                    f"telemetry {path.name!r} record {line_number}: measurement is future-dated ({-age:.1f}s); "
                    f"{identity_context}"
                )
        try:
            adapt_person1_telemetry(record, interface="s1-eth5")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"telemetry {path.name!r} record {line_number}: incompatible Person 1 ML measurement schema: "
                f"{exc}; {identity_context}"
            ) from exc
        latest = timestamp if latest is None or timestamp > latest else latest
        count += 1
    if count == 0 or latest is None:
        raise ValueError("telemetry JSONL contains no records")
    return count, latest


def _newest_completed_run() -> tuple[dict, Path]:
    if RUNS_DIR.is_symlink() or not RUNS_DIR.is_dir():
        raise ValueError(f"Traffic Lab run directory is unavailable: {RUNS_DIR}")
    candidates: list[tuple[datetime, dict, Path]] = []
    resolved_root = RUNS_DIR.resolve(strict=True)
    for run_dir in RUNS_DIR.iterdir():
        if run_dir.is_symlink() or not run_dir.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", run_dir.name):
            continue
        state_path = run_dir / "state.json"
        if state_path.is_symlink() or not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot validate run state in {run_dir.name}; refusing to select older telemetry") from exc
        if not isinstance(state, dict):
            raise ValueError(f"Run state in {run_dir.name} is not a JSON object; refusing older telemetry")
        if state.get("status") != "completed":
            continue
        try:
            finished = _state_timestamp(state.get("finished_at"), "finished_at")
            created = _state_timestamp(state.get("created_at"), "created_at")
            started = _state_timestamp(state.get("started_at"), "started_at")
            if not created <= started <= finished:
                raise ValueError("created_at, started_at, and finished_at are not in lifecycle order")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Cannot order completed run {run_dir.name} by valid lifecycle metadata: {exc}") from exc
        candidates.append((finished, state, run_dir))
    if not candidates:
        raise ValueError("No completed Traffic Lab run with valid state and matching main telemetry was found.")
    newest_time = max(item[0] for item in candidates)
    newest = [item for item in candidates if item[0] == newest_time]
    if len(newest) != 1:
        raise ValueError(f"Ambiguous completed-run metadata: {len(newest)} runs share finished_at={newest_time.isoformat()}")
    _, state, run_dir = newest[0]
    # Validate only the newest completed run. Falling back to an older scenario
    # when the newest run's evidence is broken would misrepresent the selection.
    try:
        run_resolved = run_dir.resolve(strict=True)
        if run_resolved.parent != resolved_root or run_resolved == resolved_root:
            raise ValueError("selected run directory does not resolve directly within the approved Traffic Lab run root")
        if (state.get("experiment_id") != run_dir.name or state.get("process_alive") is not False
                or state.get("process_exit_code") != 0 or state.get("scenario") not in SCENARIOS):
            raise ValueError("completed run state has inconsistent identity, completion, or scenario evidence")
        output_dir = run_dir / "output"
        resolved_output = output_dir.resolve(strict=True)
        if output_dir.is_symlink() or resolved_output.parent != run_resolved:
            raise ValueError("run output directory does not resolve directly within the selected run")
        recorded_output = state.get("output_directory")
        expected_local = os.fspath(resolved_output)
        expected_wsl = _wsl_path(resolved_output)
        if not isinstance(recorded_output, str) or not _same_recorded_path(recorded_output, expected_local, expected_wsl):
            raise ValueError("state.json output_directory does not identify this run's isolated output directory")
        telemetry = output_dir / f"{state['scenario']}.jsonl"
        if telemetry.parent.is_symlink() or telemetry.is_symlink() or not telemetry.is_file() or telemetry.stat().st_size == 0:
            raise ValueError(f"scenario telemetry {telemetry.name!r} is missing, empty, or unsafe")
        if telemetry.resolve(strict=True).parent != resolved_output:
            raise ValueError(f"scenario telemetry {telemetry.name!r} resolves outside the selected run output directory")
        source_host = state.get("source_host")
        flows = state.get("flows")
        expected_sources = set(flows) if isinstance(flows, list) and all(item in {"h1", "h2", "h3", "h4"} for item in flows) else set()
        if not expected_sources:
            raise ValueError("state.json has invalid source_host/flows evidence")
        if state.get("execution_contract_version") == 3:
            if source_host not in {"h1", "h2", "h3", "h4", "all"}:
                raise ValueError("state.json has invalid source_host evidence")
            authoritative_flows = {"h1", "h2", "h3", "h4"} if source_host == "all" and state["scenario"] == "congestion" else {source_host}
        else:
            # Legacy contracts did not bind producer identity to the folder ID;
            # validate their records for a useful mismatch diagnosis, then fail.
            authoritative_flows = expected_sources
        if expected_sources != authoritative_flows or (source_host == "all" and state["scenario"] != "congestion"):
            raise ValueError("state.json source_host does not agree with its selected flows")
        finished_at = _state_timestamp(state.get("finished_at"), "finished_at")
        _validated_records(telemetry, state["experiment_id"], state["scenario"], expected_sources, finished_at=finished_at)
        if state.get("execution_contract_version") != 3:
            raise ValueError(
                f"execution contract version {state.get('execution_contract_version')!r} predates producer experiment-ID propagation; "
                "this completed run cannot prove telemetry identity. Run Traffic Lab again using the current execution service."
            )
    except (OSError, ValueError, TypeError) as exc:
        identity = state.get("experiment_id", run_dir.name)
        scenario = state.get("scenario", "unknown")
        raise ValueError(
            f"Newest completed run {identity} (scenario {scenario}, expected telemetry {scenario}.jsonl) was rejected: {exc}. "
            "No older run was selected; a new successful Traffic Lab experiment may be required."
        ) from exc
    return state, telemetry


def _state_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} has no timezone offset")
    return parsed.astimezone(timezone.utc)


def _wsl_path(path: Path) -> str:
    if os.name != "nt":
        return path.as_posix()
    windows = PureWindowsPath(os.fspath(path))
    if not windows.drive:
        return path.as_posix()
    drive = windows.drive.rstrip(":").lower()
    tail = "/".join(windows.parts[1:])
    return f"/mnt/{drive}/{tail}"


def _same_recorded_path(recorded: str, expected_local: str, expected_wsl: str) -> bool:
    value = recorded.strip()
    if ntpath.splitdrive(value)[0]:
        return ntpath.normcase(ntpath.normpath(value)) == ntpath.normcase(ntpath.normpath(expected_local))
    normalized = value.replace("\\", "/")
    return normalized == expected_wsl.replace("\\", "/")


def _first_record_identity(path: Path) -> tuple[object, object] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        return value.get("experiment_id"), value.get("scenario")
                    return None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return None


def main() -> int:
    try:
        configured_output = os.environ.get(REPORT_DIRECTORY_ENV)
        if not configured_output or not configured_output.strip():
            raise ValueError(f"{REPORT_DIRECTORY_ENV} must name an existing approved directory outside the repository")
        output_dir = Path(configured_output).expanduser()
        if not output_dir.is_absolute():
            raise ValueError(f"{REPORT_DIRECTORY_ENV} must be an absolute directory path")
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise ValueError(f"{REPORT_DIRECTORY_ENV} must name an existing directory; it is not created automatically")
        resolved_output_dir = output_dir.resolve(strict=True)
        if resolved_output_dir == ROOT or ROOT in resolved_output_dir.parents:
            raise ValueError(f"{REPORT_DIRECTORY_ENV} must be outside the repository")
        resolved_runs_dir = RUNS_DIR.resolve(strict=False)
        if resolved_output_dir == resolved_runs_dir or resolved_runs_dir in resolved_output_dir.parents:
            raise ValueError(f"{REPORT_DIRECTORY_ENV} must be outside the Traffic Lab evidence directory")
        if not MODEL.is_file():
            raise ValueError(f"Saved ML model not found: {MODEL}")
        state, telemetry = _newest_completed_run()
        experiment_id, scenario = state["experiment_id"], state["scenario"]
        finished_at = _state_timestamp(state.get("finished_at"), "finished_at")
        print(f"Selected experiment: {experiment_id}\nScenario: {scenario}\nExpected telemetry: {scenario}.jsonl")
        configured_telemetry = os.environ.get(TELEMETRY_FILE_ENV)
        if configured_telemetry and configured_telemetry.strip():
            override = Path(configured_telemetry).expanduser()
            if any(component.is_symlink() for component in (override, *override.parents)) or not override.is_file():
                raise ValueError(f"Selected experiment {experiment_id} ({scenario}): {TELEMETRY_FILE_ENV} is not an accessible regular JSONL file")
            if override.resolve(strict=True) != telemetry.resolve(strict=True):
                observed = _first_record_identity(override)
                observed_text = (
                    f"record 1 observed experiment_id={observed[0]!r}, scenario={observed[1]!r}"
                    if observed is not None else "record 1 identity unavailable"
                )
                raise ValueError(
                    f"Selected experiment {experiment_id} (scenario {scenario}): expected experiment_id={experiment_id!r}, "
                    f"scenario={scenario!r} in telemetry {scenario}.jsonl "
                    f"from that run's output directory; configured file {override.name!r} is outside that selection, and {observed_text}. "
                    f"Use the selected run's scenario file or clear {TELEMETRY_FILE_ENV}."
                )
            telemetry = override
        source_host = state["source_host"]
        expected_sources = set(state["flows"])
        count, latest = _validated_records(
            telemetry, experiment_id, scenario, expected_sources,
            finished_at=finished_at, check_freshness=True,
        )
        age = (datetime.now(timezone.utc) - latest).total_seconds()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = resolved_output_dir / f"report_{scenario}_{experiment_id}_{stamp}.json"
        print(f"Experiment ID: {experiment_id}\nScenario: {scenario}\nInput: {telemetry}\nMeasurement age: {age:.1f}s\nInput records: {count}\nOutput: {output}")
        if age < -MAX_FUTURE_SKEW_SECONDS:
            raise ValueError(f"Telemetry timestamp is {abs(age):.0f}s in the future; refusing to report.")
        if age > MAX_AGE_SECONDS:
            raise ValueError(f"Telemetry is stale ({age:.0f}s old; maximum {MAX_AGE_SECONDS}s). Run Traffic Lab again.")
        # Runner's O_EXCL creation is the final no-overwrite guarantee.
        try:
            report = run_experiment(
                telemetry, output, mode="adaptive", model_path=MODEL,
                qos_config=QoSIntegrationConfig(
                    interface="s1-eth5", total_bandwidth_mbps=10.0,
                    flow_policy_classes={int(k): v for k, v in FLOW_CLASSES.items()},
                    filter_priorities={int(k): v for k, v in FILTER_PRIORITIES.items()}, dry_run=True,
                ), enforce_freshness=True, require_queue_measurements=True,
                experiment_metadata={
                    "experiment_id": experiment_id,
                    "scenario": scenario,
                    "source_host": source_host,
                    "sources": sorted(expected_sources),
                    "measurement_count": count,
                    "latest_measurement_timestamp": latest.isoformat(),
                    "telemetry_source": "configured" if configured_telemetry else "completed_run",
                },
            )
        except Exception as exc:
            raise ValueError(f"Offline model/report generation failed: {type(exc).__name__}: {exc}") from exc
        if report["status"] != "planned":
            raise ValueError(f"Offline runner returned {report['status']}; inspect rejected records in {output}")
        print(f"Report records: {report['counts']['records_processed']}")
        return 0
    except (OSError, ValueError, FileExistsError, TypeError) as exc:
        print(f"report.py: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"report.py: unexpected generation failure ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
