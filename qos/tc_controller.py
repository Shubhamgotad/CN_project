"""Safe command generation and ownership-scoped application for Mininet HTB."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import re
import shlex
import subprocess
import tempfile
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

from .validator import validate_interface, validate_total_bandwidth


ROOT_INTERFACE = "s1-eth5"
ROOT_HANDLE = "5:"
ROOT_CLASS = "5:1"
CHILD_HANDLE = "1:"
CHILD_AGGREGATE_CLASS = "1:1"
BOTTLENECK_MBPS = 10.0
NETEM_DELAY = "10ms"
NETEM_LIMIT = 1000
FLOW_PORTS = (5201, 5202, 5203, 5204)
FLOW_CLASS_IDS = {port: f"1:{minor}" for port, minor in zip(FLOW_PORTS, (11, 12, 13, 14))}
NETEM_HANDLES = {port: f"{110 + index * 10}:" for index, port in enumerate(FLOW_PORTS)}
SERVICE_CLASS_IDS = {
    "gaming": "1:11",
    "video": "1:12",
    "web": "1:13",
    "bulk": "1:14",
}
UNMATCHED_CLASS_ID = "1:15"
UNMATCHED_NETEM_HANDLE = "150:"
MANAGED_FILTER_PRIORITIES = dict(zip(FLOW_PORTS, (10, 20, 30, 40)))
_APPLY_LOCK = threading.RLock()


@contextmanager
def _interface_lock(interface: str):
    """Serialize this controller across threads and cooperating processes."""
    validate_interface(interface)
    lock_path = Path(tempfile.gettempdir()) / f"cn_adaptive_qos_{interface}.lock"
    with _APPLY_LOCK:
        try:
            lock_file = lock_path.open("a+b")
        except OSError as exc:
            raise TCApplyError(f"could not open tc apply lock for {interface}") from exc
        with lock_file:
            lock_kind = None
            try:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    lock_kind = "fcntl"
                elif msvcrt is not None:
                    lock_file.seek(0, 2)
                    if lock_file.tell() == 0:
                        lock_file.write(b"\0")
                        lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                    lock_kind = "msvcrt"
                else:
                    raise TCApplyError(
                        "safe tc application requires cross-process file locking support"
                    )
            except TCApplyError:
                raise
            except (OSError, AttributeError) as exc:
                raise TCApplyError(
                    f"could not obtain cross-process tc apply lock for {interface}"
                ) from exc

            try:
                yield
            finally:
                try:
                    if lock_kind == "fcntl":
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    else:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, AttributeError) as exc:
                    raise TCApplyError(
                        f"could not release cross-process tc apply lock for {interface}"
                    ) from exc


class TCPreconditionError(RuntimeError):
    """The live state does not match the one hierarchy this controller owns."""


class TCApplyError(RuntimeError):
    """An apply or rollback failed; inspect the attached error text and live state."""


@dataclass(frozen=True)
class TCState:
    qdiscs: str
    classes: str
    filters: str


@dataclass(frozen=True)
class IsolatedHTBTarget:
    """Identity-pinned target for the opt-in disposable namespace probe only."""

    namespace: str
    interface: str
    namespace_inode: int
    ifindex: int
    peer_namespace: str
    peer_interface: str
    peer_namespace_inode: int
    peer_ifindex: int

    def __post_init__(self) -> None:
        match = (
            re.fullmatch(r"qosna([0-9a-f]{8})", self.namespace)
            if isinstance(self.namespace, str)
            else None
        )
        if (
            match is None
            or not isinstance(self.peer_namespace, str)
            or not isinstance(self.interface, str)
            or not isinstance(self.peer_interface, str)
            or self.peer_namespace != f"qosnb{match.group(1)}"
            or self.interface != f"qosp{match.group(1)}"
            or self.peer_interface != f"qosq{match.group(1)}"
        ):
            raise ValueError("isolated HTB targets must use one matched probe identity")
        for label, value in (
            ("namespace inode", self.namespace_inode),
            ("interface index", self.ifindex),
            ("peer namespace inode", self.peer_namespace_inode),
            ("peer interface index", self.peer_ifindex),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"isolated target {label} must be a positive integer")
        if self.namespace_inode == self.peer_namespace_inode or self.ifindex == self.peer_ifindex:
            raise ValueError("isolated target endpoints must have distinct identities")


@dataclass(frozen=True)
class ManagedHTBConfig:
    """Validated five-leaf managed hierarchy configuration."""

    interface: str
    total_bandwidth_mbps: float
    service_allocations_mbps: Mapping[str, float]
    filter_priorities: Mapping[int, int]
    unmatched_rate_mbps: float = 1.0
    unmatched_ceil_mbps: float = 1.0
    default_class_id: str = UNMATCHED_CLASS_ID
    isolated_target: IsolatedHTBTarget | None = None

    def __post_init__(self) -> None:
        validate_interface(self.interface)
        if self.isolated_target is None and self.interface != ROOT_INTERFACE:
            raise ValueError(f"managed HTB supports only {ROOT_INTERFACE}")
        if self.isolated_target is not None:
            if not isinstance(self.isolated_target, IsolatedHTBTarget):
                raise ValueError("isolated_target must be an identity-pinned IsolatedHTBTarget")
            if self.interface != self.isolated_target.interface or self.interface == ROOT_INTERFACE:
                raise ValueError("managed HTB interface does not match its isolated target")
        total = validate_total_bandwidth(self.total_bandwidth_mbps)
        if not math.isclose(total, BOTTLENECK_MBPS, rel_tol=0, abs_tol=1e-9):
            raise ValueError("managed HTB parent bandwidth must remain 10 Mbps")
        if not isinstance(self.service_allocations_mbps, Mapping) or set(
            self.service_allocations_mbps
        ) != set(SERVICE_CLASS_IDS):
            raise ValueError("service allocations must cover Gaming, Video, Web, and Bulk")
        rates: dict[str, float] = {}
        for traffic_class in SERVICE_CLASS_IDS:
            rate = self.service_allocations_mbps[traffic_class]
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise ValueError(f"allocation for {traffic_class} must be numeric")
            rate = float(rate)
            if not math.isfinite(rate) or rate <= 0:
                raise ValueError(f"allocation for {traffic_class} must be positive and finite")
            rates[traffic_class] = rate
        unmatched_rate = self._validate_unmatched_rate(
            self.unmatched_rate_mbps, "unmatched rate"
        )
        unmatched_ceil = self._validate_unmatched_rate(
            self.unmatched_ceil_mbps, "unmatched ceil"
        )
        if unmatched_rate != 1.0 or unmatched_ceil != 1.0:
            raise ValueError("unmatched leaf rate and ceil must both be 1 Mbps")
        if not math.isclose(
            sum(rates.values()) + unmatched_rate,
            total,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("service and unmatched allocations must total 10 Mbps")
        if self.default_class_id != UNMATCHED_CLASS_ID:
            raise ValueError("default class must be the unmatched leaf 1:15")
        if (
            not isinstance(self.filter_priorities, Mapping)
            or set(self.filter_priorities) != set(FLOW_PORTS)
            or any(
                isinstance(priority, bool) or not isinstance(priority, int)
                for priority in self.filter_priorities.values()
            )
            or dict(self.filter_priorities) != MANAGED_FILTER_PRIORITIES
        ):
            raise ValueError("managed filters must use priorities 10, 20, 30, and 40")
        object.__setattr__(self, "total_bandwidth_mbps", total)
        object.__setattr__(self, "service_allocations_mbps", MappingProxyType(rates))
        object.__setattr__(
            self, "filter_priorities", MappingProxyType(dict(MANAGED_FILTER_PRIORITIES))
        )
        object.__setattr__(self, "unmatched_rate_mbps", unmatched_rate)
        object.__setattr__(self, "unmatched_ceil_mbps", unmatched_ceil)

    @staticmethod
    def _validate_unmatched_rate(value: float, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be numeric")
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{label} must be positive and finite")
        return result


def _format_rate(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def generate_managed_htb_commands(config: ManagedHTBConfig) -> list[str]:
    """Build the explicit five-leaf hierarchy; unmatched traffic defaults to 1:15."""
    if not isinstance(config, ManagedHTBConfig):
        raise ValueError("config must be a validated ManagedHTBConfig")
    interface = shlex.quote(config.interface)
    commands = [
        f"tc qdisc replace dev {interface} parent 5:1 "
        f"handle 1: htb default {UNMATCHED_CLASS_ID.split(':', 1)[1]}",
        f"tc class add dev {interface} parent 1: classid 1:1 "
        "htb rate 10mbit ceil 10mbit",
    ]
    for traffic_class, class_id in SERVICE_CLASS_IDS.items():
        rate = _format_rate(config.service_allocations_mbps[traffic_class])
        handle = f"{110 + tuple(SERVICE_CLASS_IDS).index(traffic_class) * 10}:"
        commands.extend((
            f"tc class add dev {interface} parent 1:1 classid {class_id} "
            f"htb rate {rate}mbit ceil {rate}mbit",
            f"tc qdisc add dev {interface} parent {class_id} handle {handle} "
            f"netem limit {NETEM_LIMIT} delay {NETEM_DELAY}",
        ))
    unmatched_rate = _format_rate(config.unmatched_rate_mbps)
    commands.extend((
        f"tc class add dev {interface} parent 1:1 classid {UNMATCHED_CLASS_ID} "
        f"htb rate {unmatched_rate}mbit ceil "
        f"{_format_rate(config.unmatched_ceil_mbps)}mbit",
        f"tc qdisc add dev {interface} parent {UNMATCHED_CLASS_ID} "
        f"handle {UNMATCHED_NETEM_HANDLE} netem limit {NETEM_LIMIT} delay {NETEM_DELAY}",
    ))
    for port, traffic_class in zip(FLOW_PORTS, SERVICE_CLASS_IDS):
        commands.append(
            f"tc filter add dev {interface} protocol ip parent 1: "
            f"prio {config.filter_priorities[port]} u32 match ip protocol 17 0xff "
            f"match ip dport {port} 0xffff flowid {SERVICE_CLASS_IDS[traffic_class]}"
        )
    return commands


def _validate_flow_settings(
    interface: str,
    total_bandwidth_mbps: float,
    allocations_mbps: Mapping[int, float],
    filter_priorities: Mapping[int, int],
    default_port: int,
    allow_disposable_veth: bool = False,
) -> dict[int, float]:
    validate_interface(interface)
    probe_name = re.fullmatch(r"qosp[0-9a-f]{1,10}", interface)
    if interface != ROOT_INTERFACE and not (allow_disposable_veth and probe_name):
        raise ValueError(f"this topology supports QoS only on {ROOT_INTERFACE} egress")
    total = validate_total_bandwidth(total_bandwidth_mbps)
    if not math.isclose(total, BOTTLENECK_MBPS, rel_tol=0, abs_tol=1e-9):
        raise ValueError("the existing aggregate bottleneck must remain 10 Mbps")
    if not isinstance(allocations_mbps, Mapping) or set(allocations_mbps) != set(FLOW_PORTS):
        raise ValueError("flow allocations must explicitly cover UDP ports 5201-5204")
    allocations = {}
    for port in FLOW_PORTS:
        rate = allocations_mbps[port]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ValueError(f"allocation for UDP port {port} must be numeric")
        rate = float(rate)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"allocation for UDP port {port} must be positive and finite")
        allocations[port] = rate
    if not math.isclose(sum(allocations.values()), total, rel_tol=0, abs_tol=1e-6):
        raise ValueError("flow allocations must sum to the preserved 10 Mbps aggregate")
    if not isinstance(filter_priorities, Mapping) or set(filter_priorities) != set(FLOW_PORTS):
        raise ValueError("explicit filter priorities are required for UDP ports 5201-5204")
    priorities = []
    for port, priority in filter_priorities.items():
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 65535:
            raise ValueError(f"filter priority for UDP port {port} must be 1-65535")
        priorities.append(priority)
    if len(set(priorities)) != len(FLOW_PORTS):
        raise ValueError("UDP flow filter priorities must be unique")
    if isinstance(default_port, bool) or default_port not in FLOW_PORTS:
        raise ValueError("an explicit default flow port from 5201-5204 is required")
    return allocations


def generate_tc_commands(
    interface: str,
    total_bandwidth_mbps: float,
    allocations_mbps: Mapping[int, float],
    filter_priorities: Mapping[int, int],
    default_port: int,
    *,
    allow_disposable_veth: bool = False,
) -> list[str]:
    """Build a child hierarchy; generated commands never replace root handle 5:."""
    allocations = _validate_flow_settings(
        interface, total_bandwidth_mbps, allocations_mbps,
        filter_priorities, default_port, allow_disposable_veth,
    )
    child_default = FLOW_CLASS_IDS[default_port].split(":", 1)[1]
    commands = [
        f"tc qdisc replace dev {shlex.quote(interface)} parent 5:1 "
        f"handle 1: htb default {child_default}",
        f"tc class add dev {shlex.quote(interface)} parent 1: classid 1:1 "
        "htb rate 10mbit ceil 10mbit",
    ]
    for port in FLOW_PORTS:
        class_id = FLOW_CLASS_IDS[port]
        minor = class_id.split(":", 1)[1]
        rate = _format_rate(allocations[port])
        commands.extend((
            f"tc class add dev {shlex.quote(interface)} parent 1:1 "
            f"classid {class_id} htb rate {rate}mbit ceil {rate}mbit",
            f"tc qdisc add dev {shlex.quote(interface)} parent {class_id} "
            f"handle {NETEM_HANDLES[port]} netem limit {NETEM_LIMIT} delay {NETEM_DELAY}",
            f"tc filter add dev {shlex.quote(interface)} protocol ip parent 1: "
            f"prio {filter_priorities[port]} u32 match ip protocol 17 0xff "
            f"match ip dport {port} 0xffff flowid {class_id}",
        ))
    return commands


def _run(runner: Callable[..., Any], argv: list[str]):
    result = runner(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        raise TCPreconditionError(
            f"read-only traffic-control inspection failed: {' '.join(argv)}: "
            f"{(result.stderr or '').strip()}"
        )
    return result.stdout or ""


def inspect_tc_state(interface: str, runner: Callable[..., Any] | None = None) -> TCState:
    """Read the qdisc, class and filter state without changing it."""
    validate_interface(interface)
    execute = runner or subprocess.run
    root_classes = _run(execute, ["tc", "-d", "class", "show", "dev", interface])
    nested_classes = _run(
        execute, ["tc", "-d", "class", "show", "dev", interface, "parent", "1:"]
    )
    class_lines = list(dict.fromkeys(
        line.strip()
        for output in (root_classes, nested_classes)
        for line in output.splitlines()
        if line.strip().startswith("class ")
    ))
    classes = "\n".join(class_lines) + ("\n" if class_lines else "")
    root_filters = _run(execute, ["tc", "-s", "filter", "show", "dev", interface])
    child_filters = _run(execute, ["tc", "-s", "filter", "show", "dev", interface, "parent", "1:"])
    return TCState(
        qdiscs=_run(execute, ["tc", "-d", "qdisc", "show", "dev", interface]),
        classes=classes,
        filters=root_filters + child_filters,
    )


def _verify_isolated_htb_target(
    target: IsolatedHTBTarget,
    runner: Callable[..., Any],
) -> None:
    """Prove both namespace and veth identities before every scoped tc call."""
    endpoint_specs = (
        (target.namespace, target.interface, target.namespace_inode,
         target.ifindex, target.peer_ifindex),
        (target.peer_namespace, target.peer_interface,
         target.peer_namespace_inode, target.peer_ifindex, target.ifindex),
    )
    for namespace, interface, expected_inode, expected_ifindex, peer_ifindex in endpoint_specs:
        inode_text = _run(runner, [
            "ip", "netns", "exec", namespace, "stat", "-Lc", "%i",
            "/proc/self/ns/net",
        ]).strip()
        if inode_text != str(expected_inode):
            raise TCPreconditionError("isolated target namespace identity changed")
        links_text = _run(runner, [
            "ip", "netns", "exec", namespace, "ip", "-j", "-details", "link", "show",
        ])
        try:
            links = json.loads(links_text)
        except (TypeError, ValueError) as exc:
            raise TCPreconditionError("cannot verify isolated namespace interfaces") from exc
        if not isinstance(links, list) or len(links) != 2:
            raise TCPreconditionError("isolated namespace contains unexpected interfaces")
        if any(not isinstance(item, dict) for item in links):
            raise TCPreconditionError("isolated namespace interface data is malformed")
        by_name = {item.get("ifname"): item for item in links}
        endpoint = by_name.get(interface)
        if (
            set(by_name) != {"lo", interface}
            or not isinstance(endpoint, dict)
            or endpoint.get("ifindex") != expected_ifindex
            or endpoint.get("link_index") != peer_ifindex
            or endpoint.get("master")
            or not isinstance(endpoint.get("linkinfo"), dict)
            or endpoint["linkinfo"].get("info_kind") != "veth"
        ):
            raise TCPreconditionError(
                "isolated target is not the expected unbridged veth pair"
            )


class _IsolatedHTBRunner:
    """Route all controller tc commands into an identity-verified namespace."""

    def __init__(self, target: IsolatedHTBTarget, runner: Callable[..., Any]):
        self.target = target
        self.runner = runner

    def __call__(self, argv, *, capture_output, text, check):
        if not argv or argv[0] != "tc":
            raise TCPreconditionError("isolated controller runner accepts only tc commands")
        _verify_isolated_htb_target(self.target, self.runner)
        return self.runner(
            ["ip", "netns", "exec", self.target.namespace, *argv],
            capture_output=capture_output,
            text=text,
            check=check,
        )


def _verify_disposable_veth(interface: str, runner: Callable[..., Any]) -> None:
    """Allow only a reciprocal, unmastered probe veth pair past the topology guard."""
    target_match = re.fullmatch(r"qosp([0-9a-f]{1,10})", interface)
    if target_match is None:
        raise TCPreconditionError("disposable veth interface name is not probe-scoped")
    peer = f"qosq{target_match.group(1)}"
    for endpoint, expected_peer in ((interface, peer), (peer, interface)):
        output = _run(
            runner, ["ip", "-j", "-details", "link", "show", "dev", endpoint]
        )
        try:
            links = json.loads(output)
        except (TypeError, ValueError) as exc:
            raise TCPreconditionError(
                "cannot verify disposable veth pair metadata"
            ) from exc
        if (
            not isinstance(links, list)
            or len(links) != 1
            or not isinstance(links[0], dict)
            or links[0].get("ifname") != endpoint
            or links[0].get("link") != expected_peer
            or not isinstance(links[0].get("linkinfo"), dict)
            or links[0]["linkinfo"].get("info_kind") != "veth"
            or links[0].get("master")
        ):
            raise TCPreconditionError(
                "test interface is not the expected unmastered disposable veth pair"
            )
        addresses_output = _run(
            runner, ["ip", "-j", "addr", "show", "dev", endpoint]
        )
        try:
            address_records = json.loads(addresses_output)
        except (TypeError, ValueError) as exc:
            raise TCPreconditionError(
                "cannot verify disposable veth address isolation"
            ) from exc
        if (
            not isinstance(address_records, list)
            or len(address_records) != 1
            or not isinstance(address_records[0], dict)
            or address_records[0].get("ifname") != endpoint
            or not isinstance(address_records[0].get("addr_info"), list)
            or any(
                not isinstance(address, dict) or address.get("scope") != "link"
                for address in address_records[0]["addr_info"]
            )
        ):
            raise TCPreconditionError(
                "disposable veth pair is not isolated from network addresses"
            )
    bridges = _run(runner, ["ovs-vsctl", "list-br"])
    for bridge in bridges.splitlines():
        ports = _run(runner, ["ovs-vsctl", "list-ports", bridge])
        if interface in ports.splitlines() or peer in ports.splitlines():
            raise TCPreconditionError("disposable veth pair is attached to an OVS bridge")


def _lines(text: str, prefix: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith(prefix)]


def _baseline_matches(state: TCState) -> bool:
    qdiscs = [line.strip() for line in state.qdiscs.splitlines() if line.strip()]
    classes = [line.strip() for line in state.classes.splitlines() if line.strip()]
    return (
        len(qdiscs) == 2
        and any(re.search(r"^qdisc htb 5: root .*default 0x1(?:\s|$)", line) for line in qdiscs)
        and any(re.search(r"^qdisc netem 10: parent 5:1 .*limit 1000 delay 10ms(?:\s|$)", line) for line in qdiscs)
        and len(classes) == 1
        and bool(re.search(r"^class htb 5:1 root leaf 10: .*rate 10Mbit ceil 10Mbit(?:\s|$)", classes[0]))
        and not state.filters.strip()
    )


def _rate_from_line(line: str) -> float | None:
    match = re.search(r"\brate ([0-9.]+)([KMG]?bit)\b", line)
    if not match:
        return None
    scale = {"bit": 1e-6, "Kbit": 1e-3, "Mbit": 1.0, "Gbit": 1000.0}[match.group(2)]
    return float(match.group(1)) * scale


def _ceil_from_line(line: str) -> float | None:
    match = re.search(r"\bceil ([0-9.]+)([KMG]?bit)\b", line)
    if not match:
        return None
    scale = {"bit": 1e-6, "Kbit": 1e-3, "Mbit": 1.0, "Gbit": 1000.0}[match.group(2)]
    return float(match.group(1)) * scale


def _filter_signature_matches(
    text: str, priorities: Mapping[int, int], default_port: int
) -> bool:
    lines = _filter_structure_lines(text)
    if any(line.strip() and not line.lstrip().startswith("filter ") and
           not line.lstrip().startswith("match ") for line in lines):
        return False
    header_indices = [
        index for index, line in enumerate(lines)
        if re.fullmatch(
            r"\s*filter protocol ip pref \d+ u32 chain 0\s*", line
        )
    ]
    if len(header_indices) != len(FLOW_PORTS):
        return False
    first_content = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if first_content != header_indices[0]:
        return False
    blocks: dict[int, list[str]] = {}
    for position, index in enumerate(header_indices):
        match = re.search(r"pref (\d+)", lines[index])
        if match is None:
            return False
        end = header_indices[position + 1] if position + 1 < len(header_indices) else len(lines)
        priority = int(match.group(1))
        if priority in blocks:
            return False
        block = [line.strip() for line in lines[index + 1:end] if line.strip()]
        if len(block) != 3:
            return False
        blocks[priority] = block

    if set(blocks) != {priorities[port] for port in FLOW_PORTS}:
        return False

    for port in FLOW_PORTS:
        priority = priorities[port]
        class_id = FLOW_CLASS_IDS[port]
        port_hex = f"{port:04x}"
        block = blocks[priority]
        if not _filter_detail_matches(block[0], priority, class_id):
            return False
        expected_matches = (
            f"match 00110000/00ff0000 at 8",
            f"match 0000{port_hex}/0000ffff at 20",
        )
        if tuple(block[1:]) != expected_matches:
            return False
    return True


def _managed_matches(
    state: TCState,
    allocations: Mapping[int, float],
    priorities: Mapping[int, int],
    default_port: int,
) -> bool:
    qdiscs = _lines(state.qdiscs, "qdisc ")
    classes = _lines(state.classes, "class ")
    default_minor = FLOW_CLASS_IDS[default_port].split(":", 1)[1]
    expected_qdisc_count = 2 + len(FLOW_PORTS)
    if len(qdiscs) != expected_qdisc_count or len(classes) != 2 + len(FLOW_PORTS):
        return False
    if not any(re.search(r"^qdisc htb 5: root .*default 0x1(?:\s|$)", line) for line in qdiscs):
        return False
    if not any(re.search(rf"^qdisc htb 1: parent 5:1 .*default 0x{re.escape(default_minor)}(?:\s|$)", line) for line in qdiscs):
        return False
    if not any(re.search(r"^class htb 5:1 root leaf 1: .*rate 10Mbit ceil 10Mbit(?:\s|$)", line) for line in classes):
        return False
    aggregate = next((line for line in classes if line.startswith("class htb 1:1 ")), None)
    if aggregate is None or _rate_from_line(aggregate) != BOTTLENECK_MBPS or "ceil 10Mbit" not in aggregate:
        return False
    for port in FLOW_PORTS:
        class_id = FLOW_CLASS_IDS[port]
        minor = class_id.split(":", 1)[1]
        leaf = next((line for line in classes if line.startswith(f"class htb {class_id} ")), None)
        if leaf is None or not re.search(rf"parent 1:1 leaf {110 + FLOW_PORTS.index(port) * 10}: ", leaf):
            return False
        expected_rate = allocations[port]
        actual_rate = _rate_from_line(leaf)
        actual_ceil = _ceil_from_line(leaf)
        if (
            actual_rate is None or actual_ceil is None
            or not math.isclose(actual_rate, expected_rate, rel_tol=0, abs_tol=0.001)
            or not math.isclose(actual_ceil, expected_rate, rel_tol=0, abs_tol=0.001)
        ):
            return False
        handle = NETEM_HANDLES[port]
        if not any(re.search(rf"^qdisc netem {re.escape(handle)} parent {class_id} .*limit 1000 delay 10ms(?:\s|$)", line) for line in qdiscs):
            return False
    return _filter_signature_matches(state.filters, priorities, default_port)


def _managed_structure_matches(
    state: TCState, priorities: Mapping[int, int], default_port: int
) -> bool:
    # Structure validation uses valid positive rates; actual rates are checked
    # separately when matching the desired policy.
    equal = BOTTLENECK_MBPS / len(FLOW_PORTS)
    return _managed_matches(
        state, {port: equal for port in FLOW_PORTS}, priorities, default_port
    ) or _managed_structure_with_any_rates(state, priorities, default_port)


def _managed_structure_with_any_rates(
    state: TCState, priorities: Mapping[int, int], default_port: int
) -> bool:
    qdiscs = _lines(state.qdiscs, "qdisc ")
    classes = _lines(state.classes, "class ")
    default_minor = FLOW_CLASS_IDS[default_port].split(":", 1)[1]
    if len(qdiscs) != 2 + len(FLOW_PORTS) or len(classes) != 2 + len(FLOW_PORTS):
        return False
    if not any(re.search(r"^qdisc htb 5: root .*default 0x1(?:\s|$)", line) for line in qdiscs):
        return False
    if not any(re.search(rf"^qdisc htb 1: parent 5:1 .*default 0x{re.escape(default_minor)}(?:\s|$)", line) for line in qdiscs):
        return False
    outer = next((line for line in classes if line.startswith("class htb 5:1 ")), None)
    aggregate = next((line for line in classes if line.startswith("class htb 1:1 ")), None)
    if outer is None or not re.search(r"root leaf 1: .*rate 10Mbit ceil 10Mbit", outer):
        return False
    if aggregate is None or _rate_from_line(aggregate) != 10.0 or "ceil 10Mbit" not in aggregate:
        return False
    for port in FLOW_PORTS:
        class_id = FLOW_CLASS_IDS[port]
        leaf = next((line for line in classes if line.startswith(f"class htb {class_id} ")), None)
        if leaf is None or not re.search(rf"parent 1:1 leaf {110 + FLOW_PORTS.index(port) * 10}: ", leaf):
            return False
        rate = _rate_from_line(leaf)
        ceil = _ceil_from_line(leaf)
        if rate is None or ceil is None or rate <= 0 or not math.isclose(rate, ceil, rel_tol=0, abs_tol=0.001):
            return False
        if not any(re.search(rf"^qdisc netem {re.escape(NETEM_HANDLES[port])} parent {class_id} .*limit 1000 delay 10ms(?:\s|$)", line) for line in qdiscs):
            return False
    return _filter_signature_matches(state.filters, priorities, default_port)


def _managed_htb_filter_matches(text: str) -> bool:
    lines = _filter_structure_lines(text)
    if any(
        line.strip()
        and not line.lstrip().startswith(("filter ", "match "))
        for line in lines
    ):
        return False
    header_indices = [
        index for index, line in enumerate(lines)
        if re.fullmatch(r"\s*filter protocol ip pref \d+ u32 chain 0\s*", line)
    ]
    if len(header_indices) != len(FLOW_PORTS):
        return False
    first_content = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if first_content != header_indices[0]:
        return False
    blocks: dict[int, list[str]] = {}
    for position, index in enumerate(header_indices):
        match = re.search(r"pref (\d+)", lines[index])
        if match is None:
            return False
        end = (
            header_indices[position + 1]
            if position + 1 < len(header_indices)
            else len(lines)
        )
        priority = int(match.group(1))
        if priority in blocks:
            return False
        block = [line.strip() for line in lines[index + 1:end] if line.strip()]
        if len(block) != 3:
            return False
        blocks[priority] = block
    if set(blocks) != set(MANAGED_FILTER_PRIORITIES.values()):
        return False
    for port, traffic_class in zip(FLOW_PORTS, SERVICE_CLASS_IDS):
        priority = MANAGED_FILTER_PRIORITIES[port]
        class_id = SERVICE_CLASS_IDS[traffic_class]
        port_hex = f"{port:04x}"
        block = blocks[priority]
        if not _filter_detail_matches(block[0], priority, class_id):
            return False
        if tuple(block[1:]) != (
            "match 00110000/00ff0000 at 8",
            f"match 0000{port_hex}/0000ffff at 20",
        ):
            return False
    return True


def _filter_structure_lines(text: str) -> list[str]:
    """Remove only syntactically valid tc packet counters before ownership checks."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(
            r"Sent \d+ bytes \d+ pkt \(dropped \d+, overlimits \d+ requeues \d+\)",
            stripped,
        ):
            continue
        if re.fullmatch(
            r"filter protocol ip pref \d+ u32 chain 0 fh [0-9a-fA-F]+: ht divisor 1",
            stripped,
        ):
            continue
        stripped = re.sub(r" \(success \d+ ?\)$", "", stripped)
        lines.append(stripped)
    return lines


def _filter_detail_matches(line: str, priority: int, class_id: str) -> bool:
    prefix = rf"filter protocol ip pref {priority} u32 chain 0 fh "
    flow = rf" \*flowid {re.escape(class_id)}(?: not_in_hw)?"
    legacy = re.fullmatch(
        prefix + rf"[0-9a-fA-F]+: order \d+" + flow,
        line,
    )
    modern = re.fullmatch(
        prefix
        + rf"([0-9a-fA-F]+)::[0-9a-fA-F]+ order \d+ key ht \1 bkt \d+"
        + flow
        + r"(?: \(rule hit \d+ success \d+\))?",
        line,
    )
    return legacy is not None or modern is not None


def _managed_htb_owned(state: TCState) -> bool:
    """Recognize only the complete five-leaf hierarchy owned by this controller."""
    qdiscs = _lines(state.qdiscs, "qdisc ")
    classes = _lines(state.classes, "class ")
    if len(qdiscs) != 7 or len(classes) != 7:
        return False
    class_ids = [line.split()[2] for line in classes if len(line.split()) >= 3]
    expected_class_ids = {"5:1", "1:1", *SERVICE_CLASS_IDS.values(), UNMATCHED_CLASS_ID}
    if len(class_ids) != len(classes) or set(class_ids) != expected_class_ids:
        return False
    if not any(
        re.search(r"^qdisc htb 5: root .*default 0x1(?:\s|$)", line)
        for line in qdiscs
    ):
        return False
    if not any(
        re.search(r"^qdisc htb 1: parent 5:1 .*default 0x15(?:\s|$)", line)
        for line in qdiscs
    ):
        return False
    if not any(
        re.search(r"^class htb 5:1 root leaf 1: .*rate 10Mbit ceil 10Mbit(?:\s|$)", line)
        for line in classes
    ):
        return False
    aggregate = next((line for line in classes if line.startswith("class htb 1:1 ")), None)
    if (
        aggregate is None
        or _rate_from_line(aggregate) != BOTTLENECK_MBPS
        or _ceil_from_line(aggregate) != BOTTLENECK_MBPS
    ):
        return False

    service_total = 0.0
    for index, traffic_class in enumerate(SERVICE_CLASS_IDS):
        class_id = SERVICE_CLASS_IDS[traffic_class]
        handle = f"{110 + index * 10}:"
        leaf = next(
            (line for line in classes if line.startswith(f"class htb {class_id} ")),
            None,
        )
        rate = _rate_from_line(leaf) if leaf else None
        ceil = _ceil_from_line(leaf) if leaf else None
        if (
            leaf is None
            or not re.search(rf"parent 1:1 leaf {re.escape(handle)} ", leaf)
            or rate is None
            or ceil is None
            or rate <= 0
            or not math.isclose(rate, ceil, rel_tol=0, abs_tol=0.001)
            or not any(
                re.search(
                    rf"^qdisc netem {re.escape(handle)} parent {class_id} "
                    rf".*limit {NETEM_LIMIT} delay {NETEM_DELAY}(?:\s|$)",
                    qdisc,
                )
                for qdisc in qdiscs
            )
        ):
            return False
        service_total += rate

    unmatched = next(
        (
            line for line in classes
            if line.startswith(f"class htb {UNMATCHED_CLASS_ID} ")
        ),
        None,
    )
    unmatched_rate = _rate_from_line(unmatched) if unmatched else None
    unmatched_ceil = _ceil_from_line(unmatched) if unmatched else None
    if (
        unmatched is None
        or not re.search(
            rf"parent 1:1 leaf {re.escape(UNMATCHED_NETEM_HANDLE)} ", unmatched
        )
        or unmatched_rate != 1.0
        or unmatched_ceil != 1.0
        or not any(
            re.search(
                rf"^qdisc netem {re.escape(UNMATCHED_NETEM_HANDLE)} "
                rf"parent {UNMATCHED_CLASS_ID} .*limit {NETEM_LIMIT} "
                rf"delay {NETEM_DELAY}(?:\s|$)",
                qdisc,
            )
            for qdisc in qdiscs
        )
        or not math.isclose(
            service_total + unmatched_rate, BOTTLENECK_MBPS,
            rel_tol=0, abs_tol=0.001,
        )
    ):
        return False
    return _managed_htb_filter_matches(state.filters)


def _managed_htb_matches(state: TCState, config: ManagedHTBConfig) -> bool:
    if not _managed_htb_owned(state):
        return False
    classes = _lines(state.classes, "class ")
    for traffic_class, class_id in SERVICE_CLASS_IDS.items():
        line = next(
            entry for entry in classes
            if entry.startswith(f"class htb {class_id} ")
        )
        rate = _rate_from_line(line)
        ceil = _ceil_from_line(line)
        expected = config.service_allocations_mbps[traffic_class]
        if (
            rate is None or ceil is None
            or not math.isclose(rate, expected, rel_tol=0, abs_tol=0.001)
            or not math.isclose(ceil, expected, rel_tol=0, abs_tol=0.001)
        ):
            return False
    return True


def _mutate(runner: Callable[..., Any], command: str):
    result = runner(shlex.split(command), capture_output=True, text=True, check=False)
    if result.returncode:
        raise TCApplyError(
            f"traffic-control command failed ({result.returncode}): {command}; "
            f"{(result.stderr or '').strip()}"
        )
    return result


def _restore_baseline(
    interface: str, runner: Callable[..., Any], expected_state: TCState
) -> None:
    current = inspect_tc_state(interface, runner)
    if current != expected_state:
        raise TCApplyError(
            "rollback refused: traffic-control state changed since the last "
            "controller-owned state was observed"
        )
    command = (
        f"tc qdisc replace dev {shlex.quote(interface)} parent 5:1 "
        f"handle 10: netem limit {NETEM_LIMIT} delay {NETEM_DELAY}"
    )
    _mutate(runner, command)
    restored = inspect_tc_state(interface, runner)
    if not _baseline_matches(restored):
        raise TCApplyError("rollback command ran, but original hierarchy verification failed")


def restore_original_hierarchy(
    interface: str,
    filter_priorities: Mapping[int, int],
    default_port: int,
    runner: Callable[..., Any] | None = None,
    *,
    allow_disposable_veth: bool = False,
) -> bool:
    """Restore only the known controller-owned child subtree to the original netem leaf."""
    if interface != ROOT_INTERFACE and not allow_disposable_veth:
        raise TCPreconditionError(f"refusing cleanup outside {ROOT_INTERFACE}")
    execute = runner or subprocess.run
    with _interface_lock(interface):
        if allow_disposable_veth:
            _verify_disposable_veth(interface, execute)
        state = inspect_tc_state(interface, execute)
        if _baseline_matches(state):
            return False
        if _managed_htb_owned(state):
            _restore_baseline(interface, execute, state)
            return True
        _validate_flow_settings(
            interface, BOTTLENECK_MBPS,
            {port: BOTTLENECK_MBPS / 4 for port in FLOW_PORTS},
            filter_priorities, default_port, allow_disposable_veth,
        )
        if not _managed_structure_matches(state, filter_priorities, default_port):
            raise TCPreconditionError("refusing cleanup: child subtree is not recognized as controller-owned")
        _restore_baseline(interface, execute, state)
        return True


def restore_managed_htb_hierarchy(
    interface: str,
    runner: Callable[..., Any] | None = None,
) -> bool:
    """Restore the original leaf only when the complete managed HTB tree is owned."""
    if interface != ROOT_INTERFACE:
        raise TCPreconditionError(f"refusing cleanup outside {ROOT_INTERFACE}")
    execute = runner or subprocess.run
    with _interface_lock(interface):
        state = inspect_tc_state(interface, execute)
        if _baseline_matches(state):
            return False
        if not _managed_htb_owned(state):
            raise TCPreconditionError(
                "refusing cleanup: five-leaf subtree is not recognized as controller-owned"
            )
        _restore_baseline(interface, execute, state)
        return True


def restore_isolated_managed_htb_hierarchy(
    target: IsolatedHTBTarget,
    runner: Callable[..., Any] | None = None,
) -> bool:
    """Restore only a verified probe namespace's complete managed hierarchy."""
    if not isinstance(target, IsolatedHTBTarget):
        raise TCPreconditionError("isolated restore requires an identity-pinned target")
    raw_runner = runner or subprocess.run
    execute = _IsolatedHTBRunner(target, raw_runner)
    with _interface_lock(target.interface):
        state = inspect_tc_state(target.interface, execute)
        if _baseline_matches(state):
            return False
        if not _managed_htb_owned(state):
            raise TCPreconditionError(
                "refusing isolated cleanup: hierarchy is not recognized as controller-owned"
            )
        _restore_baseline(target.interface, execute, state)
        return True


def apply_tc_policy(
    interface: str,
    total_bandwidth_mbps: float,
    allocations_mbps: Mapping[int, float],
    filter_priorities: Mapping[int, int],
    default_port: int,
    runner: Callable[..., Any] | None = None,
    *,
    allow_disposable_veth: bool = False,
) -> list[Any]:
    """Apply or update only the nested policy subtree after strict preflight."""
    allocations = _validate_flow_settings(
        interface, total_bandwidth_mbps, allocations_mbps,
        filter_priorities, default_port, allow_disposable_veth,
    )
    execute = runner or subprocess.run
    commands = generate_tc_commands(
        interface, total_bandwidth_mbps, allocations,
        filter_priorities, default_port,
        allow_disposable_veth=allow_disposable_veth,
    )
    with _interface_lock(interface):
        if allow_disposable_veth:
            _verify_disposable_veth(interface, execute)
        before = inspect_tc_state(interface, execute)
        if _managed_matches(before, allocations, filter_priorities, default_port):
            return []
        baseline = _baseline_matches(before)
        managed = _managed_structure_matches(before, filter_priorities, default_port)
        if not baseline and not managed:
            raise TCPreconditionError(
                "refusing to change traffic control: expected untouched baseline "
                "or the exact controller-owned child hierarchy"
            )

        if baseline:
            expected_partial_state = before
            try:
                results = []
                for command in commands:
                    results.append(_mutate(execute, command))
                    expected_partial_state = inspect_tc_state(interface, execute)
                after = inspect_tc_state(interface, execute)
                if not _managed_matches(after, allocations, filter_priorities, default_port):
                    raise TCApplyError("post-apply hierarchy verification failed")
            except Exception as exc:
                try:
                    _restore_baseline(interface, execute, expected_partial_state)
                except Exception as rollback_exc:
                    raise TCApplyError(
                        f"apply failed: {exc}; rollback verification also failed "
                        f"or rollback was refused: {rollback_exc}"
                    ) from rollback_exc
                raise TCApplyError(f"apply failed; original hierarchy restored and verified: {exc}") from exc
        else:
            # Repeated adaptive decisions change only owned HTB class rates. A
            # changed filter/default structure must first be explicitly restored.
            old_rates = {}
            for port, class_id in FLOW_CLASS_IDS.items():
                line = next(line for line in _lines(before.classes, "class ") if line.startswith(f"class htb {class_id} "))
                old_rates[port] = _rate_from_line(line)
            results = []
            expected_partial_state = before
            try:
                for port, class_id in FLOW_CLASS_IDS.items():
                    if math.isclose(old_rates[port], allocations[port], rel_tol=0, abs_tol=0.001):
                        continue
                    rate = _format_rate(allocations[port])
                    command = (
                        f"tc class change dev {shlex.quote(interface)} parent 1:1 "
                        f"classid {class_id} htb rate {rate}mbit ceil {rate}mbit"
                    )
                    results.append(_mutate(execute, command))
                    expected_partial_state = inspect_tc_state(interface, execute)
                after = inspect_tc_state(interface, execute)
                if not _managed_matches(after, allocations, filter_priorities, default_port):
                    raise TCApplyError("post-update hierarchy verification failed")
            except Exception as exc:
                try:
                    expected_rollback_state = expected_partial_state
                    for port in reversed(tuple(FLOW_PORTS)):
                        current = inspect_tc_state(interface, execute)
                        if current != expected_rollback_state:
                            raise TCApplyError(
                                "rollback refused: traffic-control state changed "
                                "since the last controller-owned state was observed"
                            )
                        rate = _format_rate(old_rates[port])
                        _mutate(
                            execute,
                            f"tc class change dev {shlex.quote(interface)} parent 1:1 "
                            f"classid {FLOW_CLASS_IDS[port]} htb rate {rate}mbit ceil {rate}mbit",
                        )
                        expected_rollback_state = inspect_tc_state(interface, execute)
                    restored = inspect_tc_state(interface, execute)
                    if not _managed_matches(
                        restored, old_rates, filter_priorities, default_port
                    ):
                        raise TCApplyError("rollback class-rate verification failed")
                except Exception as rollback_exc:
                    raise TCApplyError(
                        f"policy update failed: {exc}; class-rate rollback "
                        f"verification also failed or rollback was refused: {rollback_exc}"
                    ) from rollback_exc
                raise TCApplyError(f"policy update failed; prior class rates restored: {exc}") from exc

        return results


def apply_managed_htb_policy(
    config: ManagedHTBConfig,
    runner: Callable[..., Any] | None = None,
) -> list[Any]:
    """Apply or update the validated five-leaf hierarchy with guarded rollback."""
    if not isinstance(config, ManagedHTBConfig):
        raise ValueError("config must be a validated ManagedHTBConfig")
    raw_runner = runner or subprocess.run
    execute = (
        _IsolatedHTBRunner(config.isolated_target, raw_runner)
        if config.isolated_target is not None
        else raw_runner
    )
    commands = generate_managed_htb_commands(config)
    with _interface_lock(config.interface):
        before = inspect_tc_state(config.interface, execute)
        if _managed_htb_matches(before, config):
            return []
        baseline = _baseline_matches(before)
        managed = _managed_htb_owned(before)
        if not baseline and not managed:
            raise TCPreconditionError(
                "refusing to change traffic control: expected untouched baseline "
                "or the exact controller-owned five-leaf hierarchy"
            )

        if baseline:
            expected_partial_state = before
            try:
                results = []
                for command in commands:
                    results.append(_mutate(execute, command))
                    expected_partial_state = inspect_tc_state(config.interface, execute)
                after = inspect_tc_state(config.interface, execute)
                if not _managed_htb_matches(after, config):
                    raise TCApplyError("post-apply five-leaf hierarchy verification failed")
            except Exception as exc:
                try:
                    _restore_baseline(config.interface, execute, expected_partial_state)
                except Exception as rollback_exc:
                    raise TCApplyError(
                        f"apply failed: {exc}; rollback verification also failed "
                        f"or rollback was refused: {rollback_exc}"
                    ) from rollback_exc
                raise TCApplyError(
                    f"apply failed; original hierarchy restored and verified: {exc}"
                ) from exc
            return results

        old_rates: dict[str, float] = {}
        classes = _lines(before.classes, "class ")
        for traffic_class, class_id in SERVICE_CLASS_IDS.items():
            line = next(
                (entry for entry in classes if entry.startswith(f"class htb {class_id} ")),
                None,
            )
            rate = _rate_from_line(line) if line else None
            if rate is None:
                raise TCPreconditionError(
                    f"managed class {class_id} has no verifiable rate"
                )
            old_rates[traffic_class] = rate

        changed_classes: list[str] = []
        results = []
        expected_partial_state = before
        try:
            for traffic_class, class_id in SERVICE_CLASS_IDS.items():
                requested = config.service_allocations_mbps[traffic_class]
                if math.isclose(
                    old_rates[traffic_class], requested, rel_tol=0, abs_tol=0.001
                ):
                    continue
                rate = _format_rate(requested)
                command = (
                    f"tc class change dev {shlex.quote(config.interface)} parent 1:1 "
                    f"classid {class_id} htb rate {rate}mbit ceil {rate}mbit"
                )
                results.append(_mutate(execute, command))
                changed_classes.append(traffic_class)
                expected_partial_state = inspect_tc_state(config.interface, execute)
            after = inspect_tc_state(config.interface, execute)
            if not _managed_htb_matches(after, config):
                raise TCApplyError("post-update five-leaf hierarchy verification failed")
        except Exception as exc:
            try:
                for traffic_class in reversed(changed_classes):
                    current = inspect_tc_state(config.interface, execute)
                    if current != expected_partial_state:
                        raise TCApplyError(
                            "rollback refused: traffic-control state changed "
                            "since the last controller-owned state was observed"
                        )
                    class_id = SERVICE_CLASS_IDS[traffic_class]
                    rate = _format_rate(old_rates[traffic_class])
                    _mutate(
                        execute,
                        f"tc class change dev {shlex.quote(config.interface)} "
                        f"parent 1:1 classid {class_id} htb rate {rate}mbit "
                        f"ceil {rate}mbit",
                    )
                    expected_partial_state = inspect_tc_state(config.interface, execute)
                restored = inspect_tc_state(config.interface, execute)
                restored_classes = _lines(restored.classes, "class ")
                rates_restored = _managed_htb_owned(restored)
                for name, class_id in SERVICE_CLASS_IDS.items():
                    line = next(
                        (
                            entry for entry in restored_classes
                            if entry.startswith(f"class htb {class_id} ")
                        ),
                        None,
                    )
                    rate = _rate_from_line(line) if line else None
                    if rate is None or not math.isclose(
                        rate, old_rates[name], rel_tol=0, abs_tol=0.001
                    ):
                        rates_restored = False
                        break
                if not rates_restored:
                    raise TCApplyError("rollback class-rate verification failed")
            except Exception as rollback_exc:
                raise TCApplyError(
                    f"managed policy update failed: {exc}; class-rate rollback "
                    f"verification also failed or rollback was refused: {rollback_exc}"
                ) from rollback_exc
            raise TCApplyError(
                f"managed policy update failed; prior class rates restored: {exc}"
            ) from exc
        return results


def apply_tc_commands(
    commands: list[str],
    runner: Callable[..., Any] | None = None,
) -> list[Any]:
    """Reject unscoped execution; live commands require preflight/rollback."""
    raise RuntimeError(
        "unscoped tc command execution is disabled; use apply_tc_policy"
    )
