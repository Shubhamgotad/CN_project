import pytest

from ai.prediction import (
    CongestionPredictor,
    create_dataset,
    extract_features,
    label_congestion,
    train_random_forest,
    validate_telemetry,
)


def telemetry(utilization=20, latency=10, loss=0.0, flows=2, throughput=10):
    return {
        "timestamp": "2026-10-09T00:00:00Z",
        "interface": "eth0",
        "throughput_mbps": throughput,
        "bandwidth_mbps": 100,
        "utilization": utilization,
        "latency_ms": latency,
        "jitter_ms": 1,
        "packet_loss_pct": loss,
        "active_flows": flows,
    }


def test_valid_telemetry_and_features():
    record = validate_telemetry(telemetry())
    assert extract_features(record) == [20.0, 10.0, 10.0, 1.0, 0.0, 2.0]


@pytest.mark.parametrize("field,value", [
    ("latency_ms", -1), ("throughput_mbps", -1), ("utilization", 101),
    ("packet_loss_pct", 101), ("active_flows", -1),
])
def test_invalid_telemetry(field, value):
    data = telemetry()
    data[field] = value
    with pytest.raises(ValueError):
        validate_telemetry(data)


def test_missing_field():
    data = telemetry()
    del data["interface"]
    with pytest.raises(ValueError):
        validate_telemetry(data)


def test_labels():
    assert label_congestion(telemetry()) == "LOW"
    assert label_congestion(telemetry(utilization=70, latency=70)) == "MEDIUM"
    assert label_congestion(telemetry(utilization=90, latency=120, loss=3)) == "HIGH"


def test_dataset_and_training_prediction(tmp_path):
    records = [
        telemetry(utilization=10, latency=10, loss=0, throughput=10),
        telemetry(utilization=20, latency=20, loss=0, throughput=20),
        telemetry(utilization=30, latency=30, loss=0.1, throughput=30),
        telemetry(utilization=60, latency=60, loss=0.6, throughput=60),
        telemetry(utilization=65, latency=70, loss=0.8, throughput=65),
        telemetry(utilization=70, latency=80, loss=0.7, throughput=70),
        telemetry(utilization=80, latency=100, loss=2, throughput=80),
        telemetry(utilization=90, latency=120, loss=3, throughput=90),
        telemetry(utilization=95, latency=150, loss=5, throughput=95),
    ]
    dataset = create_dataset(records)
    assert list(dataset.columns) == [
        "utilization", "throughput_mbps", "latency_ms", "jitter_ms",
        "packet_loss_pct", "active_flows", "congestion",
    ]
    model_path = tmp_path / "congestion.joblib"
    result = train_random_forest(records, model_path, random_state=7)
    assert model_path.exists()
    assert set(result["evaluation"]) == {
        "accuracy", "precision", "recall", "f1", "confusion_matrix"
    }
    prediction = CongestionPredictor(model_path).predict(records[-1])
    assert prediction["congestion"] in {"LOW", "MEDIUM", "HIGH"}
    assert 0 <= prediction["confidence"] <= 1
