import json
import subprocess
import time
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from traffic import traffic_generator
from traffic.traffic_generator import (
    _IperfIntervalReader,
    _run_udp_flow,
    start_receiver_listener,
)


def _run_directory(tmp_path):
    return traffic_generator._prepare_output_directory(tmp_path / "run")
from telemetry.iperf_parser import (
    parse_iperf_interval,
    parse_iperf_interval_events,
    parse_iperf_output,
    parse_iperf_sender_interval_events,
    write_interval_jsonl,
)
from traffic.traffic_generator import IntervalTelemetryCollector


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


def _client_interval(start=1.0, end=2.0, throughput=4_000_000):
    return json.dumps({
        "event": "interval",
        "data": {
            "sum": {
                "start": start,
                "end": end,
                "bits_per_second": throughput,
                "packets": 100,
            }
        },
    })


class _FakeMonitor:
    def latest_measurement(self):
        return {
            "latency_ms": 12.0,
            "queue_size": 3,
            "queue_drops": 1,
            "latency_measurement_timestamp": "latency-time",
            "queue_measurement_timestamp": "queue-time",
            "latency_measurement_age_seconds": 0.1,
            "queue_measurement_age_seconds": 0.2,
        }


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


def test_sender_interval_parser_accepts_udp_without_receiver_metrics():
    event = parse_iperf_sender_interval_events(
        _client_interval(), measurement_timestamp="client-time"
    )
    assert event == [{
        "start": 1.0,
        "end": 2.0,
        "throughput_mbps": 4.0,
        "timestamp": "client-time",
    }]


def test_collector_pairs_sender_throughput_with_receiver_metrics(
    tmp_path, monkeypatch
):
    collector = IntervalTelemetryCollector(
        "h1", "normal", 5201, "exp-1", 3, 1, _FakeMonitor(),
        output_directory=_run_directory(tmp_path),
    )
    collector.add_receiver_event(_interval(1.1, 2.1))
    collector.add_client_event(_client_interval(throughput=3_250_000))
    collector.validate_complete()

    record = json.loads(
        (tmp_path / "run" / "normal_exp-1_h1_intervals.jsonl")
        .read_text()
        .strip()
    )
    assert record["experiment_id"] == "exp-1"
    assert record["source"] == "h1"
    assert record["scenario"] == "normal"
    assert record["throughput_mbps"] == 4.0
    assert record["throughput_measurement_source"] == "iperf3_server_json_stream"
    assert record["sender_throughput_mbps"] == 3.25
    assert record["sender_measurement_source"] == "iperf3_client_json_stream"
    assert record["interval_start_seconds"] == 1.1
    assert record["interval_end_seconds"] == 2.1
    assert record["sender_interval_start_seconds"] == 1.0
    assert record["sender_interval_end_seconds"] == 2.0
    assert record["jitter_ms"] == 0.4
    assert record["lost_packets"] == 2
    assert record["packet_loss_percent"] == 2.0
    assert record["timestamp"] == record["observation_timestamp"]
    assert isinstance(record["receiver_observation_monotonic_seconds"], float)
    assert record["receiver_observation_role"] == "diagnostic_only"
    assert "absolute_start_seconds" not in record
    assert "absolute_end_seconds" not in record
    assert record["latency_ms"] == 12.0
    assert record["queue_size"] == 3
    assert record["latency_measurement_timestamp"] == "latency-time"


def test_collector_writes_to_explicit_fresh_run_directory(tmp_path):
    run_directory = traffic_generator._prepare_output_directory(
        tmp_path / "unique-repetition"
    )
    collector = IntervalTelemetryCollector(
        "h1", "congestion", 5201, "run-unique", 4, 4, _FakeMonitor(),
        output_directory=run_directory,
    )
    collector.add_receiver_event(_interval(0, 1))
    collector.add_client_event(_client_interval(0, 1, throughput=3_000_000))
    collector.validate_complete()
    output = run_directory / "congestion_run-unique_h1_intervals.jsonl"
    record = json.loads(output.read_text().strip())
    assert record["throughput_mbps"] == 4.0
    assert record["sender_throughput_mbps"] == 3.0


def test_collector_identifies_missing_receiver_interval_by_flow(
    tmp_path, monkeypatch
):
    collector = IntervalTelemetryCollector(
        "h3", "congestion", 5203, "exp-3", 4, 4, _FakeMonitor(),
        output_directory=_run_directory(tmp_path),
    )
    collector.add_client_event(_client_interval(2.0, 3.0))

    with pytest.raises(
        RuntimeError,
        match=r"source=h3 scenario=congestion port=5203 .*interval=2-3s",
    ):
        collector.validate_complete()


def test_collector_persists_receiver_tail_after_client_finishes(
    tmp_path, monkeypatch, capsys
):
    class ChangingMonitor(_FakeMonitor):
        calls = 0

        def latest_measurement(self):
            self.calls += 1
            measurement = super().latest_measurement()
            measurement["latency_measurement_timestamp"] = (
                f"sample-{self.calls}"
            )
            return measurement

    monitor = ChangingMonitor()
    collector = IntervalTelemetryCollector(
        "h1", "normal", 5201, "exp-tail", 3, 1, monitor,
        output_directory=_run_directory(tmp_path),
    )
    collector.add_client_event(_client_interval(19.0, 20.0))
    collector.add_receiver_event(_interval(19.0, 20.0))
    collector.add_receiver_event(_interval(20.0, 20.03))

    collector.validate_complete()

    records = [
        json.loads(line)
        for line in (tmp_path / "run" / "normal_exp-tail_h1_intervals.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(records) == 2
    assert records[1]["interval_start_seconds"] == 20.0
    assert records[1]["interval_end_seconds"] == 20.03
    assert records[1]["throughput_measurement_source"] == "receiver"
    assert records[1]["jitter_ms"] == 0.4
    assert records[1]["latency_ms"] == 12.0
    assert records[0]["latency_measurement_timestamp"] == "sample-1"
    assert records[1]["latency_measurement_timestamp"] == "sample-3"
    assert "Persisted receiver-only post-client drain interval" in (
        capsys.readouterr().out
    )


def test_collector_rejects_unmatched_receiver_interval_inside_run(
    tmp_path, monkeypatch
):
    collector = IntervalTelemetryCollector(
        "h1", "normal", 5201, "exp-gap", 3, 1, _FakeMonitor(),
        output_directory=_run_directory(tmp_path),
    )
    collector.add_client_event(_client_interval(1.0, 2.0))
    collector.add_receiver_event(_interval(1.0, 2.0))
    collector.add_receiver_event(_interval(3.0, 4.0))

    with pytest.raises(RuntimeError, match="Missing client interval"):
        collector.validate_complete()


def test_collectors_do_not_cross_match_concurrent_flows(tmp_path, monkeypatch):
    run_directory = _run_directory(tmp_path)
    collectors = [
        IntervalTelemetryCollector(
            host, "congestion", port, f"exp-{port}", 4, 4, _FakeMonitor(),
            output_directory=run_directory,
        )
        for host, port in (("h1", 5201), ("h2", 5202))
    ]
    for collector in collectors:
        collector.add_client_event(_client_interval(0, 1, 1_500_000))
    collectors[1].add_receiver_event(_interval(0, 1))
    collectors[0].add_receiver_event(_interval(0, 1))
    for collector in collectors:
        collector.validate_complete()

    records = [
        json.loads(line)
        for path in sorted((tmp_path / "run").glob("congestion_*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert {(row["source"], row["experiment_id"]) for row in records} == {
        ("h1", "exp-5201"),
        ("h2", "exp-5202"),
    }
    assert all(row["throughput_mbps"] == 4.0 for row in records)
    assert all(row["sender_throughput_mbps"] == 1.5 for row in records)


def test_receiver_parser_rejects_events_missing_udp_metrics(tmp_path):
    payload = json.loads(_interval())
    del payload["data"]["sum"]["jitter_ms"]
    with pytest.raises(
        ValueError,
        match=r"source=h1 scenario=normal port=5201 .*interval=1-2s:"
        r" iperf interval is TCP or missing UDP jitter_ms",
    ):
        IntervalTelemetryCollector(
            "h1", "normal", 5201, "exp-1", 3, 1, _FakeMonitor(),
            output_directory=_run_directory(tmp_path),
        ).add_receiver_event(json.dumps(payload))


def test_receiver_listener_startup_timeout_terminates_started_process(
    monkeypatch
):
    class Process:
        args = ["iperf3", "-s"]
        returncode = None

        def __init__(self):
            self.stdout = StringIO("")
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    calls = []
    monkeypatch.setattr(
        "traffic.traffic_generator.run_on_host",
        lambda host, command, **kwargs: calls.append((host, command)) or process,
    )
    monkeypatch.setattr(
        "traffic.traffic_generator._iperf_port_is_listening",
        lambda port: False,
    )
    monkeypatch.setattr(
        "traffic.traffic_generator.IPERF_LISTENER_START_TIMEOUT_SECONDS", 0
    )

    with pytest.raises(TimeoutError, match="port 5201"):
        start_receiver_listener(5201, lambda _: None)

    assert calls[0][0] == "server"
    assert calls[0][1] == [
        "iperf3", "-s", "-1", "-p", "5201", "--json-stream"
    ]
    assert process.terminated
    assert not process._interval_reader._thread.is_alive()


def test_receiver_listener_refuses_to_duplicate_an_existing_port(monkeypatch):
    monkeypatch.setattr(
        "traffic.traffic_generator._iperf_port_is_listening",
        lambda port: True,
    )
    monkeypatch.setattr(
        "traffic.traffic_generator.run_on_host",
        lambda *args, **kwargs: pytest.fail("must not launch a duplicate listener"),
    )

    with pytest.raises(RuntimeError, match="already listening"):
        start_receiver_listener(5201, lambda _: None)


def test_udp_flow_runtime_failure_stops_client_and_receiver(monkeypatch):
    class Process:
        args = ["iperf3"]
        returncode = None

        def __init__(self):
            self.stdout = StringIO("")
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    client = Process()
    listener = Process()
    stopped = []
    collector = type(
        "Collector",
        (),
        {
            "add_receiver_event": lambda self, line: None,
            "add_client_event": lambda self, line: None,
        },
    )()
    monkeypatch.setattr(
        "traffic.traffic_generator.start_receiver_listener",
        lambda port, callback: listener,
    )
    monkeypatch.setattr(
        "traffic.traffic_generator.stop_receiver_listener",
        lambda process, port: stopped.append((process, port)),
    )
    monkeypatch.setattr(
        "traffic.traffic_generator.run_on_host",
        lambda *args, **kwargs: client,
    )
    monkeypatch.setattr(
        "traffic.traffic_generator.wait_for_process",
        lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(TimeoutError("client timeout")),
    )

    with pytest.raises(TimeoutError, match="client timeout"):
        _run_udp_flow("h1", 3, 5201, 20, collector)

    assert client.terminated
    assert stopped == [(listener, 5201)]


def test_wait_for_process_timeout_terminates_and_drains_reader():
    class Process:
        args = ["iperf3"]
        returncode = None

        def __init__(self):
            self.stdout = StringIO("")
            self.terminated = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if not self.terminated:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = -15
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.returncode = -9

    process = Process()
    from traffic.traffic_generator import start_interval_reader, wait_for_process

    start_interval_reader(process, lambda _: None)
    with pytest.raises(TimeoutError, match="timed out"):
        wait_for_process(process, timeout=0.01)

    assert process.terminated
    assert not process._interval_reader._thread.is_alive()



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


@pytest.mark.parametrize(
    "latency_age,queue_age,expected_latency,expected_queue",
    [
        (0.1, 0.2, 5.0, 2),
        (3.0, 0.2, None, 2),
        (0.1, 3.0, 5.0, None),
        (3.0, 4.0, None, None),
        (None, None, None, None),
    ],
)
def test_monitor_checks_rtt_and_queue_freshness_independently(
    monkeypatch, latency_age, queue_age, expected_latency, expected_queue
):
    from traffic import traffic_generator

    now = 100.0
    monkeypatch.setattr(
        traffic_generator.time, "monotonic", lambda: now
    )
    monitor = traffic_generator.ScenarioMonitor(["h1"])
    monitor._latest_latency = 5.0
    monitor._latest_queue = {"queue_size": 2, "queue_drops": 1}
    monitor._latency_timestamp = (
        "latency-time" if latency_age is not None else None
    )
    monitor._queue_timestamp = (
        "queue-time" if queue_age is not None else None
    )
    monitor._latency_monotonic = (
        now - latency_age if latency_age is not None else None
    )
    monitor._queue_monotonic = (
        now - queue_age if queue_age is not None else None
    )

    measurement = monitor.latest_measurement(max_age=2.0)

    assert measurement["latency_ms"] == expected_latency
    assert measurement["queue_size"] == expected_queue
    assert measurement["queue_drops"] == (
        1 if expected_queue is not None else None
    )
    assert measurement["latency_measurement_timestamp"] == (
        "latency-time" if latency_age is not None else None
    )
    assert measurement["queue_measurement_timestamp"] == (
        "queue-time" if queue_age is not None else None
    )
    assert measurement["latency_measurement_age_seconds"] == (
        pytest.approx(latency_age) if latency_age is not None else None
    )
    assert measurement["queue_measurement_age_seconds"] == (
        pytest.approx(queue_age) if queue_age is not None else None
    )


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
