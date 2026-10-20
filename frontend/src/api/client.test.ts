import { afterEach, describe, expect, it, vi } from "vitest";
import {
  apiReportToEvidenceReport,
  apiReportToRunnerReport,
  backendConnection,
  getBackendHealth,
  getReport,
  getReports,
  parseEvidenceReport,
  parseRunnerReport,
} from "./client";

const configuredBaseUrl = backendConnection.baseUrl;

afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(backendConnection, "baseUrl", {
    configurable: true,
    value: configuredBaseUrl,
  });
});

describe("read-only API client", () => {
  it("requests the health endpoint from the configured API base URL", async () => {
    Object.defineProperty(backendConnection, "baseUrl", {
      configurable: true,
      value: "http://localhost:8000/",
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "ok",
      service: "adaptive-qos-readonly-api",
      api_version: "0.1.0",
      live_network_operations: false,
    })));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getBackendHealth()).resolves.toMatchObject({
      status: "ok",
      live_network_operations: false,
    });
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/health", {
      headers: { Accept: "application/json" },
    });
  });

  it("reports unconfigured API access instead of fabricating fallback data", async () => {
    Object.defineProperty(backendConnection, "baseUrl", {
      configurable: true,
      value: null,
    });
    await expect(getReports()).rejects.toThrow("VITE_BACKEND_URL");
  });

  it("surfaces HTTP errors and rejects unsafe report identifiers", async () => {
    Object.defineProperty(backendConnection, "baseUrl", {
      configurable: true,
      value: "http://127.0.0.1:8000",
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "Configured report was not found." }),
      { status: 404 },
    ));
    vi.stubGlobal("fetch", fetchMock);

    expect(() => getReport("../private")).toThrow("Invalid report identifier");
    expect(fetchMock).not.toHaveBeenCalled();
    await expect(getReport("missing")).rejects.toThrow("Configured report was not found.");
  });

  it("keeps API evidence ineligible even when a returned report claims readiness", () => {
    const normalized = apiReportToEvidenceReport({
      report_id: "campaign-1",
      report_type: "campaign-validation",
      created_at: null,
      status: "complete",
      planning_only: true,
      campaign_status: "complete",
      complete_cycle_count: 3,
      minimum_complete_cycles: 3,
      boundary_metadata_status: "schema_valid",
      boundary_evidence_integrity_status: "verified",
      producer_provenance_status: "not_independently_validated",
      clock_mapping_status: "not_independently_validated",
      alignment_eligibility: "ineligible",
      comparison_ready: true,
      runner_records: [],
      trials: [{
        trial_id: "trial-1",
        condition: "Baseline",
        profile: "baseline",
        repetition_id: "rep-1",
        status: "complete",
        alignment_status: "ineligible",
        boundary_metadata_status: "schema_valid",
        boundary_evidence_integrity_status: "verified",
        producer_provenance_status: "not_independently_validated",
        clock_mapping_status: "not_independently_validated",
        flows: [{
          flow_id: "h1",
          status: "complete",
          metrics: { throughput_mbps: 3.1, latency_ms: null, jitter_ms: null, packet_loss_percent: null },
        }],
      }],
    });
    expect(normalized.comparisonReady).toBe(false);
    expect(normalized.alignmentStatus).toBe("ineligible");
    expect(normalized.trials[0].flows[0].throughputMbps).toBe(3.1);
    expect(normalized.trials[0].flows[0].latencyMs).toBeNull();
  });

  it("maps a sanitized API runner record to the existing planning-only report view", () => {
    const normalized = apiReportToRunnerReport({
      report_id: "runner-1",
      report_type: "experiment-runner",
      created_at: "2026-10-20T10:00:00Z",
      status: "planned",
      planning_only: true,
      campaign_status: null,
      complete_cycle_count: null,
      minimum_complete_cycles: null,
      boundary_metadata_status: "not_applicable",
      boundary_evidence_integrity_status: "not_applicable",
      producer_provenance_status: "not_applicable",
      clock_mapping_status: "not_applicable",
      alignment_eligibility: "not_applicable",
      comparison_ready: false,
      runner_records: [{
        line: 1,
        status: "planned",
        experiment_id: "exp-1",
        scenario: "normal",
        source: "h1",
        timestamp: "2026-10-20T10:00:00Z",
        freshness_status: "fresh",
        freshness_age_seconds: 5,
        prediction: "LOW",
        confidence: 0.7,
        input_features: { utilization: 25 },
        measured: { throughput_mbps: 2.9, latency_ms: null, jitter_ms: null, packet_loss_percent: null },
        service_allocations_mbps: { gaming: 2.7 },
        flow_allocations_mbps: { "5201": 2.7 },
        planning_only: true,
      }],
      trials: [],
    });
    expect(normalized.kind).toBe("experiment-runner");
    expect(normalized.records[0].prediction).toBe("LOW");
    expect(normalized.records[0].flowAllocationsMbps?.["5201"]).toBe(2.7);
    expect(normalized.records[0].planningOnly).toBe(true);
  });
});

describe("offline artifact client", () => {
  it("accepts a dry-run adaptive report and preserves its predicted plan", () => {
    const report = parseRunnerReport(JSON.stringify({
      schema_version: 1,
      run_id: "run-123",
      created_at: "2026-10-20T00:00:00Z",
      mode: "adaptive",
      status: "planned",
      planning_only: true,
      dry_run: true,
      experiment_executed: false,
      measurements_collected: false,
      live_qos_applied: false,
      tc_commands_executed: false,
      results: [{
        line: 1,
        status: "planned",
        experiment_id: "exp-1",
        scenario: "congestion",
        source: "h1",
        window: { timestamp: "2026-10-20T00:00:00Z" },
        freshness: { status: "fresh", age_seconds: 12 },
        measured: { timestamp: "2026-10-20T00:00:00Z", throughput_mbps: 3.2 },
        modes: {
          adaptive: {
            status: "planned",
            prediction: { congestion: "MEDIUM", confidence: 0.8 },
            input_features: { utilization: 32 },
            service_class_allocation_mbps: { gaming: 3.6, video: 2.7 },
            flow_allocations_mbps: { "5201": 3.6 },
          },
        },
      }],
    }));

    expect(report.records[0].prediction).toBe("MEDIUM");
    expect(report.records[0].confidence).toBe(0.8);
    expect(report.records[0].freshnessStatus).toBe("fresh");
    expect(report.records[0].freshnessAgeSeconds).toBe(12);
    expect(report.records[0].flowAllocationsMbps?.["5201"]).toBe(3.6);
  });

  it("rejects a report that claims live QoS application", () => {
    const report = {
      schema_version: 1,
      mode: "adaptive",
      planning_only: true,
      dry_run: true,
      experiment_executed: false,
      measurements_collected: false,
      live_qos_applied: true,
      tc_commands_executed: false,
      results: [],
    };
    expect(() => parseRunnerReport(JSON.stringify(report))).toThrow(
      "recognized dry-run experiment-runner report",
    );
  });

  it("keeps a structurally complete but unaligned campaign blocked", () => {
    const report = parseEvidenceReport(JSON.stringify({
      status: "complete",
      live_operations_supported: false,
      comparison_ready: false,
      alignment_status: "blocked",
      trials: [{
        trial_id: "t1",
        status: "complete",
        alignment_status: "ineligible",
        flows: {},
      }],
    }));
    expect(report.comparisonReady).toBe(false);
    expect(report.trials[0].alignment).toBe("ineligible");
  });

  it("does not treat a completion flag as enough without explicit eligible alignment", () => {
    const report = parseEvidenceReport(JSON.stringify({
      status: "complete",
      live_operations_supported: false,
      comparison_ready: true,
      trials: [{ trial_id: "t1", status: "complete", alignment_status: "eligible" }],
    }));
    expect(report.comparisonReady).toBe(false);
  });
});
