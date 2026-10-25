import type {
  CongestionClass,
  ImportedEvidenceReport,
  ImportedPlanRecord,
  ImportedRunnerReport,
  ImportedTrial,
  ImportedTrialFlow,
  MetricValue,
} from "../types";

export const backendConnection = {
  baseUrl: import.meta.env.VITE_BACKEND_URL?.trim().replace(/\/+$/, "") || "http://127.0.0.1:8000",
  message: "Dashboard API requests use VITE_BACKEND_URL or the local development API.",
};

type JsonObject = Record<string, unknown>;

export interface ApiHealth {
  status: "ok";
  service: string;
  api_version: string;
  live_network_operations: true;
}

export type ExecutionScenario = "normal" | "moderate" | "congestion" | "burst";
export type ExecutionStatus = "starting" | "running" | "stopping" | "completed" | "failed" | "stopped";

export interface ApiExecution {
  experiment_id: string;
  scenario: ExecutionScenario;
  source_host: "h1" | "h2" | "h3" | "h4" | "all" | null;
  status: ExecutionStatus;
  flows: string[];
  target_rate_mbps: number;
  duration_seconds: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  elapsed_seconds: number;
  process_alive: boolean;
  can_stop: boolean;
  process_exit_code: number | null;
  error: string | null;
}

export interface ApiTopology {
  status: "configured";
  source: string;
  live_connectivity_verified: false;
  bottleneck_interface: string;
  hosts: Array<{
    id: string;
    role: "client" | "server";
    service: string | null;
    udp_destination_port: number | null;
    ipv4_address: string | null;
  }>;
  switches: string[];
  links: Array<{
    source: string;
    target: string;
    bandwidth_mbps: number | null;
    delay: string | null;
  }>;
}

export interface ApiTelemetry {
  status: "available" | "stale" | "unavailable";
  source: "configured_person1_jsonl" | "completed_experiment_person1_jsonl" | "none";
  measurement_age_seconds: number | null;
  stale_after_seconds: number;
  message: string;
  record: {
    timestamp: string;
    experiment_id?: string | null;
    source: string;
    scenario: string;
    throughput_mbps: number;
    latency_ms: number | null;
    jitter_ms: number | null;
    packet_loss_percent: number | null;
    queue_size: number | null;
    queue_drops: number | null;
    measurement_direction: string | null;
    interface: string;
    bandwidth_mbps?: number | null;
    utilization_percent?: number | null;
    active_flows?: number | null;
  } | null;
}

export type TelemetryHistorySource = "configured_person1_jsonl" | "completed_experiment_person1_jsonl";

export interface ApiTelemetryHistoryRecord {
  timestamp: string;
  experiment_id: string;
  scenario: string;
  source: string;
  destination: string;
  traffic_type: string;
  requested_mbps: number;
  throughput_mbps: number;
  latency_ms: number | null;
  jitter_ms: number | null;
  packet_loss_percent: number | null;
  lost_packets: number;
  total_packets: number;
  bandwidth_mbps: number;
  utilization_percent: number;
  active_flows: number;
  queue_size: number | null;
  queue_drops: number | null;
  measurement_direction: string | null;
  interface: string;
  provenance: TelemetryHistorySource;
}

export interface ApiTelemetryHistory {
  status: "available" | "unavailable";
  message: string;
  source_kinds: TelemetryHistorySource[];
  host_filter: string | null;
  scenario_filter: string | null;
  limit: number;
  total_records: number;
  truncated: boolean;
  rejected_records: number;
  records: ApiTelemetryHistoryRecord[];
}

export interface TelemetryHistoryFilters {
  host?: "h1" | "h2" | "h3" | "h4";
  scenario?: "normal" | "moderate" | "congestion" | "burst";
  limit?: number;
}

export interface TelemetryLatestFilters {
  host?: "h1" | "h2" | "h3" | "h4";
  scenario?: "normal" | "moderate" | "congestion" | "burst";
}

export interface ApiQosDryRunDecision {
  status: "ready" | "stale" | "unavailable";
  dry_run: true;
  applied: false;
  execution_state: "dry_run";
  application_enabled: boolean;
  telemetry_status: ApiTelemetry["status"];
  telemetry_source: ApiTelemetry["source"];
  measurement_age_seconds: number | null;
  measurement: {
    timestamp: string;
    source: string;
    scenario: string;
    interface: string;
    throughput_mbps: number;
    bandwidth_mbps: number;
    utilization_percent: number;
    latency_ms: number;
    jitter_ms: number;
    packet_loss_percent: number;
    active_flows: number;
    queue_size: number | null;
    queue_drops: number | null;
  } | null;
  classifier: "existing_rule_based_thresholds";
  classification: CongestionClass | null;
  score: number | null;
  selected_profile: CongestionClass | null;
  candidate_profile: CongestionClass | null;
  transition: string;
  hysteresis_samples_required: number;
  hysteresis_samples_observed: number;
  cooldown_seconds: number;
  cooldown_remaining_seconds: number;
  reasons: string[];
  proposed_policy: {
    interface: string;
    total_bandwidth_mbps: number;
    service_allocations_mbps: Record<string, number>;
    unmatched_rate_mbps: number;
    unmatched_ceil_mbps: number;
    default_class_id: string;
    filter_priorities: Record<string, number>;
  } | null;
  tc_commands: string[];
  history: Array<{
    timestamp: string;
    measurement_timestamp: string;
    classification: CongestionClass;
    selected_profile: CongestionClass;
    transition: string;
  }>;
  message: string;
}

export interface ApiQosApplicationStatus {
  status: "verified" | "unrecognized" | "unavailable";
  applied_profile: CongestionClass | null;
  interface: "s1-eth5";
  message: string;
}

export interface ApiQosApplyResult {
  status: "applied_verified";
  dry_run: false;
  applied: true;
  profile: CongestionClass;
  interface: "s1-eth5";
  measurement_timestamp: string;
  verification: "kernel_readback_verified";
  message: string;
}

export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly statusCode: number,
    readonly lifecycleState?: "failed" | "verification_failed",
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

export interface ApiReportSummary {
  report_id: string;
  report_type: "experiment-runner" | "evidence-analysis" | "campaign-validation" | "unknown";
  status: string;
  campaign_status: string | null;
  alignment_eligibility: string;
  comparison_ready: boolean;
}

export interface ApiReports {
  status: "available" | "unavailable";
  message: string;
  reports: ApiReportSummary[];
}

export interface ApiReportDetail {
  report_id: string;
  report_type: "experiment-runner" | "evidence-analysis" | "campaign-validation";
  created_at: string | null;
  status: string;
  planning_only: boolean;
  campaign_status: string | null;
  complete_cycle_count: number | null;
  minimum_complete_cycles: number | null;
  boundary_metadata_status: string;
  boundary_evidence_integrity_status: string;
  producer_provenance_status: string;
  clock_mapping_status: string;
  alignment_eligibility: string;
  comparison_ready: boolean;
  runner_records: Array<{
    line: number;
    status: string;
    experiment_id: string | null;
    scenario: string | null;
    source: string | null;
    timestamp: string | null;
    freshness_status: string;
    freshness_age_seconds: number | null;
    prediction: CongestionClass | null;
    confidence: number | null;
    input_features: Record<string, number>;
    measured: Record<string, number | null>;
    service_allocations_mbps: Record<string, number>;
    flow_allocations_mbps: Record<string, number>;
    planning_only: true;
  }>;
  trials: Array<{
    trial_id: string;
    condition: string;
    profile: string;
    repetition_id: string;
    status: string;
    alignment_status: string;
    boundary_metadata_status: string;
    boundary_evidence_integrity_status: string;
    producer_provenance_status: string;
    clock_mapping_status: string;
    flows: Array<{
      flow_id: string;
      status: string;
      metrics: Record<string, number | null>;
    }>;
  }>;
}

export interface ApiExperimentsStatus {
  status: "available" | "unavailable";
  report_id: string | null;
  campaign_completeness: string;
  complete_cycle_count: number | null;
  minimum_complete_cycles: number | null;
  boundary_metadata_status: string;
  boundary_evidence_integrity_status: string;
  producer_provenance_status: string;
  clock_mapping_status: string;
  measurement_alignment_eligibility: string;
  comparison_ready: boolean;
  message: string;
}

async function requestJson<T>(path: string, options: RequestInit = {}, allowNull = false): Promise<T> {
  if (!backendConnection.baseUrl) {
    throw new Error("Backend API URL is not configured. Set VITE_BACKEND_URL to enable API requests.");
  }
  const response = await fetch(`${backendConnection.baseUrl.replace(/\/+$/, "")}${path}`, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers ?? {}) },
  });
  let payload: unknown;
  try {
    payload = await response.json() as unknown;
  } catch {
    throw new Error(`Backend API returned invalid JSON (${response.status}).`);
  }
  if (!response.ok) {
    const detail = payload !== null && typeof payload === "object" && !Array.isArray(payload)
      ? (payload as JsonObject).detail
      : null;
    const method = (options.method ?? "GET").toUpperCase();
    let lifecycleState: ApiRequestError["lifecycleState"];
    let explanation: string | null = typeof detail === "string" ? detail : null;
    if (detail !== null && typeof detail === "object" && !Array.isArray(detail)) {
      const errorDetail = detail as JsonObject;
      if (typeof errorDetail.message === "string") explanation = errorDetail.message;
      if (errorDetail.state === "failed" || errorDetail.state === "verification_failed") {
        lifecycleState = errorDetail.state;
      }
    }
    if (!explanation && Array.isArray(detail)) {
      const validationMessages = detail.slice(0, 4).flatMap((item) => {
        if (item === null || typeof item !== "object" || Array.isArray(item)) return [];
        const validation = item as JsonObject;
        if (typeof validation.msg !== "string") return [];
        const location = Array.isArray(validation.loc)
          ? validation.loc.filter((part): part is string | number => typeof part === "string" || typeof part === "number").join(".")
          : "request";
        return [`${location}: ${validation.msg}`];
      });
      if (validationMessages.length > 0) explanation = validationMessages.join("; ");
    }
    throw new ApiRequestError(
      explanation
        ? `Backend rejected ${method} ${path} (${response.status}): ${explanation}`
        : `Backend API request failed for ${method} ${path} (${response.status}).`,
      response.status,
      lifecycleState,
    );
  }
  if (payload === null && allowNull) return payload as T;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Backend API returned an unexpected response shape.");
  }
  return payload as T;
}

export function getBackendHealth(): Promise<ApiHealth> {
  return requestJson<ApiHealth>("/api/health");
}

export function getTopology(): Promise<ApiTopology> {
  return requestJson<ApiTopology>("/api/topology");
}

export function getLatestTelemetry(filters: TelemetryLatestFilters = {}): Promise<ApiTelemetry> {
  const params = new URLSearchParams();
  if (filters.host) params.set("host", filters.host);
  if (filters.scenario) params.set("scenario", filters.scenario);
  const query = params.size ? `?${params.toString()}` : "";
  return requestJson<ApiTelemetry>(`/api/telemetry/latest${query}`);
}

export function getTelemetryHistory(filters: TelemetryHistoryFilters = {}): Promise<ApiTelemetryHistory> {
  const params = new URLSearchParams();
  if (filters.host) params.set("host", filters.host);
  if (filters.scenario) params.set("scenario", filters.scenario);
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  const query = params.size ? `?${params.toString()}` : "";
  return requestJson<ApiTelemetryHistory>(`/api/telemetry/history${query}`);
}

export function getQosDryRunDecision(): Promise<ApiQosDryRunDecision> {
  return requestJson<ApiQosDryRunDecision>("/api/qos/decision");
}

export function getQosApplicationStatus(): Promise<ApiQosApplicationStatus> {
  return requestJson<ApiQosApplicationStatus>("/api/qos/status");
}

export function applyQosProfile(
  profile: CongestionClass,
  measurement_timestamp: string,
): Promise<ApiQosApplyResult> {
  return requestJson<ApiQosApplyResult>("/api/qos/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      profile,
      measurement_timestamp,
      authorization: "APPLY_QOS_PROFILE",
    }),
  });
}

export function getReports(): Promise<ApiReports> {
  return requestJson<ApiReports>("/api/reports");
}

export function getReport(reportId: string): Promise<ApiReportDetail> {
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(reportId)) {
    throw new Error("Invalid report identifier.");
  }
  return requestJson<ApiReportDetail>(`/api/reports/${encodeURIComponent(reportId)}`);
}

export function getExperimentsStatus(): Promise<ApiExperimentsStatus> {
  return requestJson<ApiExperimentsStatus>("/api/experiments/status");
}

export function startTrafficExecution(
  scenario: ExecutionScenario,
  rate_mbps: number,
  duration_seconds: number,
  source_host: "h1" | "h2" | "h3" | "h4" | "all",
): Promise<ApiExecution> {
  return requestJson<ApiExecution>("/api/experiments/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario, source_host, rate_mbps, duration_seconds }),
  });
}

export function getTrafficExecution(experimentId: string): Promise<ApiExecution> {
  if (!/^[0-9a-f]{32}$/.test(experimentId)) throw new Error("Invalid experiment identifier.");
  return requestJson<ApiExecution>(`/api/execution/${encodeURIComponent(experimentId)}`);
}

export function getActiveTrafficExecution(): Promise<ApiExecution | null> {
  return requestJson<ApiExecution | null>("/api/execution/active", {}, true);
}

export function stopTrafficExecution(experimentId: string): Promise<ApiExecution> {
  if (!/^[0-9a-f]{32}$/.test(experimentId)) throw new Error("Invalid experiment identifier.");
  return requestJson<ApiExecution>(`/api/experiments/${encodeURIComponent(experimentId)}/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

export function apiReportToEvidenceReport(detail: ApiReportDetail): ImportedEvidenceReport {
  if (detail.report_type === "experiment-runner") {
    throw new Error("Experiment-runner plans are not evidence reports.");
  }
  const trials = detail.trials.map((trial) => ({
    trialId: trial.trial_id,
    condition: trial.condition,
    profile: trial.profile,
    repetitionId: trial.repetition_id,
    status: trial.status,
    alignment: trial.alignment_status,
    boundaryMetadataStatus: trial.boundary_metadata_status,
    boundaryEvidenceIntegrityStatus: trial.boundary_evidence_integrity_status,
    producerProvenanceStatus: trial.producer_provenance_status,
    clockMappingStatus: trial.clock_mapping_status,
    flows: trial.flows.map((flow) => ({
      flowId: flow.flow_id,
      status: flow.status,
      throughputMbps: finiteNumber(flow.metrics.throughput_mbps),
      latencyMs: finiteNumber(flow.metrics.latency_ms),
      jitterMs: finiteNumber(flow.metrics.jitter_ms),
      packetLossPercent: finiteNumber(flow.metrics.packet_loss_percent),
      queueSize: null,
      queueDrops: null,
    })),
  }));
  return {
    kind: detail.report_type,
    reportStatus: detail.campaign_status ?? detail.status,
    campaignStatus: detail.campaign_status,
    completeCycleCount: detail.complete_cycle_count,
    minimumCompleteCycles: detail.minimum_complete_cycles,
    boundaryMetadataStatus: detail.boundary_metadata_status,
    boundaryEvidenceIntegrityStatus: detail.boundary_evidence_integrity_status,
    producerProvenanceStatus: detail.producer_provenance_status,
    clockMappingStatus: detail.clock_mapping_status,
    comparisonReady: detail.comparison_ready
      && detail.alignment_eligibility === "eligible"
      && trials.length > 0
      && trials.every((trial) => trial.alignment === "eligible"),
    alignmentStatus: detail.alignment_eligibility,
    trials,
  };
}

export function apiReportToRunnerReport(detail: ApiReportDetail): ImportedRunnerReport {
  if (detail.report_type !== "experiment-runner") {
    throw new Error("This API report does not contain an experiment-runner plan.");
  }
  if (!detail.runner_records.length) {
    throw new Error("This runner report has no adaptive prediction records.");
  }
  return {
    kind: "experiment-runner",
    runId: detail.report_id,
    createdAt: detail.created_at,
    mode: "adaptive",
    status: detail.status,
    records: detail.runner_records.map((item) => ({
      line: item.line,
      status: item.status,
      experimentId: item.experiment_id,
      scenario: item.scenario,
      source: item.source,
      timestamp: item.timestamp,
      freshnessStatus: item.freshness_status,
      freshnessAgeSeconds: item.freshness_age_seconds,
      prediction: item.prediction,
      confidence: item.confidence,
      flowAllocationsMbps: item.flow_allocations_mbps,
      serviceAllocationsMbps: item.service_allocations_mbps,
      inputFeatures: item.input_features,
      measured: item.measured,
      planningOnly: true,
    })),
  };
}

function object(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object.`);
  }
  return value as JsonObject;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function numberMap(value: unknown): Record<string, number> | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const entries = Object.entries(value);
  if (!entries.length || entries.some(([, item]) => finiteNumber(item) === null)) {
    return null;
  }
  return Object.fromEntries(entries.map(([key, item]) => [key, Number(item)]));
}

function metricMap(value: unknown): Record<string, MetricValue> {
  const source = value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
  return {
    throughput_mbps: finiteNumber(source.throughput_mbps),
    latency_ms: finiteNumber(source.latency_ms),
    jitter_ms: finiteNumber(source.jitter_ms),
    packet_loss_percent: finiteNumber(source.packet_loss_percent),
  };
}

function className(value: unknown): CongestionClass | null {
  return value === "LOW" || value === "MEDIUM" || value === "HIGH" ? value : null;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function parsePlanRecord(value: unknown, index: number): ImportedPlanRecord {
  const result = object(value, `results[${index}]`);
  const modes = result.modes !== null && typeof result.modes === "object"
    && !Array.isArray(result.modes)
    ? result.modes as JsonObject
    : {};
  const adaptive = modes.adaptive !== null && typeof modes.adaptive === "object"
    && !Array.isArray(modes.adaptive)
    ? modes.adaptive as JsonObject
    : {};
  const prediction = adaptive.prediction === null || adaptive.prediction === undefined
    ? null
    : object(adaptive.prediction, `results[${index}].modes.adaptive.prediction`);
  const confidence = prediction ? finiteNumber(prediction.confidence) : null;
  const window = object(result.window ?? {}, `results[${index}].window`);
  const freshness = object(result.freshness ?? {}, `results[${index}].freshness`);

  return {
    line: finiteNumber(result.line) ?? index + 1,
    status: optionalString(adaptive.status) ?? optionalString(result.status) ?? "unknown",
    experimentId: optionalString(result.experiment_id),
    scenario: optionalString(result.scenario),
    source: optionalString(result.source),
    timestamp: optionalString(window.timestamp),
    freshnessStatus: optionalString(freshness.status) ?? "not evaluated",
    freshnessAgeSeconds: finiteNumber(freshness.age_seconds),
    prediction: className(prediction?.congestion),
    confidence: confidence !== null && confidence >= 0 && confidence <= 1 ? confidence : null,
    flowAllocationsMbps: numberMap(adaptive.flow_allocations_mbps),
    serviceAllocationsMbps: numberMap(adaptive.service_class_allocation_mbps),
    inputFeatures: numberMap(adaptive.input_features),
    measured: metricMap(result.measured),
    planningOnly: true,
  };
}

export function parseRunnerReport(text: string): ImportedRunnerReport {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text) as unknown;
  } catch {
    throw new Error("The selected file is not valid JSON.");
  }
  const report = object(parsed, "runner report");
  if (
    report.schema_version !== 1
    || report.planning_only !== true
    || report.dry_run !== true
    || report.experiment_executed !== false
    || report.measurements_collected !== false
    || report.live_qos_applied !== false
    || report.tc_commands_executed !== false
  ) {
    throw new Error("This is not a recognized dry-run experiment-runner report.");
  }
  if (report.mode !== "adaptive" && report.mode !== "both") {
    throw new Error("The report does not contain adaptive planning results.");
  }
  if (!Array.isArray(report.results)) {
    throw new Error("The runner report is missing its results array.");
  }
  const records = report.results.map(parsePlanRecord);
  if (!records.length) {
    throw new Error("The runner report contains no records to display.");
  }
  return {
    kind: "experiment-runner",
    runId: optionalString(report.run_id) ?? "unknown-run",
    createdAt: optionalString(report.created_at),
    mode: report.mode,
    status: optionalString(report.status) ?? "unknown",
    records,
  };
}

function parseTrialFlow(flowId: string, value: unknown): ImportedTrialFlow {
  const flow = object(value, `flow ${flowId}`);
  const receiver = object(flow.receiver ?? {}, `flow ${flowId}.receiver`);
  const metrics = object(receiver.metrics ?? {}, `flow ${flowId}.receiver.metrics`);
  return {
    flowId,
    status: optionalString(flow.status) ?? "unavailable",
    throughputMbps: finiteNumber(metrics.throughput_mbps),
    latencyMs: finiteNumber(metrics.latency_ms),
    jitterMs: finiteNumber(metrics.jitter_ms),
    packetLossPercent: finiteNumber(metrics.packet_loss_percent),
    queueSize: finiteNumber(metrics.queue_size),
    queueDrops: finiteNumber(metrics.queue_drops),
  };
}

function parseTrial(value: unknown, index: number): ImportedTrial {
  const trial = object(value, `trials[${index}]`);
  const flowsObject = trial.flows !== null && typeof trial.flows === "object"
    && !Array.isArray(trial.flows)
    ? trial.flows as JsonObject
    : {};
  const flows = Object.entries(flowsObject).map(([flowId, flow]) => parseTrialFlow(flowId, flow));
  const aggregate = trial.receiver_window_aggregate !== null
    && typeof trial.receiver_window_aggregate === "object"
    && !Array.isArray(trial.receiver_window_aggregate)
    ? trial.receiver_window_aggregate as JsonObject
    : {};
  const alignment = optionalString(
    trial.measurement_alignment_eligibility
      ?? trial.alignment_status
      ?? aggregate.measurement_alignment_eligibility,
  ) ?? "ineligible";
  return {
    trialId: optionalString(trial.trial_id) ?? `trial-${index + 1}`,
    condition: optionalString(trial.condition) ?? "unspecified",
    profile: optionalString(trial.profile) ?? "unspecified",
    repetitionId: optionalString(trial.repetition_id) ?? "unspecified",
    status: optionalString(trial.status) ?? "unknown",
    alignment,
    flows,
    errors: stringArray(trial.errors),
    boundaryMetadataStatus: optionalString(trial.boundary_metadata_status) ?? undefined,
    boundaryEvidenceIntegrityStatus: optionalString(trial.boundary_evidence_integrity_status) ?? undefined,
    producerProvenanceStatus: optionalString(trial.producer_provenance_status) ?? undefined,
    clockMappingStatus: optionalString(trial.clock_mapping_status) ?? undefined,
  };
}

export function parseEvidenceReport(text: string): ImportedEvidenceReport {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text) as unknown;
  } catch {
    throw new Error("The selected file is not valid JSON.");
  }
  const report = object(parsed, "evidence report");
  if (report.offline_only !== true && report.live_operations_supported !== false) {
    throw new Error("This report does not identify itself as an offline analysis artifact.");
  }
  if (!Array.isArray(report.trials)) {
    throw new Error("The selected report is missing its trials array.");
  }
  const trials = report.trials.map(parseTrial);
  const declaredAlignment = optionalString(
    report.measurement_alignment_eligibility ?? report.alignment_status,
  );
  const comparisonReady = report.comparison_ready === true
    && declaredAlignment === "eligible"
    && trials.length > 0
    && trials.every((trial) => trial.alignment === "eligible");
  const campaignStatus = optionalString(report.campaign_status);
  return {
    kind: report.comparison_ready === undefined ? "evidence-analysis" : "campaign-validation",
    reportStatus: optionalString(report.status) ?? "unknown",
    campaignStatus,
    completeCycleCount: finiteNumber(report.complete_cycle_count),
    minimumCompleteCycles: finiteNumber(report.minimum_complete_cycles),
    boundaryMetadataStatus: optionalString(report.boundary_metadata_status) ?? undefined,
    boundaryEvidenceIntegrityStatus: optionalString(report.boundary_evidence_integrity_status) ?? undefined,
    producerProvenanceStatus: optionalString(report.producer_provenance_status) ?? undefined,
    clockMappingStatus: optionalString(report.clock_mapping_status) ?? undefined,
    comparisonReady,
    alignmentStatus: declaredAlignment ?? (comparisonReady ? "eligible" : "ineligible"),
    trials,
    errors: [
      ...stringArray(report.manifest_errors),
      ...stringArray(report.campaign_errors),
      ...stringArray(report.errors),
    ],
  };
}
