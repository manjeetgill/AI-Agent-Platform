"""Bounded, offline LoRA fine-tuning for a small local causal language model.

No provider account, upload, network inference or automatic deployment is used.
The dataset must be a newly prepared, reviewed train/holdout package.
"""

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import time
from pathlib import Path


def read_rows(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not 1 <= len(rows) <= 1000:
        raise ValueError("Each split needs 1–1000 examples")
    for row in rows:
        messages = row.get("messages", [])
        if len(messages) != 2 or [m.get("role") for m in messages] != ["user", "assistant"]:
            raise ValueError("Expected a user question and reviewed assistant answer")
        if any(not isinstance(m.get("content"), str) or not 1 <= len(m["content"]) <= 600 for m in messages):
            raise ValueError("Each message must contain 1–600 characters")
    return rows


def check_splits(training, holdout):
    normalize = lambda s: " ".join(s.casefold().split())
    left = {normalize(r["messages"][0]["content"]) for r in training}
    right = {normalize(r["messages"][0]["content"]) for r in holdout}
    if left & right:
        raise ValueError("Training and holdout questions overlap")


def checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_status(output, report):
    temp = output / "run.json.tmp"
    temp.write_text(json.dumps(report, indent=2))
    temp.replace(output / "run.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="Pinned, local safetensors model directory")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument(
        "--synthetic-pilot", action="store_true", help="Label run as pipeline proof, not business training"
    )
    args = parser.parse_args()
    if not 1 <= args.steps <= 200 or not 30 <= args.max_seconds <= 1800:
        raise ValueError("Use 1–200 steps and a 30–1800 second training deadline")
    if not args.model.is_dir() or not (args.model / "config.json").is_file():
        raise ValueError("A downloaded local model is required; this runner never downloads one")
    if args.output.exists():
        raise ValueError("Choose a new output directory; interrupted jobs are never silently resumed")
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    if not manifest.get("source_sha256"):
        raise ValueError("Dataset provenance manifest required")
    training, holdout = [read_rows(args.dataset / name) for name in ("train.jsonl", "holdout.jsonl")]
    check_splits(training, holdout)
    for name in ("train.jsonl", "holdout.jsonl"):
        if manifest.get("split_sha256", {}).get(name) != checksum(args.dataset / name):
            raise ValueError("Dataset checksum mismatch; prepare a fresh reviewed package")
    if len(training) != manifest["training_examples"] or len(holdout) != manifest["holdout_examples"]:
        raise ValueError("Dataset sizes differ from the reviewed manifest")
    if shutil.disk_usage(args.output.parent).free < 2 * 1024**3:
        raise ValueError("Keep at least 2 GB free before training")
    os.umask(0o077)
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
    import torch
    from peft import LoraConfig, PeftModel, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(2)
    torch.manual_seed(42)
    random.seed(42)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
    base = AutoModelForCausalLM.from_pretrained(
        args.model, local_files_only=True, trust_remote_code=False, use_safetensors=True, dtype=torch.float32
    )
    if sum(p.numel() for p in base.parameters()) > 250_000_000:
        raise ValueError("This local pilot runner supports models up to 250M parameters")
    base.config.use_cache = False
    model = get_peft_model(
        base,
        LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=8,
            lora_alpha=16,
            lora_dropout=0.0,
            target_modules=["q_proj", "v_proj"],
        ),
    )
    trainable = [p for p in model.parameters() if p.requires_grad]
    initial = [p.detach().clone() for p in trainable]

    def encode(row):
        prefix = tokenizer.apply_chat_template(
            row["messages"][:1], tokenize=True, add_generation_prompt=True, return_dict=False
        )
        tokens = tokenizer.apply_chat_template(row["messages"], tokenize=True, return_dict=False)
        if len(tokens) > 256 or tokens[: len(prefix)] != prefix or len(tokens) <= len(prefix):
            raise ValueError(
                "Example is too long or its chat template is unsupported; no truncation performed"
            )
        labels = [-100] * len(prefix) + tokens[len(prefix) :]
        return {
            "input_ids": torch.tensor([tokens]),
            "attention_mask": torch.ones(1, len(tokens), dtype=torch.long),
            "labels": torch.tensor([labels]),
        }

    batches = [encode(row) for row in training]
    held = [encode(row) for row in holdout]

    def evaluate(active):
        active.eval()
        total, count = 0.0, 0
        with torch.no_grad():
            for batch in held:
                n = int((batch["labels"][:, 1:] != -100).sum())
                total += float(active(**batch).loss) * n
                count += n
        return total / count

    args.output.mkdir(parents=True, mode=0o700)
    report = {
        "state": "STARTED",
        "purpose": "synthetic_pipeline_proof" if args.synthetic_pilot else "reviewed_dataset_pilot",
        "base_model": str(args.model.resolve()),
        "dataset_source_sha256": manifest["source_sha256"],
        "split_sha256": {n: checksum(args.dataset / n) for n in ("train.jsonl", "holdout.jsonl")},
        "trainable_parameters": sum(p.numel() for p in trainable),
        "requested_steps": args.steps,
        "completed_steps": 0,
        "provider_cost": 0,
        "deployment_started": False,
        "deployment_status": "NOT_APPROVED",
    }
    write_status(args.output, report)
    try:
        report["holdout_loss_before"] = evaluate(model)
        optimizer = torch.optim.AdamW(trainable, lr=2e-4)
        start = time.monotonic()
        losses = []
        for step in range(args.steps):
            if time.monotonic() - start >= args.max_seconds:
                report["stopped_at_deadline"] = True
                break
            model.train()
            optimizer.zero_grad(set_to_none=True)
            loss = model(**batches[step % len(batches)]).loss
            if not math.isfinite(float(loss.detach())):
                raise ValueError("Non-finite loss; training stopped")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            optimizer.step()
            losses.append(float(loss.detach()))
            report["completed_steps"] = step + 1
            if (step + 1) % 5 == 0:
                write_status(args.output, report)
                print(json.dumps({"step": step + 1, "loss": round(losses[-1], 4)}), flush=True)
        report["weights_changed"] = any(
            not torch.equal(a, p.detach()) for a, p in zip(initial, trainable, strict=True)
        )
        if not report["weights_changed"]:
            raise ValueError("No adapter weights changed")
        report["holdout_loss_after"] = evaluate(model)
        report["training_losses"] = losses
        model.save_pretrained(args.output / "adapter", safe_serialization=True)
        tokenizer.save_pretrained(args.output / "tokenizer")
        # Reload the saved adapter onto a fresh base model to prove the artifact is usable.
        del optimizer, model, base
        fresh = AutoModelForCausalLM.from_pretrained(
            args.model,
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            dtype=torch.float32,
        )
        loaded = PeftModel.from_pretrained(fresh, args.output / "adapter", local_files_only=True)
        report["reloaded_holdout_loss"] = evaluate(loaded)
        report["reload_verified"] = abs(report["reloaded_holdout_loss"] - report["holdout_loss_after"]) < 1e-4
        if not report["reload_verified"]:
            raise ValueError("Saved adapter evaluation differs from the trained adapter")
        report["holdout_loss_improved"] = report["holdout_loss_after"] < report["holdout_loss_before"]
        report["state"] = "COMPLETED_REQUIRES_REVIEW"
        report["quality_note"] = (
            "Loss change alone is not proof of factual accuracy, safety or production readiness."
        )
        report["adapter_sha256"] = checksum(args.output / "adapter" / "adapter_model.safetensors")
        write_status(args.output, report)
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "state",
                        "completed_steps",
                        "weights_changed",
                        "reload_verified",
                        "holdout_loss_before",
                        "holdout_loss_after",
                        "provider_cost",
                    )
                }
            )
        )
    except BaseException as exc:
        report.update(state="FAILED", error_type=type(exc).__name__)
        write_status(args.output, report)
        raise


if __name__ == "__main__":
    main()
