import json
import time
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from traffic.traffic_generator import _IperfIntervalReader
from telemetry.iperf_parser import (
    parse_iperf_interval,
    parse_iperf_interval_events,
    parse_iperf_output,
    write_interval_jsonl,
)


def _interval(start=1.0, end=2.0):
    return json.dumps(
        {
            "event": "interval",
            "data": {
                "streams": [{
                    "start": start,
                    "end": end,
                    "bits_per_second": 4_000_000,
                    "jitter_ms": 0.4,
                    "lost_packets": 2,
                    "packets": 100,
                    "lost_percent": 2.0,
                }],
                "sum": {
                    "start": start,
                    "end": end,
                    "bits_per_second": 4_000_000,
                    "jitter_ms": 0.4,
                    "lost_packets": 2,
                    "packets": 100,
                    "lost_percent": 2.0,
                },
            },
        }
    )


def test_interval_parser_uses_observation_timestamp_and_metadata():
    record = parse_iperf_interval(
        _interval(),
        source="h1",
        experiment_id="exp-1",
        scenario="normal",
        requested_mbps=3,
        active_flows=1,
        measurement_timestamp="2025-01-01T00:00:02+00:00",
    )

    assert record["timestamp"] == "2025-01-01T00:00:02+00:00"
    assert record["experiment_id"] == "exp-1"
    assert record["throughput_mbps"] == 4.0
    assert record["packet_loss_percent"] == 2.0
    assert record["interval_duration_seconds"] == 1.0
    assert record["interval_start_seconds"] == 1.0


def test_interval_event_with_interval_array_is_parsed():
    line = json.loads(_interval())
    line["data"].pop("sum")
    line["data"] = {"intervals": [line["data"], line["data"]]}
    records = parse_iperf_interval_events(
        json.dumps(line), "h1", "exp-1", "normal", 3, 1,
        measurement_timestamp="2025-01-01T00:00:02+00:00",
    )
    assert len(records) == 2


@pytest.mark.parametrize(
    "line",
    [
        "{}",
        json.dumps({"event": "start", "data": {}}),
        _interval(start=2, end=1),
    ],
)
def test_interval_parser_rejects_missing_or_invalid_metrics(line):
    with pytest.raises(ValueError):
        parse_iperf_interval(
            line,
            source="h1",
            experiment_id="exp-1",
            scenario="normal",
            requested_mbps=3,
            active_flows=1,
        )


def test_interval_writer_preserves_chronological_input(tmp_path):
    path = tmp_path / "normal_intervals.jsonl"
    first = parse_iperf_interval(
        _interval(0, 1),
        "h1",
        "exp-1",
        "normal",
        3,
        1,
        measurement_timestamp="2025-01-01T00:00:01+00:00",
    )
    second = parse_iperf_interval(
        _interval(1, 2),
        "h1",
        "exp-1",
        "normal",
        3,
        1,
        measurement_timestamp="2025-01-01T00:00:02+00:00",
    )
    write_interval_jsonl(first, path)
    write_interval_jsonl(second, path)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["timestamp"] for record in records] == [
        "2025-01-01T00:00:01+00:00",
        "2025-01-01T00:00:02+00:00",
    ]


def test_interval_writer_is_thread_safe_and_deduplicates(tmp_path):
    path = tmp_path / "normal_intervals.jsonl"
    record = parse_iperf_interval(
        _interval(), "h1", "exp-1", "normal", 3, 1,
        measurement_timestamp="2025-01-01T00:00:01+00:00",
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: write_interval_jsonl(record, path), range(8)
        ))
    assert sum(results) == 1
    assert len(path.read_text().splitlines()) == 1
    assert write_interval_jsonl(record, Path(path).absolute()) is False
    later = dict(record)
    later["experiment_id"] = "exp-2"
    assert write_interval_jsonl(later, path) is True
    assert len(path.read_text().splitlines()) == 2


def test_final_text_parser_remains_backward_compatible():
    record = parse_iperf_output(
        "[ 7] 0.00-20.00 sec 9.54 MBytes 4.00 Mbits/sec "
        "0.000 ms 0/100 (0%) receiver",
        source="h1",
        scenario="normal",
        requested_mbps=3,
        active_flows=1,
    )
    assert record["throughput_mbps"] == 4.0


def test_final_json_stream_event_is_parsed():
    line = json.dumps({
        "event": "end",
        "data": {
            "sum_received": {
                "bits_per_second": 4_000_000,
                "jitter_ms": 0.4,
                "lost_packets": 2,
                "packets": 100,
                "lost_percent": 2.0,
            }
        },
    })
    record = parse_iperf_output(
        line, source="h1", scenario="normal", requested_mbps=3
    )
    assert record["throughput_mbps"] == 4.0


@pytest.mark.parametrize("field, value", [
    ("bits_per_second", -1),
    ("jitter_ms", -1),
    ("lost_packets", -1),
    ("packets", 1),
    ("lost_percent", 101),
])
def test_interval_parser_rejects_invalid_metrics(field, value):
    payload = json.loads(_interval())
    payload["data"]["sum"][field] = value
    with pytest.raises(ValueError):
        parse_iperf_interval(
            json.dumps(payload), "h1", "exp-1", "normal", 3, 1
        )


@pytest.mark.parametrize(
    "lost, packets, percent",
    [(1, 3, 33.33), (2, 3, 66.67), (0, 0, 0.0)],
)
def test_interval_parser_accepts_valid_loss_rounding_and_zero_packets(
    lost, packets, percent
):
    payload = json.loads(_interval())
    summary = payload["data"]["sum"]
    summary.update(
        lost_packets=lost,
        packets=packets,
        lost_percent=percent,
    )
    record = parse_iperf_interval(
        json.dumps(payload), "h1", "exp-1", "normal", 3, 1
    )
    assert record["lost_packets"] == lost


@pytest.mark.parametrize(
    "lost, packets, percent",
    [ (1, 0, 0.0), (1, 3, 10.0), (4, 3, 100.0) ],
)
def test_interval_parser_rejects_inconsistent_loss(lost, packets, percent):
    payload = json.loads(_interval())
    payload["data"]["sum"].update(
        lost_packets=lost,
        packets=packets,
        lost_percent=percent,
    )
    with pytest.raises(ValueError):
        parse_iperf_interval(
            json.dumps(payload), "h1", "exp-1", "normal", 3, 1
        )


def test_interval_parser_rejects_invalid_bandwidth_and_overutilization():
    with pytest.raises(ValueError, match="bandwidth"):
        parse_iperf_interval(
            _interval(), "h1", "exp-1", "normal", 3, 1, bandwidth_mbps=0
        )
    with pytest.raises(ValueError, match="utilization"):
        parse_iperf_interval(
            _interval(), "h1", "exp-1", "normal", 3, 1, bandwidth_mbps=1
        )


def test_interval_reader_drains_pipe_before_reporting_callback_failure():
    process = type("Process", (), {})()
    process.stdout = StringIO(_interval() + "\n" + _interval(2, 3))
    seen = []

    def failing_callback(line):
        seen.append(line)
        raise ValueError("write failed")

    reader = _IperfIntervalReader(process, failing_callback).start()
    reader.join()

    assert len(seen) == 2
    assert isinstance(reader.error, ValueError)


def test_interval_reader_ignores_non_interval_events():
    process = type("Process", (), {})()
    process.stdout = StringIO(
        json.dumps({"event": "start", "data": {}})
        + "\n"
        + json.dumps({"event": "error", "data": {"error": "failed"}})
    )
    seen = []
    reader = _IperfIntervalReader(process, seen.append).start()
    assert reader.join() == process.stdout.getvalue()
    assert seen == []
    assert isinstance(reader.error, RuntimeError)


def test_reader_reports_callback_and_nonzero_process_failure():
    class Process:
        returncode = 7
        args = ["iperf3"]

        def wait(self):
            return self.returncode

    process = Process()
    process.stdout = StringIO(_interval())
    reader = _IperfIntervalReader(
        process, lambda _: (_ for _ in ()).throw(ValueError("write failed"))
    ).start()
    from traffic.traffic_generator import wait_for_process

    with pytest.raises(RuntimeError, match="exited with an error"):
        process._interval_reader = reader
        wait_for_process(process)


def test_monitor_latest_measurement_marks_stale_and_fresh_samples(monkeypatch):
    from traffic import traffic_generator

    monitor = traffic_generator.ScenarioMonitor(["h1"])
    monitor._latest_latency = 5.0
    monitor._latest_queue = {"queue_size": 2, "queue_drops": 1}
    monitor._latency_timestamp = "2025-01-01T00:00:00+00:00"
    monitor._queue_timestamp = "2025-01-01T00:00:01+00:00"
    monitor._latency_monotonic = time.monotonic()
    monitor._queue_monotonic = time.monotonic()
    assert monitor.latest_measurement(max_age=1)["latency_ms"] == 5.0

    monitor._latency_monotonic = time.monotonic() - 10
    stale = monitor.latest_measurement(max_age=1)
    assert stale["latency_ms"] is None
    assert stale["latency_measurement_timestamp"] == (
        "2025-01-01T00:00:00+00:00"
    )
    assert stale["latency_measurement_age_seconds"] >= 10
    assert stale["queue_size"] == 2


def test_monitor_failure_does_not_refresh_corresponding_timestamp(monkeypatch):
    from traffic import traffic_generator

    monitor = traffic_generator.ScenarioMonitor(["h1"])
    monitor._latest_latency = 5.0
    monitor._latency_timestamp = "latency-before"
    monitor._latency_monotonic = time.monotonic()
    monitor._latest_queue = {"queue_size": 2, "queue_drops": 1}
    monitor._queue_timestamp = "queue-before"
    monitor._queue_monotonic = time.monotonic()
    monkeypatch.setattr(
        traffic_generator,
        "measure_latency",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ping")),
    )
    monkeypatch.setattr(
        traffic_generator,
        "measure_queue_stats",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("tc")),
    )
    monitor._sample()
    assert monitor._latency_timestamp == "latency-before"
    assert monitor._queue_timestamp == "queue-before"
