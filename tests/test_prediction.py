import math

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
from ai.prediction.features import FEATURE_COLUMNS


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


def grouped_candidate_dataset():
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
    return create_dataset(records), groups


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
    dataset, groups = grouped_candidate_dataset()
    result = train_candidate_model(dataset, groups, random_state=17)
    evaluation = result["evaluation"]

    assert set(evaluation["train_experiment_ids"]).isdisjoint(
        evaluation["test_experiment_ids"]
    )
    assert evaluation["test_support"] == {
        "LOW": 1, "MEDIUM": 1, "HIGH": 1,
    }
    assert list(evaluation["per_class"]) == ["LOW", "MEDIUM", "HIGH"]
    assert all(
        set(metrics) == {"precision", "recall", "f1", "support"}
        for metrics in evaluation["per_class"].values()
    )
    assert len(evaluation["confusion_matrix"]) == 3
    assert all(len(row) == 3 for row in evaluation["confusion_matrix"])
    assert 0 <= evaluation["accuracy"] <= 1
    assert 0 <= evaluation["macro_f1"] <= 1
    assert all(
        math.isfinite(value)
        for metrics in evaluation["per_class"].values()
        for value in metrics.values()
        if isinstance(value, float)
    )
    assert math.isfinite(evaluation["accuracy"])
    assert math.isfinite(evaluation["macro_f1"])
    assert not evaluation["warnings"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "cannot contain missing values"),
        (float("nan"), "cannot contain missing values"),
        (float("inf"), "must be finite"),
        ("20.0", "must be numeric"),
        (True, "must be numeric"),
    ],
)
def test_candidate_evaluation_rejects_missing_non_numeric_and_non_finite_features(
    value, message
):
    dataset, groups = grouped_candidate_dataset()
    dataset = dataset.astype(object)
    dataset.loc[0, "utilization"] = value

    with pytest.raises(ValueError, match=message):
        train_candidate_model(dataset, groups)


def test_candidate_evaluation_rejects_missing_feature_column():
    dataset, groups = grouped_candidate_dataset()

    with pytest.raises(ValueError, match="missing columns: jitter_ms"):
        train_candidate_model(dataset.drop(columns=["jitter_ms"]), groups)


@pytest.mark.parametrize(
    ("group_case", "message"),
    [
        ("missing", "groups must contain one experiment ID"),
        ("string_collection", "groups must contain one experiment ID"),
        ("mismatched", "groups must contain one experiment ID"),
        ("blank", "experiment IDs must be non-empty strings"),
        ("non_string", "experiment IDs must be non-empty strings"),
        ("too_few", "at least two experiment groups"),
    ],
)
def test_candidate_evaluation_rejects_invalid_group_identifiers(
    group_case, message
):
    dataset, groups = grouped_candidate_dataset()
    if group_case == "missing":
        invalid_groups = None
    elif group_case == "string_collection":
        invalid_groups = "not-a-group-sequence"
    elif group_case == "mismatched":
        invalid_groups = groups[:-1]
    elif group_case == "blank":
        invalid_groups = [" ", *groups[1:]]
    elif group_case == "non_string":
        invalid_groups = [None, *groups[1:]]
    else:
        invalid_groups = ["same-experiment"] * len(groups)

    with pytest.raises(ValueError, match=message):
        train_candidate_model(dataset, invalid_groups)


def test_candidate_evaluation_rejects_empty_dataset():
    dataset = create_dataset([])

    with pytest.raises(ValueError, match="at least two experiment groups"):
        train_candidate_model(dataset, [])


def test_candidate_evaluation_rejects_unsupported_labels():
    dataset, groups = grouped_candidate_dataset()
    dataset.loc[0, "congestion"] = "SEVERE"

    with pytest.raises(ValueError, match="unsupported congestion labels: SEVERE"):
        train_candidate_model(dataset, groups)


def test_candidate_evaluation_keeps_canonical_feature_order():
    dataset, groups = grouped_candidate_dataset()
    reordered = dataset.loc[:, list(reversed(dataset.columns))]

    result = train_candidate_model(reordered, groups, random_state=17)

    assert tuple(result["model"].feature_names_in_) == FEATURE_COLUMNS


def test_candidate_evaluation_is_reproducible_for_fixed_seed():
    dataset, groups = grouped_candidate_dataset()

    first = train_candidate_model(dataset, groups, random_state=23)
    second = train_candidate_model(dataset, groups, random_state=23)

    assert first["evaluation"] == second["evaluation"]
    assert first["model"].predict(dataset.loc[:, FEATURE_COLUMNS]).tolist() == (
        second["model"].predict(dataset.loc[:, FEATURE_COLUMNS]).tolist()
    )


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
    assert list(result["evaluation"]["per_class"]) == ["LOW", "MEDIUM", "HIGH"]
    assert result["evaluation"]["per_class"]["MEDIUM"]["support"] == 0
    assert result["evaluation"]["per_class"]["MEDIUM"]["precision"] == 0
    assert result["evaluation"]["per_class"]["MEDIUM"]["recall"] == 0
    assert result["evaluation"]["per_class"]["MEDIUM"]["f1"] == 0


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
