# Controlled congestion-label data collection

## Purpose and status

Collect independent, measured LOW, MEDIUM, and HIGH examples for evaluation of
the threshold-derived congestion labels. This document describes planned work;
no experiments were run as part of the ML pipeline implementation.

The labels are assigned from measured utilization, latency, and packet loss.
A trial is MEDIUM only when its measured threshold score is 2 or 3. The
scenario name or offered traffic rate does not determine the label.

## Receiver/client measurements and scenario commands

Run commands from the repository root while the existing Mininet topology is
active with hosts `h1`-`h4` and `server` (`server` uses `10.0.0.5`). The
traffic generator owns the iperf3 receiver listeners for each run. Before
starting a listener it checks that its port is free; an already-listening
port is reported rather than being shared or silently reused. Listeners use
`iperf3 -s -1 --json-stream` in the `server` namespace and exit after their
single owned client completes; startup failures, timeouts, and interruptions
are also cleaned up.

Client JSON-stream interval events are authoritative for matched interval
boundaries, observation timestamps, and throughput. Matching server-side
interval events provide the UDP receiver's measured jitter, lost packets,
packet count, and loss percentage. Receiver-only post-client drain intervals
retain their receiver-observed boundaries, timestamp, and throughput and are
marked with `throughput_measurement_source=receiver`. The records are paired by
flow and interval start/end; the
congestion flow ports are 5201-5204 and the single-client scenarios use 5201.
An unmatched active interval or receiver event missing UDP metrics fails the
run with the flow, port, experiment ID, and interval identified. Receiver-only
intervals are accepted only when they begin at or after the client's final
interval boundary (within 50 ms); they represent receiver drain time, not
traffic intervals with missing client metrics. RTT and queue values continue
to come from the existing freshness-checked monitor. These interval samples are
written to separate `*_intervals.jsonl` files. The existing
scenario JSONL aggregate files and their schemas are unchanged: aggregate
throughput/loss/jitter still comes from each client's completed iperf3 output.

Run any scenario from the repository root:

```text
python traffic/traffic_generator.py --scenario normal
python traffic/traffic_generator.py --scenario moderate
python traffic/traffic_generator.py --scenario moderate --rate-mbps 7.5
python traffic/traffic_generator.py --scenario burst
python traffic/traffic_generator.py --scenario congestion
```

`normal` sends one 3 Mbps flow for 20 seconds; `moderate` sends one 6 Mbps
flow for 20 seconds by default (or the supplied `--rate-mbps`); `burst` runs
five 3-second 8 Mbps bursts; and `congestion` runs four simultaneous 4 Mbps
flows from `h1`-`h4` for 20 seconds.

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