Listener logging
================

The optional "Your name or callsign" field is saved in browser localStorage.
It is self-reported, not authenticated, and can be changed during a session.
The field tells listeners it is recorded in the server log.

INFO messages prefixed "listener " contain JSON. Events are connect (session
created, not proof of connected media), connection_state, selection, identity,
and disconnect. A separate random log session identifier correlates events
without adding the session API access token to these messages. Existing aiohttp
access logs are unchanged and can contain session URLs.

Selection events contain the full current subscription list, including channel
ID, name, group name, source IPv4 address, SSRC, and frequency_hz when known.
Unchanged selection heartbeats do not emit events. A browser's local mute or
volume setting does not change its subscriptions and is not logged.

Disconnect events give elapsed seconds and a reason: client_disconnect,
failed/closed, disconnected, idle_timeout, invalid_offer, negotiation_failed,
or server_shutdown. Abrupt browser exits may only be detected later. A killed
process cannot log its own shutdown.

The remote field is the HTTP peer address. Forwarded headers are intentionally
not trusted; behind a reverse proxy this will normally be the proxy address.
User-controlled values are JSON-escaped to prevent forged multiline entries.

Read the events under systemd:

    journalctl -u web-monitor -f -o cat | grep 'listener {'

Install the updated server.py and static files (make install-app, or rebuild
and install the Debian package), restart web-monitor, and refresh the browser.
No config or service-file changes are needed for logging.
