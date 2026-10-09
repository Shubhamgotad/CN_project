import json

from telemetry.report_congestion_labels import report_labels


def person1_record(experiment_id, utilization, latency, loss):
    return {
        "timestamp": "2026-10-09T00:00:00Z",
        "experiment_id": experiment_id,
        "scenario": "moderate",
        "source": "h1",
        "destination": "server",
        "traffic_type": "udp",
        "requested_mbps": 7.0,
        "throughput_mbps": 6.9,
        "latency_ms": latency,
        "jitter_ms": 0.01,
        "packet_loss_percent": loss,
        "lost_packets": 0,
        "total_packets": 100,
        "bandwidth_mbps": 10.0,
        "utilization_percent": utilization,
        "active_flows": 1,
        "queue_size": 0,
        "queue_drops": 0,
    }


def test_label_report_uses_existing_rules_and_validates_records(tmp_path, capsys):
    source = tmp_path / "combined.jsonl"
    records = [
        person1_record("low-run", 30, 20, 0),
        person1_record("medium-run", 70, 50, 0),
        person1_record("high-run", 90, 20, 3),
    ]
    source.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    assert report_labels(source) == 0
    output = capsys.readouterr().out
    assert "Validated records: 3" in output
    assert "Independent experiment IDs: 3" in output
    assert "LOW: 1" in output
    assert "MEDIUM: 1" in output
    assert "HIGH: 1" in output


def test_label_report_rejects_invalid_records(tmp_path, capsys):
    source = tmp_path / "invalid.jsonl"
    source.write_text("{}\n", encoding="utf-8")

    assert report_labels(source) == 1
    assert "ERROR:" in capsys.readouterr().out
