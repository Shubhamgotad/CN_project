# Adaptive QoS frontend

React, TypeScript, and Vite dashboard for the Adaptive QoS project. The
frontend supports optional read-only data requests plus guarded Traffic Lab
execution requests to the FastAPI service and
continues to support local offline artifact import. The browser never runs
shell commands or changes network configuration; guarded traffic execution is
handled by the loopback API and WSL supervisor.

## Run on Windows PowerShell

From this directory:

```powershell
npm install
npm run dev
```

The frontend uses `http://127.0.0.1:8000` by default for local API requests.
Set `VITE_BACKEND_URL` in `.env.local` only when using a different API origin;
restart Vite after changing it. If the API is stopped, the dashboard reports
the connection error and keeps its charts explicitly labelled as demo fixtures.

Validate the app with:

```powershell
npm run typecheck
npm test
npm run build
```

The dashboard topology is based on the repository Mininet topology
(`h1`-`h4` -> `s1` -> `s2` -> `server`), the 10 Mbps `s1-eth5` bottleneck,
and the server address `10.0.0.5`. No client IP addresses are assumed.

## Backend integration status

The dashboard API is implemented in `dashboard/backend/api.py`. Start it from
the repository root in a separate PowerShell terminal:

```powershell
python -m pip install -r requirements.txt
$env:ADAPTIVE_QOS_REPORT_DIR = "D:\path\to\approved\reports"
# Optional: an explicitly selected existing Person-1 telemetry JSONL file.
$env:ADAPTIVE_QOS_TELEMETRY_FILE = "D:\path\to\telemetry.jsonl"
python -m uvicorn dashboard.backend.api:app --host 127.0.0.1 --port 8000
```

Guarded execution requires the configured WSL distribution (default `Ubuntu`)
to contain this checkout at its normal `/mnt/<drive>/...` mount, plus Mininet,
Open vSwitch, `iperf3`, and the repository's Python dependencies. If the distro
has a different name, set `ADAPTIVE_QOS_WSL_DISTRO` before starting the API.
The API checks the expected Mininet namespaces, host interfaces, server address,
OVS bridges, and ports on every start. It never starts or repairs the topology.
Bind Uvicorn to `127.0.0.1`; mutating requests also require the local Vite
Origin and loopback API Host.

For a recorded-data demonstration, an existing archived Person-1 file such as
`telemetry/datasets/combined.jsonl` can be selected explicitly. Its timestamps
are historical, so the API should report it as stale; it is not live telemetry.
Do not change timestamps to make archived measurements appear fresh.

`ADAPTIVE_QOS_REPORT_DIR` must identify an existing report directory. Report
endpoints return unavailable/error states when it is unset or invalid. The
telemetry endpoint reads only the explicitly configured Person-1 JSONL file;
without it, telemetry is unavailable. The API exposes:

- `GET /api/health`
- `GET /api/topology` (configured topology, not live connectivity)
- `GET /api/telemetry/latest` (only validated configured telemetry)
- `GET /api/reports` and `GET /api/reports/{report_id}` (sanitized projections)
- `GET /api/experiments/status` (campaign completeness separated from evidence
  alignment eligibility)
- `POST /api/experiments/start` (allowlisted `normal`, `congestion`, or `burst`
  producer preset; requires local dashboard Origin)
- `GET /api/execution/active` and `GET /api/execution/{experiment_id}`
- `POST /api/experiments/{experiment_id}/stop` (targets only a tracked run)

Report detail/list responses expose whitelisted fields, not arbitrary report
contents or filesystem paths. Files must be direct regular JSON files in the
configured directory; symlink report files are rejected. The artifact,
topology, telemetry, and campaign routes remain read-only. The guarded routes
invoke only the existing producer with fixed server-side arguments; they do
not expose a shell, arbitrary paths, QoS mutation, topology control, or restore
endpoint. Runs are stored in `..\.adaptive-qos-runs\<experiment-id>\output`
on Windows. The supervisor enforces a 90-second ceiling, persists process
identity across API restarts, and reports `stopped` only after the owned
workload process group is gone.

The existing Python integration is a command-line offline planner, not an HTTP
service. To create an actual model prediction and QoS plan from an existing
Person 1 telemetry JSONL file, run this from the repository root:

```powershell
python -m integration.experiment_runner `
  --input "D:\path\to\telemetry.jsonl" `
  --output "D:\path\to\new-report.json" `
  --mode adaptive `
  --model "ai\prediction\model.joblib" `
  --flow-policy-classes '{"5201":"gaming","5202":"video","5203":"web","5204":"bulk"}' `
  --filter-priorities '{"5201":10,"5202":20,"5203":30,"5204":40}'
```

Use a new output path. The Python runner is dry-run-only: it generates a
planning report and does not execute `tc`, run traffic, collect measurements,
or apply QoS. Import its JSON report on the **AI & QoS** page to inspect the
actual prediction and planned allocations. The report uses the saved model;
it does not retrain it.

For campaign evidence, run the existing `integration.evidence_analysis` or
`integration.campaign_validation` CLI workflows as documented by those tools.
Reports from the configured directory can be retrieved through the API; local
JSON import remains available on **Experiments**. The API returns sanitized
projections and does not revalidate source artifacts. Receiver-boundary
provenance and clock mapping remain unresolved, so API-backed comparisons stay
blocked.

## Demo and safety notes

- Dashboard charts are synthetic **Demo Data**, never live measurements.
- API topology is configured project data, not proof of live connectivity.
- API telemetry is shown only when an explicitly configured file validates;
  stale, unavailable, missing, and request-error states are kept distinct.
- Traffic Lab Preview remains local-only. Start asks for confirmation and sends
  only an allowlisted scenario; the API starts the existing measurement
  producer after its read-only topology check. Stop targets that run's tracked
  process tree. Neither operation changes Mininet, OVS, routes, or `tc` rules.
- Live execution status is separate from telemetry freshness. The producer
  writes measurements under its per-run output directory; the telemetry panel
  continues to read only `ADAPTIVE_QOS_TELEMETRY_FILE`.
- A runner report means a prediction was generated and a policy was planned.
  It does not mean that a policy was applied or restored.
- Missing measurements are shown as unavailable, not as zero.
- Export is withheld because the current project evidence cannot establish
  trustworthy comparable receiver windows.
