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

The existing `probe_managed_htb_netns.py` is the isolated controller-only
probe. Its path is only
`qosna...:qosp...` to `qosnb...:qosq...`; it does not create OVS bridges or
connect to Mininet. `network/topology/basic_topology.py` still has the direct
`s1`-`s2` link, so it is not a shadow enforcement topology and must not be
modified or used as one for this validation. The automated tests exercise
target identity acquisition and loss using fake command observations; they do
not invoke Linux networking commands or prove exclusion of a second privileged
process.

#### Disposable controller probe

For an explicitly approved controller probe:

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

#### OVS-connected shadow-path probe

`telemetry/probe_ovs_shadow_path.py` builds a separate three-namespace path:
client namespace -> left OVS bridge -> routed owner namespace -> right OVS
bridge -> server namespace. The controller target is only the generated
`qosp<token>` egress inside the generated owner namespace. Unique generated
names, namespace/interface/peer identity checks, exact OVS ports, secure
OpenFlow rules, explicit operator approval, and a dedicated OVS database and
management-socket directory are required. The probe refuses any OVS database
that already has bridges. It never addresses `s1-eth5` or `s2-eth1`.

The probe is still **not an ownership service or ownership proof**. A same-VM
root process can generally inspect or mutate a network namespace. The probe's
result deliberately reports `ownership_proof.status: not_proven`; its successful
status means only that the packet path/controller/cleanup checks passed.
Provision the VM and its private OVS database/vswitchd before running. The OVS
management sockets for each bridge must be in a runtime directory dedicated to
that OVS instance; the script uses those exact sockets for `ovs-ofctl`, rather
than falling back to the default system bridge-management sockets.

Prerequisites:

- A disposable Linux VM or isolated lab with root access, `iproute2`, OVS
  (`ovs-vsctl`, `ovs-ofctl`, a matching `ovsdb-server` and `ovs-vswitchd`),
  Python 3, `ping` not required, and the project's Python dependencies.
- Explicit operator confirmation that this VM and its OVS instance may have
  temporary namespaces, veth devices, bridges, routes, OpenFlow rules, and
  `tc` state created. Do not use the active Mininet/production VM.
- A private OVS database socket and its paired vswitchd bridge-management
  socket directory. The database must list zero bridges before the probe.
  Management sockets must be created by the vswitchd connected to that same
  database. If the installation cannot provide a dedicated pair, stop.
- Operator confirmation that this OVS database and vswitchd are separate from
  every active Mininet/system OVS instance. The approval's
  `dedicated_ovs_database` field is an operator attestation, not a fact the
  script can independently prove. The script checks the approved database
  socket, its `Open_vSwitch` UUID, an empty bridge set, and a management
  directory with no pre-existing `*.mgmt` sockets. A matching UUID does not
  prove that the daemon has no other clients or that the VM is disposable.
- An approval JSON file outside the repository, mode `0600` or stricter,
  containing the operator attestation, OVS database UUID, and exact socket
  paths. Obtain the UUID read-only from the already initialized dedicated OVS
  database:

```sh
mkdir -m 700 -p /tmp/cn-adaptive-qos-shadow
OVS_DB=/run/cn-adaptive-qos-shadow/db.sock
OVS_RUNTIME=/run/cn-adaptive-qos-shadow
OVS_UUID="$(ovs-vsctl --db="unix:$OVS_DB" --timeout=3 get Open_vSwitch . _uuid)"
printf '%s\n' \
  "{\"version\":1,\"target_kind\":\"disposable_linux_vm\",\"target_id\":\"approved-shadow-vm-01\",\"allow_network_mutations\":true,\"dedicated_ovs_database\":true,\"ovs_instance_uuid\":\"$OVS_UUID\",\"ovs_db_socket\":\"$OVS_DB\",\"ovs_ofctl_dir\":\"$OVS_RUNTIME\"}" \
  > /tmp/cn-adaptive-qos-shadow/approval.json
chmod 600 /tmp/cn-adaptive-qos-shadow/approval.json
```

`ovs-vsctl get Open_vSwitch . _uuid` must succeed and return the identity
captured in the approval file. The VM owner must independently confirm that
the database socket and vswitchd management sockets belong only to this
disposable target. First check the sockets and ensure the database is empty.
These are read-only checks:

```sh
test -S "$OVS_DB"
test -d "$OVS_RUNTIME"
ovs-vsctl --db="unix:$OVS_DB" --timeout=3 show
ovs-vsctl --db="unix:$OVS_DB" --timeout=3 list-br
ovs-vsctl --db="unix:$OVS_DB" --timeout=3 get Open_vSwitch . _uuid
```

`list-br` must return no bridge names. Do not use the system/default OVS DB
socket. The runtime directory must be dedicated to the matching vswitchd and
contain no unrelated bridge-management sockets. Keep `$OVS_DB`, `$OVS_RUNTIME`,
and `$OVS_UUID` in the shell for the following commands.

#### Read-only preflight

From the repository root in the disposable Linux VM, define an unused,
out-of-repository evidence path and explicitly opt in. Preflight requires root
and the same approval file, socket paths, UUID, environment variable, and CLI
approval flag as the mutation run, but it creates no evidence directory,
namespace, interface, bridge, flow, route, or qdisc:

```sh
export ADAPTIVE_QOS_SHADOW_APPROVAL=I_APPROVE_DISPOSABLE_VM_NETWORK_CHANGES
EVIDENCE="/tmp/cn-adaptive-qos-shadow/evidence-$(date +%Y%m%d-%H%M%S)"
sudo -E python3 telemetry/probe_ovs_shadow_path.py "$EVIDENCE" \
  --approval-file /tmp/cn-adaptive-qos-shadow/approval.json \
  --ovs-db "$OVS_DB" \
  --ovs-ofctl-dir "$OVS_RUNTIME" \
  --i-approve-shadow-only-network-mutations \
  --preflight-only
```

Require exit code 0 and inspect the JSON target manifest. It names generated
namespaces, bridges, and interfaces, but kernel namespace inodes and interface
indices are deliberately unavailable until resources are created. Any
preflight error is a stop condition; do not proceed by relaxing a check.

#### Explicitly approved packet-path/controller probe

The full probe is privileged and network-mutating. Only after reviewing the
preflight output and explicitly approving the disposable target, run the same
command without `--preflight-only`:

```sh
sudo -E python3 telemetry/probe_ovs_shadow_path.py "$EVIDENCE" \
  --approval-file /tmp/cn-adaptive-qos-shadow/approval.json \
  --ovs-db "$OVS_DB" \
  --ovs-ofctl-dir "$OVS_RUNTIME" \
  --i-approve-shadow-only-network-mutations
```

The script refuses non-Linux/non-root execution, missing opt-in, invalid or
weakly permissioned approval files, an output path in the repository or an
existing output path, unapproved/mismatched OVS socket or database UUID,
missing dedicated-database attestation, an unavailable management directory,
pre-existing management sockets, an OVS database with any bridge, generated
name collisions, unexpected OVS ports/flows, interface identity changes, or
a production interface reference. It creates no production-like topology.
Network changes are limited to generated disposable namespaces, veths, two
OVS bridges in the approved empty database, static forwarding flows, and
managed HTB state on the generated owner egress.

The evidence bundle includes `commands.jsonl`, receiver output, target
namespace/interface identities, OVS bridge/port/interface data, flow dumps,
port counters, per-profile kernel readback, and rollback/cleanup status. The
probe sends forty UDP datagrams on each of ports 5201-5204 for each profile.
It requires exact bridge port sets and OpenFlow rules, positive packet-counter
deltas on every expected path edge, exact receipt at the server, matching
LOW/MEDIUM/HIGH allocator readback, and restoration to the disposable baseline.
The injected installation failure must also restore that baseline. Its final
exit status explicitly says ownership remains unproven.

#### Operator evidence checklist

Review the generated `result.json` and `commands.jsonl` before declaring the
path/controller test successful:

1. Confirm the OVS graph has exactly two generated bridges and the expected
   two ports per bridge; review `ovs_before_traffic` and each profile's
   `ovs_before`/`ovs_after` interface and flow dumps. Reject any extra port,
   flow, changed peer, production interface, or ambiguous attachment.
2. Confirm the profile section records exactly ten received datagrams on each
   UDP port 5201-5204, and that each expected OVS RX/TX port counter has a
   positive delta. The script captures counters both before and after each
   packet roundtrip and fails if a required delta is absent.
3. Confirm LOW, MEDIUM, and HIGH each have kernel readback matching the
   authoritative allocator profile; do not infer a profile from command logs
   alone.
4. Confirm the injected failed installation reports rollback restored and
   verified the disposable baseline, then confirm `cleanup.verified` is true.
   Verify that all logged `tc` commands are scoped to the generated owner
   namespace and `qosp...` interface.
5. Preserve the complete output directory as evidence. If any step fails,
   identity changes, or cleanup is uncertain, stop. Do not remove resources
   by name or rerun cleanup blindly; preserve the evidence and request
   operator-reviewed identity-scoped recovery.

If the probe fails or cleanup is not verified, do not delete namespaces,
interfaces, or bridges by name. Preserve `result.json` and `commands.jsonl`,
inspect the recorded identities and live OVS graph read-only, and require
operator-reviewed identity-scoped cleanup. A successful controller/path probe
is not permission to enable live QoS.

#### Separate privileged-writer ownership test

The OVS path probe cannot establish exclusive ownership. The project has no
persistent owner service or authenticated request channel, so no automated
external privileged-writer test can yet be run against an owner boundary. The
current safe behavior is to leave production ownership false.

Once a separately administered owner service exists, the privileged-writer
test must run from a second privileged client in a different administrative
domain, not from the owner process and not through its cooperative lock:

1. Have the owner service return a short-lived, identity-pinned test target
   descriptor over its authenticated narrow channel. Do not expose namespace
   file descriptors or invoke arbitrary commands through the service.
2. From the independent privileged client, attempt namespace entry and a
   read-only target inspection using the deployment's documented service
   interface. Record the exact request and expect an explicit authorization
   denial. If inspection succeeds, ownership is disproven; do not proceed to
   mutation testing.
3. Only on a freshly provisioned disposable target with an approved test
   window, ask the independent client to request one documented, reversible
   sentinel mutation through the privileged boundary. Require the kernel
   readback to remain unchanged and an explicit denial. Do not use raw `tc`
   against a production device. Capture client identity, service decision,
   audit record, and before/after identity/readback.
4. Repeat after owner restart, namespace/interface replacement, and ownership
   loss. Every case must reject; otherwise keep ownership unproven.

For the current shared-root WSL deployment this test is expected to demonstrate
that equivalent privileged peers can access the namespace, not to produce a
pass. The shadow probe's operator approval file, a namespace inode, interface
identity, OVS graph, and Python lock are not substitutes for the independent
administrative boundary. Do not use an owner-supplied result as proof without
independent review.

Do not set `LIVE_TC_OWNERSHIP_GUARANTEED`, change
`ADAPTIVE_QOS_ENABLE_APPLICATION`, or connect this shadow path to production as
part of either probe.

Before a persistent service or isolated Apply control can be implemented, the
deployment must provide a separately enforceable administrative boundary
(for example a dedicated VM/service identity with no peer process holding
equivalent network-administration privilege), an authenticated narrow request
channel, owner acquisition/loss/restart behavior, concurrent-request
protection, and independent tests against both unprivileged and privileged
clients. If the deployment remains a shared WSL-root domain, keep isolated
mutation disabled; a cooperative lock or namespace identity check is not a
substitute for that boundary.
