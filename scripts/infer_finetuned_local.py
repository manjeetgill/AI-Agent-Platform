"""Compare a saved local adapter with its base model on held-out questions.

All inputs are local. No gateway configuration or production agent is changed.
"""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3, choices=range(1, 7))
    args = parser.parse_args()
    report = json.loads((args.run / "run.json").read_text())
    if (
        report["state"] not in {"COMPLETED_REQUIRES_REVIEW", "COMPLETED_REVIEWED"}
        or not report["reload_verified"]
    ):
        raise ValueError("A completed and reload-verified training run is required")
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(2)
    tokenizer = AutoTokenizer.from_pretrained(
        args.run / "tokenizer", local_files_only=True, trust_remote_code=False
    )
    base = AutoModelForCausalLM.from_pretrained(
        report["base_model"],
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        dtype=torch.float32,
    )
    model = PeftModel.from_pretrained(base, args.run / "adapter", local_files_only=True)
    model.eval()
    rows = [json.loads(line) for line in args.holdout.read_text().splitlines() if line.strip()][: args.limit]
    output = []
    for row in rows:
        inputs = tokenizer.apply_chat_template(
            row["messages"][:1],
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
        with torch.no_grad():
            with model.disable_adapter():
                before = model.generate(
                    **inputs, max_new_tokens=64, do_sample=False, pad_token_id=tokenizer.eos_token_id
                )
            after = model.generate(
                **inputs, max_new_tokens=64, do_sample=False, pad_token_id=tokenizer.eos_token_id
            )
        length = inputs["input_ids"].shape[1]
        output.append(
            {
                "question": row["messages"][0]["content"],
                "expected": row["messages"][1]["content"],
                "base_answer": tokenizer.decode(before[0, length:], skip_special_tokens=True),
                "tuned_answer": tokenizer.decode(after[0, length:], skip_special_tokens=True),
            }
        )
    value = {"purpose": report["purpose"], "production_ready": False, "examples": output}
    fd = os.open(args.run / "sample-comparison.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(value, file, indent=2)
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
