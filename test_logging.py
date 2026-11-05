"""Listener log content, suppression of heartbeat noise, and input validation."""
import json
import logging
import pathlib
import tempfile
import unittest
from aiohttp import web
from core import Channel
from server import Session, listener_name, configure_listener_logging, LISTENER_LOG


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


class ListenerFileTests(unittest.TestCase):
    def test_file_routing_and_rotation(self):
        old_handlers = LISTENER_LOG.handlers[:]
        old_level, old_propagate = LISTENER_LOG.level, LISTENER_LOG.propagate
        LISTENER_LOG.handlers = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                filename = pathlib.Path(directory) / 'listeners.log'
                configure_listener_logging(filename)
                handler = LISTENER_LOG.handlers[0]
                self.assertEqual(handler.maxBytes, 10 * 1024 * 1024)
                self.assertEqual(handler.backupCount, 5)
                with self.assertLogs('web-monitor', level='INFO') as journal:
                    LISTENER_LOG.info('listener %s', '{"event":"connect"}')
                    logging.getLogger('web-monitor').warning('operational warning')
                self.assertEqual(len(journal.records), 1)
                self.assertEqual(journal.records[0].getMessage(), 'operational warning')
                self.assertIn('"event":"connect"', filename.read_text())
                self.assertNotIn('operational warning', filename.read_text())
                handler.maxBytes = 1
                LISTENER_LOG.info('listener %s', '{"event":"disconnect"}')
                self.assertIn('"event":"connect"', pathlib.Path(str(filename) + '.1').read_text())
                self.assertIn('"event":"disconnect"', filename.read_text())
        finally:
            for handler in LISTENER_LOG.handlers:
                handler.close()
            LISTENER_LOG.handlers = old_handlers
            LISTENER_LOG.setLevel(old_level)
            LISTENER_LOG.propagate = old_propagate
