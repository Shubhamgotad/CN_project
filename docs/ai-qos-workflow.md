# Adaptive QoS AI & QoS workflow

## Data flow and evidence

Traffic Lab starts an allowlisted experiment through the Windows FastAPI service and WSL supervisor. The supervisor records `state.json` in a UUID directory under `.adaptive-qos-runs` beside the repository. The state must identify the same UUID, use execution contract version 3, finish with status `completed`, `process_alive: false`, exit code 0, a timezone-aware finish time, a supported scenario, and validated source-host/flow metadata. Before reporting measurement collection as available, the supervisor validates the persisted `output/<scenario>.jsonl`: every required flow must have a timezone-aware, schema-valid receiver measurement with the same experiment ID and scenario, the bottleneck interface, and the expected iperf/tc measurement source. A zero producer exit code by itself does not prove telemetry was collected.

The producer runs UDP clients in the selected Mininet host namespaces and iperf3 UDP servers on the receiving side. Receiver interval events are matched against sender intervals; the scenario aggregate is persisted once per flow to the run's scenario JSONL. Congestion retains separate rows for h1-h4. Each aggregate carries receiver throughput, latency, jitter, packet loss, measured queue size/drops, the configured 10 Mbps `s1-eth5` capacity denominator, and measurement-source/provenance metadata. The separate interval JSONL retains aligned interval-level sender, receiver, and queue observations.

`python report.py` selects the uniquely newest completed run by timezone-aware `finished_at`, validates lifecycle order and the state `output_directory`, then reads only that run's scenario-named JSONL. If that run's state or any source row is invalid, it fails instead of choosing an older run. All complete records are inputs to inference; every row must match the run ID/scenario/source flows, be no later than `finished_at`, contain valid required measurements and queues, and meet freshness limits (maximum age 300 seconds, future skew 30 seconds). It then invokes the checked-in saved model and existing policy mappings in dry-run mode. It does not modify telemetry, start traffic, run `tc`, or apply QoS. The atomic report records the experiment, scenario, source hosts, row count and latest measurement timestamp, as well as per-record model predictions and planned allocations.

Historical runs created before the experiment-ID propagation fix may have a state/folder ID that differs from the producer-generated UUID in their telemetry. The change is in commit `2eea706` (`fix: propagate experiment IDs to traffic telemetry`): the supervisor now forwards `--experiment-id` through to each scenario. Some older artifacts carry contract version 3 despite being created before that fix, so the version number alone is not proof of identity; the CLI validates every row against state. Historical files remain unchanged and are rejected with expected/observed identity diagnostics.

## Required configuration

Set these variables in the environment inherited by both the API process and `python report.py`:

| Variable | Required | Behavior |
| --- | --- | --- |
| `ADAPTIVE_QOS_REPORT_DIR` | Yes for CLI report generation and API discovery | Absolute path to an existing approved directory outside the repository. The CLI will not create it. The API reports `unset`, `invalid`, `inaccessible`, or `valid`; a valid empty folder is distinct from an unavailable folder. |
| `ADAPTIVE_QOS_TELEMETRY_FILE` | No | When unset, the API discovers completed Traffic Lab telemetry. When set, it pins live telemetry endpoints to this one JSONL file; the API validates it and does not mix in auto-discovered experiment data. For `report.py`, a set value must resolve to the exact scenario file in the selected completed run; a copied/shared file or another run's output is rejected even if its schema is otherwise valid. |
| `ADAPTIVE_QOS_ENABLE_APPLICATION` | No; leave unset/disabled for this project | Must equal `1` before the backend considers application authorization. It does not override the exclusive ownership gate. |

The configured report folder is an operator-approved location; it must be absolute and existing. Paths inside the repository or Traffic Lab evidence directory, symbolic-link paths, missing paths, and unreadable paths are rejected. Public report API responses expose report identifiers and sanitized report fields, not local directory, telemetry or model paths. The API reads environment configuration at request time; for production, configure variables before starting/restarting the API so all processes have the same settings.

## Experiments page data sources

`GET /api/reports` lists three distinct document kinds. `experiment-runner` files contain offline saved-model predictions and planned allocations; they are shown separately in Experiments and never counted as evidence reports. `evidence-analysis` and `campaign-validation` documents are loaded as campaign evidence. A prediction report cannot be imported as a campaign report. Manual import accepts the documented version 1 evidence JSON and reports schema errors instead of treating arbitrary JSON as validated evidence.

`GET /api/experiments/runs` lists validated Traffic Lab supervisor states. Completed, failed and stopped counts refer only to execution states; they are not campaign trial counts. The required campaign comprises 48 manifested trials across Baseline, QoS LOW, QoS MEDIUM and QoS HIGH (three complete cycles). One or many ordinary single-scenario Traffic Lab executions do not establish those strata or their campaign provenance. Campaign trial counts remain unavailable until a campaign-validation or evidence-analysis report is loaded.

Dashboard and AI & QoS use the validated latest-measurement API (AI & QoS consumes the same result through the dry-run decision endpoint). Traffic Lab scopes the same API to the just-completed experiment ID and displays the returned per-flow records, so a four-flow Congestion execution remains four measurements rather than a single host presented as an aggregate. Responses distinguish `fresh`, `stale`, `no_measurement`, and `collection_failed`; stale, missing, and failed collection never produce a QoS proposal. An explicit `ADAPTIVE_QOS_TELEMETRY_FILE` remains pinned for unscoped latest requests. If its experiment ID conflicts with a newer run, the API reports both identities; the experiment-scoped Traffic Lab request reads only the requested run output and also reports the override conflict rather than silently relabeling the pinned file.

An empty run history is returned as an available response with zero counts. If the Experiments page reports a run-history request error (especially HTTP 404), verify `VITE_BACKEND_URL` and restart the API from this project's root so the frontend reaches the backend that registers `/api/experiments/runs`.

The backend currently reports campaign comparison eligibility as blocked because producer provenance and clock mapping are not independently validated. Campaign completeness and artifact/hash checks do not establish comparable measurement windows; the UI must not enable comparisons or claim improvement until those gates pass.

To persist a campaign validator result into the configured report directory without replacing an existing file:

```powershell
python -m integration.campaign_validation --manifest C:\path\to\campaign\campaign.json --json-output "$env:ADAPTIVE_QOS_REPORT_DIR\campaign-validation.json"
```

The report directory and the campaign manifest/evidence must be on distinct paths; the output command refuses existing filenames, repository paths, and paths inside the campaign evidence directory. After the validation command, choose **Refresh reports** on the Experiments page. An incomplete validation report is still inspectable with its failures; its nonzero process status means it did not complete the campaign.

Example PowerShell setup (choose a real absolute directory and create it yourself):

```powershell
$env:ADAPTIVE_QOS_REPORT_DIR = "$HOME\Documents\CN-Reports"
New-Item -ItemType Directory -Force $env:ADAPTIVE_QOS_REPORT_DIR | Out-Null
Remove-Item Env:ADAPTIVE_QOS_TELEMETRY_FILE -ErrorAction SilentlyContinue
```

For a deliberately pinned file, use its exact completed-run file path:

```powershell
$env:ADAPTIVE_QOS_TELEMETRY_FILE = "C:\path\to\repo-parent\.adaptive-qos-runs\<experiment-id>\output\<scenario>.jsonl"
```

## Run the workflow

From the repository root, start the API and frontend in separate terminals:

```powershell
python -m uvicorn dashboard.backend.api:app --host 127.0.0.1 --port 8000
```

```powershell
cd frontend
npm run dev
```

Set `VITE_BACKEND_URL=http://127.0.0.1:8000` in `frontend/.env.local` if using a non-default backend address. In Traffic Lab, choose a supported scenario and source host, start the experiment, and wait for the run to reach `completed`. The supported CLI path is offline and never launches an experiment.

With the same configured environment, generate a report from the repository root:

```powershell
python report.py
```

Open **AI & QoS** and choose **Refresh reports**. A successfully persisted, schema-valid report appears in the configured report list and can be opened using **Load plan**. Manual JSON import remains an alternative. Refresh again after generating a report outside the app; no file-watcher is currently wired between the CLI and browser.

## Interpret the page

- **Live telemetry and rule-based classification** come from `/api/telemetry/latest` and `/api/qos/decision`. The thresholds produce a rule classification; this is not an ML prediction.
- **Offline ML predictions** come only from a validated persisted report or a manually imported report. Importing a report displays its stored output; it does not rerun the model. Experiment ID, scenario, source host, measurement timestamp, freshness, confidence (when supplied), and input count remain evidence metadata.
- **Policy allocations** are planned rates from the saved-model planner. They do not prove bandwidth was applied.
- Traffic Lab's selected rate is a per-flow offered-load target. Aggregate offered load is that target times the actual selected flow count; measured throughput and utilization come from telemetry. Neither changes the configured 10 Mbps `s1`–`s2` bottleneck.
- Managed profiles budget 10 Mbps total: 9 Mbps across service classes using the selected profile's weights, plus a fixed 1 Mbps unmatched reserve. Service-class HTB ceilings equal their rates, so idle capacity in one class may not be borrowed by another.
- New adaptive runner reports carry the planner's total bandwidth and service-pool policy weights alongside the planned class rates. The API validates that class rates plus the unmatched reserve fit that budget and correspond to the stored weights. Older reports without this provenance remain readable; the dashboard labels their budget rationale unavailable instead of inferring it.
- **Application and restoration** are shown only from controller/backend state with verified kernel readback. Offline reports always remain planning-only.

Live telemetry freshness is evaluated against current time at API read. Offline report freshness is evaluated from the recorded measurement timestamps when the report is generated; an older report can later become stale and its original measurement time is never refreshed or rewritten.

## Apply safety

The Apply button remains disabled while `LIVE_TC_OWNERSHIP_GUARANTEED` is false. Backend status lists the independent blockers: application authorization and exclusive `tc` ownership. Setting the environment flag alone cannot enable application. Safe live enablement requires a dedicated ownership mechanism that excludes external `tc` writers, validated preflight against the exact original state, kernel verification after application, durable ownership/state tracking, and tested restoration/rollback on errors and shutdown. Those guarantees are not currently provided for the Mininet interface.

## Troubleshooting

- **Report directory unset:** set `ADAPTIVE_QOS_REPORT_DIR` to an existing approved directory outside the repository in the API and CLI environments.
- **Directory invalid or inaccessible:** verify the absolute path exists, is a directory, is not reached through a symbolic link, is outside the checkout, and grants the API user read/traverse access.
- **Valid directory, no reports:** generate a fresh report, then use **Refresh reports**. Invalid JSON and unsupported schemas are not offered as compatible reports.
- **Telemetry stale:** run and complete a new Traffic Lab experiment; do not edit timestamps to pass freshness checks.
- **The decision says the configured telemetry file is pinned and stale:** the API is honoring `ADAPTIVE_QOS_TELEMETRY_FILE` and intentionally will not mix in completed-run discovery. In the backend's startup shell, run `Remove-Item Env:ADAPTIVE_QOS_TELEMETRY_FILE -ErrorAction SilentlyContinue` and restart the backend to use automatic discovery, or point the setting at the exact scenario JSONL from the intended completed run. The AI & QoS page's **Refresh live telemetry** button re-reads the backend after the configuration is corrected.
- **Invalid run identity or missing scenario JSONL:** compare `state.json` experiment ID/scenario and `output/<scenario>.jsonl`; the CLI deliberately refuses to fall back to older run evidence.
- **Configured telemetry mismatch:** clear `ADAPTIVE_QOS_TELEMETRY_FILE` for automatic latest-run discovery, or configure the exact JSONL file belonging to the newest completed run.
- **Report says expected and observed experiment IDs differ:** check the diagnostic's selected run and scenario-file name. If it is execution contract v2, its producer did not receive the supervisor's experiment ID; keep the historical artifact unchanged and run a new experiment through the current Traffic Lab execution service (contract v3). If the selected run is contract v3, verify the `output_directory` in its `state.json` and the producer log; do not copy telemetry between run folders.
- **Model load or inference failure:** confirm `ai/prediction/model.joblib` is present and its installed Python dependencies match the project environment. A failed inference cannot produce a successful plan.
- **Invalid report:** inspect the CLI error and the untrusted source JSON locally. The backend will not display reports whose safety claims, IDs, timestamps, predictions or plan schema fail validation.

## Tests

Run backend tests from the repository root with `python -m pytest -q`. From `frontend`, run `npm test`, `npm run typecheck`, and `npm run build`.
