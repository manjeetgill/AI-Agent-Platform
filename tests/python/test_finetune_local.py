import importlib.util
import json
from pathlib import Path

import pytest


def runner():
    spec = importlib.util.spec_from_file_location(
        "finetune_local", Path(__file__).parents[2] / "scripts/finetune_local.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(question="Example question"):
    return {
        "messages": [
            {"role": "user", "content": question},
            {"role": "assistant", "content": "Reviewed answer"},
        ]
    }


def test_rejects_holdout_leakage_and_unsafe_message_shapes(tmp_path):
    module = runner()
    with pytest.raises(ValueError, match="overlap"):
        module.check_splits([row("Example question")], [row("  EXAMPLE   question ")])
    path = tmp_path / "data.jsonl"
    path.write_text(json.dumps({"messages": [{"role": "system", "content": "bad"}]}))
    with pytest.raises(ValueError, match="Expected"):
        module.read_rows(path)
    path.write_text(json.dumps(row()))
    assert len(module.read_rows(path)) == 1
    previous = module.checksum(path)
    path.write_text(json.dumps(row("Changed question")))
    assert module.checksum(path) != previous


def test_runner_refuses_changed_training_data_before_loading_model(tmp_path, monkeypatch):
    module = runner()
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    data = tmp_path / "dataset"
    data.mkdir()
    (data / "train.jsonl").write_text(json.dumps(row("Train question")))
    (data / "holdout.jsonl").write_text(json.dumps(row("Holdout question")))
    (data / "manifest.json").write_text(
        json.dumps(
            {
                "source_sha256": "source",
                "training_examples": 1,
                "holdout_examples": 1,
                "split_sha256": {"train.jsonl": "wrong", "holdout.jsonl": "wrong"},
            }
        )
    )
    monkeypatch.setattr(
        "sys.argv",
        ["finetune_local", "--model", str(model), "--dataset", str(data), "--output", str(tmp_path / "run")],
    )
    with pytest.raises(ValueError, match="checksum"):
        module.main()
    assert not (tmp_path / "run").exists()
