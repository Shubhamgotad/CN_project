#!/usr/bin/env python3

import argparse
import json
import math
import re
import subprocess
import sys
import threading
import time
import uuid
from statistics import fmean
from datetime import datetime, timezone
from pathlib import Path

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
MONITOR_FRESHNESS_SECONDS = 2.0
IPERF_SERVER_PORTS = range(5201, 5205)
IPERF_LISTENER_START_TIMEOUT_SECONDS = 5.0
MAX_INTERVAL_MATCH_TOLERANCE_SECONDS = 0.25


def run_on_host(host, command):
    """Run a command inside a Mininet host namespace."""
    result = subprocess.run(
        ["pgrep", "-f", f"mininet:{host}"],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode == 1:
        raise RuntimeError(
            f"Could not find Mininet namespace for {host}. "
            "Make sure Mininet is running."
        )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )

    namespace_pid = result.stdout.splitlines()[0]

    print(f"[TRAFFIC] {host}: {' '.join(command)}")

    return subprocess.Popen(
    ["mnexec", "-a", namespace_pid, *command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)


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
):
    """Parse iperf3 output and save a telemetry record."""

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

    filename = DATASET_DIR / f"{scenario}.jsonl"

    write_jsonl(record, filename)

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
        self.output_directory = Path(output_directory) if output_directory else INTERVAL_DATASET_DIR
        safe_experiment_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(experiment_id))
        self.filename = (
            self.output_directory
            / f"{scenario}_{safe_experiment_id}_{source}_intervals.jsonl"
        )
        self._lock = threading.Lock()
        self._client_intervals = {}
        self._receiver_intervals = {}
        self._receiver_measurements = {}
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
                # Callback observation is recorded separately; it is not treated
                # as the interval's absolute start/end clock boundary.
                record["receiver_observation_monotonic_seconds"] = observed_monotonic
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
        write_interval_jsonl(record, self.filename)
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
                        write_interval_jsonl(record, self.filename)
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


def normal():
    """One 3 Mbps UDP flow for 20 seconds."""

    print("\n=== NORMAL SCENARIO ===")
    experiment_id = str(uuid.uuid4())

    monitor = ScenarioMonitor(["h1"]).start()
    collector = IntervalTelemetryCollector(
        "h1", "normal", 5201, experiment_id, 3, 1, monitor
    )
    try:
        output = _run_udp_flow("h1", 3, 5201, DURATION, collector)
    finally:
        summary = monitor.stop()

    save_telemetry(
        output,
        source="h1",
        scenario="normal",
        experiment_id=experiment_id,
        requested_mbps=3,
        active_flows=1,
        **summary,
    )

    print("\n[TRAFFIC] Normal scenario completed.")


def congestion():
    """Four 4 Mbps UDP flows for 20 seconds through the bottleneck.

    A startup or collection failure aborts the scenario without writing
    incomplete flow records; successfully collected output is not persisted
    because the scenario result is intentionally all-or-nothing.
    """

    print("\n=== CONGESTION SCENARIO ===")
    experiment_id = str(uuid.uuid4())

    flows = [
        ("h1", 4, 5201),
        ("h2", 4, 5202),
        ("h3", 4, 5203),
        ("h4", 4, 5204),
    ]

    monitor = ScenarioMonitor(["h1", "h2", "h3", "h4"]).start()
    collectors = [
        IntervalTelemetryCollector(
            host, "congestion", port, experiment_id, rate, 4, monitor
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
                    "-t", str(DURATION),
                    "--json-stream",
                ],
            )
            processes.append(process)
            start_interval_reader(process, collector.add_client_event)

        print("\n[TRAFFIC] All congestion flows started.")
        for process, flow in zip(processes, flows):
            output = wait_for_process(process, timeout=DURATION + 30)
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
            active_flows=4,
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
            active_flows=4,
            total_throughput_mbps=total_throughput_mbps,
            **summary,
        )

    print("\n[TRAFFIC] Congestion scenario completed.")

def burst():
    """Five short UDP traffic bursts."""

    print("\n=== BURST SCENARIO ===")
    experiment_id = str(uuid.uuid4())

    for i in range(5):

        print(f"\n[TRAFFIC] Starting burst {i + 1}/5")

        monitor = ScenarioMonitor(["h1"]).start()
        collector = IntervalTelemetryCollector(
            "h1", "burst", 5201, experiment_id, 8, 1, monitor
        )
        try:
            output = _run_udp_flow("h1", 8, 5201, 3, collector)
        finally:
            summary = monitor.stop()

        save_telemetry(
            output,
            source="h1",
            scenario="burst",
            experiment_id=experiment_id,
            requested_mbps=8,
            active_flows=1,
            **summary,
        )

        print(f"[TRAFFIC] Burst {i + 1}/5 completed.")

        if i < 4:
            time.sleep(2)

    print("\n[TRAFFIC] Burst scenario completed.")

def moderate(rate_mbps: float = 6.0):
    """One configurable-rate UDP flow for 20 seconds."""
    if (
        isinstance(rate_mbps, bool)
        or not isinstance(rate_mbps, (int, float))
        or not math.isfinite(rate_mbps)
        or rate_mbps <= 0
    ):
        raise ValueError("moderate rate must be a positive finite number")

    print("\n=== MODERATE SCENARIO ===")
    experiment_id = str(uuid.uuid4())
    print(f"[EXPERIMENT] experiment_id={experiment_id}")

    monitor = ScenarioMonitor(["h1"]).start()
    collector = IntervalTelemetryCollector(
        "h1", "moderate", 5201, experiment_id, rate_mbps, 1, monitor
    )

    try:
        output = _run_udp_flow(
            "h1", rate_mbps, 5201, DURATION, collector
        )
    finally:
        summary = monitor.stop()

    save_telemetry(
        output,
        source="h1",
        scenario="moderate",
        experiment_id=experiment_id,
        requested_mbps=rate_mbps,
        active_flows=1,
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
        "--rate-mbps",
        type=float,
        default=6.0,
        help="offered UDP rate for the moderate scenario (default: 6 Mbps)",
    )

    args = parser.parse_args()
    if args.scenario == "moderate" and (
        not math.isfinite(args.rate_mbps) or args.rate_mbps <= 0
    ):
        parser.error("--rate-mbps must be a positive finite number")

    if args.scenario == "normal":
        normal()

    elif args.scenario == "congestion":
        congestion()

    elif args.scenario == "burst":
        burst()
    elif args.scenario == "moderate":
        moderate(args.rate_mbps)


if __name__ == "__main__":
    main()
