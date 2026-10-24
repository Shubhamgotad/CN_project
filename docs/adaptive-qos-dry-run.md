# Adaptive QoS decision and application

`GET /api/qos/decision` remains read-only. It validates fresh telemetry, runs
the existing rule-based classifier, applies the existing hysteresis and
cooldown rules, and plans one of the managed HTB profiles for `s1-eth5`.
Model predictions in imported offline reports are separate and are not used by
this path.

QoS changes are disabled by default. To make the explicit local application
action available in a trusted lab, set `ADAPTIVE_QOS_ENABLE_APPLICATION=1` for
the dashboard API process (in PowerShell: `$env:ADAPTIVE_QOS_ENABLE_APPLICATION = "1"`).
Leave it unset to retain dry-run-only behavior. The AI & QoS page still requires a separate
authorization checkbox and confirmation button. The POST endpoint accepts
only the selected profile and exact telemetry timestamp; it rejects missing
authorization, stale or changed telemetry, pending profile stabilization,
unsupported profiles, and unsupported interfaces. The request cannot provide
shell commands or arbitrary interface names.

Application delegates through the existing Windows-to-WSL adapter. The WSL
supervisor repeats its topology/interface preflight and calls the existing
managed HTB controller, which accepts only the known baseline or the exact
controller-owned hierarchy, serializes cooperating controller calls, and
performs guarded rollback. A second kernel readback must match the requested
profile before the API returns `applied_verified`. The status endpoint
identifies a currently applied profile only when the managed hierarchy matches
by kernel readback; otherwise it reports an unrecognized or unavailable state.

The controller's file lock is cooperative: it does not prevent an unrelated
process with `CAP_NET_ADMIN` (or host root) from changing `tc` state. Therefore
the opt-in is intended only for a trusted, single-operator lab where other
traffic-control writers are excluded by the operator. The current architecture
does not establish that exclusion, and no stronger ownership guarantee is
claimed. A readback verifies configuration, not QoS improvement or measurement
alignment.

Offline tests mock the WSL and `tc` boundaries. They do not start Mininet,
change OVS or `tc` state, launch traffic, or establish live integration.
