export type CongestionClass = "LOW" | "MEDIUM" | "HIGH";
export type FlowId = "h1" | "h2" | "h3" | "h4";
export type MetricValue = number | null;

export interface ClientFlow {
  id: FlowId;
  name: string;
  service: string;
  port: number;
  className: string;
}

export interface DemoTelemetryPoint {
  time: string;
  h1: number;
  h2: number;
  h3: number;
  h4: number;
  latency: number;
  jitter: number;
  packetLoss: number;
}

export interface ImportedPlanRecord {
  line: number;
  status: string;
  experimentId: string | null;
  scenario: string | null;
  source: string | null;
  timestamp: string | null;
  freshnessStatus: string;
  freshnessAgeSeconds: number | null;
  prediction: CongestionClass | null;
  confidence: number | null;
  flowAllocationsMbps: Record<string, number> | null;
  serviceAllocationsMbps: Record<string, number> | null;
  inputFeatures: Record<string, number> | null;
  measured: Record<string, MetricValue>;
  planningOnly: true;
}

export interface ImportedRunnerReport {
  kind: "experiment-runner";
  runId: string;
  createdAt: string | null;
  mode: string;
  status: string;
  records: ImportedPlanRecord[];
}

export interface ImportedTrialFlow {
  flowId: string;
  status: string;
  throughputMbps: MetricValue;
  latencyMs: MetricValue;
  jitterMs: MetricValue;
  packetLossPercent: MetricValue;
}

export interface ImportedTrial {
  trialId: string;
  condition: string;
  profile: string;
  repetitionId: string;
  status: string;
  alignment: string;
  flows: ImportedTrialFlow[];
}

export interface ImportedEvidenceReport {
  kind: "evidence-analysis" | "campaign-validation";
  reportStatus: string;
  comparisonReady: boolean;
  alignmentStatus: string;
  trials: ImportedTrial[];
}
