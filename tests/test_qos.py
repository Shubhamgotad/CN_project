import math

import pytest

from qos import (
    allocate_bandwidth,
    build_qos_decision,
    generate_tc_commands,
    get_policy,
)
from qos.tc_controller import FLOW_PORTS
from qos.policy import validate_policy
from qos.validator import validate_allocations, validate_interface


@pytest.mark.parametrize("state,expected", [
    ("LOW", {"gaming": 30.0, "video": 30.0, "web": 25.0, "bulk": 15.0}),
    ("MEDIUM", {"gaming": 40.0, "video": 30.0, "web": 20.0, "bulk": 10.0}),
    ("HIGH", {"gaming": 50.0, "video": 25.0, "web": 15.0, "bulk": 10.0}),
])
def test_policies(state, expected):
    policy = get_policy(state)
    assert policy == expected
    assert sum(policy.values()) == 100


def test_invalid_policy_and_bandwidth():
    with pytest.raises(ValueError):
        get_policy("UNKNOWN")
    with pytest.raises(ValueError):
        allocate_bandwidth(0, congestion="LOW")
    with pytest.raises(ValueError):
        allocate_bandwidth(-1, congestion="LOW")
    with pytest.raises(ValueError):
        validate_allocations({"gaming": 11, "video": 11, "web": 11, "bulk": 11}, 10)
    with pytest.raises(ValueError, match="finite"):
        validate_policy({"gaming": math.nan, "video": 30, "web": 25, "bulk": 15})
    with pytest.raises(ValueError, match="finite"):
        validate_policy({"gaming": math.inf, "video": 30, "web": 25, "bulk": 15})


@pytest.mark.parametrize("state,expected", [
    ("LOW", {"gaming": 3.0, "video": 3.0, "web": 2.5, "bulk": 1.5}),
    ("MEDIUM", {"gaming": 4.0, "video": 3.0, "web": 2.0, "bulk": 1.0}),
    ("HIGH", {"gaming": 5.0, "video": 2.5, "web": 1.5, "bulk": 1.0}),
])
def test_allocations(state, expected):
    allocation = allocate_bandwidth(10, congestion=state)
    assert allocation == expected
    assert sum(allocation.values()) == 10


def test_fractional_allocation_and_validation():
    allocation = allocate_bandwidth(7.5, congestion="HIGH")
    assert sum(allocation.values()) == pytest.approx(7.5)
    with pytest.raises(ValueError):
        validate_interface("")
    with pytest.raises(ValueError):
        validate_interface("eth0; rm -rf /")


def test_tc_commands_are_nested_and_never_replace_the_root():
    allocations = {port: 2.5 for port in FLOW_PORTS}
    priorities = {port: 10 + index for index, port in enumerate(FLOW_PORTS)}
    commands = generate_tc_commands(
        "s1-eth5",
        10,
        allocations,
        priorities,
        default_port=5204,
    )
    assert commands[0] == (
        "tc qdisc replace dev s1-eth5 parent 5:1 handle 1: htb default 14"
    )
    assert "root handle" not in " ".join(commands)
    assert any("classid 1:1 htb rate 10mbit ceil 10mbit" in c for c in commands)
    for port, minor in zip(FLOW_PORTS, (11, 12, 13, 14)):
        assert any(f"classid 1:{minor} htb rate 2.5mbit ceil 2.5mbit" in c for c in commands)
        assert any(f"parent 1:{minor} handle {110 + (minor - 11) * 10}: netem limit 1000 delay 10ms" in c for c in commands)
        assert any(
            f"dev s1-eth5 protocol ip parent 1: prio {priorities[port]} u32 "
            f"match ip protocol 17 0xff match ip dport {port} 0xffff flowid 1:{minor}" in c
            for c in commands
        )


@pytest.mark.parametrize("kwargs", [
    {"allocations_mbps": {5201: 1, 5202: 2, 5203: 3}},
    {"allocations_mbps": {5201: 1, 5202: 2, 5203: 3, 5204: 3}},
    {"filter_priorities": {5201: 1, 5202: 1, 5203: 3, 5204: 4}},
    {"default_port": None},
])
def test_incomplete_or_unsafe_hierarchy_settings_are_rejected(kwargs):
    args = {
        "interface": "s1-eth5",
        "total_bandwidth_mbps": 10,
        "allocations_mbps": {port: 2.5 for port in FLOW_PORTS},
        "filter_priorities": {port: i + 1 for i, port in enumerate(FLOW_PORTS)},
        "default_port": 5204,
    }
    args.update(kwargs)
    with pytest.raises(ValueError):
        generate_tc_commands(**args)


def test_tc_filters_are_udp_destination_matches_on_client_egress():
    commands = generate_tc_commands(
        "s1-eth5", 10, {port: 2.5 for port in FLOW_PORTS},
        {port: 10 + i for i, port in enumerate(FLOW_PORTS)}, 5204,
    )
    filters = [command for command in commands if "filter add" in command]
    assert len(filters) == 4
    assert all("dev s1-eth5" in command for command in filters)
    assert all("protocol ip parent 1:" in command for command in filters)
    assert all("match ip protocol 17 0xff" in command for command in filters)
    assert not any("dev s2-eth1" in command for command in commands)


def test_controller_rejects_other_interfaces_and_wrong_aggregate():
    allocations = {port: 2.5 for port in FLOW_PORTS}
    priorities = {port: index + 1 for index, port in enumerate(FLOW_PORTS)}
    with pytest.raises(ValueError, match="s1-eth5"):
        generate_tc_commands("s2-eth1", 10, allocations, priorities, 5204)
    with pytest.raises(ValueError, match="10 Mbps"):
        generate_tc_commands("s1-eth5", 9, allocations, priorities, 5204)


@pytest.mark.parametrize("state", ["LOW", "MEDIUM", "HIGH"])
def test_end_to_end_decision(state):
    decision = build_qos_decision(state, 10)
    assert decision["congestion"] == state
    assert sum(decision["policy"].values()) == 100
    assert sum(decision["allocation_mbps"].values()) == pytest.approx(10)
