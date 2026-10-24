# Adaptive QoS decision dry run

`GET /api/qos/decision` is a read-only proposal endpoint. It calls the existing
validated latest telemetry reader, classifies the canonical measurement with
`ai.prediction.labeling`'s deterministic thresholds, and plans one of the
existing `qos.allocator.MANAGED_HTB_PROFILES` on `s1-eth5`. The route returns
`tc` command text for inspection; it does not call a process runner, the
integration apply function, or any network-control API.

Telemetry must be fresh and include the validated bandwidth, utilization and
active-flow fields needed by the canonical classifier. Missing or invalid
inputs fail closed with no classification or policy. The API keeps the
existing five-minute freshness rule. It does not load a serialized model;
model provenance has not been established for this dry-run path.

The initial valid sample establishes the current dry-run profile. A candidate
profile needs two distinct telemetry records with the same classification.
After that, selection waits until at least 30 seconds have elapsed since the
current profile was selected. The response reports the current proposal,
candidate, qualifying sample count, and remaining cooldown separately.
Repeated records among the last 256 distinct inputs do not advance hysteresis.
The selection state and bounded history live in the API process and reset when
it restarts; neither is evidence of an applied network policy. The response always declares
`dry_run: true` and `applied: false`.

The decision is not a verified live configuration. This feature neither reads
the actual OVS/`tc` state nor establishes that the interface exists. Existing
Traffic Lab start/stop execution remains separate and unchanged.
