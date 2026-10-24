#!/usr/bin/env python3

import argparse
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from statistics import fmean
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemetry.iperf_parser import (
    parse_iperf_interval_events,
    parse_iperf_output,
    parse_iperf_sender_interval_events,
    write_interval_jsonl,
    write_jsonl,
)


SERVER_IP = "10.0.0.5"
DURATION = 20
BOTTLENECK_INTERFACE = "s1-eth5"
DATASET_DIR = Path(__file__).resolve().parents[1] / "telemetry" / "datasets"
INTERVAL_DATASET_DIR = DATASET_DIR
PROJECT_DIR = Path(__file__).resolve().parents[1]
MONITOR_FRESHNESS_SECONDS = 2.0
IPERF_SERVER_PORTS = range(5201, 5205)
IPERF_LISTENER_START_TIMEOUT_SECONDS = 5.0
MAX_INTERVAL_MATCH_TOLERANCE_SECONDS = 0.25
MININET_HOSTS = frozenset({"h1", "h2", "h3", "h4", "server"})
CLIENT_HOSTS = ("h1", "h2", "h3", "h4")
CLIENT_FLOW_PORTS = {"h1": 5201, "h2": 5202, "h3": 5203, "h4": 5204}
MININET_HOST_INTERFACES = {
    host: f"{host}-eth0" for host in MININET_HOSTS
}
_RUN_OUTPUT_DIRECTORIES = set()


def _read_process_start_time(pid):
    stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields_after_name = stat_fields[stat_fields.rfind(")") + 2 :].split()
    try:
        return fields_after_name[19]
    except IndexError as exc:
        raise RuntimeError(f"cannot verify process start time for PID {pid}") from exc


def _is_mininet_host_shell(host, pid, cmdline_reader, executable_reader):
    try:
        arguments = cmdline_reader(pid).split(b"\0")
        if arguments and arguments[-1] == b"":
            arguments.pop()
        executable = executable_reader(pid)
    except OSError as exc:
        raise RuntimeError(
            f"cannot verify Mininet namespace process {pid}"
        ) from exc
    expected_marker = f"mininet:{host}".encode()
    expected_arguments = (
        [b"--norc", b"-is", expected_marker],
        [b"--norc", b"--noediting", b"-is", expected_marker],
    )
    return (
        len(arguments) >= 4
        and Path(os.fsdecode(arguments[0])).name == "bash"
        and arguments[1:] in expected_arguments
        and Path(os.fsdecode(executable)).name == "bash"
    )


def resolve_mininet_namespace_pid(
    host,
    candidate_pids,
    *,
    cmdline_reader=None,
    executable_reader=None,
    starttime_reader=None,
):
    """Resolve exactly one Mininet bash host shell and capture its start time."""
    if host not in MININET_HOSTS:
        raise ValueError(f"unsupported Mininet host: {host!r}")
    read_cmdline = cmdline_reader or (
        lambda pid: Path(f"/proc/{pid}/cmdline").read_bytes()
    )
    read_executable = executable_reader or (
        lambda pid: os.readlink(f"/proc/{pid}/exe")
    )
    read_starttime = starttime_reader or _read_process_start_time
    matching_pids = []
    for candidate in candidate_pids:
        if not candidate.isdecimal() or int(candidate) <= 0:
            raise RuntimeError(f"invalid Mininet namespace PID: {candidate!r}")
        if _is_mininet_host_shell(
            host, candidate, read_cmdline, read_executable
        ):
            matching_pids.append((candidate, read_starttime(candidate)))
    if len(matching_pids) != 1:
        raise RuntimeError(
            f"Expected exactly one Mininet namespace process for {host}; "
            f"found {len(matching_pids)}. Verify the active Mininet topology "
            "and remove ambiguity before retrying."
        )
    return matching_pids[0]


def validate_host_interface_identity(host, interface, namespace_pid, *, runner=None):
    """Confirm the named interface exists in the selected host namespace."""
    if not isinstance(interface, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,15}", interface
    ):
        raise ValueError("interface must be a valid Linux interface name")
    execute = runner or subprocess.run
    result = execute(
        [
            "mnexec", "-a", namespace_pid,
            "ip", "-j", "-details", "link", "show", "dev", interface,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"interface {interface} is not available in Mininet namespace "
            f"{host} (pid {namespace_pid})"
        )
    try:
        links = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"could not verify interface identity for {interface} in {host}"
        ) from exc
    if (
        not isinstance(links, list)
        or len(links) != 1
        or not isinstance(links[0], dict)
        or links[0].get("ifname") != interface
    ):
        raise RuntimeError(
            f"interface identity mismatch: expected {interface} in namespace {host}"
        )
    return links[0]


def _run_in_verified_namespace(
    host,
    command,
    interface,
    candidate_pids,
    *,
    cmdline_reader,
    executable_reader,
    starttime_reader,
    interface_validator,
    command_launcher,
):
    namespace_pid, initial_starttime = resolve_mininet_namespace_pid(
        host,
        candidate_pids,
        cmdline_reader=cmdline_reader,
        executable_reader=executable_reader,
        starttime_reader=starttime_reader,
    )
    interface_validator(host, interface, namespace_pid)
    try:
        revalidated = resolve_mininet_namespace_pid(
            host,
            [namespace_pid],
            cmdline_reader=cmdline_reader,
            executable_reader=executable_reader,
            starttime_reader=starttime_reader,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"Mininet namespace process {namespace_pid} exited or changed "
            "identity during validation"
        ) from exc
    if revalidated != (namespace_pid, initial_starttime):
        raise RuntimeError(
            f"Mininet namespace process {namespace_pid} exited or changed "
            "identity during validation"
        )
    return command_launcher(
        ["mnexec", "-a", namespace_pid, *command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def run_on_host(host, command, *, interface):
    """Run a command after exact host-shell and interface validation.

    The PID is revalidated immediately before ``mnexec``. Without a pidfd-aware
    launcher/setns execution model, a small PID exit/reuse race remains between
    that last check and the subprocess creation.
    """
    if host not in MININET_HOSTS:
        raise ValueError(f"unsupported Mininet host: {host!r}")
    if interface != MININET_HOST_INTERFACES[host]:
        raise ValueError(
            f"interface {interface!r} does not belong to Mininet host {host}"
        )
    result = subprocess.run(
        ["pgrep", "-f", f"mininet:{host}"],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode == 1:
        raise RuntimeError(
            f"Expected exactly one Mininet namespace process for {host}; "
            "found 0. Make sure the intended Mininet topology is running."
        )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )

    print(f"[TRAFFIC] {host}: {' '.join(command)}")
    return _run_in_verified_namespace(
        host,
        command,
        interface,
        result.stdout.splitlines(),
        cmdline_reader=lambda pid: Path(f"/proc/{pid}/cmdline").read_bytes(),
        executable_reader=lambda pid: os.readlink(f"/proc/{pid}/exe"),
        starttime_reader=_read_process_start_time,
        interface_validator=validate_host_interface_identity,
        command_launcher=subprocess.Popen,
    )


def _prepare_output_directory(output_directory):
    if output_directory is None:
        raise ValueError(
            "an explicit isolated output_directory is required; "
            "the project dataset directory is not a safe default"
        )
    requested = Path(output_directory).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    for component in (requested, *requested.parents):
        if component.is_symlink():
            raise ValueError("output directory path must not contain symlinks")
    destination = requested.resolve()
    project_root = PROJECT_DIR.resolve()
    try:
        destination.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise ValueError(
            "output_directory must be outside the project worktree to protect "
            "datasets, models, and preserved evidence"
        )
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise ValueError(
            "output_directory must be a new, empty per-run directory"
        ) from exc
    except FileNotFoundError as exc:
        raise ValueError(
            "output_directory parent must already exist"
        ) from exc
    _RUN_OUTPUT_DIRECTORIES.add(os.fspath(destination))
    return destination


def _reuse_output_directory(output_directory):
    destination = Path(output_directory).expanduser().absolute()
    if (
        os.fspath(destination) not in _RUN_OUTPUT_DIRECTORIES
        or not destination.is_dir()
        or destination.is_symlink()
    ):
        raise ValueError(
            "output_directory must be created by this run before use"
        )
    for component in (destination, *destination.parents):
        if component.is_symlink():
            raise ValueError("output directory path must not contain symlinks")
    return destination


def _isolated_output_file(directory, filename):
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or PureWindowsPath(filename).name != filename
    ):
        raise ValueError("output filename must not contain path components")
    output = directory / filename
    if output.is_symlink() or output.resolve().parent != directory.resolve():
        raise ValueError("output file must remain inside the isolated directory")
    return output


class _IperfIntervalReader:
    """Own one iperf stdout pipe while retaining interval and final output."""

    def __init__(self, process, callback):
        self.process = process
        self.callback = callback
        self.lines = []
        self.error = None
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _read(self):
        try:
            for line in self.process.stdout:
                self.lines.append(line)
                try:
                    event = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    if str(line).strip():
                        if self.error is None:
                            self.error = ValueError(
                                "iperf JSON stream contained malformed JSON"
                            )
                    continue
                if not isinstance(event, dict):
                    if self.error is None:
                        self.error = ValueError(
                            "iperf JSON stream event must be an object"
                        )
                    continue
                if event.get("event") == "error":
                    if self.error is None:
                        data = event.get("data")
                        self.error = RuntimeError(
                            f"iperf reported an error: {data}"
                        )
                    continue
                if event.get("event") == "interval":
                    try:
                        self.callback(line)
                    except Exception as exc:
                        # Continue draining stdout so a writer/parser failure
                        # cannot deadlock the iperf process on a full pipe.
                        if self.error is None:
                            self.error = exc
        except Exception as exc:
            self.error = exc

    def join(self):
        self._thread.join()
        return "".join(self.lines)


def start_interval_reader(process, callback):
    """Start the sole stdout reader for an iperf JSON-stream process."""
    if getattr(process, "stdout", None) is None:
        return None
    reader = _IperfIntervalReader(process, callback).start()
    process._interval_reader = reader
    return reader


def wait_for_process(process, timeout=None):
    """Wait for a traffic process and return its iperf3 output."""

    reader = getattr(process, "_interval_reader", None)
    if reader is not None:
        try:
            if timeout is None:
                process.wait()
            else:
                process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            reader.join()
            raise TimeoutError("iperf traffic process timed out") from exc
        output = reader.join()
        process_error = None
        if process.returncode != 0:
            process_error = subprocess.CalledProcessError(
                process.returncode, process.args, output
            )
        if reader.error is not None and process_error is not None:
            raise RuntimeError(
                "iperf exited with an error and interval collection failed: "
                f"{process_error}; {reader.error}"
            ) from reader.error
        if reader.error is not None:
            raise RuntimeError(
                "iperf interval collection failed"
            ) from reader.error
        if process_error is not None:
            print(output)
            raise process_error
        print(output)
        return output

    try:
        if timeout is None:
            output, _ = process.communicate()
        else:
            output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _stop_process(process)
        process.communicate()
        raise TimeoutError("iperf traffic process timed out") from exc

    if process.returncode != 0:
        print(output)
        raise subprocess.CalledProcessError(
            process.returncode,
            process.args,
            output
        )

    print(output)

    return output


def _stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def measure_latency(source, destination_ip="10.0.0.5", count=5):
    """Measure average RTT from a Mininet host to the server."""

    result = run_on_host(
        source,
        [
            "ping",
            "-c",
            str(count),
            destination_ip,
        ],
        interface=MININET_HOST_INTERFACES[source],
    )

    output, _ = result.communicate()

    if result.returncode != 0:
        print(output)
        return None

    match = re.search(
        r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/",
        output,
    )

    if not match:
        return None

    return float(match.group(1))


def _drop_delta(initial_drops, current_drops):
    """Return interval drops; missing baselines remain unknown.

    A counter reset is treated as a new counter epoch, so the current
    non-negative cumulative value is used rather than reporting a negative
    interval.
    """
    if initial_drops is None or current_drops is None:
        return None
    if current_drops < initial_drops:
        return current_drops
    return current_drops - initial_drops


def _queue_stats_from_output(output, initial_drops=None):
    backlog_packets = re.findall(r"\bbacklog\s+\d+\w*\s+(\d+)p\b", output)
    drop_counts = re.findall(r"\bdropped\s+(\d+)\b", output)
    current_drops = sum(map(int, drop_counts)) if drop_counts else None
    return {
        "queue_size": max(map(int, backlog_packets)) if backlog_packets else None,
        "queue_drops": _drop_delta(initial_drops, current_drops),
        "cumulative_drops": current_drops,
    }


def measure_queue_stats(interface=BOTTLENECK_INTERFACE, initial_drops=None):
    """Read queue backlog and drops from the shared bottleneck qdiscs."""

    result = subprocess.run(
        ["tc", "-s", "qdisc", "show", "dev", interface],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"[TELEMETRY] Could not read qdisc stats: {result.stderr.strip()}")
        return {"queue_size": None, "queue_drops": None}

    stats = _queue_stats_from_output(result.stdout, initial_drops)
    stats.pop("cumulative_drops")
    return stats


def _initial_queue_drops(interface=BOTTLENECK_INTERFACE):
    """Capture the cumulative qdisc drop baseline without resetting qdiscs."""
    try:
        result = subprocess.run(
            ["tc", "-s", "qdisc", "show", "dev", interface],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"[TELEMETRY] Could not read initial qdisc stats: {exc}")
        return None
    if result.returncode != 0:
        print(f"[TELEMETRY] Could not read initial qdisc stats: {result.stderr.strip()}")
        return None
    return _queue_stats_from_output(result.stdout)["cumulative_drops"]


def summarize_measurements(latencies, queues, drops):
    """Summarize shared in-scenario samples for the telemetry schema."""
    valid_latencies = [value for value in latencies if value is not None]
    valid_queues = [value for value in queues if value is not None]
    valid_drops = [value for value in drops if value is not None]
    return {
        "latency_ms": fmean(valid_latencies) if valid_latencies else None,
        "queue_size": max(valid_queues) if valid_queues else None,
        "queue_drops": max(valid_drops) if valid_drops else None,
    }


class ScenarioMonitor:
    """Sample shared queue state and host RTTs while traffic is active.

    Queue samples describe the bottleneck as a whole. If copied into multiple
    flow records, they are shared scenario measurements, not per-flow values.
    """

    def __init__(
        self,
        hosts,
        interface=BOTTLENECK_INTERFACE,
        interval=1.0,
        latency_probe_count=1,
    ):
        self.hosts = tuple(hosts)
        self.interface = interface
        self.interval = interval
        self.latency_probe_count = latency_probe_count
        self._stop_event = threading.Event()
        self._thread = None
        self._latencies = []
        self._queue_sizes = []
        self._queue_drops = []
        self._initial_drops = None
        self._latest_latency = None
        self._latest_queue = {
            "queue_size": None,
            "queue_drops": None,
        }
        self._latency_timestamp = None
        self._queue_timestamp = None
        self._latency_monotonic = None
        self._queue_monotonic = None

    def start(self):
        self._initial_drops = _initial_queue_drops(self.interface)
        self._sample()
        self._thread = threading.Thread(
            target=self._run,
            name="scenario-telemetry-monitor",
            daemon=True,
        )
        self._thread.start()
        return self

    def _sample(self):
        current_latencies = []
        for host in self.hosts:
            try:
                latency = measure_latency(
                    host, count=self.latency_probe_count
                )
                self._latencies.append(latency)
                if latency is not None:
                    current_latencies.append(latency)
            except Exception as exc:
                print(f"[TELEMETRY] RTT measurement failed for {host}: {exc}")
                self._latencies.append(None)
        try:
            queue_stats = measure_queue_stats(
                self.interface, initial_drops=self._initial_drops
            )
            self._queue_sizes.append(queue_stats["queue_size"])
            self._queue_drops.append(queue_stats["queue_drops"])
            self._latest_queue = {
                "queue_size": queue_stats["queue_size"],
                "queue_drops": queue_stats["queue_drops"],
            }
            self._queue_timestamp = datetime.now(timezone.utc).isoformat()
            self._queue_monotonic = time.monotonic()
        except Exception as exc:
            print(f"[TELEMETRY] Queue measurement failed: {exc}")
            self._queue_sizes.append(None)
            self._queue_drops.append(None)
            self._latest_queue = {"queue_size": None, "queue_drops": None}
        if current_latencies:
            self._latest_latency = fmean(current_latencies)
            self._latency_timestamp = datetime.now(timezone.utc).isoformat()
            self._latency_monotonic = time.monotonic()

    def latest_measurement(self, max_age=MONITOR_FRESHNESS_SECONDS):
        """Return fresh shared values and diagnostic age metadata."""
        now = time.monotonic()
        latency_age = (
            now - self._latency_monotonic
            if self._latency_monotonic is not None
            else None
        )
        queue_age = (
            now - self._queue_monotonic
            if self._queue_monotonic is not None
            else None
        )
        return {
            "latency_ms": (
                self._latest_latency
                if latency_age is not None and latency_age <= max_age
                else None
            ),
            "queue_size": (
                self._latest_queue["queue_size"]
                if queue_age is not None and queue_age <= max_age
                else None
            ),
            "queue_drops": (
                self._latest_queue["queue_drops"]
                if queue_age is not None and queue_age <= max_age
                else None
            ),
            "latency_measurement_timestamp": self._latency_timestamp,
            "queue_measurement_timestamp": self._queue_timestamp,
            "latency_measurement_age_seconds": latency_age,
            "queue_measurement_age_seconds": queue_age,
        }

    def _run(self):
        while not self._stop_event.wait(self.interval):
            self._sample()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        return summarize_measurements(
            self._latencies, self._queue_sizes, self._queue_drops
        )


def save_telemetry(
    output,
    source,
    scenario,
    experiment_id,
    requested_mbps,
    active_flows,
    total_throughput_mbps=None,
    latency_ms=None,
    queue_size=None,
    queue_drops=None,
    output_directory=None,
):
    """Parse iperf3 output and save a telemetry record."""
    output_directory = _reuse_output_directory(output_directory)

    parsed_record = parse_iperf_output(
        output,
        source=source,
        scenario=scenario,
        traffic_type="udp",
        requested_mbps=requested_mbps,
        active_flows=active_flows,
        bandwidth_mbps=10.0,
    )

    if total_throughput_mbps is not None:
        parsed_record["utilization_percent"] = round(
            (total_throughput_mbps / 10.0) * 100,
            2,
        )

    record = {
        "timestamp": parsed_record["timestamp"],
        "experiment_id": experiment_id,
        "scenario": parsed_record["scenario"],
        "source": parsed_record["source"],
        "destination": parsed_record["destination"],
        "traffic_type": parsed_record["traffic_type"],
        "requested_mbps": parsed_record["requested_mbps"],
        "throughput_mbps": parsed_record["throughput_mbps"],
        "latency_ms": latency_ms,
        "jitter_ms": parsed_record["jitter_ms"],
        "packet_loss_percent": parsed_record["packet_loss_percent"],
        "lost_packets": parsed_record["lost_packets"],
        "total_packets": parsed_record["total_packets"],
        "bandwidth_mbps": parsed_record["bandwidth_mbps"],
        "utilization_percent": parsed_record["utilization_percent"],
        "active_flows": parsed_record["active_flows"],
        "queue_size": queue_size,
        "queue_drops": queue_drops,
    }

    filename = _isolated_output_file(output_directory, f"{scenario}.jsonl")

    write_jsonl(record, filename, secure_run=True)

    print(f"[TELEMETRY] Saved: {filename}")
    print(f"[EXPERIMENT] experiment_id={experiment_id}")
    print(f"[TELEMETRY] {record}")


class IntervalTelemetryCollector:
    """Pair client throughput with receiver-side UDP loss and jitter."""

    def __init__(
        self,
        source,
        scenario,
        port,
        experiment_id,
        requested_mbps,
        active_flows,
        monitor,
        output_directory=None,
    ):
        self.source = source
        self.scenario = scenario
        self.port = port
        self.experiment_id = experiment_id
        self.requested_mbps = requested_mbps
        self.active_flows = active_flows
        self.monitor = monitor
        self.output_directory = _reuse_output_directory(output_directory)
        safe_experiment_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(experiment_id))
        self.filename = _isolated_output_file(
            self.output_directory,
            f"{scenario}_{safe_experiment_id}_{source}_intervals.jsonl",
        )
        self._lock = threading.Lock()
        self._client_intervals = {}
        self._receiver_intervals = {}
        self._receiver_measurements = {}
        self._receiver_boundary_identity = None
        self._completed = set()
        self._used_client_intervals = set()
        self._used_receiver_intervals = set()

    @staticmethod
    def _key(interval):
        return round(interval["start"], 6), round(interval["end"], 6)

    def add_client_event(self, line):
        for interval in parse_iperf_sender_interval_events(line):
            interval["measurement"] = self.monitor.latest_measurement()
            with self._lock:
                key = self._key(interval)
                if key in self._client_intervals:
                    raise ValueError(
                        f"duplicate client interval for source={self.source} "
                        f"scenario={self.scenario} port={self.port} "
                        f"interval={key[0]:g}-{key[1]:g}s"
                    )
                self._client_intervals[key] = interval
                self._match_pending_intervals()

    def add_receiver_event(self, line):
        observed_monotonic = time.monotonic()
        try:
            records = parse_iperf_interval_events(
                line,
                source=self.source,
                experiment_id=self.experiment_id,
                scenario=self.scenario,
                requested_mbps=self.requested_mbps,
                active_flows=self.active_flows,
                bandwidth_mbps=10.0,
            )
        except (TypeError, ValueError) as exc:
            interval = self._event_interval_label(line)
            raise ValueError(
                "Invalid receiver-side UDP metrics for "
                f"source={self.source} scenario={self.scenario} "
                f"port={self.port} experiment_id={self.experiment_id} "
                f"{interval}: {exc}"
            ) from exc
        measurement = self.monitor.latest_measurement()
        with self._lock:
            for record in records:
                if "receiver_interval_schema_version" in record:
                    boundary_identity = tuple(
                        record[field]
                        for field in (
                            "clock_id",
                            "host_id",
                            "boot_id",
                            "producer_name",
                            "producer_version",
                            "producer_build_identity",
                            "producer_binary_sha256",
                            "producer_process_instance_id",
                        )
                    )
                    if self._receiver_boundary_identity is None:
                        self._receiver_boundary_identity = boundary_identity
                    elif boundary_identity != self._receiver_boundary_identity:
                        raise ValueError(
                            "receiver boundary clock, host, boot, producer, "
                            "build, or process identity changed during the run"
                        )
                # Callback observation is diagnostic, not an interval boundary.
                record["receiver_observation_monotonic_seconds"] = observed_monotonic
                record["receiver_observation_role"] = "diagnostic_only"
                key = self._key({
                    "start": record["interval_start_seconds"],
                    "end": record["interval_end_seconds"],
                })
                if key in self._receiver_intervals:
                    raise ValueError(
                        f"duplicate receiver interval for source={self.source} "
                        f"scenario={self.scenario} port={self.port} "
                        f"interval={key[0]:g}-{key[1]:g}s"
                    )
                self._receiver_intervals[key] = record
                self._receiver_measurements[key] = measurement
                self._match_pending_intervals()

    @staticmethod
    def _event_interval_label(line):
        try:
            payload = json.loads(line)
            data = payload["data"]
            intervals = data.get("intervals")
            if not isinstance(intervals, list):
                intervals = [data]
            interval = intervals[0]
            summary = interval.get("sum")
            if not isinstance(summary, dict):
                streams = interval.get("streams")
                summary = streams[0] if isinstance(streams, list) and streams else {}
            start = float(summary["start"])
            end = float(summary["end"])
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return "interval=unknown"
        return f"interval={start:g}-{end:g}s"

    @staticmethod
    def _intervals_match(client, receiver):
        client_duration = client["end"] - client["start"]
        receiver_duration = (
            receiver["interval_end_seconds"]
            - receiver["interval_start_seconds"]
        )
        tolerance = min(
            MAX_INTERVAL_MATCH_TOLERANCE_SECONDS,
            max(0.05, min(client_duration, receiver_duration) * 0.25),
        )
        return (
            abs(client["start"] - receiver["interval_start_seconds"])
            <= tolerance
            and abs(client["end"] - receiver["interval_end_seconds"])
            <= tolerance
        )

    def _match_pending_intervals(self):
        for client_key, client in self._client_intervals.items():
            if client_key in self._used_client_intervals:
                continue
            candidates = [
                (receiver_key, receiver)
                for receiver_key, receiver in self._receiver_intervals.items()
                if receiver_key not in self._used_receiver_intervals
                and self._intervals_match(client, receiver)
            ]
            if len(candidates) > 1:
                raise ValueError(
                    f"ambiguous receiver interval match for "
                    f"source={self.source} scenario={self.scenario} "
                    f"port={self.port} interval="
                    f"{client['start']:g}-{client['end']:g}s"
                )
            if candidates:
                receiver_key, receiver = candidates[0]
                self._persist_pair(client_key, client, receiver_key, receiver)

        for receiver_key, receiver in self._receiver_intervals.items():
            if receiver_key in self._used_receiver_intervals:
                continue
            candidates = [
                client
                for client_key, client in self._client_intervals.items()
                if client_key not in self._used_client_intervals
                and self._intervals_match(client, receiver)
            ]
            if len(candidates) > 1:
                raise ValueError(
                    f"ambiguous client interval match for "
                    f"source={self.source} scenario={self.scenario} "
                    f"port={self.port} receiver interval="
                    f"{receiver['interval_start_seconds']:g}-"
                    f"{receiver['interval_end_seconds']:g}s"
                )
            if candidates:
                client = candidates[0]
                client_key = self._key(client)
                self._persist_pair(
                    client_key, client, receiver_key, receiver
                )

    def _persist_pair(self, client_key, client, receiver_key, receiver):
        # Receiver JSON-stream data is authoritative for receiver metrics.
        record = dict(receiver)
        record["receiver_observation_timestamp"] = receiver["observation_timestamp"]
        record["receiver_interval_start_seconds"] = receiver["interval_start_seconds"]
        record["receiver_interval_end_seconds"] = receiver["interval_end_seconds"]
        record["receiver_interval_duration_seconds"] = receiver["interval_duration_seconds"]
        record["client_observation_timestamp"] = client["timestamp"]
        record["sender_interval_start_seconds"] = client["start"]
        record["sender_interval_end_seconds"] = client["end"]
        record["sender_interval_duration_seconds"] = client["end"] - client["start"]
        record["sender_throughput_mbps"] = client["throughput_mbps"]
        record["sender_measurement_source"] = "iperf3_client_json_stream"
        record["throughput_measurement_source"] = "iperf3_server_json_stream"
        record["interval_id"] = (
            f"{self.experiment_id}:{self.source}:receiver:"
            f"{receiver['interval_start_seconds']:g}-"
            f"{receiver['interval_end_seconds']:g}"
        )
        utilization = record["throughput_mbps"] / record["bandwidth_mbps"] * 100
        if not math.isfinite(utilization) or not 0 <= utilization <= 100:
            raise ValueError(
                f"invalid receiver utilization for source={self.source} "
                f"scenario={self.scenario} port={self.port} "
                f"interval={receiver['interval_start_seconds']:g}-"
                f"{receiver['interval_end_seconds']:g}s"
            )
        record["utilization_percent"] = round(utilization, 2)
        record.update(client["measurement"])
        write_interval_jsonl(record, self.filename, secure_run=True)
        self._completed.add((client_key, receiver_key))
        self._used_client_intervals.add(client_key)
        self._used_receiver_intervals.add(receiver_key)

    def validate_complete(self):
        with self._lock:
            missing_receiver = (
                self._client_intervals.keys() - self._used_client_intervals
            )
            if missing_receiver:
                start, end = sorted(missing_receiver)[0]
                raise RuntimeError(
                    "Missing receiver-side UDP metrics for "
                    f"source={self.source} scenario={self.scenario} "
                    f"port={self.port} experiment_id={self.experiment_id} "
                    f"interval={start:g}-{end:g}s"
                )
            missing_client = (
                self._receiver_intervals.keys()
                - self._used_receiver_intervals
            )
            if missing_client and self._client_intervals:
                final_client_end = max(
                    interval["end"]
                    for interval in self._client_intervals.values()
                )
                trailing_intervals = [
                    key for key in sorted(missing_client)
                ]
                is_contiguous_drain = (
                    trailing_intervals
                    and abs(trailing_intervals[0][0] - final_client_end) <= 0.05
                    and all(
                        abs(current[0] - previous[1]) <= 0.05
                        for previous, current in zip(
                            trailing_intervals, trailing_intervals[1:]
                        )
                    )
                )
                if is_contiguous_drain:
                    for key in sorted(trailing_intervals):
                        record = dict(self._receiver_intervals[key])
                        record["throughput_measurement_source"] = "receiver"
                        record.update(self._receiver_measurements[key])
                        write_interval_jsonl(
                            record, self.filename, secure_run=True
                        )
                        self._used_receiver_intervals.add(key)
                        print(
                            "[TELEMETRY] Persisted receiver-only post-client "
                            f"drain interval for source={self.source} "
                            f"scenario={self.scenario} port={self.port} "
                            f"experiment_id={self.experiment_id} "
                            f"interval={key[0]:g}-{key[1]:g}s"
                        )
                    missing_client = set()
            if missing_client:
                start, end = sorted(missing_client)[0]
                raise RuntimeError(
                    "Missing client interval for receiver metrics for "
                    f"source={self.source} scenario={self.scenario} "
                    f"port={self.port} experiment_id={self.experiment_id} "
                    f"interval={start:g}-{end:g}s"
                )
            if not self._completed:
                raise RuntimeError(
                    "No matched receiver-side UDP intervals for "
                    f"source={self.source} scenario={self.scenario} "
                    f"port={self.port} experiment_id={self.experiment_id}"
                )


def _iperf_port_is_listening(port):
    """Check a server-host TCP port without starting another listener."""
    probe = run_on_host(
        "server",
        ["ss", "-H", "-ltn", "sport", "=", f":{port}"],
        interface=MININET_HOST_INTERFACES["server"],
    )
    output, _ = probe.communicate()
    if probe.returncode != 0:
        raise RuntimeError(
            f"Could not check iperf listener port {port}: {output.strip()}"
        )
    return f":{port}" in output


def start_receiver_listener(port, callback):
    """Start and verify one receiver-side iperf JSON-stream listener."""
    if port not in IPERF_SERVER_PORTS:
        raise ValueError(f"unsupported iperf receiver port: {port}")
    if _iperf_port_is_listening(port):
        raise RuntimeError(
            f"iperf receiver port {port} is already listening in Mininet "
            "host server; stop the stale/external listener and retry"
        )
    process = None
    try:
        process = run_on_host(
            "server",
            ["iperf3", "-s", "-1", "-p", str(port), "--json-stream"],
            interface=MININET_HOST_INTERFACES["server"],
        )
        start_interval_reader(process, callback)
        deadline = time.monotonic() + IPERF_LISTENER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"iperf receiver listener exited during startup on port "
                    f"{port} (exit code {process.returncode})"
                )
            if _iperf_port_is_listening(port):
                return process
            time.sleep(0.05)
        raise TimeoutError(
            f"iperf receiver listener did not become ready on port {port}"
        )
    except BaseException:
        if process is not None:
            _stop_process(process)
            reader = getattr(process, "_interval_reader", None)
            if reader is not None:
                reader.join()
        raise


def stop_receiver_listener(process, port):
    """Stop one managed listener and surface receiver collection failures."""
    return_code = process.poll()
    _stop_process(process)
    reader = getattr(process, "_interval_reader", None)
    output = reader.join() if reader is not None else ""
    if reader is not None and reader.error is not None:
        raise RuntimeError(
            f"receiver interval collection failed on port {port}: "
            f"{reader.error}"
        ) from reader.error
    if return_code is not None and return_code != 0:
        raise subprocess.CalledProcessError(
            return_code, process.args, output
        )


def _run_udp_flow(
    source,
    rate_mbps,
    port,
    duration,
    collector,
):
    """Run one client against its owned receiver listener, cleaning both up."""
    listener = None
    client = None
    try:
        listener = start_receiver_listener(port, collector.add_receiver_event)
        client = run_on_host(
            source,
            [
                "iperf3", "-c", SERVER_IP, "-u",
                "-b", f"{rate_mbps:g}M",
                "-p", str(port),
                "-t", str(duration),
                "--json-stream",
            ],
            interface=MININET_HOST_INTERFACES[source],
        )
        start_interval_reader(client, collector.add_client_event)
        output = wait_for_process(client, timeout=duration + 30)
    finally:
        try:
            if client is not None:
                _stop_process(client)
        finally:
            if listener is not None:
                stop_receiver_listener(listener, port)
    collector.validate_complete()
    return output


def _single_source_flow(source_host):
    if not isinstance(source_host, str) or source_host not in CLIENT_FLOW_PORTS:
        raise ValueError(f"unsupported traffic source host: {source_host!r}")
    return source_host, CLIENT_FLOW_PORTS[source_host]


def _resolve_experiment_id(experiment_id):
    if experiment_id is None:
        return str(uuid.uuid4())
    if not isinstance(experiment_id, str):
        raise ValueError("experiment ID must be a canonical UUID.")
    try:
        parsed = uuid.UUID(experiment_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("experiment ID must be a canonical UUID.") from exc
    if experiment_id not in {parsed.hex, str(parsed)}:
        raise ValueError("experiment ID must be a canonical UUID.")
    return experiment_id


def normal(
    output_directory=None,
    rate_mbps=3.0,
    duration_seconds=DURATION,
    source_host="h1",
    experiment_id=None,
):
    """One selected-client UDP flow for the requested duration."""

    source_host, port = _single_source_flow(source_host)
    experiment_id = _resolve_experiment_id(experiment_id)
    output_directory = _prepare_output_directory(output_directory)
    print("\n=== NORMAL SCENARIO ===")

    monitor = ScenarioMonitor([source_host]).start()
    collector = IntervalTelemetryCollector(
        source_host, "normal", port, experiment_id, rate_mbps, 1, monitor,
        output_directory=output_directory,
    )
    try:
        output = _run_udp_flow(source_host, rate_mbps, port, duration_seconds, collector)
    finally:
        summary = monitor.stop()

    save_telemetry(
        output,
        source=source_host,
        scenario="normal",
        experiment_id=experiment_id,
        requested_mbps=rate_mbps,
        active_flows=1,
        output_directory=output_directory,
        **summary,
    )

    print("\n[TRAFFIC] Normal scenario completed.")


def congestion(
    output_directory=None,
    rate_mbps=4.0,
    duration_seconds=DURATION,
    source_host="all",
    experiment_id=None,
):
    """One selected client or all four clients send UDP through the bottleneck.

    A startup or collection failure aborts the scenario without writing
    incomplete flow records; successfully collected output is not persisted
    because the scenario result is intentionally all-or-nothing.
    """

    experiment_id = _resolve_experiment_id(experiment_id)
    output_directory = _prepare_output_directory(output_directory)
    print("\n=== CONGESTION SCENARIO ===")

    if source_host == "all":
        selected_hosts = CLIENT_HOSTS
    else:
        selected_hosts = (_single_source_flow(source_host)[0],)
    flows = [
        (host, rate_mbps, CLIENT_FLOW_PORTS[host])
        for host in selected_hosts
    ]

    monitor = ScenarioMonitor(list(selected_hosts)).start()
    collectors = [
        IntervalTelemetryCollector(
            host, "congestion", port, experiment_id, rate, len(flows), monitor,
            output_directory=output_directory,
        )
        for host, rate, port in flows
    ]
    listeners = []
    processes = []
    flow_outputs = []
    try:
        for collector, (host, rate, port) in zip(collectors, flows):
            listener = start_receiver_listener(
                port, collector.add_receiver_event
            )
            listeners.append((listener, port))

        for collector, (host, rate, port) in zip(collectors, flows):
            process = run_on_host(
                host,
                [
                    "iperf3",
                    "-c", SERVER_IP,
                    "-u",
                    "-b", f"{rate}M",
                    "-p", str(port),
                    "-t", str(duration_seconds),
                    "--json-stream",
                ],
                interface=MININET_HOST_INTERFACES[host],
            )
            processes.append(process)
            start_interval_reader(process, collector.add_client_event)

        print("\n[TRAFFIC] Congestion flow(s) started.")
        for process, flow in zip(processes, flows):
            output = wait_for_process(process, timeout=duration_seconds + 30)
            flow_outputs.append((output, flow))
    finally:
        cleanup_errors = []
        for process in processes:
            try:
                _stop_process(process)
            except Exception as exc:
                cleanup_errors.append(exc)
        for listener, port in listeners:
            try:
                stop_receiver_listener(listener, port)
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            summary = monitor.stop()
        except Exception as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise RuntimeError(
                "Failed to clean up congestion traffic processes or monitor: "
                + "; ".join(str(error) for error in cleanup_errors)
            ) from cleanup_errors[0]

    for collector in collectors:
        collector.validate_complete()

    records = [
        parse_iperf_output(
            output,
            source=host,
            scenario="congestion",
            traffic_type="udp",
            requested_mbps=rate,
            active_flows=len(flows),
            bandwidth_mbps=10.0,
        )
        for output, (host, rate, port) in flow_outputs
    ]
    total_throughput_mbps = sum(
        record["throughput_mbps"] for record in records
    )
    for output, (host, rate, port) in flow_outputs:
        save_telemetry(
            output,
            source=host,
            scenario="congestion",
            experiment_id=experiment_id,
            requested_mbps=rate,
            active_flows=len(flows),
            total_throughput_mbps=total_throughput_mbps,
            output_directory=output_directory,
            **summary,
        )

    print("\n[TRAFFIC] Congestion scenario completed.")

def burst(output_directory=None, source_host="h1", experiment_id=None):
    """Five fixed-rate short UDP bursts from one selected client."""

    source_host, port = _single_source_flow(source_host)
    experiment_id = _resolve_experiment_id(experiment_id)
    output_directory = _prepare_output_directory(output_directory)
    print("\n=== BURST SCENARIO ===")

    for i in range(5):

        print(f"\n[TRAFFIC] Starting burst {i + 1}/5")

        monitor = ScenarioMonitor([source_host]).start()
        collector = IntervalTelemetryCollector(
            source_host, "burst", port, experiment_id, 8, 1, monitor,
            output_directory=output_directory,
        )
        try:
            output = _run_udp_flow(source_host, 8, port, 3, collector)
        finally:
            summary = monitor.stop()

        save_telemetry(
            output,
            source=source_host,
            scenario="burst",
            experiment_id=experiment_id,
            requested_mbps=8,
            active_flows=1,
            output_directory=output_directory,
            **summary,
        )

        print(f"[TRAFFIC] Burst {i + 1}/5 completed.")

        if i < 4:
            time.sleep(2)

    print("\n[TRAFFIC] Burst scenario completed.")

def moderate(
    rate_mbps: float = 6.0,
    *,
    output_directory=None,
    duration_seconds=DURATION,
    source_host="h1",
    experiment_id=None,
):
    """One configurable-rate UDP flow for the requested duration."""
    source_host, port = _single_source_flow(source_host)
    if (
        isinstance(rate_mbps, bool)
        or not isinstance(rate_mbps, (int, float))
        or not math.isfinite(rate_mbps)
        or not 0.1 <= rate_mbps <= 10
        or abs(rate_mbps * 10 - round(rate_mbps * 10)) > 1e-8
    ):
        raise ValueError("moderate rate must be between 0.1 and 10 Mbps in 0.1 Mbps increments")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 1 <= duration_seconds <= 60:
        raise ValueError("moderate duration must be a whole number between 1 and 60 seconds")

    experiment_id = _resolve_experiment_id(experiment_id)
    output_directory = _prepare_output_directory(output_directory)
    print("\n=== MODERATE SCENARIO ===")
    print(f"[EXPERIMENT] experiment_id={experiment_id}")

    monitor = ScenarioMonitor([source_host]).start()
    collector = IntervalTelemetryCollector(
        source_host, "moderate", port, experiment_id, rate_mbps, 1, monitor,
        output_directory=output_directory,
    )

    try:
        output = _run_udp_flow(
            source_host, rate_mbps, port, duration_seconds, collector
        )
    finally:
        summary = monitor.stop()

    save_telemetry(
        output,
        source=source_host,
        scenario="moderate",
        experiment_id=experiment_id,
        requested_mbps=rate_mbps,
        active_flows=1,
        output_directory=output_directory,
        **summary,
    )

    print("\n[TRAFFIC] Moderate scenario completed.")


def main():

    parser = argparse.ArgumentParser(
        description="Traffic generator for Adaptive QoS project"
    )

    parser.add_argument(
        "--scenario",
        choices=["normal", "moderate","congestion", "burst"],
        required=True
    )
    parser.add_argument(
        "--source-host",
        choices=[*CLIENT_HOSTS, "all"],
        default=None,
        help="allowlisted Mininet client; all is supported only for Congestion",
    )
    parser.add_argument(
        "--experiment-id",
        type=_resolve_experiment_id,
        default=None,
        help="canonical UUID identifying this execution",
    )
    parser.add_argument(
        "--rate-mbps",
        type=float,
        default=None,
        help="offered UDP rate per flow (bounded by the guarded runner)",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help="traffic duration (Burst is fixed to its 23-second schedule)",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="isolated evidence directory outside the project worktree",
    )

    args = parser.parse_args()
    source_host = args.source_host or ("all" if args.scenario == "congestion" else "h1")
    if source_host == "all" and args.scenario != "congestion":
        parser.error("--source-host all is supported only for congestion")
    if args.scenario in {"normal", "moderate", "congestion"}:
        default_rate = {"normal": 3.0, "moderate": 6.0, "congestion": 4.0}[args.scenario]
        rate = default_rate if args.rate_mbps is None else args.rate_mbps
        duration = DURATION if args.duration_seconds is None else args.duration_seconds
        if not math.isfinite(rate) or not 0.1 <= rate <= 10 or abs(rate * 10 - round(rate * 10)) > 1e-8:
            parser.error("--rate-mbps must be between 0.1 and 10 Mbps per flow in 0.1 Mbps increments")
        if not 1 <= duration <= 60:
            parser.error("--duration-seconds must be between 1 and 60")
    if args.scenario == "normal":
        normal(
            output_directory=args.output_dir,
            rate_mbps=3.0 if args.rate_mbps is None else args.rate_mbps,
            duration_seconds=DURATION if args.duration_seconds is None else args.duration_seconds,
            source_host=source_host,
            experiment_id=args.experiment_id,
        )

    elif args.scenario == "congestion":
        congestion(
            output_directory=args.output_dir,
            rate_mbps=4.0 if args.rate_mbps is None else args.rate_mbps,
            duration_seconds=DURATION if args.duration_seconds is None else args.duration_seconds,
            source_host=source_host,
            experiment_id=args.experiment_id,
        )

    elif args.scenario == "burst":
        if args.rate_mbps not in (None, 8.0) or args.duration_seconds not in (None, 23):
            parser.error("Burst uses a fixed 8 Mbps flow schedule lasting 23 seconds")
        burst(
            output_directory=args.output_dir,
            source_host=source_host,
            experiment_id=args.experiment_id,
        )
    elif args.scenario == "moderate":
        moderate(
            6.0 if args.rate_mbps is None else args.rate_mbps,
            output_directory=args.output_dir,
            duration_seconds=DURATION if args.duration_seconds is None else args.duration_seconds,
            source_host=source_host,
            experiment_id=args.experiment_id,
        )


if __name__ == "__main__":
    main()
