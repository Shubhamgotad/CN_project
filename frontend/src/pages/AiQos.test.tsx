import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiQosDryRunDecision } from "../api/client";
import type { ImportedRunnerReport } from "../types";
import {
  apiReportToRunnerReport,
  ApiRequestError,
  applyQosProfile,
  getReport,
  getQosApplicationStatus,
  getQosDryRunDecision,
  getReports,
} from "../api/client";
import { AiQos } from "./AiQos";

vi.mock("../api/client", () => ({
  apiReportToRunnerReport: vi.fn(),
  backendConnection: { baseUrl: "http://127.0.0.1:8000", message: "local" },
  getReport: vi.fn(),
  getReports: vi.fn(),
  getQosApplicationStatus: vi.fn(),
  getQosDryRunDecision: vi.fn(),
  applyQosProfile: vi.fn(),
  ApiRequestError: class extends Error {
    statusCode: number;
    lifecycleState?: "failed" | "verification_failed";
    constructor(message: string, statusCode: number, lifecycleState?: "failed" | "verification_failed") {
      super(message);
      this.statusCode = statusCode;
      this.lifecycleState = lifecycleState;
    }
  },
  parseRunnerReport: vi.fn(),
}));

const getDecisionMock = vi.mocked(getQosDryRunDecision);
const getReportsMock = vi.mocked(getReports);
const getReportMock = vi.mocked(getReport);
const apiReportToRunnerReportMock = vi.mocked(apiReportToRunnerReport);
const getQosStatusMock = vi.mocked(getQosApplicationStatus);
const applyQosProfileMock = vi.mocked(applyQosProfile);
let root: Root;
let container: HTMLDivElement;

function decision(overrides: Partial<ApiQosDryRunDecision> = {}): ApiQosDryRunDecision {
  return {
    status: "ready",
    dry_run: true,
    applied: false,
    execution_state: "dry_run",
    application_enabled: false,
    application_blockers: ["application_disabled", "exclusive_tc_ownership_unavailable"],
    telemetry_status: "available",
    telemetry_source: "configured_person1_jsonl",
    telemetry_experiment_id: "b".repeat(32),
    measurement_age_seconds: 2.5,
    measurement: {
      timestamp: "2026-10-20T10:00:00+00:00",
      experiment_id: "b".repeat(32),
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
    tc_commands: [
      "tc qdisc replace dev s1-eth5 parent 5:1 handle 1: htb default 15",
      "tc class add dev s1-eth5 parent 1: classid 1:1 htb rate 10mbit ceil 10mbit",
      "tc class add dev s1-eth5 parent 1:1 classid 1:11 htb rate 2.7mbit ceil 2.7mbit",
      "tc qdisc add dev s1-eth5 parent 1:11 handle 110: netem limit 1000 delay 10ms",
      "tc class add dev s1-eth5 parent 1:1 classid 1:12 htb rate 2.7mbit ceil 2.7mbit",
      "tc qdisc add dev s1-eth5 parent 1:12 handle 120: netem limit 1000 delay 10ms",
      "tc class add dev s1-eth5 parent 1:1 classid 1:13 htb rate 2.25mbit ceil 2.25mbit",
      "tc qdisc add dev s1-eth5 parent 1:13 handle 130: netem limit 1000 delay 10ms",
      "tc class add dev s1-eth5 parent 1:1 classid 1:14 htb rate 1.35mbit ceil 1.35mbit",
      "tc qdisc add dev s1-eth5 parent 1:14 handle 140: netem limit 1000 delay 10ms",
      "tc class add dev s1-eth5 parent 1:1 classid 1:15 htb rate 1mbit ceil 1mbit",
      "tc qdisc add dev s1-eth5 parent 1:15 handle 150: netem limit 1000 delay 10ms",
      "tc filter add dev s1-eth5 protocol ip parent 1: prio 10 u32 match ip protocol 17 0xff match ip dport 5201 0xffff flowid 1:11",
      "tc filter add dev s1-eth5 protocol ip parent 1: prio 20 u32 match ip protocol 17 0xff match ip dport 5202 0xffff flowid 1:12",
      "tc filter add dev s1-eth5 protocol ip parent 1: prio 30 u32 match ip protocol 17 0xff match ip dport 5203 0xffff flowid 1:13",
      "tc filter add dev s1-eth5 protocol ip parent 1: prio 40 u32 match ip protocol 17 0xff match ip dport 5204 0xffff flowid 1:14",
    ],
    history: [],
    message: "Policy is a dry-run proposal only; no network configuration was changed.",
    ...overrides,
  };
}

async function render(report: ImportedRunnerReport | null = null) {
  await act(async () => {
    root.render(<AiQos report={report} reportSource="api" onReport={vi.fn()} />);
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  getReportsMock.mockReset().mockResolvedValue({ status: "available", directory_status: "valid", message: "No reports", reports: [] });
  getReportMock.mockReset();
  apiReportToRunnerReportMock.mockReset();
  getDecisionMock.mockReset().mockResolvedValue(decision());
  getQosStatusMock.mockReset().mockResolvedValue({
    status: "unrecognized",
    applied_profile: null,
    interface: "s1-eth5",
    message: "No recognized managed profile.",
  });
  applyQosProfileMock.mockReset().mockResolvedValue({
    status: "applied_verified",
    dry_run: false,
    applied: true,
    profile: "LOW",
    interface: "s1-eth5",
    measurement_timestamp: "2026-10-20T10:00:00+00:00",
    verification: "kernel_readback_verified",
    message: "Applied and verified.",
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("AI & QoS dry-run decision panel", () => {
  it("displays the returned telemetry and proposed policy as not applied", async () => {
    await render();
    expect(container.textContent).toContain("AI & QoS Decision");
    expect(container.textContent).toContain("LIVE_TC_OWNERSHIP_GUARANTEED=false");
    expect(container.textContent).toContain("Telemetry available");
    expect(container.textContent).toContain("3.00 Mbps throughput");
    expect(container.textContent).toContain("Configured bottleneck capacity: 10 Mbps");
    expect(container.textContent).toContain("20.63 ms latency");
    expect(container.textContent).toContain("Proposed LOW HTB profile");
    expect(container.textContent).toContain("Proposed QoS allocation budget: 10 Mbps");
    expect(container.textContent).toContain("remaining 9 Mbps is shared by the service classes");
    expect(container.textContent).toContain("unused capacity may not be borrowed by other classes");
    expect(container.textContent).toContain("10 Mbps total incl. unmatched");
    expect(container.textContent).toContain("9 Mbps service pool using its class weights and reserves 1 Mbps for unmatched traffic");
    expect(container.textContent).toContain("Proposed tc commands — dry run only; not executed");
    expect(container.textContent).toContain("These commands illustrate the proposed configuration only. This page does not execute them or change network settings.");
    expect(container.textContent).toContain("no network configuration was changed");
    expect(container.querySelector(".qos-application-state")?.textContent).toBe("dry run");
    expect(container.textContent).toContain("Proposal only; no application has been confirmed.");
    expect(container.textContent).not.toContain("controller applied and verified");
    expect(getDecisionMock).toHaveBeenCalledTimes(1);

    const details = container.querySelector("details");
    const summary = details?.querySelector("summary");
    await act(async () => summary?.click());
    expect(details?.open).toBe(true);
    expect(details?.textContent).toContain("Profile LOW on s1-eth5.");
    expect(details?.querySelector("pre code")?.textContent).toBe(decision().tc_commands.join("\n"));
    const applyButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Apply QoS Profile");
    expect(applyButton?.disabled).toBe(true);
    expect(applyQosProfileMock).not.toHaveBeenCalled();
  });

  it("copies only the proposed command strings without calling the apply endpoint", async () => {
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    try {
      await render();
      const details = container.querySelector("details");
      await act(async () => details?.querySelector("summary")?.click());
      const copyButton = [...container.querySelectorAll("button")]
        .find((button) => button.textContent === "Copy commands");
      await act(async () => {
        copyButton?.click();
        await Promise.resolve();
      });

      expect(writeText).toHaveBeenCalledWith(decision().tc_commands.join("\n"));
      expect(container.textContent).toContain("Proposed commands copied.");
      expect(applyQosProfileMock).not.toHaveBeenCalled();
    } finally {
      if (originalClipboard) {
        Object.defineProperty(navigator, "clipboard", originalClipboard);
      } else {
        Reflect.deleteProperty(navigator, "clipboard");
      }
    }
  });

  it.each(["LOW", "MEDIUM", "HIGH"] as const)(
    "displays the backend-provided %s profile and configured interface",
    async (profile) => {
      const gamingRate = { LOW: "2.7", MEDIUM: "3.6", HIGH: "4.5" }[profile];
      const returnedCommands = [
        `tc class add dev s1-eth5 parent 1:1 classid 1:11 htb rate ${gamingRate}mbit ceil ${gamingRate}mbit`,
      ];
      getDecisionMock.mockResolvedValue(decision({
        selected_profile: profile,
        proposed_policy: { ...decision().proposed_policy!, interface: "s1-eth5" },
        tc_commands: returnedCommands,
      }));
      await render();
      const details = container.querySelector(".qos-dry-run-policy details");
      await act(async () => details?.querySelector("summary")?.click());

      expect(container.textContent).toContain(`Proposed ${profile} HTB profile`);
      expect(details?.textContent).toContain(`Profile ${profile} on s1-eth5.`);
      expect(details?.querySelector("pre code")?.textContent).toBe(returnedCommands.join("\n"));
      expect(applyQosProfileMock).not.toHaveBeenCalled();
    },
  );

  it("explains why a command preview is unavailable instead of inventing commands", async () => {
    getDecisionMock.mockResolvedValue(decision({ tc_commands: [] }));
    await render();
    const details = container.querySelector("details");
    await act(async () => details?.querySelector("summary")?.click());

    expect(details?.textContent).toContain("Command preview unavailable: The backend did not provide proposed commands.");
    expect(details?.querySelector("pre code")).toBeNull();
    expect(applyQosProfileMock).not.toHaveBeenCalled();
  });

  const invalidPreviewCases: Array<{
    label: string;
    overrides: Partial<ApiQosDryRunDecision>;
    explanation: string;
  }> = [
    {
      label: "profile",
      overrides: { selected_profile: null },
      explanation: "The proposed profile is unavailable.",
    },
    {
      label: "interface",
      overrides: {
        proposed_policy: { ...decision().proposed_policy!, interface: "" },
      },
      explanation: "The proposed interface is unavailable.",
    },
    {
      label: "command",
      overrides: { tc_commands: [" "] },
      explanation: "The backend command preview contains an incomplete command.",
    },
  ];

  it.each(invalidPreviewCases)("does not render incomplete commands when the $label is invalid", async ({ overrides, explanation }) => {
    getDecisionMock.mockResolvedValue(decision(overrides));
    await render();
    const details = container.querySelector("details");
    await act(async () => details?.querySelector("summary")?.click());

    expect(details?.textContent).toContain(`Command preview unavailable: ${explanation}`);
    expect(details?.querySelector("pre code")).toBeNull();
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
    expect(container.textContent).toContain("currently proposed, not an applied-state claim");
    expect(container.textContent).toContain("Predicted congestionUnavailable");
    expect(container.textContent).toContain("No model prediction loaded.");
    expect(container.textContent).toContain("No report loaded");
    expect(container.textContent).not.toContain("Prediction generated");
  });

  it("does not show a command preview when telemetry status says stale despite a ready response", async () => {
    getDecisionMock.mockResolvedValue(decision({
      telemetry_status: "stale",
      telemetry_collection_status: "stale",
      measurement_age_seconds: 420,
    }));
    await render();

    expect(container.textContent).toContain("Telemetry is stale");
    expect(container.textContent).toContain("A fresh validated telemetry decision is unavailable.");
    expect(container.querySelector("details")).toBeNull();
    expect(container.textContent).not.toContain("Proposed LOW HTB profile");
  });

  it("shows no policy when telemetry is stale", async () => {
    getDecisionMock.mockResolvedValue(decision({
      status: "stale",
      telemetry_status: "stale",
      measurement_age_seconds: 420,
      telemetry_experiment_id: "c".repeat(32),
      measurement: null,
      classification: null,
      selected_profile: null,
      proposed_policy: null,
      tc_commands: [],
      message: "The explicitly configured ADAPTIVE_QOS_TELEMETRY_FILE measurement is stale: 421159 seconds old; the freshness limit is 300 seconds. Automatic completed-run discovery is not mixed with an explicit file override. Clear that setting to use the newest eligible completed-run measurement.",
    }));
    await render();
    expect(container.textContent).toContain("No decision · stale");
    expect(container.textContent).toContain(`experiment ${"c".repeat(32)}`);
    expect(container.textContent).toContain("Telemetry is stale");
    expect(container.textContent).toContain("421159 seconds old");
    expect(container.textContent).toContain("Clear that setting");
    expect(container.textContent).toContain("To use automatic latest-run discovery");
    expect(container.textContent).toContain("Remove-Item Env:ADAPTIVE_QOS_TELEMETRY_FILE");
    expect(container.textContent).toContain("restart the backend");
    expect(container.textContent).not.toContain("Proposed LOW HTB profile");
    expect(container.querySelector("details")).toBeNull();
    expect(container.querySelector("pre code")).toBeNull();
    expect([...container.querySelectorAll("button")].some((button) => button.textContent === "Copy commands")).toBe(false);
    const refresh = [...container.querySelectorAll("button")].find((button) => button.textContent === "Refresh live telemetry");
    await act(async () => {
      refresh?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getDecisionMock).toHaveBeenCalledTimes(2);
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

  it("does not label a proposal applied when controller readback reports a different profile", async () => {
    getQosStatusMock.mockResolvedValue({
      status: "verified",
      applied_profile: "HIGH",
      interface: "s1-eth5",
      message: "HIGH confirmed by kernel readback.",
    });
    await render();

    const summary = container.querySelector(".qos-dry-run-policy details summary");
    expect(summary?.textContent).toContain("dry run only; not executed");
    expect(summary?.textContent).not.toContain("controller applied and verified");
    expect(container.querySelector(".qos-application-state")?.textContent).toBe("dry run");
  });

  it.each([
    {
      name: "application disabled",
      decisionOverrides: { application_enabled: false },
      status: { status: "unrecognized" as const, applied_profile: null, interface: "s1-eth5" as const, message: "No recognized managed profile." },
    },
    {
      name: "candidate profile pending",
      decisionOverrides: { application_enabled: true, candidate_profile: "HIGH" as const },
      status: { status: "unrecognized" as const, applied_profile: null, interface: "s1-eth5" as const, message: "No recognized managed profile." },
    },
    {
      name: "controller status unavailable",
      decisionOverrides: { application_enabled: true },
      status: { status: "unavailable" as const, applied_profile: null, interface: "s1-eth5" as const, message: "Controller readback unavailable." },
    },
  ])("keeps Apply disabled when $name", async ({ decisionOverrides, status }) => {
    getDecisionMock.mockResolvedValue(decision(decisionOverrides));
    getQosStatusMock.mockResolvedValue(status);
    await render();

    const applyButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Apply QoS Profile");
    expect(applyButton?.disabled).toBe(true);
    expect(applyQosProfileMock).not.toHaveBeenCalled();
  });

  it("requires explicit authorization and displays a verified application", async () => {
    getDecisionMock.mockResolvedValue(decision({ application_enabled: true }));
    getQosStatusMock
      .mockResolvedValueOnce({
        status: "unrecognized",
        applied_profile: null,
        interface: "s1-eth5",
        message: "No recognized managed profile.",
      })
      .mockResolvedValueOnce({
        status: "verified",
        applied_profile: "LOW",
        interface: "s1-eth5",
        message: "Kernel readback verified.",
      });
    await render();

    const applyButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Apply QoS Profile");
    expect(applyButton).toBeDefined();
    await act(async () => applyButton?.click());
    expect(container.textContent).toContain("pending authorization");
    const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(checkbox).not.toBeNull();
    await act(async () => checkbox?.click());
    const confirmButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Confirm Apply QoS Profile");
    await act(async () => {
      confirmButton?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(applyQosProfileMock).toHaveBeenCalledWith(
      "LOW",
      "2026-10-20T10:00:00+00:00",
    );
    expect(container.textContent).toContain("applied verified");
    expect(container.textContent).toContain("Currently applied profileLOW");
    expect(container.textContent).toContain("controller applied and verified");
  });

  it("shows verification failure without claiming the profile was applied", async () => {
    getDecisionMock.mockResolvedValue(decision({ application_enabled: true }));
    applyQosProfileMock.mockRejectedValue(
      new ApiRequestError("Kernel readback did not match.", 502, "verification_failed"),
    );
    await render();
    const applyButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Apply QoS Profile");
    await act(async () => applyButton?.click());
    const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    await act(async () => checkbox?.click());
    const confirmButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Confirm Apply QoS Profile");
    await act(async () => {
      confirmButton?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("verification failed");
    expect(container.textContent).toContain("Kernel readback did not match.");
    expect(container.textContent).not.toContain("applied verified");
    expect(container.querySelector(".qos-dry-run-policy details summary")?.textContent)
      .toContain("application verification failed");
    expect(container.querySelector(".qos-dry-run-policy details summary")?.textContent)
      .not.toContain("dry run only; not executed");
  });

  it("keeps application wording unconfirmed when post-apply controller status cannot verify the profile", async () => {
    getDecisionMock.mockResolvedValue(decision({ application_enabled: true }));
    getQosStatusMock
      .mockResolvedValueOnce({
        status: "unrecognized",
        applied_profile: null,
        interface: "s1-eth5",
        message: "No recognized managed profile.",
      })
      .mockRejectedValueOnce(new Error("Status read failed."))
      .mockResolvedValue({
        status: "unrecognized",
        applied_profile: null,
        interface: "s1-eth5",
        message: "No recognized managed profile.",
      });
    await render();

    const applyButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Apply QoS Profile");
    await act(async () => applyButton?.click());
    const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    await act(async () => checkbox?.click());
    const confirmButton = [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "Confirm Apply QoS Profile");
    await act(async () => {
      confirmButton?.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(container.querySelector(".qos-dry-run-policy details summary")?.textContent)
      .toContain("controller application reported; status unconfirmed");
    expect(container.textContent).toContain("current controller status does not confirm this proposed profile");
    expect(container.querySelector(".qos-dry-run-policy details summary")?.textContent)
      .not.toContain("dry run only; not executed");
  });

  it("clears old decision data and explains API failures", async () => {
    getDecisionMock.mockRejectedValue(new Error("Backend unavailable"));
    await render();
    expect(container.textContent).toContain("Decision API error");
    expect(container.textContent).toContain("Backend unavailable");
    expect(container.textContent).not.toContain("Proposed LOW HTB profile");
  });

  it("shows the report-list loading state until the request resolves", async () => {
    getReportsMock.mockReturnValue(new Promise(() => {}));
    await render();
    expect(container.textContent).toContain("Loading configured reports");
    const refresh = [...container.querySelectorAll("button")].find((button) => button.textContent === "Refreshing…");
    expect(refresh?.disabled).toBe(true);
  });

  it("distinguishes an unset report directory from a valid empty directory", async () => {
    getReportsMock.mockResolvedValue({ status: "unavailable", directory_status: "unset", message: "ADAPTIVE_QOS_REPORT_DIR is unset.", reports: [] });
    await render();
    expect(container.textContent).toContain("Report directory status: unset");
    expect(container.textContent).toContain("ADAPTIVE_QOS_REPORT_DIR is unset.");

    getReportsMock.mockResolvedValue({ status: "available", directory_status: "valid", message: "Configured report directory is valid and contains no compatible reports.", reports: [] });
    const refresh = [...container.querySelectorAll("button")].find((button) => button.textContent === "Refresh reports");
    await act(async () => {
      refresh?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.textContent).toContain("No compatible reports in the valid configured directory.");
  });

  it("offers retry after report-list request errors", async () => {
    getReportsMock.mockRejectedValueOnce(new Error("Backend unavailable"));
    await render();
    expect(container.textContent).toContain("Backend unavailable");
    getReportsMock.mockResolvedValueOnce({ status: "available", directory_status: "valid", message: "No reports", reports: [] });
    const retry = [...container.querySelectorAll("button")].find((button) => button.textContent === "Retry report list");
    expect(retry).toBeDefined();
    await act(async () => {
      retry?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.textContent).toContain("No compatible reports in the valid configured directory.");
  });

  it("loads a discovered report from the configured API without manual import", async () => {
    const discovered: ImportedRunnerReport = {
      kind: "experiment-runner",
      runId: "discovered-runner-report",
      createdAt: "2026-10-25T10:01:00+00:00",
      mode: "adaptive",
      status: "planned",
      records: [{
        line: 1,
        status: "planned",
        experimentId: "c".repeat(32),
        scenario: "normal",
        source: "h1",
        timestamp: "2026-10-25T10:00:00+00:00",
        freshnessStatus: "fresh",
        freshnessAgeSeconds: 2,
        prediction: "LOW",
        confidence: 0.91,
        totalBandwidthMbps: null,
        policyPercentages: null,
        flowAllocationsMbps: { "5201": 2.7 },
        unmatchedRateMbps: 1,
        serviceAllocationsMbps: { gaming: 2.7 },
        inputFeatures: { utilization: 30 },
        measured: { throughput_mbps: 3, latency_ms: 10, jitter_ms: 1, packet_loss_percent: 0 },
        planningOnly: true,
      }],
    };
    getReportsMock.mockResolvedValue({
      status: "available",
      directory_status: "valid",
      message: "Compatible reports available.",
      reports: [{
        report_id: "report-id",
        created_at: discovered.createdAt,
        input_record_count: 1,
        freshness_status: "fresh",
        report_type: "experiment-runner",
        status: "planned",
        campaign_status: null,
        alignment_eligibility: "ineligible",
        comparison_ready: false,
      }],
    });
    getReportMock.mockResolvedValue({} as Awaited<ReturnType<typeof getReport>>);
    apiReportToRunnerReportMock.mockReturnValue(discovered);
    let loaded: ImportedRunnerReport | null = null;
    const onReport = (value: ImportedRunnerReport) => { loaded = value; };
    await act(async () => {
      root.render(<AiQos report={null} reportSource="api" onReport={onReport} />);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.textContent).toContain("1 input records");
    const load = [...container.querySelectorAll("button")].find((button) => button.textContent === "Load plan");
    await act(async () => {
      load?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getReportMock).toHaveBeenCalledWith("report-id");
    expect(loaded).toEqual(discovered);
    await act(async () => {
      root.render(<AiQos report={loaded} reportSource="api" onReport={onReport} />);
      await Promise.resolve();
    });
    expect(container.textContent).toContain("Discovered offline plan details");
    expect(container.textContent).toContain("c".repeat(32));
  });

  it("shows offline experiment evidence and distinguishes it from live telemetry", async () => {
    const offline: ImportedRunnerReport = {
      kind: "experiment-runner",
      runId: "runner-report-1",
      createdAt: "2026-10-20T10:01:00+00:00",
      mode: "adaptive",
      status: "planned",
      records: [{
        line: 1,
        status: "planned",
        experimentId: "a".repeat(32),
        scenario: "congestion",
        source: "h2",
        timestamp: "2026-10-20T10:00:00+00:00",
        freshnessStatus: "fresh",
        freshnessAgeSeconds: 2,
        prediction: "HIGH",
        confidence: 0.87,
        totalBandwidthMbps: 10,
        policyPercentages: { gaming: 40, video: 30, web: 20, bulk: 10 },
        flowAllocationsMbps: { "5201": 3.6, "5202": 2.7, "5203": 1.8, "5204": 0.9 },
        unmatchedRateMbps: 1,
        serviceAllocationsMbps: { gaming: 3.6, video: 2.7, web: 1.8, bulk: 0.9 },
        inputFeatures: { utilization: 90 },
        measured: { throughput_mbps: 8, latency_ms: 40, jitter_ms: 2, packet_loss_percent: 1 },
        planningOnly: true,
      }],
    };
    await render(offline);
    expect(container.textContent).toContain("a".repeat(32));
    expect(container.textContent).toContain("congestion");
    expect(container.textContent).toContain("Report source host: h2");
    expect(container.textContent).toContain("Freshness: fresh");
    expect(container.textContent).toContain("Confidence 87.0%");
    expect(container.textContent).toContain("different from the live telemetry experiment");
    expect(container.textContent).toContain("importing does not rerun prediction");
    expect(container.textContent).toContain("Planned budget · 10 Mbps total");
    expect(container.textContent).toContain("Planner rationale: service-pool weights gaming 40%");
    expect(container.textContent).toContain("Rates are planned and not applied.");
  });
});
