"""Listener log content, suppression of heartbeat noise, and input validation."""
import json
import unittest
from aiohttp import web
from core import Channel
from server import Session, listener_name


class LogTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_changes_only_and_escaped_labels(self):
        channel = Channel({'id': 'test', 'name': 'Test\nreceiver'}, '192.0.2.1', 123,
                          {'frequency': 146520000})
        class Peer:
            async def close(self): pass
        session = Session(Peer(), [channel], '198.51.100.2', 'KA9Q')
        try:
            with self.assertLogs('web-monitor', level='INFO') as captured:
                session.select([channel.id])
                session.select([channel.id])  # Browser heartbeat: no repeated log.
                session.select([channel.id], 'Other listener')
                session.select([])
            records = [json.loads(record.getMessage().removeprefix('listener '))
                       for record in captured.records]
            self.assertEqual([r['event'] for r in records], ['selection', 'identity', 'selection'])
            first = records[0]
            self.assertEqual(first['listener'], 'KA9Q')
            self.assertEqual(first['remote'], '198.51.100.2')
            self.assertEqual(first['channels'][0]['frequency_hz'], 146520000)
            self.assertEqual(first['channels'][0]['ssrc'], 123)
            self.assertEqual(records[-1]['channels'], [])
            self.assertNotIn('\n', captured.records[0].getMessage())
            with self.assertRaises(web.HTTPBadRequest):
                session.select(['unknown'], 'Invalid change')
            self.assertEqual(session.listener, 'Other listener')
        finally:
            await session.close()

    def test_optional_name_validation(self):
        self.assertEqual(listener_name('  KA9Q  '), 'KA9Q')
        self.assertEqual(listener_name(''), '')
        for value in [None, 12, 'x' * 81, 'fake\nevent', '\x00']:
            with self.assertRaises(web.HTTPBadRequest):
                listener_name(value)
