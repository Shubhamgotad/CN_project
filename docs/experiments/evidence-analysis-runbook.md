# Offline experiment evidence analysis

## Scope and safety

`integration.evidence_analysis` reads only the paths explicitly named in a
per-trial JSON manifest. It never globs datasets, discovers historical
artifacts, starts traffic, invokes a subprocess, or applies/restores traffic
control. It writes a JSON report and a CSV summary to caller-selected output
paths outside the project worktree and manifest evidence directory. Output
parents must already exist and must not contain symlinks; report files are
created exclusively and existing or concurrently created targets are never
overwritten. A concurrent parent-directory replacement remains a platform-level
race limitation. A complete offline report is not live validation and makes no
claim that the analyzer collected measurements.

The analyzer expects four per-flow JSONL evidence files for each trial. Every
row must contain a matching `experiment_id` and a flow identity under either
`flow_id` or `source` (host identity); when both are present, `flow_id` is the
identity field checked by the analyzer. Evidence paths must be relative to the
manifest directory and may not resolve outside it, including through symlinks.
Receiver measurements use the collector's `throughput_mbps`, `latency_ms`,
`jitter_ms`, and `packet_loss_percent` fields only when explicit receiver
provenance is present (`measurement_direction=receiver` or a recognized
receiver/server `throughput_measurement_source`). `sender_throughput_mbps`
remains separate and is never substituted. Missing metric values are reported
as unavailable except that missing or null receiver `throughput_mbps` is a
required-measurement failure and fails the trial. Invalid rows and mismatched
identities fail the trial and remain in its error list; valid rows in a file
may still be summarized, but their presence does not erase the failure.

The current interval collector emits relative `interval_start_seconds` and
`interval_end_seconds`, callback observation time, and server/client
measurements. Callback observations are marked `diagnostic_only`; the collector
does **not** emit actual receiver-monotonic measurement boundaries. The
versioned receiver interval contract requires
`receiver_interval_schema_version: 1`, `boundary_source:
"receiver_measurement"`, `clock_domain: "receiver_monotonic"`, `clock_id`,
`host_id`, `boot_id`, finite
`absolute_start_seconds`/`absolute_end_seconds`, per-edge uncertainty values,
and `boundary_uncertainty_evidence` with method, evidence path, and SHA-256.
The evidence file must match the interval uncertainties and digest.

The offline geometry aggregator validates the boundary metadata and aggregates
only the guaranteed interval interior after subtracting the start/end
uncertainty from nominal boundaries. It does not inspect evidence files.
Evidence-analysis separately verifies referenced files and their contents;
failed or missing artifacts make the aggregate unavailable. Reports distinguish
`boundary_metadata_status: schema_valid` from
`boundary_evidence_integrity_status: verified`. Neither establishes
`producer_provenance_status` or `clock_mapping_status`, which remain
`not_independently_validated`; `measurement_alignment_eligibility` therefore
remains `ineligible`. Missing flows, malformed evidence, identity mismatch,
uncertainty-induced gaps, overlaps, and incomplete common coverage fail closed.
Relative iperf offsets, callback observations, a matching clock ID, a valid
digest, or a declared `verified` status alone are insufficient. Since the
current collector cannot produce actual receiver boundaries, real collected
intervals cannot currently qualify for comparison.

Sender/receiver temporal comparison is reported separately and remains
unavailable until a sender-to-receiver mapping method is approved, implemented,
and independently validated. No mapping uncertainty threshold is configured
or implied by this receiver-only contract. Any arithmetic sum of flow means
remains labelled `unverified arithmetic sum of per-flow receiver means`.

### Receiver-boundary instrumentation blocker

The current four-flow collector starts an iperf3 server with
`--json-stream` in the receiver namespace and parses its interval events.
iperf3's reported `sum.start`/`sum.end` values are elapsed offsets for that
test, not absolute receiver-monotonic timestamps. The collector separately
captures `time.monotonic()` while handling each receiver callback; that value
is an observation time from the collector process, not either measurement
boundary. The parser's UTC timestamp likewise records report observation.
No receiver clock ID, receiver host/boot identity, or actual boundary value is
currently emitted.

Producing qualifying evidence requires an instrumented receiver-side iperf3
interval-accounting source or a separately implemented receiver measurement
producer. For each interval it must emit the actual measurement start and end
in the receiver's monotonic domain, with versioned boundary provenance,
clock/host/boot identities, edge uncertainty and independently checkable
uncertainty evidence. Callback observations must remain separate diagnostics.
The producer's boundary semantics, clock identity, timestamp precision, and
uncertainty method must be independently verified before its output can
qualify; a declared status or matching identity strings alone are not proof.
This instrumentation is not implemented or validated here.

Receiver-only alignment therefore remains unavailable for records from the
current collector. Sender/receiver temporal comparison additionally requires
an independently validated sender-to-receiver clock mapping. Before selecting
any maximum uncertainty, the project owner must approve a measurement-error
budget tied to the duration and precision of the comparison window and the
smallest effect the experiment intends to distinguish. Neither synthetic
fixtures nor campaign completeness establishes measurement alignment or live
readiness.

## Trial matrix and duration decision

The matrix below records the explicitly approved project contract, rather than
asserting that the earlier documentation consistently established it. The
project decision is 20 seconds per flow at 4 Mbps on each of four concurrent
UDP flows; this resolves the conflicting duration statements in the earlier
experiment documentation.

There are exactly four comparison strata: Baseline/BASELINE, QoS/LOW,
QoS/MEDIUM, and QoS/HIGH. `congestion` names the traffic-generator scenario; it
is not a fifth comparison condition. Every trial runs four simultaneous flows,
h1–h4 to the server on UDP ports 5201–5204, at 4 Mbps per flow for 20 seconds.
The managed profiles are:

| Profile | Gaming | Video | Web | Bulk | Unmatched |
|---|---:|---:|---:|---:|---:|
| LOW | 2.70 Mbps | 2.70 Mbps | 2.25 Mbps | 1.35 Mbps | 1.00 Mbps |
| MEDIUM | 3.60 Mbps | 2.70 Mbps | 1.80 Mbps | 0.90 Mbps | 1.00 Mbps |
| HIGH | 4.50 Mbps | 2.25 Mbps | 1.35 Mbps | 0.90 Mbps | 1.00 Mbps |

The approved comparison duration is **20 seconds per flow**. Every trial
manifest must state `duration_seconds: 20`. Explicit operator confirmation
remains a record of the approved duration decision only; it does not authorize
execution or mark evidence live-validated:

```json
"duration_confirmation": {
  "confirmed": false,
  "operator": "",
  "confirmed_duration_seconds": null,
  "note": ""
}
```

The analysis report always says `live_operations_supported: false`. Until an
operator supplies a non-empty identity and note, sets `confirmed` to true, and
confirms a duration matching every listed trial, the report also sets
`duration_confirmation.live_use_blocked: true`. Confirmation documents the
duration decision only; it does not authorize execution or mark the evidence
live-validated. A manifest with schema-level validation errors or invalid
trial structure cannot confirm the duration; its status remains
`pending_operator_confirmation` and `live_use_blocked` remains true even when
the supplied operator and duration fields otherwise match.

## Explicit manifest

Create a fresh manifest for the evidence being analyzed. Paths are relative to
the manifest directory. List each evidence file exactly once; shared references
are rejected. Do not use wildcard patterns or point the tool at a historical
dataset directory for discovery.

```json
{
  "schema_version": 1,
  "offered_rate_mbps": 4,
  "duration_confirmation": {
    "confirmed": false,
    "operator": "",
    "confirmed_duration_seconds": null,
    "note": ""
  },
  "trials": [
    {
      "trial_id": "baseline-r1",
      "trial_status": "completed",
      "experiment_id": "the-recorded-experiment-id",
      "condition": "baseline",
      "profile": "BASELINE",
      "repetition_id": "r1",
      "duration_seconds": 20,
      "declared_window": {
        "clock_id": "explicit-clock-id",
        "start_seconds": 1000.0,
        "end_seconds": 1020.0
      },
      "flows": [
        {"flow_id": "h1", "port": 5201, "evidence_path": "trial/baseline-r1-h1.jsonl"},
        {"flow_id": "h2", "port": 5202, "evidence_path": "trial/baseline-r1-h2.jsonl"},
        {"flow_id": "h3", "port": 5203, "evidence_path": "trial/baseline-r1-h3.jsonl"},
        {"flow_id": "h4", "port": 5204, "evidence_path": "trial/baseline-r1-h4.jsonl"}
      ]
    }
  ]
}
```

The clock-mapping numbers above are synthetic examples, not approved live
thresholds or evidence that a collector supports this mapping. The campaign
manifest must supply the approved maximum and matching policy identifier.

For QoS trials use `condition: "qos"` and `profile: "LOW"`, `"MEDIUM"`, or
`"HIGH"`. For a failed trial, set `trial_status: "failed"` and include a
non-empty `failure_reason`; the trial and reason remain in the report. Keep
repetitions as distinct trial IDs and repetition IDs. All four flow identities,
ports, evidence paths, experiment IDs, finite nonnegative metrics, and JSONL
rows are validated. The declared `experiment_id` must match every evidence
record for that trial.

## Campaign manifest and validation

Per-trial analysis and campaign completeness are separate results. A trial
manifest validates one trial; it does not establish repetition count or
counterbalanced execution. The campaign validator reads an explicit
`campaign.json` and references each trial package by relative path. It performs
no discovery, traffic generation, or evidence writes.

The campaign manifest uses `schema_version: 1`, a unique `campaign_id`,
`claimed_status`, `minimum_complete_cycles: 3`, `offered_rate_mbps_per_flow: 4`,
and `duration_seconds_per_flow: 20`. Its `design` declares
`id: "WILLIAMS_4"`, `version: 1`, all four `strata` mappings, and these exact
`sequences`:

```json
{
  "A": ["baseline", "qos_low", "qos_high", "qos_medium"],
  "B": ["qos_low", "qos_medium", "baseline", "qos_high"],
  "C": ["qos_medium", "qos_high", "qos_low", "baseline"],
  "D": ["qos_high", "baseline", "qos_medium", "qos_low"]
}
```

The following abbreviated synthetic example demonstrates the campaign schema
and one trial entry. It is deliberately not a complete campaign: a complete
manifest contains at least 48 trials, all artifacts, and three full cycles.
Replace the example clock policy only with a team-approved policy:

```json
{
  "schema_version": 1,
  "campaign_id": "synthetic-example",
  "claimed_status": "incomplete",
  "minimum_complete_cycles": 3,
  "offered_rate_mbps_per_flow": 4,
  "duration_seconds_per_flow": 20,
  "design": {
    "id": "WILLIAMS_4",
    "version": 1,
    "strata": {
      "baseline": {"condition": "baseline", "profile": "BASELINE"},
      "qos_low": {"condition": "qos", "profile": "LOW"},
      "qos_medium": {"condition": "qos", "profile": "MEDIUM"},
      "qos_high": {"condition": "qos", "profile": "HIGH"}
    },
    "sequences": {
      "A": ["baseline", "qos_low", "qos_high", "qos_medium"],
      "B": ["qos_low", "qos_medium", "baseline", "qos_high"],
      "C": ["qos_medium", "qos_high", "qos_low", "baseline"],
      "D": ["qos_high", "baseline", "qos_medium", "qos_low"]
    }
  },
  "trials": [{
    "trial_id": "synthetic-cycle1-A-baseline",
    "repetition_id": "synthetic-rep-001",
    "experiment_id": "synthetic-experiment-001",
    "condition": "baseline",
    "profile": "BASELINE",
    "stratum_id": "baseline",
    "cycle": 1,
    "sequence_id": "A",
    "execution_order": 1,
    "duration_seconds": 20,
    "trial_manifest_path": "trials/001/trial-manifest.json",
    "trial_status_path": "trials/001/trial_status.json",
    "aggregate_provenance_path": "trials/001/aggregate-provenance.json",
    "raw_reports": [{
      "path": "trials/001/raw-report.json",
      "sha256": "<64 lowercase hexadecimal characters>"
    }],
    "timestamps": {
      "trial_start_utc": "2026-01-01T00:00:00+00:00",
      "trial_finish_utc": "2026-01-01T00:00:30+00:00",
      "window_start_utc": "2026-01-01T00:00:05+00:00",
      "window_finish_utc": "2026-01-01T00:00:25+00:00",
      "trial_start_monotonic_seconds": 1000,
      "trial_finish_monotonic_seconds": 1030,
      "window_start_monotonic_seconds": 1005,
      "window_finish_monotonic_seconds": 1025,
      "clock_id": "synthetic-clock"
    },
    "traffic_control": {
      "before_snapshot_path": "trials/001/tc-before.json",
      "after_snapshot_path": "trials/001/tc-after.json",
      "counter_evidence_path": "trials/001/counters.json"
    },
    "connectivity_verification_path": "trials/001/connectivity-verification.json",
    "restoration_verification_path": "trials/001/restoration-verification.json"
  }]
}
```

Each campaign trial declares globally unique `trial_id`, `repetition_id`, and
`experiment_id`; `condition`, `profile`, `stratum_id`, `cycle`, `sequence_id`,
`execution_order`, and `duration_seconds`; and references to its
`trial_manifest_path`, `trial_status_path`, `aggregate_provenance_path`,
`raw_reports` (`path` plus lowercase SHA-256), `timestamps`,
`traffic_control` before/after snapshots and counter evidence,
`connectivity_verification_path`, and `restoration_verification_path`.
All references must resolve safely inside the campaign root. The trial manifest
explicitly lists the four flow-to-port mappings and their JSONL paths. A
`trial_status.json` is separate from the analyzer-generated trial status; the
validator compares them and rejects disagreement.

Each cycle contains A, B, C, and D once, with each block containing every
stratum once in the prescribed sequence. Each stratum occupies each sequence
position once per cycle. Three complete cycles require 48 trials minimum.
`execution_order` must be contiguous and follow cycle, A/B/C/D, and position.
The declared status is checked against the computed campaign status.

Required artifact checks include matching trial identity in JSON artifacts;
aggregate provenance containing the matching clock/window, four flow IDs,
the exact per-flow evidence paths (`source_evidence_paths`), aggregation
method, and finite aggregate;
hashes verified against raw-report bytes; timezone-aware
`trial_start_utc`/`trial_finish_utc` and `window_start_utc`/`window_finish_utc`,
plus finite monotonic equivalents and a matching `clock_id`; before/after
snapshots containing non-empty `qdiscs` and `classes`, a `filters` array, and
timezone-aware `captured_at_utc` (before must precede trial start; after must
follow trial finish); and connectivity/restoration JSON with
`status: "verified"`.
The `interface` counter object has `tx_bytes_*` and `rx_bytes_*` before/after/
delta integer fields; `server` has `rx_bytes_*` and `tx_bytes_*`; and `tc` has
`qdisc_packets_*`, `class_bytes_*`, and `filter_packets_*`. Every delta must
equal after minus before. Counter artifacts include timezone-aware
`before_captured_at_utc` and `after_captured_at_utc`; the first must precede
trial start and the second must follow trial finish. Status, aggregate,
snapshot, counter, connectivity, and restoration JSON artifacts include
matching `trial_id` and `experiment_id`.

The report separates `receiver_alignment_status` from
`sender_receiver_temporal_comparison`. Contract-valid geometry and verified
artifact integrity are reported separately from independent producer and clock
validation. Alignment stays ineligible until the producer and clock mapping
have been independently validated. Sender/receiver temporal comparison is
always `unavailable` in this phase; declared mapping metadata is not proof. A
structurally complete campaign or a passing synthetic test is not live
readiness. Receiver-boundary instrumentation and sender-to-receiver mapping
remain unresolved.

Run the offline campaign validator from the repository root:

```text
python -m integration.campaign_validation --manifest campaign.json
```

Optionally persist the JSON report at a new path in the already-created,
approved `ADAPTIVE_QOS_REPORT_DIR` so the dashboard API can discover it:

```text
python -m integration.campaign_validation --manifest campaign.json --json-output <approved-report-directory>/campaign-validation.json
```

The output path must be outside the repository and the campaign evidence
directory. The command uses exclusive creation and will fail rather than
overwrite an existing report. Configure `ADAPTIVE_QOS_REPORT_DIR` in the API
process environment as well, then use **Refresh reports** in Experiments.

It prints a JSON report with campaign structure/status, per-trial analyzer and
alignment status, errors, `comparison_ready`, and
`live_operations_supported: false`; exit status is zero only for computed
campaign status `complete`. Campaign completeness does not override a trial
failure or an unavailable alignment result. Three complete cycles yield 12
repetitions per stratum (three occurrences of each stratum at each of the four
sequence positions).

## Run per-trial analysis

Use a new output location; existing JSON or CSV outputs are never overwritten:

```text
python -m integration.evidence_analysis --manifest trial-manifest.json --json-output analysis.json --csv-output summary.csv
```

The JSON report retains every listed trial, including failures and actionable
reasons, and includes comparisons grouped by condition/profile with each
repetition listed separately. A trial is complete only when its evidence and
manifest validate. Overall analysis status is `complete` only when all listed
trials are complete and the manifest is valid; otherwise it is `incomplete` or
`invalid_manifest`. The command returns nonzero unless status is `complete`.
Invalid duration values (including non-finite values, booleans, nonnumeric
types, zero, and negatives) fail their trial and cannot satisfy duration
confirmation. Non-standard JSON constants such as `NaN` and `Infinity` are
treated as non-finite input and fail the relevant validation; reports encode
non-finite values as `null` rather than writing non-standard JSON. If the
manifest itself is missing, malformed, or unreadable, the report leaves
duration confirmation pending and `live_use_blocked` true.
The CSV has one row per expected flow per trial, with receiver and sender
columns separate. Artifact-integrity status refers only to file/hash/content
consistency; it does not imply validated producer provenance, clock mapping,
or comparison-eligible alignment.

## Limitations

The archived telemetry files are aggregate scenario records, not the
four-flow-per-trial explicit manifest required here. The current collector
does not provide genuine receiver-monotonic boundaries or independently
validated clock mapping. Synthetic tests validate parsing, evidence rejection,
contract geometry, summaries, and aggregate gating only; they are not live
validation. Do not claim experiment completion or QoS improvement from an
offline analyzer report.
