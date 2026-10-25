import json
import runpy
import sys
from pathlib import Path

root = Path.cwd()
reports = root / "telemetry" / "reports"
output_dir = Path.home() / "Documents" / "CN-Reports"
output_dir.mkdir(parents=True, exist_ok=True)

flow_classes = json.loads(
    (reports / "flow-classes.json").read_text(encoding="ascii")
)
priorities = json.loads(
    (reports / "filter-priorities.json").read_text(encoding="ascii")
)

sys.argv = [
    "integration.experiment_runner",
    "--input", str(root / "telemetry" / "datasets" / "combined.jsonl"),
    "--output", str(output_dir / "offline-ml-report.json"),
    "--mode", "adaptive",
    "--model", str(root / "ai" / "prediction" / "model.joblib"),
    "--flow-policy-classes", json.dumps(flow_classes),
    "--filter-priorities", json.dumps(priorities),
]

runpy.run_module("integration.experiment_runner", run_name="__main__")
