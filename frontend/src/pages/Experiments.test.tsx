import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiReportToRunnerReport, getExperimentsStatus, getReport, getReports, getTrafficRunHistory, parseEvidenceReport } from "../api/client";
import type { ImportedEvidenceReport } from "../types";
import { Experiments } from "./Experiments";

vi.mock("../api/client", () => ({
  apiReportToEvidenceReport: vi.fn(),
  apiReportToRunnerReport: vi.fn(),
  backendConnection: { baseUrl: "http://127.0.0.1:8000" },
  getExperimentsStatus: vi.fn(),
  getReport: vi.fn(),
  getReports: vi.fn(),
  getTrafficRunHistory: vi.fn(),
  parseEvidenceReport: vi.fn(),
}));

const reportsMock = vi.mocked(getReports);
const experimentsStatusMock = vi.mocked(getExperimentsStatus);
const trafficRunsMock = vi.mocked(getTrafficRunHistory);
const parseEvidenceReportMock = vi.mocked(parseEvidenceReport);
const runnerReportConversionMock = vi.mocked(apiReportToRunnerReport);
const getReportMock = vi.mocked(getReport);
let root: Root;
let container: HTMLDivElement;

const report: ImportedEvidenceReport = {
  kind: "campaign-validation",
  reportStatus: "complete",
  campaignStatus: "complete",
  completeCycleCount: 2,
  minimumCompleteCycles: 3,
  boundaryEvidenceIntegrityStatus: "verified",
  producerProvenanceStatus: "not_independently_validated",
  clockMappingStatus: "not_independently_validated",
  comparisonReady: false,
  alignmentStatus: "ineligible",
  errors: ["Manifest is missing one baseline artifact."],
  trials: [
    {
      trialId: "trial-1",
      condition: "Baseline",
      profile: "baseline",
      repetitionId: "cycle-1",
      status: "complete",
      alignment: "ineligible",
      flows: [{
        flowId: "h1",
        status: "complete",
        throughputMbps: 2.9,
        latencyMs: null,
        jitterMs: 0.03,
        packetLossPercent: 0,
        queueSize: null,
        queueDrops: null,
      }],
    },
    {
      trialId: "trial-2",
      condition: "QoS LOW",
      profile: "LOW",
      repetitionId: "cycle-2",
      status: "failed",
      alignment: "ineligible",
      errors: ["No matching receiver interval."],
      flows: [],
    },
  ],
};

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  reportsMock.mockReset().mockResolvedValue({
    status: "available",
    directory_status: "valid",
    message: "Configured reports.",
    reports: [],
  });
  experimentsStatusMock.mockReset().mockResolvedValue({
    status: "available",
    report_id: "campaign-1",
    campaign_completeness: "complete",
    complete_cycle_count: 2,
    minimum_complete_cycles: 3,
    boundary_metadata_status: "schema_valid",
    boundary_evidence_integrity_status: "verified",
    producer_provenance_status: "not_independently_validated",
    clock_mapping_status: "not_independently_validated",
    measurement_alignment_eligibility: "ineligible",
    comparison_ready: false,
    message: "Campaign is structurally complete.",
  });
  trafficRunsMock.mockReset().mockResolvedValue({
    status: "available", message: "Validated execution states, not campaign trials.",
    completed_count: 1, failed_count: 0, stopped_count: 0,
    runs: [{ experiment_id: "a".repeat(32), scenario: "normal", status: "completed", source_host: "h1", flows: ["h1"], target_rate_mbps: 2, duration_seconds: 30, started_at: "2026-10-25T10:00:00+00:00", finished_at: "2026-10-25T10:00:30+00:00" }],
  });
  parseEvidenceReportMock.mockReset();
  runnerReportConversionMock.mockReset();
  getReportMock.mockReset();
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("Experiments evidence summary", () => {
  it("separates campaign completeness from comparison eligibility and preserves reported issues", async () => {
    await act(async () => {
      root.render(<Experiments report={report} reportSource="imported" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Campaign completeness");
    expect(container.textContent).toContain("1 / 48");
    expect(container.textContent).toContain("Valid strategy comparisonsNo eligible evidence");
    expect(container.textContent).toContain("Traffic Lab executions");
    expect(container.textContent).toContain("1 runs");
    expect(container.textContent).toContain("not campaign trial evidence");
    expect(container.textContent).toContain("a".repeat(32));
    expect(container.textContent).toContain("normal");
    expect(container.textContent).toContain("completed");
    expect(container.textContent).toContain("h1 · h1");
    expect(container.textContent).toContain("Evidence · blocked");
    expect(container.textContent).toContain("Failed trials1");
    expect(container.textContent).toContain("Manifest is missing one baseline artifact.");
    expect(container.textContent).toContain("No matching receiver interval.");
    expect(container.textContent).toContain("Reported validation issues");
    expect(container.textContent).toContain("Queue drops");
    expect(container.textContent).toContain("Unavailable");
    expect(container.textContent).toContain("No validated baseline comparison is available");
  });

  it("shows API-reported campaign, boundary, provenance and alignment metadata separately", async () => {
    await act(async () => {
      root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("2 / 3 complete cycles");
    expect(container.textContent).toContain("Boundary evidence");
    expect(container.textContent).toContain("verified");
    expect(container.textContent).toContain("Producer provenance");
    expect(container.textContent).toContain("not independently validated");
    expect(container.textContent).toContain("Clock mapping");
    expect(container.textContent).toContain("Alignment eligibility");
    expect(container.textContent).toContain("Comparison eligibility");
    expect(container.textContent).toContain("Blocked");
  });

  it("shows one row per strategy and only aggregates explicitly eligible measurements", async () => {
    const eligibleReport: ImportedEvidenceReport = {
      ...report,
      comparisonReady: true,
      alignmentStatus: "eligible",
      trials: ["Baseline", "QoS LOW", "QoS MEDIUM", "QoS HIGH"].map((condition, index) => ({
        trialId: `trial-${index + 1}`,
        condition,
        profile: condition === "Baseline" ? "baseline" : condition.replace("QoS ", ""),
        repetitionId: "cycle-1",
        status: "complete",
        alignment: "eligible",
        flows: [{
          flowId: "h1", status: "complete", throughputMbps: [2, 2.5, 3, 3.5][index],
          latencyMs: [20, 18, 16, 14][index], jitterMs: 1, packetLossPercent: 0,
          queueSize: null, queueDrops: null,
        }],
      })),
    };
    await act(async () => root.render(<Experiments report={eligibleReport} reportSource="api" onReport={vi.fn()} />));
    expect(container.textContent).toContain("Completed trials4 / 48");
    expect(container.textContent).toContain("Valid strategy comparisons4 / 4");
    expect(container.textContent).toContain("Baseline");
    expect(container.textContent).toContain("QoS LOW");
    expect(container.textContent).toContain("QoS MEDIUM");
    expect(container.textContent).toContain("QoS HIGH");
    expect(container.textContent).toContain("2.5 Mbps");
    expect(container.textContent).toContain("Best: QoS HIGH");
  });

  it("keeps missing reports and missing measurements unavailable", async () => {
    await act(async () => root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />));
    expect(container.textContent).toContain("No campaign evidence reports are available");
    expect(container.textContent).toContain("Campaign report available; comparison blocked");
    expect(container.textContent).toContain("Awaiting report");
    expect(container.textContent).toContain("Not available");
    expect(container.querySelector<HTMLTableElement>('table[aria-label="Strategy comparison metrics"]')).not.toBeNull();
  });

  it("shows planned reports, invalid files and empty execution history without claiming campaign evidence", async () => {
    reportsMock.mockResolvedValueOnce({
      status: "available",
      directory_status: "valid",
      message: "Configured reports.",
      reports: [
        { report_id: "planned-normal", report_type: "experiment-runner", status: "planned", created_at: "2026-10-25T10:00:00+00:00", input_record_count: 1, freshness_status: "fresh", campaign_status: null, alignment_eligibility: "ineligible", comparison_ready: false },
        { report_id: "planned-congestion", report_type: "experiment-runner", status: "planned", created_at: "2026-10-25T11:00:00+00:00", input_record_count: 4, freshness_status: "stale", campaign_status: null, alignment_eligibility: "ineligible", comparison_ready: false },
        { report_id: "invalid-a", report_type: "unknown", status: "invalid", created_at: null, input_record_count: null, freshness_status: "unknown", campaign_status: null, alignment_eligibility: "ineligible", comparison_ready: false },
        { report_id: "invalid-b", report_type: "unknown", status: "invalid", created_at: null, input_record_count: null, freshness_status: "unknown", campaign_status: null, alignment_eligibility: "ineligible", comparison_ready: false },
      ],
    });
    experimentsStatusMock.mockResolvedValueOnce({
      status: "unavailable",
      report_id: null,
      campaign_completeness: "unavailable",
      complete_cycle_count: null,
      minimum_complete_cycles: null,
      boundary_metadata_status: "unavailable",
      boundary_evidence_integrity_status: "unavailable",
      producer_provenance_status: "not_independently_validated",
      clock_mapping_status: "not_independently_validated",
      measurement_alignment_eligibility: "ineligible",
      comparison_ready: false,
      message: "No supported evidence-analysis or campaign report is available.",
    });
    trafficRunsMock.mockResolvedValueOnce({
      status: "available",
      message: "No validated Traffic Lab execution records are available.",
      completed_count: 0,
      failed_count: 0,
      stopped_count: 0,
      runs: [],
    });

    await act(async () => {
      root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Planned experiment reports available; campaign evidence unavailable");
    expect(container.textContent).toContain("no validated campaign/evidence-analysis report is available");
    expect(container.textContent).toContain("2 invalid or incompatible JSON report(s) were excluded");
    expect(container.textContent).toContain("Offline ML prediction reports (planned experiment-runner)");
    expect(container.textContent).toContain("planned-normal");
    expect(container.textContent).toContain("created 2026-10-25T10:00:00+00:00");
    expect(container.textContent).toContain("1 input record");
    expect(container.textContent).toContain("freshness fresh");
    expect(container.textContent).toContain("planned-congestion");
    expect(container.textContent).toContain("4 input records");
    expect(container.textContent).toContain("freshness stale");
    expect(container.textContent).toContain("Invalid or incompatible reports (2)");
    expect(container.textContent).toContain("invalid-a");
    expect(container.textContent).toContain("invalid-b");
    expect(container.textContent).toContain("No Traffic Lab execution records are available.");
    expect(container.textContent).not.toContain("Evidence · blocked");
    expect(container.textContent).not.toContain("48 / 48");
    expect(reportsMock).toHaveBeenCalledTimes(1);
    expect(experimentsStatusMock).toHaveBeenCalledTimes(1);
    expect(trafficRunsMock).toHaveBeenCalledTimes(1);
  });

  it("labels validated campaign evidence separately from comparison eligibility", async () => {
    experimentsStatusMock.mockResolvedValueOnce({
      status: "available",
      report_id: "campaign-validated",
      campaign_completeness: "complete",
      complete_cycle_count: 3,
      minimum_complete_cycles: 3,
      boundary_metadata_status: "schema_valid",
      boundary_evidence_integrity_status: "verified",
      producer_provenance_status: "independently_validated",
      clock_mapping_status: "validated",
      measurement_alignment_eligibility: "eligible",
      comparison_ready: true,
      message: "Validated campaign evidence is available.",
    });
    await act(async () => {
      root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Validated campaign evidence available");
    expect(container.textContent).toContain("Validated campaign evidence is available and eligible for comparison.");
    expect(container.textContent).toContain("Evidence · eligible");
  });

  it("shows an available empty execution history separately from a load failure", async () => {
    trafficRunsMock.mockResolvedValueOnce({
      status: "available",
      message: "No validated Traffic Lab execution records are available.",
      completed_count: 0,
      failed_count: 0,
      stopped_count: 0,
      runs: [],
    });
    await act(async () => {
      root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("0 runs");
    expect(container.textContent).toContain("No Traffic Lab execution records are available.");
    expect(container.textContent).not.toContain("Could not load Traffic Lab executions");
  });

  it("shows a scoped Traffic Lab API error and can retry execution history", async () => {
    trafficRunsMock.mockRejectedValueOnce(new Error("Backend API request failed for GET /api/experiments/runs (404)."));
    await act(async () => {
      root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Load error");
    expect(container.textContent).toContain("Could not load Traffic Lab executions");
    expect(container.textContent).toContain("/api/experiments/runs (404)");
    const retryButton = Array.from(container.querySelectorAll<HTMLButtonElement>(".execution-history-card button"))
      .find((button) => button.textContent === "Retry");
    await act(async () => {
      retryButton?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("1 runs");
    expect(container.textContent).toContain("a".repeat(32));
    expect(container.textContent).not.toContain("Could not load Traffic Lab executions");
  });

  it("exposes loading and recoverable report-list errors", async () => {
    let resolveReports!: (value: Awaited<ReturnType<typeof getReports>>) => void;
    reportsMock.mockReturnValueOnce(new Promise((resolve) => { resolveReports = resolve; }));
    await act(async () => root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />));
    expect(container.textContent).toContain("Loading reports");
    await act(async () => resolveReports({ status: "available", directory_status: "valid", message: "Loaded", reports: [] }));
    reportsMock.mockRejectedValueOnce(new Error("Report service unavailable"));
    await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Refresh reports and campaign status"]')?.click());
    expect(container.textContent).toContain("Report service unavailable");
    expect(container.textContent).toContain("Retry");
  });

  it("shows campaign-status API failures separately from report-list results", async () => {
    experimentsStatusMock.mockRejectedValueOnce(new Error("Campaign status service unavailable"));
    await act(async () => {
      root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Campaign status request failed: Campaign status service unavailable");
    expect(container.textContent).toContain("Retry");
    expect(container.textContent).toContain("Evidence reports");
  });

  it("imports a JSON report and displays parse and validation feedback", async () => {
    const onReport = vi.fn();
    parseEvidenceReportMock.mockReturnValue(report);
    await act(async () => root.render(<Experiments report={null} reportSource="api" onReport={onReport} />));
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    const file = new File(["{}"], "campaign.json", { type: "application/json" });
    Object.defineProperty(file, "text", { value: () => Promise.resolve("{}") });
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); await Promise.resolve(); });
    expect(parseEvidenceReportMock).toHaveBeenCalledWith("{}");
    expect(onReport).toHaveBeenCalledWith(report, "imported");
    expect(container.textContent).toContain("campaign.json");
    expect(onReport.mock.calls[0]?.[0].comparisonReady).toBe(false);

    parseEvidenceReportMock.mockImplementationOnce(() => { throw new Error("Missing trials array"); });
    const invalidFile = new File(["{}"], "invalid.json", { type: "application/json" });
    Object.defineProperty(invalidFile, "text", { value: () => Promise.resolve("{}") });
    Object.defineProperty(input, "files", { configurable: true, value: [invalidFile] });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); await Promise.resolve(); });
    expect(container.textContent).toContain("Import failed: Missing trials array");
  });

  it("keeps discovered ML prediction reports visible but separate from campaign evidence", async () => {
    reportsMock.mockResolvedValue({
      status: "available", directory_status: "valid", message: "Prediction report found.",
      reports: [{ report_id: "prediction-run", created_at: "2026-10-20T10:00:00Z", input_record_count: 1, freshness_status: "stale", report_type: "experiment-runner", status: "planned", campaign_status: null, alignment_eligibility: "ineligible", comparison_ready: false }],
    });
    const importedRunner = {
      kind: "experiment-runner" as const, runId: "prediction-run", createdAt: "2026-10-20T10:00:00Z", mode: "adaptive", status: "planned",
      records: [{ line: 1, status: "planned", experimentId: "b".repeat(32), scenario: "normal", source: "h1", timestamp: "2026-10-20T10:00:00Z", freshnessStatus: "stale", freshnessAgeSeconds: 400, prediction: "LOW" as const, confidence: 0.7, totalBandwidthMbps: 10, policyPercentages: null, flowAllocationsMbps: { "5201": 2.7 }, unmatchedRateMbps: 1, serviceAllocationsMbps: { gaming: 2.7 }, inputFeatures: { utilization: 25 }, measured: { throughput_mbps: 2.9 }, planningOnly: true as const }],
    };
    runnerReportConversionMock.mockReturnValue(importedRunner);
    getReportMock.mockResolvedValue({
      report_id: "prediction-run", report_type: "experiment-runner", created_at: "2026-10-20T10:00:00Z", status: "planned", planning_only: true,
      campaign_status: null, complete_cycle_count: null, minimum_complete_cycles: null, boundary_metadata_status: "not_applicable",
      boundary_evidence_integrity_status: "not_applicable", producer_provenance_status: "not_applicable", clock_mapping_status: "not_applicable",
      alignment_eligibility: "not_applicable", comparison_ready: false, runner_records: [], trials: [],
    });
    await act(async () => root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />));
    expect(container.textContent).toContain("Offline ML prediction reports");
    expect(container.textContent).toContain("not campaign evidence");
    expect(container.textContent).toContain("prediction-run");
    const inspectButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent?.includes("Inspect predictions"));
    await act(async () => inspectButton?.click());
    expect(runnerReportConversionMock).toHaveBeenCalled();
    expect(container.textContent).toContain("Offline ML predictions");
    expect(container.textContent).toContain("stale · 400s");
    expect(container.textContent).toContain("2.7 Mbps");
    expect(container.textContent).toContain("planning only");
  });
});
