import pytest
from fastapi.testclient import TestClient

from dashboard.backend import api
from qos import tc_controller
from qos.allocator import plan_managed_htb
from qos.tc_controller import (
    FLOW_PORTS,
    MANAGED_FILTER_PRIORITIES,
    TCPreconditionError,
    apply_managed_htb_policy,
    apply_tc_policy,
    restore_managed_htb_hierarchy,
    restore_original_hierarchy,
)


client = TestClient(api.app)


def test_live_tc_ownership_is_permanently_fail_closed_by_default():
    assert tc_controller.LIVE_TC_OWNERSHIP_GUARANTEED is False


def test_cooperative_lock_does_not_establish_exclusive_ownership(monkeypatch, tmp_path):
    monkeypatch.setattr(tc_controller.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")

    with tc_controller._interface_lock("s1-eth5"):
        with pytest.raises(TCPreconditionError, match="external tc writers"):
            tc_controller.require_live_tc_ownership()

    assert tc_controller.LIVE_TC_OWNERSHIP_GUARANTEED is False


@pytest.mark.parametrize(
    "operation",
    [
        lambda runner: apply_managed_htb_policy(plan_managed_htb("LOW"), runner),
        lambda runner: apply_tc_policy(
            "s1-eth5",
            10.0,
            {port: 2.25 for port in FLOW_PORTS},
            MANAGED_FILTER_PRIORITIES,
            5204,
            runner,
        ),
        lambda runner: restore_managed_htb_hierarchy("s1-eth5", runner),
        lambda runner: restore_original_hierarchy(
            "s1-eth5",
            MANAGED_FILTER_PRIORITIES,
            5204,
            runner,
        ),
    ],
)
def test_root_interface_mutations_reject_before_runner_invocation(operation):
    calls = []

    def runner(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("the fail-closed gate must reject before tc inspection or mutation")

    with pytest.raises(TCPreconditionError, match="external tc writers"):
        operation(runner)
    assert calls == []


def test_api_rejects_application_even_when_environment_opt_in_is_set(monkeypatch):
    monkeypatch.setattr(api, "_require_local_mutation", lambda _request: None)
    monkeypatch.setattr(tc_controller, "LIVE_TC_OWNERSHIP_GUARANTEED", False)
    monkeypatch.setenv(api.QOS_APPLICATION_ENV, "1")
    monkeypatch.setattr(
        api,
        "latest_telemetry",
        lambda **_kwargs: pytest.fail("ownership rejection must precede telemetry access"),
    )

    response = client.post(
        "/api/qos/apply",
        json={
            "profile": "LOW",
            "measurement_timestamp": "2026-10-20T10:00:00+00:00",
            "authorization": "APPLY_QOS_PROFILE",
        },
    )

    assert response.status_code == 403
    assert "exclusive ownership" in response.json()["detail"]


def test_isolated_status_reports_unavailable_without_claiming_target_ownership():
    response = client.get("/api/qos/isolated/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "unavailable",
        "target_kind": "disposable_namespace_veth",
        "owner_service": "not_provisioned",
        "target_interface": None,
        "mutation_enabled": False,
        "message": (
            "No isolated QoS owner service or disposable target is provisioned. "
            "WSL host-root processes can enter or alter network namespaces, so "
            "exclusive ownership is not established and isolated mutation is disabled."
        ),
    }
