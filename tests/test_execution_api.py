from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from dashboard.backend import api
from dashboard.backend.execution_manager import ExecutionNotFound, ExecutionUnavailable, TopologyUnavailable


client = TestClient(api.app, client=("127.0.0.1", 50000))
LOCAL_HEADERS = {"Origin": "http://127.0.0.1:5173", "Host": "127.0.0.1:8000"}
ID = "0123456789abcdef0123456789abcdef"


def state(status="starting", *, experiment_id=ID, scenario="congestion", **updates):
    source_host = updates.get("source_host", "all" if scenario == "congestion" else "h1")
    value = {
        "experiment_id": experiment_id,
        "scenario": scenario,
        "source_host": source_host,
        "status": status,
        "flows": ["h1", "h2", "h3", "h4"] if source_host == "all" else [source_host],
        "target_rate_mbps": 4 if scenario == "congestion" else 3,
        "duration_seconds": 20,
        "created_at": "2026-10-20T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": 0.0,
        "process_alive": status in {"starting", "running", "stopping"},
        "can_stop": status in {"starting", "running"},
        "process_exit_code": None,
        "error": None,
        "supervisor_pid": 1234,
        "output_directory": "/private/path",
    }
    value.update(updates)
    return value


@pytest.fixture(autouse=True)
def mock_supervisor(monkeypatch):
    calls = []
    responses = {}

    def invoke(action, experiment_id=None, scenario=None, rate_mbps=None, duration_seconds=None, source_host=None):
        calls.append((action, experiment_id, scenario, rate_mbps, duration_seconds, source_host))
        if action in responses:
            result = responses[action]
            if isinstance(result, Exception):
                raise result
            return result
        scenario_value = scenario or "congestion"
        return state(
            experiment_id=experiment_id or ID,
            scenario=scenario_value,
            source_host=source_host or ("all" if scenario_value == "congestion" else "h1"),
            target_rate_mbps=rate_mbps if rate_mbps is not None else 4,
            duration_seconds=duration_seconds if duration_seconds is not None else 20,
        )

    monkeypatch.setattr("dashboard.backend.execution_manager.invoke_supervisor", invoke)
    return calls, responses


def test_start_requires_local_origin_and_rejects_invalid_or_extra_parameters(mock_supervisor):
    calls, _ = mock_supervisor
    moderate = client.post("/api/experiments/start", json={"scenario": "moderate"}, headers=LOCAL_HEADERS)
    assert moderate.status_code == 200
    assert moderate.json()["flows"] == ["h1"]
    assert moderate.json()["target_rate_mbps"] == 6
    assert moderate.json()["duration_seconds"] == 20
    calls.clear()
    assert client.post("/api/experiments/start", json={"scenario": "congestion", "output_dir": "C:/unsafe"}, headers=LOCAL_HEADERS).status_code == 422
    assert client.post("/api/experiments/start", json={"scenario": "normal"}).status_code == 403
    assert calls == []


def test_start_topology_unavailable_is_reported_without_launch(monkeypatch):
    monkeypatch.setattr("dashboard.backend.execution_manager.invoke_supervisor", lambda *_args: (_ for _ in ()).throw(TopologyUnavailable("Mininet host h1 is not available.")))
    response = client.post("/api/experiments/start", json={"scenario": "normal"}, headers=LOCAL_HEADERS)
    assert response.status_code == 503
    assert "Mininet host h1" in response.json()["detail"]


def test_selected_host_must_be_available_during_preflight(monkeypatch):
    monkeypatch.setattr(
        "dashboard.backend.execution_manager.invoke_supervisor",
        lambda *_args: (_ for _ in ()).throw(TopologyUnavailable("Mininet client h3 is not available.")),
    )
    response = client.post(
        "/api/experiments/start",
        json={"scenario": "normal", "source_host": "h3"},
        headers=LOCAL_HEADERS,
    )
    assert response.status_code == 503
    assert "Mininet client h3" in response.json()["detail"]


def test_start_returns_server_generated_tracked_id_and_sanitized_state(mock_supervisor):
    calls, _ = mock_supervisor
    response = client.post("/api/experiments/start", json={"scenario": "congestion"}, headers=LOCAL_HEADERS)
    assert response.status_code == 200
    experiment_id = response.json()["experiment_id"]
    assert len(experiment_id) == 32
    assert calls[0][1] == experiment_id
    assert response.json()["status"] == "starting"
    assert response.json()["flows"] == ["h1", "h2", "h3", "h4"]
    assert "supervisor_pid" not in response.json()
    assert "output_directory" not in response.json()
    assert response.json()["source_host"] == "all"
    assert calls == [("start", experiment_id, "congestion", 4.0, 20, "all")]


@pytest.mark.parametrize("scenario,rate,duration", [
    ("normal", 3.0, 20),
    ("moderate", 6.0, 20),
    ("congestion", 4.0, 20),
    ("burst", 8.0, 23),
])
def test_start_scenario_defaults_are_explicit_and_preserved(mock_supervisor, scenario, rate, duration):
    calls, _ = mock_supervisor
    response = client.post("/api/experiments/start", json={"scenario": scenario}, headers=LOCAL_HEADERS)
    assert response.status_code == 200
    assert response.json()["target_rate_mbps"] == rate
    assert response.json()["duration_seconds"] == duration
    assert calls[0][2:] == (scenario, rate, duration, "all" if scenario == "congestion" else "h1")


@pytest.mark.parametrize("scenario,rate,duration,source_host,flows,total", [
    ("normal", 5.0, 17, "h1", ["h1"], 5.0),
    ("moderate", 7.5, 30, "h1", ["h1"], 7.5),
    ("congestion", 5.0, 17, "all", ["h1", "h2", "h3", "h4"], 20.0),
    ("burst", 8.0, 23, "h1", ["h1"], 8.0),
])
def test_start_preserves_confirmed_rate_and_duration(mock_supervisor, scenario, rate, duration, source_host, flows, total):
    calls, _ = mock_supervisor
    response = client.post("/api/experiments/start", json={
        "scenario": scenario, "source_host": source_host, "rate_mbps": rate, "duration_seconds": duration,
    }, headers=LOCAL_HEADERS)
    assert response.status_code == 200
    assert response.json()["flows"] == flows
    assert response.json()["target_rate_mbps"] == rate
    assert response.json()["duration_seconds"] == duration
    assert total == response.json()["target_rate_mbps"] * len(flows)
    assert response.json()["source_host"] == source_host
    assert calls[0][2:] == (scenario, rate, duration, source_host)


@pytest.mark.parametrize("scenario,source_host", [
    ("normal", "h1"), ("normal", "h2"), ("normal", "h3"), ("normal", "h4"),
    ("moderate", "h1"), ("moderate", "h2"), ("moderate", "h3"), ("moderate", "h4"),
    ("burst", "h1"), ("burst", "h2"), ("burst", "h3"), ("burst", "h4"),
    ("congestion", "h1"), ("congestion", "h2"), ("congestion", "h3"), ("congestion", "h4"),
])
def test_start_records_each_selected_source_host(mock_supervisor, scenario, source_host):
    calls, _ = mock_supervisor
    response = client.post("/api/experiments/start", json={
        "scenario": scenario, "source_host": source_host,
    }, headers=LOCAL_HEADERS)
    assert response.status_code == 200
    assert response.json()["source_host"] == source_host
    assert response.json()["flows"] == [source_host]
    assert calls[0][-1] == source_host


@pytest.mark.parametrize("body", [
    {"scenario": "normal", "rate_mbps": 0},
    {"scenario": "normal", "rate_mbps": -1},
    {"scenario": "normal", "rate_mbps": 10.1},
    {"scenario": "normal", "rate_mbps": 5.05},
    {"scenario": "normal", "rate_mbps": "fast"},
    {"scenario": "congestion", "duration_seconds": 0},
    {"scenario": "congestion", "duration_seconds": -1},
    {"scenario": "congestion", "duration_seconds": 61},
    {"scenario": "congestion", "duration_seconds": 1.5},
    {"scenario": "burst", "rate_mbps": 9},
    {"scenario": "burst", "duration_seconds": 17},
    {"scenario": "normal", "source_host": "all"},
    {"scenario": "moderate", "source_host": "all"},
    {"scenario": "burst", "source_host": "all"},
    {"scenario": "normal", "source_host": "h5"},
    {"scenario": "normal", "source_host": "10.0.0.1"},
    {"scenario": "normal", "source_host": "; touch /tmp/nope"},
])
def test_start_rejects_invalid_custom_parameters_before_supervisor(mock_supervisor, body):
    response = client.post("/api/experiments/start", json=body, headers=LOCAL_HEADERS)
    assert response.status_code == 422
    assert mock_supervisor[0] == []


def test_start_rate_step_validation_returns_fastapi_field_location_and_message(mock_supervisor):
    response = client.post("/api/experiments/start", json={
        "scenario": "normal", "rate_mbps": 5.05, "duration_seconds": 20,
    }, headers=LOCAL_HEADERS)
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["body"]
    assert "Rate must use 0.1 Mbps increments." in error["msg"]
    assert mock_supervisor[0] == []


def test_duplicate_start_is_rejected_as_conflict(mock_supervisor):
    calls, responses = mock_supervisor
    responses["start"] = ExecutionUnavailable("Another guarded traffic experiment is already active.")
    response = client.post("/api/experiments/start", json={"scenario": "normal"}, headers=LOCAL_HEADERS)
    assert response.status_code == 409
    assert "already active" in response.json()["detail"]
    assert calls[0][0] == "start"


def test_stop_uses_only_the_requested_experiment_id_and_is_idempotent(mock_supervisor):
    calls, responses = mock_supervisor
    responses["stop"] = state("stopped", process_alive=False, can_stop=False, finished_at="2026-10-20T12:00:01+00:00")
    response = client.post(f"/api/experiments/{ID}/stop", json={}, headers=LOCAL_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert calls == [("stop", ID, None, None, None, None)]
    repeated = client.post(f"/api/experiments/{ID}/stop", json={}, headers=LOCAL_HEADERS)
    assert repeated.status_code == 200


def test_stopping_an_already_completed_experiment_preserves_completed_state(mock_supervisor):
    _calls, responses = mock_supervisor
    responses["stop"] = state("completed", process_alive=False, can_stop=False, process_exit_code=0)
    response = client.post(f"/api/experiments/{ID}/stop", json={}, headers=LOCAL_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["process_exit_code"] == 0


def test_stop_rejects_invalid_and_unknown_experiment_ids(mock_supervisor):
    calls, responses = mock_supervisor
    bad = client.post("/api/experiments/not-an-id/stop", json={}, headers=LOCAL_HEADERS)
    assert bad.status_code == 404
    responses["stop"] = ExecutionNotFound("Experiment was not found.")
    unknown = client.post(f"/api/experiments/{'f' * 32}/stop", json={}, headers=LOCAL_HEADERS)
    assert unknown.status_code == 404
    assert calls == [("stop", "f" * 32, None, None, None, None)]


def test_status_reports_failure_and_validates_identifiers(mock_supervisor):
    calls, responses = mock_supervisor
    responses["status"] = state("failed", process_alive=False, can_stop=False, error="Producer exited with code 2.")
    response = client.get(f"/api/execution/{ID}")
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "Producer exited with code 2."
    assert client.get("/api/execution/../../bad").status_code in {404, 422}
    assert calls[0] == ("status", ID, None, None, None, None)


def test_active_status_returns_null_when_no_guarded_experiment_exists(mock_supervisor):
    calls, responses = mock_supervisor
    responses["active"] = None
    response = client.get("/api/execution/active")
    assert response.status_code == 200
    assert response.json() is None
    assert calls == [("active", None, None, None, None, None)]


def test_execution_mutations_reject_non_local_host(mock_supervisor):
    response = client.post("/api/experiments/start", json={"scenario": "normal"}, headers={"Origin": "http://127.0.0.1:5173", "Host": "192.168.1.3:8000"})
    assert response.status_code == 403
    assert mock_supervisor[0] == []


def test_execution_mutations_reject_non_loopback_clients(mock_supervisor):
    remote_client = TestClient(api.app, client=("192.168.1.9", 50000))
    response = remote_client.post("/api/experiments/start", json={"scenario": "normal"}, headers=LOCAL_HEADERS)
    assert response.status_code == 403
    assert mock_supervisor[0] == []


def test_read_only_telemetry_still_has_no_post_operation():
    assert client.post("/api/telemetry/latest", headers=LOCAL_HEADERS).status_code == 405
