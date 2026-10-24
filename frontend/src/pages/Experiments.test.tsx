import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getExperimentsStatus, getReports } from "../api/client";
import type { ImportedEvidenceReport } from "../types";
import { Experiments } from "./Experiments";

vi.mock("../api/client", () => ({
  apiReportToEvidenceReport: vi.fn(),
  backendConnection: { baseUrl: "http://127.0.0.1:8000" },
  getExperimentsStatus: vi.fn(),
  getReport: vi.fn(),
  getReports: vi.fn(),
  parseEvidenceReport: vi.fn(),
}));

const reportsMock = vi.mocked(getReports);
const experimentsStatusMock = vi.mocked(getExperimentsStatus);
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
    expect(container.textContent).toContain("2 / 3 minimum");
    expect(container.textContent).toContain("Alignment and comparison eligibility are separate from campaign completeness");
    expect(container.textContent).toContain("Comparison blocked");
    expect(container.textContent).toContain("1 complete · 1 failed");
    expect(container.textContent).toContain("Manifest is missing one baseline artifact.");
    expect(container.textContent).toContain("No matching receiver interval.");
    expect(container.textContent).toContain("Reported manifest or evidence issues");
    expect(container.textContent).toContain("Queue size");
    expect(container.textContent).toContain("Queue drops");
    expect(container.textContent).toContain("Unavailable");
    expect(container.textContent).toContain("Comparison charts and export are unavailable");
  });

  it("shows API-reported campaign, boundary, provenance and alignment metadata separately", async () => {
    await act(async () => {
      root.render(<Experiments report={null} reportSource="api" onReport={vi.fn()} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Complete cycles");
    expect(container.textContent).toContain("2 / 3 minimum");
    expect(container.textContent).toContain("Boundary evidence");
    expect(container.textContent).toContain("verified");
    expect(container.textContent).toContain("Producer provenance");
    expect(container.textContent).toContain("not_independently_validated");
    expect(container.textContent).toContain("Clock mapping");
    expect(container.textContent).toContain("Alignment eligibility");
    expect(container.textContent).toContain("Comparison eligibility");
    expect(container.textContent).toContain("Blocked");
  });
});
