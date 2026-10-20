import type { ClientFlow, DemoTelemetryPoint } from "../types";

export const CLIENT_FLOWS: ClientFlow[] = [
  { id: "h1", name: "PC 1", service: "Gaming", port: 5201, className: "gaming" },
  { id: "h2", name: "PC 2", service: "Video Streaming", port: 5202, className: "video" },
  { id: "h3", name: "PC 3", service: "Web", port: 5203, className: "web" },
  { id: "h4", name: "PC 4", service: "Bulk Transfer", port: 5204, className: "bulk" },
];

export const DEMO_SERIES: DemoTelemetryPoint[] = [
  { time: "T-5", h1: 3.5, h2: 3.0, h3: 2.1, h4: 0.9, latency: 19, jitter: 0.7, packetLoss: 0.0 },
  { time: "T-4", h1: 3.8, h2: 3.1, h3: 2.3, h4: 1.0, latency: 21, jitter: 0.9, packetLoss: 0.1 },
  { time: "T-3", h1: 3.6, h2: 3.2, h3: 2.4, h4: 1.1, latency: 23, jitter: 1.1, packetLoss: 0.1 },
  { time: "T-2", h1: 4.0, h2: 3.3, h3: 2.2, h4: 1.1, latency: 24, jitter: 0.8, packetLoss: 0.2 },
  { time: "T-1", h1: 3.9, h2: 3.1, h3: 2.5, h4: 1.2, latency: 22, jitter: 1.0, packetLoss: 0.1 },
  { time: "T", h1: 4.0, h2: 3.2, h3: 2.4, h4: 1.0, latency: 20, jitter: 0.8, packetLoss: 0.0 },
];

export const PROFILE_ALLOCATIONS = {
  LOW: { gaming: 2.7, video: 2.7, web: 2.25, bulk: 1.35, unmatched: 1 },
  MEDIUM: { gaming: 3.6, video: 2.7, web: 1.8, bulk: 0.9, unmatched: 1 },
  HIGH: { gaming: 4.5, video: 2.25, web: 1.35, bulk: 0.9, unmatched: 1 },
} as const;

export const DEMO_METRICS_BY_FLOW = {
  h1: { throughput: 3.9, latency: 20, jitter: 0.8, packetLoss: 0 },
  h2: { throughput: 3.1, latency: 22, jitter: 1.0, packetLoss: 0.1 },
  h3: { throughput: 2.4, latency: 24, jitter: 0.9, packetLoss: 0.1 },
  h4: { throughput: 1.0, latency: 26, jitter: 1.2, packetLoss: 0.2 },
} as const;
