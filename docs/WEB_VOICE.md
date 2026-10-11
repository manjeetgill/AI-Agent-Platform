# Internet voice agents (local pilot)

A caller presses **Talk to AI** inside an app, consents to audio processing, and allows the microphone. WebRTC sends audio to this platform. The selected speech provider transcribes short turns, the agent answers using selected app knowledge or an explicitly configured app handler, and the selected synthesis provider generates speech back to the caller.

**2026-10-11 update:** See [isolated local setup and current contracts](VOICE_LOCAL_SETUP.md)
for per-agent providers, version restoration, barge-in, transcript consent,
session history, usage limits and the verified local ASR/model/TTS path.
The selected original checkout now uses `http://127.0.0.1:4161/voice` for this
isolated pilot; normal-profile instructions below still use port 4061.

No telephone number, Twilio, Meta authorization, or Chakra account is required. This does not dial a telephone or WhatsApp number. Hosting, AI inference and any TURN relay traffic may cost money. The existing WhatsApp messaging channel is independent.

## Try locally

Run `python scripts/console.py` from the configured virtual environment and open `http://127.0.0.1:4061/voice` in Chrome. The local console is an operator/testing UI, not public customer authentication.

1. Enter an agent name, greeting and business FAQs, then create it.
2. Select the saved agent and review the audio-processing disclosure.
3. Press **Talk to AI**, allow the microphone and speak after the greeting.
4. Press **End call** to release the microphone and server session.

The platform does not retain raw call audio. Local recognition uses a private temporary file, removed after recognition. Audio is sent to the selected speech provider; external providers' terms and retention settings apply. Browser-call text is saved for 24 hours only with transcript consent. Live browser answers do not promote content into durable knowledge answers or memory learning; voice turns save an input hash and outcome. Text rehearsal has the existing knowledge-answer retention policy. App handlers receive recognized words, so their logging must follow the app's privacy policy.

## Private server configuration

- Install the `voice` extra (aiortc).
- Apply migrations, including `024_web_voice.sql`.
- `AI_WEB_VOICE_ENABLED=1` enables the voice API.
- `AI_CALLING_AUDIO_API_KEY` is the private Gemini credential. Keep it out of source control and browsers.
- `AI_CALLING_STT_MODEL=gemini-3.1-flash-lite`
- `AI_WEB_VOICE_MODEL=gemini-3.1-flash-lite`
- `AI_CALLING_TTS_MODEL=gemini-2.5-flash-preview-tts`
- `AI_VOICE_ICE_SERVERS` is an aiortc-compatible JSON array of ICE server settings. Localhost testing can use `[]`.

The existing `AI_NVIDIA` model-dispatch kill switch also controls structured voice answers through the shared gateway. Browser agents select a configured reasoning provider and pinned model, with no automatic provider fallback. Gemini remains the default when no operator overrides are set.

## Integrate another app

Use a dedicated platform service credential with `agents:read`, `agents:write`, `agents:run` and the knowledge permissions required by your setup UI. Scope service credentials per app. Every request carries `Authorization: Bearer ...` and `X-External-User-ID` populated by the **trusted app backend** from its authenticated user, never from arbitrary browser input.

1. Publish business FAQs via `POST /v1/knowledge/documents` with `visibility: "app"`. Do not put private customer records in app-shared FAQs.
2. Create the agent with `POST /v1/voice/agents`: `external_id`, `name`, `greeting`, `documents: [document_id]`, `visibility: "app"`. Private is the default. Agent management remains owner-scoped; app-shared agents can be called by other authenticated users of that app.
3. Serve the widget module from your app and provide an authenticated, CSRF-protected backend proxy limited to the voice endpoints the caller needs. Never expose the operator console or service credential to customers.

```javascript
import {mountVoiceWidget} from './voice-widget.js';

const widget = mountVoiceWidget(document.getElementById('voice'), {
  agentId: configuredAgentId,
  // Fetch short-lived ICE/TURN credentials from your authenticated backend.
  iceServers: sessionIceServers,
  api: async (path, method = 'GET', body) => {
    const response = await fetch('/api/agent-platform/' + path, {
      method,
      signal: AbortSignal.timeout(25000),
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      ...(body ? {body: JSON.stringify(body)} : {})
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail || 'Voice unavailable');
    return value;
  }
});
// On component unmount:
// await widget.destroy();
```

Copy `src/agent_platform/console/voice-widget.js` into your frontend bundle. The proxy maps `voice/...` to `/v1/voice/...`. Allow only the selected app agent and current user's sessions. Keep management/document creation on separate administrator endpoints.

### API

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/voice/capabilities` | Configuration and pilot limits; does not claim audio was verified |
| `POST /v1/voice/agents` | Create an agent, idempotent by owner/external ID; changed inputs conflict |
| `GET /v1/voice/agents?before=UUID` | Owner's agents, 50 per page |
| `POST /v1/voice/agents/{id}/sessions` | Consent + WebRTC offer + unique request key → SDP answer/session ID |
| `GET /v1/voice/sessions/{id}` | Caller-owned session state and actual media connection state |
| `POST /v1/voice/sessions/{id}/end` | End the caller's session |

Duplicate start requests return a conflict rather than starting a second call. A timed-out start is not blindly retried. Without a returned session ID, the reserved call expires within the pilot deadline.

## Optional app actions

By default an agent only answers from its selected FAQs. To add app behavior, configure `AI_WEB_VOICE_HANDLERS` privately as a JSON map from agent UUID to `{app_id, url, secret}`. URL must be HTTPS; secret at least 32 characters. No clinic-specific routing is built into browser voice.

Requests include `event_id`, `session_id`, `agent_id`, `app_id`, `user_id` (platform-mapped identity), `input`, `input_trust: "untrusted_speech"`, `turn` and `channel: "web_voice"`. Verify `X-Agent-Signature` as hex HMAC-SHA256 of `X-Agent-Timestamp + "." + raw_body`, check timestamp freshness and expected app/agent, and deduplicate by event ID.

Return `{event_id, action: "continue" | "end" | "handoff", text}`. `handoff` ends the voice session with the spoken message; it does **not** connect a human. An uncertain handler call is recorded UNKNOWN and never replayed automatically. The app must verify real state before a new business operation. The handler is responsible for enforcing authorization and explicit confirmation before financial/transactional actions; speech itself cannot grant new privileges. No payment tool is included.

## Pilot boundaries and public deployment

- Two concurrent calls across the installation, one per caller, 30 starts per app per rolling 24 hours.
- Two-minute sessions, maximum 15 turns. FAQ answers also use existing app/user planning quotas.
- Bounded utterance recognition, sentence synthesis and interruptible playback. Optional local rolling partial transcripts are provisional, not native ASR streaming. No recording, live transfer or outbound ringing.
- Call media is transient. Audit/session rows survive a restart, but the audio call cannot resume after the media process exits.
- Run one media/API process for this pilot. Multiple processes require sticky session routing or a dedicated media service for status and end controls.
- Public use still needs an authenticated app frontend/backend, HTTPS, reachable WebRTC networking and tested STUN/TURN configuration. A normal HTTPS reverse proxy does not carry UDP audio automatically. Use short-lived TURN credentials and budget relay traffic.
- Test microphone permissions, autoplay, restrictive networks, mobile browsers, language/accent quality and latency before public rollout. The local synthetic test is not proof of public-network reliability.

WebRTC reference: https://webrtc.org/getting-started/turn-server

## Verification on 2026-10-05

- 40 integration/regression tests passed (voice isolation, session lifecycle, consent, concurrency fences, uncertain action handling and existing channel regressions).
- One additional unit test passed for Gemini-only voice gateway configuration.
- Live local synthetic test passed through the real HTTP session API and WebRTC audio: greeting received, spoken opening-hours question transcribed, FAQ answer generated by Gemini, response audio received, session explicitly ended.
- No real microphone, public-network deployment or cross-device test is implied by that synthetic test. The demo page is ready for the owner to perform the microphone test.

## WhatsApp appointment → AI receptionist

Sahayak's native appointment choices now include **Talk to AI receptionist** when the platform advertises internet voice availability. Human reception remains a separate choice. Selecting AI voice leaves the pending booking step unchanged and returns a private browser-call link through the existing WhatsApp dispatch path.

- `AI_WEB_VOICE_PUBLIC_ORIGIN` must be the verified HTTPS origin serving `/voice/call`, `/voice/assets/*` and `/public/voice/*`. Empty means no voice offer in WhatsApp. Do not expose `/v1`, the database or operator console.
- Migration `025_voice_handoffs.sql` binds each invitation to its app, WhatsApp conversation and originating message. Invitations expire in 15 minutes and permit one call; the DB stores a token hash. The WhatsApp reply necessarily contains the private link.
- The token is in the URL fragment, removed from the address bar after loading, and sent in the Authorization header only. The page has no third-party assets and uses no-store/no-referrer headers. Treat the link like a temporary password: anyone holding it can access that conversation during its lifetime. Do not forward it.
- Spoken turns go through the **same signed Sahayak WhatsApp bridge**, patient identity, conversation lock, red-flag checks, consent and booking tools. They do not start a separate voice booking record or use a generic medical chatbot. On joining, the bridge reads the latest pending assistant prompt without executing it as a patient answer.
- Human takeover or a red flag blocks further automatic voice turns. The call does not connect a human automatically; it directs the patient back to reception.
- The browser keeps no persistent invitation credential. If the page is refreshed after use, request a new link in WhatsApp. An interrupted/uncertain call is not retried or rebooked automatically.

For restrictive networks configure `AI_VOICE_TURN_URLS` and private `AI_VOICE_TURN_SECRET` for a coturn REST-authenticated relay. Both media server and authorized invitation page receive five-minute credentials; the shared secret is never exposed. Limit allocations, bandwidth, private-network peers and relay ports. Reference: [coturn authentication and server options](https://github.com/coturn/coturn/blob/master/README.turnserver).

### Speech-rate-limit fallback

Voice images built with `INSTALL_VOICE=1` include `espeak-ng`. Set `AI_VOICE_LOCAL_TTS_FALLBACK=1` to use its basic English/Hindi voice when Gemini speech generation explicitly returns HTTP 429 or 503. This keeps speech generation on the media server for that turn and adds no external TTS provider. Its quality is more robotic. Authentication failures and other errors are not silently masked. Speech recognition still needs the configured Gemini model and quota; this is not fully offline voice.

`deploy/compose.internet-voice.example.yaml` builds the voice-enabled API and a coturn image that needs no Linux capabilities. Configure the relay's shared secret, public bind address, quotas, private-peer restrictions and limited ports in a private file. The pilot uses TCP 3478 and UDP 49160–49200. TURN over TLS/443 and additional mobile/corporate-network tests remain future network coverage work.

## Conversation upgrade (2026-10-05)

Create agents with `language: "auto" | "en" | "hi" | "hinglish"` (default `auto`).
The FAQ responder uses up to four previous question/answer pairs for follow-up
context. That context is untrusted; facts must still cite currently permitted FAQ
passages. Raw conversation history is held only in the media session's memory
and cleared at call end. Live browser voice content is retained only under the optional transcript policy; text rehearsal remains a separate knowledge-answer path.

Hindi/Hinglish questions can fall back to the first six passages, interleaved
across the agent's explicitly selected, app-published documents, when English
lexical search has no match. This is a bounded FAQ fallback, not multilingual
semantic search; long documents can require more specific questions. Language
instructions do not guarantee pronunciation quality. Test real microphones and
code switching before customer activation.

### Text rehearsal

`POST /v1/voice/agents/{id}/preview` is owner-only and requires `agents:run`.
Send `{text, history: [{role: "user" | "assistant", text}], request_key}`.
History is limited to eight messages of 1,500 characters each. A model call uses
the existing per-app/per-user knowledge quotas and saved idempotency outcomes;
timeouts and uncertain outcomes are not automatically retried. The response
includes `{action, text, mode: "knowledge_preview", actions_executed: false}`.
This endpoint never calls a business handler, creates a real appointment or
sends a message. Changing the question/history/language with a used request key
is rejected. New factual questions without matching evidence use the configured
fallback rather than inventing an answer.

Use the local `/voice` screen to select a saved agent and rehearse. Text is sent
to the configured Gemini model; this is not an offline or free-model mode.
The demo clinic is fictional and cannot book real appointments.

### Pause and resume

`POST /v1/voice/agents/{id}/settings` with `{enabled, language}` requires
`agents:write` and agent ownership. Pausing closes that agent's active media
sessions in the current process and prevents new sessions/turns. An action
already accepted by a connected business cannot be undone by pausing. Keep
media control on the same runtime, as required by the existing single-process
pilot. Call expiry is checked at each dialogue turn, in addition to the audio
deadline. The app handler receives the preferred `language` and bounded
`conversation` as untrusted context; the Sahayak handoff continues to use its
own booking state and safety checks.

This upgrade is local-workspace tested. It does not activate PSTN/mobile calling,
WhatsApp Calling, live human transfer, Sarvam, or a new telephone provider.

Verification for this upgrade: 55 automated checks pass, including FAQ isolation,
request replay protection, per-call memory bounds, cross-caller isolation,
expired-call rejection, owner-only pause, and sanitized provider-quota errors.
Live Gemini requests returned correct fictional opening hours, Sunday closure,
and appointment fees in Roman Hinglish. A booking request used the fallback
instead of confirming a booking. Some live requests returned MODEL_UNAVAILABLE;
the rehearsal reports these failures and performs no automatic retry. This does
not establish reliable real-time performance or spoken Hinglish quality.

## Current provider choice

The internet voice product uses our own agent/runtime, WebRTC, the existing
Gemini account and optional self-hosted TURN relay. There is no Sarvam or Plivo
integration or account requirement. Speech/model usage and server hosting still
have costs. Ordinary telephone calls and SIM forwarding have no receiving number
in this configuration.

Apps can call `GET /v1/voice/agents/{id}/readiness` with `agents:run` before showing
microphone permission. It verifies caller access, enabled state, runtime
configuration, currently published FAQs and operator handler configuration.
`ready_to_attempt` means ready to try a call; it does not certify model quota,
audio quality or external business-handler health. The local widget runs this
check before requesting microphone access. For another app's widget, pass an
async `beforeStart(agentId)` callback that calls this endpoint and throws on a
blocked result. Keep all service keys on your authenticated backend.

## Optional Asterisk SIP pilot

For the local SIP calling server, echo test, and FAQ audio bridge, see
[ASTERISK.md](ASTERISK.md). It runs separately from browser voice. A carrier
connection is still required for ordinary mobile-number calls.
