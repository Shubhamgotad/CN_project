"""WSL-side, allowlisted supervisor for the existing Mininet traffic producer."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(PROJECT_ROOT))
from traffic.traffic_generator import CLIENT_FLOW_PORTS

RUN_ROOT = PROJECT_ROOT.parent / ".adaptive-qos-runs"
MAX_RUNTIME_SECONDS = 90
GRACEFUL_STOP_SECONDS = 18
FORCED_STOP_SECONDS = 5
SCENARIOS = {
    "normal": {"flows": ["h1"], "rate_mbps": 3, "duration_seconds": 20},
    "moderate": {"flows": ["h1"], "rate_mbps": 6, "duration_seconds": 20},
    "congestion": {"flows": ["h1", "h2", "h3", "h4"], "rate_mbps": 4, "duration_seconds": 20},
    "burst": {"flows": ["h1"], "rate_mbps": 8, "duration_seconds": 23},
}
MIN_RATE_MBPS = 0.1
MAX_RATE_MBPS = 10.0
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 60


def _execution_parameters(scenario: str, rate_mbps=None, duration_seconds=None):
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError("Unsupported traffic scenario.")
    defaults = SCENARIOS[scenario]
    rate = defaults["rate_mbps"] if rate_mbps is None else rate_mbps
    duration = defaults["duration_seconds"] if duration_seconds is None else duration_seconds
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate):
        raise ValueError("Rate must be a finite number between 0.1 and 10 Mbps per flow.")
    if not MIN_RATE_MBPS <= rate <= MAX_RATE_MBPS:
        raise ValueError("Rate must be between 0.1 and 10 Mbps per flow.")
    if abs(rate * 10 - round(rate * 10)) > 1e-8:
        raise ValueError("Rate must use 0.1 Mbps increments.")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValueError("Duration must be a whole number between 1 and 60 seconds.")
    if not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        raise ValueError("Duration must be between 1 and 60 seconds.")
    if scenario == "burst" and (float(rate) != 8.0 or duration != 23):
        raise ValueError("Burst uses a fixed 8 Mbps flow schedule lasting 23 seconds.")
    return float(rate), duration


def _selected_source_host(scenario: str, source_host: str | None) -> str:
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError("Unsupported traffic scenario.")
    if source_host is None:
        return "all" if scenario == "congestion" else "h1"
    if not isinstance(source_host, str) or source_host not in {*CLIENT_FLOW_PORTS, "all"}:
        raise ValueError("Unsupported traffic source host.")
    if source_host == "all" and scenario != "congestion":
        raise ValueError("All flows are supported only for the congestion scenario.")
    return source_host


def _planned_flows(scenario: str, source_host: str) -> list[str]:
    if source_host == "all":
        return list(SCENARIOS[scenario]["flows"])
    return [source_host]


def _rate_arg(rate: float) -> str:
    return format(rate, ".10g")


def _tracked_parameters(
    state: dict[str, Any],
) -> tuple[Any, Any, str | None, str | None]:
    version = state.get("execution_contract_version")
    if version == 3:
        return (
            state["target_rate_mbps"],
            state["duration_seconds"],
            state["source_host"],
            state["experiment_id"],
        )
    if version == 2:
        return state["target_rate_mbps"], state["duration_seconds"], None, None
    return None, None, None, None


def _wait_for_supervisor_ready(experiment_id: str, process, *, timeout: float = 5.0) -> dict[str, Any]:
    """Do not return Start until cooperative Stop handling is installed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The execution supervisor exited before Stop handling became ready.")
        try:
            state = _read_state(experiment_id)
        except (OSError, ValueError):
            state = None
        if state and state.get("supervisor_ready") and _supervisor_matches(state):
            return state
        time.sleep(0.02)
    raise TimeoutError("The execution supervisor did not become ready to handle Stop.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _experiment_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid experiment identifier.") from exc
    if parsed.hex != value:
        raise ValueError("Invalid experiment identifier.")
    return value


def _run_dir(experiment_id: str) -> Path:
    return RUN_ROOT / _experiment_id(experiment_id)


def _state_path(experiment_id: str) -> Path:
    return _run_dir(experiment_id) / "state.json"


def _read_state(experiment_id: str) -> dict[str, Any]:
    directory = _run_dir(experiment_id)
    path = _state_path(experiment_id)
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Experiment was not found.")
    if path.resolve(strict=True).parent != directory.resolve(strict=True):
        raise FileNotFoundError("Experiment was not found.")
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or payload.get("experiment_id") != experiment_id:
        raise ValueError("Experiment state is invalid.")
    if payload.get("execution_contract_version") == 3:
        scenario = payload.get("scenario")
        source_host = _selected_source_host(scenario, payload.get("source_host"))
        rate, duration = _execution_parameters(
            scenario,
            payload.get("target_rate_mbps"),
            payload.get("duration_seconds"),
        )
        if (
            payload.get("source_host") != source_host
            or payload.get("flows") != _planned_flows(scenario, source_host)
            or payload.get("target_rate_mbps") != rate
            or payload.get("duration_seconds") != duration
        ):
            raise ValueError("Experiment state is invalid.")
    return payload


def _write_state(experiment_id: str, payload: dict[str, Any]) -> None:
    directory = _run_dir(experiment_id)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Experiment output directory is unsafe.")
    target = _state_path(experiment_id)
    temporary = directory / f".state-{os.getpid()}.tmp"
    if target.is_symlink() or temporary.is_symlink():
        raise RuntimeError("Experiment state path is unsafe.")
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def _locked_root():
    import fcntl

    RUN_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUN_ROOT.is_symlink() or not RUN_ROOT.is_dir():
        raise RuntimeError("Experiment output root is unsafe.")
    lock_path = RUN_ROOT / ".execution.lock"
    if lock_path.is_symlink():
        raise RuntimeError("Experiment lock path is unsafe.")
    lock = lock_path.open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    return lock


def _proc_stat(pid: int) -> tuple[str, int, int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        return fields[0], int(fields[2]), int(fields[3]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _pid_start_ticks(pid: int | None) -> int | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    value = _proc_stat(pid)
    return value[3] if value else None


def _supervisor_matches(state: dict[str, Any]) -> bool:
    pid = state.get("supervisor_pid")
    expected_ticks = state.get("supervisor_start_ticks")
    if not isinstance(pid, int) or _pid_start_ticks(pid) != expected_ticks:
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if args and args[-1] == b"":
            args.pop()
        actual = [os.fsdecode(part) for part in args]
        executable = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return False
    expected = [
        sys.executable,
        os.fspath(Path(__file__).resolve()),
        "run",
        state["experiment_id"],
        state["scenario"],
    ]
    if state.get("execution_contract_version") == 2:
        expected.extend([
            _rate_arg(float(state["target_rate_mbps"])),
            str(state["duration_seconds"]),
        ])
    elif state.get("execution_contract_version") == 3:
        expected.extend([
            _rate_arg(float(state["target_rate_mbps"])),
            str(state["duration_seconds"]),
            state["source_host"],
        ])
    return actual == expected and executable.startswith("python3")


def _producer_matches(
    pid: int,
    start_ticks: int,
    scenario: str,
    output_dir: Path,
    rate_mbps=None,
    duration_seconds=None,
    source_host=None,
    experiment_id=None,
) -> bool:
    if _pid_start_ticks(pid) != start_ticks:
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if args and args[-1] == b"":
            args.pop()
        actual = [os.fsdecode(part) for part in args]
        executable = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return False
    rate, duration = _execution_parameters(scenario, rate_mbps, duration_seconds)
    expected = [
        sys.executable,
        os.fspath((PROJECT_ROOT / "traffic" / "traffic_generator.py").resolve()),
        "--scenario", scenario,
    ]
    if rate_mbps is not None and duration_seconds is not None:
        expected.extend(["--rate-mbps", _rate_arg(rate), "--duration-seconds", str(duration)])
    if source_host is not None:
        expected.extend(["--source-host", _selected_source_host(scenario, source_host)])
    if experiment_id is not None:
        expected.extend(["--experiment-id", experiment_id])
    expected.extend(["--output-dir", os.fspath(output_dir)])
    return actual == expected and executable.startswith("python3")


def _run_readonly(command: list[str], *, timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def verify_topology() -> dict[str, Any]:
    """Read-only check for this repo's Mininet namespace/interface contract."""
    if shutil.which("iperf3") is None:
        raise RuntimeError("The WSL iperf3 executable required by the producer is unavailable.")
    sys.path.insert(0, os.fspath(PROJECT_ROOT))
    from traffic import traffic_generator

    hosts = {}
    for host in sorted(traffic_generator.MININET_HOSTS):
        result = _run_readonly(["pgrep", "-f", f"mininet:{host}"])
        if result.returncode != 0:
            raise RuntimeError(f"Mininet host {host} is not available.")
        pid, start_ticks = traffic_generator.resolve_mininet_namespace_pid(
            host,
            result.stdout.splitlines(),
        )
        interface = traffic_generator.MININET_HOST_INTERFACES[host]
        link = traffic_generator.validate_host_interface_identity(host, interface, pid)
        if "UP" not in link.get("flags", []):
            raise RuntimeError(f"Mininet interface for {host} is not up.")
        hosts[host] = {"pid": int(pid), "start_ticks": int(start_ticks), "interface": interface}

    addresses = _run_readonly([
        "mnexec", "-a", str(hosts["server"]["pid"]),
        "ip", "-j", "-4", "addr", "show", "dev", "server-eth0",
    ])
    if addresses.returncode != 0:
        raise RuntimeError("Could not verify the Mininet server address.")
    try:
        addr_payload = json.loads(addresses.stdout)
        server_addresses = {
            item.get("local")
            for item in addr_payload[0].get("addr_info", [])
            if isinstance(item, dict)
        }
    except (ValueError, TypeError, IndexError, AttributeError):
        raise RuntimeError("Could not verify the Mininet server address.") from None
    if "10.0.0.5" not in server_addresses:
        raise RuntimeError("The Mininet server is not configured at 10.0.0.5.")

    bottleneck = _run_readonly(["ip", "-j", "link", "show", "dev", "s1-eth5"])
    if bottleneck.returncode != 0:
        raise RuntimeError("The configured s1-eth5 bottleneck interface is unavailable.")
    try:
        link_payload = json.loads(bottleneck.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("Could not verify the configured bottleneck interface.") from None
    if not link_payload or link_payload[0].get("ifname") != "s1-eth5" or "UP" not in link_payload[0].get("flags", []):
        raise RuntimeError("The configured s1-eth5 bottleneck interface is not up.")

    bridges = _run_readonly(["ovs-vsctl", "list-br"])
    if bridges.returncode != 0 or not {"s1", "s2"}.issubset(set(bridges.stdout.splitlines())):
        raise RuntimeError("The expected OVS bridges s1 and s2 are unavailable.")
    for bridge, required_ports in {
        "s1": {"s1-eth1", "s1-eth2", "s1-eth3", "s1-eth4", "s1-eth5"},
        "s2": {"s2-eth1", "s2-eth2"},
    }.items():
        ports = _run_readonly(["ovs-vsctl", "list-ports", bridge])
        if ports.returncode != 0 or not required_ports.issubset(set(ports.stdout.splitlines())):
            raise RuntimeError(f"The expected Mininet interfaces on bridge {bridge} are unavailable.")
    qdiscs = _run_readonly(["tc", "-d", "qdisc", "show", "dev", "s1-eth5"])
    classes = _run_readonly(["tc", "-d", "class", "show", "dev", "s1-eth5"])
    if qdiscs.returncode != 0 or not any(
        "netem" in line and "delay 10ms" in line for line in qdiscs.stdout.splitlines()
    ):
        raise RuntimeError("The configured s1-eth5 10 ms bottleneck delay is unavailable.")
    aggregate_10mbit = any(
        line.startswith("class ") and "rate 10Mbit" in line and "ceil 10Mbit" in line
        for line in classes.stdout.splitlines()
    )
    if classes.returncode != 0 or not aggregate_10mbit:
        raise RuntimeError("The configured s1-eth5 10 Mbps aggregate bottleneck is unavailable.")
    return {"hosts": sorted(hosts), "bridges": ["s1", "s2"], "bottleneck_interface": "s1-eth5"}


def _scenario_state(experiment_id: str, scenario: str, rate_mbps=None, duration_seconds=None, source_host=None) -> dict[str, Any]:
    rate_mbps, duration_seconds = _execution_parameters(scenario, rate_mbps, duration_seconds)
    source_host = _selected_source_host(scenario, source_host)
    now = _utc_now()
    return {
        "experiment_id": experiment_id,
        "execution_contract_version": 3,
        "supervisor_ready": False,
        "scenario": scenario,
        "source_host": source_host,
        "status": "starting",
        "flows": _planned_flows(scenario, source_host),
        "target_rate_mbps": rate_mbps,
        "duration_seconds": duration_seconds,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": 0.0,
        "process_alive": True,
        "can_stop": True,
        "process_exit_code": None,
        "error": None,
        "supervisor_pid": None,
        "supervisor_start_ticks": None,
        "producer_pid": None,
        "producer_start_ticks": None,
        "producer_process_group": None,
    }


def start_experiment(experiment_id: str, scenario: str, rate_mbps=None, duration_seconds=None, source_host=None) -> dict[str, Any]:
    experiment_id = _experiment_id(experiment_id)
    rate_mbps, duration_seconds = _execution_parameters(scenario, rate_mbps, duration_seconds)
    source_host = _selected_source_host(scenario, source_host)
    topology = verify_topology()
    expected_hosts = _planned_flows(scenario, source_host)
    if any(host not in topology["hosts"] for host in expected_hosts):
        raise RuntimeError("A selected Mininet client is not available.")
    lock = _locked_root()
    try:
        for path in RUN_ROOT.iterdir():
            if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
                continue
            try:
                existing = _read_state(path.name)
            except (ValueError, OSError, KeyError):
                continue
            if existing.get("process_alive"):
                if _supervisor_matches(existing):
                    raise RuntimeError("Another guarded traffic experiment is already active.")
                producer_pid = existing.get("producer_pid")
                producer_ticks = existing.get("producer_start_ticks")
                process_group = existing.get("producer_process_group")
                if isinstance(producer_pid, int) and process_group == producer_pid:
                    if _pid_start_ticks(producer_pid) == producer_ticks or _process_group_has_live_members(producer_pid):
                        raise RuntimeError("Another workload is active but its supervisor cannot be verified.")

        run_dir = _run_dir(experiment_id)
        run_dir.mkdir(mode=0o700)
        output_dir = run_dir / "output"
        payload = _scenario_state(experiment_id, scenario, rate_mbps, duration_seconds, source_host)
        payload["topology_verified"] = topology
        _write_state(experiment_id, payload)
        log_path = run_dir / "supervisor.log"
        with log_path.open("xb") as log:
            process = subprocess.Popen(
                [sys.executable, os.fspath(Path(__file__).resolve()), "run", experiment_id, scenario, _rate_arg(rate_mbps), str(duration_seconds), source_host],
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        ticks = _pid_start_ticks(process.pid)
        if ticks is None:
            raise RuntimeError("Could not verify the new experiment supervisor process.")
        payload["supervisor_pid"] = process.pid
        payload["supervisor_start_ticks"] = ticks
        _write_state(experiment_id, payload)
        try:
            return _wait_for_supervisor_ready(experiment_id, process)
        except (RuntimeError, TimeoutError):
            # The supervisor was created in its own session. If its readiness
            # handshake fails, terminate only that verified startup group.
            stopped = _terminate_owned_process_group(
                process.pid,
                ticks,
                validate_leader=lambda: _supervisor_matches(payload),
            )
            payload.update({
                "status": "failed",
                "process_alive": not stopped,
                "can_stop": not stopped,
                "finished_at": _utc_now() if stopped else None,
                "error": "Supervisor startup failed before Stop handling was ready.",
            })
            _write_state(experiment_id, payload)
            raise
    finally:
        lock.close()


def _process_group_has_live_members(pgid: int) -> bool:
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        value = _proc_stat(int(entry.name))
        # A separate session is created for each producer. Matching both PGID
        # and SID identifies only that workload, even if its leader has exited.
        if value and value[1] == pgid and value[2] == pgid and value[0] not in {"Z", "X"}:
            return True
    return False


def _terminate_owned_process_group(pid: int, start_ticks: int, *, validate_leader) -> bool:
    try:
        group_alive = _process_group_has_live_members(pid)
        leader_valid = validate_leader()
        if not group_alive and not leader_valid:
            return True
        os.killpg(pid, signal.SIGINT)
        for timeout, sig in (
            (GRACEFUL_STOP_SECONDS, None),
            (FORCED_STOP_SECONDS, signal.SIGTERM),
            (FORCED_STOP_SECONDS, signal.SIGKILL),
        ):
            deadline = time.monotonic() + timeout
            while _process_group_has_live_members(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not _process_group_has_live_members(pid):
                break
            if sig is not None:
                # The PGID and SID were both created by this run. Do not signal
                # if the group vanished while waiting.
                if not _process_group_has_live_members(pid):
                    break
                os.killpg(pid, sig)
        return not _process_group_has_live_members(pid)
    except OSError:
        return False


def _stop_producer(process: subprocess.Popen[bytes], start_ticks: int) -> bool:
    pid = process.pid
    identity = lambda: (
        process.poll() is None
        and _pid_start_ticks(pid) == start_ticks
        and os.getpgid(pid) == pid
    )
    if process.poll() is None and not identity():
        return False
    stopped = _terminate_owned_process_group(pid, start_ticks, validate_leader=identity)
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired:
        return False
    return stopped


def _worker(experiment_id: str, scenario: str, rate_mbps=None, duration_seconds=None, source_host=None) -> None:
    rate_mbps, duration_seconds = _execution_parameters(scenario, rate_mbps, duration_seconds)
    source_host = _selected_source_host(scenario, source_host)
    stop_requested = threading.Event()
    signal.signal(signal.SIGUSR1, lambda *_: stop_requested.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_requested.set())
    signal.signal(signal.SIGINT, lambda *_: stop_requested.set())

    deadline = time.monotonic() + 5
    state = None
    while time.monotonic() < deadline:
        try:
            state = _read_state(experiment_id)
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        if state.get("supervisor_pid") == os.getpid():
            state["supervisor_ready"] = True
            _write_state(experiment_id, state)
            break
        time.sleep(0.05)
    if state is None or state.get("supervisor_pid") != os.getpid():
        return

    started_clock = time.monotonic()
    producer: subprocess.Popen[bytes] | None = None
    producer_ticks: int | None = None
    try:
        if (
            state.get("execution_contract_version") != 3
            or state.get("scenario") != scenario
            or state.get("source_host") != source_host
            or state.get("target_rate_mbps") != rate_mbps
            or state.get("duration_seconds") != duration_seconds
            or state.get("flows") != _planned_flows(scenario, source_host)
        ):
            raise RuntimeError("Experiment state does not match the validated execution request.")
        topology = verify_topology()
        expected_hosts = _planned_flows(scenario, source_host)
        if any(host not in topology["hosts"] for host in expected_hosts):
            raise RuntimeError("A selected Mininet client is not available.")
        if stop_requested.is_set():
            state.update({"status": "stopped", "process_alive": False, "can_stop": False})
            return
        output_dir = _run_dir(experiment_id) / "output"
        output_log = _run_dir(experiment_id) / "producer.log"
        with output_log.open("xb") as log:
            argv = [
                sys.executable,
                os.fspath((PROJECT_ROOT / "traffic" / "traffic_generator.py").resolve()),
                "--scenario", scenario,
                "--rate-mbps", _rate_arg(rate_mbps),
                "--duration-seconds", str(duration_seconds),
                "--source-host", source_host,
                "--experiment-id", experiment_id,
                "--output-dir", os.fspath(output_dir),
            ]
            producer = subprocess.Popen(
                argv,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        producer_ticks = _pid_start_ticks(producer.pid)
        if producer_ticks is None:
            raise RuntimeError("Could not verify the producer process identity.")
        state.update({
            "status": "running",
            "started_at": _utc_now(),
            "producer_pid": producer.pid,
            "producer_start_ticks": producer_ticks,
            "producer_process_group": producer.pid,
            "output_directory": os.fspath(output_dir),
        })
        _write_state(experiment_id, state)
        max_end = started_clock + MAX_RUNTIME_SECONDS

        while producer.poll() is None:
            elapsed = time.monotonic() - started_clock
            state["elapsed_seconds"] = round(elapsed, 3)
            if stop_requested.is_set():
                state["status"] = "stopping"
                _write_state(experiment_id, state)
                stopped = _stop_producer(producer, producer_ticks)
                state["process_exit_code"] = producer.poll()
                state["process_alive"] = not stopped
                state["can_stop"] = not stopped and _producer_matches(
                    producer.pid, producer_ticks, scenario, output_dir,
                    rate_mbps, duration_seconds, source_host, experiment_id,
                )
                state["status"] = "stopped" if stopped else "failed"
                state["error"] = None if stopped else "Producer termination could not be verified; workload state is uncertain."
                break
            if time.monotonic() >= max_end:
                state["status"] = "stopping"
                state["error"] = "The producer exceeded the 90-second maximum runtime; stopping the owned workload."
                _write_state(experiment_id, state)
                stopped = _stop_producer(producer, producer_ticks)
                state["process_exit_code"] = producer.poll()
                state["process_alive"] = not stopped
                state["can_stop"] = False
                state["status"] = "failed"
                if not stopped:
                    state["error"] = "Maximum runtime exceeded and producer termination could not be verified."
                break
            time.sleep(0.2)
        else:
            state["process_exit_code"] = producer.returncode
            state["process_alive"] = _process_group_has_live_members(producer.pid)
            state["can_stop"] = False
            if state["process_alive"]:
                state["status"] = "failed"
                state["error"] = "Producer exited while owned child processes remain; termination is not verified."
            elif producer.returncode == 0:
                state["status"] = "completed"
                state["error"] = None
            else:
                state["status"] = "failed"
                state["error"] = f"Producer exited with code {producer.returncode}. See the local experiment log for details."
    except Exception as exc:
        state["status"] = "failed"
        state["process_alive"] = bool(
            producer is not None and producer.poll() is None
        ) or bool(
            producer is not None and _process_group_has_live_members(producer.pid)
        )
        state["can_stop"] = bool(
            producer is not None
            and producer_ticks is not None
            and producer.poll() is None
            and _producer_matches(
                producer.pid,
                producer_ticks,
                scenario,
                _run_dir(experiment_id) / "output",
                rate_mbps,
                duration_seconds,
                source_host,
                experiment_id,
            )
        )
        state["error"] = str(exc)[:240] if isinstance(exc, (RuntimeError, ValueError)) else "Producer failed unexpectedly."
    finally:
        state["elapsed_seconds"] = round(time.monotonic() - started_clock, 3)
        if state.get("status") in {"completed", "stopped", "failed"}:
            state["finished_at"] = _utc_now()
        _write_state(experiment_id, state)


def status_experiment(experiment_id: str) -> dict[str, Any]:
    state = _read_state(_experiment_id(experiment_id))
    pid = state.get("supervisor_pid")
    alive = _supervisor_matches(state)
    if state.get("status") in {"starting", "running", "stopping"} and not alive:
        producer_pid = state.get("producer_pid")
        producer_ticks = state.get("producer_start_ticks")
        producer_alive = isinstance(producer_pid, int) and _pid_start_ticks(producer_pid) == producer_ticks
        group_alive = (
            isinstance(producer_pid, int)
            and state.get("producer_process_group") == producer_pid
            and _process_group_has_live_members(producer_pid)
        )
        if producer_alive or group_alive:
            state["status"] = "failed"
            state["process_alive"] = True
            output_dir = _run_dir(experiment_id) / "output"
            safe_producer = (
                producer_alive
                and isinstance(producer_ticks, int)
                and _producer_matches(producer_pid, producer_ticks, state["scenario"], output_dir, *_tracked_parameters(state))
                and state.get("producer_process_group") == producer_pid
                and os.getpgid(producer_pid) == producer_pid
            )
            state["can_stop"] = safe_producer
            state["error"] = "Execution supervisor exited; the tracked workload requires explicit recovery stop." if safe_producer else "Execution supervisor exited; workload termination cannot be verified automatically."
        else:
            state["status"] = "failed"
            state["process_alive"] = False
            state["can_stop"] = False
            state["error"] = "Execution supervisor exited before recording a final result."
        state["finished_at"] = _utc_now()
        _write_state(experiment_id, state)
    elif alive:
        if state.get("status") in {"completed", "stopped"}:
            state["process_alive"] = False
        elif state.get("status") == "failed":
            producer_pid = state.get("producer_pid")
            producer_ticks = state.get("producer_start_ticks")
            output_dir = _run_dir(experiment_id) / "output"
            producer_matches = (
                isinstance(producer_pid, int)
                and isinstance(producer_ticks, int)
                and _producer_matches(producer_pid, producer_ticks, state["scenario"], output_dir, *_tracked_parameters(state))
            )
            group_alive = (
                isinstance(producer_pid, int)
                and state.get("producer_process_group") == producer_pid
                and _process_group_has_live_members(producer_pid)
            )
            state["process_alive"] = producer_matches or group_alive
            state["can_stop"] = producer_matches and group_alive
        else:
            state["process_alive"] = True
        if state.get("started_at"):
            start = datetime.fromisoformat(state["started_at"])
            state["elapsed_seconds"] = max(0.0, (datetime.now(timezone.utc) - start).total_seconds())
    return state


def active_experiment() -> dict[str, Any] | None:
    if not RUN_ROOT.exists() or RUN_ROOT.is_symlink():
        return None
    active: list[dict[str, Any]] = []
    for path in RUN_ROOT.iterdir():
        if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
            continue
        try:
            state = status_experiment(path.name)
        except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
            continue
        if state.get("process_alive") and (
            state.get("status") in {"starting", "running", "stopping", "failed"}
        ):
            active.append(state)
    if not active:
        return None
    active.sort(key=lambda item: item.get("created_at", ""))
    return active[-1]


def stop_experiment(experiment_id: str) -> dict[str, Any]:
    experiment_id = _experiment_id(experiment_id)
    state = status_experiment(experiment_id)
    if state.get("status") in {"completed", "stopped", "failed"} and not state.get("process_alive"):
        return state
    if state.get("status") == "stopping":
        return state
    if not _supervisor_matches(state):
        pid = state.get("producer_pid")
        ticks = state.get("producer_start_ticks")
        output_dir = _run_dir(experiment_id) / "output"
        if (
            not isinstance(pid, int)
            or not isinstance(ticks, int)
            or state.get("producer_process_group") != pid
            or (_pid_start_ticks(pid) is not None and _pid_start_ticks(pid) != ticks)
        ):
            raise RuntimeError("Tracked workload identity could not be verified; no signal was sent.")
        valid_producer = lambda: (
            _pid_start_ticks(pid) == ticks
            and _producer_matches(pid, ticks, state["scenario"], output_dir, *_tracked_parameters(state))
            and os.getpgid(pid) == pid
        )
        stopped = _terminate_owned_process_group(pid, ticks, validate_leader=valid_producer)
        state.update({
            "status": "stopped" if stopped else "failed",
            "process_alive": not stopped,
            "can_stop": False,
            "finished_at": _utc_now() if stopped else None,
            "error": None if stopped else "Tracked workload termination could not be verified.",
        })
        _write_state(experiment_id, state)
        return state
    state["status"] = "stopping"
    _write_state(experiment_id, state)
    try:
        os.kill(state["supervisor_pid"], signal.SIGUSR1)
    except ProcessLookupError:
        return status_experiment(experiment_id)
    return state


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        action = arguments[0]
        if action == "preflight" and len(arguments) == 1:
            result = {"status": "ready", "topology": verify_topology()}
        elif action == "start" and len(arguments) in {3, 5, 6}:
            rate = float(arguments[3]) if len(arguments) >= 5 else None
            duration = int(arguments[4]) if len(arguments) >= 5 else None
            source_host = arguments[5] if len(arguments) == 6 else None
            result = start_experiment(arguments[1], arguments[2], rate, duration, source_host)
        elif action == "run" and len(arguments) in {3, 5, 6}:
            rate = float(arguments[3]) if len(arguments) >= 5 else None
            duration = int(arguments[4]) if len(arguments) >= 5 else None
            source_host = arguments[5] if len(arguments) == 6 else None
            _worker(_experiment_id(arguments[1]), arguments[2], rate, duration, source_host)
            return 0
        elif action == "status" and len(arguments) == 2:
            result = status_experiment(arguments[1])
        elif action == "active" and len(arguments) == 1:
            result = active_experiment()
        elif action == "stop" and len(arguments) == 2:
            result = stop_experiment(arguments[1])
        else:
            raise ValueError("Unsupported supervisor operation.")
        print(json.dumps(result, allow_nan=False, separators=(",", ":")))
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({"error_kind": "conflict", "error": str(exc)}, separators=(",", ":")))
        return 2
    except RuntimeError as exc:
        message = str(exc)
        kind = "topology" if any(word in message.lower() for word in ("mininet", "interface", "bridge", "bottleneck", "server address")) else "conflict"
        print(json.dumps({"error_kind": kind, "error": message[:240]}, separators=(",", ":")))
        return 2
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({"error_kind": "execution", "error": "The guarded supervisor could not complete the requested operation."}, separators=(",", ":")))
        return 2
    except (ValueError, IndexError, KeyError) as exc:
        print(json.dumps({"error_kind": "validation", "error": str(exc)[:240]}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
