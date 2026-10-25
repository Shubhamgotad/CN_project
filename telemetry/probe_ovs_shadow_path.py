#!/usr/bin/env python3
"""Opt-in OVS-connected QoS path probe for an approved disposable Linux VM."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qos import tc_controller as controller  # noqa: E402
from qos.allocator import MANAGED_HTB_PROFILES  # noqa: E402

APPROVAL_ENV = "ADAPTIVE_QOS_SHADOW_APPROVAL"
APPROVAL_VALUE = "I_APPROVE_DISPOSABLE_VM_NETWORK_CHANGES"
APPROVAL_VERSION = 1
PRODUCTION_INTERFACES = {"s1-eth5", "s2-eth1"}


def validate_approval(
    approval: Any,
    *,
    environment_value: str | None,
    explicit_opt_in: bool,
    output_directory: Path,
    repository_root: Path = ROOT,
) -> str:
    """Validate explicit operator consent and refuse unsafe output locations."""
    if environment_value != APPROVAL_VALUE or not explicit_opt_in:
        raise ValueError("explicit disposable-VM opt-in is required")
    if not isinstance(approval, dict):
        raise ValueError("approval file must contain a JSON object")
    if (
        approval.get("version") != APPROVAL_VERSION
        or approval.get("target_kind") != "disposable_linux_vm"
        or approval.get("allow_network_mutations") is not True
        or approval.get("dedicated_ovs_database") is not True
    ):
        raise ValueError(
            "approval file must attest an approved disposable VM and dedicated OVS database"
        )
    target_id = approval.get("target_id")
    if not isinstance(target_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", target_id):
        raise ValueError("approval file must include a valid disposable target_id")
    instance_uuid = normalize_ovs_uuid(approval.get("ovs_instance_uuid"))
    if instance_uuid is None:
        raise ValueError("approval file must include the dedicated OVS instance UUID")
    output = output_directory.resolve()
    root = repository_root.resolve()
    if output == root or root in output.parents:
        raise ValueError("evidence output must be outside the repository")
    if output.exists():
        raise ValueError("refusing an existing evidence directory")
    return target_id


def normalize_ovs_uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip('"')
    candidate = re.sub(r"^UUID:\s*", "", candidate)
    candidate = candidate.strip("{}")
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        return None


def validate_ovs_instance(
    approval: Any,
    *,
    db_socket: Path,
    management_directory: Path,
    actual_uuid: str,
) -> str:
    if not isinstance(approval, dict):
        raise ValueError("approval file must contain a JSON object")
    expected_uuid = normalize_ovs_uuid(approval.get("ovs_instance_uuid"))
    observed_uuid = normalize_ovs_uuid(actual_uuid)
    if approval.get("dedicated_ovs_database") is not True or expected_uuid is None:
        raise ValueError("approval does not attest a dedicated OVS database instance")
    if approval.get("ovs_db_socket") != str(db_socket.resolve()):
        raise ValueError("approval OVS database socket does not match the selected database")
    if approval.get("ovs_ofctl_dir") != str(management_directory.resolve()):
        raise ValueError("approval management directory does not match the selected directory")
    if observed_uuid is None or observed_uuid != expected_uuid:
        raise ValueError("OVS database UUID does not match the approved dedicated instance")
    return observed_uuid


def validate_ovs_port_graph(
    graph: dict[str, set[str]],
    *,
    left_bridge: str,
    right_bridge: str,
    expected_left_ports: set[str],
    expected_right_ports: set[str],
) -> None:
    """Require exactly the approved two-bridge, four-edge shadow path."""
    if set(graph) != {left_bridge, right_bridge}:
        raise ValueError("OVS contains unexpected or missing shadow bridges")
    if graph[left_bridge] != expected_left_ports:
        raise ValueError("left shadow bridge has an unexpected or bypass port")
    if graph[right_bridge] != expected_right_ports:
        raise ValueError("right shadow bridge has an unexpected or bypass port")
    if graph[left_bridge] & graph[right_bridge]:
        raise ValueError("shadow bridge ports overlap")
    if any(name in PRODUCTION_INTERFACES for ports in graph.values() for name in ports):
        raise ValueError("shadow topology references a production interface")


def validate_root_shadow_veth(
    record: Any,
    *,
    interface: str,
    peer_interface: str,
    bridge: str,
    ifindex: int,
    peer_ifindex: int,
) -> None:
    if (
        not isinstance(record, dict)
        or record.get("ifname") != interface
        or record.get("ifindex") != ifindex
        or record.get("link") != peer_interface
        or record.get("link_index") != peer_ifindex
        or record.get("master") != bridge
        or "UP" not in record.get("flags", [])
        or not isinstance(record.get("linkinfo"), dict)
        or record["linkinfo"].get("info_kind") != "veth"
    ):
        raise RuntimeError(
            f"shadow root veth {interface} has an unexpected identity or attachment"
        )


def parse_ovs_port_counters(output: str) -> dict[int, dict[str, int]]:
    counters = {}
    current_port = None
    for line in output.splitlines():
        port_match = re.match(r"^\s*port\s+(\d+):", line)
        if port_match:
            current_port = int(port_match.group(1))
            counters[current_port] = {}
        if current_port is None:
            continue
        rx_match = re.search(r"\brx pkts=(\d+)", line)
        tx_match = re.search(r"\btx pkts=(\d+)", line)
        if rx_match:
            counters[current_port]["rx_packets"] = int(rx_match.group(1))
        if tx_match:
            counters[current_port]["tx_packets"] = int(tx_match.group(1))
    return counters


def build_ovs_ofctl_argv(endpoint: Path, *arguments: str) -> list[str]:
    if not arguments:
        raise ValueError("an ovs-ofctl operation is required")
    return [
        "ovs-ofctl", "-O", "OpenFlow13", arguments[0],
        f"unix:{endpoint}", *arguments[1:],
    ]


def validate_openflow_path(output: str, expected_pairs: set[tuple[int, int]]) -> None:
    actual = set()
    for line in output.splitlines():
        if " actions=" not in line:
            continue
        match = re.search(
            r"priority=100,in_port=(\d+)\s+actions=output:(\d+)\s*$",
            line,
        )
        if not match:
            raise ValueError("OpenFlow rules contain an unexpected forwarding action")
        actual.add((int(match.group(1)), int(match.group(2))))
    if actual != expected_pairs:
        raise ValueError("OpenFlow rules do not match the single approved shadow path")


def require_external_ownership_proof(proof: Any) -> None:
    """Never treat topology identity checks as proof against privileged writers."""
    if (
        not isinstance(proof, dict)
        or proof.get("boundary") != "separate_administrative_domain"
        or proof.get("external_privileged_access_denied") is not True
        or proof.get("reviewed_by")
        is None
    ):
        raise ValueError("external privileged-writer ownership proof is missing")


class ShadowProbe:
    def __init__(
        self,
        output: Path,
        ovs_db: Path,
        ovs_ofctl_dir: Path,
        approval: dict[str, Any],
    ):
        self.output = output
        self.ovs_db = ovs_db
        self.ovs_ofctl_dir = ovs_ofctl_dir
        self.approval = approval
        self.token = uuid.uuid4().hex[:8]
        self.namespaces = {
            name: f"qos{name}{self.token}" for name in ("c", "o", "s")
        }
        self.bridges = {
            name: f"qos-{name}-{self.token}" for name in ("left", "right")
        }
        self.pairs = {
            "client": (f"qocl{self.token}", f"qoch{self.token}", "c"),
            "left_owner": (f"qosl{self.token}", f"qosi{self.token}", "o"),
            "owner_right": (f"qosr{self.token}", f"qosp{self.token}", "o"),
            "server": (f"qosv{self.token}", f"qoss{self.token}", "s"),
        }
        self.namespace_inodes: dict[str, int] = {}
        self.root_ifindices: dict[str, int] = {}
        self.root_peer_ifindices: dict[str, int] = {}
        self.created_bridges: set[str] = set()
        self.bridge_ports: dict[str, set[str]] = {}
        self.target: controller.ShadowHTBTarget | None = None
        self.tc_log: list[list[str]] = []
        self.ofports: dict[str, int] = {}
        self.expected_flows: dict[str, set[tuple[int, int]]] = {}
        self.steps_path = output / "commands.jsonl"

    def run(
        self,
        argv: list[str],
        *,
        allow_failure: bool = False,
        record: bool = True,
    ):
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if record:
            row = {
                "argv": argv,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            with self.steps_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, allow_nan=False) + "\n")
        if result.returncode and not allow_failure:
            raise RuntimeError(
                f"command failed ({result.returncode}): {' '.join(argv)}: "
                f"{result.stderr.strip()}"
            )
        return result

    def ns(self, namespace: str, *argv: str):
        return self.run(["ip", "netns", "exec", namespace, *argv])

    def ovs_vsctl(self, *argv: str, record: bool = True):
        return self.run([
            "ovs-vsctl", f"--db=unix:{self.ovs_db}", *argv,
        ], record=record)

    def ovs_ofctl(self, bridge: str, *argv: str):
        endpoint = self.ovs_ofctl_dir / f"{bridge}.mgmt"
        if not endpoint.is_socket():
            raise RuntimeError(f"dedicated OVS management socket is unavailable: {endpoint}")
        return self.run(build_ovs_ofctl_argv(endpoint, *argv))

    def namespace_inode(self, namespace: str) -> int:
        text = self.ns(namespace, "stat", "-Lc", "%i", "/proc/self/ns/net").stdout.strip()
        if not text.isdecimal():
            raise RuntimeError(f"cannot verify namespace identity for {namespace}")
        return int(text)

    def link_record(self, namespace: str, interface: str) -> dict[str, Any]:
        payload = json.loads(self.ns(
            namespace, "ip", "-j", "-details", "link", "show", "dev", interface
        ).stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise RuntimeError(f"cannot verify {interface} in {namespace}")
        return payload[0]

    def root_link_record(self, interface: str) -> dict[str, Any]:
        payload = json.loads(self.run(
            ["ip", "-j", "-details", "link", "show", "dev", interface]
        ).stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise RuntimeError(f"cannot verify root-side interface {interface}")
        return payload[0]

    def target_manifest(self, target_id: str) -> dict[str, Any]:
        return {
            "target_id": target_id,
            "namespaces": self.namespaces,
            "bridges": self.bridges,
            "interfaces": {
                key: {"root": pair[0], "namespace": pair[1], "namespace_key": pair[2]}
                for key, pair in self.pairs.items()
            },
            "managed_target": {
                "namespace": self.namespaces["o"],
                "egress_interface": self.pairs["owner_right"][1],
                "ingress_interface": self.pairs["left_owner"][1],
                "identity_indices": "captured only after disposable resources are created",
            },
            "production_target_mutated": False,
            "ownership_proof": "not_proven",
        }

    def preflight(self, target_id: str) -> dict[str, Any]:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise RuntimeError("the approved probe requires root on Linux")
        required = ("ip", "tc", "ovs-vsctl", "ovs-ofctl", "python3", "stat")
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise RuntimeError(f"required executable(s) unavailable: {', '.join(missing)}")
        if not self.ovs_db.is_socket():
            raise RuntimeError("the approved dedicated OVS database socket is unavailable")
        if (
            self.ovs_ofctl_dir.is_symlink()
            or not self.ovs_ofctl_dir.is_dir()
            or self.ovs_ofctl_dir.resolve() != self.ovs_ofctl_dir
        ):
            raise RuntimeError("the dedicated OVS management directory is unavailable or ambiguous")
        if list(self.ovs_ofctl_dir.glob("*.mgmt")):
            raise RuntimeError(
                "dedicated OVS management directory contains unexpected bridge sockets"
            )
        bridges = {
            item for item in self.ovs_vsctl("--timeout=3", "list-br", record=False).stdout.splitlines()
            if item
        }
        if bridges:
            raise RuntimeError("refusing OVS instance with existing bridges; use an empty shadow VM")
        actual_uuid = self.ovs_vsctl(
            "--timeout=3", "get", "Open_vSwitch", ".", "_uuid", record=False
        ).stdout.strip()
        ovs_uuid = validate_ovs_instance(
            self.approval,
            db_socket=self.ovs_db,
            management_directory=self.ovs_ofctl_dir,
            actual_uuid=actual_uuid,
        )
        namespaces = {
            line.split()[0]
            for line in self.run(["ip", "netns", "list"], record=False).stdout.splitlines()
            if line.strip()
        }
        if namespaces & set(self.namespaces.values()):
            raise RuntimeError("generated namespace name already exists")
        links = json.loads(
            self.run(["ip", "-j", "link", "show"], record=False).stdout
        )
        existing = {row.get("ifname") for row in links if isinstance(row, dict)}
        generated = {name for pair in self.pairs.values() for name in pair[:2]}
        if generated & existing or generated & PRODUCTION_INTERFACES:
            raise RuntimeError("generated interface name conflicts with existing or production interfaces")
        bridge_names = set(self.bridges.values())
        if bridge_names & bridges:
            raise RuntimeError("generated bridge name unexpectedly exists in the approved OVS database")
        return {
            "status": "preflight_passed",
            "ovs_instance_uuid": ovs_uuid,
            "ovs_database_dedicated": "operator_attested; UUID and socket match verified",
            "resources_created": False,
            "network_mutations": False,
            **self.target_manifest(target_id),
        }

    def create(self) -> None:
        for namespace in self.namespaces.values():
            self.run(["ip", "netns", "add", namespace])
            self.namespace_inodes[namespace] = self.namespace_inode(namespace)

        for root_if, ns_if, namespace in self.pairs.values():
            self.run([
                "ip", "link", "add", "name", root_if, "type", "veth", "peer",
                "name", ns_if,
            ])
            root_link = self.root_link_record(root_if)
            self.root_ifindices[root_if] = root_link["ifindex"]
            self.root_peer_ifindices[root_if] = root_link["link_index"]
            self.run(["ip", "link", "set", "dev", ns_if, "netns", namespace])

        for name, namespace in self.namespaces.items():
            self.ns(namespace, "ip", "link", "set", "lo", "up")

        addresses = {
            "c": (("qoch" + self.token, "198.18.1.2/24"),),
            "o": (
                ("qosi" + self.token, "198.18.1.1/24"),
                ("qosp" + self.token, "198.18.2.1/24"),
            ),
            "s": (("qoss" + self.token, "198.18.2.2/24"),),
        }
        for key, entries in addresses.items():
            namespace = self.namespaces[key]
            for interface, address in entries:
                self.ns(namespace, "ip", "addr", "add", address, "dev", interface)
                self.ns(namespace, "ip", "link", "set", "dev", interface, "up")

        self.ns(self.namespaces["o"], "sysctl", "-w", "net.ipv4.ip_forward=1")
        self.ns(self.namespaces["c"], "ip", "route", "add", "default", "via", "198.18.1.1")
        self.ns(self.namespaces["s"], "ip", "route", "add", "default", "via", "198.18.2.1")

        for bridge in self.bridges.values():
            self.ovs_vsctl("--timeout=3", "add-br", bridge)
            self.created_bridges.add(bridge)
            self.ovs_vsctl(
                "set", "Bridge", bridge,
                f"external_ids:adaptive_qos_shadow_probe={self.token}",
                "fail_mode=secure",
            )
            if not (self.ovs_ofctl_dir / f"{bridge}.mgmt").is_socket():
                raise RuntimeError(
                    "the selected OVS database is not served by the dedicated "
                    "ovs-vswitchd runtime directory"
                )

        self._add_port(self.bridges["left"], self.pairs["client"][0])
        self._add_port(self.bridges["left"], self.pairs["left_owner"][0])
        self._add_port(self.bridges["right"], self.pairs["owner_right"][0])
        self._add_port(self.bridges["right"], self.pairs["server"][0])
        self.verify_topology()
        self._install_openflow_path()
        self.target = self._make_target()
        controller._verify_shadow_htb_target(self.target, subprocess.run)
        self._install_baseline()

    def _add_port(self, bridge: str, interface: str) -> None:
        self.ovs_vsctl("--timeout=3", "add-port", bridge, interface)
        self.bridge_ports.setdefault(bridge, set()).add(interface)
        self.run(["ip", "link", "set", "dev", interface, "up"])

    def _port_number(self, interface: str) -> int:
        value = self.ovs_vsctl(
            "--timeout=3", "get", "Interface", interface, "ofport"
        ).stdout.strip()
        if not value.isdecimal() or int(value) <= 0:
            raise RuntimeError(f"OVS did not assign a usable OpenFlow port to {interface}")
        return int(value)

    def verify_topology(self) -> dict[str, set[str]]:
        graph = {
            bridge: set(self.ovs_vsctl(
                "--timeout=3", "list-ports", bridge
            ).stdout.splitlines())
            for bridge in self.bridges.values()
        }
        validate_ovs_port_graph(
            graph,
            left_bridge=self.bridges["left"],
            right_bridge=self.bridges["right"],
            expected_left_ports={self.pairs["client"][0], self.pairs["left_owner"][0]},
            expected_right_ports={self.pairs["owner_right"][0], self.pairs["server"][0]},
        )
        expected_masters = {
            self.pairs["client"][0]: self.bridges["left"],
            self.pairs["left_owner"][0]: self.bridges["left"],
            self.pairs["owner_right"][0]: self.bridges["right"],
            self.pairs["server"][0]: self.bridges["right"],
        }
        for interface, expected_bridge in expected_masters.items():
            row = self.root_link_record(interface)
            pair = next(pair for pair in self.pairs.values() if pair[0] == interface)
            validate_root_shadow_veth(
                row,
                interface=interface,
                peer_interface=pair[1],
                bridge=expected_bridge,
                ifindex=self.root_ifindices[interface],
                peer_ifindex=self.root_peer_ifindices[interface],
            )
        return graph

    def _install_openflow_path(self) -> None:
        left_client = self._port_number(self.pairs["client"][0])
        left_owner = self._port_number(self.pairs["left_owner"][0])
        right_owner = self._port_number(self.pairs["owner_right"][0])
        right_server = self._port_number(self.pairs["server"][0])
        self.ofports = {
            self.pairs["client"][0]: left_client,
            self.pairs["left_owner"][0]: left_owner,
            self.pairs["owner_right"][0]: right_owner,
            self.pairs["server"][0]: right_server,
        }
        self.expected_flows = {
            self.bridges["left"]: {
                (left_client, left_owner), (left_owner, left_client)
            },
            self.bridges["right"]: {
                (right_owner, right_server), (right_server, right_owner)
            },
        }
        for bridge, incoming, outgoing in (
            (self.bridges["left"], left_client, left_owner),
            (self.bridges["left"], left_owner, left_client),
            (self.bridges["right"], right_owner, right_server),
            (self.bridges["right"], right_server, right_owner),
        ):
            self.ovs_ofctl(
                bridge, "add-flow",
                f"priority=100,in_port={incoming},actions=output:{outgoing}",
            )

    def _make_target(self) -> controller.ShadowHTBTarget:
        owner = self.namespaces["o"]
        target = self.link_record(owner, self.pairs["owner_right"][1])
        ingress = self.link_record(owner, self.pairs["left_owner"][1])
        return controller.ShadowHTBTarget(
            namespace=owner,
            interface=self.pairs["owner_right"][1],
            namespace_inode=self.namespace_inodes[owner],
            ifindex=target["ifindex"],
            ingress_interface=self.pairs["left_owner"][1],
            ingress_ifindex=ingress["ifindex"],
            ingress_peer_interface=self.pairs["left_owner"][0],
            ingress_peer_ifindex=ingress["link_index"],
            egress_peer_interface=self.pairs["owner_right"][0],
            egress_peer_ifindex=target["link_index"],
        )

    def _controller_runner(self, argv, **kwargs):
        if (
            argv[:5]
            == ["ip", "netns", "exec", self.target.namespace, "tc"]
        ):
            self.tc_log.append(list(argv))
        return self.run(list(argv))

    def _tc_scoped(self, argv: list[str]):
        return controller._IsolatedHTBRunner(
            self.target, self._controller_runner
        )(argv, capture_output=True, text=True, check=False)

    def _install_baseline(self) -> None:
        target = self.target
        for command in (
            ["tc", "qdisc", "add", "dev", target.interface, "root", "handle", "5:", "htb", "default", "1"],
            ["tc", "class", "add", "dev", target.interface, "parent", "5:", "classid", "5:1", "htb", "rate", "10mbit", "ceil", "10mbit"],
            ["tc", "qdisc", "add", "dev", target.interface, "parent", "5:1", "handle", "10:", "netem", "limit", "1000", "delay", "10ms"],
        ):
            self._tc_scoped(command)
        state = controller.inspect_tc_state(
            target.interface,
            controller._IsolatedHTBRunner(target, self._controller_runner),
        )
        if not controller._baseline_matches(state):
            raise RuntimeError("shadow egress did not match the expected 10 Mbps/10 ms baseline")

    def _config(self, profile: str):
        return controller.ManagedHTBConfig(
            interface=self.target.interface,
            total_bandwidth_mbps=10.0,
            service_allocations_mbps=MANAGED_HTB_PROFILES[profile],
            filter_priorities=controller.MANAGED_FILTER_PRIORITIES,
            isolated_target=self.target,
        )

    def snapshot_ovs(self) -> dict[str, Any]:
        graph = self.verify_topology()
        evidence = {
            "show": self.ovs_vsctl("--timeout=3", "show").stdout,
            "bridges": {},
        }
        for bridge, ports in graph.items():
            flows = self.ovs_ofctl(bridge, "dump-flows").stdout
            validate_openflow_path(flows, self.expected_flows[bridge])
            evidence["bridges"][bridge] = {
                "ports": sorted(ports),
                "interface_rows": {
                    port: self.ovs_vsctl("list", "Interface", port).stdout
                    for port in sorted(ports)
                },
                "flows": flows,
                "port_counters": self.ovs_ofctl(bridge, "dump-ports").stdout,
            }
        return evidence

    def _verify_packet_counters(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, dict[str, int]]:
        expected = (
            (self.bridges["left"], self.pairs["client"][0], "rx_packets"),
            (self.bridges["left"], self.pairs["left_owner"][0], "tx_packets"),
            (self.bridges["right"], self.pairs["owner_right"][0], "rx_packets"),
            (self.bridges["right"], self.pairs["server"][0], "tx_packets"),
        )
        deltas = {}
        for bridge, interface, direction in expected:
            port = self.ofports[interface]
            old = parse_ovs_port_counters(
                before["bridges"][bridge]["port_counters"]
            ).get(port, {}).get(direction)
            new = parse_ovs_port_counters(
                after["bridges"][bridge]["port_counters"]
            ).get(port, {}).get(direction)
            if old is None or new is None or new <= old:
                raise RuntimeError(
                    f"expected OVS {direction} counter increment on {bridge}/{interface}"
                )
            deltas[f"{bridge}/{interface}/{direction}"] = new - old
        return deltas

    def _packet_roundtrip(self) -> dict[str, Any]:
        server_code = (
            "import json,socket,select,time; "
            "ss=[]; "
            "[(lambda s,p:(s.bind(('0.0.0.0',p)),ss.append((s,p))))"
            "(socket.socket(socket.AF_INET,socket.SOCK_DGRAM),p) "
            "for p in (5201,5202,5203,5204)]; "
            "end=time.time()+8; seen={p:0 for p in (5201,5202,5203,5204)}; "
            "exec('while time.time()<end and sum(seen.values())<40:\\n r,_,_=select.select([s for s,p in ss],[],[],0.2)\\n for s in r:\\n  data,addr=s.recvfrom(2048); seen[s.getsockname()[1]]+=1'); "
            "print(json.dumps(seen))"
        )
        server_log = self.output / "udp-server.json"
        with server_log.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(
                ["ip", "netns", "exec", self.namespaces["s"], "python3", "-u", "-c", server_code],
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
            )
        time.sleep(0.3)
        sender_code = (
            "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
            "[s.sendto(b'shadow-path',('198.18.2.2',p)) "
            "for p in (5201,5202,5203,5204) for _ in range(10)]; s.close()"
        )
        self.ns(self.namespaces["c"], "python3", "-c", sender_code)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=2)
            raise RuntimeError("shadow UDP receiver did not finish")
        if process.returncode != 0:
            raise RuntimeError("shadow UDP receiver exited unsuccessfully")
        try:
            received = json.loads(server_log.read_text(encoding="utf-8").strip())
        except (ValueError, OSError) as exc:
            raise RuntimeError("could not read the shadow UDP receiver results") from exc
        if received != {"5201": 10, "5202": 10, "5203": 10, "5204": 10}:
            raise RuntimeError(f"shadow end-to-end packet delivery failed: {received}")
        return received

    def run_profiles(self) -> dict[str, Any]:
        results = {}
        for profile in MANAGED_HTB_PROFILES:
            config = self._config(profile)
            controller.apply_managed_htb_policy(config, self._controller_runner)
            state = controller.verify_managed_htb_policy(config, self._controller_runner)
            before_ovs = self.snapshot_ovs()
            received = self._packet_roundtrip()
            after_ovs = self.snapshot_ovs()
            results[profile] = {
                "allocations_mbps": dict(MANAGED_HTB_PROFILES[profile]),
                "readback": asdict(state),
                "traffic": received,
                "ovs_before": before_ovs,
                "ovs_after": after_ovs,
                "counter_increments": self._verify_packet_counters(
                    before_ovs, after_ovs
                ),
            }
        controller.restore_isolated_managed_htb_hierarchy(
            self.target, self._controller_runner
        )
        injected = {"seen": False}

        def fail_install_once(argv, **kwargs):
            if (
                not injected["seen"]
                and argv[:5]
                == ["ip", "netns", "exec", self.target.namespace, "tc"]
                and "class" in argv
                and "add" in argv
                and "1:13" in argv
            ):
                injected["seen"] = True
                self.tc_log.append(list(argv))
                return subprocess.CompletedProcess(
                    argv, 2, "", "injected shadow-only controller failure"
                )
            return self._controller_runner(argv, **kwargs)

        try:
            controller.apply_managed_htb_policy(
                self._config("HIGH"), fail_install_once
            )
        except controller.TCApplyError as exc:
            if "original hierarchy restored and verified" not in str(exc):
                raise
            rollback_error = str(exc)
        else:
            raise RuntimeError("injected shadow controller failure unexpectedly succeeded")
        if not injected["seen"]:
            raise RuntimeError("shadow rollback failure injection did not run")
        baseline = controller.inspect_tc_state(
            self.target.interface,
            controller._IsolatedHTBRunner(self.target, self._controller_runner),
        )
        if not controller._baseline_matches(baseline):
            raise RuntimeError("managed profile rollback did not restore the shadow baseline")
        return {
            "profiles": results,
            "rollback": {
                "baseline_restored": True,
                "failure_injected": True,
                "controller_result": rollback_error,
                "readback": asdict(baseline),
            },
            "ownership_proof": {
                "status": "not_proven",
                "reason": "same-VM root and OVS peers are outside this controller's enforceable boundary",
            },
        }

    def cleanup(self) -> dict[str, Any]:
        if self.target is not None:
            controller._verify_shadow_htb_target(self.target, subprocess.run)
            state = controller.inspect_tc_state(
                self.target.interface,
                controller._IsolatedHTBRunner(self.target, self._controller_runner),
            )
            if not (
                controller._baseline_matches(state)
                or controller._managed_htb_owned(state)
            ):
                raise RuntimeError("cleanup refused: shadow target identity or tc state is uncertain")

        for bridge in self.created_bridges:
            token = self.ovs_vsctl(
                "get", "Bridge", bridge, "external_ids:adaptive_qos_shadow_probe"
            ).stdout.strip().strip('"')
            if token != self.token:
                raise RuntimeError(f"cleanup refused: bridge identity changed for {bridge}")
            actual = set(self.ovs_vsctl("--timeout=3", "list-ports", bridge).stdout.splitlines())
            if actual != self.bridge_ports.get(bridge, set()):
                raise RuntimeError(f"cleanup refused: bridge port graph changed for {bridge}")

        for namespace, inode in self.namespace_inodes.items():
            if self.namespace_inode(namespace) != inode:
                raise RuntimeError(f"cleanup refused: namespace identity changed for {namespace}")

        links = json.loads(self.run(["ip", "-j", "-details", "link", "show"]).stdout)
        for interface, ifindex in self.root_ifindices.items():
            row = next((item for item in links if item.get("ifname") == interface), None)
            if row is not None and (
                row.get("ifindex") != ifindex
                or row.get("link_index") != self.root_peer_ifindices[interface]
                or not isinstance(row.get("linkinfo"), dict)
                or row["linkinfo"].get("info_kind") != "veth"
            ):
                raise RuntimeError(f"cleanup refused: root veth identity changed for {interface}")
            expected_master = next(
                (
                    bridge
                    for bridge, ports in self.bridge_ports.items()
                    if interface in ports
                ),
                None,
            )
            if row is not None and row.get("master") != expected_master:
                raise RuntimeError(f"cleanup refused: root veth attachment changed for {interface}")

        if self.target is not None:
            state = controller.inspect_tc_state(
                self.target.interface,
                controller._IsolatedHTBRunner(self.target, self._controller_runner),
            )
            if controller._managed_htb_owned(state):
                controller.restore_isolated_managed_htb_hierarchy(
                    self.target, self._controller_runner
                )
                state = controller.inspect_tc_state(
                    self.target.interface,
                    controller._IsolatedHTBRunner(self.target, self._controller_runner),
                )
            if not controller._baseline_matches(state):
                raise RuntimeError("cleanup refused: shadow target is not verified baseline")
            self._tc_scoped(["tc", "qdisc", "del", "dev", self.target.interface, "root"])

        for bridge in self.created_bridges:
            for interface in self.bridge_ports.get(bridge, set()):
                self.ovs_vsctl("--if-exists", "del-port", bridge, interface)
        for bridge in reversed(tuple(self.created_bridges)):
            token = self.ovs_vsctl("get", "Bridge", bridge, "external_ids:adaptive_qos_shadow_probe").stdout.strip().strip('"')
            if token != self.token:
                raise RuntimeError(f"cleanup refused: bridge identity changed for {bridge}")
            self.ovs_vsctl("--if-exists", "del-br", bridge)

        for namespace, inode in reversed(tuple(self.namespace_inodes.items())):
            if self.namespace_inode(namespace) != inode:
                raise RuntimeError(f"cleanup refused: namespace identity changed for {namespace}")
            self.run(["ip", "netns", "del", namespace])

        remaining = json.loads(self.run(["ip", "-j", "-details", "link", "show"]).stdout)
        for interface, ifindex in self.root_ifindices.items():
            row = next((item for item in remaining if item.get("ifname") == interface), None)
            if row is None:
                continue
            if (
                row.get("ifindex") != ifindex
                or row.get("link_index") != self.root_peer_ifindices[interface]
                or not isinstance(row.get("linkinfo"), dict)
                or row["linkinfo"].get("info_kind") != "veth"
                or row.get("master")
            ):
                raise RuntimeError(f"cleanup refused: root veth identity changed for {interface}")
            self.run(["ip", "link", "del", "dev", interface])
        return {"verified": True, "bridges_removed": sorted(self.created_bridges)}


def _approval_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("approval file must be a regular non-symlink file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError("approval file permissions must be 0600 or stricter")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("approval file must contain a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--approval-file", required=True, type=Path)
    parser.add_argument("--ovs-db", required=True, type=Path)
    parser.add_argument("--ovs-ofctl-dir", required=True, type=Path)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="perform read-only prerequisite and identity checks; create no evidence or network resources",
    )
    parser.add_argument(
        "--i-approve-shadow-only-network-mutations",
        action="store_true",
        help="explicitly approve mutations in the attested disposable VM only",
    )
    args = parser.parse_args(argv)
    output = args.output_directory.resolve()
    try:
        approval = _approval_file(args.approval_file)
        target_id = validate_approval(
            approval,
            environment_value=os.environ.get(APPROVAL_ENV),
            explicit_opt_in=args.i_approve_shadow_only_network_mutations,
            output_directory=output,
        )
        if not args.ovs_db.is_socket():
            raise ValueError("--ovs-db must be the socket of the disposable VM OVS database")
        args.ovs_db = args.ovs_db.resolve(strict=True)
        if (
            args.ovs_ofctl_dir.is_symlink()
            or not args.ovs_ofctl_dir.is_dir()
        ):
            raise ValueError("the dedicated OVS management socket directory is unavailable")
        args.ovs_ofctl_dir = args.ovs_ofctl_dir.resolve(strict=True)
        if approval.get("ovs_db_socket") != str(args.ovs_db):
            raise ValueError("approval OVS database socket does not match --ovs-db")
        if approval.get("ovs_ofctl_dir") != str(args.ovs_ofctl_dir):
            raise ValueError("approval management directory does not match --ovs-ofctl-dir")
        if output.exists():
            raise ValueError("refusing an existing evidence directory")
        probe = ShadowProbe(output, args.ovs_db, args.ovs_ofctl_dir, approval)
        preflight = probe.preflight(target_id)
        if args.preflight_only:
            print(json.dumps(preflight, indent=2))
            return 0
        output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    result: dict[str, Any] = {
        "status": "started",
        "target_id": target_id,
        "topology": "client namespace -> left OVS bridge -> routed owner namespace -> right OVS bridge -> server namespace",
        "production_target_mutated": False,
        "ownership_proof": {"status": "not_proven"},
        "preflight": preflight,
    }
    try:
        probe.preflight(target_id)
        probe.create()
        result["target_identity"] = asdict(probe.target)
        result["ovs_before_traffic"] = probe.snapshot_ovs()
        result.update(probe.run_profiles())
        result["status"] = "passed_path_and_controller_probe_ownership_unproven"
    except Exception as exc:
        result.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        result["tc_commands"] = probe.tc_log
        try:
            result["cleanup"] = probe.cleanup()
        except Exception as exc:
            result["cleanup"] = {
                "verified": False,
                "error": f"{type(exc).__name__}: {exc}",
                "action": "preserve resources and evidence; do not clean up by name",
            }
            result["status"] = "cleanup-failed"
        (output / "result.json").write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
    print(json.dumps({
        "status": result["status"],
        "evidence": str(output / "result.json"),
        "cleanup": result.get("cleanup"),
        "ownership_proof": result["ownership_proof"],
        "error": result.get("error"),
    }, indent=2))
    return 0 if result["status"] == "passed_path_and_controller_probe_ownership_unproven" and result.get("cleanup", {}).get("verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
