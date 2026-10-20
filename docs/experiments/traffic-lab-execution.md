# Guarded Traffic Lab execution parameters

The local Traffic Lab preview does not launch traffic. A confirmed Start request
submits its selected scenario, per-flow rate, and duration to the loopback API;
the API validates them before the WSL supervisor performs its read-only topology
check and launches the existing traffic generator with fixed argv arguments.
Client selection remains a preview-only control. The server chooses each
scenario's supported client hosts and UDP destination ports.

## Supported execution bounds

- Normal: one UDP flow from `h1` to server port 5201. Default is 3 Mbps for 20
  seconds; custom rates from 0.1 through 10 Mbps per flow and durations from 1
  through 60 whole seconds are accepted. Rates use 0.1 Mbps increments.
- Moderate: one UDP flow from `h1` to server port 5201. The documented default
  is 6 Mbps for 20 seconds; it accepts the same bounded custom rate and duration
  values as Normal. This is a traffic-load profile only and applies no QoS policy.
- Congestion: four concurrent UDP flows from `h1`–`h4`, on ports 5201–5204.
  Default is 4 Mbps per flow for 20 seconds (16 Mbps aggregate). The same
  per-flow rate and duration bounds apply. At the 10 Mbps bottleneck, aggregate
  offered rate can exceed link capacity by design; the 10 Mbps per-flow ceiling
  limits this four-flow workload to 40 Mbps aggregate.
- Burst: fixed at 8 Mbps on `h1`, port 5201, for five 3-second bursts separated
  by four 2-second gaps (23 seconds total). Custom rate or duration is rejected.
- Client selection remains preview-only; server-defined hosts and ports above
  determine the launched flows.

The 60-second duration ceiling leaves 30 seconds inside the supervisor's
existing 90-second hard maximum for startup, telemetry finalization, and
orderly cleanup. The supervisor continues to reject a concurrent run and Stop
continues to signal only the verified process group owned by that experiment.
This configuration support does not change topology, OVS, traffic-control, or
QoS settings. It does not change the evidence-analysis comparison gates.
