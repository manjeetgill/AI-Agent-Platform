# Chakra WhatsApp templates

The existing WhatsApp outbound approval and delivery workflow supports managed
Chakra connections. `/send-setup` verifies the bound phone and reads its business
account's template catalogue through Chakra. `templates` contains only approved,
static text templates. `pending_templates` separately exposes pending static
messages for clinic review and deferred activation. Pending messages cannot be
prepared or dispatched. Parameterised and rejected messages remain unavailable.

Template messages use the existing single-attempt Chakra transport. Existing
recipient consent, opt-out, ownership, idempotency and uncertain-send fences still
apply. A provider acceptance is not a delivery receipt.

The catalogue follows cursors at the trusted Chakra host, never a returned next
URL. Provider errors are sanitised; no credentials are returned to the client.

Contracts: [catalogue](https://apidocs.chakrahq.com/api-17615391),
[phone configuration](https://apidocs.chakrahq.com/api-34338973),
[template sending](https://apidocs.chakrahq.com/api-26944128).

Sahayak operators must still select an approved invitation, review its wording,
enable clinic delivery, and configure a reachable HTTPS patient-form address.
Submission for Meta approval does not enable sending. The pilot's private callback
binding was rotated separately; secrets belong in encrypted server configuration,
never this repository.
