# Private server-to-phone SMS pilot

This pilot connects the deployed SMS worker to an Android Traccar local service.
The phone and the operator Mac must remain awake, online, and on the same trusted
LAN. It is not an always-available public SMS service.

## Path

App backend → HTTPS Agent Platform API → durable SMS worker → private Docker
bridge relay → server loopback SSH remote forward → operator Mac → Android phone.

`deploy/sms_private_relay.py` binds only an RFC1918 Docker bridge address, admits
only that subnet, and forwards only to server loopback. It never parses or logs
SMS contents. Connections are bounded to eight and time out when idle. Do not
publish its port, alter public firewall rules, or enable global SSH GatewayPorts.

The Mac LaunchAgent reconnects SSH with server-alive probes and strict host-key
checking. Its dedicated key is restricted on the server to one loopback remote
forward, with no interactive commands, PTY, agent forwarding or useful local
forwarding. Private keys and Traccar tokens stay in mode-0600 runtime files.

## Configuration and verification

Keep one app-scoped SMS route, credential and OTP template for MyAlgoDesk. Only
explicit pilot recipients are admitted. The app generates and validates its own
OTP and retains its own resend/attempt limits. Transport acceptance is not login
and is not a delivery receipt. Keep the original queued request ID after timeouts.

Before activation: run the no-send Traccar auth/slot probe from the actual server
network, then one recipient-approved queue test through the public API. Confirm
receipt on the handset. Phone login also needs an authenticated account owner to
link the phone with their password and received code. Never pre-link a number by
writing the binding into the database.

## Operations

The production deployment retains its private configuration independently of Git.
Use the saved Compose release for API/worker restarts, preserving other apps and
providers. Back up settings before changes. Rollback disables the app route and
restores that configuration; do not replay uncertain messages.

When the Mac or phone disconnects, gateway checks fail and queued SMS requests
expire. Email sign-in remains available. For dependable production delivery,
replace the Mac bridge with a supported private network directly to the device
or configure an authorized hosted SMS provider.
