# CN Project demonstration guide

The dashboard can inspect measurements from a recent successfully completed
Traffic Lab experiment. Running that experiment sends Mininet-emulated UDP
traffic, so it is an operator action, not a read-only step. No step in this
guide applies QoS or changes `tc` state; production QoS application remains
disabled.

## Startup and preconditions

1. Confirm the expected Mininet topology is already running. The dashboard API
   does not start or repair Mininet. If an operator has authorized topology
   startup and Mininet is not running, start it once from the `cn_project-demo`
   root in WSL with:

   ```sh
   sudo mn --custom network/topology/basic_topology.py --topo adaptive
   ```

   This starts Mininet and configures the lab topology; it is not read-only.
   Never run it when the topology is already running, and do not restart
   Mininet or OVS as part of a dashboard inspection.
2. Start the API in a PowerShell terminal from the `cn_project-demo` project
   root:

   ```powershell
   python -m uvicorn dashboard.backend.api:app --host 127.0.0.1 --port 8000
   ```

   Keep it bound to loopback. Set `ADAPTIVE_QOS_REPORT_DIR` only if genuine
   offline reports are to be loaded; no report is needed for live rule-based
   classification. If a previous API process is already using port 8000,
   stop/restart only that dashboard API using the operator's normal process
   controls so it loads the current source. Do not restart Mininet or OVS.
3. From a separate PowerShell terminal, start the frontend from
   `cn_project-integration\frontend`:

   ```powershell
   npm run dev
   ```

   Open the local Vite URL (normally `http://127.0.0.1:5173`) and verify that
   the API is reachable at `http://127.0.0.1:8000/api/health`. The isolated
   status route should return `status: unavailable` and
   `mutation_enabled: false`; this is expected because no isolated owner
   service is provisioned.

4. On AI & QoS, check the installed-state section independently. A kernel
   readback may show the currently verified LOW profile, or it may show
   Unavailable if the Mininet process/interface cannot be verified. Do not
   infer installed state from the proposal. Execution must say
   **Dry Run — Not Applied** and no Apply button should be present.

## Demo sequence

1. In **Traffic Lab**, select **Congestion** and **All flows**. The default
   plan is four concurrent UDP flows at 4 Mbps each for 20 seconds from h1-h4
   to server ports 5201-5204. Preview the configuration, then explicitly
   press **Start traffic** and confirm the browser prompt. This sends real
   emulated traffic only after backend topology preflight; it is not a preview
   or QoS apply operation. Do not start a second run while one is active.
2. Wait for Traffic Lab to report **Completed** with process exit code 0.
   Completed-run telemetry is accepted only after process completion and
   successful exit. Verify the run ID/source host/ports and completed state in
   the execution panel. The experiment's recorded telemetry is not proof that
   the network is currently sending traffic.
3. Open **AI & QoS** and select scenario **congestion**. Select each host
   (`h1`, `h2`, `h3`, `h4`) in turn. For each one, confirm that selected
   telemetry identifies that exact host and scenario and that the age is
   under the displayed freshness limit (currently 300 seconds). Wait for the
   decision refresh after each selection. The host-specific endpoint filters
   before choosing the newest valid completed-run record; it must not fall
   back to another host.
4. In **Selected telemetry**, review the validated source, run ID (when available),
   timestamp, age, and freshness state. Throughput, utilization, latency,
   jitter, packet loss, queue drops, active flows, and queue size are shown
   only when the matching measurement is available.
5. In **Traffic Classification**, explain the existing rule-based score using
   the displayed utilization, latency, and packet-loss thresholds and the
   per-metric rationale. This classification is not an offline model
   prediction.
6. In **Proposed QoS Profile**, review the selected LOW/MEDIUM/HIGH proposal,
   service-to-UDP-port/class mappings, configured rates and ceilings, filter
   priorities, default/unmatched class, netem settings, and generated
   commands. The commands are shown for inspection and are not executed;
   configured rates are targets, not achieved throughput.
7. Compare the proposal with **Currently installed QoS**. That section reports
   the separate, timestamped kernel readback for `s1-eth5`, including observed
   qdiscs, classes/rates/ceilings, filters, and netem leaves. A verified
   installed profile is not evidence that the newly proposed profile was
   applied.
8. Confirm **Execution status** says **Dry Run — Not Applied**. There is no
   Apply action while exclusive ownership of `s1-eth5` against external `tc`
   writers is unproven. Enabling application requires an independently
   reviewed and tested ownership architecture; a cooperative lock or a
   successful readback is not that proof.
9. Explain **Offline Model Prediction** separately. It remains unavailable
   until a genuine supported runner JSON report is imported or loaded from an
   explicitly configured report directory. An empty report directory does not
   disable live telemetry classification.

## Expected states and troubleshooting

- **Fresh telemetry:** matching selected host/scenario from the completed run,
  age at most 300 seconds, and a rule-based classification/proposal when the
  required measurements are present. The selected host may not have a complete
  valid record even if the aggregate experiment completed successfully.
- **Stale telemetry:** the page shows stale age/message and creates no
  classification or profile proposal. A four-flow run takes time to finish and
  some per-host interval records may be older than others. If a selected host's
  newest valid measurement is older than 300 seconds, explicitly run a new
  approved Traffic Lab congestion experiment and select that host again. Do
  not alter timestamps, reuse historical sample data, or loosen freshness
  validation to make the screen appear fresh.
- **Unavailable/mismatched telemetry:** verify the selected filters against
  the endpoint contract and check the displayed API error/message. Another
  host's record must never be used as a fallback.
- **Missing offline report:** expected when the report directory is not
  configured or no runner report is loaded. Import a real supported runner
  output if model output is needed; do not create a substitute result.
- **Unavailable installed-state readback:** the page reports Unavailable and
  shows the backend error. Do not infer an installed profile from a proposal
  or old UI state.
- **Unrecognized installed hierarchy:** the page reports Verification Failed
  for managed-profile recognition and shows whatever read-only kernel details
  were returned. Do not claim a recognized profile.

The AI & QoS page refreshes decision data every five seconds and installed
kernel status every fifteen seconds. Repeated decision polling of an unchanged
telemetry record does not count as a new hysteresis sample.

The Traffic Lab and AI & QoS use different views: Traffic Lab displays the
latest general telemetry record, while AI & QoS filters by selected host and
scenario. This is expected. It is possible for Traffic Lab to show a fresh
record while one selected host in AI & QoS has no fresh matching record.
