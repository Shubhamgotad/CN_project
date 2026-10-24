# Receiver-boundary producer interface design

**Status:** Design proposal only. No receiver-boundary producer is implemented,
and no live measurement capability or readiness is established.

## Purpose and current evidence

The telemetry pipeline needs the actual start and end of each receiver
measurement interval in a named receiver monotonic clock domain. The current
collector does not provide those boundaries:

- `traffic.traffic_generator.IntervalTelemetryCollector.add_receiver_event`
  captures `time.monotonic()` while handling the receiver JSON callback. That
  is a collector callback-observation timestamp, not a measurement edge.
- `telemetry.iperf_parser._parse_interval_payload` writes iperf's `sum.start`
  and `sum.end` to `interval_start_seconds` and `interval_end_seconds`. These
  are test-relative elapsed offsets, not absolute receiver-clock values.
- The parser's UTC `timestamp`/`observation_timestamp` is set when the report
  is parsed. It is report-observation time.
- The receiver listener starts iperf3 with `--json-stream`; the repository
  contains no instrumented iperf3 source, separate receiver-boundary producer,
  producer binary, or existing actual-boundary artifact.

The existing `receiver_windows` contract rejects missing/invalid declared
boundaries, requires clock/host/boot consistency, and computes guaranteed
coverage from uncertainty-shrunk interval interiors. The analyzer checks
contract fields and uncertainty-evidence file hashes. These are useful
offline checks, but a schema value, matching identity string, hash, or
synthetic fixture does not prove that a producer recorded true measurement
edges. Report states distinguish schema-valid metadata, evidence-artifact
integrity, independent producer/clock validation, and alignment eligibility.
Artifact integrity `verified` means only that the referenced bytes and fields
match; it is not producer capability validation or live readiness.

Relevant implementation and tests:

- `traffic/traffic_generator.py`: `IntervalTelemetryCollector`,
  `start_receiver_listener`, and `_run_udp_flow`.
- `telemetry/iperf_parser.py`: `parse_iperf_interval_events` and
  `_parse_interval_payload`.
- `telemetry/receiver_windows.py`: `validate_receiver_interval_record` and
  `synchronized_receiver_aggregate`.
- `integration/evidence_analysis.py`: receiver boundary evidence loading,
  contract aggregation, and sender/receiver comparison reporting.
- `integration/campaign_validation.py`: campaign structure, trial alignment
  reporting, and the separate unavailable sender/receiver outcome.
- `tests/test_interval_telemetry.py`, `tests/test_receiver_windows.py`,
  `tests/test_evidence_analysis.py`, and `tests/test_campaign_validation.py`.
- `docs/experiments/evidence-analysis-runbook.md`.

## Producer alternatives

| Dimension | A. Instrument iperf3 receiver interval accounting | B. Separate receiver-side measurement producer |
|---|---|---|
| Genuine edges | Could capture edges if timestamps are taken at the actual receiver interval accounting transitions, not when JSON is emitted. Repository evidence does not establish that the current iperf3 build exposes those transition points or that an instrumented build is available. | Could capture edges if it owns the measurement window and timestamps the events that define its metrics. A process that only parses iperf output or observes callbacks would not qualify. No such producer exists in the repository. |
| Required components | Obtain and maintain an instrumented iperf3 source/build; identify the exact accounting transition; capture both edges in the receiver monotonic clock; emit versioned JSON fields while retaining the original relative offsets; teach the telemetry parser/collector to preserve and validate those fields. | Implement a receiver-side producer, its interval/event accounting, versioned output, lifecycle and run identity; make it the authoritative source for the metrics it reports; add an adapter to the collector/parser and keep iperf-relative timing separate. |
| Identity | Include receiver host and boot identity, the concrete monotonic clock implementation/domain, clock instance identity, iperf build identity, and process-instance identity. | Include receiver host and boot identity, concrete monotonic clock implementation/domain and instance identity, producer build identity, and producer process-instance identity. |
| Uncertainty and provenance | Characterize edge capture/resolution and accounting-to-timestamp placement. Emit per-edge uncertainty and hash-linked evidence for the method, build, raw characterization data, and independent validation. A source patch or self-declared uncertainty is not independent validation. | Characterize event timestamping and metric accounting in the producer. Emit per-edge uncertainty and hash-linked method, implementation/build, raw characterization data, and independent validation. Any use of packet capture or another measurement backend must specify packet inclusion, loss accounting, and interval semantics. |
| Offline testing | Test JSON serialization/parsing, malformed values, clock identity changes, interval edge semantics with a deterministic injectable clock, and regression that relative offsets are never copied into absolute fields. The actual patched iperf accounting path also needs an offline build/harness test. | Test the producer's interval state machine using deterministic synthetic event streams and clock injection, output validation, lifecycle/error cases, and metric accounting. Synthetic tests validate software behavior, not timing truth. |
| Independent validation | Compare emitted edges with a separately instrumented reference at the accounting transition; preserve raw traces/build hashes and quantify edge error under the intended load. Validate the exact binary used, not just a source revision. | Compare the producer's event/metric semantics and edges with an independent reference; quantify timing error and metric agreement, retain raw traces and build hashes, and validate the exact deployed binary. |
| Compatibility and operational risks | Requires an external source/build process not present here; upstream version drift, build reproducibility, binary distribution, and mismatch with stock iperf3 output are risks. The receiver's measurement meaning can stay close to iperf3 if the true accounting edge is instrumented correctly. | Adds a new runtime component and possibly a second definition of receiver metrics. Packet/accounting semantics may diverge from iperf3; lifecycle, permissions, filtering, performance overhead, and evidence management must be designed. |
| Failures that must block | Missing/unsupported instrumentation fields, unsupported binary hash, unknown clock or boot, malformed/non-finite edges, invalid uncertainty evidence, or disagreement between raw and parsed output. | Missing/failed producer, missing or inconsistent identity, invalid evidence, event loss, metric mismatch, or unsupported producer build. It must never fall back to callback time or relative offsets. |

**Recommendation:** unresolved. The repository has no instrumented iperf3
source/build or independent receiver producer, and therefore does not provide
evidence that either implementation can be safely selected. Option A is more
direct if the project can obtain and maintain the relevant iperf3 source and
instrument the actual accounting transition. Option B is viable only if the
project accepts a separately specified authoritative receiver measurement
semantics and its additional runtime/validation burden. The project owner must
select the source after confirming source availability, metrics semantics, and
operational ownership.

## Draft versioned producer contract

The following is a proposed JSON-lines record shape, not a current collector
output or evidence that these values can be produced. `schema` identifies the
contract; `producer`, `clock`, `identity`, and `measurement` must be populated
from the actual run rather than copied from a template.

```json
{
  "schema": "adaptive-qos.receiver-interval",
  "schema_version": 1,
  "producer": {
    "name": "receiver-interval-producer",
    "version": "implementation-version",
    "build_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "process_instance_id": "unique-per-process-instance",
    "pid": 1234,
    "process_start_monotonic_seconds": 9876.5
  },
  "identity": {
    "experiment_id": "experiment-id",
    "trial_id": "trial-id",
    "repetition_id": "repetition-id",
    "flow_id": "h1",
    "interval_id": "unique-interval-id"
  },
  "clock": {
    "domain": "receiver_monotonic",
    "implementation": "platform-clock-implementation",
    "clock_id": "boot-scoped-clock-instance-id",
    "host_id": "receiver-host-identity",
    "boot_id": "receiver-boot-identity",
    "unit": "seconds",
    "resolution_nanoseconds": 1
  },
  "interval": {
    "semantics": "[start,end); actual measurement-accounting window",
    "start_monotonic_seconds": 1000.0,
    "end_monotonic_seconds": 1001.0,
    "start_uncertainty_seconds": 0.0001,
    "end_uncertainty_seconds": 0.0001,
    "start_uncertainty_evidence": {
      "path": "evidence/edge-characterization.json",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    },
    "end_uncertainty_evidence": {
      "path": "evidence/edge-characterization.json",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  },
  "measurement": {
    "direction": "receiver",
    "metric": "throughput",
    "value": 3.5,
    "unit": "Mbps",
    "validity": "valid",
    "provenance": {
      "accounting_source": "receiver-interval-accounting",
      "source_artifact": {
        "path": "raw/receiver-interval-source.json",
        "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      }
    }
  },
  "observations": {
    "callback": {
      "role": "diagnostic_only",
      "utc": "2026-01-01T00:00:00.000000Z",
      "monotonic_seconds": 1001.2,
      "clock_id": "collector-clock-instance",
      "host_id": "collector-host",
      "boot_id": "collector-boot"
    },
    "parse": {
      "role": "diagnostic_only",
      "utc": "2026-01-01T00:00:00.000100Z"
    }
  },
  "source_artifacts": [
    {
      "role": "producer-raw-output",
      "path": "raw/receiver-output.jsonl",
      "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    }
  ]
}
```

All values above are illustrative synthetic placeholders. They are not a
recommended timestamp resolution or uncertainty bound.

### Required field semantics and validation

- Require the exact schema name and supported integer version; reject unknown
  required-field versions rather than silently interpreting them.
- Require non-empty producer name/version/build digest and a unique
  `process_instance_id`. `pid` alone is not a process identity because it can
  be reused. The build digest must identify the exact executable used.
- Require non-empty experiment, trial, repetition, flow, and interval IDs.
  Enforce the existing campaign uniqueness rules at campaign scope; these IDs
  do not by themselves establish measurement validity.
- Require both finite, nonnegative monotonic boundary values, with end greater
  than start. Require the fixed interval semantics and unit. Reject a record
  if the producer cannot show that these values were captured at its actual
  measurement-accounting edges.
- Require non-empty clock implementation, clock ID, host ID, and boot ID.
  Require finite positive clock resolution and a known unit. A `clock_id`
  string is metadata, not proof. A producer capability-validation artifact
  must bind the clock implementation and identity construction to the exact
  build and host/boot environment.
- Require finite nonnegative uncertainty for each edge and a separately
  hash-verified evidence reference for each. Reject absent, stale, malformed,
  contradictory, out-of-domain, or hash-mismatched evidence. The evidence must
  explain how the edge bound was measured and computed; merely repeating the
  declared uncertainty is insufficient.
- Require a finite metric value, explicit metric name, units, receiver
  direction, validity state, and accounting provenance. Reject invalid
  metrics and unsupported metric semantics. When a metric is derived from raw
  data, require safe relative artifact references and SHA-256 hashes.
- Callback and parse observations are optional diagnostics, explicitly marked
  `diagnostic_only`. If they include monotonic values, include the observing
  process's own clock/host/boot identities. Never promote these values to
  interval edges or substitute them for missing edge data.
- Resolve artifact paths relative to the trial evidence root; reject traversal,
  symlink escape, unreadable files, and digest mismatch.
- Intervals may share a monotonic domain only when their clock implementation,
  boot-scoped clock identity, host identity, and boot identity agree, and the
  producer's independently validated identity scheme says those records use
  the same clock instance. Reject identity changes across a trial. A matching
  identifier alone does not establish the identity scheme or truthfulness.

The sample's uncertainty values, clock implementation string, and digests are
examples only. Do not use them as measurement policy, actual evidence, or proof
that any producer exists.

## Acceptance gates

The gates are separate. Passing a later-looking gate must not implicitly pass
an earlier one.

1. **Schema/contract validation (offline).** Add deterministic fixtures and
   malformed-input tests for missing fields, types, units, non-finite values,
   invalid intervals, invalid provenance, bad paths/digests, identity changes,
   callback-only timestamps, and relative-only iperf offsets. Mark all fixtures
   synthetic. This validates parser and validator behavior only.
2. **Producer capability validation.** Demonstrate, for the exact producer
   build, that emitted boundaries coincide with the documented measurement
   accounting edges and not callback/parse time. Independently compare to a
   reference, preserve raw evidence and build hashes, characterize clock
   resolution and per-edge uncertainty, and review metric semantics. A schema
   pass, `status: verified`, matching IDs, or synthetic fixture is not this
   gate. No live-readiness claim is possible until an approved controlled
   validation plan is executed.
3. **Receiver-only aggregation validation.** Only after producer capability
   validation, use the existing uncertainty-aware aggregation model: compute
   guaranteed interval interiors, require all expected flows and consistent
   clock/host/boot identities, and fail closed on missing data, invalid
   uncertainty, gaps, duplicate or unresolved overlapping intervals, and
   insufficient shared-window coverage. The project owner must first approve a
   measurement-error budget; no project-wide maximum uncertainty is selected
   here.
4. **Sender/receiver temporal comparison.** Keep unavailable until a real
   sender-to-receiver clock mapping is implemented and independently
   validated, with its own uncertainty evidence and approved error budget.
   Receiver-only aggregation does not establish this mapping.
5. **Campaign completeness.** Preserve the approved four strata, exact A-D
   Williams sequences, three complete cycles, and 48-trial minimum. Report
   campaign completeness independently from producer capability, receiver
   alignment, sender/receiver comparability, and live readiness. A complete
   campaign is not aligned or live-ready merely because its structural
   validator passes.

## Proposed integration and tests after approval

1. Select and own producer option A or B; document exact metric and interval
   edge semantics and approve the measurement-error budget before setting a
   numeric maximum uncertainty.
2. Implement the producer and its deterministic clock-injected unit tests.
   Keep iperf-relative offsets and callback observations in their existing
   fields and semantics; add separate producer fields.
3. Add a parser/adapter that validates the versioned producer record without
   translating relative offsets or observations into absolute boundaries.
4. Extend the trial evidence manifest and analyzer to require/hash-check raw
   producer output and capability-validation evidence. Distinguish
   `contract_valid` from independently validated producer capability in the
   report; do not let schema conformance alone qualify alignment.
5. Gate `receiver_windows` aggregation on the accepted producer capability,
   then test guaranteed coverage, uncertain edges, missing flows, gaps,
   overlaps, and inconsistent clock/host/boot identities.
6. Keep sender/receiver mapping unavailable until a separate mapping contract
   and independent validation pass. Keep campaign completeness as its current
   separate structural outcome.

## Decisions requiring project-owner approval

- Which producer option to fund and maintain, and who owns its source, build,
  binary provenance, deployment, and independent review?
- Which receiver metric semantics and exact interval-accounting events are
  authoritative? How are packet inclusion and interval edge inclusivity
  defined?
- Which receiver monotonic clock implementation and host/boot identity source
  are supported, and how are those identities independently established?
- What measurement-error budget is acceptable for the comparison's smallest
  decision-relevant effect and window duration? Only after this decision may a
  numeric uncertainty limit be chosen.
- What independent reference method, controlled validation procedure, raw
  evidence retention, and approval authority qualify producer capability?
- Which producer versions/build hashes and evidence formats are accepted, and
  what is the compatibility/deprecation policy?
- What separate sender-to-receiver mapping method and error budget will be
  considered later?

Until these decisions and gates are satisfied, current collected records
cannot qualify as receiver-boundary-aligned measurements. No producer
capability, live validation, or live readiness is claimed by this design.
