import pytest

from traffic import traffic_generator


def test_queue_drop_delta_and_counter_reset():
    assert traffic_generator._drop_delta(10, 15) == 5
    assert traffic_generator._drop_delta(10, 3) == 3
    assert traffic_generator._drop_delta(None, 3) is None
    assert traffic_generator._drop_delta(10, None) is None


def test_queue_stats_missing_counters_are_none():
    stats = traffic_generator._queue_stats_from_output(
        "qdisc fq_codel 8001: root refcnt 2 limit 10240p"
    )
    assert stats["queue_size"] is None
    assert stats["queue_drops"] is None
    assert stats["cumulative_drops"] is None


def test_queue_stats_use_baseline_and_never_report_negative():
    output = "backlog 32Kb 4p requeues 0 dropped 12"
    stats = traffic_generator._queue_stats_from_output(output, initial_drops=20)
    assert stats["queue_size"] == 4
    assert stats["queue_drops"] == 12

    stats = traffic_generator._queue_stats_from_output(output, initial_drops=10)
    assert stats["queue_drops"] == 2


def test_measurement_summary_uses_mean_latency_and_max_backlog():
    summary = traffic_generator.summarize_measurements(
        [10.0, None, 20.0],
        [2, 8, None],
        [0, 3, None],
    )
    assert summary == {
        "latency_ms": 15.0,
        "queue_size": 8,
        "queue_drops": 3,
    }


def test_monitor_stops_and_joins_after_measurement_failure(monkeypatch):
    failed = []
    monkeypatch.setattr(traffic_generator, "_initial_queue_drops", lambda _: 0)
    monkeypatch.setattr(
        traffic_generator,
        "measure_latency",
        lambda *args, **kwargs: (
            failed.append(True),
            (_ for _ in ()).throw(RuntimeError("ping failed")),
        )[1],
    )
    monkeypatch.setattr(
        traffic_generator,
        "measure_queue_stats",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("tc failed")),
    )

    monitor = traffic_generator.ScenarioMonitor(["h1"], interval=60).start()
    summary = monitor.stop()

    assert failed
    assert monitor._thread is not None
    assert not monitor._thread.is_alive()
    assert summary == {
        "latency_ms": None,
        "queue_size": None,
        "queue_drops": None,
    }


def test_monitor_can_stop_without_waiting_for_real_interval(monkeypatch):
    monkeypatch.setattr(traffic_generator, "_initial_queue_drops", lambda _: 0)
    monkeypatch.setattr(
        traffic_generator, "measure_latency", lambda *args, **kwargs: 12.0
    )
    monkeypatch.setattr(
        traffic_generator,
        "measure_queue_stats",
        lambda *args, **kwargs: {"queue_size": 4, "queue_drops": 2},
    )

    monitor = traffic_generator.ScenarioMonitor(["h1"], interval=60).start()
    summary = monitor.stop()

    assert not monitor._thread.is_alive()
    assert summary["latency_ms"] == 12.0
    assert summary["queue_size"] == 4
    assert summary["queue_drops"] == 2


def test_normal_stops_monitor_when_iperf_fails(monkeypatch):
    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 1

    class FakeMonitor:
        stopped = False

        def start(self):
            return self

        def stop(self):
            self.stopped = True
            return {
                "latency_ms": None,
                "queue_size": None,
                "queue_drops": None,
            }

    process = FakeProcess()
    monitor = FakeMonitor()
    monkeypatch.setattr(traffic_generator, "run_on_host", lambda *args: process)
    monkeypatch.setattr(
        traffic_generator,
        "ScenarioMonitor",
        lambda *args, **kwargs: monitor,
    )
    monkeypatch.setattr(
        traffic_generator,
        "wait_for_process",
        lambda _: (_ for _ in ()).throw(RuntimeError("iperf failed")),
    )

    with pytest.raises(RuntimeError, match="iperf failed"):
        traffic_generator.normal()

    assert monitor.stopped
    assert process.terminated


def test_normal_stops_monitor_when_process_start_fails(monkeypatch):
    class FakeMonitor:
        stopped = False

        def start(self):
            return self

        def stop(self):
            self.stopped = True
            return {"latency_ms": None, "queue_size": None, "queue_drops": None}

    monitor = FakeMonitor()
    monkeypatch.setattr(
        traffic_generator, "ScenarioMonitor", lambda *args, **kwargs: monitor
    )
    monkeypatch.setattr(
        traffic_generator,
        "run_on_host",
        lambda *args: (_ for _ in ()).throw(RuntimeError("start failed")),
    )

    with pytest.raises(RuntimeError, match="start failed"):
        traffic_generator.normal()

    assert monitor.stopped


def test_congestion_cleans_started_processes_when_flow_start_fails(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    class FakeMonitor:
        stopped = False

        def start(self):
            return self

        def stop(self):
            self.stopped = True
            return {"latency_ms": None, "queue_size": None, "queue_drops": None}

    processes = []
    monitor = FakeMonitor()

    def start_flow(*args):
        if len(processes) == 1:
            raise RuntimeError("second flow failed")
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(traffic_generator, "ScenarioMonitor", lambda *a, **k: monitor)
    monkeypatch.setattr(traffic_generator, "run_on_host", start_flow)

    with pytest.raises(RuntimeError, match="second flow failed"):
        traffic_generator.congestion()

    assert monitor.stopped
    assert len(processes) == 1
    assert processes[0].terminated


def test_congestion_cleans_all_processes_when_wait_fails(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    class FakeMonitor:
        stopped = False

        def start(self):
            return self

        def stop(self):
            self.stopped = True
            return {"latency_ms": None, "queue_size": None, "queue_drops": None}

    processes = []
    monitor = FakeMonitor()
    monkeypatch.setattr(traffic_generator, "ScenarioMonitor", lambda *a, **k: monitor)
    monkeypatch.setattr(
        traffic_generator,
        "run_on_host",
        lambda *args: processes.append(FakeProcess()) or processes[-1],
    )
    monkeypatch.setattr(
        traffic_generator,
        "wait_for_process",
        lambda _: (_ for _ in ()).throw(RuntimeError("wait failed")),
    )

    with pytest.raises(RuntimeError, match="wait failed"):
        traffic_generator.congestion()

    assert monitor.stopped
    assert len(processes) == 4
    assert all(process.terminated for process in processes)