import pytest

from ai.prediction import (
    CongestionPredictor,
    create_dataset,
    extract_features,
    label_congestion,
    load_person1_dataset,
    train_candidate_model,
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("throughput_mbps", None),
        ("latency_ms", float("nan")),
        ("jitter_ms", float("inf")),
        ("packet_loss_pct", True),
        ("active_flows", True),
    ],
)
def test_non_finite_or_missing_feature_values_are_rejected(field, value):
    data = telemetry()
    data[field] = value
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
    assert list(result["model"].feature_names_in_) == [
        "utilization", "throughput_mbps", "latency_ms", "jitter_ms",
        "packet_loss_pct", "active_flows",
    ]
    assert set(result["evaluation"]) == {
        "accuracy", "precision", "recall", "f1", "confusion_matrix"
    }
    prediction = CongestionPredictor(model_path).predict(records[-1])
    assert prediction["congestion"] in {"LOW", "MEDIUM", "HIGH"}
    assert 0 <= prediction["confidence"] <= 1


def test_person1_dataset_preserves_experiment_group_outside_features():
    dataset, groups = load_person1_dataset(
        "tests/fixtures/person1_telemetry.jsonl",
        interface="configured0",
    )

    assert list(dataset.columns) == [
        "utilization", "throughput_mbps", "latency_ms", "jitter_ms",
        "packet_loss_pct", "active_flows", "congestion",
    ]
    assert groups.tolist() == ["00000000-0000-0000-0000-000000000001"]
    assert "experiment_id" not in dataset.columns
    assert "queue_drops" not in dataset.columns


def test_candidate_evaluation_splits_experiments_and_reports_all_classes(tmp_path):
    records = []
    groups = []
    class_records = {
        "LOW": telemetry(utilization=20, latency=20, loss=0),
        "MEDIUM": telemetry(utilization=70, latency=50, loss=0),
        "HIGH": telemetry(utilization=90, latency=20, loss=3),
    }
    for label, record in class_records.items():
        for repetition in range(2):
            records.append(record)
            groups.append(f"{label.lower()}-experiment-{repetition}")

    dataset = create_dataset(records)
    result = train_candidate_model(dataset, groups, random_state=17)
    evaluation = result["evaluation"]

    assert set(evaluation["train_experiment_ids"]).isdisjoint(
        evaluation["test_experiment_ids"]
    )
    assert evaluation["test_support"] == {
        "LOW": 1, "MEDIUM": 1, "HIGH": 1,
    }
    assert set(evaluation["per_class"]) == {"LOW", "MEDIUM", "HIGH"}
    assert all(
        set(metrics) == {"precision", "recall", "f1", "support"}
        for metrics in evaluation["per_class"].values()
    )
    assert len(evaluation["confusion_matrix"]) == 3
    assert 0 <= evaluation["accuracy"] <= 1
    assert 0 <= evaluation["macro_f1"] <= 1
    assert not evaluation["warnings"]
    assert not list(tmp_path.iterdir())


def test_candidate_evaluation_warns_when_a_class_is_missing():
    records = [
        telemetry(utilization=20, latency=20),
        telemetry(utilization=25, latency=20),
        telemetry(utilization=90, latency=20, loss=3),
        telemetry(utilization=95, latency=20, loss=4),
    ]
    groups = ["low-a", "low-b", "high-a", "high-b"]

    with pytest.warns(RuntimeWarning, match="dataset has no examples for: MEDIUM"):
        result = train_candidate_model(create_dataset(records), groups)

    assert result["evaluation"]["test_support"]["MEDIUM"] == 0
    assert any("MEDIUM" in message for message in result["evaluation"]["warnings"])


def test_candidate_evaluation_rejects_non_finite_features():
    records = [
        telemetry(utilization=20),
        telemetry(utilization=25),
        telemetry(utilization=90, loss=3),
        telemetry(utilization=95, loss=4),
    ]
    dataset = create_dataset(records)
    dataset.loc[0, "utilization"] = float("nan")

    with pytest.raises(ValueError, match="cannot contain missing values"):
        train_candidate_model(dataset, ["low-a", "low-b", "high-a", "high-b"])
