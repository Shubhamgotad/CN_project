import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path


_INTERVAL_WRITE_LOCK = threading.Lock()


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

    json_record = _parse_final_json_stream(output)
    if json_record is not None:
        throughput = json_record["throughput_mbps"]
        jitter = json_record["jitter_ms"]
        lost = json_record["lost_packets"]
        total = json_record["total_packets"]
        packet_loss = json_record["packet_loss_percent"]
    else:
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


def _parse_final_json_stream(output):
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = payload.get("event")
        if event not in (None, "end"):
            continue
        end = payload.get("data") if event == "end" else payload.get("end")
        if not isinstance(end, dict):
            continue
        summary = end.get("sum_received") or end.get("sum")
        if not isinstance(summary, dict):
            continue
        required = (
            "bits_per_second",
            "jitter_ms",
            "lost_packets",
            "packets",
            "lost_percent",
        )
        if not all(field in summary for field in required):
            continue
        return {
            "throughput_mbps": float(summary["bits_per_second"]) / 1_000_000,
            "jitter_ms": float(summary["jitter_ms"]),
            "lost_packets": int(summary["lost_packets"]),
            "total_packets": int(summary["packets"]),
            "packet_loss_percent": float(summary["lost_percent"]),
        }
    return None


def _interval_payloads(payload):
    if payload.get("event") != "interval":
        raise ValueError(
            "iperf JSON event is not an interval event"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("iperf interval event is missing object data")
    intervals = data.get("intervals")
    if intervals is not None:
        if not isinstance(intervals, list) or not intervals:
            raise ValueError("iperf interval data has no interval objects")
        return intervals
    return [data]


def parse_iperf_interval_events(
    line,
    source,
    experiment_id,
    scenario,
    requested_mbps,
    active_flows,
    bandwidth_mbps=10.0,
    measurement_timestamp=None,
):
    """Parse one explicit iperf3 interval event into one or more records."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("iperf event is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("iperf event must be a JSON object")
    return [
        _parse_interval_payload(
            interval,
            source=source,
            experiment_id=experiment_id,
            scenario=scenario,
            requested_mbps=requested_mbps,
            active_flows=active_flows,
            bandwidth_mbps=bandwidth_mbps,
            measurement_timestamp=measurement_timestamp,
        )
        for interval in _interval_payloads(payload)
    ]


def parse_iperf_interval(
    line,
    source,
    experiment_id,
    scenario,
    requested_mbps,
    active_flows,
    bandwidth_mbps=10.0,
    measurement_timestamp=None,
):
    """Parse one genuine iperf3 ``--json-stream`` interval report.

    The timestamp is the wall-clock time when the interval report was read.
    It is an observation timestamp, not a fabricated regularized timestamp.
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("interval output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("interval output must be a JSON object")
    if payload.get("event") == "interval":
        events = parse_iperf_interval_events(
            line,
            source,
            experiment_id,
            scenario,
            requested_mbps,
            active_flows,
            bandwidth_mbps,
            measurement_timestamp,
        )
        if len(events) != 1:
            raise ValueError("interval event contains multiple intervals")
        return events[0]
    raise ValueError("iperf interval event must use event=interval")


def _parse_interval_payload(
    interval,
    source,
    experiment_id,
    scenario,
    requested_mbps,
    active_flows,
    bandwidth_mbps,
    measurement_timestamp,
):
    if not isinstance(interval, dict):
        raise ValueError("iperf interval must be an object")
    summary = interval.get("sum")
    if not isinstance(summary, dict):
        streams = interval.get("streams")
        if not isinstance(streams, list) or not streams:
            raise ValueError("UDP interval is missing sum or streams data")
        summary = streams[0]
    required = (
        "start",
        "end",
        "bits_per_second",
        "jitter_ms",
        "lost_packets",
        "packets",
        "lost_percent",
    )
    if not all(field in summary for field in required):
        if "jitter_ms" not in summary:
            raise ValueError("iperf interval is TCP or missing UDP jitter_ms")
        raise ValueError("interval JSON is missing required UDP metrics")
    try:
        start = float(summary["start"])
        end = float(summary["end"])
        throughput = float(summary["bits_per_second"])
        jitter = float(summary["jitter_ms"])
        lost_raw = summary["lost_packets"]
        total_raw = summary["packets"]
        if (
            isinstance(lost_raw, bool)
            or not isinstance(lost_raw, int)
            or isinstance(total_raw, bool)
            or not isinstance(total_raw, int)
        ):
            raise ValueError("interval packet counters must be integers")
        lost = lost_raw
        total = total_raw
        packet_loss = float(summary["lost_percent"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("interval contains invalid numeric metrics") from exc
    if not all(math.isfinite(value) for value in (
        start, end, throughput, jitter, packet_loss
    )):
        raise ValueError("interval metrics must be finite")
    if end <= start:
        raise ValueError("interval duration must be positive")
    if throughput < 0 or jitter < 0:
        raise ValueError("interval throughput and jitter must be non-negative")
    if lost < 0 or total < 0 or lost > total:
        raise ValueError("interval packet counters are invalid")
    if not 0 <= packet_loss <= 100:
        raise ValueError("interval packet loss must be between 0 and 100")
    if total == 0:
        if lost != 0 or packet_loss != 0:
            raise ValueError(
                "zero-packet intervals must have zero loss and zero percent"
            )
    elif abs(packet_loss - (lost / total * 100)) > 0.5:
        raise ValueError("packet loss percentage is inconsistent with counters")
    if not math.isfinite(bandwidth_mbps) or bandwidth_mbps <= 0:
        raise ValueError("link bandwidth must be a positive finite number")
    values = {
        "throughput_mbps": throughput / 1_000_000,
        "jitter_ms": jitter,
        "lost_packets": lost,
        "total_packets": total,
        "packet_loss_percent": packet_loss,
    }
    timestamp = measurement_timestamp or datetime.now(timezone.utc).isoformat()
    utilization = values["throughput_mbps"] / bandwidth_mbps * 100
    if not 0 <= utilization <= 100:
        raise ValueError("interval utilization must be between 0 and 100")
    return {
        "timestamp": timestamp,
        "observation_timestamp": timestamp,
        "interval_start_seconds": start,
        "interval_end_seconds": end,
        "interval_duration_seconds": end - start,
        "interval_id": f"{experiment_id}:{source}:{start:g}-{end:g}",
        "experiment_id": experiment_id,
        "scenario": scenario,
        "source": source,
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": requested_mbps,
        **values,
        "bandwidth_mbps": bandwidth_mbps,
        "utilization_percent": round(utilization, 2),
        "active_flows": active_flows,
        "queue_size": None,
        "queue_drops": None,
    }


def write_jsonl(record, filename):
    """Append one telemetry record to a JSONL file."""

    with open(filename, "a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")


def write_interval_jsonl(record, filename):
    """Write interval samples to a dedicated file, separate from final aggregates."""
    normalized_path = Path(filename).expanduser().resolve()
    identity = (
        record.get("experiment_id"),
        record.get("source"),
        record.get("interval_start_seconds"),
        record.get("interval_end_seconds"),
    )
    with _INTERVAL_WRITE_LOCK:
        existing_identities = set()
        if normalized_path.exists():
            with normalized_path.open("r", encoding="utf-8") as file:
                for line in file:
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    existing_identities.add(
                        (
                            existing.get("experiment_id"),
                            existing.get("source"),
                            existing.get("interval_start_seconds"),
                            existing.get("interval_end_seconds"),
                        )
                    )
        if identity in existing_identities:
            return False
        with normalized_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, allow_nan=False) + "\n")
    return True