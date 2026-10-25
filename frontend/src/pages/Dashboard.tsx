import {
  Activity,
  Clock3,
  Gauge,
  Network,
  Radio,
  ShieldCheck,
  Signal,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { backendConnection, getActiveTrafficExecution, getBackendHealth, getExperimentsStatus, getLatestTelemetry, getTelemetryHistory, getTopology } from "../api/client";
import type { ApiExecution, ApiExperimentsStatus, ApiHealth, ApiTelemetry, ApiTelemetryHistory, ApiTopology, TelemetryHistoryFilters } from "../api/client";
import { MetricCard } from "../components/MetricCard";
import { Topology } from "../components/Topology";
import { TrendChart } from "../components/TrendChart";
import { CLIENT_FLOWS } from "../data/demo";
import type { FlowId } from "../types";

const TELEMETRY_REFRESH_MS = 5_000;

function formatMetric(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "Unavailable"
    : value.toFixed(digits);
}

function formatValue(value: number | null | undefined, unit: string): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "Unavailable"
    : `${value} ${unit}`;
}

function telemetrySourceLabel(source: ApiTelemetry["source"] | undefined): string {
  if (source === "configured_person1_jsonl") return "Configured recorded Person-1 JSONL";
  if (source === "completed_experiment_person1_jsonl") return "Completed experiment recording";
  return "No telemetry source";
}

export function Dashboard() {
  const [selected, setSelected] = useState<FlowId>("h1");
  const [topology, setTopology] = useState<ApiTopology | null>(null);
  const [topologyError, setTopologyError] = useState("");
  const [telemetry, setTelemetry] = useState<ApiTelemetry | null>(null);
  const [telemetryLoading, setTelemetryLoading] = useState(true);
  const [telemetryError, setTelemetryError] = useState("");
  const [history, setHistory] = useState<ApiTelemetryHistory | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState("");
  const [historyScenario, setHistoryScenario] = useState<TelemetryHistoryFilters["scenario"] | "all">("all");
  const [health, setHealth] = useState<ApiHealth | null>(null);
  const [healthError, setHealthError] = useState("");
  const [experiments, setExperiments] = useState<ApiExperimentsStatus | null>(null);
  const [experimentsError, setExperimentsError] = useState("");
  const [activeExecution, setActiveExecution] = useState<ApiExecution | null>(null);
  const [executionLoading, setExecutionLoading] = useState(true);
  const [executionError, setExecutionError] = useState("");
  const flow = CLIENT_FLOWS.find((item) => item.id === selected) ?? CLIENT_FLOWS[0];

  useEffect(() => {
    if (!backendConnection.baseUrl) {
      setHealthError("Backend API is not configured.");
      setTopologyError("Backend API is not configured.");
      setTelemetryError("Backend API is not configured; measurements are unavailable.");
      setExperimentsError("Backend API is not configured.");
      setExecutionError("Backend API is not configured.");
      setTelemetryLoading(false);
      setExecutionLoading(false);
      return;
    }
    let active = true;
    let telemetryTimer: number | undefined;
    let telemetryRequestInFlight = false;

    getBackendHealth().then(
      (value) => active && setHealth(value),
      (reason: unknown) => active && setHealthError(reason instanceof Error ? reason.message : "Health API request failed."),
    );
    getTopology().then(
      (value) => active && setTopology(value),
      (reason: unknown) => active && setTopologyError(reason instanceof Error ? reason.message : "Topology API request failed."),
    );
    getExperimentsStatus().then(
      (value) => active && setExperiments(value),
      (reason: unknown) => active && setExperimentsError(reason instanceof Error ? reason.message : "Report status request failed."),
    );
    getActiveTrafficExecution().then(
      (value) => active && setActiveExecution(value),
      (reason: unknown) => active && setExecutionError(reason instanceof Error ? reason.message : "Execution status request failed."),
    ).finally(() => active && setExecutionLoading(false));

    const refreshTelemetry = async () => {
      if (!active || telemetryRequestInFlight) return;
      telemetryRequestInFlight = true;
      setTelemetry(null);
      setTelemetryError("");
      setTelemetryLoading(true);
      try {
        const value = await getLatestTelemetry({
          host: selected,
          scenario: historyScenario === "all" ? undefined : historyScenario,
        });
        if (active) setTelemetry(value);
      } catch (reason: unknown) {
        if (active) setTelemetryError(reason instanceof Error ? reason.message : "Telemetry API request failed.");
      } finally {
        telemetryRequestInFlight = false;
        if (active) {
          setTelemetryLoading(false);
          telemetryTimer = window.setTimeout(refreshTelemetry, TELEMETRY_REFRESH_MS);
        }
      }
    };
    void refreshTelemetry();

    return () => {
      active = false;
      if (telemetryTimer !== undefined) window.clearTimeout(telemetryTimer);
    };
  }, [selected, historyScenario]);

  useEffect(() => {
    if (!backendConnection.baseUrl) {
      setHistoryError("Backend API is not configured; recorded history is unavailable.");
      setHistoryLoading(false);
      return;
    }
    let active = true;
    let timer: number | undefined;
    let inFlight = false;

    const refreshHistory = async () => {
      if (!active || inFlight) return;
      inFlight = true;
      setHistory(null);
      setHistoryError("");
      setHistoryLoading(true);
      try {
        const filters: TelemetryHistoryFilters = { host: selected, limit: 200 };
        if (historyScenario !== "all") filters.scenario = historyScenario;
        const response = await getTelemetryHistory(filters);
        if (active) setHistory(response);
      } catch (reason: unknown) {
        if (active) setHistoryError(reason instanceof Error ? reason.message : "Telemetry history request failed.");
      } finally {
        inFlight = false;
        if (active) {
          setHistoryLoading(false);
          timer = window.setTimeout(refreshHistory, TELEMETRY_REFRESH_MS);
        }
      }
    };

    void refreshHistory();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [selected, historyScenario]);

  const freshRecord = telemetry?.status === "available" ? telemetry.record : null;
  const matchingRecord = freshRecord?.source === selected ? freshRecord : null;
  const capacity = topology?.links.find((link) => link.source === "s1" && link.target === "s2")?.bandwidth_mbps;
  const configuredClients = topology?.hosts.filter((host) => host.role === "client") ?? [];
  const selectedHistory = (history?.records ?? []).filter((record) =>
    record.source === selected && (historyScenario === "all" || record.scenario === historyScenario),
  );
  const historyChartData = selectedHistory.map((record) => ({
    time: record.timestamp,
    throughput: record.throughput_mbps,
    latency: record.latency_ms ?? undefined,
    jitter: record.jitter_ms ?? undefined,
    packetLoss: record.packet_loss_percent ?? undefined,
    queueSize: record.queue_size ?? undefined,
    queueDrops: record.queue_drops ?? undefined,
  }));
  const historyQueueSeries = [
    ...(selectedHistory.some((record) => record.queue_size !== null) ? [{ key: "queueSize", label: "Queue size" }] : []),
    ...(selectedHistory.some((record) => record.queue_drops !== null) ? [{ key: "queueDrops", label: "Queue drops" }] : []),
  ];
  const missingHistoryQueueFields = [
    ...(selectedHistory.some((record) => record.queue_size === null) ? ["queue size"] : []),
    ...(selectedHistory.some((record) => record.queue_drops === null) ? ["queue drops"] : []),
  ];
  const allHistoryValuesMissing = (field: "latency_ms" | "jitter_ms" | "packet_loss_percent") =>
    selectedHistory.length > 0 && selectedHistory.every((record) => record[field] === null);
  const historySourceLabel = history?.source_kinds.map((source) => source === "configured_person1_jsonl"
    ? "configured Person-1 JSONL"
    : "completed experiment JSONL").join(" + ") || "no source";
  const historyScenarios = [...new Set(selectedHistory.map((record) => record.scenario))].join(", ");
  const historyInterfaces = [...new Set(selectedHistory.map((record) => record.interface))].join(", ");
  const historyDescription = historyLoading
    ? "Loading recorded history; previous chart values are withheld."
    : historyError
      ? `History API error: ${historyError}`
      : selectedHistory.length === 0
        ? history?.message ?? "No recorded history is available."
        : selectedHistory.length === 1
          ? `1 recorded measurement for ${selected} · scenario ${historyScenarios} · interface ${historyInterfaces} · insufficient history for a trend · ${selectedHistory[0].timestamp}`
          : `${selectedHistory.length} recorded measurements for ${selected} · scenarios ${historyScenarios} · interfaces ${historyInterfaces} · ${selectedHistory[0].timestamp} to ${selectedHistory[selectedHistory.length - 1].timestamp}${history?.truncated ? ` · displaying latest ${history.limit} of ${history.total_records}` : ""}`;
  const historyCaption = historyLoading
    ? "Loading recorded history"
    : historyError
      ? "History request failed; no data plotted"
      : selectedHistory.length
        ? `${historySourceLabel} · recorded data, not live`
        : "No recorded data plotted";
  const historyEmptyMessage = historyLoading
    ? "Loading recorded history."
    : historyError
      ? `History request failed: ${historyError}`
      : history?.message ?? "No recorded history is available.";
  const effectiveTelemetryStatus = telemetry?.status === "available" && !telemetry.record
    ? "unavailable"
    : telemetry?.status;

  return (
    <div className="page-stack">
      <section className="page-intro">
        <div>
          <div className="eyebrow"><span className="eyebrow-mark" /> ADAPTIVE NETWORK CONTROL</div>
          <h1>A clearer view of your network.</h1>
          <p>Explore the topology, traffic profiles, and offline QoS planning in one place.</p>
        </div>
        <div className="demo-banner">
          <span className="demo-banner__icon"><Radio size={16} /></span>
          <div><strong>READ-ONLY DATA</strong><span>{telemetryLoading ? "Checking backend measurements…" : telemetrySourceLabel(telemetry?.source)}</span></div>
        </div>
      </section>

      <div className="summary-grid">
        <MetricCard
          title="Bottleneck capacity"
          value={capacity === undefined || capacity === null ? "Unavailable" : `${capacity}`}
          unit={capacity === undefined || capacity === null ? undefined : "Mbps"}
          note="Configured topology · not live verification"
          icon={Gauge}
        />
        <MetricCard
          title="Client flows"
          value={topology ? `${configuredClients.length}` : "Unavailable"}
          note={topology ? "Configured topology hosts" : "Configured topology unavailable"}
          icon={Network}
        />
        <MetricCard
          title="Latest throughput"
          value={freshRecord ? formatMetric(freshRecord.throughput_mbps) : "Unavailable"}
          unit={freshRecord ? "Mbps" : undefined}
          note={freshRecord ? `${freshRecord.source} · ${freshRecord.timestamp}` : telemetry?.status === "stale" ? "Latest measurement is stale; value withheld" : "No fresh validated measurement"}
          icon={Activity}
        />
        <MetricCard
          title="Measurement status"
          value={telemetryLoading ? "Loading" : telemetryError ? "API error" : effectiveTelemetryStatus ?? "Unavailable"}
          note={telemetryError || (telemetry ? telemetry.status === "available" && !telemetry.record
            ? "API returned no measurement record."
            : `${telemetrySourceLabel(telemetry.source)} · ${telemetry.measurement_age_seconds === null ? "age unavailable" : `${telemetry.measurement_age_seconds.toFixed(1)} seconds old`}` : "No telemetry measurement found")}
          icon={Clock3}
        />
      </div>

      <section className="card api-data-card" aria-label="Read-only API data">
        <div className="section-heading section-heading--compact">
          <div>
            <h3>Configured backend data</h3>
            <p>
              {topology
                ? `Topology from ${topology.source}; live connectivity is ${topology.live_connectivity_verified ? "verified" : "not verified"}.`
                : backendConnection.baseUrl
                  ? topologyError || "Loading configured topology…"
                  : "API is not configured; topology and measurements are unavailable."}
            </p>
          </div>
          <span className="status-pill status-pill--muted">
            {topology ? "Configured topology" : backendConnection.baseUrl ? "API state" : "Unavailable"}
          </span>
        </div>
        <div className="api-data-card__message" role="status">
          <strong>API health:</strong> {health?.status === "ok"
            ? `API reachable · ${health.service} ${health.api_version} · Mininet connectivity is reported separately`
            : healthError || "Checking local API…"}
        </div>
        <div className="api-data-card__message" role="status">
          <strong>Report status:</strong> {experiments
            ? `${experiments.status} · campaign ${experiments.campaign_completeness} · alignment ${experiments.measurement_alignment_eligibility}${experiments.report_id ? ` · ${experiments.report_id}` : ""}. ${experiments.message}`
            : experimentsError || "Checking configured reports…"}
        </div>
        {telemetryLoading ? (
          <p className="api-data-card__message" role="status">Loading validated telemetry. Previous values are withheld while refreshing.</p>
        ) : telemetryError ? (
          <p className="api-data-card__message" role="status">Telemetry request error: {telemetryError}</p>
        ) : !telemetry || telemetry.status === "unavailable" || !telemetry.record ? (
          <p className="api-data-card__message" role="status">
            {telemetry?.message ?? "No backend telemetry source is connected; no measurement is substituted."}
          </p>
        ) : telemetry.status === "stale" ? (
          <div className="api-telemetry__intro" role="status">
            <strong>Telemetry is stale · values withheld</strong>
            <span>{telemetrySourceLabel(telemetry.source)} · measurement age {telemetry.measurement_age_seconds?.toFixed(1) ?? "unknown"} seconds</span>
            <span>{telemetry.message}</span>
          </div>
        ) : (
          <div className="api-telemetry">
            <div className="api-telemetry__intro">
              <strong>Read-only recorded measurement · fresh within API window</strong>
              <span>{telemetrySourceLabel(telemetry.source)} · {telemetry.record.source} · {telemetry.record.scenario} · {telemetry.record.timestamp} · {telemetry.record.interface}</span>
              <span>Measurement age {telemetry.measurement_age_seconds?.toFixed(1) ?? "unknown"} seconds · live connectivity is {topology?.live_connectivity_verified ? "verified" : "not established"}</span>
              <span>{telemetry.message}</span>
            </div>
            {matchingRecord ? (
              <div className="api-telemetry__metrics">
                <span>Throughput <strong>{formatValue(matchingRecord.throughput_mbps, "Mbps")}</strong></span>
                <span>Latency <strong>{formatValue(matchingRecord.latency_ms, "ms")}</strong></span>
                <span>Jitter <strong>{formatValue(matchingRecord.jitter_ms, "ms")}</strong></span>
                <span>Packet loss <strong>{formatValue(matchingRecord.packet_loss_percent, "%")}</strong></span>
                <span>Queue size <strong>{formatValue(matchingRecord.queue_size, "packets")}</strong></span>
                <span>Queue drops <strong>{formatValue(matchingRecord.queue_drops, "packets")}</strong></span>
              </div>
            ) : (
              <p className="api-data-card__message">
                Latest row is for {telemetry.record.source}; no API measurement is available for selected {selected}.
              </p>
            )}
          </div>
        )}
      </section>

      <section className="dashboard-workflow card" aria-label="Current execution and workflow navigation">
        <div className="dashboard-workflow__execution">
          <div className="section-heading section-heading--compact">
            <div>
              <h3>Active traffic execution</h3>
              <p>Current process status only; the API does not expose the latest completed execution here.</p>
            </div>
            <span className={`status-pill ${activeExecution ? "status-pill--surface" : "status-pill--muted"}`}>
              {executionLoading ? "Checking" : executionError ? "Unavailable" : activeExecution?.status ?? "No active run"}
            </span>
          </div>
          {executionError ? (
            <p className="api-data-card__message" role="alert">{executionError}</p>
          ) : activeExecution ? (
            <div className="dashboard-workflow__details" role="status">
              <strong>{activeExecution.scenario} · {activeExecution.status}</strong>
              <span>Source {activeExecution.source_host ?? "unavailable"} · {activeExecution.flows.join(", ") || "flows unavailable"}</span>
              <span>{activeExecution.target_rate_mbps} Mbps per flow · {activeExecution.duration_seconds}s · elapsed {activeExecution.elapsed_seconds.toFixed(1)}s</span>
              {activeExecution.process_exit_code !== null && <span>Process exit code {activeExecution.process_exit_code}</span>}
              {activeExecution.error && <span className="execution-state__error">{activeExecution.error}</span>}
            </div>
          ) : !executionLoading ? (
            <p className="api-data-card__message" role="status">No active traffic execution is reported. Historical completed execution status is not available from the active-execution endpoint.</p>
          ) : (
            <p className="api-data-card__message" role="status">Loading current execution status…</p>
          )}
        </div>
        <nav className="dashboard-workflow__links" aria-label="Adaptive QoS workflow">
          <Link className="button button--secondary" to="/traffic-lab">Traffic Lab</Link>
          <Link className="button button--secondary" to="/ai-qos">AI &amp; QoS</Link>
          <Link className="button button--secondary" to="/experiments">Experiments</Link>
        </nav>
      </section>

      <Topology selected={selected} onSelect={setSelected} />

      <section className="flow-detail card">
        <div className="flow-detail__identity">
          <span className="flow-avatar"><Signal size={18} /></span>
          <div>
            <span className="eyebrow">SELECTED CLIENT · RECORDED TELEMETRY</span>
            <h2>{flow.name} <span className="muted-inline">({flow.id})</span></h2>
            <p>{flow.service} · UDP destination port {flow.port}</p>
          </div>
        </div>
        <div className="flow-detail__metrics">
          <div><span>Throughput</span><strong>{formatValue(matchingRecord?.throughput_mbps, "Mbps")}</strong></div>
          <div><span>Latency / RTT</span><strong>{formatValue(matchingRecord?.latency_ms, "ms")}</strong></div>
          <div><span>Jitter</span><strong>{formatValue(matchingRecord?.jitter_ms, "ms")}</strong></div>
          <div><span>Packet loss</span><strong>{formatValue(matchingRecord?.packet_loss_percent, "%")}</strong></div>
        </div>
        <div className="flow-detail__status" role="status">
          <ShieldCheck size={16} />
          <span>{matchingRecord
            ? `Recorded measurement for ${matchingRecord.source} · ${matchingRecord.timestamp} · not evidence that traffic is currently running.`
            : telemetry?.status === "stale"
              ? "The latest record is stale; selected-client values are withheld."
              : telemetryError
                ? "Telemetry API failed; selected-client values are unavailable."
                : `No fresh measurement is available for ${selected}; no other client's values are substituted.`}</span>
        </div>
      </section>

      <section className="section-heading dashboard-section-heading">
        <div>
          <h2>Recorded telemetry history</h2>
          <p>{historyDescription} Missing fields remain gaps; no points are interpolated. This history is separate from the latest measurement freshness status above.</p>
        </div>
        <label className="record-select" htmlFor="dashboard-history-scenario">
          Scenario
          <select
            id="dashboard-history-scenario"
            value={historyScenario}
            onChange={(event) => setHistoryScenario(event.target.value as TelemetryHistoryFilters["scenario"] | "all")}
          >
            <option value="all">All scenarios</option>
            <option value="normal">Normal</option>
            <option value="moderate">Moderate</option>
            <option value="congestion">Congestion</option>
            <option value="burst">Burst</option>
          </select>
        </label>
      </section>

      <div className="chart-grid">
        <TrendChart
          title={`Throughput · ${selected}`}
          subtitle={`Recorded measurements for selected client${historyScenario === "all" ? " · all scenarios" : ` · ${historyScenario}`}`}
          data={historyChartData}
          series={[{ key: "throughput", label: `${flow.service} (${selected})` }]}
          unit="Mbps"
          caption={historyCaption}
          emptyMessage={historyEmptyMessage}
        />
        <TrendChart title={`Latency / RTT · ${selected}`} subtitle="Recorded history · missing values are gaps" data={historyChartData} series={[{ key: "latency", label: "Latency" }]} unit="ms" area caption={historyCaption} emptyMessage={allHistoryValuesMissing("latency_ms") ? "Latency is missing from these recorded rows." : historyEmptyMessage} />
        <TrendChart title={`Jitter · ${selected}`} subtitle="Recorded history · missing values are gaps" data={historyChartData} series={[{ key: "jitter", label: "Jitter" }]} unit="ms" caption={historyCaption} emptyMessage={allHistoryValuesMissing("jitter_ms") ? "Jitter is missing from these recorded rows." : historyEmptyMessage} />
        <TrendChart title={`Packet loss · ${selected}`} subtitle="Recorded history · missing values are gaps" data={historyChartData} series={[{ key: "packetLoss", label: "Packet loss" }]} unit="%" area caption={historyCaption} emptyMessage={allHistoryValuesMissing("packet_loss_percent") ? "Packet loss is missing from these recorded rows." : historyEmptyMessage} />
        <TrendChart
          title={`Queue occupancy & drops · ${selected}`}
          subtitle={`Recorded history · missing values are gaps${missingHistoryQueueFields.length ? ` · missing: ${missingHistoryQueueFields.join(", ")}` : ""}`}
          data={historyChartData}
          series={historyQueueSeries}
          unit="packets"
          caption={historyCaption}
          emptyMessage={selectedHistory.length > 0 && historyQueueSeries.length === 0
            ? "Queue size and queue drops are missing from these recorded rows."
            : historyEmptyMessage}
        />
      </div>
    </div>
  );
}
