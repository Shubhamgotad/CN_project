import json
from types import SimpleNamespace

import pytest

from qos.allocator import MANAGED_HTB_PROFILES, plan_managed_htb
from qos.tc_controller import (
    FLOW_PORTS,
    MANAGED_FILTER_PRIORITIES,
    SERVICE_CLASS_IDS,
    UNMATCHED_CLASS_ID,
    UNMATCHED_NETEM_HANDLE,
    IsolatedHTBTarget,
    ManagedHTBConfig,
    TCApplyError,
    TCPreconditionError,
    apply_managed_htb_policy,
    generate_managed_htb_commands,
    inspect_tc_state,
    restore_managed_htb_hierarchy,
    restore_isolated_managed_htb_hierarchy,
    restore_original_hierarchy,
)


def baseline_state():
    return {
        "qdiscs": (
            "qdisc htb 5: root refcnt 1 r2q 10 default 0x1 direct_packets_stat 0\n"
            "qdisc netem 10: parent 5:1 limit 1000 delay 10ms\n"
        ),
        "classes": (
            "class htb 5:1 root leaf 10: prio 0 rate 10Mbit ceil 10Mbit\n"
        ),
        "filters": "",
    }


def managed_state(rates=None):
    rates = dict(rates or MANAGED_HTB_PROFILES["LOW"])
    qdiscs = [
        "qdisc htb 5: root refcnt 1 r2q 10 default 0x1 direct_packets_stat 0",
        "qdisc htb 1: parent 5:1 r2q 10 default 0x15 direct_packets_stat 0",
    ]
    classes = [
        "class htb 5:1 root leaf 1: prio 0 rate 10Mbit ceil 10Mbit",
        "class htb 1:1 root rate 10Mbit ceil 10Mbit",
    ]
    for index, (traffic_class, class_id) in enumerate(SERVICE_CLASS_IDS.items()):
        handle = f"{110 + index * 10}:"
        rate = rates[traffic_class]
        classes.append(
            f"class htb {class_id} parent 1:1 leaf {handle} prio 0 "
            f"rate {rate:g}Mbit ceil {rate:g}Mbit"
        )
        qdiscs.append(
            f"qdisc netem {handle} parent {class_id} limit 1000 delay 10ms"
        )
    classes.append(
        f"class htb {UNMATCHED_CLASS_ID} parent 1:1 leaf {UNMATCHED_NETEM_HANDLE} "
        "prio 0 rate 1Mbit ceil 1Mbit"
    )
    qdiscs.append(
        f"qdisc netem {UNMATCHED_NETEM_HANDLE} parent {UNMATCHED_CLASS_ID} "
        "limit 1000 delay 10ms"
    )
    filters = []
    for index, (port, traffic_class) in enumerate(
        zip(FLOW_PORTS, SERVICE_CLASS_IDS)
    ):
        priority = MANAGED_FILTER_PRIORITIES[port]
        filters.extend((
            f"filter protocol ip pref {priority} u32 chain 0",
            f"filter protocol ip pref {priority} u32 chain 0 fh {800 + index}: "
            f"order 2048 *flowid {SERVICE_CLASS_IDS[traffic_class]} not_in_hw",
            "  match 00110000/00ff0000 at 8",
            f"  match 0000{port:04x}/0000ffff at 20",
        ))
    return {
        "qdiscs": "\n".join(qdiscs) + "\n",
        "classes": "\n".join(classes) + "\n",
        "filters": "\n".join(filters) + "\n",
    }


class MockManagedTC:
    def __init__(
        self,
        state=None,
        *,
        fail_fragment=None,
        rollback_fails=False,
        rollback_verification_fails=False,
    ):
        self.state = state or baseline_state()
        self.fail_fragment = fail_fragment
        self.rollback_fails = rollback_fails
        self.rollback_verification_fails = rollback_verification_fails
        self.failed_once = False
        self.mutations = []

    def __call__(self, argv, capture_output, text, check):
        if argv[:3] == ["tc", "-d", "qdisc"]:
            return SimpleNamespace(returncode=0, stdout=self.state["qdiscs"], stderr="")
        if argv[:3] == ["tc", "-d", "class"]:
            return SimpleNamespace(returncode=0, stdout=self.state["classes"], stderr="")
        if argv[:3] == ["tc", "-s", "filter"]:
            if "parent" in argv:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=self.state["filters"], stderr="")

        command = " ".join(argv)
        self.mutations.append(command)
        if self.fail_fragment and self.fail_fragment in command and not self.failed_once:
            self.failed_once = True
            return SimpleNamespace(returncode=2, stdout="", stderr="injected failure")
        if self.rollback_fails and "handle 10:" in command:
            return SimpleNamespace(returncode=2, stdout="", stderr="rollback failure")
        if "qdisc replace" in command and "handle 1:" in command:
            self.state = {
                "qdiscs": (
                    "qdisc htb 5: root refcnt 1 r2q 10 default 0x1 direct_packets_stat 0\n"
                    "qdisc htb 1: parent 5:1 r2q 10 default 0x15 direct_packets_stat 0\n"
                ),
                "classes": (
                    "class htb 5:1 root leaf 1: prio 0 rate 10Mbit ceil 10Mbit\n"
                ),
                "filters": "",
            }
        elif "qdisc replace" in command and "handle 10:" in command:
            if not self.rollback_verification_fails:
                self.state = baseline_state()
        elif "class add" in command:
            words = command.split()
            class_id = words[words.index("classid") + 1]
            rate = words[words.index("rate") + 1].removesuffix("mbit")
            ceil = words[words.index("ceil") + 1].removesuffix("mbit")
            if class_id == "1:1":
                line = f"class htb 1:1 root rate {rate}Mbit ceil {ceil}Mbit"
            else:
                traffic_class = next(
                    key for key, value in SERVICE_CLASS_IDS.items()
                    if value == class_id
                ) if class_id in SERVICE_CLASS_IDS.values() else None
                if traffic_class is None:
                    line = (
                        f"class htb {class_id} parent 1:1 leaf 150: prio 0 "
                        f"rate {rate}Mbit ceil {ceil}Mbit"
                    )
                else:
                    index = tuple(SERVICE_CLASS_IDS).index(traffic_class)
                    line = (
                        f"class htb {class_id} parent 1:1 leaf {110 + index * 10}: "
                        f"prio 0 rate {rate}Mbit ceil {ceil}Mbit"
                    )
            self.state["classes"] += line + "\n"
        elif "qdisc add" in command:
            words = command.split()
            parent = words[words.index("parent") + 1]
            handle = words[words.index("handle") + 1]
            self.state["qdiscs"] += (
                f"qdisc netem {handle} parent {parent} limit 1000 delay 10ms\n"
            )
        elif "filter add" in command:
            words = command.split()
            priority = words[words.index("prio") + 1]
            port = words[words.index("dport") + 1]
            class_id = words[words.index("flowid") + 1]
            index = int(port) - FLOW_PORTS[0]
            self.state["filters"] += (
                f"filter protocol ip pref {priority} u32 chain 0\n"
                f"filter protocol ip pref {priority} u32 chain 0 fh {800 + index}: "
                f"order 2048 *flowid {class_id} not_in_hw\n"
                "  match 00110000/00ff0000 at 8\n"
                f"  match 0000{int(port):04x}/0000ffff at 20\n"
            )
        elif "class change" in command:
            words = command.split()
            class_id = words[words.index("classid") + 1]
            traffic_class = next(
                key for key, value in SERVICE_CLASS_IDS.items() if value == class_id
            )
            rate = float(words[words.index("rate") + 1].removesuffix("mbit"))
            current = {}
            for name, managed_class_id in SERVICE_CLASS_IDS.items():
                line = next(
                    entry for entry in self.state["classes"].splitlines()
                    if entry.startswith(f"class htb {managed_class_id} ")
                )
                current[name] = float(line.split("rate ", 1)[1].split("Mbit", 1)[0])
            current[traffic_class] = rate
            self.state = managed_state(current)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class IsolatedMockRunner:
    def __init__(self, *, namespace_inode=401, ifindex=11, peer_inode=402, peer_ifindex=12):
        self.tc = MockManagedTC()
        self.namespace_inode = namespace_inode
        self.ifindex = ifindex
        self.peer_inode = peer_inode
        self.peer_ifindex = peer_ifindex
        self.tc_commands = []

    def __call__(self, argv, capture_output, text, check):
        if argv[:4] == ["ip", "netns", "exec", "qosna12345678"]:
            namespace, interface, inode, ifindex, peer_ifindex = (
                "qosna12345678", "qosp12345678", self.namespace_inode,
                self.ifindex, self.peer_ifindex,
            )
        elif argv[:4] == ["ip", "netns", "exec", "qosnb12345678"]:
            namespace, interface, inode, ifindex, peer_ifindex = (
                "qosnb12345678", "qosq12345678", self.peer_inode,
                self.peer_ifindex, self.ifindex,
            )
        else:
            return SimpleNamespace(returncode=2, stdout="", stderr="unexpected namespace")
        command = argv[4:]
        if command[:2] == ["stat", "-Lc"]:
            return SimpleNamespace(returncode=0, stdout=f"{inode}\n", stderr="")
        if command[:5] == ["ip", "-j", "-details", "link", "show"]:
            rows = [
                {"ifname": "lo", "ifindex": 1, "linkinfo": {"info_kind": "loopback"}},
                {"ifname": interface, "ifindex": ifindex, "link_index": peer_ifindex,
                 "linkinfo": {"info_kind": "veth"}},
            ]
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if command and command[0] == "tc":
            self.tc_commands.append(list(argv))
            return self.tc(command, capture_output=capture_output, text=text, check=check)
        return SimpleNamespace(returncode=2, stdout="", stderr="unexpected isolated command")


def isolated_target(**changes):
    values = {
        "namespace": "qosna12345678",
        "interface": "qosp12345678",
        "namespace_inode": 401,
        "ifindex": 11,
        "peer_namespace": "qosnb12345678",
        "peer_interface": "qosq12345678",
        "peer_namespace_inode": 402,
        "peer_ifindex": 12,
    }
    values.update(changes)
    return IsolatedHTBTarget(**values)


def test_planner_produces_all_three_exact_ten_mbps_profiles():
    unmatched = 1.0
    expected_totals = {"LOW": 10.0, "MEDIUM": 10.0, "HIGH": 10.0}
    for profile, expected in MANAGED_HTB_PROFILES.items():
        config = plan_managed_htb(profile)
        assert dict(config.service_allocations_mbps) == dict(expected)
        assert sum(config.service_allocations_mbps.values()) + unmatched == expected_totals[profile]
        assert config.unmatched_rate_mbps == config.unmatched_ceil_mbps == 1.0
        assert config.default_class_id == "1:15"
    with pytest.raises(ValueError, match="invalid congestion"):
        plan_managed_htb("UNKNOWN")


def test_generated_commands_have_five_leaves_and_default_unmatched_mapping():
    config = plan_managed_htb("LOW")
    commands = generate_managed_htb_commands(config)
    assert len(commands) == 16
    assert commands[0] == (
        "tc qdisc replace dev s1-eth5 parent 5:1 handle 1: htb default 15"
    )
    assert any(
        "parent 1: classid 1:1 htb rate 10mbit ceil 10mbit" in command
        for command in commands
    )
    for traffic_class, class_id in SERVICE_CLASS_IDS.items():
        rate = config.service_allocations_mbps[traffic_class]
        assert any(
            f"classid {class_id} htb rate {rate:g}mbit ceil {rate:g}mbit" in command
            for command in commands
        )
    assert any(
        "classid 1:15 htb rate 1mbit ceil 1mbit" in command
        for command in commands
    )
    assert any("parent 1:15 handle 150: netem" in command for command in commands)
    filters = [command for command in commands if "filter add" in command]
    assert len(filters) == 4
    for port, class_id in zip(FLOW_PORTS, SERVICE_CLASS_IDS.values()):
        assert any(
            f"prio {MANAGED_FILTER_PRIORITIES[port]} " in command
            and f"match ip dport {port} 0xffff flowid {class_id}" in command
            for command in filters
        )
    assert not any("filter add" in command and "flowid 1:15" in command for command in commands)


def test_generated_udp_filter_commands_are_exact_and_ordered():
    commands = generate_managed_htb_commands(plan_managed_htb("LOW"))
    filters = [command for command in commands if "filter add" in command]

    assert filters == [
        "tc filter add dev s1-eth5 protocol ip parent 1: prio 10 u32 "
        "match ip protocol 17 0xff match ip dport 5201 0xffff flowid 1:11",
        "tc filter add dev s1-eth5 protocol ip parent 1: prio 20 u32 "
        "match ip protocol 17 0xff match ip dport 5202 0xffff flowid 1:12",
        "tc filter add dev s1-eth5 protocol ip parent 1: prio 30 u32 "
        "match ip protocol 17 0xff match ip dport 5203 0xffff flowid 1:13",
        "tc filter add dev s1-eth5 protocol ip parent 1: prio 40 u32 "
        "match ip protocol 17 0xff match ip dport 5204 0xffff flowid 1:14",
    ]


@pytest.mark.parametrize("changes, message", [
    ({"service_allocations_mbps": {"gaming": 2, "video": 2, "web": 2, "bulk": 2}}, "total 10"),
    ({"service_allocations_mbps": {"gaming": -1, "video": 4, "web": 3, "bulk": 3}}, "positive"),
    ({"service_allocations_mbps": {"gaming": 2, "video": 2, "web": 2}}, "cover"),
    ({"unmatched_rate_mbps": 0.5}, "both be 1 Mbps"),
    ({"unmatched_ceil_mbps": 2}, "both be 1 Mbps"),
    ({"default_class_id": "1:14"}, "default class"),
    ({"filter_priorities": {5201: 10, 5202: 20, 5203: 30, 5204: 50}}, "priorities"),
])
def test_malformed_managed_configuration_fails_before_generation(changes, message):
    values = {
        "interface": "s1-eth5",
        "total_bandwidth_mbps": 10,
        "service_allocations_mbps": dict(MANAGED_HTB_PROFILES["LOW"]),
        "filter_priorities": MANAGED_FILTER_PRIORITIES,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        generate_managed_htb_commands(ManagedHTBConfig(**values))


@pytest.mark.parametrize("component", ["class", "qdisc", "filter", "default"])
def test_apply_rejects_unexpected_managed_state(component):
    state = managed_state()
    if component == "class":
        state["classes"] += "class htb 1:99 parent 1:1 rate 1Mbit ceil 1Mbit\n"
    elif component == "qdisc":
        state["qdiscs"] += "qdisc ingress ffff: parent ffff:fff1\n"
    elif component == "filter":
        state["filters"] += (
            "filter protocol ip pref 90 u32 chain 0\n"
            "filter protocol ip pref 90 u32 chain 0 fh 900: order 10 *flowid 1:15\n"
            "  match 00110000/00ff0000 at 8\n"
            "  match 00000009/0000ffff at 20\n"
        )
    else:
        state["qdiscs"] = state["qdiscs"].replace("default 0x15", "default 0x14")
    runner = MockManagedTC(state)
    with pytest.raises(TCPreconditionError):
        apply_managed_htb_policy(plan_managed_htb("LOW"), runner)
    assert runner.mutations == []


def test_inspection_fails_closed_and_requires_full_expected_state():
    state = managed_state()
    runner = MockManagedTC(state)
    inspected = inspect_tc_state("s1-eth5", runner)
    from qos.tc_controller import _managed_htb_owned

    assert _managed_htb_owned(inspected)
    assert not _managed_htb_owned(type(inspected)(
        state["qdiscs"] + "qdisc ingress ffff: parent ffff:fff1\n",
        state["classes"],
        state["filters"],
    ))

    def failed_read(argv, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="inspection failed")

    with pytest.raises(TCPreconditionError, match="inspection failed"):
        apply_managed_htb_policy(plan_managed_htb("LOW"), failed_read)


def test_apply_and_partial_failure_restore_original_hierarchy():
    runner = MockManagedTC()
    commands = apply_managed_htb_policy(plan_managed_htb("LOW"), runner)
    assert len(commands) == 16
    assert runner.state == managed_state()
    assert apply_managed_htb_policy(plan_managed_htb("LOW"), runner) == []

    failed = MockManagedTC(fail_fragment="class add")
    with pytest.raises(TCApplyError, match="original hierarchy restored and verified"):
        apply_managed_htb_policy(plan_managed_htb("LOW"), failed)
    assert failed.state == baseline_state()
    assert any("handle 10:" in command for command in failed.mutations)


def test_isolated_target_applies_only_inside_identity_verified_namespace():
    target = isolated_target()
    config = ManagedHTBConfig(
        interface=target.interface,
        total_bandwidth_mbps=10,
        service_allocations_mbps=MANAGED_HTB_PROFILES["LOW"],
        filter_priorities=MANAGED_FILTER_PRIORITIES,
        isolated_target=target,
    )
    runner = IsolatedMockRunner()
    assert len(apply_managed_htb_policy(config, runner)) == 16
    assert runner.tc_commands
    assert all(command[:4] == ["ip", "netns", "exec", "qosna12345678"] for command in runner.tc_commands)
    assert not any("s1-eth5" in " ".join(command) for command in runner.tc_commands)
    assert apply_managed_htb_policy(config, runner) == []
    assert restore_isolated_managed_htb_hierarchy(target, runner)
    assert runner.tc.state == baseline_state()


def test_isolated_target_rejects_demo_and_unapproved_interfaces():
    values = isolated_target().__dict__
    with pytest.raises(ValueError, match="matched probe identity"):
        IsolatedHTBTarget(**{**values, "interface": "s1-eth5"})
    with pytest.raises(ValueError, match="matched probe identity"):
        IsolatedHTBTarget(**{**values, "interface": "eth0"})
    target = isolated_target()
    with pytest.raises(ValueError, match="does not match its isolated target"):
        ManagedHTBConfig(
            interface="s1-eth5",
            total_bandwidth_mbps=10,
            service_allocations_mbps=MANAGED_HTB_PROFILES["LOW"],
            filter_priorities=MANAGED_FILTER_PRIORITIES,
            isolated_target=target,
        )


def test_isolated_target_identity_mismatch_fails_before_any_tc_command():
    target = isolated_target()
    config = ManagedHTBConfig(
        interface=target.interface,
        total_bandwidth_mbps=10,
        service_allocations_mbps=MANAGED_HTB_PROFILES["LOW"],
        filter_priorities=MANAGED_FILTER_PRIORITIES,
        isolated_target=target,
    )
    runner = IsolatedMockRunner(namespace_inode=999)
    with pytest.raises(TCPreconditionError, match="namespace identity changed"):
        apply_managed_htb_policy(config, runner)
    assert runner.tc_commands == []


def test_managed_filter_ownership_ignores_only_valid_counter_lines():
    from qos.tc_controller import TCState, _managed_htb_owned

    state = managed_state()
    state["filters"] = state["filters"].replace(
        "  match 00001451/0000ffff at 20\n",
        "  match 00001451/0000ffff at 20\n"
        " Sent 768 bytes 8 pkt (dropped 0, overlimits 0 requeues 0)\n",
        1,
    )
    assert _managed_htb_owned(TCState(**state))

    state["filters"] = state["filters"].replace(
        "Sent 768 bytes 8 pkt (dropped 0, overlimits 0 requeues 0)",
        "Sent unknown counter values",
    )
    assert not _managed_htb_owned(TCState(**state))


def test_managed_filter_ownership_accepts_linux_kernel_u32_readback():
    from qos.tc_controller import TCState, _managed_htb_owned

    state = managed_state()
    rendered = []
    for index, port in enumerate(FLOW_PORTS):
        priority = MANAGED_FILTER_PRIORITIES[port]
        class_id = tuple(SERVICE_CLASS_IDS.values())[index]
        rendered.extend((
            f"filter protocol ip pref {priority} u32 chain 0",
            f"filter protocol ip pref {priority} u32 chain 0 fh {800 + index}: ht divisor 1",
            f"filter protocol ip pref {priority} u32 chain 0 fh {800 + index}::800 "
            f"order 2048 key ht {800 + index} bkt 0 *flowid {class_id} not_in_hw "
            "(rule hit 8 success 8)",
            "  match 00110000/00ff0000 at 8 (success 8 ) ",
            f"  match 0000{port:04x}/0000ffff at 20 (success 8 ) ",
            "  Sent 768 bytes 8 pkt (dropped 0, overlimits 0 requeues 0)",
        ))
    state["filters"] = "\n".join(rendered) + "\n"
    assert _managed_htb_owned(TCState(**state))


def test_apply_rollback_verifies_and_reports_failed_restoration():
    runner = MockManagedTC(
        fail_fragment="class add", rollback_verification_fails=True
    )
    with pytest.raises(TCApplyError, match="rollback verification also failed"):
        apply_managed_htb_policy(plan_managed_htb("LOW"), runner)
    assert any("handle 10:" in command for command in runner.mutations)
    assert "qdisc htb 1:" in runner.state["qdiscs"]


def test_profile_update_and_failed_update_restore_previous_rates():
    runner = MockManagedTC()
    apply_managed_htb_policy(plan_managed_htb("LOW"), runner)
    updated = apply_managed_htb_policy(plan_managed_htb("HIGH"), runner)
    assert len(updated) == 4
    assert runner.state == managed_state(MANAGED_HTB_PROFILES["HIGH"])

    runner.fail_fragment = "classid 1:12"
    with pytest.raises(TCApplyError, match="prior class rates restored"):
        apply_managed_htb_policy(plan_managed_htb("MEDIUM"), runner)
    assert runner.state == managed_state(MANAGED_HTB_PROFILES["HIGH"])


@pytest.mark.parametrize("restore", [
    restore_managed_htb_hierarchy,
    lambda interface, runner: restore_original_hierarchy(
        interface, MANAGED_FILTER_PRIORITIES, 5204, runner
    ),
])
def test_managed_hierarchy_restoration_is_owned_and_verified(restore):
    runner = MockManagedTC(managed_state())
    assert restore("s1-eth5", runner)
    assert runner.state == baseline_state()
    assert not restore("s1-eth5", runner)


def test_restore_refuses_unexpected_state_without_mutation():
    state = managed_state()
    state["filters"] += "unexpected filter output\n"
    runner = MockManagedTC(state)
    with pytest.raises(TCPreconditionError, match="not recognized"):
        restore_managed_htb_hierarchy("s1-eth5", runner)
    assert runner.mutations == []
