# Person 2 Handoff — Adaptive QoS

## Scope and repository state

Person 2's offline scope covers telemetry contracts and validation, ML dataset quality, experiment-group evaluation, tests, and documentation. This handoff records offline evidence only. No live Mininet experiment, network/QoS policy change, commit, or push was performed.

Audited checkout: branch `main`, baseline commit `0bff40216b3427f66192a8cf89ddb1707cc0e5c0`.

## Dataset and duplicate audit

`telemetry/datasets/combined.jsonl` contains 48 JSONL records. The telemetry validator accepted all 48 (0 invalid); the prediction loader derived 22 experiment groups and threshold labels:

| Label | Rows |
|---|---:|
| LOW | 16 |
| MEDIUM | 8 |
| HIGH | 24 |

Group sizes are 14 one-row groups, 6 four-row groups, and 2 five-row groups. Thus there are 56 possible pairs of rows within the same multirow experiment group; these are repeated records, not evidence of duplicate collection.

Exact duplicate detection compared the six canonical predictive features (`FEATURE_COLUMNS`) as numeric tuples. Result: **0 exact duplicate feature vectors**.

Near similarity was measured using each feature's observed range in these 48 rows: `max(abs(a_i - b_i) / range_i)` over the six features. A zero-range feature contributes zero when its values match. A pair is reported at threshold `t` only if every normalized feature difference is at most `t`. These are heuristic similarities, not confirmed duplicates:

| Maximum normalized difference | Pairs | Same experiment ID | Different experiment IDs | Different labels |
|---|---:|---:|---:|---:|
| 1% | 33 | 11 | 22 | 0 |
| 5% | 95 | 19 | 76 | 40 |
| 10% | 139 | 21 | 118 | 55 |

At 1%, all 22 cross-group pairs have the same source, requested rate, and derived label. Sixteen are within the same scenario (1 normal, 9 burst, 6 moderate); six compare burst and moderate records. The 11 within-group close pairs are burst records. The wider 5%/10% criteria quickly include records with different labels, illustrating why a closeness threshold cannot establish duplication. No records were removed.

Reproduce the vector-pair counts and fixed-seed evaluation from the repository root:

```python
import itertools
import numpy as np
from ai.prediction import load_person1_dataset, train_candidate_model
from ai.prediction.features import FEATURE_COLUMNS

dataset, groups = load_person1_dataset(
    "telemetry/datasets/combined.jsonl", interface="training-bottleneck"
)
matrix = dataset.loc[:, FEATURE_COLUMNS].to_numpy(dtype=float)
ranges = np.ptp(matrix, axis=0)
pairs = list(itertools.combinations(range(len(dataset)), 2))

def max_normalized_difference(i, j):
    return max(
        abs(matrix[i, k] - matrix[j, k]) / ranges[k] if ranges[k] else 0.0
        for k in range(len(FEATURE_COLUMNS))
    )

print("exact feature pairs:", sum(
    np.array_equal(matrix[i], matrix[j]) for i, j in pairs
))
for threshold in (0.01, 0.05, 0.10):
    close = [
        (i, j) for i, j in pairs
        if max_normalized_difference(i, j) <= threshold
    ]
    print(
        threshold,
        len(close),
        sum(groups.iloc[i] == groups.iloc[j] for i, j in close),
        sum(dataset.congestion.iloc[i] != dataset.congestion.iloc[j]
            for i, j in close),
    )
evaluation = train_candidate_model(
    dataset, groups.tolist(), random_state=42
)["evaluation"]
print(evaluation)
```

The JSONL provides experiment ID, scenario, source, requested rate, and observation timestamp, but has no unique sample/collection lineage identifier. Different IDs and timestamps establish distinct metadata values, not independent collection provenance; the available source data cannot distinguish repeated conditions from a copied/re-collected measurement with certainty.

## Grouped evaluation and interpretation

`train_candidate_model()` selects the canonical features in stable `FEATURE_COLUMNS` order and keeps experiment IDs outside the feature matrix. Its seeded `StratifiedGroupKFold` uses a single two-fold grouped holdout; the observed split has 11 IDs / 24 rows in each partition, with no ID overlap. With `random_state=42`, the current snapshot produced:

- Accuracy: **1.0**
- Macro-F1: **1.0**
- Confusion matrix in `LOW`, `MEDIUM`, `HIGH` order: `[[8, 0, 0], [0, 4, 0], [0, 0, 12]]`
- No grouped-evaluation warnings; repeated execution at the same seed returned identical evaluation metadata.

This is a single holdout, not an average over repeated splits. Group separation prevents an experiment ID from crossing the split, but it does not prevent similar records assigned different IDs from crossing it: 15 of the 22 cross-group pairs meeting the 1% heuristic straddled this holdout. More importantly, labels are generated from utilization, latency, and packet-loss thresholds that are also model inputs. The perfect score therefore demonstrates agreement with those rules on this small dataset, not verified real-world congestion prediction.

`train_random_forest()` remains the ordinary row-level split API and is now explicitly documented as not experiment-group leakage-safe. It must not be presented as group-separated evaluation.

## Telemetry contract review

The Person 1 adapter validates required fields and their types, timezone-bearing ISO-8601 observation timestamps, non-empty experiment/scenario/source/destination/traffic strings, finite non-negative requested rate and packet counters, and canonical numeric feature constraints. Repeated experiment IDs are retained as group membership; IDs are not predictive features. Existing fields such as queue size/drops remain metadata, not model inputs. The adapter and current fixtures preserve the established schema.

The combined dataset passes `telemetry.validate_telemetry` for all 48 records and includes normal, moderate, burst, and congestion scenarios. The separately supplied Person 1 evidence bundle contains optional RTT/queue timestamps and ages in selected interval records. In `traffic/traffic_generator.py`, wall-clock timestamps are assigned after individual monitor reads, while `latest_measurement()` computes sample ages from monotonic time and omits values older than its `max_age`. Unit tests cover stale-value rejection and independent RTT/queue freshness. The JSONL validator checks timestamp syntax and finite non-negative ages; it does not impose a freshness threshold or independently establish timestamp provenance. The bundle's displayed fresh ages do not verify stale behavior in those runs. An observation timestamp still does not prove a receiver measurement-window boundary.

### Supplied Person 1 evidence (selected records only)

The shareable bundle supplied outside the repository was inspected offline. Its manifest, selected state fields, aggregate summaries, interval-selection manifests, selected raw interval records, output inventories, and redacted producer-log excerpts agree on the run identifiers and selected record identities:

| Scenario / source node | State | Manifest-reported source interval records | Aggregate throughput | Packet loss |
|---|---|---:|---:|---:|
| Normal / h2 | completed, exit code 0 | 21 | 2.997186581 Mbps | 0% |
| Moderate / h3 | completed, exit code 0 | 21 | 2.997144514 Mbps | 0% |

For both runs, the state ID matches the run ID, summary ID, and the three selected first/middle/final interval IDs. The selected final interval is explicitly reported in producer logs as a receiver-only post-client drain; it has `throughput_measurement_source="receiver"` and no sender interval/provenance fields. Selected matched rows use `throughput_measurement_source="iperf3_server_json_stream"` and contain the sender interval/provenance fields.

The bundle itself states that `receiver_observation_monotonic_seconds` and its timestamp are callback-observation times, not measurement-window edges; `interval_start_seconds`/`interval_end_seconds` are iperf test-relative offsets, not absolute receiver-monotonic boundaries. The selected records contain no versioned receiver-boundary evidence. These are observations and source-code semantics, not verification of genuine receiver-monotonic measurement boundaries.

The bundle contains SHA-256 values for original state, summary, complete interval-file, and producer-log artifacts, but those original artifacts are not included. The redacted log excerpts and selected records therefore do not allow independent recomputation of every listed original-artifact hash or verification of all 21 source interval rows. No private machine hostname, username, or local path is recorded here.

## Telemetry contract follow-up

1. **Relative offsets vs receiver-monotonic time:** Field names and the producer/parser comments distinguish iperf offsets from callback-observation monotonic time; `receiver_windows.synchronized_receiver_aggregate()` separately requires versioned absolute receiver boundaries and does not accept relative offsets as proof. The JSONL validator does not require an explicit clock-domain marker for the relative offset fields. Do not use them as absolute edges.
2. **Drain vs matched interval:** Existing producer output distinguishes matched intervals by a server-stream throughput source plus sender metadata, and drain intervals by receiver source without sender metadata. The validator accepts known source values, restricts the sender source to `iperf3_client_json_stream`, requires complete sender metadata for a server-stream matched row, rejects contradictory source combinations, and requires matched rows to have ordered, finite, non-negative iperf-relative `interval_start_seconds`/`interval_end_seconds`. Those relative offsets are not absolute receiver-monotonic boundaries. Receiver-only drain rows do not require sender metadata. For legacy records with no source and no sender fields, the validator leaves the match status unknown rather than inferring a drain.
3. **Mixed interval rows:** `validate_file()` compares the required/non-interval schema while allowing declared optional interval fields to vary. It still invokes `_validate_record()` per row, so required fields remain mandatory. Regression tests validate a matched-plus-drain file, reject a missing required field in a mixed file, and reject incomplete or contradictory source metadata.
4. **RTT/queue semantics:** The current producer stores separate wall-clock timestamp strings and monotonic ages for latency and queue readings. The schema only validates their representation/range; it does not specify a cross-source clock/provenance contract or enforce a maximum age at validation time. Existing offline `ScenarioMonitor` tests do exercise stale-value rejection at a configured `max_age`, but neither those tests nor the supplied selected fresh samples establish independent timestamp provenance or stale behavior in live runs.

Only the selected aggregate and first/middle/final interval rows from the supplied Normal/Moderate runs were checked against the offline telemetry validator; the full source interval files were not included. These selected records do not replace full-file, cryptographic, receiver-boundary, or live integration evidence.

## Changes and offline validation

- `ai/prediction/training.py`: reject boolean and coercible-string feature values in grouped candidate evaluation, convert a missing group collection into a clear validation error, and document that ordinary row-level training is not experiment-group leakage-safe.
- `tests/test_prediction.py`: add regression coverage for invalid/missing group collections, empty data, missing/non-numeric/non-finite features, unsupported labels, feature ordering, fixed-seed reproducibility, finite metrics, and fixed class ordering/absent-class behavior. Existing group-disjoint and metadata separation tests remain in place.
- `telemetry/validate_telemetry.py`: align source validation with producer values, validate complete sender-vs-receiver-only metadata combinations, require ordered iperf-relative boundaries on matched server-stream records, and permit declared optional interval fields to vary between rows.
- `tests/test_interval_telemetry.py`: cover matched/drain mixed-file validation, unknown legacy records, required fields, measurement-source combinations, and matched relative interval boundaries.
- `docs/experiments/person2-handoff.md`: record current dataset audit, grouped evaluation, supplied Person 1 evidence, contract gaps, validation results, and handoff requirements.

Commands and results:

- `.\.venv\Scripts\python.exe -m pytest tests/test_interval_telemetry.py tests/test_receiver_windows.py -q` — **89 passed, 0 skipped, 0 warnings**.
- `.\.venv\Scripts\python.exe -m telemetry.validate_telemetry telemetry/datasets/combined.jsonl` — **48 records, 48 valid, 0 invalid**.
- Read-only Python `zipfile` audit of the externally supplied bundle — IDs matched in both runs; all eight selected aggregate/interval records passed `_validate_record()`. This checks selected rows, not the omitted full source files or hashes.
- `.\.venv\Scripts\python.exe -m pytest -q` — **575 passed, 7 skipped, 3 warnings**.
- `git diff --check` — passed.

The full-suite warnings were the existing Starlette/httpx deprecation and two expected RuntimeWarnings from the missing-MEDIUM-class grouped-evaluation test. Skips were reported by pytest; none were forced or suppressed.

The first telemetry-focused run failed because the drain test fixture used placeholder timestamp strings, which correctly failed ISO-8601 validation. The fixture was corrected to use UTC timestamps; the focused and full suites above then passed.

## Remaining work

### Offline

- No source metadata identifies whether near-similar cross-ID rows are repeated conditions or duplicate collection. Keep all rows unless collection provenance establishes invalid duplication.
- The grouped candidate path reports one deterministic holdout, not repeated-split aggregates. Do not report unimplemented cross-validation averages.
- The evidence bundle contains only selected rows and redacted log excerpts, not full original source artifacts. Every source interval row and every original-artifact hash cannot be independently checked from this bundle.

### Runtime evidence still required

1. Genuine receiver-monotonic interval boundaries.
2. Independent RTT/queue measurement timestamp provenance and stale-measurement behavior in live runs.
3. Congestion and Burst runtime validation.
4. End-to-end live ML prediction → QoS application → kernel-state verification → safe restoration.
5. Full-file compatibility and provenance for the Normal/Moderate runs. The selected records in the supplied bundle passed the offline validator, but the full original interval files are absent.

### Person 1 decisions/evidence needed before relying on runtime telemetry

- Confirm what each RTT/queue wall-clock timestamp denotes and the clock/source used; provide evidence for independent measurement provenance.
- Specify the acceptable RTT/queue freshness threshold and the expected behavior when either measurement is stale or missing.
- If full-file integrity or all-row compatibility is required, provide the original source files (or an independently verifiable export) corresponding to the included hash manifest.
- Provide receiver-instrumented monotonic interval-boundary evidence before using interval data for synchronized measurement claims.

The supplied bundle verifies selected Normal and Moderate run completion and record consistency only. No live runtime behavior beyond those reported run artifacts is claimed as verified.
