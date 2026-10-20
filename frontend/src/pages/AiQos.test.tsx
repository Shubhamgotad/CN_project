import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiQosDryRunDecision } from "../api/client";
import { getQosDryRunDecision, getReports } from "../api/client";
import { AiQos } from "./AiQos";

vi.mock("../api/client", () => ({
  apiReportToRunnerReport: vi.fn(),
  backendConnection: { baseUrl: "http://127.0.0.1:8000", message: "local" },
  getReport: vi.fn(),
  getReports: vi.fn(),
  getQosDryRunDecision: vi.fn(),
  parseRunnerReport: vi.fn(),
}));

const getDecisionMock = vi.mocked(getQosDryRunDecision);
const getReportsMock = vi.mocked(getReports);
let root: Root;
let container: HTMLDivElement;

function decision(overrides: Partial<ApiQosDryRunDecision> = {}): ApiQosDryRunDecision {
  return {
    status: "ready",
    dry_run: true,
    applied: false,
    telemetry_status: "available",
    telemetry_source: "configured_person1_jsonl",
    measurement_age_seconds: 2.5,
    measurement: {
      timestamp: "2026-10-20T10:00:00+00:00",
      source: "h1",
      scenario: "normal",
      interface: "s1-eth5",
      throughput_mbps: 2.997,
      bandwidth_mbps: 10,
      utilization_percent: 29.97,
      latency_ms: 20.63,
      jitter_ms: 0.03,
      packet_loss_percent: 0,
      active_flows: 1,
      queue_size: 4,
      queue_drops: 0,
    },
    classifier: "existing_rule_based_thresholds",
    classification: "LOW",
    score: 0,
    selected_profile: "LOW",
    candidate_profile: null,
    transition: "initial_profile_selected",
    hysteresis_samples_required: 2,
    hysteresis_samples_observed: 0,
    cooldown_seconds: 30,
    cooldown_remaining_seconds: 0,
    reasons: ["Utilization 29.97%: medium threshold 60.0%, high threshold 80.0%."],
    proposed_policy: {
      interface: "s1-eth5",
      total_bandwidth_mbps: 10,
      service_allocations_mbps: { gaming: 2.7, video: 2.7, web: 2.25, bulk: 1.35 },
      unmatched_rate_mbps: 1,
      unmatched_ceil_mbps: 1,
      default_class_id: "1:15",
      filter_priorities: { "5201": 10, "5202": 20, "5203": 30, "5204": 40 },
    },
    tc_commands: ["tc qdisc replace dev s1-eth5 root handle 1: htb default 15"],
    history: [],
    message: "Policy is a dry-run proposal only; no network configuration was changed.",
    ...overrides,
  };
}

async function render() {
  await act(async () => {
    root.render(<AiQos report={null} reportSource="api" onReport={vi.fn()} />);
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  getReportsMock.mockReset().mockResolvedValue({ status: "available", message: "No reports", reports: [] });
  getDecisionMock.mockReset().mockResolvedValue(decision());
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("AI & QoS dry-run decision panel", () => {
  it("displays the returned telemetry and proposed policy as not applied", async () => {
    await render();
    expect(container.textContent).toContain("Dry Run — Not Applied");
    expect(container.textContent).toContain("Telemetry available");
    expect(container.textContent).toContain("2.997 Mbps throughput");
    expect(container.textContent).toContain("20.630 ms latency");
    expect(container.textContent).toContain("Proposed LOW HTB profile");
    expect(container.textContent).toContain("tc commands (not executed)");
    expect(container.textContent).toContain("no network configuration was changed");
    expect(getDecisionMock).toHaveBeenCalledTimes(1);
  });

  it("separates a HIGH rule candidate from the current LOW dry-run profile", async () => {
    getDecisionMock.mockResolvedValue(decision({
      classification: "HIGH",
      score: 4,
      selected_profile: "LOW",
      candidate_profile: "HIGH",
      transition: "awaiting_hysteresis",
      hysteresis_samples_observed: 1,
      hysteresis_samples_required: 2,
      cooldown_remaining_seconds: 24.5,
    }));
    await render();

    expect(container.textContent).toContain("Rule-based classification");
    expect(container.textContent).toContain("HIGH · score 4");
    expect(container.textContent).toContain("Current dry-run profileLOW");
    expect(container.textContent).toContain("Candidate profileHIGH");
    expect(container.textContent).toContain("1 / 2");
    expect(container.textContent).toContain("24.5s");
    expect(container.textContent).toContain("Proposed LOW HTB profile");
    expect(container.textContent).toContain("dry-run only · not applied");
    expect(container.textContent).toContain("Predicted congestionUnavailable");
    expect(container.textContent).toContain("No model prediction loaded.");
    expect(container.textContent).toContain("No report loaded");
    expect(container.textContent).not.toContain("Prediction generated");
  });

  it("shows no policy when telemetry is stale", async () => {
    getDecisionMock.mockResolvedValue(decision({
      status: "stale",
      telemetry_status: "stale",
      measurement_age_seconds: 420,
      measurement: null,
      classification: null,
      selected_profile: null,
      proposed_policy: null,
      tc_commands: [],
      message: "Latest telemetry is stale.",
    }));
    await render();
    expect(container.textContent).toContain("No decision · stale");
    expect(container.textContent).toContain("Telemetry is stale");
    expect(container.textContent).not.toContain("Proposed LOW HTB profile");
  });

  it("shows an explicit unavailable state without a proposed policy", async () => {
    getDecisionMock.mockResolvedValue(decision({
      status: "unavailable",
      telemetry_status: "unavailable",
      telemetry_source: "none",
      measurement_age_seconds: null,
      measurement: null,
      classification: null,
      selected_profile: null,
      proposed_policy: null,
      tc_commands: [],
      message: "No valid telemetry is available.",
    }));
    await render();
    expect(container.textContent).toContain("No safe QoS decision available");
    expect(container.textContent).toContain("No valid telemetry is available.");
    expect(container.textContent).not.toContain("Proposed LOW HTB profile");
  });

  it("clears old decision data and explains API failures", async () => {
    getDecisionMock.mockRejectedValue(new Error("Backend unavailable"));
    await render();
    expect(container.textContent).toContain("Decision API error");
    expect(container.textContent).toContain("Backend unavailable");
    expect(container.textContent).not.toContain("Proposed LOW HTB profile");
  });
});
