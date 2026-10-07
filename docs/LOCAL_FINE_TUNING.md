# Actual local fine-tuning

`scripts/finetune_local.py` performs LoRA weight updates using Hugging Face PEFT.
It is a separate offline training process, not a prompt or memory-store update.
See the [PEFT quicktour](https://huggingface.co/docs/peft/quicktour).

## Prerequisites

- An approved, current dataset exported from the memory review workflow.
- A selected base model downloaded to a local directory using a pinned revision.
- `pip install '.[training]'` in the platform virtual environment.
- At least 2 GB free disk; sufficient RAM for the chosen model. This pilot runner
  limits the model to 250 million parameters and trains only LoRA attention adapters.
- A distinct train/holdout package from `scripts/prepare_training_dataset.py`.
  Re-export after consent changes. Dataset checksums reject modified splits.

A possible first local proof model is
[HuggingFaceTB/SmolLM2-135M-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct).
Its small size is suitable for checking the training pipeline, not for replacing
an evaluated production agent model. Model choice and dataset purpose must be
settled before launching the first run.

```sh
python scripts/finetune_local.py \
  --model .runtime/models/selected-pinned-model \
  --dataset .runtime/training/reviewed-v1 \
  --output .runtime/training/run-v1 \
  --steps 40 --max-seconds 600
```

Use `--synthetic-pilot` for explicitly approved synthetic test data. Such a run
proves that training and artifact loading work; it provides no evidence of
quality on real customers or business tasks.

The runner uses local safetensors, disables Hub network calls and telemetry,
limits CPU threads, masks question tokens from the loss, checks finite loss,
clips gradients and bounds training steps/time. The training deadline is checked
between steps; loading and before/after evaluation happen outside that deadline.
It does not upload data or call paid APIs.

Outputs are private `adapter/`, `tokenizer/` and `run.json`. The report includes
input hashes, completed steps, changed-weight verification, before/after holdout
loss and verification by reloading the saved adapter. A completed run remains
`COMPLETED_REQUIRES_REVIEW`. Lower holdout loss is not a safety or factual accuracy
guarantee. No adapter is activated in the live agent gateway automatically.

For a production candidate, use a sufficiently diverse, reviewed business dataset,
evaluate behavior and isolation in addition to loss, select an inference deployment,
and retain the existing provider as rollback. Larger cloud training is a separate
provider integration and requires a model, data-sharing permission and spending cap.

## Completed local proof — 2026-10-07

- Base: SmolLM2-135M-Instruct, revision `12fd25f77366fa6b3b4b768ec3050bf629380bac`.
- Dataset: 30 explicitly approved synthetic examples; 24 training / 6 held out.
- Method: LoRA on query/value attention projections, 460,800 trainable parameters.
- Completed 40 optimizer steps; adapter weights changed and reloaded successfully.
- Held-out token loss: 3.8248 before, 3.1964 after. This is not an accuracy score.
- Provider charges: zero. No customer data used or uploaded.
- **Deployment rejected after sample review.** The tuned model requested card
  details, misunderstood a medication question, and did not follow the intended
  rescheduling response. The existing production model was not replaced.

Private artifacts are under `.runtime/training/smollm2-pilot-run-v1/`:
`adapter/`, `tokenizer/`, `run.json`, `sample-comparison.json`.
Reproduce the labelled synthetic dataset with
`examples/fine-tuning/create_pilot_dataset.py`. Use
`scripts/infer_finetuned_local.py` to compare a saved run with its base model.

This result establishes that actual offline weight updates, saving and loading
work. It does not establish a useful medical, booking, payment or general agent.
