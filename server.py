#!/usr/bin/env python3
"""Self-hosted multicast Opus → WebRTC listening gateway."""
import argparse
import asyncio
import contextlib
import fractions
import json
import logging
import pathlib
import secrets
import ssl
import time

from aiohttp import web
from aiortc import (MediaStreamTrack, RTCConfiguration, RTCIceServer,
                    RTCPeerConnection, RTCRtpSender, RTCSessionDescription)
from av import Packet
from core import Hub, validate_config
from discovery import OpusDiscovery, discovery_options

ROOT = pathlib.Path(__file__).resolve().parent
LOG = logging.getLogger('web-monitor')
TIME_BASE = fractions.Fraction(1, 48000)
HUB = web.AppKey('hub', Hub)
SESSIONS = web.AppKey('sessions', dict)


class OpusTrack(MediaStreamTrack):
    kind = 'audio'

    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        self.queue = asyncio.Queue(maxsize=5)
        self.enabled = False
        self.dropped = 0
        channel.listeners.add(self)

    def select(self, enabled):
        self.enabled = enabled
        if not enabled:
            while not self.queue.empty():
                self.queue.get_nowait()

    def push(self, payload, pts):
        if not self.enabled or self.readyState != 'live':
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait((payload, pts))

    async def recv(self):
        # Encoded AV packets use aiortc's Opus pack() path, not its encoder.
        while True:
            payload, pts = await self.queue.get()
            if self.enabled:
                packet = Packet(payload)
                packet.pts = pts
                packet.time_base = TIME_BASE
                return packet

    def stop(self):
        self.select(False)
        self.channel.listeners.discard(self)
        super().stop()


class Session:
    def __init__(self, peer, channels):
        self.peer = peer
        self.tracks = {channel.id: OpusTrack(channel) for channel in channels}
        self.touched = time.monotonic()

    def select(self, ids):
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in self.tracks for i in ids):
            raise web.HTTPBadRequest(text='selection must contain negotiated channel IDs')
        selected = set(ids)
        for key, track in self.tracks.items():
            track.select(key in selected)
        self.touched = time.monotonic()

    async def close(self):
        for track in self.tracks.values():
            track.stop()
        await self.peer.close()


@web.middleware
async def same_origin(request, handler):
    # No cookies or login in this prototype; block cross-origin browser writes.
    origin = request.headers.get('Origin')
    if request.method not in ('GET', 'HEAD') and origin:
        from urllib.parse import urlsplit
        if urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text='cross-origin request denied')
    response = await handler(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def create_app(config, demo=False):
    validate_config(config)
    discovery_config = discovery_options(config)
    app = web.Application(client_max_size=128 * 1024, middlewares=[same_origin])
    hub = Hub(config)
    sessions = {}
    app[HUB] = hub
    app[SESSIONS] = sessions

    async def json_object(request):
        try:
            body = await request.json()
        except (ValueError, UnicodeError) as error:
            raise web.HTTPBadRequest(text='invalid JSON') from error
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='expected a JSON object')
        return body

    async def index(request):
        return web.FileResponse(ROOT / 'static' / 'index.html')

    async def catalogue(request):
        hub.prune_idle()
        return web.json_response(dict(title=config.get('title', 'Radio monitor'),
            channels=sorted((c.describe() for c in hub.channels.values()), key=lambda c: (c['group'], c['name'])),
            ice_servers=config.get('ice_servers', []),
            max_channels=config.get('max_channels_per_session', 32),
            invalid_packets=hub.invalid, ignored_packets=hub.ignored,
            status_packets=hub.status_packets, invalid_status_packets=hub.invalid_status))

    async def remove(key):
        session = sessions.pop(key, None)
        if session:
            await session.close()

    async def offer(request):
        if len(sessions) >= config.get('max_sessions', 16):
            raise web.HTTPServiceUnavailable(text='listener limit reached')
        body = await json_object(request)
        ids = body.get('channels')
        if (not isinstance(ids, list) or not ids or
                len(ids) > config.get('max_channels_per_session', 32) or
                any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids))):
            raise web.HTTPBadRequest(text='invalid channel list')
        available = hub.by_id()
        if any(key not in available for key in ids):
            raise web.HTTPBadRequest(text='unknown channel')
        if body.get('type') != 'offer' or not isinstance(body.get('sdp'), str):
            raise web.HTTPBadRequest(text='an SDP offer is required')
        ice = [RTCIceServer(**item) for item in config.get('ice_servers', [])]
        peer = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        session = Session(peer, [available[key] for key in ids])
        key = secrets.token_urlsafe(24)
        sessions[key] = session

        @peer.on('connectionstatechange')
        async def state_change():
            if peer.connectionState in ('failed', 'closed'):
                await remove(key)
            elif peer.connectionState == 'disconnected':
                await asyncio.sleep(15)
                if peer.connectionState == 'disconnected':
                    await remove(key)

        try:
            session.select(body.get('selected', []))
            await peer.setRemoteDescription(RTCSessionDescription(sdp=body['sdp'], type='offer'))
            transceivers = peer.getTransceivers()
            if len(transceivers) != len(ids) or any(t.kind != 'audio' for t in transceivers):
                raise ValueError('offer must have one audio transceiver per channel')
            codecs = [c for c in RTCRtpSender.getCapabilities('audio').codecs if c.mimeType.lower() == 'audio/opus']
            for transceiver, channel_id in zip(transceivers, ids):
                transceiver.setCodecPreferences(codecs)
                transceiver.direction = 'sendonly'
                transceiver.sender.replaceTrack(session.tracks[channel_id])
            await asyncio.wait_for(peer.setLocalDescription(await peer.createAnswer()), 20)
            return web.json_response(dict(id=key, type=peer.localDescription.type,
                                          sdp=peer.localDescription.sdp))
        except web.HTTPException:
            await remove(key)
            raise
        except Exception as error:
            LOG.warning('offer failed: %s', error)
            await remove(key)
            raise web.HTTPBadRequest(text='WebRTC negotiation failed; check server log') from error

    async def selection(request):
        session = sessions.get(request.match_info['id'])
        if not session:
            raise web.HTTPNotFound(text='session expired; reconnect')
        body = await json_object(request)
        session.select(body.get('selected'))
        return web.json_response({'ok': True})

    async def delete(request):
        await remove(request.match_info['id'])
        return web.Response(status=204)

    async def reap():
        while True:
            await asyncio.sleep(15)
            hub.prune_idle()
            for key, session in list(sessions.items()):
                if time.monotonic() - session.touched > 90:
                    await remove(key)

    async def lifecycle(application):
        hub.start()
        tasks = []
        discovery = None
        try:
            if discovery_config['enabled'] and not demo:
                discovery = OpusDiscovery(hub, discovery_config)
                await discovery.start()
            tasks.append(asyncio.create_task(reap()))
            if demo:
                from demo import generate
                tasks.append(asyncio.create_task(generate(hub)))
            yield
        finally:
            if discovery:
                await discovery.close()
            hub.close()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.gather(*(remove(key) for key in list(sessions)))

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/', index)
    app.router.add_static('/static/', ROOT / 'static')
    app.router.add_get('/api/channels', catalogue)
    app.router.add_post('/api/sessions', offer)
    app.router.add_put('/api/sessions/{id}', selection)
    app.router.add_delete('/api/sessions/{id}', delete)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=pathlib.Path)
    parser.add_argument('--demo', action='store_true', help='generate two test channels; no multicast input')
    parser.add_argument('--listen', help='override HTTP bind address')
    parser.add_argument('--port', type=int, help='override HTTP port')
    parser.add_argument('--cert', help='TLS certificate PEM')
    parser.add_argument('--key', help='TLS private key PEM')
    args = parser.parse_args()
    if not args.config and not args.demo:
        parser.error('provide --config FILE or --demo')
    config = json.loads(args.config.read_text()) if args.config else {'title': 'Radio monitor · demo', 'groups': []}
    if args.demo:
        config['groups'] = []
    if bool(args.cert) != bool(args.key):
        parser.error('provide both --cert and --key')
    tls = None
    if args.cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)
    logging.basicConfig(level=logging.INFO)
    app = create_app(config, demo=args.demo)
    web.run_app(app, host=args.listen or config.get('listen', '127.0.0.1'),
                port=args.port or config.get('port', 8080), ssl_context=tls)


if __name__ == '__main__':
    main()
