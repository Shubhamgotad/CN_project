import math

import pytest

from qos import (
    allocate_bandwidth,
    build_qos_decision,
    generate_tc_commands,
    get_policy,
)
from qos.tc_controller import apply_tc_commands
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


def test_tc_commands_are_configurable_and_not_executed():
    allocations = allocate_bandwidth(10, congestion="HIGH")
    commands = generate_tc_commands(
        "demo0",
        10,
        allocations,
        {"gaming": {"port": 5000, "protocol": "udp"}, "video": {"ip": "10.0.0.2"}},
    )
    assert commands[0] == "tc qdisc add dev demo0 root handle 1: htb default 40"
    assert any("rate 5mbit" in command for command in commands)
    assert any("fq_codel" in command for command in commands)
    assert any("dport 5000" in command for command in commands)
    assert any("dst 10.0.0.2" in command for command in commands)

    called = []
    apply_tc_commands(["echo safe"], runner=lambda *args, **kwargs: called.append((args, kwargs)))
    assert called


def test_tc_commands_have_root_htb_class_and_child_hierarchy():
    commands = generate_tc_commands("demo0", 10, allocate_bandwidth(10, congestion="HIGH"))
    assert commands[0] == "tc qdisc add dev demo0 root handle 1: htb default 40"
    assert commands[1] == (
        "tc class add dev demo0 parent 1: classid 1:1 htb rate 10mbit ceil 10mbit"
    )
    class_commands = [
        command for command in commands if command.startswith("tc class add")
    ]
    qdisc_commands = [
        command for command in commands if " fq_codel" in command
    ]
    assert len(class_commands) == 5
    assert all("parent 1:1 classid 1:" in command for command in class_commands[1:])
    assert all(
        f"parent 1:{class_id}" in command
        for command, class_id in zip(qdisc_commands, ("10", "20", "30", "40"))
    )


@pytest.mark.parametrize("filter_config", [
    {"port": 0},
    {"port": 65536},
    {"port": "5201"},
    {"protocol": "icmp", "port": 5201},
    {"priority": 0, "port": 5201},
    {"priority": "1", "port": 5201},
    {"ip": "not-an-ip"},
    {"dscp": -1},
    {"dscp": 64},
])
def test_invalid_tc_filter_values_are_rejected(filter_config):
    with pytest.raises(ValueError):
        generate_tc_commands(
            "demo0",
            10,
            allocate_bandwidth(10, congestion="LOW"),
            {"web": filter_config},
        )


@pytest.mark.parametrize("state", ["LOW", "MEDIUM", "HIGH"])
def test_end_to_end_decision(state):
    decision = build_qos_decision(state, 10)
    assert decision["congestion"] == state
    assert sum(decision["policy"].values()) == 100
    assert sum(decision["allocation_mbps"].values()) == pytest.approx(10)
