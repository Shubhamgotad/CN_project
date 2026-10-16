import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import qos.tc_controller as tc_controller
from qos.tc_controller import (
    FLOW_CLASS_IDS,
    FLOW_PORTS,
    NETEM_HANDLES,
    TCApplyError,
    TCPreconditionError,
    apply_tc_policy,
    apply_tc_commands,
    restore_original_hierarchy,
)


PRIORITIES = {port: index + 11 for index, port in enumerate(FLOW_PORTS)}
DEFAULT_PORT = 5204
RATES = {port: 2.5 for port in FLOW_PORTS}


def baseline_state(filters=""):
    return {
        "qdiscs": (
            "qdisc htb 5: root refcnt 1 r2q 10 default 0x1 direct_packets_stat 0\n"
            "qdisc netem 10: parent 5:1 limit 1000 delay 10ms\n"
        ),
        "classes": (
            "class htb 5:1 root leaf 10: prio 0 rate 10Mbit ceil 10Mbit\n"
        ),
        "filters": filters,
    }


def managed_state(rates=RATES, priorities=PRIORITIES, default_port=DEFAULT_PORT):
    default_minor = FLOW_CLASS_IDS[default_port].split(":")[1]
    qdiscs = [
        "qdisc htb 5: root refcnt 1 r2q 10 default 0x1 direct_packets_stat 0",
        f"qdisc htb 1: parent 5:1 r2q 10 default 0x{default_minor} direct_packets_stat 0",
    ]
    classes = [
        "class htb 5:1 root leaf 1: prio 0 rate 10Mbit ceil 10Mbit",
        "class htb 1:1 root rate 10Mbit ceil 10Mbit",
    ]
    filters = []
    for index, port in enumerate(FLOW_PORTS):
        class_id = FLOW_CLASS_IDS[port]
        minor = class_id.split(":")[1]
        handle = NETEM_HANDLES[port]
        rate = rates[port]
        qdiscs.append(
            f"qdisc netem {handle} parent {class_id} limit 1000 delay 10ms"
        )
        classes.append(
            f"class htb {class_id} parent 1:1 leaf {handle} prio 0 "
            f"rate {rate:g}Mbit ceil {rate:g}Mbit"
        )
        priority = priorities[port]
        filters.extend((
            f"filter protocol ip pref {priority} u32 chain 0",
            f"filter protocol ip pref {priority} u32 chain 0 fh {800 + index}: "
            f"order 2048 *flowid {class_id} not_in_hw",
            "  match 00110000/00ff0000 at 8",
            f"  match 0000{port:04x}/0000ffff at 20",
        ))
    return {
        "qdiscs": "\n".join(qdiscs) + "\n",
        "classes": "\n".join(classes) + "\n",
        "filters": "\n".join(filters) + "\n",
    }


class MockTC:
    def __init__(
        self, state=None, fail_fragment=None, rollback_fails=False,
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
            if "parent" in argv:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
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
            return SimpleNamespace(returncode=2, stdout="", stderr="injected rollback failure")
        if "qdisc replace" in command and "handle 1:" in command:
            self.state = managed_state()
        elif "qdisc replace" in command and "handle 10:" in command:
            if not self.rollback_verification_fails:
                self.state = baseline_state()
        elif "class change" in command:
            pieces = command.split()
            port = next(port for port, class_id in FLOW_CLASS_IDS.items() if class_id in pieces)
            rate = float(pieces[pieces.index("rate") + 1].removesuffix("mbit"))
            updated = {p: _rate_from_mock(self.state["classes"], p) for p in FLOW_PORTS}
            updated[port] = rate
            self.state = managed_state(updated)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _rate_from_mock(text, port):
    class_id = FLOW_CLASS_IDS[port]
    line = next(line for line in text.splitlines() if line.startswith(f"class htb {class_id} "))
    return float(line.split("rate ")[1].split("Mbit")[0])


def _apply(runner, rates=RATES):
    return apply_tc_policy("s1-eth5", 10, rates, PRIORITIES, DEFAULT_PORT, runner)


def test_preconditions_reject_root_delay_class_or_filters_drift():
    for state in (
        {**baseline_state(), "qdiscs": baseline_state()["qdiscs"].replace("5: root", "1: root")},
        {**baseline_state(), "qdiscs": baseline_state()["qdiscs"].replace("10ms", "9ms")},
        {**baseline_state(), "classes": baseline_state()["classes"].replace("10Mbit", "9Mbit")},
        {**baseline_state(), "filters": "unexpected filter object\n"},
    ):
        runner = MockTC(state)
        with pytest.raises(TCPreconditionError):
            _apply(runner)
        assert runner.mutations == []


@pytest.mark.parametrize(
    ("field", "unexpected_line"),
    [
        ("qdiscs", "UNEXPECTED OUTPUT"),
        ("classes", "UNEXPECTED OUTPUT"),
    ],
)
def test_baseline_rejects_unexpected_output_lines(field, unexpected_line):
    state = baseline_state()
    state[field] += unexpected_line + "\n"
    assert not tc_controller._baseline_matches(tc_controller.TCState(**state))


def test_baseline_accepts_exact_expected_output():
    assert tc_controller._baseline_matches(
        tc_controller.TCState(**baseline_state())
    )


@pytest.fixture
def representative_tc_output():
    managed = managed_state()
    duplicate_class_output = (
        "class htb 1:1 root rate 10Mbit ceil 10Mbit\n"
        + "".join(
            line + "\n"
            for line in managed["classes"].splitlines()
            if line.startswith("class htb 1:")
        )
    )
    return {
        "qdiscs": managed["qdiscs"],
        "classes": managed["classes"],
        "overlapping_classes": duplicate_class_output,
        "filters": managed["filters"],
        "baseline": baseline_state(),
    }


class FixtureTC:
    def __init__(self, fixture, *, filters=None):
        self.fixture = fixture
        self.filters = fixture["filters"] if filters is None else filters
        self.commands = []

    def __call__(self, argv, capture_output, text, check):
        self.commands.append(argv)
        if argv[:3] == ["tc", "-d", "qdisc"]:
            output = self.fixture["baseline"]["qdiscs"] if self.filters == "" else self.fixture["qdiscs"]
        elif argv[:3] == ["tc", "-d", "class"]:
            if "parent" in argv:
                output = self.fixture["overlapping_classes"]
            else:
                output = self.fixture["baseline"]["classes"] if self.filters == "" else self.fixture["classes"]
        elif argv[:3] == ["tc", "-s", "filter"]:
            output = self.filters if "parent" in argv else ""
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


def test_inspect_tc_state_uses_one_full_class_listing_and_validates_fixture(
    representative_tc_output,
):
    runner = FixtureTC(representative_tc_output)
    state = tc_controller.inspect_tc_state("s1-eth5", runner)

    class_queries = [
        argv for argv in runner.commands
        if argv[:3] == ["tc", "-d", "class"]
    ]
    assert class_queries == [["tc", "-d", "class", "show", "dev", "s1-eth5"]]
    class_ids = [
        line.split()[2]
        for line in state.classes.splitlines()
        if line.startswith("class htb ")
    ]
    overlapping_class_ids = [
        line.split()[2]
        for line in representative_tc_output["overlapping_classes"].splitlines()
        if line.startswith("class htb ")
    ]
    assert len(overlapping_class_ids) > len(set(overlapping_class_ids))
    assert len(class_ids) == len(set(class_ids)) == 6
    assert tc_controller._managed_matches(
        state, RATES, PRIORITIES, DEFAULT_PORT
    )


@pytest.mark.parametrize("filter_mutation", [
    lambda text: text + (
        "filter protocol ip pref 99 u32 chain 0\n"
        "filter protocol ip pref 99 u32 chain 0 fh 999: order 2048 "
        "*flowid 1:99 not_in_hw\n"
        "  match 00110000/00ff0000 at 8\n"
        "  match 00000009/0000ffff at 20\n"
    ),
    lambda text: text.replace(
        "  match 00110000/00ff0000 at 8", "  malformed match output"
    ),
    lambda text: "unexpected filter output\n" + text,
])
def test_filter_inspection_rejects_extra_or_malformed_entries(
    representative_tc_output, filter_mutation
):
    state = managed_state()
    state = {
        **state,
        "filters": filter_mutation(representative_tc_output["filters"]),
    }
    assert not tc_controller._managed_matches(
        tc_controller.TCState(**state), RATES, PRIORITIES, DEFAULT_PORT
    )


def test_inspection_preserves_valid_baseline_state(representative_tc_output):
    class BaselineFixtureTC(FixtureTC):
        def __call__(self, argv, capture_output, text, check):
            self.commands.append(argv)
            if argv[:3] == ["tc", "-d", "qdisc"]:
                output = self.fixture["baseline"]["qdiscs"]
            elif argv[:3] == ["tc", "-d", "class"]:
                output = self.fixture["baseline"]["classes"]
            elif argv[:3] == ["tc", "-s", "filter"]:
                output = ""
            else:
                output = ""
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

    state = tc_controller.inspect_tc_state(
        "s1-eth5", BaselineFixtureTC(representative_tc_output, filters="")
    )
    assert tc_controller._baseline_matches(state)


def test_interface_lock_uses_cross_process_windows_byte_lock(monkeypatch, tmp_path):
    calls = []

    class FakeMSVCRT:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, size):
            calls.append((mode, size))
            assert size == 1
            assert os.fstat(fd).st_size >= 1

    monkeypatch.setattr(tc_controller.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(tc_controller, "fcntl", None)
    monkeypatch.setattr(tc_controller, "msvcrt", FakeMSVCRT)

    with tc_controller._interface_lock("qosp1234"):
        assert calls == [(FakeMSVCRT.LK_LOCK, 1)]

    assert calls == [
        (FakeMSVCRT.LK_LOCK, 1),
        (FakeMSVCRT.LK_UNLCK, 1),
    ]


def test_interface_lock_uses_posix_flock_when_available(monkeypatch, tmp_path):
    calls = []

    class FakeFCNTL:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(fd, operation):
            calls.append(operation)

    monkeypatch.setattr(tc_controller.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(tc_controller, "fcntl", FakeFCNTL)
    monkeypatch.setattr(tc_controller, "msvcrt", None)

    with tc_controller._interface_lock("qosp1234"):
        assert calls == [FakeFCNTL.LOCK_EX]

    assert calls == [FakeFCNTL.LOCK_EX, FakeFCNTL.LOCK_UN]


def test_lock_failure_prevents_all_tc_commands(monkeypatch, tmp_path):
    class FailingMSVCRT:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd, mode, size):
            raise OSError("injected lock failure")

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tc_controller.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(tc_controller, "fcntl", None)
    monkeypatch.setattr(tc_controller, "msvcrt", FailingMSVCRT)

    with pytest.raises(TCApplyError, match="could not obtain cross-process"):
        _apply(runner)

    assert calls == []


def test_lock_failure_without_cross_process_backend_fails_closed(
    monkeypatch, tmp_path
):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tc_controller.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(tc_controller, "fcntl", None)
    monkeypatch.setattr(tc_controller, "msvcrt", None)

    with pytest.raises(TCApplyError, match="requires cross-process"):
        _apply(runner)

    assert calls == []


def test_lock_acquisition_failure_closes_open_lock_file(monkeypatch, tmp_path):
    opened_files = []
    original_open = Path.open

    def tracked_open(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        opened_files.append(file)
        return file

    class FailingFCNTL:
        LOCK_EX = 1

        @staticmethod
        def flock(fd, operation):
            raise OSError("injected lock failure")

    monkeypatch.setattr(tc_controller.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(tc_controller, "fcntl", FailingFCNTL)
    monkeypatch.setattr(tc_controller, "msvcrt", None)

    with pytest.raises(TCApplyError, match="could not obtain cross-process"):
        with tc_controller._interface_lock("qosp1234"):
            pytest.fail("lock acquisition unexpectedly succeeded")

    assert len(opened_files) == 1
    assert opened_files[0].closed


def test_interface_lock_contends_across_subprocesses(monkeypatch, tmp_path):
    if tc_controller.fcntl is None and tc_controller.msvcrt is None:
        pytest.skip("no supported cross-process file-lock backend")
    monkeypatch.setattr(tc_controller.tempfile, "tempdir", str(tmp_path))
    interface = "qosp1234"
    script = r"""
import sys
from pathlib import Path
import tempfile
import qos.tc_controller as controller

interface = "qosp1234"
path = Path(tempfile.gettempdir()) / f"cn_adaptive_qos_{interface}.lock"
with path.open("a+b") as lock_file:
    if controller.fcntl is not None:
        try:
            controller.fcntl.flock(
                lock_file.fileno(),
                controller.fcntl.LOCK_EX | controller.fcntl.LOCK_NB,
            )
        except BlockingIOError:
            print("READY", flush=True)
            print("BLOCKED", flush=True)
        else:
            controller.fcntl.flock(lock_file.fileno(), controller.fcntl.LOCK_UN)
            print("READY", flush=True)
            print("NOT_BLOCKED", flush=True)
    elif controller.msvcrt is not None:
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            controller.msvcrt.locking(
                lock_file.fileno(), controller.msvcrt.LK_NBLCK, 1
            )
        except OSError:
            print("READY", flush=True)
            print("BLOCKED", flush=True)
        else:
            lock_file.seek(0)
            controller.msvcrt.locking(
                lock_file.fileno(), controller.msvcrt.LK_UNLCK, 1
            )
            print("READY", flush=True)
            print("NOT_BLOCKED", flush=True)

with controller._interface_lock(interface):
    print("ENTERED", flush=True)
"""
    environment = os.environ.copy()
    environment["TEMP"] = str(tmp_path)
    environment["TMP"] = str(tmp_path)
    project_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = project_root + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    messages = queue.Queue()
    process = None
    stderr = ""
    with tc_controller._interface_lock(interface):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=project_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def read_stdout():
            for line in process.stdout:
                messages.put(line.strip())

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            assert messages.get(timeout=10) == "READY"
            assert messages.get(timeout=10) == "BLOCKED"
        except Exception:
            process.kill()
            process.communicate(timeout=5)
            raise

    try:
        assert messages.get(timeout=10) == "ENTERED"
        _, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    reader.join(timeout=5)


def test_disposable_veth_seam_requires_probe_name_and_veth_json():
    class VethRunner(MockTC):
        def __call__(self, argv, capture_output, text, check):
            if argv[:4] == ["ip", "-j", "-details", "link"]:
                interface = argv[-1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{
                        "ifname": interface,
                        "link": "qosq0123abcd" if interface.startswith("qosp") else "qosp0123abcd",
                        "linkinfo": {"info_kind": "veth"},
                    }]),
                    stderr="",
                )
            if argv[:4] == ["ip", "-j", "addr", "show"]:
                interface = argv[-1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{"ifname": interface, "addr_info": []}]),
                    stderr="",
                )
            if argv[:1] == ["ovs-vsctl"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return super().__call__(argv, capture_output, text, check)

    runner = VethRunner()
    apply_tc_policy(
        "qosp0123abcd", 10, RATES, PRIORITIES, DEFAULT_PORT,
        runner=runner, allow_disposable_veth=True,
    )
    assert runner.mutations
    assert all("dev qosp0123abcd" in command for command in runner.mutations)

    runner = VethRunner()
    original = runner.__call__

    def invalid_peer(argv, capture_output, text, check):
        if argv[:4] == ["ip", "-j", "-details", "link"] and argv[-1] == "qosq0123abcd":
            return SimpleNamespace(
                returncode=0,
                stdout='[{"ifname":"qosq0123abcd","link":"other0","linkinfo":{"info_kind":"veth"}}]',
                stderr="",
            )
        return original(argv, capture_output, text, check)

    with pytest.raises(TCPreconditionError, match="expected unmastered disposable veth pair"):
        apply_tc_policy(
            "qosp0123abcd", 10, RATES, PRIORITIES, DEFAULT_PORT,
            runner=invalid_peer,
            allow_disposable_veth=True,
        )


def test_first_apply_is_nested_and_repeated_apply_is_idempotent():
    runner = MockTC()
    commands = _apply(runner)
    assert commands
    assert "tc qdisc replace dev s1-eth5 parent 5:1 handle 1: htb" in runner.mutations[0]
    assert not any("root handle" in command for command in runner.mutations)
    assert _apply(runner) == []
    assert len(runner.mutations) == len(commands)


def test_class_rate_update_changes_only_owned_classes():
    runner = MockTC()
    _apply(runner)
    before = len(runner.mutations)
    revised = {5201: 3.0, 5202: 2.0, 5203: 2.5, 5204: 2.5}
    _apply(runner, revised)
    changes = runner.mutations[before:]
    assert len(changes) == 2
    assert all("class change" in command for command in changes)
    assert all("parent 1:1" in command for command in changes)
    assert runner.state == managed_state(revised)


def test_class_rate_rollback_restores_and_verifies_expected_partial_state():
    runner = MockTC()
    _apply(runner)
    runner.fail_fragment = "classid 1:12"
    revised = {5201: 3.0, 5202: 2.0, 5203: 2.5, 5204: 2.5}

    with pytest.raises(TCApplyError, match="prior class rates restored"):
        _apply(runner, revised)

    assert runner.state == managed_state(RATES)


def test_class_rate_rollback_refuses_unexpected_external_change():
    runner = MockTC()
    _apply(runner)
    runner.fail_fragment = "classid 1:12"
    revised = {5201: 3.0, 5202: 2.0, 5203: 2.5, 5204: 2.5}
    failed = False

    def external_change(argv, capture_output, text, check):
        nonlocal failed
        result = runner(argv, capture_output, text, check)
        if result.stderr == "injected failure" and not failed:
            failed = True
            runner.state["qdiscs"] += "qdisc ingress ffff: parent ffff:fff1\n"
        return result

    with pytest.raises(TCApplyError) as exc_info:
        _apply(external_change, revised)

    message = str(exc_info.value)
    assert "injected failure" in message
    assert "rollback was refused" in message
    assert runner.state["qdiscs"].endswith(
        "qdisc ingress ffff: parent ffff:fff1\n"
    )
    assert sum("class change" in command for command in runner.mutations) == 2


def test_partial_apply_failure_restores_and_verifies_original_child():
    runner = MockTC(fail_fragment="class add")
    with pytest.raises(TCApplyError, match="restored and verified"):
        _apply(runner)
    assert runner.state == baseline_state()
    assert any("handle 10:" in command for command in runner.mutations)
    assert not any("root handle" in command or "qdisc del" in command for command in runner.mutations)


def test_apply_rollback_refuses_unexpected_state_change():
    runner = MockTC(fail_fragment="class add")

    def external_change(argv, capture_output, text, check):
        result = runner(argv, capture_output, text, check)
        if result.stderr == "injected failure":
            runner.state["qdiscs"] += "qdisc ingress ffff: parent ffff:fff1\n"
        return result

    with pytest.raises(TCApplyError) as exc_info:
        _apply(external_change)

    message = str(exc_info.value)
    assert "injected failure" in message
    assert "rollback was refused" in message
    assert "state changed since the last controller-owned state" in message
    assert runner.state["qdiscs"].endswith(
        "qdisc ingress ffff: parent ffff:fff1\n"
    )
    assert not any("handle 10:" in command for command in runner.mutations)


def test_apply_rollback_fails_closed_when_state_inspection_fails():
    runner = MockTC(fail_fragment="class add")
    fail_inspection = False

    def failing_inspection(argv, capture_output, text, check):
        nonlocal fail_inspection
        if fail_inspection and argv[:2] == ["tc", "-d"]:
            return SimpleNamespace(
                returncode=2, stdout="", stderr="injected inspection failure"
            )
        result = runner(argv, capture_output, text, check)
        if result.stderr == "injected failure":
            fail_inspection = True
        return result

    with pytest.raises(TCApplyError) as exc_info:
        _apply(failing_inspection)

    message = str(exc_info.value)
    assert "injected failure" in message
    assert "injected inspection failure" in message
    assert not any("handle 10:" in command for command in runner.mutations)


def test_rollback_failure_is_reported_and_never_claimed_restored():
    runner = MockTC(fail_fragment="class add", rollback_fails=True)
    with pytest.raises(TCApplyError) as exc_info:
        _apply(runner)
    assert "injected failure" in str(exc_info.value)
    assert "injected rollback failure" in str(exc_info.value)
    assert "qdisc htb 1:" in runner.state["qdiscs"]


def test_rollback_postcondition_mismatch_is_reported():
    runner = MockTC(
        fail_fragment="class add", rollback_verification_fails=True
    )
    with pytest.raises(TCApplyError, match="rollback verification also failed"):
        _apply(runner)
    assert "qdisc htb 1:" in runner.state["qdiscs"]


def test_unrelated_qdisc_or_filter_is_not_removed():
    unrelated = baseline_state(filters="unrelated filter\n")
    runner = MockTC(unrelated)
    with pytest.raises(TCPreconditionError):
        _apply(runner)
    assert runner.state == unrelated
    assert runner.mutations == []


def test_restore_requires_explicit_owned_structure_and_verifies_baseline():
    runner = MockTC(managed_state())
    assert restore_original_hierarchy("s1-eth5", PRIORITIES, DEFAULT_PORT, runner)
    assert runner.state == baseline_state()
    assert not restore_original_hierarchy("s1-eth5", PRIORITIES, DEFAULT_PORT, runner)


def test_unscoped_executor_is_disabled():
    called = []
    with pytest.raises(RuntimeError, match="unscoped tc command execution is disabled"):
        apply_tc_commands(["tc qdisc add dev s1-eth5 root handle 1:"],
                          runner=lambda *args, **kwargs: called.append(args))
    assert called == []
