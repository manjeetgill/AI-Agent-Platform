# WhatsApp to internet voice pilot — 2026-10-05

## Verified and enabled

- Public call page: `https://agents.myalgodesk.in/voice/call` (a valid WhatsApp invitation is required).
- Only `/voice/call`, `/voice/assets/*`, and `/public/voice/*` were added to the public proxy. Private `/v1/voice/agents` still returns 404; an invitation request without a bearer token returns 401.
- API and WhatsApp worker use `agent-platform:internet-voice`. Migrations 024 and 025 applied. The older WhatsApp Calling worker remains paused.
- Own authenticated coturn relay on the existing Droplet, TCP 3478 and UDP relay ports 49160–49200. It runs as an unprivileged user with no Linux capabilities, capped at 128 MB, 0.25 CPU, eight allocations and bounded bandwidth. Its base image digest is pinned in `Turn.Dockerfile`.
- TURN shared secret stays in the private server configuration. Only expiring five-minute HMAC credentials reach a caller. No telephony or WhatsApp Calling provider is involved.
- Gemini TTS returned HTTP 429 during verification. Optional eSpeak fallback is enabled in this pilot; it is less natural. Gemini speech recognition is still required.
- Real cross-network synthetic test forced **relay candidates on both peers**: HTTPS invitation → WebRTC → greeting audio → spoken clinic-timings question → existing Sahayak bridge → DONE turn → response audio → clean end. No WhatsApp message was sent by this test, and no appointment was created.
- Platform regression suite: 44 tests, plus two added TURN/fallback tests passed. Sahayak: 30 bridge/safety/isolation tests, 25 shared-language tests, API typecheck, and 48-message safety evaluation passed. One pre-existing optional bridge test was skipped. Changed-file lint had no errors.

## Remaining user test and availability limits

Send `Hi` in the connected clinic WhatsApp chat, choose **Talk to AI receptionist**, open the private link, consent to audio processing, and permit the microphone. Ask for clinic timings, then test continuation of a booking in the approved pilot clinic. The user phone/microphone test is separate from the completed synthetic test.

The current Sahayak HTTPS booking bridge still relays to the local Sahayak API through the existing SSH tunnel. Keep that computer/API/tunnel running for this pilot. This is not a fully independent public customer deployment. Restrictive networks that require TURN-over-TLS on port 443, mobile/in-app-browser behavior, and broader language quality still need testing.

## Operations / rollback

Server root: `/opt/agent-platform/pilot`. Include `-f deploy/compose.internet-voice.json` after the base and small-host Compose files; omitting it can revert the media image. The private environment file is mode 0600. Coturn config/rollback backups are under `/opt/agent-platform/secrets/internet-voice`; never publish them.

To stop new WhatsApp voice offers, clear `AI_WEB_VOICE_PUBLIC_ORIGIN` and recreate the WhatsApp worker using the three Compose files. Existing calls are bounded to two minutes. Full rollback should restore the saved environment and preceding API/worker images; keep additive tables for audit. Remove public voice route additions and the two voice firewall rules only when shutting the pilot down. Do not affect the main app or existing WhatsApp messaging routes.

The existing Caddy overlay must still be preserved/reapplied after an NRAIAlgo release (see `PILOT_STATUS.md`). Current main app health remained HTTP 200 after deployment.

## Conversation release — 2026-10-05

The agent API now uses `agent-platform:voice-conversation-20261005`, built as a
narrow overlay on the previously deployed API image. Migration 026 adds the
language preference without removing data. Other application/worker images are
unchanged. The current API image is selected in the existing
`deploy/compose.internet-voice.json` override.

- 56 local automated checks passed before deployment.
- API health and main-app login returned 200 after deployment.
- Public voice page returned 200; private voice API returned 404; invitation
  requests without a token returned 401.
- Database and previous Compose/image snapshots are private on the server under
  `/opt/agent-platform/secrets/internet-voice/conversation-20261005T170550Z`.
- No Plivo, Sarvam or Twilio account, SDK, credential or subscription was added.
- The Sahayak bridge tunnel was found disconnected during verification. Its
  existing loopback-only forwarding was restarted. This laptop dependency remains
  an operational limitation; a tunnel restart is not a permanent deployment.

### Tomorrow: owner test

1. Open `http://127.0.0.1:4061/voice` on this computer. Select the saved fictional
   **Hinglish receptionist — demo**. Check the displayed readiness message.
2. Choose **Talk to AI**, accept the audio-processing disclosure, and allow the
   microphone yourself. Ask one question, wait for the answer, and ask a follow-up.
   The current pilot does not support interrupting the assistant mid-sentence.
3. Check whether Hindi/English names, timings and mixed-language speech sound
   correct. If Gemini reports quota/rate-limit problems, inspect the existing
   Gemini account quota; no replacement provider is silently selected.
4. For the Sahayak booking pilot, keep the local API/tunnel online and use the
   private invitation from the already connected clinic chat. Confirm the exact
   appointment details before completing a real booking. Do not share the link.

There is no telephone forwarding destination for `7703961610` in this setup.
Do not activate SIM forwarding to a guessed number. No new-provider signup is
part of tomorrow's internet-voice test. Full independent clinic availability
requires deploying the Sahayak booking API and database, beyond this voice update.

Post-release public audio verification passed after tunnel restoration: both
peers used TURN relay candidates; greeting and response audio were received;
the saved synthetic call ended with one DONE turn and the expected clinic-timings
reply. The API remained healthy. This synthetic check sent no WhatsApp message
and created no appointment.

The existing general pilot service credential returns 403 for the private voice
API; its scopes were not expanded. Before embedding `/v1/voice` directly in an
additional app, provision an explicitly authorized, app-scoped credential with
only the voice/knowledge permissions needed by that app. The verified public
WhatsApp invitation flow uses its existing conversation-bound authorization.
