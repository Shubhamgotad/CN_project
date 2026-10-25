import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiTopology } from "../api/client";
import { Topology } from "./Topology";

let root: Root;
let container: HTMLDivElement;
let unmounted = false;
const onSelect = vi.fn();

const configuredTopology: ApiTopology = {
  status: "configured",
  source: "test configured topology",
  live_connectivity_verified: false,
  bottleneck_interface: "s1-eth5",
  hosts: [
    { id: "h1", role: "client", service: "Gaming", udp_destination_port: 5201, ipv4_address: "10.0.0.1" },
    { id: "h2", role: "client", service: "Video Streaming", udp_destination_port: 5202, ipv4_address: null },
    { id: "h3", role: "client", service: "Web", udp_destination_port: 5203, ipv4_address: null },
    { id: "h4", role: "client", service: "Bulk Transfer", udp_destination_port: 5204, ipv4_address: null },
    { id: "server", role: "server", service: null, udp_destination_port: null, ipv4_address: "10.0.0.5" },
  ],
  switches: ["s1", "s2"],
  links: [
    { source: "h1", target: "s1", bandwidth_mbps: null, delay: null },
    { source: "h2", target: "s1", bandwidth_mbps: null, delay: null },
    { source: "h3", target: "s1", bandwidth_mbps: null, delay: null },
    { source: "h4", target: "s1", bandwidth_mbps: null, delay: null },
    { source: "s1", target: "s2", bandwidth_mbps: 10, delay: "10ms" },
    { source: "s2", target: "server", bandwidth_mbps: null, delay: null },
  ],
};

async function render(topology: ApiTopology | null = configuredTopology) {
  await act(async () => {
    root.render(<Topology topology={topology} onSelect={onSelect} />);
  });
}

async function click(label: string) {
  const button = Array.from(container.querySelectorAll("button"))
    .find((item) => item.getAttribute("aria-label") === label);
  expect(button).toBeDefined();
  await act(async () => button?.click());
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  unmounted = false;
  onSelect.mockReset();
});

afterEach(async () => {
  if (!unmounted) await act(async () => root.unmount());
  container.remove();
});

describe("interactive configured topology", () => {
  it("selects a host, displays API-backed details, and highlights its full server path", async () => {
    await render();
    const nodes = Array.from(container.querySelectorAll(".topology-node"));
    expect(nodes).toHaveLength(7);
    expect(nodes.every((node) => node.tagName === "BUTTON" && (node as HTMLButtonElement).tabIndex === 0)).toBe(true);

    const host = container.querySelector<HTMLButtonElement>('[aria-label="Select PC 3, host h3"]');
    host?.focus();
    expect(document.activeElement).toBe(host);
    await click("Select PC 3, host h3");

    expect(container.textContent).toContain("IP addressNot available");
    expect(container.textContent).toContain("Traffic categoryWeb");
    expect(container.textContent).toContain("Configured protocol / portUDP / 5203");
    expect(container.textContent).toContain("Connected switchs1");
    expect(container.textContent).toContain("Configured network pathh3 → s1 → s2 → server");
    expect(onSelect).toHaveBeenCalledWith("h3");
    expect(host?.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelectorAll(".topology-wire.is-link-highlighted")).toHaveLength(2);
    expect(container.querySelector(".link-label")?.classList.contains("is-link-highlighted")).toBe(true);
  });

  it("shows configured switch neighbors, QoS egress and link capacity, then changes selection", async () => {
    await render();
    await click("Select switch s1");
    expect(container.textContent).toContain("Connected nodesh1, h2, h3, h4, s2");
    expect(container.textContent).toContain("Configured QoS egresss1-eth5");
    expect(container.textContent).toContain("s1 ↔ s2 · 10 Mbps · 10ms · s1 QoS egress s1-eth5");
    expect(container.textContent).toContain("Runtime measurementsNot provided by configured topology data");
    expect(container.querySelector(".topology-wire--clients")?.classList.contains("is-link-highlighted")).toBe(true);
    expect(container.querySelector(".topology-wire--server")?.classList.contains("is-link-highlighted")).toBe(false);

    await click("Select switch s2");
    expect(container.textContent).toContain("s2 details");
    expect(container.querySelector(".topology-wire--clients")?.classList.contains("is-link-highlighted")).toBe(false);
    expect(container.querySelector(".topology-wire--server")?.classList.contains("is-link-highlighted")).toBe(true);
  });

  it("shows the configured receiver IP and supports deselection", async () => {
    await render();
    await click("Select server receiver endpoint");
    expect(container.textContent).toContain("Configured IP address10.0.0.5");
    expect(container.textContent).toContain("Connected switchs2");
    expect(container.textContent).toContain("Receiver configurationNot available from topology API");
    expect(container.querySelector(".topology-wire--server")?.classList.contains("is-link-highlighted")).toBe(true);

    await click("Close details for server");
    expect(container.querySelector(".topology-details")).toBeNull();
    expect(container.querySelector(".topology-node.is-node-selected")).toBeNull();
    expect(container.querySelector(".topology-wire.is-link-highlighted")).toBeNull();
  });

  it("does not invent configuration when topology data is unavailable", async () => {
    await render(null);
    await click("Select server receiver endpoint");
    expect(container.textContent).toContain("Configured IP addressNot available");
    expect(container.textContent).toContain("Connected switchNot available");
    expect(container.textContent).toContain("Receiver configurationNot available from topology API");
    expect(container.textContent).not.toContain("10.0.0.5");
  });
});
