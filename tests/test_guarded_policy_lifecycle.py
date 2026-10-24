from __future__ import annotations

import pytest

from integration import guarded_policy_lifecycle as lifecycle
from qos.allocator import plan_managed_htb
from qos.tc_controller import (
    TCApplyError,
    TCPreconditionError,
    TCState,
)


BASELINE = TCState("baseline qdiscs", "baseline classes", "baseline filters")
MANAGED = TCState("managed qdiscs", "managed classes", "managed filters")
UNKNOWN = TCState("unknown qdiscs", "unknown classes", "unknown filters")


def config():
    return plan_managed_htb("LOW", interface="s1-eth5")


def install_fake_backend(monkeypatch, initial=BASELINE):
    state = {"current": initial, "applies": 0, "restores": 0, "inspections": 0}

    def inspect(interface, runner=None):
        assert interface == "s1-eth5"
        state["inspections"] += 1
        return state["current"]

    def is_baseline(value):
        return value == BASELINE

    def is_managed(value):
        return value == MANAGED

    def managed_matches(value, wanted):
        assert wanted.interface == "s1-eth5"
        return value == MANAGED

    def apply(wanted, runner=None):
        assert wanted.interface == "s1-eth5"
        state["applies"] += 1
        state["current"] = MANAGED
        return ["fake apply"]

    def restore(interface, runner=None):
        assert interface == "s1-eth5"
        state["restores"] += 1
        state["current"] = BASELINE
        return True

    monkeypatch.setattr(lifecycle, "inspect_tc_state", inspect)
    monkeypatch.setattr(lifecycle, "_baseline_matches", is_baseline)
    monkeypatch.setattr(lifecycle, "_managed_htb_owned", is_managed)
    monkeypatch.setattr(lifecycle, "_managed_htb_matches", managed_matches)
    monkeypatch.setattr(lifecycle, "apply_managed_htb_policy", apply)
    monkeypatch.setattr(lifecycle, "restore_managed_htb_hierarchy", restore)
    return state


def live_kwargs(**overrides):
    values = {
        "apply_enabled": True,
        "confirmation": lifecycle.LIVE_CONFIRMATION,
        "measure": lambda: {
            "experiment_id": "offline-test-run",
            "throughput_mbps": 2.5,
        },
    }
    values.update(overrides)
    return values


def test_dry_run_is_default_and_never_inspects_or_mutates_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not inspect or mutate system network state")

    monkeypatch.setattr(lifecycle, "inspect_tc_state", forbidden)
    monkeypatch.setattr(lifecycle, "apply_managed_htb_policy", forbidden)
    monkeypatch.setattr(lifecycle, "restore_managed_htb_hierarchy", forbidden)

    result = lifecycle.run_guarded_policy_lifecycle(config())

    assert result["status"] == "dry_run_planned"
    assert result["dry_run"] is True
    assert result["live_qos_applied"] is False
    assert result["kernel_state_verified"] is False
    assert result["measurement_collected"] is False
    assert result["restoration_verified"] is False
    assert result["generated_commands"]
    assert result["receiver_timing_validation"] == "separate_unvalidated_gate"


def test_live_mode_requires_exact_confirmation_before_any_network_inspection(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("confirmation must be checked before inspection")

    monkeypatch.setattr(lifecycle, "inspect_tc_state", forbidden)

    with pytest.raises(PermissionError, match="requires confirmation"):
        lifecycle.run_guarded_policy_lifecycle(
            config(), apply_enabled=True, measure=lambda: {}
        )


def test_live_mode_requires_measurement_callback_before_network_inspection(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("missing measurement callback must be rejected before inspection")

    monkeypatch.setattr(lifecycle, "inspect_tc_state", forbidden)

    with pytest.raises(ValueError, match="measurement callback"):
        lifecycle.run_guarded_policy_lifecycle(
            config(),
            apply_enabled=True,
            confirmation=lifecycle.LIVE_CONFIRMATION,
        )


def test_live_mode_refuses_unknown_or_nonbaseline_initial_state(monkeypatch):
    state = install_fake_backend(monkeypatch, initial=UNKNOWN)

    with pytest.raises(TCPreconditionError, match="untouched, recognized original baseline"):
        lifecycle.run_guarded_policy_lifecycle(config(), **live_kwargs())

    assert state["applies"] == 0
    assert state["restores"] == 0


def test_happy_path_applies_verifies_measures_and_restores(monkeypatch):
    state = install_fake_backend(monkeypatch)
    measured_in_active_state = []

    def measure():
        measured_in_active_state.append(state["current"] == MANAGED)
        return {"experiment_id": "offline-test-run", "throughput_mbps": 2.5}

    result = lifecycle.run_guarded_policy_lifecycle(
        config(), **live_kwargs(measure=measure)
    )

    assert state["applies"] == 1
    assert state["restores"] == 1
    assert measured_in_active_state == [True]
    assert result["status"] == "completed_baseline_restored"
    assert result["dry_run"] is False
    assert result["apply_attempted"] is True
    assert result["live_qos_applied"] is True
    assert result["kernel_state_verified"] is True
    assert result["measurement_collected"] is True
    assert result["measurement"]["throughput_mbps"] == 2.5
    assert result["restoration_verified"] is True
    assert result["pre_state_sha256"]
    assert result["active_state_sha256"]
    assert result["restored_state_sha256"]


def test_measurement_exception_still_restores_then_reports_failure(monkeypatch):
    state = install_fake_backend(monkeypatch)

    def broken_measurement():
        assert state["current"] == MANAGED
        raise RuntimeError("injected measurement failure")

    with pytest.raises(
        lifecycle.LifecycleSafetyError,
        match="baseline restoration was verified",
    ):
        lifecycle.run_guarded_policy_lifecycle(
            config(), **live_kwargs(measure=broken_measurement)
        )

    assert state["current"] == BASELINE
    assert state["applies"] == 1
    assert state["restores"] == 1


def test_non_json_measurement_value_restores_and_fails_closed(monkeypatch):
    state = install_fake_backend(monkeypatch)

    with pytest.raises(lifecycle.LifecycleSafetyError, match="baseline restoration was verified"):
        lifecycle.run_guarded_policy_lifecycle(
            config(),
            **live_kwargs(measure=lambda: {"throughput_mbps": float("nan")}),
        )

    assert state["current"] == BASELINE
    assert state["restores"] == 1


def test_unrecognized_state_after_measurement_refuses_destructive_restore(monkeypatch):
    state = install_fake_backend(monkeypatch)
    state["restores"] = 0

    def measure():
        # Simulate an external actor replacing the controller-owned tree.
        state["current"] = UNKNOWN
        return {"experiment_id": "offline-test-run"}

    with pytest.raises(
        lifecycle.LifecycleSafetyError,
        match="manual inspection is required",
    ):
        lifecycle.run_guarded_policy_lifecycle(
            config(), **live_kwargs(measure=measure)
        )

    assert state["current"] == UNKNOWN
    assert state["restores"] == 0


def test_apply_failure_that_leaves_baseline_still_reports_failure(monkeypatch):
    state = install_fake_backend(monkeypatch)

    def broken_apply(wanted, runner=None):
        state["applies"] += 1
        raise TCApplyError("injected apply failure; controller rollback already restored baseline")

    monkeypatch.setattr(lifecycle, "apply_managed_htb_policy", broken_apply)

    with pytest.raises(
        lifecycle.LifecycleSafetyError,
        match="baseline restoration was verified",
    ):
        lifecycle.run_guarded_policy_lifecycle(config(), **live_kwargs())

    assert state["current"] == BASELINE
    assert state["applies"] == 1
    assert state["restores"] == 0


def test_measurement_callback_return_must_be_mapping_and_is_restored(monkeypatch):
    state = install_fake_backend(monkeypatch)

    with pytest.raises(lifecycle.LifecycleSafetyError, match="measurement callback must return a mapping"):
        lifecycle.run_guarded_policy_lifecycle(
            config(), **live_kwargs(measure=lambda: ["not", "a", "mapping"])
        )

    assert state["current"] == BASELINE
    assert state["restores"] == 1
