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
import { backendConnection, getLatestTelemetry, getTopology } from "../api/client";
import type { ApiTelemetry, ApiTopology } from "../api/client";
import { MetricCard } from "../components/MetricCard";
import { Topology } from "../components/Topology";
import { TrendChart } from "../components/TrendChart";
import { CLIENT_FLOWS, DEMO_METRICS_BY_FLOW, DEMO_SERIES } from "../data/demo";
import type { FlowId } from "../types";

export function Dashboard() {
  const [selected, setSelected] = useState<FlowId>("h1");
  const [topology, setTopology] = useState<ApiTopology | null>(null);
  const [topologyError, setTopologyError] = useState("");
  const [telemetry, setTelemetry] = useState<ApiTelemetry | null>(null);
  const [telemetryError, setTelemetryError] = useState("");
  const flow = CLIENT_FLOWS.find((item) => item.id === selected) ?? CLIENT_FLOWS[0];
  const metrics = DEMO_METRICS_BY_FLOW[selected];

  useEffect(() => {
    if (!backendConnection.baseUrl) return;
    let active = true;
    getTopology().then(
      (value) => active && setTopology(value),
      (reason: unknown) => active && setTopologyError(
        reason instanceof Error ? reason.message : "Topology API request failed.",
      ),
    );
    getLatestTelemetry().then(
      (value) => active && setTelemetry(value),
      (reason: unknown) => active && setTelemetryError(
        reason instanceof Error ? reason.message : "Telemetry API request failed.",
      ),
    );
    return () => {
      active = false;
    };
  }, []);

  const matchingRecord = telemetry?.record?.source === selected ? telemetry.record : null;
  const apiMetric = (value: number | null | undefined, unit: string) =>
    value === null || value === undefined ? "Unavailable" : `${value} ${unit}`;

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
          <div><strong>DEMO DATA</strong><span>Synthetic preview · no live measurements</span></div>
        </div>
      </section>

      <div className="summary-grid">
        <MetricCard title="Bottleneck capacity" value="10" unit="Mbps" note="Configured topology value" icon={Gauge} />
        <MetricCard title="Client flows" value="4" note="h1-h4 · UDP 5201-5204" icon={Network} />
        <MetricCard title="Latest throughput" value={`${metrics.throughput.toFixed(1)}`} unit="Mbps" note={`${flow.id} synthetic sample`} icon={Activity} demo />
        <MetricCard title="Measurement status" value="Unavailable" note="No live telemetry connection" icon={Clock3} />
      </div>

      <section className="card api-data-card" aria-label="Read-only API data">
        <div className="section-heading section-heading--compact">
          <div>
            <h3>Configured backend data</h3>
            <p>
              {topology
                ? `Topology from ${topology.source}; live connectivity is not verified.`
                : backendConnection.baseUrl
                  ? topologyError || "Loading configured topology…"
                  : "API is not configured. Dashboard charts below remain Demo Data."}
            </p>
          </div>
          <span className="status-pill status-pill--muted">
            {topology ? "Configured topology" : backendConnection.baseUrl ? "API state" : "Demo Data"}
          </span>
        </div>
        {telemetryError ? (
          <p className="api-data-card__message" role="status">Telemetry request error: {telemetryError}</p>
        ) : !telemetry || telemetry.status === "unavailable" ? (
          <p className="api-data-card__message">
            {telemetry?.message ?? (backendConnection.baseUrl
              ? "Loading latest telemetry…"
              : "No backend telemetry source is connected; no measurement is substituted.")}
          </p>
        ) : (
          <div className="api-telemetry">
            <div className="api-telemetry__intro">
              <strong>Read-only API measurement · {telemetry.status === "stale" ? "STALE" : "CURRENT"}</strong>
              <span>{telemetry.record?.source} · {telemetry.record?.timestamp} · {telemetry.record?.interface}</span>
              <span>{telemetry.message}</span>
            </div>
            {matchingRecord ? (
              <div className="api-telemetry__metrics">
                <span>Throughput <strong>{apiMetric(matchingRecord.throughput_mbps, "Mbps")}</strong></span>
                <span>Latency <strong>{apiMetric(matchingRecord.latency_ms, "ms")}</strong></span>
                <span>Jitter <strong>{apiMetric(matchingRecord.jitter_ms, "ms")}</strong></span>
                <span>Packet loss <strong>{apiMetric(matchingRecord.packet_loss_percent, "%")}</strong></span>
                <span>Queue size <strong>{apiMetric(matchingRecord.queue_size, "packets")}</strong></span>
                <span>Queue drops <strong>{apiMetric(matchingRecord.queue_drops, "packets")}</strong></span>
              </div>
            ) : (
              <p className="api-data-card__message">
                Latest row is for {telemetry.record?.source}; no API measurement is available for selected {selected}.
              </p>
            )}
          </div>
        )}
      </section>

      <Topology selected={selected} onSelect={setSelected} />

      <section className="flow-detail card">
        <div className="flow-detail__identity">
          <span className="flow-avatar"><Signal size={18} /></span>
          <div>
            <span className="eyebrow">SELECTED CLIENT · DEMO VIEW</span>
            <h2>{flow.name} <span className="muted-inline">({flow.id})</span></h2>
            <p>{flow.service} · UDP destination port {flow.port}</p>
          </div>
        </div>
        <div className="flow-detail__metrics">
          <div><span>Throughput</span><strong>{metrics.throughput.toFixed(1)} <small>Mbps</small></strong></div>
          <div><span>Latency / RTT</span><strong>{metrics.latency} <small>ms</small></strong></div>
          <div><span>Jitter</span><strong>{metrics.jitter.toFixed(1)} <small>ms</small></strong></div>
          <div><span>Packet loss</span><strong>{metrics.packetLoss.toFixed(1)} <small>%</small></strong></div>
        </div>
        <div className="flow-detail__status">
          <ShieldCheck size={16} />
          <span>Not connected to a live host. Values above are demo fixtures.</span>
        </div>
      </section>

      <section className="section-heading dashboard-section-heading">
        <div>
          <h2>Telemetry overview</h2>
          <p>Sample timestamps are relative fixture labels, not wall-clock measurement times.</p>
        </div>
        <span className="status-pill status-pill--surface">Demo Data</span>
      </section>

      <div className="chart-grid">
        <TrendChart
          title="Throughput per flow"
          subtitle="Synthetic receiver-side example"
          data={DEMO_SERIES}
          series={[
            { key: "h1", label: "Gaming" },
            { key: "h2", label: "Video" },
            { key: "h3", label: "Web" },
            { key: "h4", label: "Bulk" },
          ]}
          unit="Mbps"
        />
        <TrendChart
          title="Latency / RTT"
          subtitle="Synthetic sample trend"
          data={DEMO_SERIES}
          series={[{ key: "latency", label: "Latency" }]}
          unit="ms"
          area
        />
        <TrendChart
          title="Jitter"
          subtitle="Synthetic sample trend"
          data={DEMO_SERIES}
          series={[{ key: "jitter", label: "Jitter" }]}
          unit="ms"
        />
        <TrendChart
          title="Packet loss"
          subtitle="Synthetic sample trend"
          data={DEMO_SERIES}
          series={[{ key: "packetLoss", label: "Packet loss" }]}
          unit="%"
          area
        />
        <article className="chart-card chart-card--empty card">
          <div className="section-heading section-heading--compact">
            <div><h3>Queue occupancy & drops</h3><p>No queue fields in the demo fixture</p></div>
            <span className="unit-label">Unavailable</span>
          </div>
          <div className="empty-chart">
            <div className="empty-chart__icon"><Network size={19} /></div>
            <strong>Queue telemetry is not available</strong>
            <span>No value is substituted for missing queue measurements.</span>
          </div>
        </article>
      </div>
    </div>
  );
}
