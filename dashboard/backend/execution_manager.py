"""Windows-to-WSL adapter for allowlisted Mininet traffic and QoS operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WSL_DISTRO_ENV = "ADAPTIVE_QOS_WSL_DISTRO"
DEFAULT_WSL_DISTRO = "Ubuntu"
_DISTRO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ExecutionUnavailable(RuntimeError):
    pass


class TopologyUnavailable(RuntimeError):
    pass


class ExecutionNotFound(RuntimeError):
    pass


class QosVerificationFailed(ExecutionUnavailable):
    pass


def _wsl_executable() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "wsl.exe"
    return str(candidate) if candidate.is_file() else "wsl.exe"


def _distribution() -> str:
    value = os.environ.get(WSL_DISTRO_ENV, DEFAULT_WSL_DISTRO).strip()
    if not _DISTRO_PATTERN.fullmatch(value):
        raise ExecutionUnavailable("Configured WSL distribution name is invalid.")
    return value


def _run_wsl(arguments: list[str], *, timeout: float = 20) -> dict[str, Any] | None:
    if os.name != "nt":
        raise ExecutionUnavailable("Guarded traffic execution requires the Windows-hosted API and WSL.")
    command = [
        _wsl_executable(), "--distribution", _distribution(), "--user", "root",
        "--exec", *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionUnavailable("Could not communicate with the configured WSL distribution.") from exc
    if result.returncode != 0:
        message = result.stdout.strip()
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            error = payload["error"]
            if payload.get("error_kind") == "verification":
                raise QosVerificationFailed(
                    "QoS application completed without matching kernel readback."
                )
            if payload.get("error_kind") == "topology":
                raise TopologyUnavailable(error)
            if payload.get("error_kind") == "conflict" and "not found" in error.lower():
                raise ExecutionNotFound(error)
            if payload.get("error_kind") == "conflict":
                raise ExecutionUnavailable(error)
        raise ExecutionUnavailable("The WSL execution supervisor returned an error.")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExecutionUnavailable("The WSL execution supervisor returned an invalid response.") from exc
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ExecutionUnavailable("The WSL execution supervisor returned an invalid response.")
    return payload


def _wsl_project_root() -> str:
    if os.name != "nt":
        raise ExecutionUnavailable("Guarded traffic execution requires the Windows-hosted API and WSL.")
    try:
        result = subprocess.run(
            [
                _wsl_executable(), "--distribution", _distribution(), "--user", "root",
                "--exec", "wslpath", "-a", str(PROJECT_ROOT),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionUnavailable("Could not resolve the repository path in WSL.") from exc
    root = result.stdout.strip()
    if result.returncode != 0 or not re.match(r"^/mnt/[a-z]/", root):
        raise ExecutionUnavailable("The repository is not available through a supported WSL mount.")
    return root


def invoke_supervisor(
    action: str,
    experiment_id: str | None = None,
    scenario: str | None = None,
    rate_mbps: float | None = None,
    duration_seconds: int | None = None,
    source_host: str | None = None,
) -> dict[str, Any] | None:
    if action == "start" and (rate_mbps is None) != (duration_seconds is None):
        raise ExecutionUnavailable("Rate and duration must be provided together.")
    if action == "start":
        if not isinstance(scenario, str) or scenario not in {"normal", "moderate", "congestion", "burst"}:
            raise ExecutionUnavailable("Unsupported traffic scenario.")
        if source_host is not None and (
            not isinstance(source_host, str)
            or source_host not in {"h1", "h2", "h3", "h4", "all"}
        ):
            raise ExecutionUnavailable("Unsupported traffic source host.")
        if source_host == "all" and scenario != "congestion":
            raise ExecutionUnavailable("All flows are supported only for the congestion scenario.")
        if source_host is not None and rate_mbps is None:
            raise ExecutionUnavailable("Rate and duration are required with an explicit source host.")
    project_root = _wsl_project_root()
    helper = f"{project_root.rstrip('/')}/dashboard/backend/wsl_experiment_supervisor.py"
    arguments = ["python3", helper, action]
    if experiment_id is not None:
        arguments.append(experiment_id)
    if scenario is not None:
        arguments.append(scenario)
    if rate_mbps is not None:
        arguments.append(format(rate_mbps, ".10g"))
    if duration_seconds is not None:
        arguments.append(str(duration_seconds))
    if action == "start" and source_host is not None:
        arguments.append(source_host)
    return _run_wsl(arguments, timeout=30 if action == "stop" else 15)


def invoke_qos_profile(profile: str) -> dict[str, Any]:
    if not isinstance(profile, str) or profile not in {"LOW", "MEDIUM", "HIGH"}:
        raise ExecutionUnavailable("Unsupported QoS profile.")
    if os.name != "nt":
        raise ExecutionUnavailable("Guarded QoS application requires the Windows-hosted API and WSL.")
    project_root = _wsl_project_root()
    helper = f"{project_root.rstrip('/')}/dashboard/backend/wsl_experiment_supervisor.py"
    result = _run_wsl(["python3", helper, "qos-apply", profile], timeout=30)
    if result is None:
        raise ExecutionUnavailable("The WSL QoS supervisor returned no application result.")
    return result


def inspect_qos_profile() -> dict[str, Any]:
    if os.name != "nt":
        raise ExecutionUnavailable("Guarded QoS inspection requires the Windows-hosted API and WSL.")
    project_root = _wsl_project_root()
    helper = f"{project_root.rstrip('/')}/dashboard/backend/wsl_experiment_supervisor.py"
    result = _run_wsl(["python3", helper, "qos-status"], timeout=30)
    if result is None:
        raise ExecutionUnavailable("The WSL QoS supervisor returned no inspection result.")
    return result
