#!/usr/bin/env python3

import argparse
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemetry.iperf_parser import parse_iperf_output, write_jsonl


SERVER_IP = "10.0.0.5"
DURATION = 20
BOTTLENECK_INTERFACE = "s1-eth5"
_LAST_QDISC_DROPS = {}


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


def wait_for_process(process):
    """Wait for a traffic process and return its iperf3 output."""

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


def measure_queue_stats(interface=BOTTLENECK_INTERFACE):
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

    backlog_packets = re.findall(r"\bbacklog\s+\d+\w*\s+(\d+)p\b", result.stdout)
    drop_counts = re.findall(r"\bdropped\s+(\d+)\b", result.stdout)

    queue_size = max(map(int, backlog_packets)) if backlog_packets else None
    cumulative_drops = sum(map(int, drop_counts)) if drop_counts else None
    queue_drops = None

    if cumulative_drops is not None:
        previous_drops = _LAST_QDISC_DROPS.get(interface)
        if previous_drops is None or cumulative_drops < previous_drops:
            queue_drops = cumulative_drops
        else:
            queue_drops = cumulative_drops - previous_drops
        _LAST_QDISC_DROPS[interface] = cumulative_drops

    return {"queue_size": queue_size, "queue_drops": queue_drops}


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

    filename = f"telemetry/datasets/{scenario}.jsonl"

    write_jsonl(record, filename)

    print(f"[TELEMETRY] Saved: {filename}")
    print(f"[TELEMETRY] {record}")


def normal():
    """One 3 Mbps UDP flow for 20 seconds."""

    print("\n=== NORMAL SCENARIO ===")
    experiment_id = str(uuid.uuid4())

    process = run_on_host(
        "h1",
        ["iperf3", "-c", SERVER_IP, "-u", "-b", "3M", "-t", str(DURATION)]
    )

    output = wait_for_process(process)
    latency_ms = measure_latency("h1")
    queue_stats = measure_queue_stats()

    save_telemetry(
        output,
        source="h1",
        scenario="normal",
        experiment_id=experiment_id,
        requested_mbps=3,
        active_flows=1,
        latency_ms=latency_ms,
        **queue_stats,
    )

    print("\n[TRAFFIC] Normal scenario completed.")


def congestion():
    """Four 4 Mbps UDP flows for 20 seconds through the bottleneck."""

    print("\n=== CONGESTION SCENARIO ===")
    experiment_id = str(uuid.uuid4())

    flows = [
        ("h1", 4, 5201),
        ("h2", 4, 5202),
        ("h3", 4, 5203),
        ("h4", 4, 5204),
    ]

    processes = []

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
            ],
        )

        processes.append(process)

    print("\n[TRAFFIC] All congestion flows started.")

    flow_outputs = []
    for process, flow in zip(processes, flows):
        output = wait_for_process(process)
        flow_outputs.append((output, flow))

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
    queue_stats = measure_queue_stats()

    for output, (host, rate, port) in flow_outputs:
        latency_ms = measure_latency(host)
        save_telemetry(
            output,
            source=host,
            scenario="congestion",
            experiment_id=experiment_id,
            requested_mbps=rate,
            active_flows=4,
            total_throughput_mbps=total_throughput_mbps,
            latency_ms=latency_ms,
            **queue_stats,
        )

    print("\n[TRAFFIC] Congestion scenario completed.")

def burst():
    """Five short UDP traffic bursts."""

    print("\n=== BURST SCENARIO ===")
    experiment_id = str(uuid.uuid4())

    for i in range(5):

        print(f"\n[TRAFFIC] Starting burst {i + 1}/5")

        process = run_on_host(
            "h1",
            ["iperf3", "-c", SERVER_IP, "-u", "-b", "8M", "-t", "3"]
        )

        output = wait_for_process(process)
        latency_ms = measure_latency("h1")
        queue_stats = measure_queue_stats()

        save_telemetry(
            output,
            source="h1",
            scenario="burst",
            experiment_id=experiment_id,
            requested_mbps=8,
            active_flows=1,
            latency_ms=latency_ms,
            **queue_stats,
        )

        print(f"[TRAFFIC] Burst {i + 1}/5 completed.")

        if i < 4:
            time.sleep(2)

    print("\n[TRAFFIC] Burst scenario completed.")


def main():

    parser = argparse.ArgumentParser(
        description="Traffic generator for Adaptive QoS project"
    )

    parser.add_argument(
        "--scenario",
        choices=["normal", "congestion", "burst"],
        required=True
    )

    args = parser.parse_args()

    if args.scenario == "normal":
        normal()

    elif args.scenario == "congestion":
        congestion()

    elif args.scenario == "burst":
        burst()


if __name__ == "__main__":
    main()