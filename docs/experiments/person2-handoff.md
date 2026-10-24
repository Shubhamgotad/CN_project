# Person 2 Handoff — Adaptive QoS

## 1. Scope

Person 2 owns offline engineering work covering:
- Receiver-measurement telemetry contracts and validation.
- ML dataset quality and group-aware model evaluation.
- Offline evidence and experiment-campaign analysis.
- Automated tests and project documentation.
- Handoff of runtime validation requirements to Person 1.

Live experiment execution, WSL/Mininet networking, traffic generation, and QoS changes are outside the offline validation performed for this handoff.

## 2. Repository and branch

- Repository: `CN_project`
- Working branch: `feature/telemetry-ml-docs`
- Baseline recorded in the original handoff: `4fc7aba`
- This handoff is documentation; the runtime-evidence section below records Person 1's later results separately from Person 2's offline validation.

## 3. Test status

The full Python test suite completed with:

- **547 passed**
- **7 skipped**
- **3 warnings**
- Runtime: approximately 52 seconds

Focused prediction and dataset-builder tests completed with 19 passed and 2 warnings.

The warnings included expected missing-class warnings in a test partition and a Starlette/httpx deprecation warning. These results establish offline test status only; they do not establish live Mininet, receiver-producer, or QoS runtime readiness.

## 4. ML evaluation status

The project contains a group-aware candidate-evaluation path using `StratifiedGroupKFold`. It checks experiment-group separation and reports accuracy, macro-F1, per-class metrics, class distribution, group support, and a confusion matrix.

The tracked JSONL datasets inspected for this handoff do not contain `metadata.experiment_id`. They are therefore not ready to demonstrate experiment-group-separated evaluation without appropriate source metadata.

Do not report an unverified model score or treat the presence of `ai/prediction/model.joblib` as proof of model quality. Labels are derived from utilization, latency, and packet-loss thresholds, so evaluation against those labels measures agreement with the labeling rules rather than independent predictive validity.

## 5. Receiver-measurement and telemetry limitations

Receiver-boundary measurement provenance remains a runtime validation requirement. Before treating interval telemetry as authoritative, verify that interval boundaries correspond to the intended receiver-side measurement windows and that throughput, packet loss, RTT, and queue measurements have independently meaningful timestamps.

Do not assume that iperf3 relative interval offsets or callback observation timestamps establish absolute measurement boundaries. Verify timestamp semantics, stale-measurement handling, and compatibility with the telemetry consumer using controlled Linux/Mininet runs.

Person 1's evidence now covers real Normal and Moderate UDP traffic (Section 7). It does not satisfy the separate receiver-boundary, independent RTT/queue timestamp, congestion/burst, or live-policy validation requirements.

## 6. Offline evidence and experiment-campaign analysis

Evidence and campaign analysis should validate source provenance, required fields, experiment alignment, and completeness before comparing runs. Incomplete or mismatched evidence must not be presented as a valid baseline-versus-adaptive comparison.

A campaign should include at least three complete cycles and at least 48 trials, subject to the project's campaign-validation requirements. These thresholds are requirements for evidence collection, not proof that a campaign has been completed.

Report measured QoS changes only after the underlying Linux/Mininet evidence has been collected and validated. Offline tests, dashboard demo data, model-artifact presence, and successful API startup do not establish live runtime readiness or demonstrate QoS improvement.

Keep offline validation results separate from Person 1's live integration results.

## 7. Person 1 runtime evidence update — 2026-10-24

No additional live experiments were performed for this evidence collection. The evidence bundle was assembled from two existing local Mininet runs and reviewed for identity consistency. The shareable bundle was provided separately as `CN_project_evidence_bundle_20261024_163758_shareable.zip`; it is not committed as a repository artifact. It contains selected interval records and log excerpts, state fields, output-file inventories, timestamp-semantics source excerpts, and SHA-256 hashes of source artifacts. The full original source files are not included in that bundle, so recipients cannot independently recompute every source-artifact hash from the bundle alone.

### 7.1 Verified run summaries

| Field | Normal | Moderate |
|---|---|---|
| Run / experiment ID | `4fd268ba3d29444e8c607720a4edaeb6` | `a2aa9106eb484a66ba3211e1f232bb6b` |
| Source host | `h2` | `h3` |
| Requested UDP rate | 3.0 Mbps | 3.0 Mbps |
| Requested duration | 20 seconds | 20 seconds |
| State status / exit code | `completed` / 0 | `completed` / 0 |
| Process alive at inspection | false | false |
| Aggregate summary file | `normal.jsonl` | `moderate.jsonl` |
| Interval file | `normal_4fd268ba3d29444e8c607720a4edaeb6_h2_intervals.jsonl` | `moderate_a2aa9106eb484a66ba3211e1f232bb6b_h3_intervals.jsonl` |
| Interval record count | 21 | 21 |

The execution ID in `state.json`, the aggregate summary's `experiment_id`, and the ID encoded in the interval filename match for both runs. The summary's scenario and source also agree with the state fields.

### 7.2 Aggregate summary measurements

These values are copied from the respective aggregate summary JSONL records; they describe these two traffic runs and are not evidence of QoS improvement.

| Summary field | Normal | Moderate |
|---|---:|---:|
| `throughput_mbps` | 2.99718658122217 | 2.9971445144223523 |
| `latency_ms` | 20.53875 | 20.3364 |
| `jitter_ms` | 0.008157464799468434 | 0.010964410308177037 |
| `packet_loss_percent` | 0.0 | 0.0 |
| `lost_packets` / `total_packets` | 0 / 5180 | 0 / 5180 |
| `bandwidth_mbps` | 10.0 | 10.0 |
| `utilization_percent` | 29.97 | 29.97 |
| `active_flows` | 1 | 1 |
| `queue_size` / `queue_drops` | 3 / 0 | 3 / 0 |

### 7.3 Selected interval records and timestamp semantics

The bundle preserves the first, middle, and final interval JSONL records from each run with their original field names and values. The selected relative interval offsets are:

| Selected record | Normal | Moderate |
|---|---|---|
| First interval | 0.0–1.000303 s | 0.0–1.000150 s |
| Middle interval | 10.000291–11.000274 s | 10.000125–11.000258 s |
| Final interval | 20.000286–20.020482 s | 20.000144–20.020763 s |

The producer implementation captures `receiver_observation_monotonic_seconds` when a receiver JSON callback is handled and labels its role `diagnostic_only`. This is callback-observation time, not a measurement edge. The `interval_start_seconds` and `interval_end_seconds` values originate from iperf3's test-relative interval offsets; they are not absolute receiver-monotonic boundaries. The producer log/source excerpts show this distinction.

The final selected record for each run is a receiver-only post-client drain interval. It has no matching sender interval fields and must not be described as a normal matched sender/receiver interval. Other selected records include sender-side relative offsets and sender metrics alongside receiver-stream measurements.

The selected records include `latency_measurement_timestamp`, `queue_measurement_timestamp`, and corresponding age fields. Their presence alone does not establish independent timestamp provenance, validate stale-measurement handling, or make them synchronized receiver-window boundaries.

### 7.4 What this evidence does and does not establish

**Established for these two runs:**
- Normal UDP traffic from `h2` and Moderate UDP traffic from `h3` completed with exit code 0.
- Execution, summary telemetry, and interval-file identities are consistent.
- Each run has 21 interval records and an aggregate summary record with the fields and values shown above.
- The reviewed record excerpts show the distinction between receiver callback-observation time and iperf-relative interval offsets.

**Not established by this evidence:**
- Genuine receiver-monotonic measurement boundaries with independently validated uncertainty and clock/host/boot provenance.
- Independent RTT/queue timestamp provenance or validated stale-measurement handling.
- Runtime behavior for Burst or Congestion (including all-four-client concurrency and receiver readiness).
- Live prediction-to-QoS policy application, kernel-state verification, rollback/restoration, or a baseline-versus-adaptive QoS improvement.
- A completed experiment campaign meeting the three-cycle/48-trial requirement.

The full local run directories contain `state.json`, producer logs, and output files. The paths are machine-local and are intentionally not embedded in this shared document. The reviewed evidence bundle is available separately from Person 1; no new traffic experiment was run to assemble it.
