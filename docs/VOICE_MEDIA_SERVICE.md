# Voice media service and authenticated session contract

Status: implemented and locally tested. Public deployment and human/mobile acceptance are separate gates. See [acceptance report](../reports/FINAL_ACCEPTANCE_REPORT.md).

## Ownership

The public API authenticates the consuming app and user, checks consent, agent permissions and quotas, and commits a STARTING session with an immutable agent snapshot. The private media process claims that reservation once, owns WebRTC and the conversation, and writes scoped events and usage. The browser continues to use the existing `/v1/voice` API; it never receives the internal signing key.

The two processes share the platform database and conversation code. This is process separation, not a distributed media scheduler. API restarts can preserve established media. Losing the media process ends its calls; no automatic continuation or replay is claimed. The local development launcher stops companion processes together; production must supervise API and media independently.

## Contract v1

Audience: `agent-platform-media-v1`. Private routes:

| Method and path | Result |
| --- | --- |
| `GET /health` | Content-free process liveness; no authentication or configuration disclosure |
| `POST /internal/v1/readiness` | Authenticated database-manifest and configured audio-runtime check |
| `POST /internal/v1/status` | Authenticated active-call count, draining flag and uptime |
| `POST /internal/v1/drain` | Reject new calls; keep existing calls until they end; restart to clear drain |
| `POST /internal/v1/sessions` | Claim the already reserved session and return its SDP answer |

Session payload (all identifiers are UUIDs):

```json
{
  "session_id": "<reserved-session-id>",
  "app_id": "<authenticated-app-id>",
  "owner_id": "<authenticated-caller-id>",
  "sdp": "<original-WebRTC-offer>"
}
```

Extra fields are rejected. The signed body cannot replace the stored agent profile, consent, permissions, provider configuration or spending limits. SDP must match the reserved SHA-256 hash. Session ownership, enabled agent, expiry, STARTING state, and an unused claim are checked inside a database transaction. The reservation must be younger than 30 seconds.

### Authentication

Use a dedicated random secret of at least 48 characters, stored only in private API/media configuration. Each request sends:

- `X-Media-Time`: integer Unix seconds, within 30 seconds of the receiver.
- `X-Media-Nonce`: canonical random UUID, used once.
- `X-Media-Signature`: lowercase hexadecimal HMAC-SHA256.

HMAC input is these six newline-separated UTF-8 fields, with no trailing newline:

```text
agent-platform-media-v1
POST
/internal/v1/sessions
<timestamp>
<nonce>
<SHA-256 hexadecimal digest of the exact raw request body>
```

Query strings are rejected. Replay protection uses a shared database nonce table, so multiple HTTP workers cannot reuse a signed request. Nonces expire after 61 seconds. Raw request bodies are limited to 70,000 bytes and SDP to 65,536 characters. Do not log signatures, SDP, credentials, transcripts or raw audio.

HTTPS is required between hosts; loopback HTTP is supported locally. On a single Linux host, use `AI_VOICE_MEDIA_SOCKET` with an absolute private Unix-socket path and `AI_VOICE_MEDIA_URL=http://localhost`. The socket directory is owned by container UID 10001 with mode 0700. Do not expose the internal API through Caddy. HMAC is required even over the private socket.

### Outcomes and lifecycle

- 201: SDP answer plus session ID, CONNECTING state and expiry. It is not proof the caller heard audio.
- 401: missing, tampered or stale signature.
- 404: no eligible reservation for the supplied identity, including a session already past STARTING.
- 409: duplicate nonce, competing claim, changed offer or expired claim window.
- 413/422: oversized or malformed request.
- 503: unavailable or draining media runtime; unrelated platform features remain usable.
- Public API returns 502 and records UNKNOWN if dispatch outcome is uncertain. It never retries the offer automatically.

Client connect timeout is 2 seconds; request timeout is 23 seconds. SDP negotiation is bounded to 20 seconds. A media heartbeat checks durable authority every second. End, pause, deletion or expiry stops audio on the next check. Remote `/end` returns `media_stop_requested`; the `ended` event confirms cleanup. More than 10 seconds without a media heartbeat changes observed/admission state to UNKNOWN. Database authority-check failures stop media.

Every media call retains a bound application/caller database scope for callbacks after the HTTP request ends. The media process uses the restricted API database role, never a migration or unrestricted worker credential. Rates/limits are operator-owned; runtime cannot raise them. Global admission uses a count-only database function under the existing transaction lock, preserving the two-call limit across apps despite row-level security.

## Configuration and deployment

`AI_VOICE_MEDIA_URL` enables extraction. Without it the embedded compatibility runtime remains available. API and media share `AI_VOICE_MEDIA_SECRET`; audio/model settings stay on the media process. `AI_PLATFORM_DATABASE_URL` must be a restricted API credential for media. Public WebRTC also needs a verified ICE/TURN path.

`deploy/compose.media-service.yaml` is a prepared overlay, not proof of deployment. Its 512 MiB limit is for a media gateway using external speech/model services; it cannot host the pilot's Whisper/Ollama models. Provision and size those separately if using local inference. The current Docker voice image does not install a Whisper model automatically.

For maintenance: send signed drain, poll signed status until active_sessions is zero, then restart media. Do not force-restart a busy media process expecting calls to resume. Rotate the shared signing key during a drained coordinated API/media rollout; existing audio does not require another claim.

## Production reconciliation

The selected original checkout was based on `9d59fa7`; production runs `1392440`, with security/SMS migrations through 057. With owner approval, the production baseline is being merged into this folder. Voice migrations now follow it as 058–064. The old isolated database with voice migrations 050–054 is preserved, never rewritten or used with the new manifest. A fresh local database verifies the combined migration history. Do not apply the new lineage to the preserved old pilot database.

## Verification

`tests/python/test_media_service.py` covers authentication, body tampering, replay, cross-app and owner denial, duplicate claims, unavailable media, drain and uncertain dispatch. `scripts/verify_media_restart.py` provisions synthetic fixtures with a restricted worker and launches its own temporary API on port 4190; it never restarts the user's API. It verifies greeting audio, restarts only that API, asks a question and checks the spoken answer and ENDED state.

Run with the isolated voice services:

```sh
.venv-voice/bin/python scripts/verify_media_restart.py
.venv-voice/bin/python scripts/verify_voice_load.py --waves 5
```

Synthetic tests do not verify a person's microphone, mobile networks, real business writes, carrier calls or production sustained capacity.
