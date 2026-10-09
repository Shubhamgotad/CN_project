"""Linux tc command generation and explicit command execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import ipaddress
import shlex
import subprocess
from typing import Any

from .policy import TRAFFIC_CLASSES
from .validator import validate_allocations, validate_interface, validate_total_bandwidth

CLASS_IDS = {"gaming": "10", "video": "20", "web": "30", "bulk": "40"}


def _format_rate(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def generate_tc_commands(
    interface: str,
    total_bandwidth_mbps: float,
    allocations: Mapping[str, float],
    traffic_filters: Mapping[str, Mapping[str, Any] | Sequence[Mapping[str, Any]]] | None = None,
) -> list[str]:
    validate_interface(interface)
    total = validate_total_bandwidth(total_bandwidth_mbps)
    allocation = validate_allocations(allocations, total)
    filters = traffic_filters or {}
    commands = [
        f"tc qdisc add dev {shlex.quote(interface)} root handle 1: htb default {CLASS_IDS['bulk']}"
    ]
    total_rate = _format_rate(total)
    commands.append(
        f"tc class add dev {shlex.quote(interface)} parent 1: classid 1:1 "
        f"htb rate {total_rate}mbit ceil {total_rate}mbit"
    )
    for traffic_class in TRAFFIC_CLASSES:
        class_id = CLASS_IDS[traffic_class]
        rate = _format_rate(allocation[traffic_class])
        commands.append(
            f"tc class add dev {shlex.quote(interface)} parent 1:1 classid 1:{class_id} "
            f"htb rate {rate}mbit ceil {rate}mbit"
        )
        commands.append(
            f"tc qdisc add dev {shlex.quote(interface)} parent 1:{class_id} "
            f"handle {class_id}: fq_codel"
        )
        class_filters = filters.get(traffic_class, [])
        if isinstance(class_filters, Mapping):
            class_filters = [class_filters]
        for filter_config in class_filters:
            commands.append(_filter_command(interface, class_id, filter_config))
    return commands


def _filter_command(interface: str, class_id: str, config: Mapping[str, Any]) -> str:
    if not isinstance(config, Mapping):
        raise ValueError("traffic filter configuration must be a mapping")
    protocol = config.get("protocol", "tcp")
    if not isinstance(protocol, str) or protocol.lower() not in {"tcp", "udp"}:
        raise ValueError("traffic filter protocol must be TCP or UDP")
    protocol = protocol.lower()
    priority = config.get("priority", 1)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 65535:
        raise ValueError("traffic filter priority must be an integer from 1 to 65535")
    if "port" in config:
        port = config["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("traffic filter port must be an integer from 1 to 65535")
        return (
            f"tc filter add dev {shlex.quote(interface)} protocol ip parent 1: "
            f"prio {priority} u32 match ip protocol "
            f"{protocol} 0xff match ip dport {port} 0xffff flowid 1:{class_id}"
        )
    if "ip" in config:
        ip = config["ip"]
        try:
            ipaddress.ip_address(ip)
        except (ValueError, TypeError) as exc:
            raise ValueError("traffic filter IP address is invalid") from exc
        return (
            f"tc filter add dev {shlex.quote(interface)} protocol ip parent 1: "
            f"prio {priority} u32 match ip dst {ip} "
            f"flowid 1:{class_id}"
        )
    if "dscp" in config:
        dscp = config["dscp"]
        if isinstance(dscp, bool) or not isinstance(dscp, int) or not 0 <= dscp <= 63:
            raise ValueError("traffic filter DSCP must be an integer from 0 to 63")
        return (
            f"tc filter add dev {shlex.quote(interface)} protocol ip parent 1: "
            f"prio {priority} u32 match ip tos {dscp} "
            f"0xfc flowid 1:{class_id}"
        )
    raise ValueError("traffic filter must define port, ip, or dscp")


def apply_tc_commands(
    commands: Sequence[str],
    runner: Callable[..., Any] | None = None,
) -> list[Any]:
    """Explicitly execute generated commands; importing this module executes nothing."""
    execute = runner or subprocess.run
    return [execute(shlex.split(command), check=True) for command in commands]
