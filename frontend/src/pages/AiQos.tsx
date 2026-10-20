import {
  AlertCircle,
  ArrowDownToLine,
  CheckCircle2,
  CircleHelp,
  FileJson2,
  Gauge,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  apiReportToRunnerReport,
  backendConnection,
  getReport,
  getReports,
  parseRunnerReport,
} from "../api/client";
import type { ApiReportSummary } from "../api/client";
import { PROFILE_ALLOCATIONS } from "../data/demo";
import type { CongestionClass, ImportedRunnerReport } from "../types";

const PROFILE_NAMES: CongestionClass[] = ["LOW", "MEDIUM", "HIGH"];

interface AiQosProps {
  report: ImportedRunnerReport | null;
  reportSource: "api" | "imported";
  onReport: (report: ImportedRunnerReport, source: "api" | "imported") => void;
}

export function AiQos({ report, reportSource, onReport }: AiQosProps) {
  const [error, setError] = useState("");
  const [importName, setImportName] = useState("");
  const [selectedRecord, setSelectedRecord] = useState(0);
  const [apiReports, setApiReports] = useState<ApiReportSummary[]>([]);
  const [apiReportMessage, setApiReportMessage] = useState("");
  const [apiError, setApiError] = useState("");
  const [loadingReport, setLoadingReport] = useState("");
  const record = report?.records[selectedRecord] ?? report?.records[0] ?? null;

  useEffect(() => {
    if (!backendConnection.baseUrl) return;
    let active = true;
    getReports().then((response) => {
      if (active) {
        setApiReports(response.reports.filter((item) => item.report_type === "experiment-runner"));
        setApiReportMessage(response.message);
      }
    }).catch((reason: unknown) => {
      if (active) setApiError(reason instanceof Error ? reason.message : "Could not list API reports.");
    });
    return () => {
      active = false;
    };
  }, []);

  async function importReport(file?: File) {
    if (!file) return;
    setError("");
    try {
      const parsed = parseRunnerReport(await file.text());
      onReport(parsed, "imported");
      setImportName(file.name);
      setSelectedRecord(0);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not read the report.");
    }
  }

  async function loadApiReport(reportId: string) {
    setApiError("");
    setLoadingReport(reportId);
    try {
      onReport(apiReportToRunnerReport(await getReport(reportId)), "api");
      setImportName("");
      setSelectedRecord(0);
    } catch (reason) {
      setApiError(reason instanceof Error ? reason.message : "Could not load the API report.");
    } finally {
      setLoadingReport("");
    }
  }

  return (
    <div className="page-stack">
      <section className="page-intro">
        <div>
          <div className="eyebrow"><span className="eyebrow-mark" /> PREDICTION & POLICY PLANNING</div>
          <h1>AI & QoS</h1>
          <p>Inspect real offline runner output separately from policy execution status.</p>
        </div>
        <span className="status-pill status-pill--muted"><ShieldCheck size={14} /> Planning only</span>
      </section>

      {backendConnection.baseUrl && (
        <section className="card api-report-card">
          <div className="section-heading">
            <div><h2>Configured prediction reports</h2><p>Read-only sanitized runner plans from the configured report directory.</p></div>
            <span className="status-pill status-pill--muted">Read-only API</span>
          </div>
          {apiError && <div className="inline-error" role="alert">{apiError}</div>}
          {apiReports.length ? (
            <div className="api-report-list">
              {apiReports.map((item) => (
                <div className="api-report-row" key={item.report_id}>
                  <span><strong>{item.report_id}</strong><small>experiment-runner · {item.status} · planning only</small></span>
                  <button
                    className="button button--secondary"
                    type="button"
                    disabled={loadingReport !== ""}
                    onClick={() => void loadApiReport(item.report_id)}
                  >
                    {loadingReport === item.report_id ? "Loading…" : "Load plan"}
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="api-report-empty">
              {apiError || apiReportMessage || "No experiment-runner reports are available through the configured API."}
            </p>
          )}
        </section>
      )}

      <section className="card artifact-import">
        <div className="artifact-import__copy">
          <span className="section-icon"><FileJson2 size={19} /></span>
          <div>
            <h2>Import an offline prediction report</h2>
            <p>The existing Python runner loads the saved model and plans QoS without applying tc. Import its JSON output here.</p>
          </div>
        </div>
        <label className="button button--secondary upload-button">
          <Upload size={16} /> Choose runner JSON
          <input type="file" accept="application/json,.json" onChange={(event) => void importReport(event.target.files?.[0])} />
        </label>
        {error && <div className="inline-error" role="alert"><AlertCircle size={16} />{error}</div>}
        {importName && <div className="import-success"><CheckCircle2 size={16} /> Imported <strong>{importName}</strong> · values are from the selected report.</div>}
        {report && !importName && (
          <div className="import-success">
            {reportSource === "api" ? "Loaded sanitized plan from the configured read-only API." : "Loaded offline imported plan."}
          </div>
        )}
      </section>

      <section className="ai-status-grid">
        <article className="card ai-state-card">
          <div className="state-card__label">01 / INPUT</div>
          <div className="state-card__icon"><ArrowDownToLine size={19} /></div>
          <h2>Telemetry input</h2>
          {record ? (
            <>
              <strong className="state-main-value">{record.source ?? "Source unavailable"}</strong>
              <p>{record.scenario ?? "Scenario unavailable"} · {record.timestamp ?? "Timestamp unavailable"}</p>
              <span className={`status-pill ${record.freshnessStatus === "fresh" ? "status-pill--surface" : "status-pill--muted"}`}>
                Freshness: {record.freshnessStatus}
                {record.freshnessAgeSeconds !== null ? ` · ${record.freshnessAgeSeconds.toFixed(0)}s` : ""}
              </span>
            </>
          ) : (
            <>
              <strong className="state-main-value state-main-value--empty">No report loaded</strong>
              <p>Measurement freshness is unavailable until a report is imported.</p>
              <span className="status-pill status-pill--muted">Not evaluated</span>
            </>
          )}
        </article>

        <article className="card ai-state-card">
          <div className="state-card__label">02 / MODEL OUTPUT</div>
          <div className="state-card__icon"><Gauge size={19} /></div>
          <h2>Predicted congestion</h2>
          <strong className={`prediction-value ${record?.prediction ? `prediction-value--${record.prediction.toLowerCase()}` : ""}`}>
            {record?.prediction ?? "Unavailable"}
          </strong>
          <p>{record?.confidence !== null && record?.confidence !== undefined
            ? `Confidence ${(record.confidence * 100).toFixed(1)}% · reported by the model`
            : "No model prediction loaded."}</p>
          <span className={`status-pill ${record?.prediction ? "status-pill--surface" : "status-pill--muted"}`}>
            {record?.prediction ? "Prediction generated" : "Awaiting runner report"}
          </span>
        </article>

        <article className="card ai-state-card">
          <div className="state-card__label">03 / POLICY PLAN</div>
          <div className="state-card__icon"><ShieldCheck size={19} /></div>
          <h2>QoS policy</h2>
          <strong className="state-main-value">{record?.prediction ? `${record.prediction} profile` : "Not selected"}</strong>
          <p>{record?.flowAllocationsMbps ? "Per-flow allocation from imported plan." : "The profile plan is shown when a report is loaded."}</p>
          <span className={`status-pill ${record?.status === "planned" ? "status-pill--surface" : "status-pill--muted"}`}>
            {record?.status === "planned" ? "Policy planned" : "No plan loaded"}
          </span>
        </article>

        <article className="card ai-state-card">
          <div className="state-card__label">04 / EXECUTION</div>
          <div className="state-card__icon"><CircleHelp size={19} /></div>
          <h2>Application & restore</h2>
          <strong className="state-main-value state-main-value--empty">Not applied</strong>
          <p>The runner is dry-run-only. No live application or restoration is reported.</p>
          <span className="status-pill status-pill--muted">Not applicable · offline</span>
        </article>
      </section>

      {report && (
        <section className="card plan-card">
          <div className="section-heading">
            <div><h2>Imported plan details</h2><p>Run {report.runId} · {report.status} · mode {report.mode}</p></div>
            {report.records.length > 1 && (
              <label className="record-select">Record
                <select value={selectedRecord} onChange={(event) => setSelectedRecord(Number(event.target.value))}>
                  {report.records.map((item, index) => <option key={item.line} value={index}>Line {item.line} · {item.source ?? "source unknown"}</option>)}
                </select>
              </label>
            )}
          </div>
          {record?.flowAllocationsMbps ? (
            <div className="allocation-table-wrap">
              <table className="data-table">
                <thead><tr><th>Flow / port</th><th>Service</th><th>Planned allocation</th><th>Execution</th></tr></thead>
                <tbody>
                  {Object.entries(record.flowAllocationsMbps).map(([port, allocation]) => {
                    const serviceByPort: Record<string, string> = { "5201": "Gaming", "5202": "Video", "5203": "Web", "5204": "Bulk" };
                    return <tr key={port}><td>UDP {port}</td><td>{serviceByPort[port] ?? "Unmapped"}</td><td>{allocation.toFixed(2)} Mbps</td><td><span className="status-pill status-pill--muted">Not applied</span></td></tr>;
                  })}
                  <tr><td>Default class 1:15</td><td>Unmatched</td><td>{record.prediction ? PROFILE_ALLOCATIONS[record.prediction].unmatched.toFixed(2) : "1.00"} Mbps</td><td><span className="status-pill status-pill--muted">Not applied</span></td></tr>
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-inline"><CircleHelp size={18} /><span>This record has no adaptive flow allocation plan.</span></div>
          )}
          <p className="report-integrity-note">
            Report fields are displayed as supplied. This browser does not re-run prediction, verify referenced evidence files, or apply/restore policy.
          </p>
        </section>
      )}

      <section className="section-heading dashboard-section-heading">
        <div><h2>Available managed profiles</h2><p>Allocations from the existing planner; rates are not evidence of applied bandwidth.</p></div>
        <span className="status-pill status-pill--surface">10 Mbps total incl. unmatched</span>
      </section>
      <div className="profile-grid">
        {PROFILE_NAMES.map((profile) => (
          <article className={`card profile-card ${record?.prediction === profile ? "profile-card--selected" : ""}`} key={profile}>
            <div className="profile-card__header"><span>{profile}</span><small>Service rates · Mbps</small></div>
            <div className="profile-allocation">
              {(["gaming", "video", "web", "bulk"] as const).map((service) => (
                <div key={service}><span>{service}</span><strong>{PROFILE_ALLOCATIONS[profile][service].toFixed(2)}</strong></div>
              ))}
              <div className="profile-allocation__unmatched"><span>Unmatched · 1:15</span><strong>{PROFILE_ALLOCATIONS[profile].unmatched.toFixed(2)}</strong></div>
            </div>
            <p>{record?.prediction === profile ? "Selected in imported plan" : "Planner profile"}</p>
          </article>
        ))}
      </div>
    </div>
  );
}
