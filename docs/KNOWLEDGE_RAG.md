# Shared knowledge and RAG

The platform now supports ingestion → chunking → retrieval → grounded generation → citations.
This is an English lexical RAG implementation using PostgreSQL full-text search, not an embedding
or vector search service. Source text is never treated as executable instructions.

## Enable an app

Apply migration 014 to the platform database. Provision/rotate its backend service key with:

- `knowledge:read`: list/search permitted documents and request model answers.
- `knowledge:write`: upload/update/delete that external user's documents.
- `knowledge:publish`: additionally required to publish or update app-wide knowledge.

Keys remain server-side. Every call also requires `X-External-User-ID`, derived from the app's
signed-in session. An app-wide document is visible to users of that app only, even within the same
tenant. Private documents are visible only to their owner. Only the uploader can edit/delete a
particular document. Document sharing is explicit; publishing is never inferred by the model.
Existing service keys receive no new scopes automatically.

## API

All routes are under `/v1/knowledge` and appear in `/openapi.json`:

- `POST /documents`: `{external_id,title,text,source_label,visibility}`. `visibility` defaults to
  `private` and can be `app`. Reusing an external ID for the same app/user updates the document;
  identical content preserves its version. Updates atomically replace indexed passages.
- `GET /documents?limit=50&after=<uuid>`: permitted document metadata with pagination.
- `DELETE /documents/{id}`: remove an owned document and its indexed passages.
- `POST /search`: `{question}` returns up to six relevant permitted passages without calling a model.
- `POST /ask`: `{question,idempotency_key}` retrieves passages and returns `claims`, `missing`,
  `citations`, `tokens`, `provider`, `model`. Each claim cites source IDs; citations include the source
  title, label, document ID, version, passage index and text. No matching sources means abstention
  with no model call. Reuse request keys after an uncertain HTTP result, not for a different question.

Supported ingestion is UTF-8 text supplied as JSON (including text extracted by an app from TXT or
Markdown). PDF/DOCX extraction, OCR, URL fetching and website crawling are not included. Each document
is limited to 200,000 characters, split into overlapping 1,000-character passages. Retrieval uses
English stemming/stopwords and ranks matching query terms; it does not understand semantic similarity.

## Agent integration

Grant `knowledge.answer` to an app using the operator service-identity CLI or app manifest. The tool
is installed in the API and worker. Its input is `{question}`; its typed output is the cited answer.
It can be selected by the existing natural-language planner, saved in a reusable agent, or included
in an app built-in template. The worker derives app/user identity from the task, never from model
arguments. A tool grant explicitly authorizes knowledge use by that app's tasks; read API scopes and
tool grants are separate permissions. No existing app receives that grant automatically.

Research Lab's older evidence-report path does not automatically query this collection. An app must
use `/ask` or grant/use the knowledge tool to enable knowledge-grounded answers. The legacy `AI_RAG`
flag still controls only the older memory-search endpoint; it does not toggle these scoped APIs.

## Reliability and limits

Model dispatch is recorded before calling NVIDIA. A cached successful answer is returned only while
its cited document versions remain accessible. Updates, unsharing and deletion invalidate those
cached answers. Concurrent duplicate requests and uncertain provider outcomes never automatically
repeat a model call. Requests use the app's existing daily planning quotas; this is request accounting,
not a monetary budget. Successful answers include provider token usage. Direct answers and worker
attempts are bounded to 30 seconds; a crashed/uncertain request needs inspection, not blind retry.

Citation IDs are validated against retrieved passages. This prevents fabricated references but
cannot prove every model claim is entailed by its passage; applications should show sources for
review and evaluate accuracy on domain-specific questions. Documents are untrusted context, not
permission to invoke tools, follow links, or reveal secrets.

Removing a source removes it from future retrieval. Previously delivered answers and task audit
results are historical records and are not retroactively erased. Complete retention/purge policies,
embedding search/reranking, organisation groups within an app, ingestion jobs and storage quotas
remain separate work. This release adds platform support, not an upload UI in every consuming app.
