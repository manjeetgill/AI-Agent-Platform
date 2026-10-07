# Provider-neutral memory and handoffs

The platform stores memory, checkpoint summaries, reviewed examples and outcome feedback in PostgreSQL. It supports opt-in answer capture, local semantic retrieval, a review UI, model evaluation and consent-controlled dataset export. It never treats conversation volume as evidence of improvement or automatically changes model weights.

## Ownership and permissions

Every record belongs to one platform app and external-user mapping. The authenticated service key determines the app; callers cannot select another app in the request body. Provision one app per business workspace using the existing operator workflow. `private` belongs to its owner; `app` is an explicit sharing decision within that business. There is no cross-app/global memory scope.

New scopes are `memory:read`, `memory:write`, and `memory:review`. Existing credentials do not acquire these scopes automatically. Keep reviewer keys on the trusted app backend, away from model tools and browsers. Apps remain responsible for authenticating their users before setting `X-External-User-ID`.

Candidates are immutable and private. Only a credential with `memory:review`, acting for the owner, can validate and optionally share one. A review requires the current version, validation method and evidence reference. This is an **app/reviewer attestation**: a nonempty reference does not independently establish that a web page, tool output or fact is true. The application must check the cited evidence or obtain user confirmation before submitting its review. Model output alone is insufficient.

Corrections: revoke the old record and submit/review a new candidate. Expiry is mandatory (maximum one year); confidence is recorded, never treated as proof. Deletion removes the content; minimal audit metadata remains. The service caps stored memories at 10,000 per app. Expired records are excluded immediately, but operators must delete them to reclaim capacity.

## API

All paths require a service credential and `X-External-User-ID`. No provider keys appear in these requests.

| Method/path | Scope | Purpose |
| --- | --- | --- |
| POST `/v1/memory/candidates` | memory:write | Submit an idempotent private candidate |
| GET `/v1/memory/records` | memory:read | List own records, including candidates; cursor pagination |
| GET `/v1/memory/search?q=...&namespace=general` | memory:read | Retrieve only permitted, validated, unexpired memories |
| POST `/v1/memory/records/{id}/review` | memory:review | Validate, reject, or revoke using an expected version |
| DELETE `/v1/memory/records/{id}` | memory:write | Delete owned content and invalidate future cached use |
| PUT `/v1/memory/tasks/{task_id}/checkpoint` | tasks:write | Save structured decisions and open questions with concurrency checks |
| GET `/v1/memory/tasks/{task_id}/checkpoint` | tasks:read | Read execution snapshot and optional authored checkpoint; report staleness |
| POST `/v1/memory/feedback` | memory:write | Record an app-reported outcome against an owned task or knowledge answer |
| GET `/v1/memory/feedback` | memory:read | Read own outcome records for review; not an accuracy benchmark |
| GET `/v1/memory/events` | memory:read | Read own audit events using an event cursor |
| GET / PUT `/v1/memory/settings` | memory:read / memory:review | Inspect or change opt-in capture with revision checking |
| GET `/v1/memory/records/{id}/evidence` | memory:read | Read original captured claims and citations; flag stale sources |
| POST `/v1/memory/index` | memory:review | Index up to 32 owned validated memories using a local embedding model |
| GET `/v1/memory/metrics` | memory:read | Own memory counts, app-reported outcomes and latest evaluation results |
| GET / POST `/v1/memory/examples` | memory:read / memory:write | Inspect or create draft anonymous examples linked to owned validated memory |
| POST `/v1/memory/examples/{id}/review` | memory:review | Approve after checking consent and sensitive data, or revoke consent |
| POST `/v1/memory/evaluations` | memory:review | Run the configured model against a reviewed example, using existing generation quotas |
| POST `/v1/memory/dataset/export` | memory:review | Export only approved examples with current validated memory sources |

Example candidate (set a real future expiry):

```json
{
  "request_key": "clinic-hours-2026-10-v1",
  "kind": "FACT",
  "namespace": "general",
  "content": "The clinic opens at 9 AM on weekdays.",
  "source_type": "TOOL",
  "source_ref": "clinic-settings:version-18",
  "confidence": 1.0,
  "expires_at": "2026-11-07T00:00:00Z"
}
```

It is not retrieved until the app verifies that exact assertion and records a review:

```json
{
  "expected_version": 1,
  "decision": "VALIDATED",
  "method": "TOOL_VERIFICATION",
  "evidence_ref": "clinic-settings:version-18:review-52",
  "visibility": "app"
}
```

Do not share patient-specific or trading-account-specific facts as app memory. Business records remain authoritative in the consuming app. Keep source references as identifiers; do not put credentials in them.

## Runtime integration

- Knowledge answers retrieve reviewed memories alongside permitted documents, using the same context for the selected model and its fallback. Cited memory IDs and versions are stored with the answer, model and token usage.
- Answers are rechecked against current validation, expiry and access rules before caching/returning a cached result. A revoked/deleted source causes the old request to fail closed; submit a new request key for a fresh answer.
- Customer-facing calls with attached documents (including WhatsApp FAQ and voice FAQ paths) retrieve **only app-shared memories**, never the operator's private memory.
- WhatsApp rechecks memory versions and sharing immediately before sending. A revoked source causes handoff rather than sending the prepared reply. Already delivered messages cannot be recalled by this mechanism.
- The reusable-agent planner loads scoped memories. They cannot grant tool permissions, authorize payment/sending or supply missing transaction arguments. Tool validation and approval gates remain authoritative.
- Setting `AI_MEMORY=0` disables memory retrieval into model context. Stored records can still be administered/deleted.
- Legacy research preferences remain explicitly user-supplied. Generated research observations, including existing ones, are labelled `UNVERIFIED`; they are not promoted into the new validated retrieval store.

## Handoff checkpoints

Every durable app task event atomically updates a deterministic snapshot containing the goal, status, completed steps, next step, reconciliation flag and tool-result reference. `GET /v1/tasks/{id}` includes this `handoff`; no extra model request is made.

An app can additionally save `goal`, `completed`, structured `decisions` with reasons, `open_questions`, `next_action` and `artifacts`. Supply `expected_revision` (zero on first write), `expected_task_revision` and `expected_event_id` from current task state. Concurrent changes return 409. Staleness checks include execution events, not just plan revision. Handoff prose never overrides current task state or authorizes an action.

## Capture and review

Capture is disabled until the owning app/user reviewer enables it. Each completed knowledge answer can save up to three cited model claims as private `CANDIDATE` records, atomically with its answer. Capture requires no additional model call. Candidates retain the original answer and citation references, a zero initial confidence, bounded expiry and a configurable daily cap (0–100, default 25). Repeated requests cannot create duplicate claims. The app-wide 10,000 memory limit still applies.

This is candidate extraction, not fact validation. Reviewers inspect `/evidence` and check the actual business records before approving. The capture currently covers cited knowledge answers (including FAQ-based WhatsApp/voice answers), not arbitrary raw transcripts or every tool result. Durable task state and verified tool results continue to be stored in the existing task system.

## Local semantic retrieval

Install the optional `.[embeddings]` extra. Download a Sentence Transformers model once into a private local directory. Set `AI_MEMORY_EMBEDDING_PATH` and `AI_MEMORY_EMBEDDING_REVISION` to that immutable directory and its pinned revision. The runtime uses CPU inference, two Torch threads, `local_files_only=True` and `trust_remote_code=False`; it does not download model code or send memory text to a remote embedding service. See the [Sentence Transformers API](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html).

Run `python -m agent_platform.memory_worker` alongside the API. Local `scripts/console.py` starts it automatically. Validated, unexpired records without a current vector are the durable indexing queue, so a worker restart loses no pending work. Vector writes recheck memory versions. Revocation immediately excludes a record even if its old vector still exists; deleting memory also deletes its vector and linked examples.

Retrieval combines lexical results with cosine similarity through reciprocal rank fusion. App/user/visibility filters run before vectors leave PostgreSQL. Small-pilot indexing uses PostgreSQL arrays and exact ranking, bounded to the 10,000-record app cap, rather than an approximate vector extension. The semantic score threshold is 0.35; evaluate it on each domain. Changing model/revision triggers reindexing. If the optional encoder is unavailable, lexical retrieval continues with a server warning.

Automatic planner/RAG context uses the `general` namespace. Applications can use other namespaces through `/search`. The configured demonstration model is English-focused; use and evaluate a multilingual embedding model before relying on cross-language semantic matches.

## Feedback, evaluation and controlled training preparation

Knowledge answers include `answer_id`, which apps can use for feedback. Feedback distinguishes confirmed success, correction, failed and unknown, and remains labelled `app_reported`. It is never automatically promoted to fact or used for training.

Draft examples contain an anonymous question, reviewed expected answer, current owned memory version and consent reference. Approval requires `memory:review` plus explicit sensitive-data-removal and consent attestations. A basic identifier/secret detector rejects obvious email addresses, phone numbers and token patterns; it is not a complete anonymizer. Reviewers must inspect the full text. Examples have a separate approval lifecycle; ordinary memory review does not grant training consent.

An evaluation calls the configured model through the normal RAG path and generation quotas. It records provider/model/tokens, exact normalized answer match and whether the expected memory was cited. These are diagnostic checks, not semantic truth judgments. Idempotency prevents duplicate dispatch; uncertain outcomes are recorded and not retried automatically. Comparing saved runs after a provider/configuration change shows continuity and differences. No evaluator is allowed to grant action permissions.

Dataset export selects only approved examples whose memory version remains validated and unexpired. Consent withdrawal or source revocation excludes them immediately from future exports and evaluations. Exports are provider-neutral and never start provider uploads or paid training. Previously downloaded files cannot be recalled: delete withdrawn examples from copies and re-export before training.

To prepare a training/holdout package:

```sh
python scripts/prepare_training_dataset.py reviewed-export.json --output .runtime/training/pilot-v1
```

The script deduplicates normalized questions, rejects conflicting answers, requires at least five distinct examples, separates holdout questions and writes private JSONL files with a source-hash manifest. Five is a mechanical minimum, not a recommendation for model quality. Actual model training needs an appropriate reviewed dataset, a chosen trainable model/provider, cost approval and holdout evaluation. No paid training job or fine-tuned model deployment is automatically initiated by this workflow.

## Operator UI and app integrations

Open `/memory` in the local Agent Workspace. It provides capture settings, candidates, source inspection, review/revoke/delete, a live answer test, feedback, example approval, evaluation and dataset download. It manages only the Console app identity. It cannot inspect another business’s records.

Backend clients are provided in `agent_platform.memory_client` and `examples/memory-integration/client.ts`; see the integration README. Existing consuming apps must explicitly provision memory scopes and connect their review/feedback UI or backend. They do not gain access merely because the platform is upgraded. Existing WhatsApp/voice FAQ and reusable-agent planning integrations automatically retrieve eligible memories once those businesses have reviewed them.

## Rollout and limits

Apply migrations `030_validated_memory.sql` and `031_memory_workflow.sql` before starting this code. Provision integration and reviewer credentials explicitly; do not give model tools a reviewer credential. The local console provisions its own scoped operator credential and starts its indexing worker. Production deployments need the migration, optional embedding dependencies/model mount and worker process; account for CPU and memory capacity before enabling a local encoder on a small Droplet.

These changes do not automatically enrol live business apps, publish their data, initiate telephone/WhatsApp sends or train a model. Existing business data and action approval gates remain authoritative. Automated factual verification, a general transcript extractor, large-scale approximate vector indexing and an actual provider-specific fine-tuning job are outside this implementation.
