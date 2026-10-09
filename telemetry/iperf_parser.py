import json
import re
from datetime import datetime, timezone


def parse_iperf_output(
    output,
    source,
    destination="server",
    scenario="unknown",
    traffic_type="udp",
    requested_mbps=None,
    active_flows=1,
    bandwidth_mbps=10.0,
):
    """
    Parse the final receiver line from iperf3 output
    and convert it into our standard telemetry format.
    """

    # Example:
    # 0.00-21.20 sec  5.98 MBytes  2.37 Mbits/sec
    # 0.450 ms  2572/6905 (37%)

    pattern = (
        r"\[.*?\]\s+"
        r"[\d.]+-[\d.]+\s+sec\s+"
        r"[\d.]+\s+\w+Bytes\s+"
        r"([\d.]+)\s+Mbits/sec\s+"
        r"([\d.]+)\s+ms\s+"
        r"(\d+)/(\d+)\s+\(([\d.]+)%\)\s+receiver"
    )

    match = re.search(pattern, output)

    if not match:
        raise ValueError(
            "Could not find final UDP receiver statistics "
            "in iperf3 output."
        )

    throughput = float(match.group(1))
    jitter = float(match.group(2))
    lost = int(match.group(3))
    total = int(match.group(4))
    packet_loss = float(match.group(5))

    utilization = (
        (throughput / bandwidth_mbps) * 100
        if bandwidth_mbps > 0
        else 0
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scenario": scenario,
        "source": source,
        "destination": destination,
        "traffic_type": traffic_type,
        "requested_mbps": requested_mbps,
        "throughput_mbps": throughput,
        "jitter_ms": jitter,
        "packet_loss_percent": packet_loss,
        "lost_packets": lost,
        "total_packets": total,
        "bandwidth_mbps": bandwidth_mbps,
        "utilization_percent": round(utilization, 2),
        "active_flows": active_flows,
    }


def write_jsonl(record, filename):
    """Append one telemetry record to a JSONL file."""

    with open(filename, "a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")