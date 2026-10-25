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
  applyQosProfile,
  backendConnection,
  getQosApplicationStatus,
  getReport,
  getReports,
  getQosDryRunDecision,
  parseRunnerReport,
  ApiRequestError,
} from "../api/client";
import type {
  ApiQosApplicationStatus,
  ApiQosDryRunDecision,
  ApiReportSummary,
} from "../api/client";
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
  const [dryRunDecision, setDryRunDecision] = useState<ApiQosDryRunDecision | null>(null);
  const [decisionError, setDecisionError] = useState("");
  const [qosStatus, setQosStatus] = useState<ApiQosApplicationStatus | null>(null);
  const [applicationState, setApplicationState] = useState<
    "dry_run" | "pending_authorization" | "applying" | "applied_verified" | "failed" | "verification_failed"
  >("dry_run");
  const [authorizationChecked, setAuthorizationChecked] = useState(false);
  const [applicationError, setApplicationError] = useState("");
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

  useEffect(() => {
    if (!backendConnection.baseUrl) return;
    let active = true;
    getQosApplicationStatus().then((response) => {
      if (active) setQosStatus(response);
    }).catch((reason: unknown) => {
      if (active) {
        setQosStatus({
          status: "unavailable",
          applied_profile: null,
          interface: "s1-eth5",
          message: reason instanceof Error ? reason.message : "Could not read applied QoS state.",
        });
      }
    });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!backendConnection.baseUrl) return;
    let active = true;
    let pending = false;
    const refreshDecision = async () => {
      if (pending) return;
      pending = true;
      try {
        const response = await getQosDryRunDecision();
        if (active) {
          setDryRunDecision(response);
          setDecisionError("");
        }
      } catch (reason) {
        if (active) {
          setDryRunDecision(null);
          setDecisionError(reason instanceof Error ? reason.message : "Could not load the dry-run decision.");
        }
      } finally {
        pending = false;
      }
    };
    void refreshDecision();
    const timer = window.setInterval(() => void refreshDecision(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
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

  function requestApplicationAuthorization() {
    setApplicationError("");
    setAuthorizationChecked(false);
    setApplicationState("pending_authorization");
  }

  async function applySelectedProfile() {
    if (
      !dryRunDecision?.application_enabled
      || !dryRunDecision.selected_profile
      || !dryRunDecision.measurement
      || !authorizationChecked
    ) return;
    setApplicationState("applying");
    setApplicationError("");
    try {
      await applyQosProfile(
        dryRunDecision.selected_profile,
        dryRunDecision.measurement.timestamp,
      );
    } catch (reason) {
      setApplicationState(
        reason instanceof ApiRequestError && reason.lifecycleState === "verification_failed"
          ? "verification_failed"
          : "failed",
      );
      setApplicationError(reason instanceof Error ? reason.message : "QoS application failed.");
      setAuthorizationChecked(false);
      Promise.all([getQosDryRunDecision(), getQosApplicationStatus()])
        .then(([decision, status]) => {
          setDryRunDecision(decision);
          setQosStatus(status);
        })
        .catch(() => {
          setQosStatus({
            status: "unavailable",
            applied_profile: null,
            interface: "s1-eth5",
            message: "Applied QoS state could not be read after the failed operation.",
          });
        });
      return;
    }
    setApplicationState("applied_verified");
    setAuthorizationChecked(false);
    try {
      const [decision, status] = await Promise.all([
        getQosDryRunDecision(),
        getQosApplicationStatus(),
      ]);
      setDryRunDecision(decision);
      setQosStatus(status);
    } catch (reason) {
      setApplicationError(
        `Profile was applied and verified, but refreshing its displayed state failed: ${
          reason instanceof Error ? reason.message : "backend refresh failed"
        }`,
      );
      getQosApplicationStatus().then(setQosStatus).catch(() => {
        setQosStatus({
          status: "unavailable",
          applied_profile: null,
          interface: "s1-eth5",
          message: "Applied QoS state could not be read after the failed operation.",
        });
      });
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
        <span className="status-pill status-pill--muted"><ShieldCheck size={14} /> Planning · authorized application</span>
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

      <section className="card qos-dry-run-card" aria-labelledby="qos-dry-run-title">
        <div className="section-heading">
          <div>
            <h2 id="qos-dry-run-title">AI & QoS Decision</h2>
            <p>Fresh telemetry is classified by existing rules. Application remains opt-in and requires a separate, deliberate authorization; this is not an ML prediction.</p>
          </div>
          <span className={`status-pill ${dryRunDecision?.status === "ready" ? "status-pill--surface" : "status-pill--muted"}`}>
            {dryRunDecision?.status === "ready"
              ? `Telemetry ${dryRunDecision.telemetry_status} · ${dryRunDecision.measurement_age_seconds?.toFixed(1) ?? "?"}s old`
              : decisionError ? "Decision API error" : `No decision · ${dryRunDecision?.telemetry_status ?? "loading"}`}
          </span>
        </div>
        {decisionError ? (
          <p className="inline-error" role="alert">{decisionError}</p>
        ) : dryRunDecision?.status === "ready" && dryRunDecision.measurement && dryRunDecision.proposed_policy ? (
          <>
            <div className="qos-dry-run-summary">
              <div>
                <small>Currently applied profile</small>
                <strong>{qosStatus?.status === "verified" && qosStatus.applied_profile
                  ? qosStatus.applied_profile
                  : qosStatus?.status === "verified"
                    ? "No recognized managed profile"
                    : qosStatus?.status === "unrecognized"
                      ? "Unrecognized configuration"
                      : "Unknown"}</strong>
                <span>{qosStatus?.message ?? "Reading controller state."}</span>
              </div>
              <div>
                <small>Application lifecycle</small>
                <strong className={`qos-application-state qos-application-state--${applicationState}`}>
                  {applicationState.replaceAll("_", " ")}
                </strong>
                <span>{applicationState === "applied_verified"
                  ? "Profile confirmed by WSL kernel readback."
                  : applicationState === "verification_failed"
                    ? "Application was not confirmed; current state may be uncertain."
                    : "Proposal only until explicitly authorized."}</span>
              </div>
            </div>
            <div className="qos-dry-run-summary">
              <div><small>Rule-based classification</small><strong>{dryRunDecision.classification} · score {dryRunDecision.score}</strong><span>Threshold result, not a model prediction</span></div>
              <div><small>Current dry-run profile</small><strong>{dryRunDecision.selected_profile ?? "None"}</strong><span>Proposed selection only; not the applied network profile</span></div>
              <div><small>Candidate profile</small><strong>{dryRunDecision.candidate_profile ?? "None"}</strong><span>{dryRunDecision.candidate_profile ? "Awaiting stabilization" : "No profile change pending"}</span></div>
              <div><small>Hysteresis samples</small><strong>{dryRunDecision.hysteresis_samples_observed} / {dryRunDecision.hysteresis_samples_required}</strong><span>Distinct qualifying telemetry records</span></div>
              <div><small>Cooldown remaining</small><strong>{dryRunDecision.candidate_profile ? `${dryRunDecision.cooldown_remaining_seconds.toFixed(1)}s` : "Not pending"}</strong><span>{dryRunDecision.candidate_profile ? `Minimum ${dryRunDecision.cooldown_seconds.toFixed(0)}s since the current profile was selected` : "Applies while a candidate profile is pending"}</span></div>
            </div>
            <div className="qos-dry-run-measurement">
              <strong>Validated measurement</strong>
              <span>Record source: {dryRunDecision.telemetry_source}</span>
              <span>{dryRunDecision.measurement.source} · {dryRunDecision.measurement.scenario} · {dryRunDecision.measurement.interface}</span>
              <span>{new Date(dryRunDecision.measurement.timestamp).toLocaleString()} · {dryRunDecision.measurement.throughput_mbps.toFixed(3)} Mbps throughput · {dryRunDecision.measurement.utilization_percent.toFixed(2)}% utilization</span>
              <span>{dryRunDecision.measurement.latency_ms.toFixed(3)} ms latency · {dryRunDecision.measurement.jitter_ms.toFixed(3)} ms jitter · {dryRunDecision.measurement.packet_loss_percent.toFixed(3)}% loss</span>
            </div>
            <div className="qos-dry-run-policy">
              <div><strong>Proposed {dryRunDecision.selected_profile} HTB profile</strong><span>{dryRunDecision.proposed_policy.interface} · {dryRunDecision.proposed_policy.total_bandwidth_mbps.toFixed(1)} Mbps root · currently proposed, not an applied-state claim</span></div>
              <div className="qos-dry-run-rates">
                {Object.entries(dryRunDecision.proposed_policy.service_allocations_mbps).map(([service, rate]) => <span key={service}>{service}: {rate.toFixed(2)} Mbps</span>)}
                <span>unmatched: {dryRunDecision.proposed_policy.unmatched_rate_mbps.toFixed(2)} Mbps</span>
              </div>
              <details><summary>Show proposed tc commands ({applicationState === "applied_verified" ? "controller applied and verified" : "not executed"})</summary><pre>{dryRunDecision.tc_commands.join("\n")}</pre></details>
            </div>
            <div className="qos-dry-run-explanation"><strong>Why this profile</strong><ul>{dryRunDecision.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></div>
            {applicationError && <p className="inline-error" role="alert">{applicationError}</p>}
            {applicationState === "pending_authorization" ? (
              <div className="qos-apply-authorization">
                <label>
                  <input
                    type="checkbox"
                    checked={authorizationChecked}
                    onChange={(event) => setAuthorizationChecked(event.target.checked)}
                  />
                  I authorize applying the {dryRunDecision.selected_profile} profile to {dryRunDecision.proposed_policy.interface}.
                </label>
                <button
                  className="button button--primary"
                  type="button"
                  disabled={!authorizationChecked}
                  onClick={() => void applySelectedProfile()}
                >
                  Confirm Apply QoS Profile
                </button>
                <button
                  className="button button--secondary"
                  type="button"
                  onClick={() => {
                    setApplicationState("dry_run");
                    setAuthorizationChecked(false);
                  }}
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                className="button button--primary"
                type="button"
                disabled={
                  !dryRunDecision.application_enabled
                  || !qosStatus
                  || qosStatus.status === "unavailable"
                  || dryRunDecision.candidate_profile !== null
                  || applicationState === "applying"
                }
                onClick={requestApplicationAuthorization}
              >
                {applicationState === "applying" ? "Applying…" : "Apply QoS Profile"}
              </button>
            )}
            {!dryRunDecision.application_enabled && (
              <p className="qos-dry-run-note">Application is disabled by default. Set ADAPTIVE_QOS_ENABLE_APPLICATION=1 only for a trusted local lab.</p>
            )}
            {dryRunDecision.candidate_profile && (
              <p className="qos-dry-run-note">Application is disabled while the candidate profile awaits hysteresis and cooldown stabilization.</p>
            )}
            <p className="qos-dry-run-note">{dryRunDecision.message}</p>
          </>
        ) : (
          <div className={`qos-dry-run-empty qos-dry-run-empty--${dryRunDecision?.status ?? "loading"}`} role="status">
            <strong>{dryRunDecision?.status === "stale" ? "Telemetry is stale" : "No safe QoS decision available"}</strong>
            <span>{dryRunDecision?.message ?? "Waiting for a validated telemetry response."}</span>
            <span>Stale, unavailable, or unsupported telemetry never produces a policy proposal.</span>
          </div>
        )}
        {dryRunDecision && dryRunDecision.history.length > 0 && (
          <details className="qos-dry-run-history"><summary>Decision history ({dryRunDecision.history.length})</summary>
            <ul>{dryRunDecision.history.map((item, index) => <li key={`${item.measurement_timestamp}-${index}`}>{item.timestamp} · {item.classification} · selected {item.selected_profile} · {item.transition.replaceAll("_", " ")}</li>)}</ul>
          </details>
        )}
      </section>

      <section className="card artifact-import">
        <div className="artifact-import__copy">
          <span className="section-icon"><FileJson2 size={19} /></span>
          <div>
            <h2>Import an offline prediction report</h2>
          <p>The existing Python runner loads the saved model and plans QoS without applying tc. Import its JSON output here. Model predictions below come only from a successfully imported report.</p>
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
