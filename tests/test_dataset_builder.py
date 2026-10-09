from telemetry.build_dataset import DATASET_DIR, INPUT_FILES


def test_dataset_builder_includes_moderate_telemetry():
    assert DATASET_DIR / "moderate.jsonl" in INPUT_FILES
