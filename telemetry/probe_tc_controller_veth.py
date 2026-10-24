#!/usr/bin/env python3
"""Exercise the real tc controller on a fresh, probe-owned veth pair only.

This is an opt-in Linux kernel probe. It refuses existing interface names and
never targets Mininet's s1-eth5/s2-eth1 interfaces.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qos import tc_controller as controller  # noqa: E402


PORTS = controller.FLOW_PORTS
RATES = {port: 2.5 for port in PORTS}  # isolated probe fixture only
PRIORITIES = {port: 10 + index for index, port in enumerate(PORTS)}
DEFAULT_PORT = 5204


class Probe:
    def __init__(self, output: Path):
        self.output = output
        token = uuid.uuid4().hex[:8]
        self.target = f"qosp{token}"
        self.peer = f"qosq{token}"
        self.log_path = output / "commands.jsonl"
        self.steps = []
        self.creation_attempted = False
        self.created = False
        self.created_interfaces = set()
        self.original_state = {}
        self.target_mutable = False
        self.target_expected = None
        self.peer_mutable = False
        self.peer_expected = None

    def run(self, argv, *, stage, execute=True, override=None):
        if execute:
            proc = subprocess.run(argv, capture_output=True, text=True, check=False)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        else:
            rc, stdout, stderr = override
        if rc == 0 and argv[:3] == ["ip", "link", "add"]:
            self.created = True
            self.created_interfaces.update((self.target, self.peer))
        elif rc == 0 and argv[:4] == ["tc", "qdisc", "add", "dev"]:
            if len(argv) > 4 and argv[4] == self.target and "root" in argv:
                self.target_mutable = True
            elif len(argv) > 4 and argv[4] == self.peer and "root" in argv:
                self.peer_mutable = True
        row = {"stage": stage, "argv": list(argv), "exit_code": rc,
               "stdout": stdout, "stderr": stderr}
        self.steps.append(row)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(row, allow_nan=False) + "\n")
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    def checked(self, argv, stage):
        result = self.run(argv, stage=stage)
        if result.returncode:
            raise RuntimeError(f"{stage}: {' '.join(argv)} failed: {result.stderr.strip()}")
        return result.stdout

    def controller_runner(self, failure=None, hide_rollback_verification=False):
        state = {"injected": False, "rollback_done": False, "hidden": False}

        def runner(argv, capture_output, text, check):
            command = " ".join(argv)
            if failure and not state["injected"] and failure in command:
                state["injected"] = True
                return self.run(
                    argv, stage="injected-command-failure", execute=False,
                    override=(2, "", "injected probe failure"),
                )
            result = self.run(argv, stage="controller")
            if "qdisc replace" in command and "handle 10:" in command:
                state["rollback_done"] = True
            if (
                hide_rollback_verification and state["rollback_done"]
                and not state["hidden"] and argv[:3] == ["tc", "-d", "qdisc"]
            ):
                state["hidden"] = True
                return self.run(
                    argv, stage="injected-rollback-verification-mismatch",
                    execute=False,
                    override=(0, result.stdout + "qdisc ingress ffff: parent ffff:fff1\n", ""),
                )
            return result

        return runner, state

    def snapshot(self, label, interface):
        data = {}
        for key, argv in (
            ("qdiscs", ["tc", "-d", "qdisc", "show", "dev", interface]),
            ("classes", ["tc", "-d", "class", "show", "dev", interface]),
            ("classes_nested", ["tc", "-d", "class", "show", "dev", interface, "parent", "1:"]),
            ("filters", ["tc", "-s", "filter", "show", "dev", interface]),
            ("filters_nested", ["tc", "-s", "filter", "show", "dev", interface, "parent", "1:"]),
        ):
            result = self.run(argv, stage=f"snapshot-{label}")
            if result.returncode:
                raise RuntimeError(f"cannot capture {label} {key} for {interface}")
            data[key] = result.stdout
        path = self.output / f"{label}-{interface}.json"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        # Match inspect_tc_state's complete root plus nested-parent readback.
        return {
            "qdiscs": data["qdiscs"],
            "classes": data["classes"] + data["classes_nested"],
            "filters": data["filters"] + data["filters_nested"],
        }

    def verify_pair(self):
        infos = []
        for interface in (self.target, self.peer):
            raw = self.checked(
                ["ip", "-j", "-details", "link", "show", "dev", interface],
                "verify-veth-details",
            )
            records = json.loads(raw)
            if (
                not isinstance(records, list)
                or len(records) != 1
                or not isinstance(records[0], dict)
                or records[0].get("ifname") != interface
                or not isinstance(records[0].get("linkinfo"), dict)
                or records[0]["linkinfo"].get("info_kind") != "veth"
            ):
                raise RuntimeError(f"{interface} is not veth")
            if records[0].get("master"):
                raise RuntimeError(f"{interface} is attached to a master")
            infos.append(records[0])
            addresses = json.loads(
                self.checked(
                    ["ip", "-j", "addr", "show", "dev", interface],
                    "verify-addresses",
                )
            )
            if (
                not isinstance(addresses, list)
                or len(addresses) != 1
                or not isinstance(addresses[0], dict)
                or addresses[0].get("ifname") != interface
            ):
                raise RuntimeError(f"cannot verify addresses for {interface}")
            address_info = addresses[0].get("addr_info")
            if not isinstance(address_info, list) or any(
                not isinstance(address, dict) for address in address_info
            ):
                raise RuntimeError(f"cannot verify addresses for {interface}")
            global_addresses = [
                address for address in address_info
                if address.get("scope") != "link"
            ]
            if global_addresses:
                raise RuntimeError(f"{interface} unexpectedly has network addresses")
            bridges = self.checked(["ovs-vsctl", "list-br"], "verify-no-ovs-bridge")
            for bridge in bridges.splitlines():
                ports = self.checked(
                    ["ovs-vsctl", "list-ports", bridge], "verify-not-ovs-port"
                )
                if interface in ports.splitlines():
                    raise RuntimeError(f"{interface} unexpectedly belongs to OVS bridge {bridge}")
        if infos[0].get("link") != self.peer or infos[1].get("link") != self.target:
            raise RuntimeError("veth endpoints do not point to each other")
        self.checked(["test", "!", "-e", f"/sys/class/net/{self.target}/master"], "verify-no-master-path")
        self.checked(["test", "!", "-e", f"/sys/class/net/{self.peer}/master"], "verify-no-peer-master-path")

    def create(self):
        if self.target in {"s1-eth5", "s2-eth1"} or self.peer in {"s1-eth5", "s2-eth1"}:
            raise RuntimeError("refusing Mininet bottleneck interface name")
        existing_names = self._list_link_names("preflight-name-absence")
        for interface in (self.target, self.peer):
            if interface in existing_names:
                raise RuntimeError(f"interface {interface} already exists")
        self.creation_attempted = True
        self.checked(["ip", "link", "add", "name", self.target,
                      "type", "veth", "peer", "name", self.peer], "create-disposable-veth")
        self.created = True
        initial = {}
        for name in (self.target, self.peer):
            initial[name] = self.snapshot("original", name)
            self.original_state[name] = initial[name]
        (self.output / "original-state.json").write_text(json.dumps(initial, indent=2) + "\n", encoding="utf-8")
        for name, state in initial.items():
            if state["classes"].strip() or state["filters"].strip():
                raise RuntimeError(f"fresh interface {name} unexpectedly has classes or filters")
        return initial

    def install_peer_unrelated(self):
        commands = (
            ["tc", "qdisc", "add", "dev", self.peer, "root", "handle", "6:", "htb", "default", "1"],
            ["tc", "class", "add", "dev", self.peer, "parent", "6:", "classid", "6:1", "htb", "rate", "100mbit", "ceil", "100mbit"],
            ["tc", "filter", "add", "dev", self.peer, "protocol", "ip", "parent", "6:", "prio", "500", "u32", "match", "ip", "dport", "9", "0xffff", "flowid", "6:1"],
        )
        for index, command in enumerate(commands):
            self.checked(command, "install-unrelated-peer-object")
            if index == 0:
                self.peer_mutable = True
            self.peer_expected = self.snapshot(
                f"unrelated-peer-step-{index + 1}", self.peer
            )
        return self.peer_expected

    def install_baseline(self):
        commands = (
            ["tc", "qdisc", "add", "dev", self.target, "root", "handle", "5:", "htb", "default", "1"],
            ["tc", "class", "add", "dev", self.target, "parent", "5:", "classid", "5:1", "htb", "rate", "10mbit", "ceil", "10mbit"],
            ["tc", "qdisc", "add", "dev", self.target, "parent", "5:1", "handle", "10:", "netem", "limit", "1000", "delay", "10ms"],
        )
        for index, command in enumerate(commands):
            self.checked(command, "install-probe-baseline")
            if index == 0:
                self.target_mutable = True
            self.target_expected = self.snapshot(
                f"controller-baseline-step-{index + 1}", self.target
            )
        return self.snapshot("controller-baseline", self.target)

    def controller_apply(self, runner):
        return controller.apply_tc_policy(
            self.target, 10, RATES, PRIORITIES, DEFAULT_PORT,
            runner=runner, allow_disposable_veth=True,
        )

    def _list_link_names(self, stage):
        result = self.run(
            ["ip", "-j", "link", "show"],
            stage=stage,
        )
        if result.returncode:
            raise RuntimeError("cannot verify probe interface presence")
        try:
            links = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("cannot parse probe interface listing") from exc
        if (
            not isinstance(links, list)
            or any(not isinstance(link, dict) or not isinstance(link.get("ifname"), str)
                   for link in links)
        ):
            raise RuntimeError("probe interface listing is malformed")
        names = [link["ifname"] for link in links]
        if len(names) != len(set(names)):
            raise RuntimeError("probe interface listing contains duplicate names")
        return set(names)

    def cleanup(self, original=None):
        if not self.creation_attempted and not self.created:
            return {"verified": True, "interfaces_removed": []}
        existing_names = self._list_link_names("cleanup-check-interfaces")
        presence = {
            name: name in existing_names for name in (self.target, self.peer)
        }
        if not any(presence.values()):
            self.created = False
            self.created_interfaces.clear()
            return {"verified": True, "interfaces_removed": []}
        if not all(presence.values()):
            raise RuntimeError(
                "cleanup stopped: cannot verify ownership of an incomplete veth pair"
            )
        self.verify_pair()
        original = original or self.original_state
        if set(original) != {self.target, self.peer}:
            if self.target_mutable or self.peer_mutable:
                raise RuntimeError(
                    "cleanup stopped: original interface state is incomplete after mutation"
                )
            current = {name: self.snapshot("cleanup-unmodified", name)
                       for name in (self.target, self.peer)}
            if any(state["classes"].strip() or state["filters"].strip()
                   for state in current.values()):
                raise RuntimeError(
                    "cleanup stopped: untracked classes or filters exist on probe pair"
                )
            self.checked(
                ["ip", "link", "del", "dev", self.target],
                "remove-probe-veth-pair",
            )
            self.created = False
            return {"verified": True, "interfaces_removed": [self.target, self.peer]}
        current = self.snapshot("cleanup-current-target", self.target)
        peer_initial = self.snapshot("cleanup-current-peer", self.peer)
        if current == original[self.target] and peer_initial == original[self.peer]:
            self.checked(["ip", "link", "del", "dev", self.target], "remove-probe-veth-pair")
            self.created = False
            self.created_interfaces.clear()
            for interface in (self.target, self.peer):
                absent = self.run(["ip", "-j", "link", "show", "dev", interface], stage="verify-interface-removed")
                if absent.returncode == 0:
                    raise RuntimeError(f"probe interface {interface} remains after deletion")
            return {"verified": True, "interfaces_removed": [self.target, self.peer], "restored_state": original}
        # Only known probe states are eligible for cleanup. Unexpected objects
        # stop cleanup so unrelated state is never broadly removed.
        baseline = controller._baseline_matches(controller.TCState(**current))
        managed = controller._managed_structure_matches(
            controller.TCState(**current), PRIORITIES, DEFAULT_PORT
        )
        if not baseline:
            if not managed:
                raise RuntimeError("cleanup stopped: target is not baseline or recognized controller state")
            controller.restore_original_hierarchy(
                self.target, PRIORITIES, DEFAULT_PORT,
                runner=self.run_adapter("cleanup-controller-restore"),
                allow_disposable_veth=True,
            )
        if self.peer_mutable:
            peer_now = self.snapshot("cleanup-current-peer", self.peer)
            if self.peer_expected is None or peer_now != self.peer_expected:
                raise RuntimeError("cleanup stopped: unrelated peer objects changed unexpectedly")
            self.checked(["tc", "qdisc", "del", "dev", self.peer, "root"], "remove-probe-peer-root")
            self.peer_mutable = False
        if self.target_mutable:
            if (
                not baseline
                and not managed
                and (self.target_expected is None or current != self.target_expected)
            ):
                raise RuntimeError(
                    "cleanup stopped: target state is not a verified probe-created state"
                )
            self.checked(["tc", "qdisc", "del", "dev", self.target, "root"], "remove-probe-target-root")
            self.target_mutable = False
        restored = {name: self.snapshot("restored-original", name) for name in (self.target, self.peer)}
        if restored != original:
            raise RuntimeError("cleanup stopped: original fresh-veth state did not verify")
        self.checked(["ip", "link", "del", "dev", self.target], "remove-probe-veth-pair")
        self.created = False
        self.created_interfaces.clear()
        for interface in (self.target, self.peer):
            absent = self.run(["ip", "-j", "link", "show", "dev", interface], stage="verify-interface-removed")
            if absent.returncode == 0:
                raise RuntimeError(f"probe interface {interface} remains after deletion")
        return {"verified": True, "interfaces_removed": [self.target, self.peer], "restored_state": restored}

    def run_adapter(self, stage):
        def adapter(argv, capture_output, text, check):
            return self.run(argv, stage=stage)
        return adapter


def main():
    if os.geteuid() != 0:
        raise SystemExit("run as root inside the Linux environment")
    if len(sys.argv) != 2:
        raise SystemExit("usage: probe_tc_controller_veth.py NEW_EMPTY_OUTPUT_DIRECTORY")
    output = Path(sys.argv[1]).resolve()
    if output.exists():
        raise SystemExit(f"refusing existing evidence directory: {output}")
    output.mkdir(parents=True, exist_ok=False)
    probe = Probe(output)
    report = {"status": "started", "live_mininet_interfaces_touched": False,
              "controller_target": probe.target, "veth_peer": probe.peer,
              "probe_only_allocations_mbps": RATES, "filter_priorities": PRIORITIES,
              "default_port": DEFAULT_PORT, "steps": []}
    original = None
    try:
        original = probe.create()
        probe.verify_pair()
        probe.peer_expected = probe.install_peer_unrelated()
        baseline = probe.install_baseline()
        (output / "controller-baseline.json").write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

        runner, _ = probe.controller_runner()
        applied = probe.controller_apply(runner)
        managed = probe.snapshot("after-apply", probe.target)
        if not controller._managed_matches(controller.TCState(**managed), RATES, PRIORITIES, DEFAULT_PORT):
            raise RuntimeError("actual kernel state did not pass controller managed-state verifier")
        if "qdisc htb 5: root" not in managed["qdiscs"] or "rate 10Mbit ceil 10Mbit" not in managed["classes"]:
            raise RuntimeError("existing root/cap was not retained")
        (output / "after-apply.json").write_text(json.dumps({"commands_returned": len(applied), "state": managed}, indent=2) + "\n", encoding="utf-8")

        repeated, _ = probe.controller_runner()
        repeat_result = probe.controller_apply(repeated)
        repeated_state = probe.snapshot("after-identical-reapply", probe.target)
        if repeat_result or repeated_state != managed:
            raise RuntimeError("identical reapply was not a verified no-op")
        peer_after_apply = probe.snapshot("unrelated-peer-after-apply", probe.peer)
        if peer_after_apply != probe.peer_expected:
            raise RuntimeError("controller changed unrelated peer qdisc/class/filter objects")

        restore_result = controller.restore_original_hierarchy(
            probe.target, PRIORITIES, DEFAULT_PORT,
            runner=probe.run_adapter("controller-explicit-restore"),
            allow_disposable_veth=True,
        )
        restored = probe.snapshot("after-explicit-restore", probe.target)
        if not restore_result or restored != baseline:
            raise RuntimeError("actual controller cleanup did not restore baseline exactly")

        failing, fail_state = probe.controller_runner(failure="class add")
        try:
            probe.controller_apply(failing)
            raise RuntimeError("injected mutation failure unexpectedly passed")
        except controller.TCApplyError as exc:
            report["injected_failure_message"] = str(exc)
        rollback_state = probe.snapshot("after-injected-failure-rollback", probe.target)
        if not fail_state["injected"] or rollback_state != baseline:
            raise RuntimeError("injected failure rollback did not restore exact baseline")
        report["partial_failure_rollback_verified"] = True

        # Inject a false-negative only into the controller's rollback readback;
        # an independent kernel read immediately afterward must still equal the
        # saved baseline before ordinary probe cleanup proceeds.
        failing_verify, verify_state = probe.controller_runner(
            failure="class add", hide_rollback_verification=True
        )
        try:
            probe.controller_apply(failing_verify)
            raise RuntimeError("rollback-verification fault unexpectedly passed")
        except controller.TCApplyError as exc:
            report["rollback_verification_failure_message"] = str(exc)
        independent = probe.snapshot("independent-after-verification-fault", probe.target)
        if not verify_state["injected"] or not verify_state["hidden"] or independent != baseline:
            raise RuntimeError("rollback verification fault was not reported or kernel baseline differs")
        report["rollback_verification_fault_reported"] = True
        report["independent_kernel_state_after_fault_matches_baseline"] = True

        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if probe.creation_attempted or probe.created:
            try:
                report["cleanup"] = probe.cleanup(original)
            except Exception as exc:
                report["cleanup"] = {"verified": False, "error": f"{type(exc).__name__}: {exc}",
                                     "remaining_target": probe.target if probe.created else None,
                                     "remaining_peer": probe.peer if probe.created else None}
                report["status"] = "cleanup-failed"
        report["steps"] = [{"stage": row["stage"], "argv": row["argv"],
                            "exit_code": row["exit_code"]} for row in probe.steps]
        (output / "result.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0 if report.get("status") == "passed" and report.get("cleanup", {}).get("verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
