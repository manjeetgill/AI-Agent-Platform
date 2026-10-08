# WhatsApp calling agents

Calling now uses **Meta WhatsApp Cloud API or the existing Chakra connection**.
The previous ordinary telephone adapter and SDK have been removed. Calls ring
inside WhatsApp; typing an ordinary mobile number does not connect it to an agent.

## Customer and app flow

Each subscribing app uses its own backend service credential and customer/business
identity (`X-External-User-ID`). The business connects WhatsApp through existing
onboarding, creates a calling agent, selects its authorized WhatsApp channel, checks
readiness, then activates it. No provider tokens are exposed to the browser.
The credential needs `calling:read` and `calling:write`; channel connection also
needs `channels:read`. Text-support credentials do not receive calling access unless
the parent workspace explicitly enables the `whatsapp_calling` feature bundle.
After migration 036, rotate existing calling-app credentials to include these scopes;
existing `channels:*` keys intentionally stop authorizing calling management.

The local customer-flow preview is `http://127.0.0.1:4061/calling`. Its loopback-only
proxy is for testing; customer apps must proxy the API with their own authenticated
user-to-business mapping. The existing public pilot ingress keeps `/v1` private.

- GET `/v1/calling/capabilities`: channel, provider and feature support.
- POST `/v1/calling/agents`: draft, idempotent by external ID. Same fields as before;
  `connection_ref` must now be null. Greeting, language, handoff text and turn limit.
- GET `/v1/calling/agents`: app/owner-scoped paginated list.
- POST `/v1/calling/agents/{id}/connection`: `channel_id` plus
  `authorized_to_connect: true`. Reuses the same owner's WhatsApp channel.
- GET `/setup` or `/connection`: saved connection status; not a live-call claim.
- GET `/readiness`: checks provider calling settings and audio-worker heartbeat.
- POST `/provider-settings`: `approved: true` enables calling on the bound number,
  then reads the actual provider state. It does not overwrite SIP routing.
- POST `/activation`: `enabled: true/false`. Pause prevents future answering and
  the audio worker closes active sessions when it next observes the pause.
- POST `/preview`: configuration only, no external call.
- GET `/calls`: paginated call activity; caller numbers are omitted.
- GET `/permission?recipient=+E164`: fresh provider call permission and action limit.
- POST `/test-calls`: `recipient`, `idempotency_key`; saves an exact call summary.
- POST `/test-calls/{test_id}/approve`: `approved: true`; queue only, never dials
  from the web request. Approval expires after five minutes.
- GET `/test-calls/{test_id}`: saved outcome. SUBMITTED means provider acceptance,
  not proof that a recipient answered or audio worked.

`examples/calling-integration/client.py` is a backend integration example.

## Platform setup

1. Apply migration 023. It pauses existing calling agents while preserving history.
2. Install `pip install '.[voice]'` in the voice worker, or use the Compose `voice`
   profile. Text/API workers do not need WebRTC dependencies.
3. Enable calling in the number's Meta/Chakra settings and subscribe the app/plugin
   to the `calls` webhook field. Existing signed WhatsApp webhook routes ingest calls.
4. Configure Gemini audio access: `AI_CALLING_AUDIO_API_KEY`, or the existing Gemini
   fallback key. Speech defaults: `gemini-2.5-flash` for transcription and
   `gemini-2.5-flash-preview-tts` for speech. Model access/quotas must be checked.
5. Configure ICE/STUN/TURN if required by the deployment network using
   `AI_CALLING_ICE_SERVERS` (aiortc RTCIceServer keyword fields). Check real audio
   connectivity; HTTPS alone does not establish a WebRTC media path.
6. Set `AI_WHATSAPP_CALLING_ENABLED=1` and start
   `python -m agent_platform.calling_worker`.
7. Connect the owned WhatsApp channel, check readiness, activate, then make a real
   inbound test. An outbound test additionally needs Meta's recipient permission.

There is one active media worker per database, enforced by a PostgreSQL advisory
lock, and at most two sessions in the pilot worker. Outbound testing permits one
unresolved request per app and three approvals per rolling 24 hours. This is not a
load-tested hundreds-of-apps deployment. Production needs explicit media sharding,
subscription entitlements, metering and further capacity testing.

## Business knowledge and actions

For a RAG WhatsApp channel, calls use that channel's selected platform documents and
existing grounded-answer flow. For app actions, configure `AI_CALLING_APP_HANDLERS`
privately, keyed by calling-agent UUID. Each entry contains matching `app_id`, an
operator-approved HTTPS `handler_url`, and `handler_secret` (at least 32 characters).

The stable handler contract is HMAC-SHA256 of timestamp + '.' + raw JSON, in
`X-Agent-Timestamp` and `X-Agent-Signature`. A probe includes `probe:true,event_id,
agent_id,app_id`; return `ready:true` and the exact event ID. A voice turn includes
`probe:false,event_id,call_id,agent_id,app_id,caller,input,input_trust,turn,language`.
Return `event_id`, `action` (continue/end/handoff), and `text` (1–1200 characters).
Actions such as appointments remain subject to the app's own authorization and
confirmation rules. Caller ID and transcribed speech do not authenticate a person.
Sahayak is one client of this generic contract, not the owner of the voice runtime.

## Safety, costs and honest status

- Provider mutations are sent once. Timeouts and uncertain responses are quarantined;
  a crash does not requeue a possibly accepted call. UNKNOWN requests require an
  operator to reconcile provider history before another test can be approved.
- Recipients must grant WhatsApp call permission; a platform approval cannot bypass
  it. Permission and provider action limits are checked again immediately before dial.
- Local audio sessions close after 120 seconds and request provider termination.
  This is not a guaranteed billing cap if provider termination is unavailable.
- No recording feature is enabled. Audio remains in memory and is transmitted to
  the configured Gemini account for transcription/synthesis. Provider retention
  rules still apply. Speech turns store hashes and replies; signaling SDP is removed
  after processing. A full retention/erasure policy remains a production gate.
- Business information is treated as evidence, not system instructions. A business
  action turn is committed PROCESSING before invocation; uncertain actions are not
  replayed. Missing knowledge and action errors hand off instead of inventing answers.
- Test-call recipient, exact agent configuration, approver and outcome are durable.
- No claim of free unlimited calls: Meta/Chakra calling, permission messages, AI and
  hosting can have charges. Number eligibility is separate from messaging. Chakra's
  documentation currently excludes coexistence numbers from Calling API support.
- A readiness check and simulated tests do not establish live phone/audio success.

## References

- https://apidocs.chakrahq.com/api-39108030 (calling settings)
- https://apidocs.chakrahq.com/api-39108032 (recipient permission)
- https://apidocs.chakrahq.com/api-39108033 (call signaling)
- https://chakrahq.com/help/chat/whatsapp-calling-api/whatsapp-calling-setup
- https://ai.google.dev/gemini-api/docs/audio
- https://ai.google.dev/gemini-api/docs/speech-generation
- https://aiortc.readthedocs.io/en/latest/api.html
