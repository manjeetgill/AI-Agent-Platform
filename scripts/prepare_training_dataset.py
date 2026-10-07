"""Prepare reviewed, provider-neutral exports for offline training and holdout evaluation.

No provider uploads, paid jobs or model-weight changes are performed.
Example: python scripts/prepare_training_dataset.py export.json --output .runtime/training/pilot
"""

import argparse
import hashlib
import json
import os
from pathlib import Path


def prepare(value):
    if value.get("format") != "provider-neutral-messages-v1":
        raise ValueError("Expected a reviewed memory dataset export")
    examples = value["examples"]
    unique = {}
    for example in examples:
        if not example.get("consent_ref"):
            raise ValueError("Consent reference missing")
        messages = example["messages"]
        if len(messages) != 2 or [m["role"] for m in messages] != ["user", "assistant"]:
            raise ValueError("Expected one question and reviewed answer")
        normalized = " ".join(messages[0]["content"].casefold().split())
        key = hashlib.sha256(normalized.encode()).hexdigest()
        answer = " ".join(messages[1]["content"].casefold().split())
        if key in unique and unique[key][0] != answer:
            raise ValueError("Conflicting answers to the same question; review before training")
        unique[key] = (answer, {"messages": messages})
    if len(unique) < 5:
        raise ValueError("At least five distinct reviewed questions are required for a train/holdout split")
    ordered = [unique[k][1] for k in sorted(unique)]
    count = max(1, len(ordered) // 5)
    return ordered[count:], ordered[:count]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.export.read_bytes()
    training, holdout = prepare(json.loads(raw))
    # Existing output cannot be silently overwritten; exports may have been revoked meanwhile.
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, rows in [("train.jsonl", training), ("holdout.jsonl", holdout)]:
        fd = os.open(args.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "split_sha256": {
            name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
            for name in ("train.jsonl", "holdout.jsonl")
        },
        "training_examples": len(training),
        "holdout_examples": len(holdout),
        "provider_upload_started": False,
        "requirement": "Re-export and recheck consent immediately before any training submission. Evaluate on holdout before rollout.",
    }
    fd = os.open(args.output / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(manifest, file, indent=2)
    print(
        json.dumps(
            {"training_examples": len(training), "holdout_examples": len(holdout), "training_started": False}
        )
    )


if __name__ == "__main__":
    main()
