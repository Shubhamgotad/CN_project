# Adaptive QoS before/after comparison plan

**Status:** Integration code and offline tests are prepared; 131 repository
tests passed. The actual controller was exercised on a newly created,
isolated disposable veth pair. No live Mininet traffic-control changes or
adaptive comparisons have been run.

## Integration design

The topology has root HTB `5:` and the 10 Mbps aggregate class `5:1` on both
bottleneck interfaces. Client-to-server QoS is scoped to `s1-eth5` egress; the
controller refuses other interfaces. Its proposed child hierarchy replaces
only the leaf qdisc below `5:1` with nested HTB handle `1:`. The existing root
and outer 10 Mbps class remain. Nested classes are flow-specific (`1:11` for
UDP destination port 5201, through `1:14` for 5204); the nested aggregate class
is `1:1`. Each flow leaf has netem `limit 1000 delay 10ms`, including the
explicitly selected default leaf. UDP filters attach under `1:`. The
default/unmatched flow is a required caller choice and shares that flow's
class/rate. No service meaning is assigned by port.

Integration requires a complete one-to-one `flow_policy_classes` mapping, an
explicit unique filter priority per port, and an explicit default port. Those
values are not selected by this plan. Until configured, the integration fails
closed. Existing policy allocations are mapped to flows only through this
caller-provided mapping; this plan does not approve those service assignments.

Before applying, the controller checks the exact baseline qdisc/class/filter
state or recognizes its own exact child hierarchy. It refuses unknown objects,
never replaces/deletes root `5:`, treats an identical policy application as a
no-op, and updates only owned leaf class rates when the mapping is unchanged.
Initial-apply failure restores the original netem leaf and verifies the
baseline. An explicit restore operation accepts only a recognized owned
subtree. Offline mocked tests cover failure branches. A separate actual-kernel
probe also applied and reapplied the controller hierarchy on a fresh veth,
exercised command failure and rollback, and verified cleanup. This validates
the controller on that WSL2 kernel and interface, not on Mininet's OVS-managed
interfaces. The controller serializes its own calls with a POSIX file lock,
but cannot coordinate arbitrary external `tc` commands.

The probe evidence is in
`telemetry/datasets/controller-veth-validation-20261015T192447Z/`; it records
the command log, original/baseline/applied/restored snapshots, injected
rollback-readback mismatch, and cleanup verification. The readback mismatch
was injected in the test runner after real rollback; an independent kernel
read confirmed the baseline. No broad cleanup was attempted by the controller.
A later controlled Mininet review must still confirm exact preconditions and
approved policy settings before application. Example structure (placeholders
are deliberately unresolved):

```text
tc qdisc replace dev s1-eth5 parent 5:1 handle 1: htb default <approved-minor>
tc class add dev s1-eth5 parent 1: classid 1:1 htb rate 10mbit ceil 10mbit
tc class add dev s1-eth5 parent 1:1 classid 1:11 htb rate <approved-rate> ceil <approved-ceil>
tc qdisc add dev s1-eth5 parent 1:11 handle 111: netem limit 1000 delay 10ms
tc filter add dev s1-eth5 protocol ip parent 1: prio <approved-priority> u32 match ip protocol 17 0xff match ip dport 5201 0xffff flowid 1:11
```

**The example commands are NOT EXECUTED.** The controller should be used for
eventual application because it performs preflight, verification, and rollback;
do not copy the structural example into a live shell. No live application is
authorized by this plan.

## Receiver telemetry

Per-run, per-flow interval JSONL filenames include scenario, experiment ID, and
host to avoid appending new records to a stale scenario file. The collector
accepts a caller-owned output directory; the comparison runner must create each
fresh repetition directory exclusively and pass it through. When a client
interval is paired with a server `--json-stream` interval, saved primary
throughput, packet loss, jitter, and interval boundaries are from the receiver.
Sender throughput and sender-relative interval boundaries are retained under
separate `sender_*` fields. Missing receiver intervals remain errors; sender
values are not substituted.

Pairing occurs within a flow using relative interval offsets and a match
tolerance. The collector now records the monotonic time when a receiver event
is observed, separately from its relative interval boundaries; observation
time is not the interval boundary. `telemetry.receiver_windows` accepts an
aggregate only when every receiver interval includes absolute start/end values
on the same explicit `clock_id`, all expected flows are present, and the
intervals continuously cover the exact shared window. Its synthetic tests
validate that calculation and its rejection cases; they do not establish that
the current collector or existing artifacts can produce such aligned absolute
boundaries. Until a live collector supplies a defensible mapping from receiver
interval boundaries onto one clock, report per-flow receiver metrics and an
arithmetic summary separately.

## Comparison protocol

Run at least three independent repetitions per condition:

| Condition | Flows | Offer per flow | Offered duration | Traffic control |
|---|---|---:|---:|---|
| Baseline | h1–h4 to server, UDP ports 5201–5204 | 4 Mbps | 5 seconds | Original root `5:`, class `5:1` at 10 Mbps, netem 10 ms |
| QoS | Same hosts, ports, and loads | 4 Mbps | 5 seconds | Reviewed nested hierarchy; exact mapping and policy must be approved first |

Use a counterbalanced order across repetitions. For every repetition, make a
fresh unique evidence directory and explicit four-file manifest; never glob
historical datasets. Confirm listeners and no active clients without killing
or restarting anything. Verify the baseline or approved nested tree, start
receiver collectors and latency monitoring, then snapshot qdisc/class/filter
and interface/server counters immediately before traffic. Launch each client
once, with a predeclared launch-skew limit, and record monotonic and UTC start
and finish times. Capture complete per-flow JSON plus client and server
interval streams. Snapshot the same counters immediately after all flows and
receiver drains complete; report counter deltas, not cumulative values.

Report per-flow receiver throughput, jitter, received/total/lost packets, loss
percentage, latency with sample timestamp/age, and receiver window boundaries.
Also report sender metrics separately. Include qdisc/class/filter configuration
and counters before/after, interface TX/RX deltas, server RX/TX deltas, and both
the common-window aggregate (only when alignment is verified) and arithmetic
summary.

Reject a repetition if a listener or client fails, a flow/file/interval is
missing or invalid, receiver intervals cannot be aligned, the required root,
10 Mbps aggregate, or 10 ms delay is not verified, an unexpected traffic
control object appears, or restoration fails verification. Stop the campaign
on any rollback failure. Do not interpret QoS as an improvement until the
per-class objective and acceptable trade-offs are declared and the repeated
receiver-window results meet them without violating the aggregate cap.

## Offline baseline/adaptive report

`integration/experiment_runner.py` accepts the existing Person 1 telemetry
JSONL records and writes a new JSON report without applying traffic control.
For example, after creating a fresh output path:

```text
python -m integration.experiment_runner --input telemetry/datasets/congestion.jsonl --output telemetry/datasets/comparison.json --mode both --model ai/prediction/model.joblib --flow-policy-classes "{\"5201\":\"gaming\",\"5202\":\"video\",\"5203\":\"web\",\"5204\":\"bulk\"}" --filter-priorities "{\"5201\":10,\"5202\":20,\"5203\":30,\"5204\":40}" --default-port 5204
```

Replace the example model path with an existing compatible trained artifact.
The four port/class assignments and priorities in this example are placeholders
for explicit caller-approved settings, not approved service semantics. The
baseline is the existing shared 10 Mbps HTB class with its 10 ms netem leaf;
it has no flow-specific allocation. Adaptive allocation is a generated plan
using the existing model, policy, and allocation code. The report pairs both
mode results to each same input record and timestamp. Existing aggregate
telemetry does not establish a synchronized multi-flow receiver window, so the
runner does not aggregate separate records or claim performance improvement.
Malformed records are retained as line-specific errors; queue metrics that are
unavailable remain `null`. The output path must be new and must not be the
input path.

## Baseline limitations

The two saved clean runs have arithmetic receiver summary sums of 9.5427 Mbps
and 9.5996 Mbps, high and shifting per-host loss, and sender-side interval
arrays. Their coarse timestamps and per-flow durations are not exact shared
receiver windows. The historical above-10 Mbps sums therefore remain
unresolved. The first clean run's metadata file also has trailing literal
`\\n` characters and is not valid JSON; its timing metadata needs
corroboration. Counter deltas are consistent with the flow summaries but do
not identify per-flow qdisc drops.
