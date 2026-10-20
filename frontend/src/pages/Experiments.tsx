import { AlertTriangle, ChartNoAxesCombined, FileUp, LockKeyhole } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useMemo, useState } from "react";
import { useEffect } from "react";
import {
  apiReportToEvidenceReport,
  backendConnection,
  getExperimentsStatus,
  getReport,
  getReports,
  parseEvidenceReport,
} from "../api/client";
import type { ApiExperimentsStatus, ApiReportSummary } from "../api/client";
import type { ImportedEvidenceReport } from "../types";

interface ExperimentsProps {
  report: ImportedEvidenceReport | null;
  reportSource: "api" | "imported";
  onReport: (report: ImportedEvidenceReport, source: "api" | "imported") => void;
}

function mean(values: (number | null)[]) {
  const available = values.filter((value): value is number => value !== null);
  return available.length ? available.reduce((total, value) => total + value, 0) / available.length : null;
}

function formatMetric(value: number | null, unit: string) {
  return value === null ? "Unavailable" : `${value.toFixed(2)} ${unit}`;
}

export function Experiments({ report, reportSource, onReport }: ExperimentsProps) {
  const [error, setError] = useState("");
  const [importName, setImportName] = useState("");
  const [apiError, setApiError] = useState("");
  const [apiReports, setApiReports] = useState<ApiReportSummary[]>([]);
  const [apiReportMessage, setApiReportMessage] = useState("");
  const [apiStatus, setApiStatus] = useState<ApiExperimentsStatus | null>(null);
  const [loadingReport, setLoadingReport] = useState("");

  useEffect(() => {
    if (!backendConnection.baseUrl) return;
    let active = true;
    getReports().then((response) => {
      if (!active) return;
      setApiReports(response.reports.filter((item) => (
        item.report_type === "campaign-validation" || item.report_type === "evidence-analysis"
      )));
      setApiReportMessage(response.message);
    }).catch((reason: unknown) => {
      if (active) setApiError(reason instanceof Error ? reason.message : "Could not list API reports.");
    });
    getExperimentsStatus().then(
      (response) => active && setApiStatus(response),
      (reason: unknown) => active && setApiError(
        reason instanceof Error ? reason.message : "Could not load experiment status.",
      ),
    );
    return () => {
      active = false;
    };
  }, []);

  async function importReport(file?: File) {
    if (!file) return;
    setError("");
    try {
      onReport(parseEvidenceReport(await file.text()), "imported");
      setImportName(file.name);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not read the report.");
    }
  }

  async function loadApiReport(reportId: string) {
    setApiError("");
    setLoadingReport(reportId);
    try {
      onReport(apiReportToEvidenceReport(await getReport(reportId)), "api");
      setImportName("");
    } catch (reason) {
      setApiError(reason instanceof Error ? reason.message : "Could not load the API report.");
    } finally {
      setLoadingReport("");
    }
  }

  const summary = useMemo(() => {
    if (!report?.comparisonReady) return [];
    const groups = new Map<string, (number | null)[]>();
    for (const trial of report.trials) {
      const label = trial.condition.toLowerCase() === "baseline"
        ? "Baseline"
        : `${trial.condition} / ${trial.profile}`;
      const entries = groups.get(label) ?? [];
      entries.push(...trial.flows.map((flow) => flow.throughputMbps));
      groups.set(label, entries);
    }
    return [...groups.entries()].map(([condition, values]) => ({
      condition,
      throughput: mean(values),
    })).filter((row) => row.throughput !== null);
  }, [report]);

  const trialRows = report?.trials.flatMap((trial) => trial.flows.map((flow) => ({
    ...flow,
    trialId: trial.trialId,
    condition: trial.condition,
    profile: trial.profile,
    repetition: trial.repetitionId,
    trialStatus: trial.status,
    alignment: trial.alignment,
  }))) ?? [];

  return (
    <div className="page-stack">
      <section className="page-intro">
        <div>
          <div className="eyebrow"><span className="eyebrow-mark" /> CAMPAIGN EVIDENCE</div>
          <h1>Experiments</h1>
          <p>Review baseline and QoS artifacts without overstating what the evidence validates.</p>
        </div>
        <span className="status-pill status-pill--blocked"><LockKeyhole size={14} /> Comparison blocked</span>
      </section>

      <section className="comparison-gate card">
        <span className="comparison-gate__icon"><AlertTriangle size={20} /></span>
        <div>
          <strong>Receiver alignment is not independently validated</strong>
          <p>The receiver-boundary producer and clock validation remain unresolved. Campaign structure or valid file hashes alone do not establish comparable measurement windows or prove QoS improvement.</p>
          <div className="gate-chips">
            <span>Producer provenance: not validated</span>
            <span>Clock mapping: not validated</span>
            <span>QoS improvement: not claimed</span>
          </div>
        </div>
      </section>

      {backendConnection.baseUrl && (
        <section className="card api-report-card">
          <div className="section-heading">
            <div>
              <h2>Configured report directory</h2>
              <p>{apiReportMessage || "Loading sanitized report summaries from the read-only API…"}</p>
            </div>
            <span className="status-pill status-pill--muted">Read-only API</span>
          </div>
          {apiError && <div className="inline-error" role="alert">API request error: {apiError}</div>}
          {apiStatus && (
            <div className="api-report-status">
              <span>Campaign completeness <strong>{apiStatus.campaign_completeness}</strong></span>
              <span>Producer provenance <strong>{apiStatus.producer_provenance_status}</strong></span>
              <span>Clock mapping <strong>{apiStatus.clock_mapping_status}</strong></span>
              <span>Alignment eligibility <strong>{apiStatus.measurement_alignment_eligibility}</strong></span>
            </div>
          )}
          {apiReports.length ? (
            <div className="api-report-list">
              {apiReports.map((item) => (
                <div className="api-report-row" key={item.report_id}>
                  <span><strong>{item.report_id}</strong><small>{item.report_type} · {item.status} · alignment {item.alignment_eligibility}</small></span>
                  <button
                    className="button button--secondary"
                    type="button"
                    disabled={loadingReport !== ""}
                    onClick={() => void loadApiReport(item.report_id)}
                  >
                    {loadingReport === item.report_id ? "Loading…" : "Load report"}
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="api-report-empty">
              {apiError || "No supported evidence reports are available from the configured API directory."}
            </p>
          )}
        </section>
      )}

      <section className="card artifact-import">
        <div className="artifact-import__copy">
          <span className="section-icon"><FileUp size={19} /></span>
          <div>
            <h2>Import an offline evidence report</h2>
            <p>Choose a JSON report from the existing evidence analyzer or campaign validator. The browser parses its summary but does not revalidate source artifacts.</p>
          </div>
        </div>
        <label className="button button--secondary upload-button">
          <FileUp size={16} /> Choose analysis JSON
          <input type="file" accept="application/json,.json" onChange={(event) => void importReport(event.target.files?.[0])} />
        </label>
        {error && <div className="inline-error" role="alert">{error}</div>}
        {importName && <div className="import-success">Imported <strong>{importName}</strong> · browser display only</div>}
      </section>

      {report ? (
        <>
          <div className="report-source-banner">
            <span className="status-pill status-pill--surface">
              {reportSource === "api" ? "Configured API report" : "Imported offline report"}
            </span>
            <span>
              {reportSource === "api"
                ? "Sanitized by the backend API; campaign and comparison eligibility remain separately gated."
                : importName
                  ? `Browser-imported artifact: ${importName}`
                  : "Browser-imported offline artifact"}
            </span>
          </div>
          <div className="results-summary-grid">
            <article className="card result-stat"><span>Report status</span><strong>{report.reportStatus}</strong><small>Offline analysis artifact</small></article>
            <article className="card result-stat"><span>Trials in report</span><strong>{report.trials.length}</strong><small>Imported trial records</small></article>
            <article className="card result-stat"><span>Measurement alignment</span><strong>{report.alignmentStatus}</strong><small>Eligibility is separately gated</small></article>
            <article className="card result-stat"><span>Comparison readiness</span><strong>{report.comparisonReady ? "Eligible" : "Blocked"}</strong><small>No export unless eligible data exists</small></article>
          </div>
          {report.comparisonReady && summary.length > 0 ? (
            <section className="card chart-card results-chart">
              <div className="section-heading"><div><h2>Aligned throughput summary</h2><p>Mean reported receiver throughput by condition, from eligible imported trials.</p></div><span className="unit-label">Mbps</span></div>
              <div className="chart-frame chart-frame--large">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={summary} margin={{ top: 12, right: 12, left: -12, bottom: 2 }}>
                    <CartesianGrid stroke="#D6BFA9" strokeDasharray="3 5" vertical={false} />
                    <XAxis dataKey="condition" tickLine={false} axisLine={false} tick={{ fill: "#704B3A", fontSize: 11 }} />
                    <YAxis tickLine={false} axisLine={false} tick={{ fill: "#704B3A", fontSize: 11 }} />
                    <Tooltip contentStyle={{ border: "1px solid #704B3A", borderRadius: 10, background: "#F4EDE4" }} formatter={(value) => [`${Number(value).toFixed(2)} Mbps`, "Mean throughput"]} />
                    <Bar dataKey="throughput" fill="#704B3A" radius={[6, 6, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </section>
          ) : (
            <section className="card blocked-results">
              <span><LockKeyhole size={18} /></span>
              <div><strong>Comparison charts and export are unavailable</strong><p>Only reports that explicitly establish measurement alignment eligibility can support a baseline-versus-QoS comparison. Current imported structure: {report.reportStatus}; alignment: {report.alignmentStatus}.</p></div>
            </section>
          )}

          <section className="card table-card">
            <div className="section-heading">
              <div><h2>Trial history & reported flow metrics</h2><p>Rows remain descriptive when alignment is blocked; no comparative conclusion is drawn.</p></div>
              <span className="status-pill status-pill--muted">{trialRows.length} flow records</span>
            </div>
            {trialRows.length ? (
              <div className="table-scroll">
                <table className="data-table">
                  <thead><tr><th>Trial</th><th>Repetition</th><th>Condition / profile</th><th>Flow</th><th>Throughput</th><th>Latency</th><th>Jitter</th><th>Packet loss</th><th>Alignment</th></tr></thead>
                  <tbody>
                    {trialRows.map((row, index) => (
                      <tr key={`${row.trialId}-${row.flowId}-${index}`}>
                        <td>{row.trialId}<small>{row.trialStatus}</small></td>
                        <td>{row.repetition}</td>
                        <td>{row.condition} / {row.profile}</td>
                        <td>{row.flowId}</td>
                        <td>{formatMetric(row.throughputMbps, "Mbps")}</td>
                        <td>{formatMetric(row.latencyMs, "ms")}</td>
                        <td>{formatMetric(row.jitterMs, "ms")}</td>
                        <td>{formatMetric(row.packetLossPercent, "%")}</td>
                        <td><span className={`status-pill ${row.alignment === "eligible" ? "status-pill--surface" : "status-pill--muted"}`}>{row.alignment}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-inline"><ChartNoAxesCombined size={19} /><span>No trial history is available in the imported artifact.</span></div>
            )}
          </section>
        </>
      ) : (
        <section className="card blocked-results blocked-results--large">
          <span><LockKeyhole size={19} /></span>
          <div><strong>Waiting for an analysis artifact</strong><p>The approved strata are Baseline, QoS LOW, QoS MEDIUM, and QoS HIGH. The campaign protocol specifies at least three complete cycles (48 trials minimum), but completeness does not prove measurement alignment.</p></div>
        </section>
      )}
    </div>
  );
}
