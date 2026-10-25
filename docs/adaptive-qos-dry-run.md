# Adaptive QoS decision and application

`GET /api/qos/decision` remains read-only. It validates fresh telemetry, runs
the existing rule-based classifier, applies the existing hysteresis and
cooldown rules, and plans one of the managed HTB profiles for `s1-eth5`.
Model predictions in imported offline reports are separate and are not used by
this path.

QoS application is unavailable while `LIVE_TC_OWNERSHIP_GUARANTEED` is
hard-coded false in `qos/tc_controller.py`. Setting
`ADAPTIVE_QOS_ENABLE_APPLICATION=1` does not bypass this ownership gate: the
decision response continues to report `application_enabled: false`, the API
rejects application before telemetry or WSL access, and controller mutation
and restoration entry points reject before inspecting or changing the root
interface. There is no environment, API, or UI override.

Application delegates through the existing Windows-to-WSL adapter. The WSL
supervisor repeats its topology/interface preflight and calls the existing
managed HTB controller, which accepts only the known baseline or the exact
controller-owned hierarchy, serializes cooperating controller calls, and
performs guarded rollback. A second kernel readback must match the requested
profile before the API returns `applied_verified`. The status endpoint
identifies a currently applied profile only when the managed hierarchy matches
by kernel readback; otherwise it reports an unrecognized or unavailable state.

The controller's file lock is cooperative: it does not prevent an unrelated
process with `CAP_NET_ADMIN` (or host root) from changing `tc` state. The
current architecture does not establish exclusive ownership, so real
application remains fail-closed. Existing mocked controller tests verify
command planning, readback, and rollback mechanics only; they do not authorize
mutations of the Mininet interface. A readback would verify configuration, not
QoS improvement or measurement alignment.

Offline tests mock the WSL and `tc` boundaries. They do not start Mininet,
change OVS or `tc` state, launch traffic, or establish live integration.

## Ownership architecture and migration assessment

The active WSL topology is `h1`-`h4` to `s1`, a 10 Mbps `TCLink` from `s1`
to `s2`, then `server` from `s2`. The only inter-switch link is
`s1-eth5` <-> `s2-eth1`; `s1-eth5` is not a spare enforcement interface. The
managed profiles preserve the existing 10 Mbps aggregate, UDP destination
ports 5201-5204, their Gaming/Video/Web/Bulk class mapping, the 1 Mbps
unmatched class, and the 10 ms netem leaves.

The WSL runtime inspection for this assessment found:

- Mininet (`mn`, observed PID 630) runs as root in the initial WSL network
  namespace and has effective and bounding capability masks that include
  `CAP_NET_ADMIN`. It owns the Mininet CLI, can run commands against `s1-eth5`,
  and creates/configures the topology.
- The root Mininet switch shells (`s1` and `s2`, observed PIDs 651 and 654)
  are also in the initial network namespace with `CAP_NET_ADMIN`.
- OVS (`ovsdb-server`, PID 153; `ovs-vswitchd`, PID 246) runs as root in that
  same namespace with `CAP_NET_ADMIN`. OVS is a topology/control-plane writer;
  it is not the sole `tc` owner.
- The host shells for h1-h4 and server run as root with `CAP_NET_ADMIN` in
  their individual host network namespaces. They can change their own host
  interface qdiscs, not the `s1-eth5` device while it remains in the initial
  namespace.
- The Windows-hosted API's WSL adapter starts its supervisor using
  `wsl.exe --user root`; the supervisor invokes the controller and `tc` in the
  initial WSL namespace. Any other WSL-root process with the relevant
  capability is likewise a potential writer.

Linux network namespaces alone do not establish exclusive ownership against
the current WSL-root workload: Mininet, OVS, and the supervisor share the
initial user/network namespace and have `CAP_NET_ADMIN`. WSL did permit a
transient nested user+network namespace to be created, but that fact alone
does not prove that an outside privileged process cannot enter it, access its
interfaces, or change its qdisc. No production enforcement service exists,
and no namespace-entry-denial or external-writer test has been demonstrated.
Accordingly, `LIVE_TC_OWNERSHIP_GUARANTEED` remains false. Do not promote it
based on a Python lock, a process-local lease, interface naming, a readback,
or the existing disposable namespace probe.

### Candidate architecture (not enabled)

The practical candidate is a single QoS enforcement service in a dedicated
network namespace owned by a separately delegated user namespace. The service
would be the only process granted `CAP_NET_ADMIN` in that owner namespace and
the only holder of its namespace/interface handles. The dashboard and
supervisor would become authenticated local-socket clients; they would not
run `tc` directly. The service would expose only validated profile IDs and
the fixed target, serialize apply operations, retain the target namespace
inode and interface ifindex, and check those identities before every
operation and kernel readback.

For the data path, a two-port transparent bridge in the enforcement namespace
would sit between s1 and s2. Its s1-facing and s2-facing veth peers would be
the only inter-switch OVS ports. The service-owned egress target toward s2
would carry the existing managed HTB profile. The reverse egress would retain
the existing 10 Mbps/10 ms link behavior. The direct s1-s2 path must not remain
active as a bypass or create a forwarding loop. Host links, OVS management,
and the Mininet control process remain in place; only the inter-switch data
path is changed at an explicitly approved maintenance cutover.

The service lifecycle required before any live gate could be considered is:

1. `STOPPED`: no owner proof; all apply and restore requests reject.
2. `ACQUIRING`: create or attach to the separately owned user/network
   namespaces; positively validate namespace ownership, namespace inode,
   interface ifindex, veth peer identities, OVS ports, and the single
   enforcement path. Any ambiguity returns to fail-closed `STOPPED`.
3. `OWNED`: continuously verify the service process identity and namespace
   ownership. A separate initial-user-namespace process with
   `CAP_NET_ADMIN` must be unable to enter the owner namespace or alter its
   target. This must be demonstrated with a second-process test on a disposable
   target, not inferred from configuration.
4. `APPLYING`: accept only LOW/MEDIUM/HIGH from the existing allocator, use
   fixed interface names and argument-list subprocess calls, and record every
   command result. On failure, restore only a snapshot still matching the
   service's last observed state; otherwise report rollback failure and stop
   further mutations.
5. `VERIFIED`: compare qdisc, HTB classes/rates/ceilings/default, UDP filters,
   and netem leaves from kernel readback to the selected existing profile.
   Only this state may report success.
6. `LOST`: on service exit, identity change, namespace/interface replacement,
   ownership test failure, or unexpected kernel change, reject all new
   mutations. Do not attempt rollback from a process whose ownership proof was
   lost; report the last state as unknown until a new owner performs a fresh,
   read-only reconciliation.

The user-namespace isolation is a candidate trust boundary, not yet a proven
guarantee in this WSL installation. WSL root can still terminate or interfere
with processes, so the threat model and setns/capability behavior must be
reviewed and tested. If a second privileged process can enter the owner
namespace, the service architecture is insufficient; use a separate VM or
other enforceable administrative boundary instead.

### Non-destructive migration plan (future approval required)

No migration step below has been performed.

1. Schedule a maintenance window, stop traffic producers, and require the
   current kernel readback to confirm the existing LOW state on `s1-eth5`.
   Save read-only OVS port, qdisc, class, filter, interface, and namespace
   snapshots. Do not first change the current LOW hierarchy.
2. On a disposable, disconnected veth pair, build the delegated
   user+network-namespace owner and enforcement service. Test both owner
   acquisition and loss. From a separate process in the initial WSL user
   namespace, attempt namespace entry and a harmless rejected mutation; prove
   that it cannot acquire the owner's network-admin authority. Verify the
   owner can apply/read back all three existing profiles, and inject command,
   timeout, ownership-loss, rollback, and readback-mismatch failures. Delete
   only that disposable test target after identity-checked cleanup.
3. In a shadow Mininet topology, create the two-veth transit bridge and
   demonstrate the expected host-to-server path, port 5201-5204 classification,
   LOW-profile readback, 10 Mbps aggregate, and 10 ms behavior in both
   directions. Confirm management/control connectivity does not use the
   inter-switch data path and that no alternate OVS path bypasses enforcement.
4. After review of those results, build the production enforcement path beside
   the current one without attaching both as active parallel L2 paths. Install
   and read back LOW on the new target first. At the approved cutover, disable
   the old direct s1/s2 OVS ports, enable the new bridge path, then verify the
   OVS port graph, target identities, kernel hierarchy, and control
   connectivity. This brief data-plane interruption is required to avoid a
   forwarding loop or bypass; the existing LOW qdisc remains untouched on the
   old link for rollback.
5. If any check fails, disable the new OVS path and re-enable the old
   s1-eth5/s2-eth1 path. Confirm that the original LOW qdisc remains intact by
   readback. Do not use `tc` rollback on the old interface. If service
   ownership is lost, fail closed and restore connectivity through the old
   path rather than mutating an unowned qdisc.
6. Only after an independent review confirms all ownership, external-writer,
   failure-injection, path, and readback evidence may a separate change
   introduce a narrowly scoped ownership proof for the service target. The
   current fixed-false constant must not be toggled as part of this assessment.

The controller already has allowlisted profile planning, target validation,
rollback guards, and readback comparison, and its tests cover mocked failures
and mismatches. Those mechanisms are not an ownership lifecycle. The new
service and its owner-acquisition/loss and external-writer tests are
prerequisites, not functionality that this repository currently provides.

## Phase 2 isolation status and future validation

The current WSL check was read-only: `wsl.exe --distribution Ubuntu --user
root --exec id` returned UID 0, and `ip netns list` reported no registered
namespaces. No disposable target or long-running owner service is provisioned.
The Windows API's existing adapter also invokes the supervisor as WSL root.
This does not isolate that service from another WSL-root process. A root
process can potentially enter a network namespace or alter its interfaces;
the proposed service therefore cannot claim exclusive ownership in the
current WSL administrative domain.

No isolated QoS enforcement service is implemented or enabled. The API
`GET /api/qos/isolated/status` endpoint reports `unavailable`, no owner service,
no target interface, and `mutation_enabled: false`. The AI & QoS page presents
this independently from production kernel status. It deliberately has no
isolated Apply action. The production `LIVE_TC_OWNERSHIP_GUARANTEED` gate
remains false and has not been changed.

### Isolated-target validation procedure (future operator approval required)

The existing `telemetry/probe_managed_htb_netns.py` script is a disposable
controller probe, not a persistent enforcement service and not an ownership
proof against host root. It creates two fresh network namespaces and a veth
pair, applies each managed profile to the disposable endpoint, sends test UDP
packets within that disposable pair, verifies kernel readback and rollback,
checks that `s1-eth5` readback is unchanged, and attempts identity-checked
cleanup. Do not run it during implementation or on a production WSL instance
where root-level interference is not in the accepted threat model.

For a future explicitly approved controller probe:

1. Use a disposable lab with Mininet already present for the script's
   read-only `s1-eth5` before/after snapshot. Do not start/restart Mininet or
   OVS as part of this procedure. Confirm the existing LOW state and save
   read-only qdisc, class, filter, and OVS-port snapshots before proceeding.
2. Confirm the requested output directory does not exist. From the project
   root in WSL, run as root:
   `python3 telemetry/probe_managed_htb_netns.py "/tmp/cn-adaptive-qos-isolated-probe-$(date +%Y%m%d-%H%M%S)"`
   This is a network-mutating probe command and requires explicit approval.
   It must not be substituted for the future service's operator-confirmed
   profile-apply workflow.
3. Accept the probe only when its `result.json` reports `status: passed`,
   `demo_tc_unchanged: true`, `cleanup.verified: true`, all logged `tc`
   operations are scoped to the generated `qosna...` namespace and interface,
   and the LOW/MEDIUM/HIGH kernel snapshots match the existing allocator.
   The probe also verifies UDP destination ports 5201-5204 map to the existing
   managed classes and that a failed install restores the disposable baseline.
4. If the probe reports a cleanup failure, do not remove namespaces or links
   by name and do not try rollback from an uncertain owner. Preserve the
   evidence, inspect namespace inodes and both veth ifindices read-only, then
   require an operator-reviewed identity-checked cleanup. Keep all live
   application disabled.
5. Treat a successful probe only as controller behavior evidence. It does not
   establish that a second privileged process cannot enter the target, does
   not demonstrate a persistent service lifecycle, and does not enable the
   UI or production application path.

Before a persistent service or isolated Apply control can be implemented, the
deployment must provide a separately enforceable administrative boundary
(for example a dedicated VM/service identity with no peer process holding
equivalent network-administration privilege), an authenticated narrow request
channel, owner acquisition/loss/restart behavior, concurrent-request
protection, and independent tests against both unprivileged and privileged
clients. If the deployment remains a shared WSL-root domain, keep isolated
mutation disabled; a cooperative lock or namespace identity check is not a
substitute for that boundary.
