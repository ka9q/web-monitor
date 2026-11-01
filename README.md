# Web monitor — initial prototype

A standalone browser listener for existing ka9q-radio Opus/RTP multicast streams.
No receiver tuning controls and no dependency on ka9q-web.

- Discovers `_opus._udp.local.` advertisements, joins their IPv4 multicast
  groups, and discovers streams by group,
  source IPv4 address, and SSRC. Multiple SSRCs per group are supported.
- Forwards the encoded Opus payloads unchanged through aiortc's encoded-packet
  path. WebRTC provides new RTP headers, DTLS-SRTP, ICE, and RTCP.
- One WebRTC peer connection per browser. Each stream has its own audio track.
  Browser Web Audio mixes tracks with individual and master volume controls
  and independent left/center/right stereo pan. Double-click a pan slider to
  center it; pan positions are remembered and restored on reconnect.
- Checkboxes control forwarding at the server. Unchecked tracks send no audio
  RTP. Muting or turning the volume down does **not** unsubscribe.
- Bounded per-listener queues discard old audio instead of building latency.
- Remembers selections and volumes in browser local storage; reconnects after
  connection failures. Stale sessions expire after 90 seconds without heartbeat.

## Install

Python 3.10+ on Linux is the intended deployment environment. The prototype was
also tested on macOS. Install in a virtual environment; it does not need root.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Debian, install `python3-venv` if necessary. Dependencies normally have binary
wheels on current platforms. On older systems or architectures where PyAV has
no wheel, its installation may need FFmpeg development libraries and a compiler.

## Quick demo

```bash
.venv/bin/python server.py --demo
```

Open **http://localhost:8080/** on that computer. Check one or both test channels
to start listening. Selecting a channel starts audio immediately. The first is a continuous 440 Hz tone; the second is
660 Hz and alternates between three seconds of audio and three seconds of silence.
The demo generates Opus and feeds the same RTP parser and forwarding pipeline,
but does not join real multicast groups.

## Connect your receivers — automatic discovery

Install the dependencies and copy the minimal example configuration:

```bash
sudo install -d -m 0755 /etc/radio
sudo install -m 0644 config.example.json /etc/radio/web-monitor.json
.venv/bin/python server.py --config /etc/radio/web-monitor.json
```

Edit an existing configuration rather than overwriting it. Discovery can be
selected with just:

```json
{"discovery": true}
```

The gateway browses `_opus._udp.local.` via mDNS, reads the SRV port and the
IPv4 multicast addresses of its target, and joins each advertised stream.
ka9q-radio's Opus payload type 111 is the default. No unicast DNS queries or
Avahi daemon/D-Bus access are needed. The service instance name labels the group;
individual channels retain their SSRC identity and display the tuned frequency
from radiod status beacons.

On Linux, automatic interface selection uses up, multicast-capable interfaces
and excludes loopback; it skips ordinary WireGuard links. To choose the LAN
explicitly, provide its local **IPv4 address**, not its interface name:

```json
{
  "discovery": {
    "interfaces": ["192.168.1.10"],
    "payload_types": [111]
  }
}
```

SRV updates change memberships; goodbye/expiry removes them once no other service
references the endpoint. Channels expire after the configured inactivity timeout;
browser selections remain saved for streams that return.
Only multicast IPv4 targets are accepted, with a default limit of 64 discovered
memberships and 128 tracked service/interface pairs. Override those with
`discovery.max_groups` and `discovery.max_services` if needed. mDNS discovery is
local-link and unauthenticated: enable it only on receiver LANs you trust, because
advertised audio becomes available to listeners. Existing multicast groups do
not need to cross the Internet; WebRTC handles delivery to the browser.

Open **http://localhost:8080/** on the gateway computer and select a channel.
For a browser on another machine, use trusted HTTPS, either with `--cert` and
`--key` or through your existing HTTPS reverse proxy. Preserve its original Host
header. The supplied configurations listen on both `127.0.0.1:8080` and `[::1]:8080`
and have no login layer. A `listen` list creates separate IPv4 and IPv6 listeners.

### Manual groups and labels

Existing configurations with a nonempty `groups` list retain manual-only
behavior unless you explicitly set `discovery` to true. With no manual groups,
discovery defaults to enabled. Set `"discovery": false` to disable it.
`config.manual.example.json` shows the previous explicit group configuration.
It uses numeric IPv4 multicast addresses and a local interface IPv4 address.
The `payload_types` list must identify the Opus payloads, never PCM or IQ.

Manual groups may coexist with discovery and take precedence for a matching
endpoint. Their existing labels and channel IDs are preserved. Optional channel
entries provide `ssrc`, `name`, `frequency`, `mode`, and `source`; an explicit
source and SSRC can list a configured silent channel before its first packet.
SSRC may be a JSON integer or a hexadecimal string such as `"0x1234abcd"`.

HTTP(S) carries only the page, catalogue, negotiation and subscription changes.
Audio uses the separately negotiated WebRTC UDP path. An HTTPS proxy or SSH HTTP
port-forward does not relay the WebRTC media.

## Network / NAT

The supplied configurations use Cloudflare's public STUN endpoint:

```json
"ice_servers": [{"urls": "stun:stun.cloudflare.com:3478"}]
```

STUN helps the browser and gateway gather numeric address candidates, including
when browsers hide host addresses behind mDNS names. Audio is not relayed through
the STUN server. Set `"ice_servers": []` for host-only ICE with no external STUN;
that may fail across networks or with browser mDNS candidate obfuscation.
These are config-file defaults; demo mode and configurations omitting
`ice_servers` still use host-only ICE.
WebRTC binds dynamic UDP ports, so firewall rules must permit the negotiated
media path; opening TCP 8080 alone is insufficient.

For Internet listening, configure your own STUN and/or TURN service as needed:

```json
"ice_servers": [
  {"urls": "stun:stun.example.net:3478"},
  {"urls": "turn:turn.example.net:3478?transport=udp",
   "username": "example", "credential": "example"}
]
```

These examples are placeholders. The browser receives these ICE settings, so
TURN credentials must be intended for clients, preferably short-lived. TURN
support and remote NAT/firewall behavior have not been tested in this initial
version. There is no application audio-over-TCP fallback, although a configured
TURN transport can change the underlying path.

For a metered receiver link, place the gateway on the same multicast LAN to
avoid shipping unused input audio across the WAN. It continuously monitors all
configured input groups even when nobody is listening. Only selected channels
are forwarded to each browser, but ICE keepalives, RTCP (including for negotiated
idle tracks), five-second catalogue polling, and subscription heartbeats still
use bandwidth. Click **Disconnect** to close the WebRTC session.

## Live test checklist

1. Start with one known Opus group and verify channels appear. The catalogue
   endpoint `/api/channels` includes received packet counts, source/SSRC,
   last-packet age, and rejected/ignored packet counts.
2. Select a channel and verify the page says **WebRTC connected**.
3. Select another and verify both are audible, with independent volume controls.
4. Uncheck one and verify its outbound audio stops while the other continues.
5. Exercise squelch and a receiver restart. Audio should resume without replaying
   old queued audio. Reload the page to check remembered selections.
6. Open a second browser to check independent selections and shared input groups.

A channel's **Receiving / Quiet** indicator reports recent RTP arrival, not an
actual audio level or an authoritative squelch state. Opus DTX or network loss
may also produce a Quiet indication.

If the list stays empty, check the multicast interface, group, UDP port and RTP
payload type. Numeric group addresses are required in this first version; it
reads radiod status on UDP 5006 by default. For discovery, check that mDNS on UDP
5353 is allowed on the receiver LAN and look for `Discovered` messages in the log.
If the list works but audio does not, inspect browser developer tools and gateway
logs for ICE/DTLS errors, check HTTPS/browser audio permission, and check UDP rules.
Use Firefox `about:webrtc` or Chromium `chrome://webrtc-internals` for transport stats.

## Tests

```bash
.venv/bin/python -m unittest -v
```

The tests cover RTP extensions/padding, payload-type filtering, SSRC separation,
sequence/timestamp wrap, duplicate suppression, queue bounds, unchanged encoded
Opus payloads, real loopback multicast reception, automatic mDNS resolution/join/withdrawal, and a local HTTP-signaled
ICE/DTLS/SRTP connection with decoded audio and subscription gating.
They require local network socket permissions.

## Initial-version limits

- IPv4 multicast input only. One Opus packet per RTP payload, with a standard
  48 kHz Opus RTP timestamp clock. PCM, IQ, and other codecs are not supported.
- Tuned frequency is decoded from radiod status beacons. Mode and repeater
  identification are not decoded yet; names/modes may still be configured
  manually. Quiet channels remain listed while audio/status activity renews their
  lifetime; inactive entries expire after five minutes by default.
- Maximum 256 discovered channels, 32 negotiated tracks per browser and 16
  simultaneous sessions by default, configurable via `max_channels`,
  `max_channels_per_session`, and `max_sessions`.
- Channels negotiated in a session can be toggled without renegotiation. Selecting
  a newly discovered channel outside that session rebuilds the connection and
  briefly interrupts playback. When over the track limit, selected channels
  receive priority in the negotiated set.
- Input duplicates and late/out-of-order packets are discarded, not reordered.
  Source timestamp gaps are retained, but input RTP sequence numbers are not:
  aiortc generates new ones. This is a starting point for clean local multicast,
  not a full RTP translator preserving upstream loss reporting.
- Opus payloads are shared unchanged. There is no per-listener bitrate adaptation
  or transcoding, so choose source bitrates suitable for listeners' connections.
- All selected channels are mixed; lower channel/master gains to avoid clipping.
- Browser UI verified in the Codex Chromium-based browser. Safari, Firefox,
  mobile/background playback, and actual ka9q-radio streams still need live tests.

## Implementation references

- aiortc: https://github.com/aiortc/aiortc
- Encoded Opus packing: https://github.com/aiortc/aiortc/blob/main/src/aiortc/codecs/opus.py
- WebRTC transports: https://www.rfc-editor.org/rfc/rfc8835.html
- Opus RTP format: https://www.rfc-editor.org/rfc/rfc7587.html

Selecting a channel provides the browser gesture needed to start audio; there is
no separate Enable audio step. After a page reload, saved selections do not
autoplay: click **Start selected audio** or select another channel.

## Local Debian package

A `debian/` directory now provides a native `web-monitor` package. See
`debian/README.Debian` for build instructions, dependency compatibility notes,
and migration from the manually installed service. It retains this installation's
`/usr/local` convention and uses `/etc/radio/web-monitor.json`. Unlike the manual
virtual-environment installation, the package uses Debian Python dependencies.
The `/usr/local` layout is deliberately for local use, not Debian archive policy.

## Upgrading to discovery (0.2.0)

Install the new dependencies (`pip install -r requirements.txt` in your venv,
or `apt install python3-zeroconf python3-ifaddr` for the packaged system Python).
Copy **all four modules**: `server.py`, `core.py`, `demo.py`, and `discovery.py`.
Set `"discovery": true` in `/etc/radio/web-monitor.json`; remove the manual
`groups` entries if you want advertised group names rather than manual overrides.
Then restart `web-monitor`. No change to the hardened service is required.
Discovery needs UDP 5353 on the LAN in addition to the RTP multicast groups.
The new code was tested with actual local mDNS announcements, multicast input,
withdrawal, and WebRTC; ka9q-radio advertisements still need a live field test.

## Local installation with make

From the source directory (Python 3.10+ and venv support required):

```bash
sudo make install-deps
sudo make install
sudo systemctl daemon-reload
sudo systemctl enable --now web-monitor
```

Defaults are `/usr/local/bin/web-monitor`, private code/assets and a venv under
`/usr/local/lib/web-monitor`, `/etc/radio/web-monitor.json`, and the local unit
`/etc/systemd/system/web-monitor.service`. Existing configuration is preserved.
The dependency target uses pip only inside that private venv. The ordinary
`install` target does not download dependencies or change service state.

To use Debian-provided dependencies instead of a venv:

```bash
sudo apt install python3-aiohttp python3-aiortc python3-av python3-zeroconf python3-ifaddr
sudo make install APP_PYTHON=/usr/bin/python3
```

For source/UI updates with the existing local virtual environment:

```bash
sudo make install-app
sudo systemctl restart web-monitor
```

If using system Python, pass `APP_PYTHON=/usr/bin/python3` on `install-app` too.
Run `install-deps` again when requirements change if using the private venv.
Use `make install DESTDIR=/some/staging/tree` to stage an installation; embedded
paths refer to the target system, not the staging tree. Prefix/directory variables
are overridable; the default keeps the existing flat Python-module/static layout.
Directory paths should be conventional paths without whitespace or sed delimiters.

`install-service` replaces the local unit with the current hardened template.
Keep machine-specific changes in a systemd drop-in. Do not mix local and Debian
package ownership of the same files; a unit in `/etc/systemd/system` overrides a
package's unit under `/usr/lib/systemd/system`.

The channel-list sort control supports name, numeric frequency (unknown values
last), group, activity (receiving first in ascending order), and numeric SSRC.
Direction and optional Selected first grouping are saved in browser preferences.
Sorting moves existing rows without changing their audio tracks or subscriptions.

Local Makefile interpreter selection: an existing `$(VENV)/bin/python` is used
when present; otherwise the launcher uses `/usr/bin/python3`, matching the Debian
package. `APP_PYTHON` overrides this choice. Installation does not create a venv
unless `install-deps` is explicitly run.

## Channel frequency from radiod status (0.3.0)

For every joined audio group, the gateway now also listens to **the same
multicast address on UDP 5006**, radiod's default status-beacon port. This works
for both manually configured and mDNS-discovered groups. It passively receives
beacons; it does not send commands, retune receivers, or request status polls.

Status TLVs `OUTPUT_SSRC` (18) and `RADIO_FREQUENCY` (33) associate the frequency
with the existing group's source-IP/SSRC channel identity. When present, the
output destination-socket TLV is checked against the audio group's address and
port. Status from other sources/SSRCs remains separate. Beacons can create a
quiet channel before its first audio packet. Partial updates preserve the last
frequency; malformed messages are rejected before applying any fields.

The page displays the frequency and uses its numeric Hz value for sorting.
The five-second catalogue refresh picks up frequency changes without rebuilding
WebRTC tracks. Beacon reception does not make a squelched channel look active:
**Receiving** continues to mean recently received audio RTP.

`/api/channels` exposes `frequency_hz`, a formatted `frequency`, `status_packets`
and `status_age` per channel, with aggregate `status_packets` and
`invalid_status_packets` for diagnostics. A missing `frequency_hz` is JSON null.
Live metadata supersedes a manually configured frequency; the configuration
provides a fallback before the first beacon. This version does not persist
received frequency across gateway restarts or infer it from the SSRC.

For nonstandard installations, set top-level `"status_port": 5006` to a different
port; individual manual groups may override `status_port`. Audio and status ports
must differ. Multiple audio endpoints sharing a status endpoint share its socket;
the listener is closed after the last associated audio endpoint leaves.

Upgrade by extracting the source update, then running:

```bash
sudo make install-app
sudo systemctl restart web-monitor
```

No dependency changes. The corrected Makefile uses an existing private venv or
falls back to system Python; an explicit `APP_PYTHON` override is still supported.
Allow UDP **5006 on the receiver LAN**, in addition to the audio port and mDNS.
This does not require exposing status UDP to Internet clients. Frequency appears
when the next channel status beacon arrives; a silent receiver that sends no
beacon will remain unidentified until it does.

Protocol reference: ka9q-radio `src/status.h`, `src/status.c`, `src/radio_status.c`
and `monitor`'s status listener in `src/monitor.c`. Tests exercise compressed
numbers, extended/unknown TLVs, truncated/invalid values, metadata association,
retuning, actual multicast beacons, shared-socket cleanup, JSON frequency
reporting, and unchanged WebRTC audio forwarding. Actual receivers remain the
next live test.

## Idle channel aging (0.4.0)

Channels disappear from the catalogue after **300 seconds without either an
accepted audio RTP packet or a valid channel status beacon**. Configure the
number of seconds with top-level `"channel_timeout": 300`; `0` disables aging.
A channel that still sends status beacons stays listed even while squelched.
Explicit configured channels also age out if they never receive packets; their
initial timeout starts when they are created. Aging is checked every 15 seconds
and on catalogue requests; the browser refreshes the list every five seconds.

Expiration removes channel rows and reclaims catalogue capacity; it does not
leave the multicast group, because the gateway must still receive returning
streams. It also does not erase browser selections, volumes, or pan settings.
When the same source/SSRC returns in the same group, its identity is stable and
saved selections are reapplied on catalogue refresh. Existing WebRTC tracks can
reuse a returning channel without being stranded on an expired channel object;
otherwise normal connection setup applies. Channels with no remaining session
references are released instead of being held in a permanent retired cache.

Install the source update using `sudo make install-app`, restart the service,
and reload the page for the returning-channel subscription update. No new
Python dependencies or unit changes are required.
