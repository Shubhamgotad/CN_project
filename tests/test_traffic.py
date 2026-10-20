import json
import pytest

from traffic import traffic_generator


def test_queue_drop_delta_and_counter_reset():
    assert traffic_generator._drop_delta(10, 15) == 5
    assert traffic_generator._drop_delta(10, 3) == 3
    assert traffic_generator._drop_delta(None, 3) is None
    assert traffic_generator._drop_delta(10, None) is None


def test_namespace_selection_requires_exactly_one_matching_process():
    command_lines = {
        "101": b"/bin/bash\0--norc\0-is\0mininet:h1\0",
        "102": b"/bin/bash\0--norc\0-is\0mininet:h2\0",
    }
    reader = lambda pid: command_lines[pid]
    executable_reader = lambda _: b"/bin/bash"
    starttime_reader = lambda pid: f"start-{pid}"

    with pytest.raises(RuntimeError, match="found 0"):
        traffic_generator.resolve_mininet_namespace_pid(
            "h1", [], cmdline_reader=reader,
            executable_reader=executable_reader,
            starttime_reader=starttime_reader,
        )
    assert traffic_generator.resolve_mininet_namespace_pid(
        "h1", ["101"], cmdline_reader=reader,
        executable_reader=executable_reader,
        starttime_reader=starttime_reader,
    ) == ("101", "start-101")
    with pytest.raises(RuntimeError, match="found 2"):
        traffic_generator.resolve_mininet_namespace_pid(
            "h1",
            ["101", "103"],
            cmdline_reader=lambda pid: b"bash\0--norc\0-is\0mininet:h1\0",
            executable_reader=executable_reader,
            starttime_reader=starttime_reader,
        )
    with pytest.raises(RuntimeError, match="found 0"):
        traffic_generator.resolve_mininet_namespace_pid(
            "h1", ["102"], cmdline_reader=reader,
            executable_reader=executable_reader,
            starttime_reader=starttime_reader,
        )
    with pytest.raises(RuntimeError, match="found 0"):
        traffic_generator.resolve_mininet_namespace_pid(
            "h1", ["103"],
            cmdline_reader=lambda _: b"/usr/bin/unrelated\0--tag\0mininet:h1\0",
            executable_reader=lambda _: b"/usr/bin/unrelated",
            starttime_reader=starttime_reader,
        )


def test_host_interface_identity_rejects_namespace_mismatch():
    class Result:
        returncode = 0
        stdout = '[{"ifname":"s1-eth1","linkinfo":{"info_kind":"veth"}}]'

    with pytest.raises(RuntimeError, match="identity mismatch"):
        traffic_generator.validate_host_interface_identity(
            "h1", "s1-eth5", "101",
            runner=lambda *args, **kwargs: Result(),
        )


def test_output_directory_is_required_and_cannot_target_project_data(tmp_path):
    with pytest.raises(ValueError, match="explicit isolated output_directory"):
        traffic_generator._prepare_output_directory(None)
    with pytest.raises(ValueError, match="outside the project worktree"):
        traffic_generator._prepare_output_directory(traffic_generator.DATASET_DIR)
    with pytest.raises(ValueError, match="outside the project worktree"):
        traffic_generator._prepare_output_directory(
            traffic_generator.DATASET_DIR / "nested"
        )
    with pytest.raises(ValueError, match="parent must already exist"):
        traffic_generator._prepare_output_directory(
            tmp_path / "missing-parent" / "run"
        )

    output = traffic_generator._prepare_output_directory(tmp_path / "isolated")
    assert output.is_dir()
    assert output == (tmp_path / "isolated").resolve()
    with pytest.raises(ValueError, match="path components"):
        traffic_generator._isolated_output_file(output, "..\\outside.jsonl")
    with pytest.raises(ValueError, match="new, empty"):
        traffic_generator._prepare_output_directory(output)


def test_output_directory_rejects_parent_symlinks_and_preserves_file(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        traffic_generator._prepare_output_directory(linked_parent / "run")


def test_output_directory_rejects_dangling_parent_symlink(tmp_path):
    dangling_parent = tmp_path / "dangling-parent"
    try:
        dangling_parent.symlink_to(
            tmp_path / "missing-target", target_is_directory=True
        )
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        traffic_generator._prepare_output_directory(dangling_parent / "run")


def test_secure_writer_refuses_preexisting_file_without_modifying_it(tmp_path):
    output = traffic_generator._prepare_output_directory(tmp_path / "run")
    existing = output / "normal.jsonl"
    existing.write_text('{"preserved":true}\n', encoding="utf-8")

    with pytest.raises(FileExistsError, match="not exclusively created"):
        traffic_generator.write_jsonl(
            {"new": True}, existing, secure_run=True
        )
    assert existing.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_secure_writer_rejects_output_symlink_without_modifying_target(tmp_path):
    output = traffic_generator._prepare_output_directory(tmp_path / "run")
    target = tmp_path / "protected.jsonl"
    target.write_text("preserve\n", encoding="utf-8")
    link = output / "normal.jsonl"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(ValueError, match="must not be a symlink"):
        traffic_generator.write_jsonl({"new": True}, link, secure_run=True)
    assert target.read_text(encoding="utf-8") == "preserve\n"


def test_run_on_host_rejects_namespace_ambiguity_before_launch(monkeypatch):
    monkeypatch.setattr(
        traffic_generator.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "101\n102\n", "stderr": ""}
        )(),
    )
    monkeypatch.setattr(
        traffic_generator,
        "resolve_mininet_namespace_pid",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("ambiguous namespace")
        ),
    )
    monkeypatch.setattr(
        traffic_generator.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("must not launch an ambiguous target"),
    )

    with pytest.raises(RuntimeError, match="ambiguous namespace"):
        traffic_generator.run_on_host(
            "h1", ["true"], interface="h1-eth0"
        )


def test_run_on_host_rejects_interface_from_another_namespace(monkeypatch):
    monkeypatch.setattr(
        traffic_generator.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("must reject before process lookup"),
    )
    with pytest.raises(ValueError, match="does not belong to Mininet host h1"):
        traffic_generator.run_on_host(
            "h1", ["true"], interface="h2-eth0"
        )


@pytest.mark.parametrize("change", ["disappeared", "identity", "starttime"])
def test_namespace_process_is_revalidated_before_launch(change):
    calls = []
    current = {"index": 0}

    def read_cmdline(_pid):
        return [
            b"bash\0--norc\0-is\0mininet:h1\0",
            (
                b"" if change == "disappeared"
                else b"bash\0--norc\0-is\0mininet:h2\0"
                if change == "identity"
                else b"bash\0--norc\0-is\0mininet:h1\0"
            ),
        ][current["index"]]

    def read_executable(_pid):
        return b"/bin/bash"

    def read_starttime(_pid):
        value = "start-1" if current["index"] == 0 else (
            "start-2" if change == "starttime" else "start-1"
        )
        current["index"] += 1
        return value

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return "launched"

    with pytest.raises(RuntimeError, match="exited or changed identity"):
        traffic_generator._run_in_verified_namespace(
            "h1",
            ["true"],
            "h1-eth0",
            ["101"],
            cmdline_reader=read_cmdline,
            executable_reader=read_executable,
            starttime_reader=read_starttime,
            interface_validator=lambda *args: None,
            command_launcher=launch,
        )
    assert calls == []


def test_namespace_interface_and_identity_checks_precede_command_launch():
    events = []
    command_calls = []

    def read_cmdline(_pid):
        events.append("cmdline")
        return b"bash\0--norc\0-is\0mininet:h1\0"

    def read_executable(_pid):
        events.append("executable")
        return b"/bin/bash"

    def read_starttime(_pid):
        events.append("starttime")
        return "start-1"

    def validate_interface(*args):
        events.append("interface")

    def launch(argv, **kwargs):
        events.append("launch")
        command_calls.append(argv)
        return "launched"

    result = traffic_generator._run_in_verified_namespace(
        "h1",
        ["true"],
        "h1-eth0",
        ["101"],
        cmdline_reader=read_cmdline,
        executable_reader=read_executable,
        starttime_reader=read_starttime,
        interface_validator=validate_interface,
        command_launcher=launch,
    )

    assert result == "launched"
    assert events.index("interface") < events.index("cmdline", events.index("interface"))
    assert events[-2:] == ["starttime", "launch"]
    assert command_calls == [["mnexec", "-a", "101", "true"]]


def test_namespace_interface_validation_failure_prevents_launch():
    launches = []

    with pytest.raises(RuntimeError, match="interface mismatch"):
        traffic_generator._run_in_verified_namespace(
            "h1",
            ["true"],
            "h1-eth0",
            ["101"],
            cmdline_reader=lambda _: b"bash\0--norc\0-is\0mininet:h1\0",
            executable_reader=lambda _: b"/bin/bash",
            starttime_reader=lambda _: "start-1",
            interface_validator=lambda *args: (_ for _ in ()).throw(
                RuntimeError("interface mismatch")
            ),
            command_launcher=lambda *args, **kwargs: launches.append(args),
        )
    assert launches == []


def test_scenario_requires_isolated_output_before_starting_monitor(monkeypatch):
    monkeypatch.setattr(
        traffic_generator,
        "ScenarioMonitor",
        lambda *args, **kwargs: pytest.fail("monitor must not start"),
    )
    with pytest.raises(ValueError, match="explicit isolated output_directory"):
        traffic_generator.normal()


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


def test_normal_stops_monitor_when_iperf_fails(monkeypatch, tmp_path):
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

    monitor = FakeMonitor()
    monkeypatch.setattr(
        traffic_generator,
        "ScenarioMonitor",
        lambda *args, **kwargs: monitor,
    )
    monkeypatch.setattr(
        traffic_generator,
        "_run_udp_flow",
        lambda *args: (_ for _ in ()).throw(RuntimeError("iperf failed")),
    )

    with pytest.raises(RuntimeError, match="iperf failed"):
        traffic_generator.normal(
            tmp_path / "run"
        )

    assert monitor.stopped


def test_normal_stops_monitor_when_process_start_fails(monkeypatch, tmp_path):
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
        "_run_udp_flow",
        lambda *args: (_ for _ in ()).throw(RuntimeError("start failed")),
    )

    with pytest.raises(RuntimeError, match="start failed"):
        traffic_generator.normal(
            tmp_path / "run"
        )

    assert monitor.stopped


def test_congestion_cleans_started_processes_when_flow_start_fails(monkeypatch, tmp_path):
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
    listeners = []

    def start_listener(port, callback):
        listener = FakeProcess()
        listeners.append((listener, port))
        return listener

    def start_flow(*args, **kwargs):
        if len(processes) == 1:
            raise RuntimeError("second flow failed")
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(traffic_generator, "ScenarioMonitor", lambda *a, **k: monitor)
    monkeypatch.setattr(
        traffic_generator, "start_receiver_listener", start_listener
    )
    monkeypatch.setattr(
        traffic_generator, "stop_receiver_listener", lambda *args: None
    )
    monkeypatch.setattr(traffic_generator, "run_on_host", start_flow)

    with pytest.raises(RuntimeError, match="second flow failed"):
        traffic_generator.congestion(
            tmp_path / "run"
        )

    assert monitor.stopped
    assert len(processes) == 1
    assert processes[0].terminated
    assert len(listeners) == 4


def test_congestion_cleans_all_processes_when_wait_fails(monkeypatch, tmp_path):
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
    listeners = []

    def start_listener(port, callback):
        listener = FakeProcess()
        listeners.append((listener, port))
        return listener

    monkeypatch.setattr(traffic_generator, "ScenarioMonitor", lambda *a, **k: monitor)
    monkeypatch.setattr(
        traffic_generator, "start_receiver_listener", start_listener
    )
    monkeypatch.setattr(
        traffic_generator, "stop_receiver_listener", lambda *args: None
    )
    monkeypatch.setattr(
        traffic_generator,
        "run_on_host",
        lambda *args, **kwargs: processes.append(FakeProcess()) or processes[-1],
    )
    monkeypatch.setattr(
        traffic_generator,
        "wait_for_process",
        lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("wait failed")),
    )

    with pytest.raises(RuntimeError, match="wait failed"):
        traffic_generator.congestion(
            tmp_path / "run"
        )

    assert monitor.stopped
    assert len(processes) == 4
    assert all(process.terminated for process in processes)
    assert len(listeners) == 4


@pytest.mark.parametrize(
    ("argv", "expected_rate"),
    [
        (
            [
                "traffic_generator", "--scenario", "moderate",
                "--output-dir", "C:\\temp\\qos-evidence",
            ],
            6.0,
        ),
        (
            [
                "traffic_generator",
                "--scenario",
                "moderate",
                "--rate-mbps",
                "8.5",
                "--output-dir",
                "C:\\temp\\qos-evidence",
            ],
            8.5,
        ),
    ],
)
def test_moderate_rate_cli_keeps_default_and_accepts_sweep_value(
    monkeypatch, argv, expected_rate
):
    observed = []
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(
        traffic_generator,
        "moderate",
        lambda rate, **kwargs: observed.append(rate),
    )

    traffic_generator.main()

    assert observed == [expected_rate]


@pytest.mark.parametrize("rate_mbps", [0, -1, float("inf"), float("nan")])
def test_moderate_rejects_invalid_rate(rate_mbps):
    with pytest.raises(ValueError, match="positive finite"):
        traffic_generator.moderate(rate_mbps)


def test_moderate_rate_is_used_in_iperf_and_saved_telemetry(monkeypatch, tmp_path):
    class FakeMonitor:
        def start(self):
            return self

        def stop(self):
            return {"latency_ms": 20.0, "queue_size": 0, "queue_drops": 0}

    flow_args = []
    saved = []
    monkeypatch.setattr(
        traffic_generator, "_run_udp_flow",
        lambda *args: flow_args.append(args) or "iperf",
    )
    monkeypatch.setattr(
        traffic_generator,
        "ScenarioMonitor",
        lambda *args, **kwargs: FakeMonitor(),
    )
    monkeypatch.setattr(
        traffic_generator,
        "save_telemetry",
        lambda *args, **kwargs: saved.append(kwargs),
    )

    traffic_generator.moderate(
        8.5,
        output_directory=tmp_path / "run",
    )

    assert flow_args[0][:4] == ("h1", 8.5, 5201, traffic_generator.DURATION)
    assert flow_args[0][4].requested_mbps == 8.5
    assert saved[0]["requested_mbps"] == 8.5


def test_moderate_saves_distinct_experiment_ids_and_appends(tmp_path, monkeypatch):
    output = tmp_path / "moderate.jsonl"
    output_directory = traffic_generator._prepare_output_directory(
        tmp_path / "run"
    )
    iperf_output = (
        "[  7] 0.00-20.00 sec 9.54 MBytes 4.00 Mbits/sec "
        "0.000 ms 0/6906 (0%) receiver"
    )

    for experiment_id in ("trial-7", "trial-7.5"):
        traffic_generator.save_telemetry(
            iperf_output,
            source="h1",
            scenario="moderate",
            experiment_id=experiment_id,
            requested_mbps=7.0 if experiment_id == "trial-7" else 7.5,
            active_flows=1,
            latency_ms=20.0,
            output_directory=output_directory,
        )

    output = output_directory / "moderate.jsonl"
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(records) == 2
    assert [record["experiment_id"] for record in records] == [
        "trial-7",
        "trial-7.5",
    ]
    assert [record["requested_mbps"] for record in records] == [7.0, 7.5]
    assert set(records[0]) == {
        "timestamp",
        "experiment_id",
        "scenario",
        "source",
        "destination",
        "traffic_type",
        "requested_mbps",
        "throughput_mbps",
        "latency_ms",
        "jitter_ms",
        "packet_loss_percent",
        "lost_packets",
        "total_packets",
        "bandwidth_mbps",
        "utilization_percent",
        "active_flows",
        "queue_size",
        "queue_drops",
    }


def test_each_moderate_invocation_generates_a_fresh_experiment_id(monkeypatch, tmp_path):
    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    class FakeMonitor:
        def start(self):
            return self

        def stop(self):
            return {"latency_ms": 20.0, "queue_size": 0, "queue_drops": 0}

    generated_ids = iter(("trial-id-1", "trial-id-2"))
    recorded_ids = []
    monkeypatch.setattr(traffic_generator.uuid, "uuid4", lambda: next(generated_ids))
    monkeypatch.setattr(
        traffic_generator, "ScenarioMonitor", lambda *args, **kwargs: FakeMonitor()
    )
    monkeypatch.setattr(
        traffic_generator,
        "_run_udp_flow",
        lambda *args, **kwargs: "iperf",
    )
    monkeypatch.setattr(
        traffic_generator,
        "save_telemetry",
        lambda *args, **kwargs: recorded_ids.append(kwargs["experiment_id"]),
    )

    traffic_generator.moderate(
        7.0,
        output_directory=tmp_path / "run-1",
    )
    traffic_generator.moderate(
        7.5,
        output_directory=tmp_path / "run-2",
    )

    assert recorded_ids == ["trial-id-1", "trial-id-2"]