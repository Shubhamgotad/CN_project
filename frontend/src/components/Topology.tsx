import { Monitor, Router, Server, Waypoints } from "lucide-react";
import { CLIENT_FLOWS } from "../data/demo";
import type { FlowId } from "../types";

interface TopologyProps {
  selected: FlowId;
  onSelect: (flow: FlowId) => void;
}

export function Topology({ selected, onSelect }: TopologyProps) {
  return (
    <section className="topology-card card">
      <div className="section-heading">
        <div>
          <h2>Network topology</h2>
          <p>Repository topology view · traffic animation disabled</p>
        </div>
        <span className="status-pill status-pill--muted">Offline view</span>
      </div>

      <div className="topology-layout">
        <div className="client-stack" aria-label="Client PCs">
          {CLIENT_FLOWS.map((flow, index) => (
            <button
              className={`client-node ${selected === flow.id ? "is-selected" : ""}`}
              key={flow.id}
              onClick={() => onSelect(flow.id)}
              aria-pressed={selected === flow.id}
              type="button"
            >
              <span className="node-icon"><Monitor size={18} /></span>
              <span className="client-node__copy">
                <strong>{flow.name}</strong>
                <small>{flow.id} · {flow.service}</small>
              </span>
              <span className="client-node__port">UDP {flow.port}</span>
              <span className="client-node__num">0{index + 1}</span>
            </button>
          ))}
        </div>

        <div className="topology-wire topology-wire--clients" aria-hidden="true">
          <span />
        </div>

        <div className="switch-stack">
          <div className="switch-node">
            <span className="switch-node__icon"><Router size={20} /></span>
            <div><strong>s1</strong><small>Switch + QoS egress</small></div>
          </div>
          <div className="link-label">
            <Waypoints size={15} />
            <span>s1-eth5 · 10 Mbps bottleneck</span>
          </div>
          <div className="switch-node switch-node--secondary">
            <span className="switch-node__icon"><Router size={18} /></span>
            <div><strong>s2</strong><small>Distribution switch</small></div>
          </div>
        </div>

        <div className="topology-wire topology-wire--server" aria-hidden="true">
          <span />
        </div>

        <div className="server-node">
          <span className="server-node__icon"><Server size={23} /></span>
          <strong>Server</strong>
          <small>10.0.0.5</small>
          <span className="server-note">Receiver endpoint</span>
        </div>
      </div>

      <div className="topology-footnote">
        <span className="legend-dot" />
        <span>Lines show configured topology only, not packet activity.</span>
        <span className="topology-source">h1-h4 → s1 → s2 → server</span>
      </div>
    </section>
  );
}
