import { ChartNoAxesCombined, FileUp, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  apiReportToEvidenceReport,
  apiReportToRunnerReport,
  backendConnection,
  getExperimentsStatus,
  getReport,
  getReports,
  getTrafficRunHistory,
  parseEvidenceReport,
} from "../api/client";
import type { ApiExperimentsStatus, ApiReportSummary, ApiTrafficRunHistory } from "../api/client";
import type { ImportedEvidenceReport, ImportedRunnerReport, ImportedTrial, ImportedTrialFlow } from "../types";
import { formatMeasurement, formatMbps, formatNumber } from "../utils/format";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface ExperimentsProps {
  report: ImportedEvidenceReport | null;
  reportSource: "api" | "imported";
  onReport: (report: ImportedEvidenceReport, source: "api" | "imported") => void;
}

const STRATEGIES = ["Baseline", "QoS LOW", "QoS MEDIUM", "QoS HIGH"] as const;
const REQUIRED_TRIALS = 48;
const METRICS = [
  { key: "throughputMbps", label: "Average throughput", unit: "Mbps", direction: "higher" },
  { key: "latencyMs", label: "Average latency", unit: "ms", direction: "lower" },
  { key: "jitterMs", label: "Average jitter", unit: "ms", direction: "lower" },
  { key: "packetLossPercent", label: "Packet loss", unit: "%", direction: "lower" },
  { key: "queueDrops", label: "Queue drops", unit: "packets", direction: "lower" },
] as const;
type MetricKey = typeof METRICS[number]["key"];

function mean(values: Array<number | null | undefined>): number | null {
  const valid = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  return valid.length ? valid.reduce((total, value) => total + value, 0) / valid.length : null;
}

function strategyName(trial: ImportedTrial): typeof STRATEGIES[number] | null {
  const condition = trial.condition.trim().toLowerCase();
  const profile = trial.profile.trim().toLowerCase();
  if (condition === "baseline" || profile === "baseline") return "Baseline";
  const candidate = `${condition} ${profile}`;
  if (candidate.includes("low")) return "QoS LOW";
  if (candidate.includes("medium")) return "QoS MEDIUM";
  if (candidate.includes("high")) return "QoS HIGH";
  return null;
}

type StrategyAggregate = { strategy: typeof STRATEGIES[number]; trialCount: number } & Record<MetricKey, number | null>;

function statusText(value: string | null | undefined): string {
  if (!value || value === "not reported") return "Unavailable";
  return value.replaceAll("_", " ");
}

function checkStatus(value: string | null | undefined, positive: string[] = ["eligible", "verified", "complete", "valid"]): "Pass" | "Blocked" | "Unavailable" {
  if (!value || value === "not reported") return "Unavailable";
  const normalized = value.toLowerCase().replaceAll(" ", "_");
  return positive.some((token) => normalized === token) ? "Pass" : "Blocked";
}

function EvidenceCheck({ label, value, positive, nextStep, imported }: { label: string; value?: string | null; positive?: string[]; nextStep?: string; imported?: boolean }) {
  const status = checkStatus(value, positive);
  return <div className="evidence-check">
    <span><strong>{label}</strong><small>{statusText(value)}</small>{status !== "Pass" && nextStep && <small>{nextStep}</small>}</span>
    <span className={`status-pill ${status === "Pass" && !imported ? "status-pill--surface" : "status-pill--muted"}`}>{imported ? "Reported" : status}</span>
  </div>;
}

export function Experiments({ report, reportSource, onReport }: ExperimentsProps) {
  const [error, setError] = useState("");
  const [importName, setImportName] = useState("");
  const [reportsError, setReportsError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [reportDetailError, setReportDetailError] = useState("");
  const [runHistoryError, setRunHistoryError] = useState("");
  const [apiReports, setApiReports] = useState<ApiReportSummary[]>([]);
  const [runnerReports, setRunnerReports] = useState<ApiReportSummary[]>([]);
  const [unsupportedReports, setUnsupportedReports] = useState<ApiReportSummary[]>([]);
  const [runHistory, setRunHistory] = useState<ApiTrafficRunHistory | null>(null);
  const [apiReportMessage, setApiReportMessage] = useState("");
  const [reportDirectoryStatus, setReportDirectoryStatus] = useState<"unset" | "invalid" | "inaccessible" | "valid" | null>(null);
  const [apiStatus, setApiStatus] = useState<ApiExperimentsStatus | null>(null);
  const [loading, setLoading] = useState(Boolean(backendConnection.baseUrl));
  const [refreshing, setRefreshing] = useState(false);
  const [loadingReport, setLoadingReport] = useState("");
  const [selectedApiReportId, setSelectedApiReportId] = useState("");
  const [runnerReport, setRunnerReport] = useState<ImportedRunnerReport | null>(null);

  const refresh = useCallback(async (initial = false) => {
    if (!backendConnection.baseUrl) {
      setLoading(false);
      setReportsError("Backend API URL is not configured.");
      setStatusError("Backend API URL is not configured.");
      setRunHistoryError("Backend API URL is not configured.");
      return;
    }
    setReportsError("");
    setStatusError("");
    setRunHistoryError("");
    if (initial) setLoading(true);
    setRefreshing(true);
    const [reportsResult, statusResult, runsResult] = await Promise.allSettled([getReports(), getExperimentsStatus(), getTrafficRunHistory()]);
    if (reportsResult.status === "fulfilled") {
      setReportDirectoryStatus(reportsResult.value.directory_status);
      setApiReports(reportsResult.value.reports.filter((item) => item.report_type === "campaign-validation" || item.report_type === "evidence-analysis"));
      setRunnerReports(reportsResult.value.reports.filter((item) => item.report_type === "experiment-runner"));
      setUnsupportedReports(reportsResult.value.reports.filter((item) => item.report_type === "unknown" || item.status === "invalid"));
      setApiReportMessage(reportsResult.value.message);
      if (reportsResult.value.status !== "available") setReportsError(reportsResult.value.message);
    } else setReportsError(reportsResult.reason instanceof Error ? reportsResult.reason.message : "Could not list reports.");
    if (statusResult.status === "fulfilled") {
      setApiStatus(statusResult.value);
      setStatusError("");
    } else {
      setApiStatus(null);
      setStatusError(statusResult.reason instanceof Error ? statusResult.reason.message : "Could not load campaign status.");
    }
    if (runsResult.status === "fulfilled") setRunHistory(runsResult.value);
    else {
      setRunHistory(null);
      setRunHistoryError(runsResult.reason instanceof Error ? runsResult.reason.message : "Request failed.");
    }
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void refresh(true);
  }, [refresh]);

  async function importReport(file?: File) {
    if (!file) return;
    setError("");
    try {
      const parsed = parseEvidenceReport(await file.text());
      onReport(parsed, "imported");
      setRunnerReport(null);
      setImportName(file.name);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not read the report.");
    }
  }

  async function loadApiReport(reportId: string) {
    setReportDetailError("");
    setLoadingReport(reportId);
    try {
      const detail = await getReport(reportId);
      if (detail.report_type === "experiment-runner") {
        setRunnerReport(apiReportToRunnerReport(detail));
        setSelectedApiReportId(reportId);
        return;
      }
      onReport(apiReportToEvidenceReport(detail), "api");
      setRunnerReport(null);
      setImportName("");
      setSelectedApiReportId(reportId);
    } catch (reason) {
      setReportDetailError(reason instanceof Error ? reason.message : "Could not load the API report.");
    } finally {
      setLoadingReport("");
    }
  }

  const trials = report?.trials ?? [];
  const comparisonReady = reportSource === "api" && report?.comparisonReady === true;
  const eligibleTrials = comparisonReady
    ? trials.filter((trial) => trial.alignment === "eligible" && trial.status.toLowerCase() === "complete")
    : [];
  const trialGroups = useMemo<StrategyAggregate[]>(() => STRATEGIES.map((strategy) => {
    const members = eligibleTrials.filter((trial) => strategyName(trial) === strategy);
    const flows = members.flatMap((trial) => trial.flows);
    const row = {} as Record<MetricKey, number | null>;
    for (const metric of METRICS) row[metric.key] = mean(flows.map((flow) => flow[metric.key]));
    return { strategy, trialCount: members.length, ...row };
  }), [eligibleTrials]);
  const chartData = trialGroups.filter((row) => row.throughputMbps !== null).map((row) => ({ name: row.strategy, throughput: row.throughputMbps }));
  const completedTrials = report ? trials.filter((trial) => trial.status.toLowerCase() === "complete").length : null;
  const failedTrials = report ? trials.filter((trial) => ["failed", "invalid"].includes(trial.status.toLowerCase())).length : null;
  const eligibleStrategyCount = comparisonReady ? trialGroups.filter((row) => row.trialCount > 0).length : null;
  const campaignStatus = report?.campaignStatus ?? apiStatus?.campaign_completeness;
  const reportedCycles = report?.completeCycleCount ?? apiStatus?.complete_cycle_count;
  const requiredCycles = report?.minimumCompleteCycles ?? apiStatus?.minimum_complete_cycles;
  const campaignValidationStatus = reportedCycles !== null && reportedCycles !== undefined
    && requiredCycles !== null && requiredCycles !== undefined && reportedCycles < requiredCycles
    ? `incomplete (${reportedCycles}/${requiredCycles} cycles)`
    : campaignStatus;
  const boundaryStatus = report?.boundaryEvidenceIntegrityStatus ?? apiStatus?.boundary_evidence_integrity_status;
  const provenanceStatus = report?.producerProvenanceStatus ?? apiStatus?.producer_provenance_status;
  const clockStatus = report?.clockMappingStatus ?? apiStatus?.clock_mapping_status;
  const alignmentStatus = report?.alignmentStatus ?? apiStatus?.measurement_alignment_eligibility;
  const comparisonStatus = report
    ? comparisonReady ? "eligible" : "blocked"
    : apiStatus?.status === "unavailable"
      ? "unavailable"
      : apiStatus?.comparison_ready ? "eligible" : apiStatus ? "blocked" : null;
  const campaignEvidenceMessage = loading
    ? "Loading campaign evidence and report inventory."
    : reportsError
      ? `The report service could not be reached or read its configured directory: ${reportsError}`
      : statusError
        ? `Campaign status could not be loaded: ${statusError}`
        : apiStatus?.status === "unavailable"
          ? runnerReports.length > 0
            ? `Planned experiment reports are available, but no validated campaign/evidence-analysis report is available. ${apiStatus.message}${unsupportedReports.length ? ` ${unsupportedReports.length} invalid or incompatible JSON report(s) were excluded.` : ""}`
            : `No supported campaign/evidence-analysis report is available. ${apiStatus.message}${unsupportedReports.length ? ` ${unsupportedReports.length} invalid or incompatible JSON report(s) were excluded.` : ""}`
          : apiStatus?.status === "available"
            ? apiStatus.comparison_ready
              ? "Validated campaign evidence is available and eligible for comparison."
              : `A campaign report is available, but comparison eligibility is blocked. ${apiStatus.message}`
            : reportDirectoryStatus === "valid"
              ? "The report directory is available, but campaign evidence status has not been reported."
              : apiReportMessage;

  const baseline = trialGroups.find((row) => row.strategy === "Baseline");
  const comparisons = METRICS.map((metric) => {
    const baseValue = baseline?.[metric.key] ?? null;
    const values = trialGroups.map((row) => ({ strategy: row.strategy, value: row[metric.key] ?? null })).filter((row) => row.value !== null);
    const best = values.length ? values.reduce((current, item) => metric.direction === "higher"
      ? item.value! > current.value! ? item : current
      : item.value! < current.value! ? item : current) : null;
    return { metric, baseValue, best };
  });
  const reportName = importName || (reportSource === "api" ? selectedApiReportId : "") || "Selected evidence report";

  return <div className="page-stack experiments-page">
    <section className="page-intro">
      <div><div className="eyebrow"><span className="eyebrow-mark" /> EXPERIMENT EVALUATION</div><h1>Experiments</h1><p>Evaluate network performance across baseline and QoS strategies.</p></div>
      <div className="experiments-header-actions">
        <span className={`status-pill ${checkStatus(comparisonStatus) === "Pass" ? "status-pill--surface" : "status-pill--muted"}`}>Evidence · {statusText(comparisonStatus)}</span>
        {backendConnection.baseUrl && <button className="button button--secondary" type="button" onClick={() => void refresh()} disabled={refreshing} aria-label="Refresh reports and campaign status"><RefreshCw size={15} />{refreshing ? "Refreshing…" : "Refresh reports"}</button>}
      </div>
    </section>

    {!report && <div className="api-report-empty" role={reportsError || statusError ? "alert" : "status"}>
      <strong>{loading ? "Loading evidence status" : reportsError || statusError ? "Evidence service unavailable" : apiStatus?.status === "available" && apiStatus.comparison_ready ? "Validated campaign evidence available" : apiStatus?.status === "available" ? "Campaign report available; comparison blocked" : runnerReports.length > 0 ? "Planned experiment reports available; campaign evidence unavailable" : "No supported campaign/evidence-analysis report available"}</strong>
      <p>{campaignEvidenceMessage}</p>
    </div>}

    <section className="campaign-progress" aria-label="Campaign progress">
      <article className="card result-stat"><span>Completed trials</span><strong>{completedTrials === null ? "No campaign report" : `${completedTrials} / ${REQUIRED_TRIALS}`}</strong><small>Required by the 3-cycle campaign protocol</small></article>
      <article className="card result-stat"><span>Failed trials</span><strong>{failedTrials ?? "No campaign report"}</strong><small>{report ? "From selected report" : "Load a campaign report to inspect trial outcomes"}</small></article>
      <article className="card result-stat"><span>Evidence validation</span><strong>{statusText(campaignValidationStatus)}</strong><small>{reportedCycles != null && requiredCycles != null ? `${reportedCycles} / ${requiredCycles} complete cycles` : "Campaign structure and evidence checks"}</small></article>
      <article className="card result-stat"><span>Valid strategy comparisons</span><strong>{eligibleStrategyCount === null ? "No eligible evidence" : `${eligibleStrategyCount} / 4`}</strong><small>{comparisonReady ? "Eligible strategies with complete trials" : "Requires eligible comparison evidence"}</small></article>
    </section>

    <section className="card execution-history-card">
      <div className="section-heading"><div><h2>Traffic Lab executions</h2><p>Supervisor run records are execution history, not campaign trial evidence.</p></div><span className="status-pill status-pill--muted">{runHistoryError ? "Load error" : runHistory?.status === "available" ? `${runHistory.completed_count + runHistory.failed_count + runHistory.stopped_count} runs` : loading ? "Loading" : runHistory?.status === "unavailable" ? "Unavailable" : "Not loaded"}</span></div>
      {runHistoryError && <div className="inline-error" role="alert">Could not load Traffic Lab executions: {runHistoryError} <button type="button" className="text-button" onClick={() => void refresh()}>Retry</button></div>}
      {runHistory?.status === "unavailable" && <p className="api-report-empty">{runHistory.message}</p>}
      {runHistory?.status === "available" && <>
        <div className="execution-history-counts"><span>Completed <strong>{runHistory.completed_count}</strong></span><span>Failed <strong>{runHistory.failed_count}</strong></span><span>Stopped <strong>{runHistory.stopped_count}</strong></span></div>
        <p className="comparison-unavailable">These counts do not contribute to the 48-trial campaign requirement. A Traffic Lab execution alone does not establish a Baseline or QoS strategy trial.</p>
        {runHistory.runs.length === 0 && <p className="api-report-empty">No Traffic Lab execution records are available.</p>}
        {runHistory.runs.length > 0 && <div className="table-scroll"><table className="data-table" aria-label="Traffic Lab execution history"><thead><tr><th>Experiment ID</th><th>Scenario</th><th>Status</th><th>Source / flows</th><th>Offered rate</th><th>Started</th><th>Finished</th></tr></thead><tbody>
          {runHistory.runs.slice(0, 6).map((run) => <tr key={run.experiment_id}><td>{run.experiment_id}</td><td>{run.scenario}</td><td>{run.status}</td><td>{run.source_host} · {run.flows.join(", ")}</td><td>{formatMbps(run.target_rate_mbps)} Mbps/flow · {run.duration_seconds}s</td><td>{run.started_at}</td><td>{run.finished_at}</td></tr>)}
        </tbody></table></div>}
      </>}
    </section>

    <section className="card strategy-comparison">
      <div className="section-heading"><div><h2>Strategy comparison</h2><p>{comparisonReady ? "Aggregates use complete, alignment-eligible trials." : "Metrics are withheld until the backend establishes comparison eligibility."}</p></div></div>
      <div className="table-scroll"><table className="data-table strategy-table" aria-label="Strategy comparison metrics">
        <thead><tr><th>Strategy</th><th>Trials</th><th>Avg throughput</th><th>Avg latency</th><th>Avg jitter</th><th>Packet loss</th><th>Queue drops</th><th>Status</th></tr></thead>
        <tbody>{trialGroups.map((row) => <tr key={row.strategy}>
          <th scope="row">{row.strategy}</th><td>{comparisonReady ? row.trialCount || "—" : "—"}</td>
          {METRICS.map((metric) => <td key={metric.key}>{comparisonReady && row.trialCount ? formatMeasurement(row[metric.key] ?? null, metric.unit) : "Not available"}</td>)}
          <td><span className={`status-pill ${comparisonReady && row.trialCount ? "status-pill--surface" : "status-pill--muted"}`}>{comparisonReady && row.trialCount ? "Eligible" : report ? "Blocked" : "Awaiting report"}</span></td>
        </tr>)}</tbody>
      </table></div>
      {comparisonReady && chartData.length > 0 && <div className="chart-card results-chart">
        <div className="section-heading"><div><h3>Mean throughput by strategy</h3><p>Eligible receiver throughput · Mbps</p></div></div>
        <div className="chart-frame chart-frame--large"><ResponsiveContainer width="100%" height="100%"><BarChart data={chartData} margin={{ top: 12, right: 12, left: -12, bottom: 2 }}>
          <CartesianGrid stroke="#D6BFA9" strokeDasharray="3 5" vertical={false} /><XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ fill: "#704B3A", fontSize: 11 }} />
          <YAxis tickLine={false} axisLine={false} tick={{ fill: "#704B3A", fontSize: 11 }} tickFormatter={(value) => formatMeasurement(Number(value), "Mbps")} />
          <Tooltip contentStyle={{ border: "1px solid #704B3A", borderRadius: 10, background: "#F4EDE4" }} formatter={(value) => [formatMeasurement(Number(value), "Mbps"), "Mean throughput"]} />
          <Bar dataKey="throughput" fill="#704B3A" radius={[6, 6, 0, 0]} />
        </BarChart></ResponsiveContainer></div>
      </div>}
      {!report?.comparisonReady && <p className="comparison-unavailable">Comparison is blocked until report evidence meets the backend eligibility checks.</p>}
    </section>

    <section className="card validation-summary">
      <div className="section-heading"><div><h2>Evidence validation</h2><p>Parsed report claims and backend validation are shown separately.</p></div><span className={`status-pill ${comparisonReady || (!report && apiStatus?.comparison_ready) ? "status-pill--surface" : "status-pill--muted"}`}>{comparisonReady || (!report && apiStatus?.comparison_ready) ? "Comparison eligible" : "Review required"}</span></div>
      <div className="evidence-check-grid">
        <EvidenceCheck label="Campaign completeness" value={campaignValidationStatus} positive={["complete"]} nextStep="Complete the required cycles and trials." imported={reportSource === "imported" && Boolean(report)} />
        <EvidenceCheck label="Boundary evidence" value={boundaryStatus} positive={["verified"]} nextStep="Supply verified receiver boundary evidence." imported={reportSource === "imported" && Boolean(report)} />
        <EvidenceCheck label="Producer provenance" value={provenanceStatus} positive={["independently_validated"]} nextStep="Validate measurement producer provenance." imported={reportSource === "imported" && Boolean(report)} />
        <EvidenceCheck label="Clock mapping" value={clockStatus} positive={["independently_validated", "validated"]} nextStep="Provide validated clock mapping." imported={reportSource === "imported" && Boolean(report)} />
        <EvidenceCheck label="Alignment eligibility" value={alignmentStatus} positive={["eligible"]} nextStep="Resolve measurement alignment issues." imported={reportSource === "imported" && Boolean(report)} />
        <EvidenceCheck label="Comparison eligibility" value={comparisonStatus} positive={["eligible"]} nextStep="Load evidence that passes all comparison gates." imported={reportSource === "imported" && Boolean(report)} />
      </div>
      {report?.errors?.length ? <details className="reported-issues"><summary>Reported validation issues ({report.errors.length})</summary><ul>{report.errors.map((issue, i) => <li key={`${issue}-${i}`}>{issue}</li>)}</ul></details> : null}
    </section>

    <section className="card api-report-card">
      <div className="section-heading"><div><h2>Evidence reports</h2><p>{loading ? "Loading reports…" : apiReportMessage || "Load a report to review campaign evidence."}</p></div><span className="status-pill status-pill--muted">{loading ? "Loading" : `${apiReports.length} available`}</span></div>
      {reportsError && <div className="inline-error" role="alert">{reportsError} <button type="button" className="text-button" onClick={() => void refresh()}>Retry</button></div>}
      {statusError && <div className="inline-error" role="alert">Campaign status request failed: {statusError} <button type="button" className="text-button" onClick={() => void refresh()}>Retry</button></div>}
      {reportDetailError && <div className="inline-error" role="alert">Could not load the selected report: {reportDetailError} <button type="button" className="text-button" onClick={() => void refresh()}>Retry</button></div>}
      {!loading && !reportsError && apiReports.length === 0 && <p className="api-report-empty">{runnerReports.length > 0 ? "No validated campaign/evidence-analysis report is available; the planned reports below are not campaign evidence." : "No campaign evidence reports are available. Run the campaign validator or import an offline report below."}</p>}
      {apiReports.map((item) => <div className="api-report-row" key={item.report_id}>
        <span><strong>{item.report_id}</strong><small>{item.report_type} · {item.status} · created {item.created_at ?? "time unavailable"} · {item.input_record_count ?? "record count unavailable"} records · {item.freshness_status} · comparison {item.comparison_ready ? "eligible" : "blocked"}</small></span>
        <button className="button button--secondary" type="button" disabled={loadingReport !== ""} onClick={() => void loadApiReport(item.report_id)}>{loadingReport === item.report_id ? "Loading…" : "Load report"}</button>
      </div>)}
      {runnerReports.length > 0 && <div className="prediction-report-list"><div><h3>Offline ML prediction reports (planned experiment-runner)</h3><p>These offline model plans are not campaign evidence and cannot establish strategy comparisons or count as executed experiments.</p></div>{runnerReports.map((item) => <div className="api-report-row" key={item.report_id}>
        <span><strong>{item.report_id}</strong><small>experiment-runner · {item.status} · created {item.created_at ?? "time unavailable"} · {item.input_record_count ?? "record count unavailable"} input {item.input_record_count === 1 ? "record" : "records"} · freshness {item.freshness_status} · planning only</small></span>
        <button className="button button--secondary" type="button" disabled={loadingReport !== ""} onClick={() => void loadApiReport(item.report_id)}>{loadingReport === item.report_id ? "Loading…" : "Inspect predictions"}</button>
      </div>)}</div>}
      {unsupportedReports.length > 0 && <div className="prediction-report-list"><div><h3>Invalid or incompatible reports ({unsupportedReports.length})</h3><p>These files were discovered but failed JSON reading or supported report-schema checks. The reports endpoint does not provide per-file rejection details.</p></div>{unsupportedReports.map((item) => <div className="api-report-row" key={item.report_id}><span><strong>{item.report_id}</strong><small>{item.status} · {item.report_type}</small></span><span className="status-pill status-pill--muted">Not loadable</span></div>)}</div>}
    </section>

    {runnerReport && <section className="card runner-report-card">
      <div className="section-heading"><div><h2>Offline ML predictions</h2><p>Report {runnerReport.runId} · {runnerReport.status} · prediction and allocation are model output and a plan, not network execution.</p></div><span className="status-pill status-pill--muted">Planning only</span></div>
      <div className="table-scroll"><table className="data-table" aria-label="Offline ML prediction records"><thead><tr><th>Experiment</th><th>Scenario</th><th>Source</th><th>Measurement time</th><th>Freshness</th><th>Prediction</th><th>Confidence</th><th>Planned service allocation</th></tr></thead><tbody>
        {runnerReport.records.map((record) => <tr key={`${record.line}-${record.experimentId}`}><td>{record.experimentId ?? "Not available"}</td><td>{record.scenario ?? "Not available"}</td><td>{record.source ?? "Not available"}</td><td>{record.timestamp ?? "Not available"}</td><td>{record.freshnessStatus}{record.freshnessAgeSeconds === null ? "" : ` · ${formatNumber(record.freshnessAgeSeconds)}s`}</td><td>{record.prediction ?? "Not available"}</td><td>{record.confidence === null ? "Not available" : `${formatNumber(record.confidence * 100)}%`}</td><td>{record.serviceAllocationsMbps ? Object.entries(record.serviceAllocationsMbps).map(([name, rate]) => `${name} ${formatMbps(rate)} Mbps`).join(" · ") : "Not available"}</td></tr>)}
      </tbody></table></div>
    </section>}

    <section className="card artifact-import">
      <div className="artifact-import__copy"><span className="section-icon"><FileUp size={19} /></span><div><h2>Import an offline evidence report</h2><p>Parsed report fields are shown separately from independently validated evidence.</p></div></div>
      <label className="button button--secondary upload-button"><FileUp size={16} /> Choose JSON report<input type="file" accept="application/json,.json" onChange={(event) => void importReport(event.target.files?.[0])} /></label>
      {error && <div className="inline-error" role="alert">Import failed: {error}</div>}
      {importName && <div className="import-success" role="status">Parsed <strong>{importName}</strong> · evidence validation is reported separately.</div>}
      {report && <div className="import-success" role="status">Selected <strong>{reportName}</strong> · {report.kind} · status {report.reportStatus} · comparison {report.comparisonReady ? "eligible per report" : "blocked"}</div>}
    </section>

    {report && <>
      <section className="card results-interpretation">
        <div className="section-heading"><div><h2>Results interpretation</h2><p>Metric leaders are shown separately; they do not imply an overall winner.</p></div></div>
        {comparisonReady && baseline?.trialCount ? <div className="interpretation-grid">{comparisons.map(({ metric, baseValue, best }) => {
          const delta = best && baseValue !== null ? best.value! - baseValue : null;
          const percent = delta !== null && baseValue !== null && baseValue !== 0 ? (delta / Math.abs(baseValue)) * 100 : null;
          return <div className="interpretation-row" key={metric.key}><strong>{metric.label}</strong><span>Best: {best?.strategy ?? "Not available"}</span><small>vs Baseline: {delta === null ? "Not available" : `${delta > 0 ? "+" : ""}${formatMeasurement(delta, metric.unit)}${percent === null ? "" : ` (${percent > 0 ? "+" : ""}${formatMeasurement(percent, "%")})`}`}</small></div>;
        })}</div> : <p className="comparison-unavailable">No validated baseline comparison is available. Do not infer QoS improvement from parsed or ineligible measurements.</p>}
      </section>
      {trials.some((trial) => trial.errors?.length) && <section className="card evidence-errors"><strong>Trial issues</strong><ul>{trials.flatMap((trial) => (trial.errors ?? []).map((issue) => <li key={`${trial.trialId}-${issue}`}>{trial.trialId}: {issue}</li>))}</ul></section>}
      <section className="card table-card"><div className="section-heading"><div><h2>Trial history</h2><p>Descriptive records from the selected report.</p></div><span className="status-pill status-pill--muted">{trials.length} trials</span></div>
        {trials.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>Trial</th><th>Cycle</th><th>Strategy</th><th>Status</th><th>Flow</th><th>Throughput</th><th>Latency</th><th>Jitter</th><th>Packet loss</th><th>Queue drops</th><th>Alignment</th></tr></thead><tbody>
          {trials.flatMap((trial) => trial.flows.map((flow: ImportedTrialFlow, index) => <tr key={`${trial.trialId}-${flow.flowId}-${index}`}><td>{trial.trialId}</td><td>{trial.repetitionId}</td><td>{strategyName(trial) ?? `${trial.condition} / ${trial.profile}`}</td><td>{trial.status}</td><td>{flow.flowId}</td><td>{formatMeasurement(flow.throughputMbps, "Mbps")}</td><td>{formatMeasurement(flow.latencyMs, "ms")}</td><td>{formatMeasurement(flow.jitterMs, "ms")}</td><td>{formatMeasurement(flow.packetLossPercent, "%")}</td><td>{formatMeasurement(flow.queueDrops, "packets")}</td><td>{trial.alignment}</td></tr>))}
        </tbody></table></div> : <div className="empty-inline"><ChartNoAxesCombined size={19} /><span>No trial measurements are available in this report.</span></div>}
      </section>
    </>}
  </div>;
}
