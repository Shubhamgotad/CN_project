import { afterEach, describe, expect, it, vi } from "vitest";
import {
  apiReportToEvidenceReport,
  apiReportToRunnerReport,
  backendConnection,
  getBackendHealth,
  getTelemetryHistory,
  getActiveTrafficExecution,
  getTrafficExecution,
  startTrafficExecution,
  stopTrafficExecution,
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

describe("dashboard API client", () => {
  it("requests the health endpoint from the configured API base URL", async () => {
    Object.defineProperty(backendConnection, "baseUrl", {
      configurable: true,
      value: "http://localhost:8000/",
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "ok",
      service: "adaptive-qos-dashboard-api",
      api_version: "0.1.0",
      live_network_operations: true,
    })));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getBackendHealth()).resolves.toMatchObject({
      status: "ok",
      live_network_operations: true,
    });
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/health", {
      headers: { Accept: "application/json" },
    });
  });

  it("requests historical telemetry through bounded host and scenario filters", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "available",
      message: "Recorded history, not live telemetry.",
      source_kinds: ["completed_experiment_person1_jsonl"],
      host_filter: "h2",
      scenario_filter: "congestion",
      limit: 50,
      total_records: 1,
      truncated: false,
      rejected_records: 0,
      records: [],
    })));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getTelemetryHistory({ host: "h2", scenario: "congestion", limit: 50 })).resolves.toMatchObject({
      status: "available",
      source_kinds: ["completed_experiment_person1_jsonl"],
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/telemetry/history?host=h2&scenario=congestion&limit=50",
      { headers: { Accept: "application/json" } },
    );
  });

  it("uses only fixed execution endpoints and validates IDs", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "starting" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "running" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "stopped" })))
      .mockResolvedValueOnce(new Response("null"));
    vi.stubGlobal("fetch", fetchMock);
    const id = "0123456789abcdef0123456789abcdef";
    await startTrafficExecution("congestion", 5, 17, "all");
    await getTrafficExecution(id);
    await stopTrafficExecution(id);
    await expect(getActiveTrafficExecution()).resolves.toBeNull();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/experiments/start",
      `http://127.0.0.1:8000/api/execution/${id}`,
      `http://127.0.0.1:8000/api/experiments/${id}/stop`,
      "http://127.0.0.1:8000/api/execution/active",
    ]);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST", body: JSON.stringify({ scenario: "congestion", source_host: "all", rate_mbps: 5, duration_seconds: 17 }) });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: "POST", body: "{}" });
    expect(() => getTrafficExecution("../bad")).toThrow("Invalid experiment identifier");
    expect(() => stopTrafficExecution("../bad")).toThrow("Invalid experiment identifier");
  });

  it.each(["h1", "h2", "h3", "h4"] as const)(
    "serializes selected source host %s without changing the fixed endpoint",
    async (source_host) => {
      const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "starting" })));
      vi.stubGlobal("fetch", fetchMock);
      await startTrafficExecution("normal", 3, 20, source_host);
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8000/api/experiments/start",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ scenario: "normal", source_host, rate_mbps: 3, duration_seconds: 20 }),
        }),
      );
    },
  );

  it("uses the local development API by default instead of fabricating fallback data", async () => {
    Object.defineProperty(backendConnection, "baseUrl", {
      configurable: true,
      value: "http://127.0.0.1:8000",
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "unavailable", message: "Set ADAPTIVE_QOS_REPORT_DIR", reports: [],
    })));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getReports()).resolves.toMatchObject({ status: "unavailable", reports: [] });
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8000/api/reports", {
      headers: { Accept: "application/json" },
    });
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

  it("shows FastAPI validation details for the exact Start request instead of a generic 422", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: [{
        type: "value_error",
        loc: ["body", "rate_mbps"],
        msg: "Value error, Rate must use 0.1 Mbps increments.",
      }],
    }), { status: 422 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(startTrafficExecution("normal", 5.05, 20, "h1")).rejects.toThrow(
      "Backend rejected POST /api/experiments/start (422): body.rate_mbps: Value error, Rate must use 0.1 Mbps increments.",
    );
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8000/api/experiments/start", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ scenario: "normal", source_host: "h1", rate_mbps: 5.05, duration_seconds: 20 }),
    }));
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
    expect(normalized.campaignStatus).toBe("complete");
    expect(normalized.completeCycleCount).toBe(3);
    expect(normalized.minimumCompleteCycles).toBe(3);
    expect(normalized.boundaryEvidenceIntegrityStatus).toBe("verified");
    expect(normalized.producerProvenanceStatus).toBe("not_independently_validated");
    expect(normalized.clockMappingStatus).toBe("not_independently_validated");
    expect(normalized.trials[0].flows[0].throughputMbps).toBe(3.1);
    expect(normalized.trials[0].flows[0].latencyMs).toBeNull();
    expect(normalized.trials[0].flows[0].queueSize).toBeNull();
    expect(normalized.trials[0].flows[0].queueDrops).toBeNull();
  });

  it("requires report-level and every trial's explicit eligibility before enabling API comparisons", () => {
    const eligibleDetail = {
      report_id: "campaign-eligible",
      report_type: "campaign-validation" as const,
      created_at: null,
      status: "complete",
      planning_only: true,
      campaign_status: "complete",
      complete_cycle_count: 3,
      minimum_complete_cycles: 3,
      boundary_metadata_status: "schema_valid",
      boundary_evidence_integrity_status: "verified",
      producer_provenance_status: "validated",
      clock_mapping_status: "validated",
      alignment_eligibility: "eligible",
      comparison_ready: true,
      runner_records: [],
      trials: [{
        trial_id: "trial-1",
        condition: "Baseline",
        profile: "baseline",
        repetition_id: "rep-1",
        status: "complete",
        alignment_status: "eligible",
        boundary_metadata_status: "schema_valid",
        boundary_evidence_integrity_status: "verified",
        producer_provenance_status: "validated",
        clock_mapping_status: "validated",
        flows: [{
          flow_id: "h1",
          status: "complete",
          metrics: { throughput_mbps: 3, latency_ms: null, jitter_ms: null, packet_loss_percent: null },
        }],
      }],
    };

    expect(apiReportToEvidenceReport(eligibleDetail).comparisonReady).toBe(true);
    expect(apiReportToEvidenceReport({
      ...eligibleDetail,
      trials: eligibleDetail.trials.map((trial) => ({ ...trial, alignment_status: "ineligible" })),
    }).comparisonReady).toBe(false);
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

  it("preserves offline campaign metadata, reported issues and queue metrics", () => {
    const report = parseEvidenceReport(JSON.stringify({
      offline_only: true,
      status: "complete",
      campaign_status: "complete",
      complete_cycle_count: 2,
      minimum_complete_cycles: 3,
      boundary_metadata_status: "schema_valid",
      boundary_evidence_integrity_status: "verified",
      producer_provenance_status: "not_independently_validated",
      clock_mapping_status: "not_independently_validated",
      comparison_ready: false,
      alignment_status: "ineligible",
      manifest_errors: ["Manifest is missing an artifact."],
      trials: [{
        trial_id: "trial-1",
        status: "failed",
        alignment_status: "ineligible",
        errors: ["No matching receiver interval."],
        boundary_metadata_status: "schema_valid",
        boundary_evidence_integrity_status: "verified",
        producer_provenance_status: "not_independently_validated",
        clock_mapping_status: "not_independently_validated",
        flows: {
          h1: {
            status: "complete",
            receiver: { metrics: { throughput_mbps: 2.9, queue_size: 4, queue_drops: 1 } },
          },
        },
      }],
    }));

    expect(report.campaignStatus).toBe("complete");
    expect(report.completeCycleCount).toBe(2);
    expect(report.minimumCompleteCycles).toBe(3);
    expect(report.boundaryEvidenceIntegrityStatus).toBe("verified");
    expect(report.producerProvenanceStatus).toBe("not_independently_validated");
    expect(report.clockMappingStatus).toBe("not_independently_validated");
    expect(report.errors).toEqual(["Manifest is missing an artifact."]);
    expect(report.trials[0].errors).toEqual(["No matching receiver interval."]);
    expect(report.trials[0].boundaryMetadataStatus).toBe("schema_valid");
    expect(report.trials[0].flows[0].queueSize).toBe(4);
    expect(report.trials[0].flows[0].queueDrops).toBe(1);
    expect(report.comparisonReady).toBe(false);
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
