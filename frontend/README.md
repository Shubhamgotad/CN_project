# Adaptive QoS frontend

React, TypeScript, and Vite dashboard for the Adaptive QoS project. The
frontend supports optional read-only requests to the FastAPI service and
continues to support local offline artifact import. It does not execute network
actions.

## Run on Windows PowerShell

From this directory:

```powershell
npm install
npm run dev
```

To connect to the API, copy `.env.example` to `.env.local` and keep
`VITE_BACKEND_URL` set to the API origin. Restart Vite after changing it.
Without this setting, the app makes no API requests and remains in offline mode.

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

The read-only API is implemented in `dashboard/backend/api.py`. Start it from
the repository root in a separate PowerShell terminal:

```powershell
python -m pip install -r requirements.txt
$env:ADAPTIVE_QOS_REPORT_DIR = "D:\path\to\approved\reports"
# Optional: an explicitly selected existing Person-1 telemetry JSONL file.
$env:ADAPTIVE_QOS_TELEMETRY_FILE = "D:\path\to\telemetry.jsonl"
python -m uvicorn dashboard.backend.api:app --host 127.0.0.1 --port 8000
```

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

Report detail/list responses expose whitelisted fields, not arbitrary report
contents or filesystem paths. Files must be direct regular JSON files in the
configured directory; symlink report files are rejected. The API is read-only
and has no traffic, QoS mutation, shell, or restore endpoint.

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
- Traffic Lab previews settings only. Start/stop controls are disabled; the
  frontend does not launch a shell, Mininet, OVS, `tc`, or iperf.
- A runner report means a prediction was generated and a policy was planned.
  It does not mean that a policy was applied or restored.
- Missing measurements are shown as unavailable, not as zero.
- Export is withheld because the current project evidence cannot establish
  trustworthy comparable receiver windows.
