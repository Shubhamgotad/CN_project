import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiTelemetry, ApiTelemetryHistory, ApiTelemetryHistoryRecord } from "../api/client";
import { getActiveTrafficExecution, getBackendHealth, getExperimentsStatus, getLatestTelemetry, getTelemetryHistory, getTopology } from "../api/client";
import { Dashboard } from "./Dashboard";

vi.mock("../api/client", () => ({
  backendConnection: { baseUrl: "http://127.0.0.1:8000" },
  getActiveTrafficExecution: vi.fn(),
  getBackendHealth: vi.fn(),
  getExperimentsStatus: vi.fn(),
  getLatestTelemetry: vi.fn(),
  getTelemetryHistory: vi.fn(),
  getTopology: vi.fn(),
}));

const healthMock = vi.mocked(getBackendHealth);
const activeExecutionMock = vi.mocked(getActiveTrafficExecution);
const reportsMock = vi.mocked(getExperimentsStatus);
const telemetryMock = vi.mocked(getLatestTelemetry);
const historyMock = vi.mocked(getTelemetryHistory);
const topologyMock = vi.mocked(getTopology);
let root: Root;
let container: HTMLDivElement;
let unmounted = false;

function record(overrides: Partial<NonNullable<ApiTelemetry["record"]>> = {}): NonNullable<ApiTelemetry["record"]> {
  return {
    timestamp: "2026-10-20T10:11:12.123+00:00",
    source: "h1",
    scenario: "normal",
    throughput_mbps: 2.997,
    latency_ms: 20.6309,
    jitter_ms: 0.0334,
    packet_loss_percent: 0,
    queue_size: 4,
    queue_drops: 0,
    measurement_direction: "receiver",
    interface: "s1-eth5",
    ...overrides,
  };
}

function telemetry(status: ApiTelemetry["status"], value: ApiTelemetry["record"] = record()): ApiTelemetry {
  return {
    status,
    source: status === "unavailable" ? "none" : "completed_experiment_person1_jsonl",
    measurement_age_seconds: status === "available" ? 2.5 : status === "stale" ? 420 : null,
    stale_after_seconds: 300,
    message: status === "available" ? "Latest validated completed experiment record." : status === "stale" ? "Latest validated measurement is stale." : "No valid telemetry is available.",
    record: status === "unavailable" ? null : value,
  };
}

function historyRecord(overrides: Partial<ApiTelemetryHistoryRecord> = {}): ApiTelemetryHistoryRecord {
  return {
    ...record(),
    experiment_id: "test-experiment",
    destination: "server",
    traffic_type: "udp",
    requested_mbps: 3,
    lost_packets: 0,
    total_packets: 100,
    bandwidth_mbps: 10,
    utilization_percent: 29,
    active_flows: 1,
    provenance: "completed_experiment_person1_jsonl",
    ...overrides,
  };
}

function history(records: ApiTelemetryHistoryRecord[] = [historyRecord()]): ApiTelemetryHistory {
  return {
    status: records.length ? "available" : "unavailable",
    message: records.length ? "Validated recorded history." : "No history.",
    source_kinds: records.length ? ["completed_experiment_person1_jsonl"] : [],
    host_filter: "h1",
    scenario_filter: null,
    limit: 200,
    total_records: records.length,
    truncated: false,
    rejected_records: 0,
    records,
  };
}

async function renderDashboard() {
  await act(async () => {
    root.render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  unmounted = false;
  healthMock.mockReset().mockResolvedValue({ status: "ok", service: "dashboard", api_version: "0.1.0", live_network_operations: true });
  activeExecutionMock.mockReset().mockResolvedValue(null);
  reportsMock.mockReset().mockResolvedValue({
    status: "unavailable", report_id: null, campaign_completeness: "unavailable", complete_cycle_count: null,
    minimum_complete_cycles: null, boundary_metadata_status: "unavailable", boundary_evidence_integrity_status: "unavailable",
    producer_provenance_status: "not_independently_validated", clock_mapping_status: "not_independently_validated",
    measurement_alignment_eligibility: "ineligible", comparison_ready: false, message: "No report.",
  });
  topologyMock.mockReset().mockResolvedValue({
    status: "configured", source: "repository topology", live_connectivity_verified: false, bottleneck_interface: "s1-eth5",
    hosts: [
      { id: "h1", role: "client", service: "Gaming", udp_destination_port: 5201, ipv4_address: "10.0.0.1" },
      { id: "h2", role: "client", service: "Video", udp_destination_port: 5202, ipv4_address: "10.0.0.2" },
      { id: "server", role: "server", service: null, udp_destination_port: null, ipv4_address: "10.0.0.5" },
    ],
    switches: ["s1", "s2"],
    links: [{ source: "s1", target: "s2", bandwidth_mbps: 10, delay: "10ms" }],
  });
  telemetryMock.mockReset().mockResolvedValue(telemetry("available"));
  historyMock.mockReset().mockResolvedValue(history());
});

afterEach(async () => {
  if (!unmounted) await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("Dashboard recorded telemetry", () => {
  it("uses actual API measurements and identifies their source, scenario, and timestamp", async () => {
    await renderDashboard();

    expect(container.textContent).toContain("2.997 Mbps");
    expect(container.textContent).toContain("20.6309 ms");
    expect(container.textContent).toContain("0.0334 ms");
    expect(container.textContent).toContain("0 %");
    expect(container.textContent).toContain("4 packets");
    expect(container.textContent).toContain("Completed experiment recording");
    expect(container.textContent).toContain("normal");
    expect(container.textContent).toContain("2026-10-20T10:11:12.123+00:00");
    expect(container.textContent).toContain("insufficient history for a trend");
    expect(container.textContent).toContain("Recorded telemetry history");
    expect(container.textContent).toContain("completed experiment JSONL · recorded data, not live");
    expect(container.textContent).toContain("live connectivity is not verified");
    expect(container.textContent).toContain("No active traffic execution is reported.");
    expect(container.textContent).toContain("Historical completed execution status is not available");
    expect(container.querySelector('a[href="/traffic-lab"]')?.textContent).toContain("Traffic Lab");
    expect(container.querySelector('a[href="/ai-qos"]')?.textContent).toContain("AI & QoS");
    expect(container.querySelector('a[href="/experiments"]')?.textContent).toContain("Experiments");
    expect(container.querySelector('[aria-label="Latency / RTT · h1, validated recorded telemetry at 2026-10-20T10:11:12.123+00:00"]')).not.toBeNull();
    expect(container.textContent).not.toContain("Synthetic sample");
    expect(telemetryMock).toHaveBeenCalledWith({ host: "h1", scenario: undefined });
  });

  it("shows only the active execution returned by the backend, including a failure", async () => {
    activeExecutionMock.mockResolvedValue({
      experiment_id: "0123456789abcdef0123456789abcdef",
      scenario: "burst",
      source_host: "h2",
      status: "failed",
      flows: ["h2"],
      target_rate_mbps: 8,
      duration_seconds: 23,
      created_at: "2026-10-20T10:00:00Z",
      started_at: "2026-10-20T10:00:00Z",
      finished_at: "2026-10-20T10:00:04Z",
      elapsed_seconds: 4,
      process_alive: false,
      can_stop: false,
      process_exit_code: 2,
      error: "Producer exited with code 2.",
    });
    await renderDashboard();

    expect(container.textContent).toContain("burst · failed");
    expect(container.textContent).toContain("Process exit code 2");
    expect(container.textContent).toContain("Producer exited with code 2.");
    expect(activeExecutionMock).toHaveBeenCalledTimes(1);
  });

  it("keeps selected-client values tied to the actual record source", async () => {
    await renderDashboard();
    const h2Button = Array.from(container.querySelectorAll("button")).find((button) => button.textContent?.includes("h2 · Video Streaming"));
    expect(h2Button).toBeDefined();
    await act(async () => h2Button?.click());

    expect(container.textContent).toContain("Latest row is for h1; no API measurement is available for selected h2.");
    expect(container.textContent).toContain("No fresh measurement is available for h2");
    const selectedMetrics = container.querySelector(".flow-detail__metrics")?.textContent ?? "";
    expect(selectedMetrics).toContain("Unavailable");
    expect(selectedMetrics).not.toContain("2.997");
    expect(telemetryMock).toHaveBeenLastCalledWith({ host: "h2", scenario: undefined });
    expect(historyMock).toHaveBeenLastCalledWith({ host: "h2", limit: 200 });
  });

  it("plots only actual selected-host records and requests the selected scenario", async () => {
    const historyRecords = [
      historyRecord({ timestamp: "2026-01-01T10:00:00Z", source: "h1", scenario: "normal" }),
      historyRecord({ timestamp: "2026-01-01T10:01:00Z", source: "h1", scenario: "normal", throughput_mbps: 3.1 }),
      historyRecord({ timestamp: "2026-01-01T10:02:00Z", source: "h2", scenario: "normal", throughput_mbps: 8 }),
    ];
    historyMock.mockResolvedValue(history(historyRecords));
    await renderDashboard();

    expect(container.textContent).toContain("2026-01-01T10:00:00Z to 2026-01-01T10:01:00Z");
    expect(container.textContent).not.toContain("2026-01-01T10:02:00Z");
    expect(container.querySelector('[aria-label="Throughput · h1, validated recorded telemetry at 2026-01-01T10:00:00Z, 2026-01-01T10:01:00Z"]')).not.toBeNull();

    const scenarioSelect = container.querySelector<HTMLSelectElement>("#dashboard-history-scenario");
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(scenarioSelect, "congestion");
      scenarioSelect?.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(historyMock).toHaveBeenLastCalledWith({ host: "h1", limit: 200, scenario: "congestion" });
  });

  it("withholds stale values from cards, selected-client metrics, and charts", async () => {
    telemetryMock.mockResolvedValue(telemetry("stale"));
    await renderDashboard();

    expect(container.textContent).toContain("Telemetry is stale · values withheld");
    expect(container.textContent).toContain("420.0 seconds");
    expect(container.textContent).toContain("Measurement statusstale");
    expect(container.textContent).toContain("Recorded telemetry history");
    expect(container.textContent).not.toContain("2.997 Mbps");
    expect(container.textContent).not.toContain("20.6309 ms");
    expect(container.textContent).toContain("completed experiment JSONL · recorded data, not live");
  });

  it("renders missing metric fields as unavailable without substituting values", async () => {
    historyMock.mockResolvedValue(history([historyRecord({
      latency_ms: null,
      jitter_ms: null,
      packet_loss_percent: null,
      queue_size: null,
      queue_drops: null,
    })]));
    telemetryMock.mockResolvedValue(telemetry("available", record({
      latency_ms: null,
      jitter_ms: null,
      packet_loss_percent: null,
      queue_size: null,
      queue_drops: null,
    })));
    await renderDashboard();

    expect(container.textContent).toContain("2.997 Mbps");
    expect(container.textContent).toContain("Latency Unavailable");
    expect(container.textContent).toContain("Jitter Unavailable");
    expect(container.textContent).toContain("Packet loss Unavailable");
    expect(container.textContent).toContain("missing: queue size, queue drops");
    expect(container.textContent).toContain("Queue size and queue drops are missing from these recorded rows.");
  });

  it("shows explicit unavailable and API error states without retaining measurements", async () => {
    historyMock.mockResolvedValue(history([]));
    telemetryMock.mockResolvedValueOnce(telemetry("unavailable"));
    await renderDashboard();
    expect(container.textContent).toContain("No valid telemetry is available.");
    expect(container.textContent).toContain("No history.");
    expect(container.textContent).not.toContain("2.997 Mbps");

    await act(async () => root.unmount());
    unmounted = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    unmounted = false;
    telemetryMock.mockRejectedValueOnce(new Error("connection refused"));
    await renderDashboard();
    expect(container.textContent).toContain("Telemetry request error: connection refused");
    expect(container.textContent).not.toContain("2.997 Mbps");
  });

  it("reports history API failures separately from the latest measurement", async () => {
    historyMock.mockRejectedValue(new Error("history endpoint unavailable"));
    await renderDashboard();

    expect(container.textContent).toContain("History API error: history endpoint unavailable");
    expect(container.textContent).toContain("2.997 Mbps");
    expect(container.textContent).not.toContain("2026-10-20T10:11:12.123+00:00 to");
  });
});
