# Connect any business app to platform memory

Use a distinct platform app identity for each business workspace. Keep service
credentials on your backend. Your backend authenticates the user and supplies
`X-External-User-ID`; do not accept that header directly from the public browser.

1. Provision `memory:read` and `memory:write` for the integration. Keep
   `memory:review` in a separate trusted reviewer backend. Add existing knowledge
   or task scopes only when the app needs those APIs.
2. Use `MemoryClient` from `agent_platform.memory_client` (Python) or the backend
   TypeScript helper beside this file. Timeouts are bounded; uncertain requests
   are not automatically retried.
3. Enable capture through PUT `/v1/memory/settings` if the business wants it.
   This captures cited knowledge-answer claims for review. It never makes them
   true or shares them automatically.
4. Review `/records` and `/records/{id}/evidence`; submit the expected version,
   validation method and actual evidence reference. Select `app` sharing only
   for facts all users of that business may access.
5. Call `/v1/knowledge/ask` or the existing task APIs normally. Retrieval happens
   on the platform. Every fresh provider call gets current permitted memory.
   Answer responses include `answer_id` for `/memory/feedback`.
6. Use `/metrics` and reviewed `/examples` to evaluate answers, compare saved
   provider/model results, withdraw consent or export reviewed examples.

Private customer/patient/account details must remain private. Voice and WhatsApp
FAQ answers use only app-shared memory. `external_user` mappings must preserve
customer identity if you want individual memory; never combine all customers
under one app-wide user and share their facts.

Memory is not needed for an app to start. Catch platform errors in the integration,
show a temporary agent-unavailable state and keep the rest of the business app
working. Do not replay a spending/sending action because a memory call timed out.

For the local operator UI, open `/memory` in Agent Workspace. That page manages
the Console app only; it does not grant access to other businesses.
