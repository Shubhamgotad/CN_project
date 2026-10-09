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
    write_interval_jsonl,
    write_jsonl,
)


SERVER_IP = "10.0.0.5"
DURATION = 20
BOTTLENECK_INTERFACE = "s1-eth5"
DATASET_DIR = Path(__file__).resolve().parents[1] / "telemetry" / "datasets"
INTERVAL_DATASET_DIR = DATASET_DIR
MONITOR_FRESHNESS_SECONDS = 2.0


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


def wait_for_process(process):
    """Wait for a traffic process and return its iperf3 output."""

    reader = getattr(process, "_interval_reader", None)
    if reader is not None:
        process.wait()
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

    output, _ = process.communicate()

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


def save_interval_telemetry(
    line,
    source,
    scenario,
    experiment_id,
    requested_mbps,
    active_flows,
    monitor,
):
    """Parse and persist one genuine iperf interval observation.

    Interval files are intentionally separate from the existing final
    aggregate files, so current consumers continue to see only final records.
    RTT and queue values are the latest monitor sample at interval-report
    receipt time; the two measurements are therefore aligned approximately,
    not guaranteed simultaneous.
    """
    filename = INTERVAL_DATASET_DIR / f"{scenario}_intervals.jsonl"
    records = parse_iperf_interval_events(
        line,
        source=source,
        experiment_id=experiment_id,
        scenario=scenario,
        requested_mbps=requested_mbps,
        active_flows=active_flows,
        bandwidth_mbps=10.0,
    )
    measurement = monitor.latest_measurement()
    for record in records:
        record.update(measurement)
        write_interval_jsonl(record, filename)


def normal():
    """One 3 Mbps UDP flow for 20 seconds."""

    print("\n=== NORMAL SCENARIO ===")
    experiment_id = str(uuid.uuid4())

    process = None
    monitor = ScenarioMonitor(["h1"]).start()
    try:
        process = run_on_host(
            "h1",
            [
                "iperf3", "-c", SERVER_IP, "-u", "-b", "3M",
                "-t", str(DURATION), "--json-stream",
            ]
        )
        start_interval_reader(
            process,
            lambda line: save_interval_telemetry(
                line, "h1", "normal", experiment_id, 3, 1, monitor
            ),
        )
        output = wait_for_process(process)
    finally:
        summary = monitor.stop()
        if process is not None:
            _stop_process(process)

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

    processes = []
    monitor = ScenarioMonitor(["h1", "h2", "h3", "h4"]).start()
    flow_outputs = []
    try:
        for host, rate, port in flows:
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
            start_interval_reader(
                process,
                lambda line, host=host, rate=rate: save_interval_telemetry(
                    line, host, "congestion", experiment_id, rate, 4, monitor
                ),
            )

        print("\n[TRAFFIC] All congestion flows started.")
        for process, flow in zip(processes, flows):
            output = wait_for_process(process)
            flow_outputs.append((output, flow))
    finally:
        summary = monitor.stop()
        for process in processes:
            _stop_process(process)

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

        process = None
        monitor = ScenarioMonitor(["h1"]).start()
        try:
            process = run_on_host(
                "h1",
                [
                    "iperf3", "-c", SERVER_IP, "-u", "-b", "8M",
                    "-t", "3", "--json-stream",
                ]
            )
            start_interval_reader(
                process,
                lambda line: save_interval_telemetry(
                    line, "h1", "burst", experiment_id, 8, 1, monitor
                ),
            )
            output = wait_for_process(process)
        finally:
            summary = monitor.stop()
            if process is not None:
                _stop_process(process)

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

    process = None
    monitor = ScenarioMonitor(["h1"]).start()

    try:
        process = run_on_host(
            "h1",
            [
                "iperf3",
                "-c", SERVER_IP,
                "-u",
                "-b", f"{rate_mbps:g}M",
                "-t", str(DURATION),
                "--json-stream",
            ],
        )
        start_interval_reader(
            process,
            lambda line: save_interval_telemetry(
                line, "h1", "moderate", experiment_id, rate_mbps, 1, monitor
            ),
        )
        output = wait_for_process(process)
    finally:
        summary = monitor.stop()
        if process is not None:
            _stop_process(process)

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