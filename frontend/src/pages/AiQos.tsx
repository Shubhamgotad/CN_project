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
import { formatMbps, formatNumber } from "../utils/format";
import type { CongestionClass, ImportedRunnerReport } from "../types";

const PROFILE_NAMES: CongestionClass[] = ["LOW", "MEDIUM", "HIGH"];
const PROFILE_TOTAL_Mbps = Object.values(PROFILE_ALLOCATIONS.LOW).reduce((total, rate) => total + rate, 0);

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
  const [apiDirectoryStatus, setApiDirectoryStatus] = useState<"unset" | "invalid" | "inaccessible" | "valid" | null>(null);
  const [loadingReports, setLoadingReports] = useState(true);
  const [loadingReport, setLoadingReport] = useState("");
  const [dryRunDecision, setDryRunDecision] = useState<ApiQosDryRunDecision | null>(null);
  const [decisionError, setDecisionError] = useState("");
  const [refreshingDecision, setRefreshingDecision] = useState(false);
  const [decisionRefreshCount, setDecisionRefreshCount] = useState(0);
  const [tcCommandCopyStatus, setTcCommandCopyStatus] = useState("");
  const [qosStatus, setQosStatus] = useState<ApiQosApplicationStatus | null>(null);
  const [applicationState, setApplicationState] = useState<
    "dry_run" | "pending_authorization" | "applying" | "applied_verified" | "status_unconfirmed" | "failed" | "verification_failed"
  >("dry_run");
  const [authorizationChecked, setAuthorizationChecked] = useState(false);
  const [applicationError, setApplicationError] = useState("");
  const record = report?.records[selectedRecord] ?? report?.records[0] ?? null;
  const profileBudgetMbps = dryRunDecision?.proposed_policy?.total_bandwidth_mbps ?? PROFILE_TOTAL_Mbps;
  const tcPreviewCommands = dryRunDecision?.tc_commands ?? [];
  const previewTelemetryFresh = dryRunDecision?.status === "ready"
    && dryRunDecision.telemetry_status === "available"
    && (dryRunDecision.telemetry_collection_status === undefined
      || dryRunDecision.telemetry_collection_status === "fresh");
  const selectedProfileAppliedAndVerified = Boolean(
    dryRunDecision?.selected_profile
    && dryRunDecision.proposed_policy?.interface
    && qosStatus?.status === "verified"
    && qosStatus.applied_profile === dryRunDecision.selected_profile
    && qosStatus.interface === dryRunDecision.proposed_policy.interface,
  );
  const displayedApplicationState = selectedProfileAppliedAndVerified
    ? "applied_verified"
    : applicationState === "applied_verified"
      ? "status_unconfirmed"
      : applicationState;
  const tcPreviewLifecycleLabel = selectedProfileAppliedAndVerified
    ? "controller applied and verified"
    : applicationState === "status_unconfirmed"
      ? "controller application reported; status unconfirmed"
      : applicationState === "verification_failed"
        ? "application verification failed"
        : applicationState === "failed"
          ? "application failed; state uncertain"
          : applicationState === "applying"
            ? "application in progress"
            : "dry run only; not executed";
  const tcPreviewNotice = selectedProfileAppliedAndVerified
    ? "This command preview is read-only; the selected profile is confirmed by backend controller status."
    : applicationState === "status_unconfirmed"
      ? "The command preview is read-only. The apply endpoint reported success, but current controller status does not confirm this proposed profile."
      : applicationState === "verification_failed"
        ? "The command preview is read-only. An application attempt failed verification; network state may be uncertain."
        : applicationState === "failed"
          ? "The command preview is read-only. An application attempt failed; current network state may be uncertain."
          : applicationState === "applying"
            ? "The command preview is read-only; a separately authorized application is in progress."
            : "These commands illustrate the proposed configuration only. This page does not execute them or change network settings.";
  const tcPreviewUnavailableReason = !previewTelemetryFresh
    ? "A fresh validated telemetry decision is unavailable."
    : !dryRunDecision?.selected_profile
      ? "The proposed profile is unavailable."
    : !dryRunDecision.proposed_policy?.interface?.trim()
      ? "The proposed interface is unavailable."
      : tcPreviewCommands.length === 0
        ? "The backend did not provide proposed commands."
        : tcPreviewCommands.some((command) => typeof command !== "string" || !command.trim())
          ? "The backend command preview contains an incomplete command."
          : "";
  const allocationServices = record?.serviceAllocationsMbps;
  const budgetTotal = record?.totalBandwidthMbps;
  const policyWeights = record?.policyPercentages;
  const validPolicyWeights = Boolean(
    policyWeights
    && ["gaming", "video", "web", "bulk"].every((key) => Number.isFinite(policyWeights[key]) && policyWeights[key] >= 0)
    && Math.abs(Object.values(policyWeights).reduce((sum, value) => sum + value, 0) - 100) < 0.011
    && allocationServices && record?.unmatchedRateMbps !== null && record?.unmatchedRateMbps !== undefined
    && ["gaming", "video", "web", "bulk"].every((key) =>
      Math.abs(allocationServices[key] - (budgetTotal! - record!.unmatchedRateMbps!) * policyWeights[key] / 100) < 0.011),
  );
  const completeBudget = Boolean(
    allocationServices && budgetTotal !== null && budgetTotal !== undefined && budgetTotal > 0
    && record?.unmatchedRateMbps !== null && record?.unmatchedRateMbps !== undefined
    && ["gaming", "video", "web", "bulk"].every((key) => Number.isFinite(allocationServices[key]))
    && Math.abs(Object.values(allocationServices).reduce((sum, value) => sum + value, 0)
      + record!.unmatchedRateMbps! - budgetTotal) < 0.011,
  );

  async function refreshReports() {
    setLoadingReports(true);
    setApiError("");
    try {
      const response = await getReports();
      setApiReports(response.reports.filter((item) => item.report_type === "experiment-runner"));
      setApiReportMessage(response.message);
      setApiDirectoryStatus(response.directory_status);
    } catch (reason) {
      setApiReports([]);
      setApiError(reason instanceof Error ? reason.message : "Could not list API reports.");
    } finally {
      setLoadingReports(false);
    }
  }

  async function copyTcCommands() {
    if (!navigator.clipboard?.writeText) {
      setTcCommandCopyStatus("Clipboard access is unavailable in this browser. Select the commands to copy them.");
      return;
    }
    try {
      await navigator.clipboard.writeText(tcPreviewCommands.join("\n"));
      setTcCommandCopyStatus("Proposed commands copied.");
    } catch (reason) {
      setTcCommandCopyStatus(
        reason instanceof Error
          ? `Could not copy proposed commands: ${reason.message}`
          : "Could not copy proposed commands. Select the code block to copy it manually.",
      );
    }
  }

  useEffect(() => {
    if (!backendConnection.baseUrl) return;
    let active = true;
    getReports().then((response) => {
      if (active) {
        setApiReports(response.reports.filter((item) => item.report_type === "experiment-runner"));
        setApiReportMessage(response.message);
        setApiDirectoryStatus(response.directory_status);
        setLoadingReports(false);
      }
    }).catch((reason: unknown) => {
      if (active) {
        setApiError(reason instanceof Error ? reason.message : "Could not list API reports.");
        setLoadingReports(false);
      }
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
      setRefreshingDecision(true);
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
        if (active) setRefreshingDecision(false);
      }
    };
    void refreshDecision();
    const timer = window.setInterval(() => void refreshDecision(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [decisionRefreshCount]);

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
      const applyResult = await applyQosProfile(
        dryRunDecision.selected_profile,
        dryRunDecision.measurement.timestamp,
      );
      if (
        applyResult.status !== "applied_verified"
        || applyResult.applied !== true
        || applyResult.profile !== dryRunDecision.selected_profile
        || applyResult.interface !== dryRunDecision.proposed_policy?.interface
        || applyResult.verification !== "kernel_readback_verified"
      ) {
        throw new Error("The controller did not confirm the selected profile with kernel readback.");
      }
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
    setApplicationState("status_unconfirmed");
    setAuthorizationChecked(false);
    try {
      const [decision, status] = await Promise.all([
        getQosDryRunDecision(),
        getQosApplicationStatus(),
      ]);
      setDryRunDecision(decision);
      setQosStatus(status);
      setApplicationState(
        status.status === "verified"
          && status.applied_profile === dryRunDecision.selected_profile
          && status.interface === dryRunDecision.proposed_policy?.interface
          ? "applied_verified"
          : "status_unconfirmed",
      );
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
        <span className="status-pill status-pill--muted"><ShieldCheck size={14} /> Planning · application safety-gated</span>
      </section>

      {backendConnection.baseUrl && (
        <section className="card api-report-card">
          <div className="section-heading">
            <div><h2>Configured prediction reports</h2><p>Validated offline saved-model plans discovered by the backend. Import JSON below remains available as a manual alternative.</p></div>
            <button className="button button--secondary" type="button" onClick={() => void refreshReports()} disabled={loadingReports}>
              {loadingReports ? "Refreshing…" : "Refresh reports"}
            </button>
          </div>
          {apiError && <div className="inline-error" role="alert">{apiError}</div>}
          {apiReports.length ? (
            <div className="api-report-list">
              {apiReports.map((item) => (
                <div className="api-report-row" key={item.report_id}>
                    <span><strong>{item.report_id}</strong><small>experiment-runner · {item.status} · freshness {item.freshness_status} · {item.input_record_count ?? "?"} input records · {item.created_at ?? "creation time unavailable"} · planning only</small></span>
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
          ) : loadingReports ? (
            <p className="api-report-empty" role="status">Loading configured reports…</p>
          ) : (
            <div className="api-report-empty" role={apiError || apiDirectoryStatus !== "valid" ? "alert" : "status"}>
              <p>{apiError || apiReportMessage || "No experiment-runner reports are available."}</p>
              {apiError && <button className="button button--secondary" type="button" onClick={() => void refreshReports()} disabled={loadingReports}>Retry report list</button>}
              {apiDirectoryStatus && apiDirectoryStatus !== "valid" && <strong>Report directory status: {apiDirectoryStatus}</strong>}
              {apiDirectoryStatus === "valid" && !apiError && apiReports.length === 0 && <strong>No compatible reports in the valid configured directory.</strong>}
            </div>
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
              ? `Telemetry ${dryRunDecision.telemetry_status} · ${dryRunDecision.measurement_age_seconds?.toFixed(1) ?? "?"}s old · experiment ${dryRunDecision.telemetry_experiment_id ?? dryRunDecision.measurement?.experiment_id ?? "unknown"}`
              : decisionError ? "Decision API error" : `No decision · ${dryRunDecision?.telemetry_collection_status ?? dryRunDecision?.telemetry_status ?? "loading"}${dryRunDecision?.telemetry_experiment_id ? ` · experiment ${dryRunDecision.telemetry_experiment_id}` : ""}`}
          </span>
          <button
            className="button button--secondary"
            type="button"
            disabled={refreshingDecision}
            onClick={() => setDecisionRefreshCount((count) => count + 1)}
          >
            {refreshingDecision ? "Refreshing live telemetry…" : "Refresh live telemetry"}
          </button>
        </div>
        {dryRunDecision?.configuration_conflict && <p className="inline-error" role="alert">{dryRunDecision.configuration_conflict}</p>}
        {dryRunDecision?.telemetry_source === "configured_person1_jsonl"
          && (dryRunDecision.telemetry_status === "stale"
            || dryRunDecision.telemetry_collection_status === "collection_failed"
            || Boolean(dryRunDecision.configuration_conflict))
          && <p className="qos-dry-run-note">
            To use automatic latest-run discovery, clear <code>ADAPTIVE_QOS_TELEMETRY_FILE</code> in the shell that starts the backend with <code>Remove-Item Env:ADAPTIVE_QOS_TELEMETRY_FILE -ErrorAction SilentlyContinue</code>, then restart the backend and select <strong>Refresh live telemetry</strong>. To keep an explicit override, point it to the exact scenario JSONL for the intended completed run and restart the backend. The override cannot be cleared from this page.
          </p>}
        {decisionError ? (
          <p className="inline-error" role="alert">{decisionError}</p>
        ) : previewTelemetryFresh && dryRunDecision?.measurement && dryRunDecision.proposed_policy ? (
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
                <strong className={`qos-application-state qos-application-state--${displayedApplicationState}`}>
                  {displayedApplicationState.replaceAll("_", " ")}
                </strong>
                <span>{displayedApplicationState === "applied_verified"
                  ? "Selected profile confirmed by backend controller status readback."
                  : displayedApplicationState === "status_unconfirmed"
                    ? "The apply response succeeded, but current controller status does not confirm this proposed profile."
                    : displayedApplicationState === "verification_failed"
                      ? "Application was not confirmed; current state may be uncertain."
                      : displayedApplicationState === "failed"
                        ? "Application failed; current network state may be uncertain."
                        : displayedApplicationState === "applying"
                          ? "An authorized application request is in progress."
                          : "Proposal only; no application has been confirmed."}</span>
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
              <span>Live experiment: {dryRunDecision.measurement.experiment_id ?? "identity unavailable"}</span>
              <span>{dryRunDecision.measurement.source} · {dryRunDecision.measurement.scenario} · {dryRunDecision.measurement.interface}</span>
              <span>Configured bottleneck capacity: {formatMbps(dryRunDecision.measurement.bandwidth_mbps)} Mbps</span>
              <span>{new Date(dryRunDecision.measurement.timestamp).toLocaleString()} · {formatMbps(dryRunDecision.measurement.throughput_mbps)} Mbps throughput · {formatNumber(dryRunDecision.measurement.utilization_percent)}% utilization</span>
              <span>{formatNumber(dryRunDecision.measurement.latency_ms, 3)} ms latency · {formatNumber(dryRunDecision.measurement.jitter_ms, 3)} ms jitter · {formatNumber(dryRunDecision.measurement.packet_loss_percent)}% loss</span>
            </div>
            <div className="qos-dry-run-policy">
              <div><strong>Proposed {dryRunDecision.selected_profile} HTB profile</strong><span>Proposed QoS allocation budget: {formatMbps(dryRunDecision.proposed_policy.total_bandwidth_mbps)} Mbps · {dryRunDecision.proposed_policy.interface} · currently proposed, not an applied-state claim</span></div>
              <div className="qos-dry-run-rates">
                {Object.entries(dryRunDecision.proposed_policy.service_allocations_mbps).map(([service, rate]) => <span key={service}>{service}: {formatMbps(rate)} Mbps</span>)}
                <span>unmatched: {formatMbps(dryRunDecision.proposed_policy.unmatched_rate_mbps)} Mbps</span>
              </div>
              <p className="field-help">The managed profile reserves {formatMbps(dryRunDecision.proposed_policy.unmatched_rate_mbps)} Mbps for unmatched traffic; the remaining {formatMbps(Object.values(dryRunDecision.proposed_policy.service_allocations_mbps).reduce((total, rate) => total + rate, 0))} Mbps is shared by the service classes. Each class ceiling equals its configured rate, so unused capacity may not be borrowed by other classes.</p>
              <details>
                <summary>
                  Proposed <code>tc</code> commands — {tcPreviewLifecycleLabel}
                </summary>
                <p className="field-help">{tcPreviewNotice}</p>
                {tcPreviewUnavailableReason ? (
                  <p className="qos-dry-run-note" role="status">
                    Command preview unavailable: {tcPreviewUnavailableReason}
                  </p>
                ) : (
                  <>
                    <p className="field-help">Profile {dryRunDecision.selected_profile} on {dryRunDecision.proposed_policy.interface}.</p>
                    <button className="button button--secondary" type="button" onClick={() => void copyTcCommands()}>
                      Copy commands
                    </button>
                    {tcCommandCopyStatus && <p className="qos-dry-run-note" role="status">{tcCommandCopyStatus}</p>}
                    <pre><code>{tcPreviewCommands.join("\n")}</code></pre>
                  </>
                )}
              </details>
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
                aria-describedby={!dryRunDecision.application_enabled ? "apply-qos-disabled-reason" : undefined}
                title={!dryRunDecision.application_enabled ? dryRunDecision.application_blockers.join(", ") : undefined}
              >
                {applicationState === "applying" ? "Applying…" : "Apply QoS Profile"}
              </button>
            )}
            {!dryRunDecision.application_enabled && (
              <p className="qos-dry-run-note" id="apply-qos-disabled-reason">Apply is disabled: {dryRunDecision.application_blockers.map((blocker) => blocker === "application_disabled" ? "ADAPTIVE_QOS_ENABLE_APPLICATION is not enabled. " : "The backend does not guarantee exclusive tc ownership (LIVE_TC_OWNERSHIP_GUARANTEED=false). ").join("") || "The backend has not confirmed all application safety gates."} Exclusive ownership, verified preflight/readback, and reliable restoration are required before live application is safe.</p>
            )}
            {dryRunDecision.candidate_profile && (
              <p className="qos-dry-run-note">Application is disabled while the candidate profile awaits hysteresis and cooldown stabilization.</p>
            )}
            <p className="qos-dry-run-note">{dryRunDecision.message}</p>
          </>
        ) : (
          <div className={`qos-dry-run-empty qos-dry-run-empty--${dryRunDecision?.status ?? "loading"}`} role="status">
            <strong>{dryRunDecision?.telemetry_collection_status === "collection_failed"
              ? "Telemetry collection failed"
              : dryRunDecision?.telemetry_collection_status === "no_measurement"
                ? "No measurement available"
                : dryRunDecision?.status === "stale"
                  || dryRunDecision?.telemetry_status === "stale"
                  || dryRunDecision?.telemetry_collection_status === "stale"
                  ? "Telemetry is stale" : "No safe QoS decision available"}</strong>
            <span>{dryRunDecision?.message ?? "Waiting for a validated telemetry response."}</span>
            {!previewTelemetryFresh && dryRunDecision && (
              <span>Command preview unavailable: {tcPreviewUnavailableReason}</span>
            )}
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
              <p>Experiment {record.experimentId ?? "unavailable"} · {record.scenario ?? "Scenario unavailable"}</p>
              <p>Measurement {record.timestamp ?? "Timestamp unavailable"}</p>
              <p>Report source host: {record.source ?? "unknown"}{dryRunDecision?.measurement?.experiment_id && record.experimentId !== dryRunDecision.measurement.experiment_id
                ? " · different from the live telemetry experiment"
                : dryRunDecision?.measurement?.experiment_id && record.experimentId === dryRunDecision.measurement.experiment_id
                  ? " · matches the live telemetry experiment ID"
                  : " · live experiment identity unavailable for comparison"}</p>
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
            <div><h2>{reportSource === "api" ? "Discovered offline plan details" : "Imported offline plan details"}</h2><p>Report {report.runId} · {report.status} · mode {report.mode} · {report.records.length} input records · importing does not rerun prediction</p></div>
            {report.records.length > 1 && (
              <label className="record-select">Record
                <select value={selectedRecord} onChange={(event) => setSelectedRecord(Number(event.target.value))}>
                  {report.records.map((item, index) => <option key={item.line} value={index}>Line {item.line} · {item.source ?? "source unknown"}</option>)}
                </select>
              </label>
            )}
          </div>
          {record?.flowAllocationsMbps ? (
            <>
            {completeBudget ? (
              <div className="plan-budget" aria-label={`Planned allocation total ${formatMbps(budgetTotal!)} of ${formatMbps(budgetTotal!)} megabits per second`}>
                <div className="plan-budget__heading"><strong>Planned budget · {formatMbps(budgetTotal!)} Mbps total</strong><span>{formatMbps(budgetTotal! - record!.unmatchedRateMbps!)} Mbps services + {formatMbps(record!.unmatchedRateMbps!)} Mbps unmatched</span></div>
                <div className="plan-budget__bar" role="img" aria-label="Stacked proposed allocation by traffic class">
                  {(["gaming", "video", "web", "bulk", "unmatched"] as const).map((key) => {
                    const amount = key === "unmatched" ? record!.unmatchedRateMbps! : allocationServices![key];
                    const title = `${key}: ${formatMbps(amount)} Mbps`;
                    return <span key={key} title={title} aria-label={title} style={{ width: `${amount / budgetTotal! * 100}%` }} />;
                  })}
                </div>
                <p>{validPolicyWeights && policyWeights
                  ? `Planner rationale: service-pool weights gaming ${policyWeights.gaming}%, video ${policyWeights.video}%, web ${policyWeights.web}%, bulk ${policyWeights.bulk}%. Rates are planned and not applied.`
                  : "Budget is balanced; policy weighting rationale was not included in this report."}</p>
                <p>The unmatched reserve is separate from the service pool. Each class ceiling equals its configured rate, so unused capacity may not be borrowed by another class.</p>
              </div>
            ) : <p className="empty-inline">Total budget and allocation rationale are unavailable or inconsistent in this report.</p>}
            <div className="allocation-table-wrap">
              <table className="data-table">
                <thead><tr><th>Flow / port</th><th>Service</th><th>Planned allocation</th><th>Execution</th></tr></thead>
                <tbody>
                  {Object.entries(record.flowAllocationsMbps).map(([port, allocation]) => {
                    const serviceByPort: Record<string, string> = { "5201": "Gaming", "5202": "Video", "5203": "Web", "5204": "Bulk" };
                    return <tr key={port}><td>UDP {port}</td><td>{serviceByPort[port] ?? "Unmapped"}</td><td>{formatMbps(allocation)} Mbps</td><td><span className="status-pill status-pill--muted">Not applied</span></td></tr>;
                  })}
                  {record.unmatchedRateMbps !== null && <tr><td>Default class 1:15</td><td>Unmatched</td><td>{formatMbps(record.unmatchedRateMbps)} Mbps</td><td><span className="status-pill status-pill--muted">Not applied</span></td></tr>}
                </tbody>
              </table>
            </div>
            </>
          ) : (
            <div className="empty-inline"><CircleHelp size={18} /><span>This record has no adaptive flow allocation plan.</span></div>
          )}
          <p className="report-integrity-note">
            Report fields are displayed as supplied. This browser does not re-run prediction, verify referenced evidence files, or apply/restore policy.
          </p>
        </section>
      )}

      <section className="section-heading dashboard-section-heading">
        <div><h2>Available managed profiles</h2><p>Each profile splits a {formatMbps(PROFILE_TOTAL_Mbps - PROFILE_ALLOCATIONS.LOW.unmatched)} Mbps service pool using its class weights and reserves {formatMbps(PROFILE_ALLOCATIONS.LOW.unmatched)} Mbps for unmatched traffic. Class ceilings equal their configured rates, so unused capacity may not be borrowed; these planned rates are not evidence of applied bandwidth.</p></div>
        <span className="status-pill status-pill--surface">{formatMbps(profileBudgetMbps)} Mbps total incl. unmatched</span>
      </section>
      <div className="profile-grid">
        {PROFILE_NAMES.map((profile) => (
          <article className={`card profile-card ${record?.prediction === profile ? "profile-card--selected" : ""}`} key={profile}>
            <div className="profile-card__header"><span>{profile}</span><small>Service rates · Mbps</small></div>
            <div className="profile-allocation">
              {(["gaming", "video", "web", "bulk"] as const).map((service) => (
                <div key={service}><span>{service}</span><strong>{formatMbps(PROFILE_ALLOCATIONS[profile][service])}</strong></div>
              ))}
              <div className="profile-allocation__unmatched"><span>Unmatched · 1:15</span><strong>{formatMbps(PROFILE_ALLOCATIONS[profile].unmatched)}</strong></div>
            </div>
            <p>{record?.prediction === profile ? "Selected in imported plan" : "Planner profile"}</p>
          </article>
        ))}
      </div>
    </div>
  );
}
