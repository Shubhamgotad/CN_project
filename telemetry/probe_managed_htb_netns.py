#!/usr/bin/env python3
"""Opt-in kernel probe for managed HTB inside two disposable network namespaces.

This probe does not create Mininet/OVS objects. Its only network changes are a
fresh veth pair and TC state inside two uniquely named, identity-checked netns.
It refuses the demo interface and leaves namespaces in place if safe restore or
identity verification fails.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qos import tc_controller as controller  # noqa: E402
from qos.allocator import MANAGED_HTB_PROFILES  # noqa: E402


def _run(argv: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode and not allow_failure:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(argv)}: "
            f"{result.stderr.strip()}"
        )
    return result


def _namespace_inode(name: str) -> int:
    text = _run([
        "ip", "netns", "exec", name, "stat", "-Lc", "%i", "/proc/self/ns/net",
    ]).stdout.strip()
    if not text.isdigit():
        raise RuntimeError(f"namespace {name} returned invalid identity")
    return int(text)


def _link_record(namespace: str, interface: str) -> dict:
    value = json.loads(_run([
        "ip", "netns", "exec", namespace, "ip", "-j", "-details", "link",
        "show", "dev", interface,
    ]).stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError(f"cannot positively identify {interface} in {namespace}")
    return value[0]


def _managed_snapshot(target: controller.IsolatedHTBTarget) -> dict:
    scoped = controller._IsolatedHTBRunner(target, subprocess.run)
    state = controller.inspect_tc_state(target.interface, scoped)
    return {"qdiscs": state.qdiscs, "classes": state.classes, "filters": state.filters}


def _filter_port_successes(filters: str) -> dict[int, int]:
    """Read kernel u32 destination-match counters, keyed by UDP destination port."""
    result = {}
    for line in filters.splitlines():
        match = re.search(
            r"match 0000([0-9a-fA-F]{4})/0000ffff at 20 \(success (\d+) ?\)",
            line.strip(),
        )
        if match:
            result[int(match.group(1), 16)] = int(match.group(2))
    return result


def _demo_snapshot() -> dict:
    # Read-only structural snapshot. Never issue a tc command against this link.
    result = {}
    for name, argv in (
        ("qdiscs", ["tc", "-d", "qdisc", "show", "dev", "s1-eth5"]),
        ("classes", ["tc", "-d", "class", "show", "dev", "s1-eth5"]),
        ("filters", ["tc", "-d", "filter", "show", "dev", "s1-eth5"]),
    ):
        result[name] = _run(argv).stdout
    return result


class Probe:
    def __init__(self, output: Path):
        self.output = output
        token = uuid.uuid4().hex[:8]
        self.ns_a = f"qosna{token}"
        self.ns_b = f"qosnb{token}"
        self.target_if = f"qosp{token}"
        self.peer_if = f"qosq{token}"
        self.target: controller.IsolatedHTBTarget | None = None
        self.configs = {}
        self.namespaces_created: list[tuple[str, int]] = []
        self.pair_created = False
        self.created_ifindices: dict[str, int] = {}
        self.baseline_created = False
        self.tc_command_log: list[list[str]] = []
        self.post_install_readback: dict | None = None

    def create(self) -> None:
        existing_ns = {line.split()[0] for line in _run(["ip", "netns", "list"]).stdout.splitlines() if line.strip()}
        existing_links = {
            item["ifname"] for item in json.loads(_run(["ip", "-j", "link", "show"]).stdout)
        }
        if {self.ns_a, self.ns_b} & existing_ns:
            raise RuntimeError("generated namespace name already exists; refusing reuse")
        if {self.target_if, self.peer_if} & existing_links:
            raise RuntimeError("generated interface name already exists; refusing reuse")

        _run(["ip", "netns", "add", self.ns_a])
        self.namespaces_created.append((self.ns_a, _namespace_inode(self.ns_a)))
        _run(["ip", "netns", "add", self.ns_b])
        self.namespaces_created.append((self.ns_b, _namespace_inode(self.ns_b)))
        _run([
            "ip", "link", "add", "name", self.target_if, "type", "veth",
            "peer", "name", self.peer_if,
        ])
        self.pair_created = True
        created_links = json.loads(_run([
            "ip", "-j", "-details", "link", "show",
        ]).stdout)
        for row in created_links:
            if row.get("ifname") in {self.target_if, self.peer_if}:
                self.created_ifindices[row["ifname"]] = row["ifindex"]
        if set(self.created_ifindices) != {self.target_if, self.peer_if}:
            raise RuntimeError("cannot record ownership of the freshly created veth pair")
        _run(["ip", "link", "set", "dev", self.target_if, "netns", self.ns_a])
        _run(["ip", "link", "set", "dev", self.peer_if, "netns", self.ns_b])
        for namespace, interface, address in (
            (self.ns_a, self.target_if, "198.18.0.1/30"),
            (self.ns_b, self.peer_if, "198.18.0.2/30"),
        ):
            _run(["ip", "-n", namespace, "link", "set", "lo", "up"])
            _run(["ip", "-n", namespace, "addr", "add", address, "dev", interface])
            _run(["ip", "-n", namespace, "link", "set", "dev", interface, "up"])

        target_link = _link_record(self.ns_a, self.target_if)
        peer_link = _link_record(self.ns_b, self.peer_if)
        self.target = controller.IsolatedHTBTarget(
            namespace=self.ns_a,
            interface=self.target_if,
            namespace_inode=self.namespaces_created[0][1],
            ifindex=target_link["ifindex"],
            peer_namespace=self.ns_b,
            peer_interface=self.peer_if,
            peer_namespace_inode=self.namespaces_created[1][1],
            peer_ifindex=peer_link["ifindex"],
        )
        controller._verify_isolated_htb_target(self.target, subprocess.run)

        # This baseline is installed only after the two-netns identity guard passes.
        for argv in (
            ["tc", "qdisc", "add", "dev", self.target_if, "root", "handle", "5:", "htb", "default", "1"],
            ["tc", "class", "add", "dev", self.target_if, "parent", "5:", "classid", "5:1", "htb", "rate", "10mbit", "ceil", "10mbit"],
            ["tc", "qdisc", "add", "dev", self.target_if, "parent", "5:1", "handle", "10:", "netem", "limit", "1000", "delay", "10ms"],
        ):
            self._scoped(argv)
        self.baseline_created = True
        state = controller.inspect_tc_state(
            self.target_if, controller._IsolatedHTBRunner(self.target, subprocess.run)
        )
        if not controller._baseline_matches(state):
            raise RuntimeError("disposable kernel interface did not match the required baseline")

    def _scoped(self, argv: list[str]) -> subprocess.CompletedProcess:
        assert self.target is not None
        controller._verify_isolated_htb_target(self.target, subprocess.run)
        if argv[0] != "tc" or "s1-eth5" in argv:
            raise RuntimeError("probe refused an unscoped or demo-interface TC operation")
        scoped = ["ip", "netns", "exec", self.ns_a, *argv]
        self.tc_command_log.append(scoped)
        return _run(scoped)

    def run_profiles(self) -> dict:
        assert self.target is not None
        report = {"profiles": {}, "traffic_classification": {}, "failure_rollback": {}}
        for name, allocations in MANAGED_HTB_PROFILES.items():
            config = controller.ManagedHTBConfig(
                interface=self.target.interface,
                total_bandwidth_mbps=10.0,
                service_allocations_mbps=allocations,
                filter_priorities=controller.MANAGED_FILTER_PRIORITIES,
                isolated_target=self.target,
            )
            self.configs[name] = config
            try:
                first_apply = controller.apply_managed_htb_policy(
                    config, runner=self._recording_runner
                )
            except Exception as exc:
                actual = _managed_snapshot(self.target)
                raise RuntimeError(
                    f"managed profile {name} failed: {exc}; actual kernel readback="
                    f"{json.dumps(self.post_install_readback or actual, sort_keys=True)}"
                ) from exc
            state = controller.TCState(**_managed_snapshot(self.target))
            if not controller._managed_htb_matches(state, config):
                raise RuntimeError(f"kernel readback did not verify profile {name}")
            before_repeat = _managed_snapshot(self.target)
            repeated = controller.apply_managed_htb_policy(
                config, runner=self._recording_runner
            )
            after_repeat = _managed_snapshot(self.target)
            if repeated or before_repeat != after_repeat:
                raise RuntimeError(f"profile {name} reapplication was not an exact no-op")

            _run([
                "ip", "netns", "exec", self.ns_a, "ping", "-c", "1", "-W", "2",
                "198.18.0.2",
            ])
            before_state = controller.TCState(**_managed_snapshot(self.target))
            before_success = _filter_port_successes(before_state.filters)
            send_script = (
                "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "[s.sendto(b'isolated-qos-probe', ('198.18.0.2', p)) "
                "for p in (5201,5202,5203,5204) for _ in range(8)]; s.close()"
            )
            _run(["ip", "netns", "exec", self.ns_a, "python3", "-c", send_script])
            time.sleep(0.05)
            after_state = controller.TCState(**_managed_snapshot(self.target))
            after_success = _filter_port_successes(after_state.filters)
            expected = {5201: "1:11", 5202: "1:12", 5203: "1:13", 5204: "1:14"}
            increments = {
                str(port): after_success.get(port, 0) - before_success.get(port, 0)
                for port, class_id in expected.items()
            }
            if any(value <= 0 for value in increments.values()):
                raise RuntimeError(
                    f"UDP destination-port classification failed: {increments}; "
                    f"before={before_success}; after={after_success}; "
                    f"classes={after_state.classes}; filters={after_state.filters}"
                )
            report["profiles"][name] = {
                "first_apply_commands": len(first_apply),
                "reapply_commands": len(repeated),
                "service_allocations_mbps": dict(allocations),
                "unmatched_mbps": 1.0,
                "total_mbps": sum(allocations.values()) + 1.0,
                "kernel_state": {"qdiscs": after_state.qdiscs, "classes": after_state.classes,
                                 "filters": after_state.filters},
            }
            report["traffic_classification"][name] = increments

        # Failure during initial installation must restore the exact baseline.
        self._restore_current_profile()
        baseline = controller.TCState(**_managed_snapshot(self.target))
        if not controller._baseline_matches(baseline):
            raise RuntimeError("could not return disposable interface to baseline before failure test")
        high = self.configs["HIGH"]
        injected = {"done": False}

        def fail_one(argv, **kwargs):
            command = " ".join(argv)
            if not injected["done"] and "class add" in command and "classid 1:13" in command:
                injected["done"] = True
                self.tc_command_log.append(list(argv))
                return SimpleNamespace(returncode=2, stdout="", stderr="injected isolated probe failure")
            return self._recording_runner(argv, **kwargs)

        try:
            controller.apply_managed_htb_policy(high, runner=fail_one)
            raise RuntimeError("injected class creation failure unexpectedly succeeded")
        except controller.TCApplyError as exc:
            if "original hierarchy restored and verified" not in str(exc):
                raise
            report["failure_rollback"]["error"] = str(exc)
        failed_state = controller.TCState(**_managed_snapshot(self.target))
        if not injected["done"] or not controller._baseline_matches(failed_state):
            raise RuntimeError("injected failure did not restore verified baseline")
        report["failure_rollback"]["baseline_restored"] = True

        # ICMP reaches only the paired disposable namespace.
        ping = _run([
            "ip", "netns", "exec", self.ns_a, "ping", "-c", "2", "-W", "2",
            "198.18.0.2",
        ])
        report["connectivity"] = {"command": "isolated namespace ping 198.18.0.2", "output": ping.stdout}
        self._restore_current_profile()
        return report

    def _recording_runner(self, argv, **kwargs):
        if argv[:4] == ["ip", "netns", "exec", self.ns_a] and len(argv) > 4 and argv[4] == "tc":
            self.tc_command_log.append(list(argv))
        result = subprocess.run(argv, **kwargs)
        if (
            result.returncode == 0
            and argv[:4] == ["ip", "netns", "exec", self.ns_a]
            and argv[4:6] == ["tc", "filter"]
            and "5204" in argv
        ):
            self.post_install_readback = _managed_snapshot(self.target)
        return result

    def _restore_current_profile(self) -> None:
        assert self.target is not None
        # Only the verified target object can select this isolated restore path.
        controller.restore_isolated_managed_htb_hierarchy(
            self.target, runner=self._recording_runner
        )

    def cleanup(self) -> dict:
        if not self.namespaces_created:
            return {"verified": True, "removed": []}
        if self.target is not None:
            controller._verify_isolated_htb_target(self.target, subprocess.run)
            state = controller.TCState(**_managed_snapshot(self.target))
            if controller._managed_htb_owned(state):
                controller.restore_isolated_managed_htb_hierarchy(
                    self.target, runner=self._recording_runner
                )
                state = controller.TCState(**_managed_snapshot(self.target))
            if self.baseline_created and not controller._baseline_matches(state):
                raise RuntimeError("cleanup refused: disposable TC state is not verified baseline")
            self._scoped(["tc", "qdisc", "del", "dev", self.target_if, "root"])
        removed = []
        for namespace, expected_inode in reversed(self.namespaces_created):
            if _namespace_inode(namespace) != expected_inode:
                raise RuntimeError(f"cleanup refused: namespace identity changed for {namespace}")
            _run(["ip", "netns", "del", namespace])
            removed.append(namespace)
        remaining = json.loads(_run(["ip", "-j", "-details", "link", "show"]).stdout)
        for row in remaining:
            name = row.get("ifname")
            if name not in self.created_ifindices:
                continue
            if (
                row.get("ifindex") != self.created_ifindices[name]
                or not isinstance(row.get("linkinfo"), dict)
                or row["linkinfo"].get("info_kind") != "veth"
            ):
                raise RuntimeError(f"cleanup refused: leftover {name} no longer matches the created veth")
            _run(["ip", "link", "del", "dev", name])
        remaining_names = {
            row.get("ifname") for row in json.loads(_run(["ip", "-j", "link", "show"]).stdout)
        }
        if remaining_names & set(self.created_ifindices):
            raise RuntimeError("probe-owned veth endpoint remains after cleanup")
        return {"verified": True, "removed": removed}


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("run as root inside WSL/Linux")
    if len(sys.argv) != 2:
        raise SystemExit("usage: probe_managed_htb_netns.py NEW_EMPTY_OUTPUT_DIRECTORY")
    output = Path(sys.argv[1]).resolve()
    if output.exists():
        raise SystemExit(f"refusing existing evidence directory: {output}")
    output.mkdir(parents=True, exist_ok=False)
    probe = Probe(output)
    report = {
        "status": "started",
        "isolation": "two fresh network namespaces joined only by a probe-owned veth pair",
        "demo_interface_mutated": False,
        "demo_tc_before": None,
        "demo_tc_after": None,
        "steps": [],
    }
    try:
        report["demo_tc_before"] = _demo_snapshot()
        probe.create()
        report["target_identity"] = probe.target.__dict__
        report.update(probe.run_profiles())
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["tc_commands"] = probe.tc_command_log
        try:
            report["demo_tc_after"] = _demo_snapshot()
            report["demo_tc_unchanged"] = report["demo_tc_before"] == report["demo_tc_after"]
            if not report["demo_tc_unchanged"]:
                report["status"] = "failed"
                report["error"] = "demo s1-eth5 TC structure changed during isolated probe"
        except Exception as exc:
            report["demo_tc_after_error"] = f"{type(exc).__name__}: {exc}"
            report["status"] = "failed"
        if any(
            command[:4] != ["ip", "netns", "exec", probe.ns_a]
            or "s1-eth5" in command
            for command in probe.tc_command_log
        ):
            report["status"] = "failed"
            report["error"] = "TC command log contains an unscoped or demo-interface command"
        try:
            report["cleanup"] = probe.cleanup()
        except Exception as exc:
            report["cleanup"] = {"verified": False, "error": f"{type(exc).__name__}: {exc}"}
            report["status"] = "cleanup-failed"
        (output / "result.json").write_text(
            json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        print(json.dumps({
            "status": report["status"],
            "evidence": str(output / "result.json"),
            "cleanup": report.get("cleanup"),
            "demo_tc_unchanged": report.get("demo_tc_unchanged"),
            "error": report.get("error"),
        }, indent=2))
    return 0 if report.get("status") == "passed" and report.get("cleanup", {}).get("verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
