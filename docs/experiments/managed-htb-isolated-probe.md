# Managed HTB isolated-kernel probe

This opt-in probe applies the managed five-leaf controller only inside two
fresh Linux network namespaces connected by a probe-owned veth pair. It does not
create Mininet/OVS objects, start traffic on the demo topology, or expose an API
for applying a policy. The ordinary `ManagedHTBConfig` path remains fixed to
`s1-eth5`.

The isolated path requires a server-created `IsolatedHTBTarget` containing
matched probe-only names (`qosna<8 hex>` / `qosnb<8 hex>` and `qosp<8 hex>` /
`qosq<8 hex>`), both namespace inode identities, and both interface indices.
Before every scoped `tc` command the controller rechecks those namespace and
veth identities, requires each namespace to contain only loopback and its
unbridged veth endpoint, and executes `tc` through `ip netns exec`. There is no
fallback to `s1-eth5`. The probe refuses pre-existing generated names and leaves
the namespaces in place if restoration or identity verification fails.

Run from WSL as root, using a new evidence-directory name:

```bash
cd /mnt/c/Users/sansk/OneDrive/Desktop/cn1/cn_project-demo
out="/tmp/managed-htb-probe-$(date -u +%Y%m%dT%H%M%SZ)"
python3 telemetry/probe_managed_htb_netns.py "$out"
```

The script records the actual kernel qdisc/class/filter readback for LOW,
MEDIUM, and HIGH, sends UDP datagrams only across the isolated pair to check
per-filter UDP destination-match counter deltas for ports 5201–5204, verifies no-op reapply,
injects a class-creation failure and verifies rollback to baseline, pings only
the peer namespace, restores and removes the disposable namespaces, and records
a read-only structural `s1-eth5` TC snapshot before and after. Its JSON result
is written to `result.json` under the requested new directory. A passing probe
is kernel validation of this isolated veth environment only; it does not prove
the live Mininet switch-port behavior or authorize live policy application.
