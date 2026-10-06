# Two-app provider pilot — 2026-10-06

## Deployed

- API and WhatsApp worker: `agent-platform:providers-20261006t113900z`.
- Migration 029 applied. Sarvam speech adapter installed; existing active speech selection preserved.
- Chakra partner settings reuse the existing team webhook secret and Graph API version.
- Existing Sahayak booking bridge passed a signed readiness probe.
- Reach CRM Demo Organization uses a distinct platform app and seven-day service credential.
- Reach cannot access the Sahayak conversation endpoint (404 verified).
- Reach signup session created successfully through Chakra; owner authorization still pending.
- Both pilot apps: seven-day trial, 25 WhatsApp turns/day, 25 sends/day, zero telephone calls until configured.
- These are operation limits, not provider subscription/rental or monetary budget caps.

## Local Reach

- UI: http://localhost:13000/ → WhatsApp agent → Continue WhatsApp connection.
- Saved business number: +91 82196 13387. Existing Sahayak number was not reassigned.
- Private configuration: `.local/agent-platform.json` inside Reach CRM (never commit).
- Reach API migration 0009 applied with a SQLite backup; API restarted on port 18000.
- Temporary callback relay: platform `scripts/reach_pilot_callback.py`, port 18001.
- Cloudflare tunnel/process logs: platform `.runtime/local/reach-pilot/` (private runtime).
- Only the signed callback POST is accepted. UI/docs/other routes return 404; unsigned callback returns 403.
- Temporary tunnel depends on this Mac. A permanent deployment is required before public customers.

## Still required

1. Owner completes Chakra/Facebook signup and OTP for Reach’s number.
2. Operator verifies account/plugin ownership, prepares channel, and verifies provider webhook.
3. Activate only after a controlled real send/reply test; no live Reach delivery has yet been demonstrated.
4. Telephone number/carrier onboarding is separate from Sarvam speech API access. No real phone call has been made.
5. Perform the real two-app messaging/calling acceptance test after those prerequisites.

## Verification

33 targeted platform regression tests passed before deployment. Reach TypeScript and Sahayak web type checks passed.
Public service endpoints require authentication (401), operator console remains unavailable publicly (404), main app login remains healthy (200).
Provider keys were verified directly: existing Chakra plugin configuration read (200), short Sarvam audio response decoded successfully.
