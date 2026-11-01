"""Run with python -m unittest -v. Includes real local ICE/DTLS/SRTP."""
import asyncio
import socket
import struct
import unittest

from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.codecs.opus import OpusEncoder
from core import Channel, Hub, parse_rtp, validate_config
from server import HUB, SESSIONS, OpusTrack, create_app


def rtp(seq=1, timestamp=960, ssrc=1234, payload=b'\xf8\xff\xfe', pt=111):
    return struct.pack('!BBHII', 0x80, pt, seq, timestamp, ssrc) + payload


class PacketTests(unittest.TestCase):
    def test_extensions_csrc_padding_and_malformed(self):
        raw = struct.pack('!BBHII', 0xb1, 111, 12, 960, 1234)
        raw += struct.pack('!IHHI', 77, 0xbede, 1, 0) + b'encoded' + b'\x00\x02'
        packet = parse_rtp(raw)
        self.assertEqual(packet.payload, b'encoded')
        self.assertEqual(packet.ssrc, 1234)
        for bad in [b'', raw[:11], raw[:15], raw[:20], rtp(payload=b''), bytes([0x80, 200]) + bytes(20)]:
            with self.assertRaises(ValueError):
                parse_rtp(bad)

    def test_wrap_duplicate_and_source_separation(self):
        group = dict(id='test', name='Receivers', payload_types=[111])
        hub = Hub({'groups': [group]})
        hub.ingest(group, rtp(seq=65535, timestamp=0xfffffc40), '192.0.2.1')
        hub.ingest(group, rtp(seq=0, timestamp=0), '192.0.2.1')
        hub.ingest(group, rtp(seq=0, timestamp=0), '192.0.2.1')
        hub.ingest(group, rtp(seq=65535, timestamp=0xfffffc40), '192.0.2.1')
        first = next(iter(hub.channels.values()))
        self.assertEqual(first.pts, 960)
        self.assertEqual(first.packets, 2)
        hub.ingest(group, rtp(pt=112), '192.0.2.1')
        self.assertEqual(hub.ignored, 1)
        hub.ingest(group, rtp(), '192.0.2.2')
        self.assertEqual(len(hub.channels), 2)

    def test_validation(self):
        with self.assertRaises(ValueError):
            validate_config({'groups': [dict(id='bad', address='192.0.2.1', port=5004, payload_types=[111])]})


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_encoded_passthrough_and_bounded_queue(self):
        channel = Channel({'id':'test'}, '192.0.2.1', 123)
        track = OpusTrack(channel)
        track.select(True)
        for n in range(12):
            track.push(b'unchanged Opus payload', 960 * n)
        self.assertEqual(track.queue.qsize(), 5)
        packet = await track.recv()
        payloads, timestamp = OpusEncoder().pack(packet)
        self.assertEqual(payloads, [b'unchanged Opus payload'])
        self.assertEqual(timestamp, 7 * 960)
        track.select(False)
        self.assertTrue(track.queue.empty())
        track.stop()
        self.assertFalse(channel.listeners)

    async def test_multicast_socket_ingest(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        group = dict(id='local', address='239.255.87.23', port=port,
                     interface='127.0.0.1', payload_types=[111])
        hub = Hub({'groups':[group]})
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton('127.0.0.1'))
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        try:
            hub.start()
            for _ in range(10):
                sender.sendto(rtp(), (group['address'], port))
                await asyncio.sleep(.05)
                if hub.channels:
                    break
            self.assertEqual(len(hub.channels), 1)
            self.assertEqual(next(iter(hub.channels.values())).ssrc, 1234)
        finally:
            sender.close()
            hub.close()

    async def test_real_webrtc_audio_and_subscription_gate(self):
        app = create_app({'groups': [], 'ice_servers': []}, demo=True)
        client = TestClient(TestServer(app))
        await client.start_server()
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        receivers = []
        @peer.on('track')
        def track_received(track):
            receivers.append(track)
        try:
            await asyncio.sleep(.1)
            response = await client.get('/api/channels')
            catalogue = await response.json()
            self.assertEqual(len(catalogue['channels']), 2)
            ids = [c['id'] for c in catalogue['channels']]
            for _ in ids:
                peer.addTransceiver('audio', direction='recvonly')
            await peer.setLocalDescription(await peer.createOffer())
            response = await client.post('/api/sessions', json={'channels':ids, 'selected':[ids[0]],
                'type':'offer', 'sdp':peer.localDescription.sdp})
            self.assertEqual(response.status, 200, await response.text())
            answer = await response.json()
            await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type='answer'))
            frame = await asyncio.wait_for(receivers[0].recv(), 10)
            self.assertEqual(frame.sample_rate, 48000)
            self.assertTrue(any(bytes(frame.planes[0])))
            session = app[SESSIONS][answer['id']]
            self.assertEqual(sum(t.enabled for t in session.tracks.values()), 1)
            response = await client.put('/api/sessions/' + answer['id'], json={'selected':[]})
            self.assertEqual(response.status, 200)
            await asyncio.sleep(.1)
            async def total_sent():
                stats = await session.peer.getStats()
                return sum(s.packetsSent for s in stats.values() if s.type == 'outbound-rtp')
            before = await total_sent()
            await asyncio.sleep(.2)
            self.assertEqual(await total_sent(), before, 'unchecked tracks must not send RTP')
            response = await client.put('/api/sessions/' + answer['id'], json={'selected':[ids[0]]})
            self.assertEqual(response.status, 200)
            await asyncio.sleep(.2)
            self.assertGreater(await total_sent(), before)
            response = await client.put('/api/sessions/' + answer['id'], json={'selected':['unknown']})
            self.assertEqual(response.status, 400)
            response = await client.delete('/api/sessions/' + answer['id'])
            self.assertEqual(response.status, 204)
            self.assertFalse(app[SESSIONS])
            self.assertTrue(all(not c.listeners for c in app[HUB].channels.values()))
        finally:
            await peer.close()
            await client.close()

if __name__ == '__main__':
    unittest.main()
