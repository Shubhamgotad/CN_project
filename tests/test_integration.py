from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from ai.prediction import train_random_forest
from integration.telemetry_qos import (
    QoSIntegrationConfig,
    adapt_person1_telemetry,
    apply_integration_commands,
    build_qos_integration,
    read_person1_jsonl,
)


def person1_telemetry(utilization=30.0, packet_loss=0.0):
    return {
        "timestamp": "2026-10-09T00:00:00Z",
        "experiment_id": "00000000-0000-0000-0000-000000000001",
        "scenario": "normal",
        "source": "h1",
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": 3.0,
        "throughput_mbps": 3.0,
        "latency_ms": 20.26,
        "jitter_ms": 0.014,
        "packet_loss_percent": packet_loss,
        "lost_packets": 0,
        "total_packets": 428,
        "bandwidth_mbps": 10.0,
        "utilization_percent": utilization,
        "active_flows": 1,
        "queue_size": 0,
        "queue_drops": 0,
    }


def training_record(utilization, latency, loss, throughput):
    return {
        "timestamp": 0,
        "interface": "test0",
        "throughput_mbps": throughput,
        "bandwidth_mbps": 10,
        "utilization": utilization,
        "latency_ms": latency,
        "jitter_ms": 1,
        "packet_loss_pct": loss,
        "active_flows": 1,
    }


def train_fixture_model(path):
    records = [
        training_record(10, 10, 0, 1),
        training_record(20, 20, 0, 2),
        training_record(30, 30, 0.1, 3),
        training_record(60, 60, 0.6, 6),
        training_record(65, 70, 0.8, 6.5),
        training_record(70, 80, 0.7, 7),
        training_record(80, 100, 2, 8),
        training_record(90, 120, 3, 9),
        training_record(95, 150, 5, 9.5),
    ]
    train_random_forest(records, path, random_state=11)


def test_timestamp_mapping_and_timezone_handling():
    adapted = adapt_person1_telemetry(
        {**person1_telemetry(), "timestamp": "2026-10-09T05:30:00+05:30"},
        interface="configured0",
    )
    assert adapted.record.timestamp == pytest.approx(
        datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    )


def test_mapping_and_metadata_preservation():
    adapted = adapt_person1_telemetry(person1_telemetry(42, 1.5), interface="configured0")
    assert adapted.record.utilization == 42
    assert adapted.record.packet_loss_pct == 1.5
    assert adapted.metadata["experiment_id"].endswith("001")
    assert adapted.metadata["queue_size"] == 0


def test_malformed_or_missing_telemetry_rejected():
    missing = person1_telemetry()
    del missing["latency_ms"]
    with pytest.raises(ValueError, match="latency_ms"):
        adapt_person1_telemetry(missing, interface="configured0")
    with pytest.raises(ValueError, match="malformed ISO-8601"):
        adapt_person1_telemetry(
            {**person1_telemetry(), "timestamp": "not-a-timestamp"},
            interface="configured0",
        )
    with pytest.raises(ValueError, match="timezone"):
        adapt_person1_telemetry(
            {**person1_telemetry(), "timestamp": "2026-10-09T00:00:00"},
            interface="configured0",
        )


def test_jsonl_fixture_and_line_number_error(tmp_path):
    fixture = "tests/fixtures/person1_telemetry.jsonl"
    assert len(read_person1_jsonl(fixture, interface="configured0")) == 1
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text(
        json_line(person1_telemetry()) + "\nnot-json\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="line 2"):
        read_person1_jsonl(invalid, interface="configured0")


def json_line(record):
    import json

    return json.dumps(record)


def test_end_to_end_prediction_qos_and_dry_run(tmp_path):
    model_path = tmp_path / "model.joblib"
    train_fixture_model(model_path)
    config = QoSIntegrationConfig(
        interface="configured-bottleneck",
        total_bandwidth_mbps=10.0,
        traffic_filters={"web": {"port": 5201, "protocol": "udp"}},
        neutral_flow_filters={
            "flow_h1": {"port": 5201, "protocol": "udp"},
            "flow_h2": {"port": 5202, "protocol": "udp"},
            "flow_h3": {"port": 5203, "protocol": "udp"},
            "flow_h4": {"port": 5204, "protocol": "udp"},
        },
        dry_run=True,
    )
    result = build_qos_integration(
        {**person1_telemetry(), "utilization_percent": 90.0},
        model_path=model_path,
        config=config,
    )
    assert result["prediction"]["congestion"] in {"LOW", "MEDIUM", "HIGH"}
    assert sum(result["qos_decision"]["allocation_mbps"].values()) == pytest.approx(10)
    assert any("dev configured-bottleneck" in command for command in result["tc_commands"])
    assert "flow_h4" in result["neutral_flow_filters"]
    with pytest.raises(RuntimeError, match="dry_run"):
        apply_integration_commands(result)


def test_neutral_filters_are_not_translated_to_application_filters(tmp_path):
    model_path = tmp_path / "model.joblib"
    train_fixture_model(model_path)
    result = build_qos_integration(
        person1_telemetry(),
        model_path=model_path,
        config=QoSIntegrationConfig(
            interface="configured0",
            total_bandwidth_mbps=10,
            neutral_flow_filters={"flow_h1": {"port": 5201, "protocol": "udp"}},
        ),
    )
    assert result["neutral_flow_filters"]["flow_h1"]["port"] == 5201
    assert not any("filter add" in command for command in result["tc_commands"])


def test_command_runner_is_only_used_for_explicit_non_dry_run(tmp_path):
    model_path = tmp_path / "model.joblib"
    train_fixture_model(model_path)
    config = QoSIntegrationConfig(
        interface="configured0",
        total_bandwidth_mbps=10,
        dry_run=True,
    )
    result = build_qos_integration(
        person1_telemetry(), model_path=model_path, config=config
    )
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))

    with pytest.raises(RuntimeError, match="dry_run"):
        apply_integration_commands(result, runner=runner)
    assert calls == []

    result["dry_run"] = False
    apply_integration_commands(result, runner=runner)
    assert calls

def test_combined_person1_dataset_through_ml_to_qos(tmp_path):
    """Exercise every combined telemetry record without executing tc commands."""
    model_path = tmp_path / "model.joblib"
    train_fixture_model(model_path)

    dataset_path = Path("telemetry/datasets/combined.jsonl")
    assert dataset_path.exists(), f"Dataset not found: {dataset_path}"

    records = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records, "Combined telemetry dataset is empty"

    config = QoSIntegrationConfig(
        interface="configured-bottleneck",
        total_bandwidth_mbps=10.0,
        dry_run=True,
    )

    for index, record in enumerate(records, start=1):
        result = build_qos_integration(
            record,
            model_path=model_path,
            config=config,
        )

        prediction = result["prediction"]
        assert prediction["congestion"] in {"LOW", "MEDIUM", "HIGH"}, (
            f"Record {index}: invalid congestion class"
        )
        assert 0.0 <= prediction["confidence"] <= 1.0, (
            f"Record {index}: invalid confidence"
        )

        allocations = result["qos_decision"]["allocation_mbps"]
        assert sum(allocations.values()) == pytest.approx(10.0), (
            f"Record {index}: bandwidth allocation does not total 10 Mbps"
        )
        assert result["tc_commands"], f"Record {index}: no tc commands generated"
        assert result["dry_run"] is True

        # Generated commands must not be executed by this test.
        with pytest.raises(RuntimeError, match="dry_run"):
            apply_integration_commands(result)