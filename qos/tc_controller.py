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


def _format_rate(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


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
    classes = _run(execute, ["tc", "-d", "class", "show", "dev", interface])
    root_filters = _run(execute, ["tc", "-s", "filter", "show", "dev", interface])
    child_filters = _run(execute, ["tc", "-s", "filter", "show", "dev", interface, "parent", "1:"])
    return TCState(
        qdiscs=_run(execute, ["tc", "-d", "qdisc", "show", "dev", interface]),
        classes=classes,
        filters=root_filters + child_filters,
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
    qdiscs = _lines(state.qdiscs, "qdisc ")
    classes = _lines(state.classes, "class ")
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
    lines = text.splitlines()
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
        detail = re.fullmatch(
            rf"filter protocol ip pref {priority} u32 chain 0 "
            rf"fh [0-9a-fA-F]+: order \d+ \*flowid "
            rf"{re.escape(class_id)}(?: not_in_hw)?",
            block[0],
        )
        if detail is None:
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
        _validate_flow_settings(
            interface, BOTTLENECK_MBPS,
            {port: BOTTLENECK_MBPS / 4 for port in FLOW_PORTS},
            filter_priorities, default_port, allow_disposable_veth,
        )
        if not _managed_structure_matches(state, filter_priorities, default_port):
            raise TCPreconditionError("refusing cleanup: child subtree is not recognized as controller-owned")
        _restore_baseline(interface, execute, state)
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


def apply_tc_commands(
    commands: list[str],
    runner: Callable[..., Any] | None = None,
) -> list[Any]:
    """Reject unscoped execution; live commands require preflight/rollback."""
    raise RuntimeError(
        "unscoped tc command execution is disabled; use apply_tc_policy"
    )
