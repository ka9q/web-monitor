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

The service tags its journal output with SyslogIdentifier=web-monitor. The
rsyslog rule in debian/30-web-monitor.conf selects only the listener logger's
records and writes /var/log/web-monitor.log, owned by root:adm, mode 0640.
The stop action prevents listener records from also filling /var/log/syslog.
Listener records remain visible in the journal. Service start/stop messages,
errors and other operational messages retain their normal syslog routing.
Routine HTTP access logging is disabled; --access-log enables it for debugging.

DynamicUser remains enabled. No private log directory or static account is
needed: rsyslog owns the file. Do not pass --listener-log in this setup; that
optional argument writes directly to a file instead of the journal.

Logrotate rotates weekly, or above 10 MiB at its next scheduled run, retaining
five backups. Files and rotated logs are readable by root and the adm group.

Install rsyslog and logrotate if missing. Extract this update in your source
tree, rebuild/install your Debian package, then run:

    sudo rsyslogd -N1
    sudo systemctl restart rsyslog
    sudo systemctl daemon-reload
    sudo systemctl reset-failed web-monitor
    sudo systemctl restart web-monitor

For a Makefile installation, instead of rebuilding the package, run:

    sudo make install-app install-service install-logging

Then run the same validation/restart commands above. Check systemctl cat
web-monitor: a manually installed /etc/systemd/system unit overrides a packaged
unit. Update any such unit too, preserving local bind/TLS arguments. Remove
LogsDirectory and the old --listener-log argument; add SyslogIdentifier=web-monitor.

Test routing without making a listener connection:

    logger -t web-monitor 'INFO:web-monitor.listener:listener {"event":"routing_test"}'
    sudo tail /var/log/web-monitor.log

Do not delete old logs or /var/log/private; they are no longer used by this
configuration. No permission changes to /var/log/private are necessary.
