import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiTelemetry } from "../api/client";
import { getActiveTrafficExecution, getLatestTelemetry, getTrafficExecution, startTrafficExecution, stopTrafficExecution } from "../api/client";
import { TrafficLab } from "./TrafficLab";

vi.mock("../api/client", () => ({
  getActiveTrafficExecution: vi.fn(),
  getLatestTelemetry: vi.fn(),
  getTrafficExecution: vi.fn(),
  startTrafficExecution: vi.fn(),
  stopTrafficExecution: vi.fn(),
}));

const getLatestTelemetryMock = vi.mocked(getLatestTelemetry);
const getActiveMock = vi.mocked(getActiveTrafficExecution);
const getExecutionMock = vi.mocked(getTrafficExecution);
const startExecutionMock = vi.mocked(startTrafficExecution);
const stopExecutionMock = vi.mocked(stopTrafficExecution);
let root: Root;
let container: HTMLDivElement;
let rootUnmounted = false;

function telemetry(status: ApiTelemetry["status"]): ApiTelemetry {
  return {
    status,
    source: status === "unavailable" ? "none" : "configured_person1_jsonl",
    measurement_age_seconds: status === "available" ? 2.5 : status === "stale" ? 420 : null,
    stale_after_seconds: 300,
    message: status === "available"
      ? "Latest validated record from the configured file."
      : status === "stale" ? "Latest configured measurement is stale." : "No telemetry file is configured.",
    record: status === "unavailable" ? null : {
      timestamp: "2026-10-20T10:00:00+00:00",
      source: "h1",
      scenario: "normal",
      throughput_mbps: 2.997,
      latency_ms: 20.6309,
      jitter_ms: 0.0334,
      packet_loss_percent: 0,
      queue_size: 4,
      queue_drops: 0,
      measurement_direction: null,
      interface: "s1-eth5",
    },
  };
}

async function renderTrafficLab() {
  await act(async () => {
    root.render(<TrafficLab />);
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  rootUnmounted = false;
  getLatestTelemetryMock.mockReset();
  getLatestTelemetryMock.mockResolvedValue(telemetry("available"));
  getActiveMock.mockReset().mockResolvedValue(null);
  getExecutionMock.mockReset();
  startExecutionMock.mockReset();
  stopExecutionMock.mockReset();
});

afterEach(async () => {
  if (!rootUnmounted) await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Traffic Lab telemetry and execution boundary", () => {
  it("renders the measurements and source from an available API record", async () => {
    getLatestTelemetryMock.mockResolvedValue(telemetry("available"));
    await renderTrafficLab();

    expect(container.textContent).toContain("Fresh · Available");
    expect(container.textContent).toContain("Available");
    expect(container.textContent).toContain("h1");
    expect(container.textContent).toContain("normal");
    expect(container.textContent).toContain("s1-eth5");
    expect(container.textContent).toContain("2026-10-20T10:00:00+00:00");
    expect(container.textContent).toContain("2.5 seconds");
    expect(container.textContent).toContain("2.997 Mbps");
    expect(container.textContent).toContain("20.6309 ms");
    expect(container.textContent).toContain("0.0334 ms");
    expect(container.textContent).toContain("0 %");
    expect(container.textContent).toContain("4 packets");
    expect(container.textContent).toContain("configured_person1_jsonl");
  });

  it("labels stale measurements stale and never calls them current", async () => {
    getLatestTelemetryMock.mockResolvedValue(telemetry("stale"));
    await renderTrafficLab();

    expect(container.textContent).toContain("Stale");
    expect(container.textContent).toContain("stale recorded measurements, not current traffic values");
    expect(container.textContent).toContain("420.0 seconds");
  });

  it("shows an explicit unavailable state without retaining measurement values", async () => {
    getLatestTelemetryMock.mockResolvedValue(telemetry("unavailable"));
    await renderTrafficLab();

    expect(container.textContent).toContain("Unavailable");
    expect(container.textContent).toContain("No telemetry file is configured.");
    expect(container.textContent).not.toContain("2.997 Mbps");
  });

  it("keeps traffic controls independent from the telemetry source and labels Mininet emulation", async () => {
    getLatestTelemetryMock.mockResolvedValue(telemetry("unavailable"));
    await renderTrafficLab();

    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.disabled).toBe(false);
    expect(container.textContent).toContain("This is emulated traffic, not production or physical-network traffic.");
    expect(container.textContent).toContain("configured inventory, not proof that Mininet is currently connected");
    expect(container.textContent).not.toContain("verified Mininet topology");
  });

  it("clears prior measurements and reports request failures", async () => {
    getLatestTelemetryMock.mockResolvedValueOnce(telemetry("available"));
    getLatestTelemetryMock.mockRejectedValueOnce(new Error("connection lost"));
    vi.useFakeTimers();
    await renderTrafficLab();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(container.textContent).toContain("API error");
    expect(container.textContent).toContain("Telemetry request failed: connection lost");
    expect(container.textContent).not.toContain("2.997 Mbps");
  });

  it("refreshes periodically and clears its timer on unmount", async () => {
    getLatestTelemetryMock.mockResolvedValue(telemetry("available"));
    vi.useFakeTimers();
    await renderTrafficLab();
    expect(getLatestTelemetryMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(getLatestTelemetryMock).toHaveBeenCalledTimes(2);

    await act(async () => root.unmount());
    rootUnmounted = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getLatestTelemetryMock).toHaveBeenCalledTimes(2);
  });

  it("keeps Preview local-only and enables validated custom execution", async () => {
    getLatestTelemetryMock.mockResolvedValue(telemetry("available"));
    await renderTrafficLab();

    const buttons = Array.from(container.querySelectorAll("button"));
    const startButton = buttons.find((button) => button.textContent?.includes("Start traffic"));
    const stopButton = buttons.find((button) => button.textContent?.includes("Stop"));
    const previewButton = buttons.find((button) => button.textContent?.includes("Preview configuration"));
    const notice = container.querySelector('[role="note"][aria-label="Local preview information"]');
    expect(startButton?.disabled).toBe(false);
    expect(stopButton?.disabled).toBe(true);
    expect(notice?.textContent).toContain("Preview and execution");
    expect(notice?.textContent).toContain("Preview stays in your browser");
    expect(notice?.textContent).toContain("Start executes the selected source and planned flow(s)");
    expect(container.textContent).toContain("Planned");

    await act(async () => previewButton?.click());
    expect(container.textContent).toContain("Local configuration preview");
    expect(container.textContent).toContain("This preview does not launch traffic");
    expect(getLatestTelemetryMock).toHaveBeenCalledTimes(1);
  });

  it("confirms and starts the selected real congestion preset", async () => {
    const experiment = execution("starting");
    startExecutionMock.mockResolvedValue(experiment);
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderTrafficLab();
    expect(container.querySelector<HTMLButtonElement>('.flow-choice[aria-pressed="true"]')?.textContent).toContain("All flows");
    const startButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent?.includes("Start traffic"));
    expect(startButton?.disabled).toBe(false);
    await act(async () => startButton?.click());
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Rate: 4 Mbps per flow"));
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Total offered rate: 16 Mbps"));
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("h1, h2, h3, h4"));
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Destination ports: 5201, 5202, 5203, 5204"));
    expect(startExecutionMock).toHaveBeenCalledWith("congestion", 4, 20, "all");
    expect(container.textContent).toContain("Starting");
    expect(container.textContent).toContain("Traffic execution");
  });

  it.each(["normal", "moderate", "burst"] as const)(
    "sends the selected host to the %s execution request",
    async (scenarioName) => {
      const host = "h3" as const;
      startExecutionMock.mockResolvedValue(execution("starting", {
        scenario: scenarioName,
        source_host: host,
        flows: [host],
        target_rate_mbps: scenarioName === "normal" ? 3 : scenarioName === "moderate" ? 6 : 8,
        duration_seconds: scenarioName === "burst" ? 23 : 20,
      }));
      vi.stubGlobal("confirm", vi.fn(() => true));
      await renderTrafficLab();

      const scenario = container.querySelector<HTMLSelectElement>("#scenario");
      await act(async () => {
        if (scenario) {
          scenario.value = scenarioName;
          scenario.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
      const hostChoice = Array.from(container.querySelectorAll<HTMLButtonElement>(".flow-choice"))
        .find((button) => button.textContent?.startsWith(`${host} ·`));
      await act(async () => hostChoice?.click());
      await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.click());

      expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining(`Selected source: ${host}`));
      expect(startExecutionMock).toHaveBeenCalledWith(scenarioName, expect.any(Number), expect.any(Number), host);
      expect(container.textContent).toContain(`API-reported source: ${host}`);
      expect(container.textContent).toContain(`Actual flows: ${host}`);
    },
  );

  it("reports source hosts from the API rather than the changed local selection", async () => {
    getActiveMock.mockResolvedValue(execution("running", {
      scenario: "normal",
      source_host: "h2",
      flows: ["h2"],
    }));
    await renderTrafficLab();
    expect(container.textContent).toContain("API-reported source: h2");
    expect(container.textContent).toContain("Actual flows: h2");
  });

  it("confirms and sends the actual custom rate and duration", async () => {
    startExecutionMock.mockResolvedValue(execution("starting", { target_rate_mbps: 5, duration_seconds: 17 }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderTrafficLab();
    const [rateInput, durationInput] = container.querySelectorAll<HTMLInputElement>('.form-row input[type="number"]');
    const setInput = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setInput?.call(rateInput, "5");
      rateInput.dispatchEvent(new Event("input", { bubbles: true }));
      setInput?.call(durationInput, "17");
      durationInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const start = container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]');
    expect(start?.disabled).toBe(false);
    await act(async () => start?.click());
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Rate: 5 Mbps per flow"));
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Total offered rate: 20 Mbps"));
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Duration: 17 seconds"));
    expect(startExecutionMock).toHaveBeenCalledWith("congestion", 5, 17, "all");
  });

  it("disables Start and explains an out-of-range rate", async () => {
    await renderTrafficLab();
    const [rateInput] = container.querySelectorAll<HTMLInputElement>('.form-row input[type="number"]');
    const setInput = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setInput?.call(rateInput, "10.1");
      rateInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.disabled).toBe(true);
    expect(container.textContent).toContain("Rate must be between 0.1 and 10 Mbps per flow");
  });

  it("keeps Burst controls fixed to the documented 23-second schedule", async () => {
    startExecutionMock.mockResolvedValue(execution("starting", { scenario: "burst", flows: ["h1"], target_rate_mbps: 8, duration_seconds: 23 }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderTrafficLab();
    const scenario = container.querySelector<HTMLSelectElement>("#scenario");
    await act(async () => {
      if (scenario) {
        scenario.value = "burst";
        scenario.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    const [rateInput, durationInput] = container.querySelectorAll<HTMLInputElement>('.form-row input[type="number"]');
    expect(rateInput.value).toBe("8");
    expect(durationInput.value).toBe("23");
    expect(rateInput.disabled).toBe(true);
    expect(durationInput.disabled).toBe(true);
    const start = container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]');
    expect(start?.disabled).toBe(false);
    expect(container.textContent).toContain("five 3-second UDP bursts");
    await act(async () => start?.click());
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Duration: 23 seconds total"));
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Rate: 8 Mbps per flow"));
    expect(startExecutionMock).toHaveBeenCalledWith("burst", 8, 23, "h1");
  });

  it("retains the Normal default execution values", async () => {
    startExecutionMock.mockResolvedValue(execution("starting", { scenario: "normal", flows: ["h1"], target_rate_mbps: 3, duration_seconds: 20 }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderTrafficLab();
    const scenario = container.querySelector<HTMLSelectElement>("#scenario");
    await act(async () => {
      if (scenario) {
        scenario.value = "normal";
        scenario.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.disabled).toBe(false);
    await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.click());
    expect(startExecutionMock).toHaveBeenCalledWith("normal", 3, 20, "h1");
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Destination ports: 5201"));
  });

  it("does not offer All flows outside Congestion and reports the planned host", async () => {
    startExecutionMock.mockResolvedValue(execution("starting", {
      scenario: "normal", flows: ["h1"], target_rate_mbps: 3, duration_seconds: 20,
    }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderTrafficLab();

    const scenario = container.querySelector<HTMLSelectElement>("#scenario");
    await act(async () => {
      if (scenario) {
        scenario.value = "normal";
        scenario.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    expect(Array.from(container.querySelectorAll(".flow-choice")).some((button) => button.textContent?.startsWith("All flows"))).toBe(false);
    expect(container.textContent).toContain("Normal plans only h1 on UDP 5201");
    await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.click());
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Planned execution flows: h1"));
    expect(startExecutionMock).toHaveBeenCalledWith("normal", 3, 20, "h1");
    expect(container.textContent).toContain("Not in execution");
    expect(container.textContent).toContain("Not included in the API-reported execution");
  });

  it("confirms and starts the documented Moderate profile", async () => {
    startExecutionMock.mockResolvedValue(execution("starting", { scenario: "moderate", flows: ["h1"], target_rate_mbps: 6, duration_seconds: 20 }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderTrafficLab();
    const scenario = container.querySelector<HTMLSelectElement>("#scenario");
    await act(async () => {
      if (scenario) {
        scenario.value = "moderate";
        scenario.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    const start = container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]');
    expect(start?.disabled).toBe(false);
    await act(async () => start?.click());
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Moderate"));
    expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Rate: 6 Mbps per flow"));
    expect(startExecutionMock).toHaveBeenCalledWith("moderate", 6, 20, "h1");
  });

  it.each(["h1", "h2", "h3", "h4"] as const)(
    "keeps the fixed Congestion launch available when %s is selected for the local preview",
    async (flow) => {
      const experiment = execution("starting");
      startExecutionMock.mockResolvedValue(experiment);
      vi.stubGlobal("confirm", vi.fn(() => true));
      await renderTrafficLab();

      const clientChoice = Array.from(container.querySelectorAll<HTMLButtonElement>(".flow-choice"))
        .find((button) => button.textContent?.startsWith(`${flow} ·`));
      await act(async () => clientChoice?.click());
      expect(clientChoice?.getAttribute("aria-pressed")).toBe("true");

      const startButton = container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]');
      expect(startButton?.disabled).toBe(false);
      await act(async () => startButton?.click());

      expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining(`Selected source: ${flow}`));
      expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining(`Planned execution flows: ${flow}`));
      expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Rate: 4 Mbps per flow"));
      expect(globalThis.confirm).toHaveBeenCalledWith(expect.stringContaining("Duration: 20 seconds"));
      expect(startExecutionMock).toHaveBeenCalledTimes(1);
      expect(startExecutionMock).toHaveBeenCalledWith("congestion", 4, 20, flow);
      expect(container.textContent).toContain(`Congestion plans only ${flow}`);
      expect(container.textContent).toContain("h1 · Gaming");
      expect(container.textContent).toContain("h2 · Video Streaming");
      expect(container.textContent).toContain("h3 · Web");
      expect(container.textContent).toContain("h4 · Bulk Transfer");
    },
  );

  it("shows the confirmed process lifecycle and sends Stop for its exact ID", async () => {
    const running = execution("running");
    getActiveMock.mockResolvedValue(running);
    stopExecutionMock.mockResolvedValue(execution("stopping"));
    await renderTrafficLab();
    expect(container.textContent).toContain("Running");
    expect(container.textContent).toContain("Elapsed 3.2s");
    expect(container.querySelector<HTMLProgressElement>("#traffic-execution-progress")?.value).toBeCloseTo(16);
    expect(container.textContent).toContain("3.2 / 20s · reported by the execution API");
    const stopButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent?.trim() === "Stop");
    expect(stopButton?.disabled).toBe(false);
    await act(async () => stopButton?.click());
    expect(stopExecutionMock).toHaveBeenCalledWith(running.experiment_id);
    expect(container.textContent).toContain("Stopping");
  });

  it("polls after Stop and displays stopped only after status confirms it", async () => {
    const running = execution("running");
    getActiveMock.mockResolvedValue(running);
    stopExecutionMock.mockResolvedValue(execution("stopping"));
    getExecutionMock.mockResolvedValue(execution("stopped", { process_alive: false, can_stop: false }));
    vi.useFakeTimers();
    await renderTrafficLab();
    const stop = Array.from(container.querySelectorAll("button")).find((button) => button.textContent?.trim() === "Stop");
    expect(stop?.disabled).toBe(false);
    await act(async () => stop?.click());
    expect(container.textContent).toContain("Stopping");
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(getExecutionMock).toHaveBeenCalledWith(running.experiment_id);
    expect(container.textContent).toContain("Stopped");
  });

  it("reports completed, failed and stopped states from the API without inferring from telemetry", async () => {
    for (const result of [
      execution("completed", { process_alive: false, can_stop: false }),
      execution("failed", { process_alive: false, can_stop: false, error: "Producer exited with code 2." }),
      execution("stopped", { process_alive: false, can_stop: false }),
    ]) {
      getActiveMock.mockResolvedValueOnce(result);
      await renderTrafficLab();
      expect(container.textContent).toContain(result.status[0].toUpperCase() + result.status.slice(1));
      if (result.error) expect(container.textContent).toContain(result.error);
      await act(async () => root.unmount());
      rootUnmounted = true;
      container.remove();
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
      rootUnmounted = false;
    }
  });

  it("does not present telemetry as execution-linked results after completion", async () => {
    getActiveMock.mockResolvedValue(execution("completed", { process_alive: false, can_stop: false }));
    await renderTrafficLab();

    expect(container.textContent).toContain("Process completed.");
    expect(container.textContent).toContain("does not link measured telemetry to the execution");
  });

  it("locks controls while start is pending, then exposes API failure and retry", async () => {
    let resolveStart: ((value: ReturnType<typeof execution>) => void) | undefined;
    startExecutionMock.mockReturnValue(new Promise((resolve) => { resolveStart = resolve; }));
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderTrafficLab();
    const button = () => container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]');
    await act(async () => button()?.click());
    expect(button()?.disabled).toBe(true);
    expect(Array.from(container.querySelectorAll("button")).find((item) => item.textContent?.trim() === "Stop")?.disabled).toBe(true);
    await act(async () => resolveStart?.(execution("running")));
    expect(container.textContent).toContain("Running");

    getActiveMock.mockRejectedValueOnce(new Error("API restart"));
    // A stop API error is visible and does not fabricate a terminal result.
    stopExecutionMock.mockRejectedValueOnce(new Error("stop failed"));
    const stop = Array.from(container.querySelectorAll("button")).find((item) => item.textContent?.trim() === "Stop");
    await act(async () => stop?.click());
    expect(container.textContent).toContain("stop failed");
    expect(container.textContent).toContain("Running");
  });

  it("recovers the execution controls after an API status failure", async () => {
    getActiveMock.mockRejectedValueOnce(new Error("API unavailable"));
    getActiveMock.mockResolvedValueOnce(null);
    await renderTrafficLab();
    expect(container.textContent).toContain("API unavailable");
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.disabled).toBe(true);
    const retry = Array.from(container.querySelectorAll("button")).find((button) => button.textContent?.includes("Retry status check"));
    await act(async () => retry?.click());
    expect(container.textContent).not.toContain("API unavailable");
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Start traffic"]')?.disabled).toBe(false);
  });

  it("shows detailed status-refresh validation errors and retains the last confirmed execution", async () => {
    const running = execution("running");
    getActiveMock.mockResolvedValue(running);
    getExecutionMock.mockRejectedValueOnce(new Error(
      "Backend rejected GET /api/execution/0123456789abcdef0123456789abcdef (422): path.experiment_id: invalid identifier",
    ));
    vi.useFakeTimers();
    await renderTrafficLab();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(container.textContent).toContain("Backend rejected GET /api/execution/0123456789abcdef0123456789abcdef (422)");
    expect(container.textContent).toContain("Running");
  });
});

function execution(status: "starting" | "running" | "stopping" | "completed" | "failed" | "stopped", updates: Partial<import("../api/client").ApiExecution> = {}): import("../api/client").ApiExecution {
  return {
    experiment_id: "0123456789abcdef0123456789abcdef",
    scenario: "congestion",
    source_host: "all",
    status,
    flows: ["h1", "h2", "h3", "h4"],
    target_rate_mbps: 4,
    duration_seconds: 20,
    created_at: "2026-10-20T10:00:00+00:00",
    started_at: "2026-10-20T10:00:00+00:00",
    finished_at: null,
    elapsed_seconds: 3.2,
    process_alive: true,
    can_stop: true,
    process_exit_code: null,
    error: null,
    ...updates,
  };
}
