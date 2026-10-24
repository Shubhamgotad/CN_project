from __future__ import annotations

import json
import signal
import subprocess
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.backend import execution_manager, wsl_experiment_supervisor as supervisor


ID = "0123456789abcdef0123456789abcdef"


def test_supervisor_rejects_unsafe_ids_and_output_paths():
    with pytest.raises(ValueError, match="identifier"):
        supervisor._experiment_id("../../unsafe")
    with pytest.raises(ValueError, match="scenario"):
        supervisor.start_experiment(ID, "; touch /tmp/not-allowed")


@pytest.mark.parametrize("scenario,rate,duration", [
    ("normal", 5.0, 17),
    ("moderate", 7.5, 30),
    ("congestion", 5.0, 17),
    ("burst", 8.0, 23),
])
def test_supervisor_preserves_custom_values_and_fixed_burst(scenario, rate, duration):
    resolved_rate, resolved_duration = supervisor._execution_parameters(scenario, rate, duration)
    state = supervisor._scenario_state(ID, scenario, rate, duration)
    assert (resolved_rate, resolved_duration) == (rate, duration)
    assert state["target_rate_mbps"] == rate
    assert state["duration_seconds"] == duration
    assert state["flows"] == (
        ["h1", "h2", "h3", "h4"] if scenario == "congestion" else ["h1"]
    )
    assert state["source_host"] == ("all" if scenario == "congestion" else "h1")


@pytest.mark.parametrize("scenario", ["normal", "moderate", "burst"])
@pytest.mark.parametrize("source_host", ["h1", "h2", "h3", "h4"])
def test_single_flow_scenarios_plan_the_selected_host(scenario, source_host):
    state = supervisor._scenario_state(ID, scenario, source_host=source_host)
    assert state["source_host"] == source_host
    assert state["flows"] == [source_host]


@pytest.mark.parametrize("source_host", ["h1", "h2", "h3", "h4"])
def test_congestion_can_plan_one_selected_host(source_host):
    state = supervisor._scenario_state(ID, "congestion", source_host=source_host)
    assert state["source_host"] == source_host
    assert state["flows"] == [source_host]


def test_all_flows_is_limited_to_congestion():
    assert supervisor._scenario_state(ID, "congestion", source_host="all")["flows"] == [
        "h1", "h2", "h3", "h4",
    ]
    for scenario in ("normal", "moderate", "burst"):
        with pytest.raises(ValueError, match="only for the congestion"):
            supervisor._scenario_state(ID, scenario, source_host="all")
    with pytest.raises(ValueError, match="source host"):
        supervisor._scenario_state(ID, "normal", source_host="10.0.0.1")


def test_version_three_state_recovery_validates_host_and_flow_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "RUN_ROOT", tmp_path)
    run_dir = tmp_path / ID
    run_dir.mkdir()
    state_path = run_dir / "state.json"
    payload = supervisor._scenario_state(ID, "normal", source_host="h3")
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    assert supervisor._read_state(ID)["flows"] == ["h3"]

    payload["flows"] = ["h1"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="state is invalid"):
        supervisor._read_state(ID)


@pytest.mark.parametrize("source_host,port", [
    ("h1", 5201), ("h2", 5202), ("h3", 5203), ("h4", 5204),
])
def test_source_host_is_validated_in_the_actual_producer_command(monkeypatch, tmp_path, source_host, port):
    state, _writes, process = run_worker_with_fake_producer(monkeypatch, tmp_path, exit_code=0)
    state["source_host"] = source_host
    state["flows"] = [source_host]
    captured = {}

    def fake_popen(argv, **_kwargs):
        captured["argv"] = argv
        return process

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    supervisor._worker(ID, "normal", 3.0, 20, source_host)
    argv = captured["argv"]
    assert argv[argv.index("--source-host") + 1] == source_host
    assert argv[argv.index("--experiment-id") + 1] == ID
    assert supervisor.CLIENT_FLOW_PORTS[source_host] == port


def test_preflight_rejects_selected_host_missing_from_verified_topology(monkeypatch):
    monkeypatch.setattr(supervisor, "verify_topology", lambda: {"hosts": ["h1", "server"]})
    with pytest.raises(RuntimeError, match="selected Mininet client"):
        supervisor.start_experiment(ID, "normal", source_host="h3")


@pytest.mark.parametrize("source_host", ["h2", "h3", "h4"])
def test_producer_process_ownership_requires_the_recorded_source_host(
    monkeypatch, tmp_path, source_host
):
    producer = (supervisor.PROJECT_ROOT / "traffic" / "traffic_generator.py").resolve()
    output_dir = tmp_path / "output"
    arguments = [
        supervisor.sys.executable,
        os.fspath(producer),
        "--scenario", "normal",
        "--rate-mbps", "3",
        "--duration-seconds", "20",
        "--source-host", source_host,
        "--experiment-id", ID,
        "--output-dir", os.fspath(output_dir),
    ]
    monkeypatch.setattr(supervisor, "_pid_start_ticks", lambda _pid: 77)
    monkeypatch.setattr(
        supervisor.Path,
        "read_bytes",
        lambda _self: b"\0".join(os.fsencode(argument) for argument in arguments) + b"\0",
    )
    monkeypatch.setattr(supervisor.os, "readlink", lambda _path: "/usr/bin/python3")
    assert supervisor._producer_matches(
        4321, 77, "normal", output_dir, 3.0, 20, source_host, ID
    )
    other_host = "h2" if source_host != "h2" else "h3"
    assert not supervisor._producer_matches(
        4321, 77, "normal", output_dir, 3.0, 20, other_host, ID
    )
    assert not supervisor._producer_matches(
        4321, 77, "normal", output_dir, 3.0, 20, source_host,
        "fedcba9876543210fedcba9876543210",
    )


@pytest.mark.parametrize("scenario,rate,duration", [
    ("normal", 0, 20),
    ("moderate", 7.55, 20),
    ("normal", 10.1, 20),
    ("normal", 4.55, 20),
    ("congestion", 4, 0),
    ("congestion", 4, 61),
    ("burst", 9, 23),
    ("burst", 8, 17),
])
def test_supervisor_rejects_invalid_or_unsupported_custom_values(scenario, rate, duration):
    with pytest.raises(ValueError):
        supervisor._execution_parameters(scenario, rate, duration)


def test_stop_signals_only_the_verified_producer_process_group(monkeypatch, tmp_path):
    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

    process = FakeProcess()
    signals = []
    monkeypatch.setattr(supervisor, "_pid_start_ticks", lambda _pid: 77)
    monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(supervisor.os, "getpgid", lambda _pid: 4321, raising=False)
    monkeypatch.setattr(supervisor, "_process_group_has_live_members", lambda _pgid: False)
    monkeypatch.setattr(supervisor.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)), raising=False)
    assert supervisor._stop_producer(process, 77)
    assert signals == [(4321, signal.SIGINT)]

    signals.clear()
    other_process = FakeProcess()
    assert not supervisor._stop_producer(other_process, 76)
    assert signals == []


def test_stop_never_signals_when_process_identity_does_not_match(monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return self.returncode

    signals = []
    monkeypatch.setattr(supervisor, "_pid_start_ticks", lambda _pid: 99)
    monkeypatch.setattr(supervisor.os, "getpgid", lambda _pid: 4321, raising=False)
    monkeypatch.setattr(supervisor.os, "killpg", lambda *args: signals.append(args), raising=False)
    assert not supervisor._stop_producer(FakeProcess(), 77)
    assert signals == []


def test_stop_returns_failure_when_owned_group_does_not_terminate(monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("producer", timeout)

    signals = []
    monkeypatch.setattr(supervisor, "GRACEFUL_STOP_SECONDS", 0)
    monkeypatch.setattr(supervisor, "FORCED_STOP_SECONDS", 0)
    monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(supervisor, "_pid_start_ticks", lambda _pid: 77)
    monkeypatch.setattr(supervisor.os, "getpgid", lambda _pid: 4321, raising=False)
    monkeypatch.setattr(supervisor, "_process_group_has_live_members", lambda _pgid: True)
    monkeypatch.setattr(supervisor.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)), raising=False)
    assert not supervisor._stop_producer(FakeProcess(), 77)
    assert [pgid for pgid, _signal in signals] == [4321, 4321, 4321]


def run_worker_with_fake_producer(monkeypatch, tmp_path, *, exit_code=None, max_runtime=90):
    run_dir = tmp_path / ID
    run_dir.mkdir()
    current_state = supervisor._scenario_state(ID, "normal")
    current_state.update({"supervisor_pid": os.getpid(), "supervisor_start_ticks": 21})
    writes = []

    class FakeProcess:
        pid = 4321
        returncode = exit_code

        def poll(self):
            return self.returncode

    fake_process = FakeProcess()
    monkeypatch.setattr(supervisor, "_read_state", lambda _id: current_state)
    monkeypatch.setattr(supervisor, "_write_state", lambda _id, value: writes.append(dict(value)))
    monkeypatch.setattr(supervisor, "_run_dir", lambda _id: run_dir)
    monkeypatch.setattr(supervisor, "verify_topology", lambda: {"hosts": ["h1", "h2", "h3", "h4"]})
    monkeypatch.setattr(supervisor, "_pid_start_ticks", lambda _pid: 88)
    monkeypatch.setattr(supervisor, "_process_group_has_live_members", lambda _pgid: False)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.signal, "SIGUSR1", getattr(supervisor.signal, "SIGINT", 2), raising=False)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: fake_process)
    monkeypatch.setattr(supervisor, "MAX_RUNTIME_SECONDS", max_runtime)
    return current_state, writes, fake_process


def test_worker_reports_actual_nonzero_producer_exit(monkeypatch, tmp_path):
    state, writes, _ = run_worker_with_fake_producer(monkeypatch, tmp_path, exit_code=7)
    supervisor._worker(ID, "normal")
    assert state["status"] == "failed"
    assert state["process_exit_code"] == 7
    assert state["process_alive"] is False
    assert writes[-1]["status"] == "failed"
    assert any(row.get("supervisor_ready") is True for row in writes)


def test_supervisor_ready_handshake_rejects_process_exit_and_timeout(monkeypatch):
    class LiveProcess:
        def poll(self):
            return None

    class ExitedProcess:
        def poll(self):
            return 3

    monkeypatch.setattr(supervisor, "_read_state", lambda _id: {"supervisor_ready": True})
    monkeypatch.setattr(supervisor, "_supervisor_matches", lambda _state: True)
    assert supervisor._wait_for_supervisor_ready(ID, LiveProcess(), timeout=0.1)["supervisor_ready"]
    with pytest.raises(RuntimeError, match="exited"):
        supervisor._wait_for_supervisor_ready(ID, ExitedProcess(), timeout=0.1)
    with pytest.raises(TimeoutError, match="did not become ready"):
        supervisor._wait_for_supervisor_ready(ID, LiveProcess(), timeout=0)


def test_worker_enforces_max_runtime_and_verifies_cleanup(monkeypatch, tmp_path):
    state, _writes, process = run_worker_with_fake_producer(monkeypatch, tmp_path, exit_code=None, max_runtime=0)

    def stop_exact(fake_process, _ticks):
        assert fake_process is process
        fake_process.returncode = -signal.SIGINT
        return True

    monkeypatch.setattr(supervisor, "_stop_producer", stop_exact)
    supervisor._worker(ID, "normal")
    assert state["status"] == "failed"
    assert state["process_alive"] is False
    assert "90-second maximum runtime" in state["error"]


def test_worker_passes_custom_parameters_to_existing_producer(monkeypatch, tmp_path):
    state, _writes, _process = run_worker_with_fake_producer(monkeypatch, tmp_path, exit_code=0)
    state.update({
        "scenario": "congestion",
        "source_host": "all",
        "flows": ["h1", "h2", "h3", "h4"],
        "target_rate_mbps": 5.0,
        "duration_seconds": 17,
    })
    captured = {}
    def fake_popen(argv, **_kwargs):
        captured["argv"] = argv
        return _process
    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    supervisor._worker(ID, "congestion", 5.0, 17)
    assert "--scenario" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--rate-mbps") + 1] == "5"
    assert captured["argv"][captured["argv"].index("--duration-seconds") + 1] == "17"
    assert captured["argv"][captured["argv"].index("--source-host") + 1] == "all"
    assert captured["argv"][captured["argv"].index("--experiment-id") + 1] == ID


def test_windows_adapter_uses_fixed_argument_list_and_no_shell(monkeypatch):
    captured = {}
    monkeypatch.setattr(execution_manager.os, "name", "nt")
    monkeypatch.setattr(execution_manager, "_wsl_project_root", lambda: "/mnt/c/project root")

    def run_wsl(arguments, *, timeout):
        captured.update(arguments=arguments, timeout=timeout)
        return {"status": "starting"}

    monkeypatch.setattr(execution_manager, "_run_wsl", run_wsl)
    result = execution_manager.invoke_supervisor("start", ID, "congestion")
    assert result == {"status": "starting"}
    assert captured["arguments"] == [
        "python3", "/mnt/c/project root/dashboard/backend/wsl_experiment_supervisor.py",
        "start", ID, "congestion",
    ]


def test_wsl_qos_apply_runs_topology_guard_and_kernel_verification(monkeypatch):
    from qos import tc_controller

    calls = []
    monkeypatch.setattr(supervisor, "verify_topology", lambda: calls.append("topology"))
    monkeypatch.setattr(
        tc_controller,
        "apply_managed_htb_policy",
        lambda config: calls.append(("apply", config.service_allocations_mbps["gaming"])),
    )
    monkeypatch.setattr(
        tc_controller,
        "verify_managed_htb_policy",
        lambda config: calls.append(("verify", config.interface)),
    )
    result = supervisor.apply_qos_profile("LOW")

    assert result == {
        "status": "applied_verified",
        "profile": "LOW",
        "interface": "s1-eth5",
        "kernel_readback_verified": True,
    }
    assert calls == ["topology", ("apply", 2.7), ("verify", "s1-eth5")]


def test_wsl_qos_apply_rejects_unsupported_profile_before_preflight(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "verify_topology",
        lambda: pytest.fail("invalid profile must be rejected before topology access"),
    )
    with pytest.raises(ValueError, match="Unsupported QoS profile"):
        supervisor.apply_qos_profile("custom")


def test_wsl_qos_apply_distinguishes_failed_readback(monkeypatch):
    from qos import tc_controller

    monkeypatch.setattr(supervisor, "verify_topology", lambda: {"bottleneck_interface": "s1-eth5"})
    monkeypatch.setattr(tc_controller, "apply_managed_htb_policy", lambda _config: [])
    monkeypatch.setattr(
        tc_controller,
        "verify_managed_htb_policy",
        lambda _config: (_ for _ in ()).throw(
            tc_controller.TCVerificationError("readback mismatch")
        ),
    )
    with pytest.raises(supervisor.QosVerificationFailed, match="readback mismatch"):
        supervisor.apply_qos_profile("HIGH")


def test_windows_qos_adapter_uses_a_fixed_profile_argument(monkeypatch):
    captured = {}
    monkeypatch.setattr(execution_manager.os, "name", "nt")
    monkeypatch.setattr(execution_manager, "_wsl_project_root", lambda: "/mnt/c/project root")
    monkeypatch.setattr(
        execution_manager,
        "_run_wsl",
        lambda arguments, *, timeout: captured.update(arguments=arguments, timeout=timeout)
        or {"status": "applied_verified"},
    )

    result = execution_manager.invoke_qos_profile("MEDIUM")

    assert result == {"status": "applied_verified"}
    assert captured["arguments"] == [
        "python3",
        "/mnt/c/project root/dashboard/backend/wsl_experiment_supervisor.py",
        "qos-apply",
        "MEDIUM",
    ]


def test_windows_qos_adapter_rejects_unallowlisted_profile_before_wsl(monkeypatch):
    monkeypatch.setattr(execution_manager.os, "name", "nt")
    monkeypatch.setattr(
        execution_manager,
        "_wsl_project_root",
        lambda: pytest.fail("invalid profile must not reach WSL"),
    )
    with pytest.raises(execution_manager.ExecutionUnavailable, match="Unsupported QoS profile"):
        execution_manager.invoke_qos_profile("custom")


def test_windows_adapter_passes_only_validated_scalar_parameters(monkeypatch):
    captured = {}
    monkeypatch.setattr(execution_manager.os, "name", "nt")
    monkeypatch.setattr(execution_manager, "_wsl_project_root", lambda: "/mnt/c/project")
    monkeypatch.setattr(execution_manager, "_run_wsl", lambda arguments, *, timeout: captured.update(arguments=arguments) or {"status": "starting"})
    execution_manager.invoke_supervisor("start", ID, "congestion", 5.0, 17, "h3")
    assert captured["arguments"][-6:] == ["start", ID, "congestion", "5", "17", "h3"]


@pytest.mark.parametrize("scenario,source_host", [
    ("normal", "h5"),
    ("normal", "10.0.0.1"),
    ("normal", "all"),
    ("congestion", "; touch /tmp/nope"),
])
def test_windows_adapter_rejects_invalid_source_hosts_before_wsl(scenario, source_host, monkeypatch):
    monkeypatch.setattr(execution_manager.os, "name", "nt")
    monkeypatch.setattr(execution_manager, "_wsl_project_root", lambda: pytest.fail("must reject before WSL"))
    with pytest.raises(execution_manager.ExecutionUnavailable):
        execution_manager.invoke_supervisor("start", ID, scenario, 3.0, 20, source_host)


def test_wsl_adapter_calls_subprocess_with_a_fixed_argv_not_a_shell(monkeypatch):
    monkeypatch.setattr(execution_manager.os, "name", "nt")
    monkeypatch.setattr(execution_manager, "_wsl_executable", lambda: "wsl.exe")
    monkeypatch.setattr(execution_manager, "_distribution", lambda: "Ubuntu")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"status":"ready"}', stderr="")

    monkeypatch.setattr(execution_manager.subprocess, "run", run)
    result = execution_manager._run_wsl(["python3", "/fixed/helper.py", "status", ID])
    assert result == {"status": "ready"}
    argv, options = calls[0]
    assert argv == ["wsl.exe", "--distribution", "Ubuntu", "--user", "root", "--exec", "python3", "/fixed/helper.py", "status", ID]
    assert "shell" not in options
    assert options["stdin"] == subprocess.DEVNULL


def test_wsl_timeout_is_reported_as_unavailable_without_raw_command_output(monkeypatch):
    monkeypatch.setattr(execution_manager.os, "name", "nt")
    monkeypatch.setattr(execution_manager, "_wsl_executable", lambda: "wsl.exe")
    monkeypatch.setattr(execution_manager, "_distribution", lambda: "Ubuntu")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("wsl.exe", timeout=15)

    monkeypatch.setattr(execution_manager.subprocess, "run", timeout)
    with pytest.raises(execution_manager.ExecutionUnavailable, match="Could not communicate"):
        execution_manager._run_wsl(["python3", "/fixed/helper.py", "start", ID, "normal"])
