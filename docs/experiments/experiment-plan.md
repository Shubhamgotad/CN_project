# Controlled congestion-label data collection

## Purpose and status

Collect independent, measured LOW, MEDIUM, and HIGH examples for evaluation of
the threshold-derived congestion labels. This document describes planned work;
no experiments were run as part of the ML pipeline implementation.

The labels are assigned from measured utilization, latency, and packet loss.
A trial is MEDIUM only when its measured threshold score is 2 or 3. The
scenario name or offered traffic rate does not determine the label.

## Fixed network conditions

- Use the existing four-client/two-switch topology.
- Keep the inter-switch bottleneck at 10 Mbps and configured delay at 10 ms.
- Do not change or manually apply `tc`/qdisc settings for this experiment.
- Use the existing 20-second moderate scenario duration.

## Independent moderate trials

From the repository root, run each command separately and wait for it to finish
before starting the next trial. Each invocation starts one 20-second UDP flow
from `h1` and creates a fresh UUID experiment ID. `moderate.jsonl` is opened in
append mode at a path anchored to the project directory, so running from another
working directory does not redirect output or replace prior records.

```text
python traffic/traffic_generator.py --scenario moderate
python traffic/traffic_generator.py --scenario moderate --rate-mbps 7
python traffic/traffic_generator.py --scenario moderate --rate-mbps 7.5
```

The first command is an independent 6 Mbps baseline run; the existing recorded
6 Mbps observation remains in the same JSONL file. Every successful invocation
appends exactly one record with its experiment ID printed to the console. Keep
all measured outcomes; derive labels only from the existing measured-field
threshold rules. Do not overwrite, copy, or manually relabel telemetry.

After the three commands complete, rebuild the combined data from the scenario
files, validate its schema, and report labels using the existing labeling
logic:

```text
python telemetry/build_dataset.py
python telemetry/validate_telemetry.py telemetry/datasets/combined.jsonl
python telemetry/report_congestion_labels.py telemetry/datasets/combined.jsonl
```

The combined builder regenerates only `combined.jsonl` and `combined.csv`;
scenario source files, including `moderate.jsonl`, are inputs and remain
append-only. The report exits with an error if any record fails validation and
prints LOW/MEDIUM/HIGH counts plus the number of experiment IDs. If either new
rate produces MEDIUM, collect at least two more independent runs at that rate
to improve experiment-group support. If neither does, select a follow-up rate
based on the observed metrics; do not alter thresholds or fabricate labels.
Record run date, exact command, topology state, exit status, and printed
experiment ID. A failed/incomplete run is not a successful sample. Do not
interpret the scenario-wide `queue_drops` count as a per-flow measurement or
model input.

## Evaluation

Use experiment ID as the group key. All records from an experiment must remain
in the same train or test partition. Report the full class distribution,
train/test class support, per-class precision/recall/F1, confusion matrix,
accuracy, and macro-F1. Warn and qualify results when any class is absent or
cannot be represented in both partitions. Because labels are generated from
the predictor inputs, these metrics assess agreement with the labeling rules,
not independent proof of future congestion prediction.