"""Idle-channel pruning, capacity reclamation and selected-track revival."""
import gc
import math
import unittest

from aiohttp.test_utils import TestClient, TestServer
from core import Hub, validate_config
from server import HUB, OpusTrack, create_app
from test_monitor import rtp
from test_status import beacon

GROUP = dict(id='radio', address='239.1.2.3', port=5004, payload_types=[111])
SOURCE = '192.0.2.1'


class AgingTests(unittest.TestCase):
    def test_audio_and_status_renew_lifetime_independently(self):
        hub = Hub({'channel_timeout':10})
        channel = hub._channel(GROUP, SOURCE, 1234)
        base = channel.created_at
        self.assertEqual(hub.prune_idle(base + 9), [])
        channel.last_seen = base + 8
        self.assertEqual(hub.prune_idle(base + 17), [])
        channel.status_seen = base + 16
        self.assertEqual(hub.prune_idle(base + 25), [])
        self.assertEqual(hub.prune_idle(base + 26), [channel.id])
        self.assertFalse(hub.channels)

    def test_disabled_and_invalid_timeout(self):
        hub = Hub({'channel_timeout':0})
        channel = hub._channel(GROUP, SOURCE, 1234)
        self.assertFalse(hub.prune_idle(channel.created_at + 1e9))
        self.assertEqual(len(hub.channels), 1)
        for value in [-1, math.nan, math.inf, '300', True]:
            with self.assertRaises(ValueError):
                validate_config({'channel_timeout':value})

    def test_idle_entries_reclaimed_at_capacity(self):
        hub = Hub({'channel_timeout':10, 'max_channels':1})
        channel = hub._channel(GROUP, SOURCE, 1234)
        channel.created_at -= 20
        next_channel = hub._channel(GROUP, SOURCE, 5678)
        self.assertIsNotNone(next_channel)
        self.assertEqual(len(hub.channels), 1)
        self.assertNotEqual(channel.id, next_channel.id)

    def test_unreferenced_expired_channels_are_not_retained(self):
        hub = Hub({'channel_timeout':10})
        channel = hub._channel(GROUP, SOURCE, 1234)
        key = (GROUP['id'], SOURCE, 1234)
        hub.prune_idle(channel.created_at + 11)
        self.assertIn(key, hub.retired)
        del channel
        gc.collect()
        self.assertNotIn(key, hub.retired)


class AgingAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_track_resumes_on_same_channel_object(self):
        hub = Hub({'channel_timeout':10})
        hub.ingest(GROUP, rtp(), SOURCE)
        channel = next(iter(hub.channels.values()))
        track = OpusTrack(channel)
        track.select(True)
        try:
            hub.prune_idle(channel.created_at + 11)
            self.assertFalse(hub.channels)
            # Simulate the browser excluding the absent row from its selection.
            track.select(False)
            # Even a partial status beacon revives a live referenced channel.
            hub.ingest_status(GROUP, beacon(frequency=None), SOURCE)
            self.assertIs(next(iter(hub.channels.values())), channel)
            track.select(True)
            hub.ingest(GROUP, rtp(seq=2, timestamp=1920), SOURCE)
            packet = await track.recv()
            self.assertEqual(bytes(packet), b'\xf8\xff\xfe')
        finally:
            track.stop()

    async def test_http_catalogue_prunes_without_losing_saved_channel_identity(self):
        app = create_app({'groups':[], 'discovery':False, 'source_names':False, 'channel_timeout':10})
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            hub = app[HUB]
            hub.ingest_status(GROUP, beacon(), SOURCE)
            channel = next(iter(hub.channels.values()))
            identity = channel.id
            channel.created_at -= 11
            channel.status_seen -= 11
            response = await client.get('/api/channels')
            self.assertEqual((await response.json())['channels'], [])
            hub.ingest_status(GROUP, beacon(), SOURCE)
            response = await client.get('/api/channels')
            channels = (await response.json())['channels']
            self.assertEqual(len(channels), 1)
            self.assertEqual(channels[0]['id'], identity)
        finally:
            await client.close()

if __name__ == '__main__':
    unittest.main()
