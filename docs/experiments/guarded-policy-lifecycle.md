# Guarded Adaptive QoS Policy Lifecycle

**Status: opt-in implementation prototype; not wired into the dashboard or experiment runner.**

The module `integration/guarded_policy_lifecycle.py` provides a narrow orchestration boundary around the existing managed HTB controller. Its default path is an inert dry-run plan. The live path should not be used for an experiment campaign until the project owner has reviewed and approved the live safeguards and measurement callback.

## What it does

1. Accepts an already validated `ManagedHTBConfig`.
2. In dry-run mode (the default), emits the intended commands and requested allocation. It does not inspect the kernel, run `tc`, or call a measurement callback.
3. To enter the live path, the caller must explicitly provide `apply_enabled=True`, the confirmation phrase `APPLY_ADAPTIVE_POLICY_TO_S1_ETH5`, and a callable measurement callback.
4. Requires the target to be exactly `s1-eth5`, without an isolated-namespace target, and requires the known original HTB baseline before applying.
5. Uses the existing controller apply function, which performs its own ownership preflight, guarded update, kernel read-back, and rollback on a failed application.
6. Checks that the resulting kernel state matches the requested policy before calling the measurement callback.
7. Attempts to restore the known original baseline even when measurement fails. It only requests restoration for a complete controller-owned managed hierarchy; an unknown state is not overwritten. The restored baseline must be verified.
8. Records hashes of the observed pre-apply, active-policy, and restored state strings to help identify the recorded state. These hashes are not a substitute for independently captured raw kernel evidence.

## Example shape

The caller must construct a measurement function appropriate to a controlled test:

```python
result = run_guarded_policy_lifecycle(
    validated_managed_htb_config,
    apply_enabled=True,
    confirmation=LIVE_CONFIRMATION,
    measure=collect_one_controlled_measurement,
)
```

This snippet describes the API only. Do not run it against the current Mininet topology without an explicit review and approved test procedure. The current code does not create or control the traffic generator itself; the caller-supplied callback is responsible for starting, collecting, and stopping the measurement safely.

## Fail-closed conditions

- No live apply unless the caller explicitly opts in and provides the exact confirmation phrase.
- No live apply without a measurement callback.
- Refuse if the initial interface does not match the known original baseline.
- Verify the actual managed HTB state before measuring.
- Attempt baseline restoration after application has started, including after measurement errors.
- If restoration cannot be proven because the interface is in an unknown state, raise an error and require manual inspection rather than issuing destructive cleanup commands.
- A measurement must be JSON-serializable and cannot contain NaN or infinity.
- The `runner` injection point exists for offline tests. Production invocation should omit it.
- This module is not wired into `dashboard/backend/api.py` or `integration/experiment_runner.py`; both existing planning paths remain read-only.

## Limitations and next gates

- The human confirmation phrase is an explicit safety gate, not an authentication mechanism. Access control and operational authorization remain the caller's responsibility.
- This first version requires the untouched original baseline and refuses to replace an existing managed policy. It is not a long-running decision loop and does not handle a prior owner’s policy state.
- Per-apply locking is implemented by the existing controller; this wrapper does not reserve the interface against arbitrary external processes for the whole measurement interval. Run only in a controlled test window. If concurrent or manual traffic-control changes occur, restoration intentionally fails closed on an unrecognized state.
- The module does not itself prove that the measurement is valid or synchronized. Receiver-boundary producer capability, clock identity, uncertainty evidence, RTT/queue timestamp provenance, and multi-flow alignment remain separate validation gates.
- The isolated managed-HTB probe proves controller mechanics on disposable namespaces only. It does not authorize a change on `s1-eth5`.
- No baseline-versus-adaptive QoS improvement has been demonstrated by this wrapper.

## Offline tests

Run:

```bash
python -m pytest tests/test_guarded_policy_lifecycle.py -q
python -m pytest tests/test_integration.py tests/test_managed_htb.py tests/test_tc_controller_safe.py -q
git diff --check
```

These tests use mocked state and must not be described as live Mininet validation.
