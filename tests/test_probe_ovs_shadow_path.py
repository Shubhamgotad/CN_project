import pytest

from qos.tc_controller import ShadowHTBTarget
from telemetry.probe_ovs_shadow_path import (
    APPROVAL_VALUE,
    PRODUCTION_INTERFACES,
    ShadowProbe,
    build_ovs_ofctl_argv,
    normalize_ovs_uuid,
    parse_ovs_port_counters,
    require_external_ownership_proof,
    validate_approval,
    validate_openflow_path,
    validate_ovs_instance,
    validate_ovs_port_graph,
    validate_root_shadow_veth,
    main,
)


def approval(**overrides):
    value = {
        "version": 1,
        "target_kind": "disposable_linux_vm",
        "target_id": "shadow-vm-001",
        "allow_network_mutations": True,
        "dedicated_ovs_database": True,
        "ovs_instance_uuid": "4f3a41d4-4d65-4a13-996e-112233445566",
    }
    value.update(overrides)
    return value


def test_probe_requires_explicit_cli_and_environment_approval(tmp_path):
    output = tmp_path / "evidence"

    with pytest.raises(ValueError, match="explicit disposable-VM opt-in"):
        validate_approval(
            approval(),
            environment_value=None,
            explicit_opt_in=False,
            output_directory=output,
            repository_root=tmp_path / "repo",
        )

    with pytest.raises(ValueError, match="explicit disposable-VM opt-in"):
        validate_approval(
            approval(),
            environment_value=APPROVAL_VALUE,
            explicit_opt_in=False,
            output_directory=output,
            repository_root=tmp_path / "repo",
        )

    assert validate_approval(
        approval(),
        environment_value=APPROVAL_VALUE,
        explicit_opt_in=True,
        output_directory=output,
        repository_root=tmp_path / "repo",
    ) == "shadow-vm-001"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        approval(target_kind="linux"),
        approval(allow_network_mutations=False),
        approval(dedicated_ovs_database=False),
        approval(ovs_instance_uuid="not-a-uuid"),
        approval(target_id="../production"),
        approval(version=2),
    ],
)
def test_probe_rejects_invalid_disposable_environment_attestation(tmp_path, payload):
    with pytest.raises(ValueError):
        validate_approval(
            payload,
            environment_value=APPROVAL_VALUE,
            explicit_opt_in=True,
            output_directory=tmp_path / "evidence",
            repository_root=tmp_path / "repo",
        )


def test_probe_rejects_output_inside_repository_or_existing_directory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="outside the repository"):
        validate_approval(
            approval(),
            environment_value=APPROVAL_VALUE,
            explicit_opt_in=True,
            output_directory=repo / "evidence",
            repository_root=repo,
        )
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="existing evidence"):
        validate_approval(
            approval(),
            environment_value=APPROVAL_VALUE,
            explicit_opt_in=True,
            output_directory=existing,
            repository_root=repo,
        )


def expected_graph(left_bridge="qos-left", right_bridge="qos-right"):
    return {
        left_bridge: {"qocl12345678", "qosl12345678"},
        right_bridge: {"qosr12345678", "qosv12345678"},
    }


def test_shadow_graph_accepts_only_the_single_expected_two_bridge_path():
    validate_ovs_port_graph(
        expected_graph(),
        left_bridge="qos-left",
        right_bridge="qos-right",
        expected_left_ports={"qocl12345678", "qosl12345678"},
        expected_right_ports={"qosr12345678", "qosv12345678"},
    )


@pytest.mark.parametrize(
    "graph",
    [
        {"qos-left": {"qocl12345678", "qosl12345678", "bypass"}},
        {"qos-left": {"qocl12345678"}, "qos-right": {"qosr12345678", "qosv12345678"}},
        {"qos-left": {"qocl12345678", "qosl12345678"}, "unexpected": set()},
        {
            "qos-left": {"qocl12345678", "qosl12345678"},
            "qos-right": {"qosr12345678", "qosv12345678", "s1-eth5"},
        },
    ],
)
def test_shadow_graph_rejects_bypass_or_production_topology(graph):
    with pytest.raises(ValueError):
        validate_ovs_port_graph(
            graph,
            left_bridge="qos-left",
            right_bridge="qos-right",
            expected_left_ports={"qocl12345678", "qosl12345678"},
            expected_right_ports={"qosr12345678", "qosv12345678"},
        )


def test_shadow_topology_never_accepts_production_interfaces():
    assert PRODUCTION_INTERFACES == {"s1-eth5", "s2-eth1"}


def test_root_shadow_veth_requires_exact_peer_identity_and_bridge_attachment():
    record = {
        "ifname": "qocl12345678",
        "ifindex": 10,
        "link": "qoch12345678",
        "link_index": 11,
        "master": "qos-left",
        "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
        "linkinfo": {"info_kind": "veth"},
    }
    validate_root_shadow_veth(
        record,
        interface="qocl12345678",
        peer_interface="qoch12345678",
        bridge="qos-left",
        ifindex=10,
        peer_ifindex=11,
    )
    for field, replacement in (
        ("ifindex", 99),
        ("link", "unexpected-peer"),
        ("link_index", 99),
        ("master", "unexpected-bridge"),
        ("flags", ["BROADCAST"]),
        ("linkinfo", {"info_kind": "dummy"}),
    ):
        changed = {**record, field: replacement}
        with pytest.raises(RuntimeError, match="unexpected identity or attachment"):
            validate_root_shadow_veth(
                changed,
                interface="qocl12345678",
                peer_interface="qoch12345678",
                bridge="qos-left",
                ifindex=10,
                peer_ifindex=11,
            )


def test_openflow_validation_rejects_extra_or_missing_bypass_rules():
    expected = {(1, 2), (2, 1)}
    validate_openflow_path(
        " cookie=0x0, table=0, priority=100,in_port=1 actions=output:2\n"
        " cookie=0x0, table=0, priority=100,in_port=2 actions=output:1\n",
        expected,
    )
    with pytest.raises(ValueError, match="single approved shadow path"):
        validate_openflow_path(
            " table=0, priority=100,in_port=1 actions=output:2\n"
            " table=0, priority=100,in_port=3 actions=output:4\n",
            expected,
        )
    with pytest.raises(ValueError, match="unexpected forwarding action"):
        validate_openflow_path(
            " table=0, priority=100,in_port=1 actions=output:2\n"
            " table=0, priority=0 actions=NORMAL\n",
            expected,
        )


def test_ovs_counter_parser_captures_packet_totals():
    parsed = parse_ovs_port_counters(
        "OFPST_PORT reply\n"
        "  port 1: rx pkts=10, bytes=100, drop=0, errs=0, frame=0, over=0, crc=0\n"
        "           tx pkts=9, bytes=90, drop=0, errs=0, coll=0\n"
        "  port 2: rx pkts=4, bytes=40, drop=0, errs=0, frame=0, over=0, crc=0\n"
        "           tx pkts=3, bytes=30, drop=0, errs=0, coll=0\n"
    )
    assert parsed == {
        1: {"rx_packets": 10, "tx_packets": 9},
        2: {"rx_packets": 4, "tx_packets": 3},
    }


def test_ovs_ofctl_places_switch_endpoint_before_operation_arguments(tmp_path):
    endpoint = tmp_path / "shadow.mgmt"
    assert build_ovs_ofctl_argv(
        endpoint,
        "add-flow",
        "priority=100,in_port=1,actions=output:2",
    ) == [
        "ovs-ofctl",
        "-O",
        "OpenFlow13",
        "add-flow",
        f"unix:{endpoint}",
        "priority=100,in_port=1,actions=output:2",
    ]


def test_ovs_ofctl_requires_an_operation(tmp_path):
    with pytest.raises(ValueError, match="operation is required"):
        build_ovs_ofctl_argv(tmp_path / "shadow.mgmt")


def test_ovs_instance_requires_approved_socket_directory_and_database_uuid(tmp_path):
    database = tmp_path / "ovs" / "db.sock"
    management = tmp_path / "ovs" / "run"
    management.mkdir(parents=True)
    payload = approval(
        ovs_db_socket=str(database.resolve()),
        ovs_ofctl_dir=str(management.resolve()),
    )
    observed = "UUID: 4f3a41d4-4d65-4a13-996e-112233445566"

    assert validate_ovs_instance(
        payload,
        db_socket=database,
        management_directory=management,
        actual_uuid=observed,
    ) == "4f3a41d4-4d65-4a13-996e-112233445566"
    with pytest.raises(ValueError, match="does not match"):
        validate_ovs_instance(
            payload,
            db_socket=tmp_path / "other.sock",
            management_directory=management,
            actual_uuid=observed,
        )
    with pytest.raises(ValueError, match="UUID does not match"):
        validate_ovs_instance(
            payload,
            db_socket=database,
            management_directory=management,
            actual_uuid="00000000-0000-0000-0000-000000000001",
        )


def test_ovs_uuid_normalization_handles_vsctl_output():
    assert normalize_ovs_uuid(
        "UUID: 4f3a41d4-4d65-4a13-996e-112233445566"
    ) == "4f3a41d4-4d65-4a13-996e-112233445566"
    assert normalize_ovs_uuid("invalid") is None


def test_preflight_is_read_only_and_print_manifest_without_creating_resources(
    tmp_path, monkeypatch
):
    output = tmp_path / "evidence"
    management = tmp_path / "ovs-run"
    management.mkdir()
    database = tmp_path / "db.sock"
    approval_payload = approval(
        ovs_db_socket=str(database.resolve()),
        ovs_ofctl_dir=str(management.resolve()),
    )
    probe = ShadowProbe(output, database, management, approval_payload)
    observed_commands = []

    monkeypatch.setattr("telemetry.probe_ovs_shadow_path.sys.platform", "linux")
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path.os.geteuid", lambda: 0, raising=False
    )
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path.shutil.which", lambda executable: f"/bin/{executable}"
    )
    monkeypatch.setattr(
        type(database),
        "is_socket",
        lambda path: path == database,
    )

    def fake_run(argv, *, allow_failure=False, record=True):
        observed_commands.append((argv, record))
        if "list-br" in argv:
            stdout = ""
        elif "Open_vSwitch" in argv:
            stdout = approval_payload["ovs_instance_uuid"] + "\n"
        elif argv[0:3] == ["ip", "netns", "list"]:
            stdout = ""
        elif argv[0:4] == ["ip", "-j", "link", "show"]:
            stdout = "[]"
        else:
            raise AssertionError(f"unexpected preflight command: {argv}")
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr(probe, "run", fake_run)

    result = probe.preflight("approved-shadow")

    assert result["status"] == "preflight_passed"
    assert result["resources_created"] is False
    assert result["network_mutations"] is False
    assert result["managed_target"]["namespace"].startswith("qoso")
    assert result["managed_target"]["egress_interface"].startswith("qosp")
    assert not output.exists()
    assert not probe.steps_path.exists()
    assert all(record is False for _, record in observed_commands)
    assert all(
        not any(token in argv for token in ("add-br", "add-port", "add", "set"))
        for argv, _ in observed_commands
    )


def test_preflight_fails_closed_on_existing_ovs_bridge(tmp_path, monkeypatch):
    management = tmp_path / "ovs-run"
    management.mkdir()
    database = tmp_path / "db.sock"
    approval_payload = approval(
        ovs_db_socket=str(database.resolve()),
        ovs_ofctl_dir=str(management.resolve()),
    )
    probe = ShadowProbe(tmp_path / "evidence", database, management, approval_payload)
    monkeypatch.setattr("telemetry.probe_ovs_shadow_path.sys.platform", "linux")
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path.os.geteuid", lambda: 0, raising=False
    )
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path.shutil.which", lambda executable: f"/bin/{executable}"
    )
    monkeypatch.setattr(type(database), "is_socket", lambda path: path == database)

    def fake_run(argv, *, allow_failure=False, record=True):
        if "list-br" in argv:
            return type("Result", (), {"returncode": 0, "stdout": "br-existing\n", "stderr": ""})()
        raise AssertionError("preflight must reject existing bridges before further inspection")

    monkeypatch.setattr(probe, "run", fake_run)
    with pytest.raises(RuntimeError, match="existing bridges"):
        probe.preflight("approved-shadow")


def test_preflight_fails_closed_on_existing_management_socket_entry(tmp_path, monkeypatch):
    management = tmp_path / "ovs-run"
    management.mkdir()
    (management / "orphan.mgmt").touch()
    database = tmp_path / "db.sock"
    payload = approval(
        ovs_db_socket=str(database.resolve()),
        ovs_ofctl_dir=str(management.resolve()),
    )
    probe = ShadowProbe(tmp_path / "evidence", database, management, payload)
    monkeypatch.setattr("telemetry.probe_ovs_shadow_path.sys.platform", "linux")
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path.os.geteuid", lambda: 0, raising=False
    )
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path.shutil.which", lambda executable: f"/bin/{executable}"
    )
    monkeypatch.setattr(type(database), "is_socket", lambda path: path == database)

    with pytest.raises(RuntimeError, match="unexpected bridge sockets"):
        probe.preflight("approved-shadow")


def test_preflight_cli_does_not_create_evidence_directory(tmp_path, monkeypatch, capsys):
    output = tmp_path / "evidence"
    database = tmp_path / "db.sock"
    database.touch()
    management = tmp_path / "ovs-run"
    management.mkdir()
    payload = approval(
        ovs_db_socket=str(database.resolve()),
        ovs_ofctl_dir=str(management.resolve()),
    )
    monkeypatch.setenv("ADAPTIVE_QOS_SHADOW_APPROVAL", APPROVAL_VALUE)
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path._approval_file", lambda path: payload
    )
    monkeypatch.setattr(type(database), "is_socket", lambda path: path == database)
    monkeypatch.setattr(
        ShadowProbe,
        "preflight",
        lambda self, target_id: {
            "status": "preflight_passed",
            "target_id": target_id,
            "network_mutations": False,
        },
    )

    result = main([
        str(output),
        "--approval-file",
        str(tmp_path / "approval.json"),
        "--ovs-db",
        str(database),
        "--ovs-ofctl-dir",
        str(management),
        "--i-approve-shadow-only-network-mutations",
        "--preflight-only",
    ])

    assert result == 0
    assert not output.exists()
    assert '"network_mutations": false' in capsys.readouterr().out


def test_mutation_cli_rechecks_preflight_before_probe_without_network_calls(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "evidence"
    database = tmp_path / "db.sock"
    database.touch()
    management = tmp_path / "ovs-run"
    management.mkdir()
    payload = approval(
        ovs_db_socket=str(database.resolve()),
        ovs_ofctl_dir=str(management.resolve()),
    )
    preflight_ids = []
    monkeypatch.setenv("ADAPTIVE_QOS_SHADOW_APPROVAL", APPROVAL_VALUE)
    monkeypatch.setattr(
        "telemetry.probe_ovs_shadow_path._approval_file", lambda path: payload
    )
    monkeypatch.setattr(type(database), "is_socket", lambda path: path == database)
    monkeypatch.setattr(
        ShadowProbe,
        "preflight",
        lambda self, target_id: preflight_ids.append(target_id)
        or {"status": "preflight_passed", "network_mutations": False},
    )
    monkeypatch.setattr(
        ShadowProbe,
        "create",
        lambda self: setattr(
            self,
            "target",
            ShadowHTBTarget(
                namespace="qoso12345678",
                interface="qosp12345678",
                namespace_inode=1,
                ifindex=2,
                ingress_interface="qosi12345678",
                ingress_ifindex=3,
                ingress_peer_interface="qosl12345678",
                ingress_peer_ifindex=4,
                egress_peer_interface="qosr12345678",
                egress_peer_ifindex=5,
            ),
        ),
    )
    monkeypatch.setattr(ShadowProbe, "snapshot_ovs", lambda self: {})
    monkeypatch.setattr(ShadowProbe, "run_profiles", lambda self: {})
    monkeypatch.setattr(
        ShadowProbe,
        "cleanup",
        lambda self: {"verified": True, "bridges_removed": []},
    )

    result = main([
        str(output),
        "--approval-file",
        str(tmp_path / "approval.json"),
        "--ovs-db",
        str(database),
        "--ovs-ofctl-dir",
        str(management),
        "--i-approve-shadow-only-network-mutations",
    ])

    assert result == 0
    assert preflight_ids == ["shadow-vm-001", "shadow-vm-001"]
    assert (output / "result.json").is_file()
    assert '"status": "passed_path_and_controller_probe_ownership_unproven"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "proof",
    [
        None,
        {},
        {"boundary": "network_namespace", "external_privileged_access_denied": True, "reviewed_by": "reviewer"},
        {"boundary": "separate_administrative_domain", "external_privileged_access_denied": False, "reviewed_by": "reviewer"},
        {"boundary": "separate_administrative_domain", "external_privileged_access_denied": True},
    ],
)
def test_probe_fails_closed_without_independent_privileged_writer_proof(proof):
    with pytest.raises(ValueError, match="ownership proof is missing"):
        require_external_ownership_proof(proof)


def test_probe_accepts_only_reviewed_separate_boundary_writer_evidence():
    require_external_ownership_proof({
        "boundary": "separate_administrative_domain",
        "external_privileged_access_denied": True,
        "reviewed_by": "independent-operator",
    })
