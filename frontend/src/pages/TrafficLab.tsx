import { AlertCircle, Check, CircleStop, FlaskConical, Play, ShieldAlert } from "lucide-react";
import { useMemo, useState } from "react";
import { CLIENT_FLOWS } from "../data/demo";
import type { FlowId } from "../types";

type Scenario = "normal" | "moderate" | "congestion" | "burst";

const SCENARIOS: Record<Scenario, { label: string; rate: number; duration: number; description: string }> = {
  normal: { label: "Normal", rate: 3, duration: 20, description: "One 3 Mbps UDP flow for 20 seconds." },
  moderate: { label: "Moderate", rate: 6, duration: 20, description: "One configurable-rate UDP flow; default 6 Mbps for 20 seconds." },
  congestion: { label: "Congestion", rate: 4, duration: 20, description: "Four concurrent UDP flows at 4 Mbps each for 20 seconds." },
  burst: { label: "Burst", rate: 8, duration: 3, description: "Five 3-second UDP bursts with 2-second gaps." },
};

export function TrafficLab() {
  const [scenario, setScenario] = useState<Scenario>("congestion");
  const [selectedFlow, setSelectedFlow] = useState<FlowId | "all">("all");
  const [rate, setRate] = useState(4);
  const [duration, setDuration] = useState(20);
  const [previewed, setPreviewed] = useState(false);
  const [error, setError] = useState("");

  const selectedFlows = useMemo(
    () => selectedFlow === "all" ? CLIENT_FLOWS : CLIENT_FLOWS.filter((flow) => flow.id === selectedFlow),
    [selectedFlow],
  );
  const scenarioDetails = SCENARIOS[scenario];

  function updateScenario(value: Scenario) {
    setScenario(value);
    setRate(value === "congestion" ? 4 : SCENARIOS[value].rate);
    setDuration(SCENARIOS[value].duration);
    setPreviewed(false);
  }

  function previewConfiguration() {
    setError("");
    if (!Number.isFinite(rate) || rate <= 0 || rate > 10) {
      setError("Enter a finite rate greater than 0 and no more than the 10 Mbps bottleneck.");
      setPreviewed(false);
      return;
    }
    if (!Number.isInteger(duration) || duration <= 0 || duration > 3600) {
      setError("Enter a whole-number duration between 1 and 3600 seconds.");
      setPreviewed(false);
      return;
    }
    setPreviewed(true);
  }

  return (
    <div className="page-stack">
      <section className="page-intro">
        <div>
          <div className="eyebrow"><span className="eyebrow-mark" /> TRAFFIC CONFIGURATION</div>
          <h1>Traffic Lab</h1>
          <p>Review supported traffic profiles without launching a network process.</p>
        </div>
        <span className="status-pill status-pill--muted"><ShieldAlert size={14} /> Live execution disabled</span>
      </section>

      <div className="safety-callout card">
        <div className="callout-icon"><AlertCircle size={18} /></div>
        <div>
          <strong>Offline preview only</strong>
          <p>This project has no authorized HTTP operation for starting or stopping traffic. The browser never invokes shell commands, Mininet, OVS, tc, or iperf.</p>
        </div>
      </div>

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
          <p className="field-help">Client selection and editable values below affect this local preview only; they do not rewrite or launch the backend preset.</p>

          <fieldset className="flow-picker">
            <legend className="field-label">Client selection</legend>
            <div className="flow-choice-grid">
              <button
                type="button"
                className={`flow-choice ${selectedFlow === "all" ? "flow-choice--active" : ""}`}
                onClick={() => { setSelectedFlow("all"); setPreviewed(false); }}
                aria-pressed={selectedFlow === "all"}
              >
                <strong>All flows</strong><span>h1-h4 · 4 clients</span>
              </button>
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

          <div className="form-row">
            <label>
              <span className="field-label">Protocol</span>
              <input value="UDP" readOnly aria-label="Protocol UDP, fixed by supported runner" />
            </label>
            <label>
              <span className="field-label">Target rate / flow</span>
              <span className="input-with-unit"><input type="number" min="0.1" max="10" step="0.1" value={rate} onChange={(event) => { setRate(Number(event.target.value)); setPreviewed(false); }} /><span>Mbps</span></span>
            </label>
            <label>
              <span className="field-label">Duration</span>
              <span className="input-with-unit"><input type="number" min="1" max="3600" step="1" value={duration} onChange={(event) => { setDuration(Number(event.target.value)); setPreviewed(false); }} /><span>sec</span></span>
            </label>
          </div>

          {error && <div className="inline-error" role="alert"><AlertCircle size={16} />{error}</div>}
          <div className="button-row">
            <button className="button button--primary" type="button" onClick={previewConfiguration}>
              <Check size={16} /> Preview configuration
            </button>
            <button className="button button--disabled" type="button" disabled title="No authorized backend execution operation is available">
              <Play size={16} /> Start traffic
            </button>
            <button className="button button--secondary" type="button" disabled title="Nothing is running from this offline frontend">
              <CircleStop size={16} /> Stop
            </button>
          </div>
          <p className="disabled-reason">Start and Stop are disabled because no guarded live-execution API exists.</p>
        </section>

        <aside className="card traffic-summary">
          <div className="section-heading">
            <div><h2>Flow status</h2><p>No process has been started.</p></div>
          </div>
          <div className="flow-status-list">
            {selectedFlows.map((flow) => (
              <div className="flow-status-row" key={flow.id}>
                <span className="flow-status-marker" />
                <div><strong>{flow.id} · {flow.service}</strong><small>UDP destination port {flow.port}</small></div>
                <span className="flow-run-state"><span className="status-pill status-pill--muted">Not started</span><small>Progress unavailable</small></span>
              </div>
            ))}
          </div>
          {previewed ? (
            <div className="preview-result" role="status">
              <div className="preview-result__title"><Check size={16} /> Local configuration preview</div>
              <p>{SCENARIOS[scenario].label} · {selectedFlows.length} selected flow{selectedFlows.length === 1 ? "" : "s"} · {rate} Mbps each · {duration}s</p>
              <small>This preview does not change network state and may not correspond to a supported CLI preset.</small>
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
