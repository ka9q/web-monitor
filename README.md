# Web monitor — initial prototype

A standalone browser listener for existing ka9q-radio Opus/RTP multicast streams.
No receiver tuning controls and no dependency on ka9q-web.

- Joins configured IPv4 multicast groups once and discovers streams by group,
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

## Connect your receivers

```bash
sudo install -d -m 0755 /etc/radio
sudo install -m 0644 config.example.json /etc/radio/web-monitor.json
```

The installed configuration is `/etc/radio/web-monitor.json`.
Edit its example values before starting:

- `address`: the numeric multicast address carrying **Opus audio**, not the
  receiver's status/control group.
- `port`: its RTP UDP port.
- `interface`: the **local IPv4 address** of the receiving network interface,
  not its device name. Use `0.0.0.0` for OS-selected membership routing.
- `payload_types`: the actual RTP payload type number(s) carrying Opus in that
  group. **111 is an example, not a codec detector.** Other payload types are
  ignored so PCM/status packets are not accidentally forwarded as Opus.
- `id`: any stable unique group identifier, used to preserve channel identity.

Add as many group objects as needed. Streams are discovered when RTP arrives.
Example minimal group:

```json
{
  "id": "local-opus",
  "name": "Local receivers",
  "address": "239.1.2.3",
  "port": 5004,
  "interface": "192.168.1.10",
  "payload_types": [111]
}
```

Optional `channels` entries assign labels by SSRC. SSRC can be a JSON integer
or a string such as `"0x1234abcd"`. Add `"source": "192.168.1.20"` to distinguish
senders reusing an SSRC. With both source and SSRC specified, the channel appears
even before it sends its first packet, useful for squelched channels. Without
labels, the page shows group name and SSRC. Example:

```json
"channels": [
  {
    "ssrc": 12345,
    "source": "192.168.1.20",
    "name": "Local calling channel",
    "frequency": "146.520 MHz",
    "mode": "FM"
  }
]
```

Start the gateway:

```bash
.venv/bin/python server.py --config /etc/radio/web-monitor.json
```

Open **http://localhost:8080/**. If the gateway and browser are on different
machines, use HTTPS with a certificate trusted by the browser:

```bash
.venv/bin/python server.py --config /etc/radio/web-monitor.json --listen 0.0.0.0 \
  --cert fullchain.pem --key privkey.pem
```

Alternatively put the HTTP endpoint behind an existing HTTPS reverse proxy,
keeping the original Host header. The gateway defaults to `127.0.0.1:8080`.
This is a trusted-network prototype without login or a production abuse-control
layer; add your existing access controls before exposing the HTTP API publicly.

HTTP(S) carries only the page, catalogue, negotiation, and subscription changes.
Audio uses the independently negotiated WebRTC UDP path. An HTTPS proxy does
not relay the media. An SSH HTTP port-forward likewise does not tunnel WebRTC.

## Network / NAT

The default `ice_servers: []` uses direct host candidates, appropriate for the
initial same-machine / LAN test. No external STUN service is contacted by default.
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
does not yet resolve ka9q multicast service names or parse radiod status traffic.
If the list works but audio does not, inspect browser developer tools and gateway
logs for ICE/DTLS errors, check HTTPS/browser audio permission, and check UDP rules.
Use Firefox `about:webrtc` or Chromium `chrome://webrtc-internals` for transport stats.

## Tests

```bash
.venv/bin/python -m unittest -v
```

The tests cover RTP extensions/padding, payload-type filtering, SSRC separation,
sequence/timestamp wrap, duplicate suppression, queue bounds, unchanged encoded
Opus payloads, real loopback multicast reception, and a local HTTP-signaled
ICE/DTLS/SRTP connection with decoded audio and subscription gating.
They require local network socket permissions.

## Initial-version limits

- IPv4 multicast input only. One Opus packet per RTP payload, with a standard
  48 kHz Opus RTP timestamp clock. PCM, IQ, and other codecs are not supported.
- No receiver-status metadata integration yet; labels are manual. Discovered
  channels remain listed while quiet. Restart the gateway to clear the catalogue.
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
