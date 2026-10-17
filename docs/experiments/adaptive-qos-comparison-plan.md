# Adaptive QoS before/after comparison plan

**Status:** The managed five-leaf implementation and offline tests are
prepared; 186 repository tests passed, with 2 expected warnings. The five-leaf implementation has not
been live-validated. No `tc`, `ip`, Mininet, iperf, sudo, or live mutation was
used for this implementation or its validation.

## Integration design

The topology has root HTB `5:` and the 10 Mbps aggregate class `5:1` on both
bottleneck interfaces. Client-to-server QoS is scoped to `s1-eth5` egress; the
managed controller refuses other interfaces. Its explicit five-leaf
configuration replaces only the leaf qdisc below `5:1` with nested HTB handle
`1:` and keeps the existing root and outer 10 Mbps class. The nested aggregate
class is `1:1`. Service classes are fixed: `1:11` Gaming, `1:12` Video, `1:13`
Web, and `1:14` Bulk. UDP destination ports 5201–5204 map one-to-one to those
classes, with filter priorities 10, 20, 30, and 40. Each service class keeps
`rate=ceil` in this experiment.

Unmatched traffic uses the HTB default class `1:15`, not a service filter or
service class. That leaf has netem handle `150:` and a 1 Mbps rate and ceil.
Each leaf has netem `limit 1000 delay 10ms`. The approved profiles, including
the unmatched 1 Mbps leaf, are:

| Congestion | Gaming | Video | Web | Bulk | Unmatched | Total |
|---|---:|---:|---:|---:|---:|---:|
| LOW | 2.70 | 2.70 | 2.25 | 1.35 | 1.00 | 10.00 |
| MEDIUM | 3.60 | 2.70 | 1.80 | 0.90 | 1.00 | 10.00 |
| HIGH | 4.50 | 2.25 | 1.35 | 0.90 | 1.00 | 10.00 |

The managed planner validates the complete allocations and fixed class/filter
configuration before generating commands. It is separate from the existing
standalone four-class command generator. The telemetry integration requires
the explicit port-to-service mapping and exact filter priorities; it has no
default-port setting because unmatched traffic is selected by HTB default
class configuration.

Before applying, the managed controller checks the exact baseline
qdisc/class/filter state or recognizes its complete five-leaf hierarchy. It
rejects unexpected objects and ownership, never replaces/deletes root `5:`,
treats an identical managed policy as a no-op, and updates only owned service
class rates. Mutations are read back; failures restore and verify the prior
baseline or prior rates, and explicit restoration accepts only the recognized
managed subtree. Offline mocked tests cover command generation, all profiles,
inspection, apply, rollback, and restore. An earlier actual-kernel probe
covered only the pre-existing four-leaf controller; it is not validation of
the five-leaf implementation. The five-leaf implementation remains
unvalidated on a live kernel, disposable veth, or Mininet interface. Controller
locking cannot coordinate arbitrary external `tc` commands.

Example structure (these commands are illustrative only):

```text
tc qdisc replace dev s1-eth5 parent 5:1 handle 1: htb default 15
tc class add dev s1-eth5 parent 1: classid 1:1 htb rate 10mbit ceil 10mbit
tc class add dev s1-eth5 parent 1:1 classid 1:11 htb rate 2.7mbit ceil 2.7mbit
tc class add dev s1-eth5 parent 1:1 classid 1:12 htb rate 2.7mbit ceil 2.7mbit
tc class add dev s1-eth5 parent 1:1 classid 1:13 htb rate 2.25mbit ceil 2.25mbit
tc class add dev s1-eth5 parent 1:1 classid 1:14 htb rate 1.35mbit ceil 1.35mbit
tc class add dev s1-eth5 parent 1:1 classid 1:15 htb rate 1mbit ceil 1mbit
tc qdisc add dev s1-eth5 parent 1:15 handle 150: netem limit 1000 delay 10ms
tc filter add dev s1-eth5 protocol ip parent 1: prio 10 u32 match ip protocol 17 0xff match ip dport 5201 0xffff flowid 1:11
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
python -m integration.experiment_runner --input telemetry/datasets/congestion.jsonl --output telemetry/datasets/comparison.json --mode both --model ai/prediction/model.joblib --flow-policy-classes "{\"5201\":\"gaming\",\"5202\":\"video\",\"5203\":\"web\",\"5204\":\"bulk\"}" --filter-priorities "{\"5201\":10,\"5202\":20,\"5203\":30,\"5204\":40}"
```

Replace the example model path with an existing compatible trained artifact.
Baseline-only mode does not require a model. Report-level status is `planned`
only when all applicable plans succeed; `incomplete` means some plans coexist
with invalid, blocked, or failed records; `planning_failed` means validated
input produced no plan; and `invalid_input` means there were no nonblank rows
or no row passed telemetry validation. The CLI returns zero only for
`planned`, and nonzero otherwise, while retaining a JSON report and per-line
diagnostics whenever the output path can be written. Counts separately report
records processed, invalid telemetry records, baseline plans, adaptive plans,
adaptive decision failures, and records blocked by strict quality/freshness.

All runs are offline planning only: report fields explicitly state
`planning_only=true` and `experiment_executed`,
`measurements_collected`, `live_qos_applied`, and `tc_commands_executed` are
false. Input measurements are reported as supplied telemetry; passing a
validation policy does not mean the runner collected them.

Historical replay is permissive by default. `--strict-freshness` opts into
checking the Person 1 `timestamp` field as a record-freshness reference,
requiring an ISO-8601 timezone-aware value no older than 300 seconds and no
more than 30 seconds in the future. The telemetry schema maps `timestamp` to
the record timestamp; the iperf interval parser sets it when the report is
observed, so it is not a verified interval-start measurement time. Strict
freshness is therefore a caller-approved freshness policy for that reference,
not proof of sample-time freshness. The API accepts an aware `now` value for
deterministic offline checks. This option only gates plans; it never enables
live operations. Queue size and drop values are metadata, not model features.
Permissive queue policy keeps absent or null measurements visible without
fabricating values or blocking historical plans. `--strict-queue-quality`
requires both to be present, numeric, finite, and nonnegative. A
measurement-quality-ready record remains a plan input, not an executed or
measured experiment.
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
