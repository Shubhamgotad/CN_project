"""Opt-in lifecycle for one guarded adaptive QoS trial.

This module is intentionally not wired into the dashboard or experiment runner.
Dry-run planning is the default. Live application requires an explicit caller
flag, a confirmation phrase, a measurement callback, an untouched known
baseline, post-apply kernel-state verification, and verified restoration.

Receiver timing remains a separate gate: a callback supplied here must not be
described as a synchronized, independently validated measurement unless its
provenance has been independently established.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
from typing import Any

from qos.tc_controller import (
    ROOT_INTERFACE,
    ManagedHTBConfig,
    TCApplyError,
    TCPreconditionError,
    TCState,
    _baseline_matches,
    _managed_htb_matches,
    _managed_htb_owned,
    apply_managed_htb_policy,
    generate_managed_htb_commands,
    inspect_tc_state,
    restore_managed_htb_hierarchy,
)

# This is an intentional human confirmation, not a secret or authentication
# mechanism. The caller must separately control who can invoke live execution.
LIVE_CONFIRMATION = "APPLY_ADAPTIVE_POLICY_TO_S1_ETH5"


class LifecycleSafetyError(RuntimeError):
    """The requested lifecycle failed or could not safely restore its baseline."""


def _state_digest(state: TCState) -> str:
    payload = json.dumps(
        {
            "qdiscs": state.qdiscs,
            "classes": state.classes,
            "filters": state.filters,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _restore_and_verify_baseline(
    interface: str,
    runner: Callable[..., Any] | None,
) -> tuple[bool, str]:
    """Restore only a recognized managed policy, then verify the known baseline."""
    current = inspect_tc_state(interface, runner=runner)
    if _baseline_matches(current):
        return False, _state_digest(current)
    if not _managed_htb_owned(current):
        raise TCPreconditionError(
            "refusing lifecycle restoration: current state is neither the known "
            "baseline nor the complete controller-owned managed HTB hierarchy; "
            "manual inspection is required"
        )

    restore_managed_htb_hierarchy(interface, runner=runner)
    restored = inspect_tc_state(interface, runner=runner)
    if not _baseline_matches(restored):
        raise TCApplyError(
            "lifecycle restore ran, but the original HTB baseline was not verified"
        )
    return True, _state_digest(restored)


def run_guarded_policy_lifecycle(
    config: ManagedHTBConfig,
    *,
    apply_enabled: bool = False,
    confirmation: str | None = None,
    measure: Callable[[], Mapping[str, Any]] | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Plan by default; optionally apply, verify, measure, and restore one policy.

    The live path deliberately accepts only the real topology's `s1-eth5`
    target and only when that interface is in the documented original baseline.
    It refuses to replace an already-active managed policy, preserving a narrow,
    auditable first live-integration step. A caller-provided `measure` callback
    runs only after kernel read-back confirms the requested policy is active.

    `runner` exists for offline tests. Production callers should omit it so
    the controller uses subprocess; no dashboard/API wiring is introduced here.
    """
    if not isinstance(config, ManagedHTBConfig):
        raise ValueError("config must be a validated ManagedHTBConfig")
    if (
        config.interface != ROOT_INTERFACE
        or config.isolated_target is not None
    ):
        raise ValueError(
            f"live lifecycle supports only the explicitly configured {ROOT_INTERFACE} target"
        )
    if not isinstance(apply_enabled, bool):
        raise ValueError("apply_enabled must be a boolean")

    commands = generate_managed_htb_commands(config)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "dry_run_planned",
        "dry_run": True,
        "interface": config.interface,
        "requested_allocations_mbps": dict(config.service_allocations_mbps),
        "generated_commands": list(commands),
        "pre_state_sha256": None,
        "active_state_sha256": None,
        "restored_state_sha256": None,
        "apply_attempted": False,
        "live_qos_applied": False,
        "kernel_state_verified": False,
        "measurement_collected": False,
        "measurement": None,
        "restoration_verified": False,
        "receiver_timing_validation": "separate_unvalidated_gate",
    }

    # Dry-run is inert: do not inspect or invoke traffic-control, and do not
    # call the measurement callback.
    if not apply_enabled:
        return report

    if confirmation != LIVE_CONFIRMATION:
        raise PermissionError(
            f"live application requires confirmation={LIVE_CONFIRMATION!r}"
        )
    if not callable(measure):
        raise ValueError("live lifecycle requires a measurement callback")

    before = inspect_tc_state(config.interface, runner=runner)
    if not _baseline_matches(before):
        raise TCPreconditionError(
            "live lifecycle requires the untouched, recognized original baseline; "
            "an existing or unknown hierarchy will not be replaced"
        )

    report["dry_run"] = False
    report["pre_state_sha256"] = _state_digest(before)
    report["apply_attempted"] = True

    operation_error: BaseException | None = None
    try:
        apply_managed_htb_policy(config, runner=runner)
        active = inspect_tc_state(config.interface, runner=runner)
        if not _managed_htb_matches(active, config):
            raise TCApplyError(
                "post-apply kernel read-back does not match the requested managed HTB policy"
            )
        report["active_state_sha256"] = _state_digest(active)
        report["live_qos_applied"] = True
        report["kernel_state_verified"] = True

        measurement = measure()
        if not isinstance(measurement, Mapping):
            raise ValueError("measurement callback must return a mapping")
        # Reject NaN, infinity and non-JSON values before storing experiment data.
        serialized_measurement = json.dumps(
            dict(measurement), allow_nan=False, sort_keys=True
        )
        report["measurement"] = json.loads(serialized_measurement)
        report["measurement_collected"] = True
    except BaseException as exc:
        # Also preserve the restoration attempt on Ctrl+C/SystemExit. Do not
        # let process-level interruptions skip the cleanup block below.
        operation_error = exc

    try:
        restored_now, restored_digest = _restore_and_verify_baseline(
            config.interface, runner
        )
        report["restoration_verified"] = True
        report["restored_state_sha256"] = restored_digest
        report["restoration_action_taken"] = restored_now
    except BaseException as restore_error:
        report["status"] = "restoration_failed"
        report["restoration_verified"] = False
        details = (
            f"policy operation failed with {type(operation_error).__name__}: "
            f"{operation_error}; "
            if operation_error is not None
            else ""
        )
        raise LifecycleSafetyError(
            details
            + "baseline restoration could not be verified; stop further policy "
            "changes and inspect the interface state manually: "
            + f"{type(restore_error).__name__}: {restore_error}"
        ) from restore_error

    if operation_error is not None:
        if isinstance(operation_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            # Cleanup succeeded; preserve the original process-level signal.
            raise operation_error
        report["status"] = "operation_failed_baseline_restored"
        raise LifecycleSafetyError(
            f"policy lifecycle operation failed; baseline restoration was verified "
            f"({type(operation_error).__name__}: {operation_error})"
        ) from operation_error

    report["status"] = "completed_baseline_restored"
    return report
