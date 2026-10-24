import { AlertCircle, Check, CircleStop, FlaskConical, Play, Radio, ShieldAlert, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { getActiveTrafficExecution, getLatestTelemetry, getTrafficExecution, startTrafficExecution, stopTrafficExecution } from "../api/client";
import type { ApiExecution, ApiTelemetry, ExecutionScenario } from "../api/client";
import { CLIENT_FLOWS } from "../data/demo";
import type { FlowId } from "../types";

type Scenario = "normal" | "moderate" | "congestion" | "burst";

const SCENARIOS: Record<Scenario, { label: string; rate: number; duration: number; description: string }> = {
  normal: { label: "Normal", rate: 3, duration: 20, description: "One selected-client 3 Mbps UDP flow for 20 seconds." },
  moderate: { label: "Moderate", rate: 6, duration: 20, description: "One selected-client configurable-rate UDP flow; default 6 Mbps for 20 seconds." },
  congestion: { label: "Congestion", rate: 4, duration: 20, description: "All flows: four concurrent UDP flows at 4 Mbps each. Select one client for a single 4 Mbps flow." },
  burst: { label: "Burst", rate: 8, duration: 23, description: "Fixed schedule from one selected client: five 3-second UDP bursts with four 2-second gaps (23 seconds total)." },
};

const TELEMETRY_REFRESH_MS = 5_000;
const EXECUTION_REFRESH_MS = 1_000;
const TERMINAL_STATES = new Set<ApiExecution["status"]>(["completed", "failed", "stopped"]);

function metric(value: number | null, unit: string) {
  return value === null ? "Unavailable" : `${value} ${unit}`;
}

function telemetryStatusLabel(value: ApiTelemetry["status"]) {
  if (value === "available") return "Available";
  if (value === "stale") return "Stale";
  return "Unavailable";
}

export function TrafficLab() {
  const [scenario, setScenario] = useState<Scenario>("congestion");
  const [selectedFlow, setSelectedFlow] = useState<FlowId | "all">("all");
  const [rate, setRate] = useState(4);
  const [duration, setDuration] = useState(20);
  const [previewed, setPreviewed] = useState(false);
  const [error, setError] = useState("");
  const [telemetry, setTelemetry] = useState<ApiTelemetry | null>(null);
  const [telemetryError, setTelemetryError] = useState("");
  const [telemetryLoading, setTelemetryLoading] = useState(true);
  const [telemetryCheckedAt, setTelemetryCheckedAt] = useState<string | null>(null);
  const [execution, setExecution] = useState<ApiExecution | null>(null);
  const [executionError, setExecutionError] = useState("");
  const [executionLoading, setExecutionLoading] = useState(true);
  const [executionApiAvailable, setExecutionApiAvailable] = useState(false);
  const [requestPending, setRequestPending] = useState<"start" | "stop" | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const refreshTelemetry = async () => {
      try {
        const result = await getLatestTelemetry();
        if (active) {
          setTelemetry(result);
          setTelemetryError("");
        }
      } catch (reason: unknown) {
        if (active) {
          setTelemetry(null);
          setTelemetryError(reason instanceof Error ? reason.message : "Telemetry request failed.");
        }
      } finally {
        if (active) {
          setTelemetryLoading(false);
          setTelemetryCheckedAt(new Date().toISOString());
          timer = setTimeout(() => void refreshTelemetry(), TELEMETRY_REFRESH_MS);
        }
      }
    };

    void refreshTelemetry();
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    let active = true;
    getActiveTrafficExecution().then((result) => {
      if (active) {
        setExecution(result);
        setExecutionApiAvailable(true);
      }
    }).catch((reason: unknown) => {
      if (active) {
        setExecutionApiAvailable(false);
        setExecutionError(reason instanceof Error ? reason.message : "Could not check traffic execution state.");
      }
    }).finally(() => {
      if (active) setExecutionLoading(false);
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!execution || (TERMINAL_STATES.has(execution.status) && !execution.process_alive)) return undefined;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const current = await getTrafficExecution(execution.experiment_id);
        if (active) {
          setExecution(current);
          setExecutionApiAvailable(true);
          setExecutionError("");
        }
      } catch (reason: unknown) {
        if (active) {
          setExecutionApiAvailable(false);
          setExecutionError(reason instanceof Error ? reason.message : "Could not refresh traffic execution state.");
        }
      } finally {
        if (active) timer = setTimeout(() => void poll(), EXECUTION_REFRESH_MS);
      }
    };
    timer = setTimeout(() => void poll(), EXECUTION_REFRESH_MS);
    return () => { active = false; if (timer !== undefined) clearTimeout(timer); };
  }, [execution?.experiment_id, execution?.status, execution?.process_alive]);

  const scenarioDetails = SCENARIOS[scenario];
  const apiConnected = executionApiAvailable;
  const plannedFlows = useMemo(
    () => scenario === "congestion" && selectedFlow === "all"
      ? CLIENT_FLOWS
      : CLIENT_FLOWS.filter((flow) => flow.id === selectedFlow),
    [scenario, selectedFlow],
  );
  const selectedSourceHost = scenario === "congestion" ? selectedFlow : selectedFlow === "all" ? "h1" : selectedFlow;
  const executionPlanHelp = scenario === "congestion" && selectedFlow === "all"
    ? "All flows plans four concurrent UDP flows: h1–h4 on UDP 5201–5204."
    : `${scenarioDetails.label} plans only ${selectedSourceHost} on UDP ${CLIENT_FLOWS.find((flow) => flow.id === selectedSourceHost)?.port}.`;
  const validationReason = !Number.isFinite(rate) || rate < 0.1 || rate > 10 || Math.abs(rate * 10 - Math.round(rate * 10)) > 1e-8
      ? "Rate must be between 0.1 and 10 Mbps per flow, in 0.1 Mbps increments."
      : !Number.isInteger(duration) || duration < 1 || duration > 60
        ? "Duration must be a whole number between 1 and 60 seconds."
        : scenario === "burst" && (rate !== 8 || duration !== 23)
          ? "Burst is fixed at 8 Mbps for a 23-second schedule: five 3-second bursts with 2-second gaps."
          : "";
  const profileValid = validationReason === "";
  const activeExecution = Boolean(execution && (!TERMINAL_STATES.has(execution.status) || execution.process_alive));
  const canStart = apiConnected && !executionLoading && !requestPending && !activeExecution && profileValid;

  async function refreshExecution() {
    setExecutionError("");
    setExecutionLoading(true);
    try {
      setExecution(await getActiveTrafficExecution());
      setExecutionApiAvailable(true);
    } catch (reason: unknown) {
      setExecutionApiAvailable(false);
      setExecutionError(reason instanceof Error ? reason.message : "Could not check traffic execution state.");
    } finally {
      setExecutionLoading(false);
    }
  }

  async function startExecution() {
    if (!canStart) return;
    const preset = SCENARIOS[scenario];
    const flowNames = plannedFlows.map((flow) => flow.id).join(", ");
    const ports = plannedFlows.map((flow) => flow.port);
    const totalRate = rate * plannedFlows.length;
    const durationLabel = scenario === "burst" ? "23 seconds total (5 × 3-second bursts, 4 × 2-second gaps)" : `${duration} seconds`;
    const sourceLabel = selectedFlow === "all" ? "All flows (h1–h4)" : selectedSourceHost;
    const confirmation = `Launch real ${preset.label} traffic after the API confirms the expected Mininet topology is running?\n\nSelected source: ${sourceLabel}\nPlanned execution flows: ${flowNames}\nProtocol: UDP\nRate: ${rate} Mbps per flow\nTotal offered rate: ${totalRate} Mbps across ${plannedFlows.length} flow${plannedFlows.length === 1 ? "" : "s"}\nDuration: ${durationLabel}\nDestination ports: ${ports.join(", ")}\n\nThis sends real UDP traffic. Preview configuration does not launch traffic.`;
    if (!window.confirm(confirmation)) return;
    setRequestPending("start");
    setExecutionError("");
    setError("");
    try {
      const result = await startTrafficExecution(scenario as ExecutionScenario, rate, duration, selectedSourceHost);
      setExecution(result);
      setExecutionApiAvailable(true);
    } catch (startReason: unknown) {
      const startMessage = startReason instanceof Error ? startReason.message : "Traffic start request failed.";
      setExecutionError(startMessage);
      try {
        const active = await getActiveTrafficExecution();
        setExecution(active);
        setExecutionApiAvailable(true);
      } catch (statusReason: unknown) {
        setExecutionApiAvailable(false);
        const statusMessage = statusReason instanceof Error
          ? statusReason.message
          : "Could not refresh execution status after the failed start request.";
        setExecutionError(`${startMessage} Status refresh failed: ${statusMessage}`);
      }
    } finally {
      setRequestPending(null);
    }
  }

  async function stopExecution() {
    if (!execution || !execution.can_stop || requestPending) return;
    setRequestPending("stop");
    setExecutionError("");
    try {
      setExecution(await stopTrafficExecution(execution.experiment_id));
    } catch (reason: unknown) {
      setExecutionError(reason instanceof Error ? reason.message : "Traffic stop request failed.");
    } finally {
      setRequestPending(null);
    }
  }

  function updateScenario(value: Scenario) {
    setScenario(value);
    setSelectedFlow(value === "congestion" ? "all" : selectedFlow === "all" ? "h1" : selectedFlow);
    setRate(value === "congestion" ? 4 : SCENARIOS[value].rate);
    setDuration(SCENARIOS[value].duration);
    setPreviewed(false);
  }

  function previewConfiguration() {
    setError("");
    if (!Number.isFinite(rate) || rate < 0.1 || rate > 10 || Math.abs(rate * 10 - Math.round(rate * 10)) > 1e-8) {
      setError("Enter a rate from 0.1 to 10 Mbps per flow in 0.1 Mbps increments.");
      setPreviewed(false);
      return;
    }
    if (!Number.isInteger(duration) || duration < 1 || duration > 60) {
      setError("Enter a whole-number duration between 1 and 60 seconds.");
      setPreviewed(false);
      return;
    }
    setPreviewed(true);
  }

  const progressPercent = execution
    ? execution.status === "completed"
      ? 100
      : Math.min(100, Math.max(0, (execution.elapsed_seconds / execution.duration_seconds) * 100))
    : 0;

  return (
    <div className="page-stack">
      <section className="page-intro">
        <div>
          <div className="eyebrow"><span className="eyebrow-mark" /> TRAFFIC CONFIGURATION</div>
          <h1>Traffic Lab</h1>
          <p>Preview locally or launch a validated Normal, Moderate, or Congestion configuration, or the fixed Burst schedule, in the expected Mininet topology. This is emulated traffic, not production or physical-network traffic.</p>
        </div>
        <span className={`status-pill ${executionLoading ? "status-pill--muted" : apiConnected ? "status-pill--surface" : "status-pill--muted"}`}><ShieldAlert size={14} /> {executionLoading ? "Checking execution API" : apiConnected ? "Execution API available" : "Execution API unavailable"}</span>
      </section>

      <div className="safety-callout card">
        <div className="callout-icon"><AlertCircle size={18} /></div>
        <div>
          <strong>Guarded traffic execution</strong>
          <p>Start launches only an allowlisted scenario with validated parameters after the backend verifies the required Mininet namespaces, interfaces, and server address. The topology display is configured inventory, not proof that Mininet is currently connected. Traffic is Mininet-emulated; this does not represent production or physical-network traffic. The runner does not change Mininet, OVS, routes, or traffic-control settings.</p>
        </div>
      </div>

      <section className="card traffic-telemetry" aria-label="Latest telemetry">
        <div className="section-heading">
          <div>
            <h2>Latest measured telemetry</h2>
            <p>Read-only measurements from the configured telemetry file. This status does not indicate whether traffic is running.</p>
          </div>
          <span
            className={`status-pill ${telemetry?.status === "available" ? "status-pill--surface" : "status-pill--muted"}`}
            aria-label={`Telemetry freshness: ${telemetry?.status === "available" ? "fresh" : telemetry?.status ?? (telemetryError ? "API error" : "checking")}`}
          >
            <Radio size={13} /> {telemetry?.status === "available" ? "Fresh · Available" : telemetry ? telemetryStatusLabel(telemetry.status) : telemetryError ? "API error" : "Checking"}
          </span>
        </div>
        {telemetryError ? (
          <p className="api-data-card__message" role="alert">Telemetry request failed: {telemetryError}</p>
        ) : telemetryLoading && !telemetry ? (
          <p className="api-data-card__message" role="status">Loading latest telemetry…</p>
        ) : telemetry?.status === "unavailable" ? (
          <p className="api-data-card__message" role="status">{telemetry.message}</p>
        ) : telemetry?.record ? (
          <div className="traffic-telemetry__content">
            <div className="traffic-telemetry__meta">
              <span><strong>Record source:</strong> {telemetry.record.source}</span>
          <span><strong>Scenario:</strong> {telemetry.record.scenario}</span>
              <span><strong>Interface:</strong> {telemetry.record.interface}</span>
              <span><strong>Timestamp:</strong> {telemetry.record.timestamp}</span>
              <span><strong>Age:</strong> {telemetry.measurement_age_seconds === null ? "Unavailable" : `${telemetry.measurement_age_seconds.toFixed(1)} seconds`}</span>
              <span><strong>File source:</strong> {telemetry.source}</span>
            </div>
            <div className="traffic-telemetry__metrics" aria-label={`${telemetryStatusLabel(telemetry.status)} measurements`}>
              <span>Throughput <strong>{metric(telemetry.record.throughput_mbps, "Mbps")}</strong></span>
              <span>Latency <strong>{metric(telemetry.record.latency_ms, "ms")}</strong></span>
              <span>Jitter <strong>{metric(telemetry.record.jitter_ms, "ms")}</strong></span>
              <span>Packet loss <strong>{metric(telemetry.record.packet_loss_percent, "%")}</strong></span>
              <span>Queue size <strong>{metric(telemetry.record.queue_size, "packets")}</strong></span>
              <span>Queue drops <strong>{metric(telemetry.record.queue_drops, "packets")}</strong></span>
            </div>
            {telemetry.status === "stale" && <p className="api-data-card__message" role="status">These are stale recorded measurements, not current traffic values. {telemetry.message}</p>}
          </div>
        ) : (
          <p className="api-data-card__message" role="status">No validated telemetry record is available.</p>
        )}
        <p className="traffic-telemetry__refresh" aria-live="polite">
          Refreshes every 5 seconds{telemetryCheckedAt ? ` · Last checked ${new Date(telemetryCheckedAt).toLocaleTimeString()}` : ""}
        </p>
      </section>

      <div className="traffic-layout">
        <section className="card traffic-form">
          <div className="section-heading">
            <div><h2>Configure a scenario</h2><p>Values are local form state until reviewed.</p></div>
            <span className="section-icon"><FlaskConical size={18} /></span>
          </div>
          <label className="field-label" htmlFor="scenario">Traffic scenario</label>
          <select id="scenario" value={scenario} onChange={(event) => updateScenario(event.target.value as Scenario)}>
            {Object.entries(SCENARIOS).map(([value, option]) => (
              <option key={value} value={value}>{option.label}</option>
            ))}
          </select>
          <p className="field-help">{scenarioDetails.description}</p>
          <p className="field-help">Client selection sets the source host used by Start. Preview remains local; traffic starts only after you press Start and confirm.</p>

          <fieldset className="flow-picker">
            <legend className="field-label">Client selection</legend>
            <div className="flow-choice-grid">
              {scenario === "congestion" && <button
                type="button"
                className={`flow-choice ${selectedFlow === "all" ? "flow-choice--active" : ""}`}
                onClick={() => { setSelectedFlow("all"); setPreviewed(false); }}
                aria-pressed={selectedFlow === "all"}
              >
                <strong>All flows</strong><span>h1-h4 · UDP 5201-5204</span>
              </button>}
              {CLIENT_FLOWS.map((flow) => (
                <button
                  type="button"
                  key={flow.id}
                  className={`flow-choice ${selectedFlow === flow.id ? "flow-choice--active" : ""}`}
                  onClick={() => { setSelectedFlow(flow.id); setPreviewed(false); }}
                  aria-pressed={selectedFlow === flow.id}
                >
                  <strong>{flow.id} · {flow.service}</strong><span>UDP {flow.port}</span>
                </button>
              ))}
            </div>
          </fieldset>
          <p className="field-help" role="note" aria-label="Scenario execution flow contract">{executionPlanHelp}</p>

          <div className="form-row">
            <label>
              <span className="field-label">Protocol</span>
              <input value="UDP" readOnly aria-label="Protocol UDP, fixed by supported runner" />
            </label>
            <label>
              <span className="field-label">Target rate / flow</span>
              <span className="input-with-unit"><input type="number" min="0.1" max="10" step="0.1" value={rate} disabled={scenario === "burst"} onChange={(event) => { setRate(Number(event.target.value)); setPreviewed(false); }} /><span>Mbps</span></span>
            </label>
            <label>
              <span className="field-label">Duration</span>
              <span className="input-with-unit"><input type="number" min="1" max="60" step="1" value={duration} disabled={scenario === "burst"} onChange={(event) => { setDuration(Number(event.target.value)); setPreviewed(false); }} /><span>sec</span></span>
            </label>
          </div>
          {validationReason && <p className="field-help" role="status">{validationReason}</p>}

          {error && <div className="inline-error" role="alert"><AlertCircle size={16} />{error}</div>}
          <div className="button-row">
            <button className="button button--primary" type="button" onClick={previewConfiguration}>
              <Check size={16} /> Preview configuration
            </button>
            <button className={`button ${canStart ? "button--primary" : "button--disabled"}`} type="button" aria-label="Start traffic" disabled={!canStart} onClick={() => void startExecution()} title={validationReason || (!apiConnected ? "Connect to the backend before starting traffic" : undefined)}>
              <Play size={16} /> {requestPending === "start" ? "Starting…" : "Start traffic"}
            </button>
            <button className="button button--secondary" type="button" disabled={!execution?.can_stop || requestPending !== null || execution.status === "stopping"} onClick={() => void stopExecution()}>
              <CircleStop size={16} /> {requestPending === "stop" ? "Stopping…" : "Stop"}
            </button>
          </div>
          <div className="execution-notice" role="note" aria-label="Local preview information">
            <ShieldAlert size={15} aria-hidden="true" />
            <div>
              <strong>Preview and execution</strong>
              <span>Preview stays in your browser and never launches traffic. Start executes the selected source and planned flow(s) only after confirmation. Limits: 0.1–10 Mbps per flow and 1–60 seconds; Burst is fixed at 8 Mbps and 23 seconds total.</span>
            </div>
          </div>
        </section>

        <aside className="card traffic-summary">
          <div className="section-heading">
            <div><h2>Traffic execution</h2><p>Execution status comes from the guarded process supervisor. Telemetry remains a separate measurement stream.</p></div>
          </div>
          {executionLoading && <p className="api-data-card__message" role="status">Checking for an active guarded experiment…</p>}
          {executionError && <div className="inline-error" role="alert"><AlertCircle size={16} />{executionError}<button type="button" className="text-button" onClick={() => void refreshExecution()}><RefreshCw size={13} /> Retry status check</button></div>}
          {!executionLoading && !execution && !executionError && <p className="api-data-card__message" role="status">No guarded traffic experiment has been started.</p>}
          {execution && <div className={`execution-state execution-state--${execution.status}`} role="status">
            <strong>{execution.status[0].toUpperCase() + execution.status.slice(1)}</strong>
            <span>{SCENARIOS[execution.scenario].label} · API-reported source: {execution.source_host === null ? "Not recorded" : execution.source_host === "all" ? "All flows" : execution.source_host} · Actual flows: {execution.flows.join(", ")} · {execution.target_rate_mbps} Mbps per flow · {execution.duration_seconds}s</span>
            <span>Elapsed {execution.elapsed_seconds.toFixed(1)}s{execution.process_exit_code !== null ? ` · Process exit ${execution.process_exit_code}` : ""}</span>
            {!TERMINAL_STATES.has(execution.status) && (
              <div className="execution-progress">
                <label htmlFor="traffic-execution-progress">Process elapsed time</label>
                <progress id="traffic-execution-progress" max={100} value={progressPercent} />
                <small>{Math.min(execution.elapsed_seconds, execution.duration_seconds).toFixed(1)} / {execution.duration_seconds}s · reported by the execution API</small>
              </div>
            )}
            {execution.error && <span className="execution-state__error">{execution.error}</span>}
            {executionError && <span className="execution-state__error">Last status refresh failed. Showing the last confirmed process state.</span>}
            {execution.status === "completed" && (
              <span>Process completed. This API does not link measured telemetry to the execution, so no execution-specific metrics are inferred here.</span>
            )}
          </div>}
          <div className="flow-status-list">
            {CLIENT_FLOWS.map((flow) => {
              const inActualExecution = execution?.flows.includes(flow.id) ?? false;
              const inPlan = execution ? inActualExecution : plannedFlows.some((planned) => planned.id === flow.id);
              const flowStatus = execution && inActualExecution
                ? execution.status
                : inPlan ? execution ? "Planned" : selectedFlow === flow.id ? "Selected · planned" : "Planned"
                  : execution ? "Not in execution" : "Not planned";
              const flowDetail = execution && inActualExecution
                ? execution.status === "failed" ? "Execution failed; per-flow result is unavailable" : `API execution elapsed ${execution.elapsed_seconds.toFixed(1)}s`
                : inPlan ? "Included in the current Start plan" : execution ? "Not included in the API-reported execution" : "Outside the current plan";
              return <div className="flow-status-row" key={flow.id}>
                <span className="flow-status-marker" />
                <div><strong>{flow.id} · {flow.service}</strong><small>UDP destination port {flow.port}</small></div>
                <span className="flow-run-state"><span className={`status-pill ${inPlan ? "status-pill--surface" : "status-pill--muted"}`}>{flowStatus}</span><small>{flowDetail}</small></span>
              </div>
            })}
          </div>
          {previewed ? (
            <div className="preview-result" role="status">
              <div className="preview-result__title"><Check size={16} /> Local configuration preview</div>
              <p>{SCENARIOS[scenario].label} · selected source {selectedFlow === "all" ? "All flows" : selectedSourceHost} · planned: {plannedFlows.map((flow) => flow.id).join(", ")} · {rate} Mbps each · {duration}s</p>
              <small>This preview does not launch traffic. Start uses this plan after confirmation.</small>
            </div>
          ) : (
            <div className="summary-empty"><span>01</span><p>Select options, then preview the local configuration.</p></div>
          )}
          <div className="traffic-config-note">
            <strong>Backend-supported defaults</strong>
            <span>Congestion: 4 concurrent UDP flows · 4 Mbps each · 20 seconds.</span>
          </div>
        </aside>
      </div>
    </div>
  );
}
