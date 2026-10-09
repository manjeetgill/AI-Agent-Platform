# Reviewed browser evidence

`evidence.reviewed` v1 lets a consuming app read an operator-reviewed browser
observation through the normal task API, worker and audit trail. It is a manual
fallback when a public page works in a normal browser but the HTTP collector does
not. It does not provide unattended browser collection or retailer authorization.

## Operator setup

Set `AI_REVIEWED_EVIDENCE_ROOT` to the same absolute private directory for API and
worker, and grant `evidence.reviewed` only to the intended application. Capture the
actual product page using an authorized browser session. Record the capture time,
canonical HTTPS URL, exact selected variant, price/currency, and any uncertainty.
Never infer delivery location from a requested pincode or use an MRP/EMI amount as
an observed selling price. Never copy account details, cookies or tokens.

Create an Observation JSON locally. `fees_known` defaults to false. For localized
sources set `location_required=true`; set `location_confirmed=true` and a
`location_label` only when the retailer visibly confirms it. Contradictory product
information must carry `quality_issues`, for example `["variant_conflict"]`.

```bash
.venv/bin/python scripts/store_reviewed_evidence.py \
  --root /absolute/private/evidence \
  --app-id APPLICATION_UUID --owner-id PLATFORM_OWNER_UUID \
  --input /absolute/private/reviewed-observation.json
```

The owner UUID is the platform's authenticated external-user mapping, not an
arbitrary email or external user ID. The operator command prints a SHA-256 evidence
ID. Provision that ID to this application's user, who can run:

```json
{"tool":"evidence.reviewed","version":"1","arguments":{"evidence_id":"SHA256_FROM_OPERATOR"}}
```

## Guarantees and limits

- Caller input cannot supply prices, files, URLs or timestamps.
- The task's authenticated app and owner select the file namespace. Both must
  also match the content. Content digest, size, schema and symlink checks apply.
- Normal runtime grants, quotas, cancellation and persistent reports still apply.
- Results always say `verification_mode: stored_observation` and
  `reason_code: operator_reviewed_browser_observation`. Reading evidence does not
  refresh `observed_at`, open a retailer page or reset an HTTP source block.
- Evidence older than one hour, missing required location, or carrying a quality
  issue has `usable_evidence=false`. Missing fees stay unknown.
- Operator review is a trust boundary: schema/hash validation alone cannot prove
  a price was on a retailer page. This is not a public evidence-upload endpoint.
- This tool does not ingest or overwrite DealAgent's catalogue. Consumers must
  preserve provenance and honor `usable_evidence` when using task results.
- New deployments need an operator-managed shared private volume. Local evidence
  does not automatically become available on a remote worker.

Do not schedule repeated reads as fresh retailer checks. A new browser observation
requires another actual capture and a new content ID.
