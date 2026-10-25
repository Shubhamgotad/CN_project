import { Monitor, Router, Server, Waypoints, X } from "lucide-react";
import { useState } from "react";
import type { ApiTopology } from "../api/client";
import { CLIENT_FLOWS } from "../data/demo";
import type { FlowId } from "../types";
import { formatMbps } from "../utils/format";

type TopologyNode = FlowId | "s1" | "s2" | "server";
type TopologyLink = ApiTopology["links"][number];

interface TopologyProps {
  topology: ApiTopology | null;
  onSelect: (flow: FlowId) => void;
}

function linkKey(link: TopologyLink): string {
  return [link.source, link.target].sort().join("|");
}

function findPath(start: string, target: string, links: TopologyLink[]): TopologyLink[] {
  const queue: Array<{ node: string; path: TopologyLink[] }> = [{ node: start, path: [] }];
  const visited = new Set([start]);
  while (queue.length) {
    const current = queue.shift()!;
    if (current.node === target) return current.path;
    for (const link of links) {
      const next = link.source === current.node
        ? link.target
        : link.target === current.node ? link.source : null;
      if (next && !visited.has(next)) {
        visited.add(next);
        queue.push({ node: next, path: [...current.path, link] });
      }
    }
  }
  return [];
}

function findPathNodes(start: string, target: string, links: TopologyLink[]): string[] {
  const path = findPath(start, target, links);
  if (!path.length) return [];
  const nodes = [start];
  for (const link of path) {
    const current = nodes[nodes.length - 1];
    const next = link.source === current ? link.target : link.target === current ? link.source : null;
    if (!next) return [];
    nodes.push(next);
  }
  return nodes[nodes.length - 1] === target ? nodes : [];
}

function valueOrUnavailable(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === "" ? "Not available" : String(value);
}

export function Topology({ topology, onSelect }: TopologyProps) {
  const [selectedNode, setSelectedNode] = useState<TopologyNode | null>(null);
  const selectedHost = CLIENT_FLOWS.find((flow) => flow.id === selectedNode);
  const selectedHostConfig = topology?.hosts.find((host) => host.id === selectedNode);
  const selectedNodeName = selectedHost?.name ?? selectedNode;
  const selectedNeighbors = selectedNode && topology
    ? topology.links
      .filter((link) => link.source === selectedNode || link.target === selectedNode)
      .map((link) => ({
        node: link.source === selectedNode ? link.target : link.source,
        link,
      }))
    : [];
  const highlightedKeys = new Set<string>();
  if (selectedNode && topology) {
    for (const { link } of selectedNeighbors) highlightedKeys.add(linkKey(link));
    if (selectedHost) {
      for (const link of findPath(selectedNode, "server", topology.links)) highlightedKeys.add(linkKey(link));
    }
  }
  const clientLinks = topology?.links.filter((link) => link.target === "s1" || link.source === "s1")
    .filter((link) => CLIENT_FLOWS.some((flow) => flow.id === (link.source === "s1" ? link.target : link.source))) ?? [];
  const interSwitchLink = topology?.links.find((link) =>
    (link.source === "s1" && link.target === "s2") || (link.source === "s2" && link.target === "s1"),
  );
  const serverLink = topology?.links.find((link) =>
    (link.source === "s2" && link.target === "server") || (link.source === "server" && link.target === "s2"),
  );
  const hasHighlightedLink = (links: TopologyLink[]) => links.some((link) => highlightedKeys.has(linkKey(link)));

  function selectNode(node: TopologyNode) {
    if (selectedNode === node) {
      setSelectedNode(null);
      return;
    }
    setSelectedNode(node);
    if (CLIENT_FLOWS.some((flow) => flow.id === node)) onSelect(node as FlowId);
  }

  return (
    <section className="topology-card card" aria-labelledby="network-topology-heading">
      <div className="section-heading">
        <div>
          <h2 id="network-topology-heading">Network topology</h2>
          <p>Repository topology view · traffic animation disabled</p>
        </div>
        <span className="status-pill status-pill--muted">Offline view</span>
      </div>

      <div className="topology-layout">
        <div className="client-stack" aria-label="Client PCs">
          {CLIENT_FLOWS.map((flow, index) => {
            const host = topology?.hosts.find((item) => item.id === flow.id);
            const active = selectedNode === flow.id;
            return (
              <button
                className={`client-node topology-node ${active ? "is-node-selected" : ""}`}
                key={flow.id}
                onClick={() => selectNode(flow.id)}
                aria-label={`Select ${flow.name}, host ${flow.id}`}
                aria-pressed={active}
                type="button"
              >
                <span className="node-icon"><Monitor size={18} /></span>
                <span className="client-node__copy">
                  <strong>{flow.name}</strong>
                  <small>{flow.id} · {host?.service ?? "Service not available"}</small>
                </span>
                <span className="client-node__port">{host?.udp_destination_port === null || host?.udp_destination_port === undefined
                  ? "Port unavailable"
                  : `UDP ${host.udp_destination_port}`}</span>
                <span className="client-node__num">0{index + 1}</span>
              </button>
            );
          })}
        </div>

        <div className={`topology-wire topology-wire--clients ${hasHighlightedLink(clientLinks) ? "is-link-highlighted" : ""}`} aria-hidden="true">
          <span />
        </div>

        <div className="switch-stack">
          <button
            className={`switch-node topology-node ${selectedNode === "s1" ? "is-node-selected" : ""}`}
            type="button"
            aria-label="Select switch s1"
            aria-pressed={selectedNode === "s1"}
            onClick={() => selectNode("s1")}
          >
            <span className="switch-node__icon"><Router size={20} /></span>
            <span><strong>s1</strong><small>Switch + QoS egress</small></span>
          </button>
          <div className={`link-label ${interSwitchLink && highlightedKeys.has(linkKey(interSwitchLink)) ? "is-link-highlighted" : ""}`}>
            <Waypoints size={15} />
            <span>{interSwitchLink?.bandwidth_mbps === null || !interSwitchLink
              ? "Configured link bandwidth not available"
              : `Configured link · ${formatMbps(interSwitchLink.bandwidth_mbps)} Mbps`}</span>
          </div>
          <button
            className={`switch-node switch-node--secondary topology-node ${selectedNode === "s2" ? "is-node-selected" : ""}`}
            type="button"
            aria-label="Select switch s2"
            aria-pressed={selectedNode === "s2"}
            onClick={() => selectNode("s2")}
          >
            <span className="switch-node__icon"><Router size={18} /></span>
            <span><strong>s2</strong><small>Distribution switch</small></span>
          </button>
        </div>

        <div className={`topology-wire topology-wire--server ${serverLink && highlightedKeys.has(linkKey(serverLink)) ? "is-link-highlighted" : ""}`} aria-hidden="true">
          <span />
        </div>

        <button
          className={`server-node topology-node ${selectedNode === "server" ? "is-node-selected" : ""}`}
          type="button"
          aria-label="Select server receiver endpoint"
          aria-pressed={selectedNode === "server"}
          onClick={() => selectNode("server")}
        >
          <span className="server-node__icon"><Server size={23} /></span>
          <strong>Server</strong>
          <small>{valueOrUnavailable(topology?.hosts.find((host) => host.id === "server")?.ipv4_address)}</small>
          <span className="server-note">Receiver endpoint</span>
        </button>
      </div>

      {selectedNode && (
        <section className="topology-details" aria-live="polite" aria-labelledby="topology-details-heading">
          <div className="topology-details__heading">
            <div><h3 id="topology-details-heading">{selectedNodeName} details</h3><p>Configured topology information · read-only</p></div>
            <button className="topology-details__close" type="button" aria-label={`Close details for ${selectedNodeName}`} onClick={() => setSelectedNode(null)}><X size={16} /> Close</button>
          </div>
          {selectedHost ? (
            <dl className="topology-details__grid">
              <div><dt>Host</dt><dd>{selectedHostConfig?.id ?? "Not available"}</dd></div>
              <div><dt>IP address</dt><dd>{valueOrUnavailable(selectedHostConfig?.ipv4_address)}</dd></div>
              <div><dt>Traffic category</dt><dd>{valueOrUnavailable(selectedHostConfig?.service)}</dd></div>
              <div><dt>Configured protocol / port</dt><dd>{selectedHostConfig?.udp_destination_port == null ? "Not available" : `UDP / ${selectedHostConfig.udp_destination_port}`}</dd></div>
              <div><dt>Connected switch</dt><dd>{valueOrUnavailable(selectedNeighbors[0]?.node)}</dd></div>
              <div><dt>Configured network path</dt><dd>{findPathNodes(selectedNode, "server", topology?.links ?? []).join(" → ") || "Not available"}</dd></div>
            </dl>
          ) : selectedNode === "server" ? (
            <dl className="topology-details__grid">
              <div><dt>Role</dt><dd>{valueOrUnavailable(topology?.hosts.find((host) => host.id === "server")?.role)} · receiver endpoint</dd></div>
              <div><dt>Configured IP address</dt><dd>{valueOrUnavailable(topology?.hosts.find((host) => host.id === "server")?.ipv4_address)}</dd></div>
              <div><dt>Connected switch</dt><dd>{valueOrUnavailable(selectedNeighbors[0]?.node)}</dd></div>
              <div><dt>Receiver configuration</dt><dd>Not available from topology API</dd></div>
              <div><dt>Configured link bandwidth</dt><dd>{serverLink?.bandwidth_mbps == null ? "Not available" : `${formatMbps(serverLink.bandwidth_mbps)} Mbps`}</dd></div>
            </dl>
          ) : (
            <>
              <dl className="topology-details__grid">
                <div><dt>Node</dt><dd>{selectedNode}</dd></div>
                <div><dt>Role</dt><dd>{selectedNode === "s1" ? "Client-side switch and configured QoS egress" : "Server-side distribution switch"}</dd></div>
                <div><dt>Connected nodes</dt><dd>{selectedNeighbors.map((item) => item.node).join(", ") || "Not available"}</dd></div>
                <div><dt>Configured QoS egress</dt><dd>{selectedNode === "s1" ? valueOrUnavailable(topology?.bottleneck_interface) : "Not available"}</dd></div>
                <div><dt>Runtime measurements</dt><dd>Not provided by configured topology data</dd></div>
              </dl>
              <div className="topology-details__links">
                <strong>Configured connected links</strong>
                {selectedNeighbors.length ? selectedNeighbors.map(({ node, link }) => (
                  <span key={`${selectedNode}-${node}`}>{selectedNode} ↔ {node} · {link.bandwidth_mbps == null ? "bandwidth not available" : `${formatMbps(link.bandwidth_mbps)} Mbps`}{link.delay ? ` · ${link.delay}` : ""} · {((link.source === "s1" && link.target === "s2") || (link.source === "s2" && link.target === "s1")) && topology?.bottleneck_interface ? `s1 QoS egress ${topology.bottleneck_interface}` : "interface names not available"}</span>
                )) : <span>Not available</span>}
              </div>
            </>
          )}
        </section>
      )}

      <div className="topology-footnote">
        <span className="legend-dot" />
        <span>Lines show configured topology only, not packet activity.</span>
        <span className="topology-source">h1-h4 → s1 → s2 → server</span>
      </div>
    </section>
  );
}
