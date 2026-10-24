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
- Baseline: `4fc7aba`
- Changes in this handoff: documentation only, unless explicitly noted otherwise.

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

Person 1 should provide representative interval JSONL records and test logs showing normal UDP traffic, congestion UDP traffic, interval boundaries, independent RTT/queue timestamps, stale-measurement behavior, and final aggregate telemetry compatibility.

## 6. Offline evidence and experiment-campaign analysis

Evidence and campaign analysis should validate source provenance, required fields, experiment alignment, and completeness before comparing runs. Incomplete or mismatched evidence must not be presented as a valid baseline-versus-adaptive comparison.

A campaign should include at least three complete cycles and at least 48 trials, subject to the project's campaign-validation requirements. These thresholds are requirements for evidence collection, not proof that a campaign has been completed.

Report measured QoS changes only after the underlying Linux/Mininet evidence has been collected and validated. Offline tests, dashboard demo data, model-artifact presence, and successful API startup do not establish live runtime readiness or demonstrate QoS improvement.

Relevant documentation and runtime evidence should be linked from the handoff when available. Keep offline validation results separate from Person 1's live integration results.
