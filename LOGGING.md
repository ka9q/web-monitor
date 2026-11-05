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

Listener file and routine logs
------------------------------

The supplied systemd service writes listener activity exclusively to:

    /var/log/web-monitor/listeners.log

Entries include a timestamp followed by the existing listener JSON record.
The file rotates at 10 MiB with five backups (about 60 MiB total). systemd
creates a private directory for DynamicUser; no manual chown is needed.
Do not also configure external logrotate for this file.

    sudo tail -F /var/log/web-monitor/listeners.log

Systemd start/stop messages and application operational warnings/errors retain
their existing journal/syslog routing. Routine HTTP access logging is disabled;
--access-log restores it when troubleshooting. Other libraries may still emit
messages to the journal. This change does not alter rsyslog configuration.

Outside the supplied service, pass --listener-log /writable/path/listeners.log.
The parent directory must exist. Without that option listener records still go
to stderr/journal. An unwritable log file causes startup to fail visibly.

Installing this update
----------------------

Extract the update into your web-monitor source directory. For a local Makefile
installation, update both the application and service (install-app alone does
not update the service):

    sudo make install-app install-service
    sudo systemctl daemon-reload
    sudo systemctl restart web-monitor

For a Debian package installation, rebuild/install the package instead, then:

    sudo systemctl daemon-reload
    sudo systemctl restart web-monitor

If a manually installed /etc/systemd/system/web-monitor.service overrides the
packaged unit, update that unit too. Check with systemctl cat web-monitor.
Preserve any local bind/TLS options when adapting ExecStart.
