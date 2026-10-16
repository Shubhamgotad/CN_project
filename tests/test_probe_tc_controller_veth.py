import json
from types import SimpleNamespace

import pytest

from telemetry import probe_tc_controller_veth as probe_module


def test_partial_pair_creation_failure_attempts_cleanup_but_refuses_unknown_owner(
    tmp_path,
):
    output = tmp_path / "evidence"
    output.mkdir()
    probe = probe_module.Probe(output)
    calls = []

    def fake_run(argv, *, stage, execute=True, override=None):
        calls.append((stage, argv))
        if argv == ["ip", "-j", "link", "show"]:
            rows = [{"ifname": probe.target}] if len(calls) > 1 else []
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(rows), stderr=""
            )
        if argv[:3] == ["ip", "link", "add"]:
            return SimpleNamespace(
                returncode=2, stdout="", stderr="injected partial creation failure"
            )
        raise AssertionError(f"unexpected command: {argv}")

    probe.run = fake_run
    with pytest.raises(RuntimeError, match="create-disposable-veth"):
        probe.create()

    assert probe.creation_attempted
    assert not probe.created
    with pytest.raises(RuntimeError, match="incomplete veth pair"):
        probe.cleanup()
    assert any(stage == "cleanup-check-interfaces" for stage, _ in calls)
    assert not any(argv[:3] == ["ip", "link", "del"] for _, argv in calls)


def test_snapshot_failure_after_pair_creation_still_cleans_verified_pair(tmp_path):
    output = tmp_path / "evidence"
    output.mkdir()
    probe = probe_module.Probe(output)
    calls = []
    pair_created = False
    deleted = False
    snapshot_failed = False

    def fake_run(argv, *, stage, execute=True, override=None):
        nonlocal pair_created, deleted, snapshot_failed
        calls.append((stage, argv))
        if argv == ["ip", "-j", "link", "show"]:
            names = [] if deleted or not pair_created else [
                {"ifname": probe.target}, {"ifname": probe.peer}
            ]
            return SimpleNamespace(returncode=0, stdout=json.dumps(names), stderr="")
        if argv[:3] == ["ip", "link", "add"]:
            pair_created = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["tc", "-d", "qdisc"] and not snapshot_failed:
            snapshot_failed = True
            return SimpleNamespace(returncode=2, stdout="", stderr="injected read failure")
        if argv[:3] == ["tc", "-d", "qdisc"]:
            return SimpleNamespace(
                returncode=0, stdout="qdisc noqueue 0: root refcnt 2\n", stderr=""
            )
        if argv[:3] in (["tc", "-d", "class"], ["tc", "-s", "filter"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:4] == ["ip", "-j", "-details", "link"]:
            endpoint = argv[-1]
            peer = probe.peer if endpoint == probe.target else probe.target
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{
                    "ifname": endpoint,
                    "link": peer,
                    "linkinfo": {"info_kind": "veth"},
                }]),
                stderr="",
            )
        if argv[:4] == ["ip", "-j", "addr", "show"]:
            endpoint = argv[-1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"ifname": endpoint, "addr_info": []}]),
                stderr="",
            )
        if argv[:1] == ["ovs-vsctl"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["test", "!", "-e"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:4] == ["ip", "link", "del", "dev"]:
            deleted = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    probe.run = fake_run
    with pytest.raises(RuntimeError, match="cannot capture original qdiscs"):
        probe.create()

    result = probe.cleanup()
    assert result["verified"]
    assert probe.creation_attempted
    assert not probe.created
    assert deleted
    assert any(stage == "cleanup-check-interfaces" for stage, _ in calls)


def test_pair_verification_rejects_wrong_peer_identity(tmp_path):
    output = tmp_path / "evidence"
    output.mkdir()
    probe = probe_module.Probe(output)

    def fake_run(argv, *, stage, execute=True, override=None):
        if argv[:4] == ["ip", "-j", "-details", "link"]:
            endpoint = argv[-1]
            peer = "wrong-peer" if endpoint == probe.target else probe.target
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{
                    "ifname": endpoint,
                    "link": peer,
                    "linkinfo": {"info_kind": "veth"},
                }]),
                stderr="",
            )
        if argv[:4] == ["ip", "-j", "addr", "show"]:
            endpoint = argv[-1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"ifname": endpoint, "addr_info": []}]),
                stderr="",
            )
        if argv[:1] == ["ovs-vsctl"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    probe.run = fake_run
    with pytest.raises(RuntimeError, match="endpoints do not point to each other"):
        probe.verify_pair()


def test_main_reports_cleanup_failure_without_hiding_creation_failure(
    monkeypatch, tmp_path
):
    output = tmp_path / "new-evidence"
    cleanup_calls = []

    def fail_create(probe):
        probe.creation_attempted = True
        raise RuntimeError("original creation failure")

    def fail_cleanup(probe, original=None):
        cleanup_calls.append(original)
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(probe_module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(probe_module.sys, "argv", [
        "probe_tc_controller_veth.py", str(output)
    ])
    monkeypatch.setattr(probe_module.Probe, "create", fail_create)
    monkeypatch.setattr(probe_module.Probe, "cleanup", fail_cleanup)

    assert probe_module.main() == 2
    report = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert report["error"] == "RuntimeError: original creation failure"
    assert report["cleanup"]["error"] == "RuntimeError: injected cleanup failure"
    assert report["status"] == "cleanup-failed"
    assert cleanup_calls == [None]
