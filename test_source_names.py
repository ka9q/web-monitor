"""Server-side direct mDNS reverse lookups: caching, bounds and actual packets."""
import asyncio
import contextlib
import ipaddress
import socket
import unittest
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from zeroconf import DNSIncoming, DNSOutgoing, DNSPointer, IPVersion
from zeroconf.asyncio import AsyncZeroconf
from discovery import SourceNames, source_name_options
from server import HUB, SOURCE_NAMES, create_app
from test_status import beacon


def pointer(address='192.0.2.145', name='receiver-test.local.', ttl=120):
    return DNSPointer(ipaddress.IPv4Address(address).reverse_pointer + '.', 12, 1, ttl, name)


class NameTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_positive_negative_cache_and_disable(self):
        names = SourceNames({'enabled':True})
        names.running = True
        names.resolvers = [object()]
        names._query = AsyncMock(return_value=pointer())
        self.assertIsNone(names.lookup('192.0.2.145'))
        self.assertIsNone(names.lookup('192.0.2.145'))
        await asyncio.gather(*list(names.tasks.values()))
        self.assertEqual(names.lookup('192.0.2.145'), 'receiver-test.local')
        names._query.assert_awaited_once()
        names._query = AsyncMock(return_value=None)
        names.lookup('192.0.2.146')
        await asyncio.gather(*list(names.tasks.values()))
        self.assertIsNone(names.lookup('192.0.2.146'))
        self.assertIsNone(names.lookup('192.0.2.146'))
        names._query.assert_awaited_once()
        await names.close()
        self.assertIsNone(names.lookup('192.0.2.145'))
        self.assertFalse(source_name_options({'source_names':False})['enabled'])

    async def test_pending_requests_are_bounded_and_cancelled(self):
        names = SourceNames({'enabled':True})
        names.running = True
        names.resolvers = [object()]
        gate = asyncio.Event()
        async def blocked(reverse):
            await gate.wait()
        names._query = blocked
        for last in range(1, 20):
            names.lookup('192.0.2.' + str(last))
        self.assertEqual(len(names.tasks), names.max_pending)
        await names.close()
        self.assertFalse(names.tasks)
        self.assertFalse(names.cache)

    async def test_expiry_refresh_and_untrusted_label(self):
        names = SourceNames({'enabled':True})
        names.running = True
        names.resolvers = [object()]
        names.cache['192.0.2.145'] = ('old.local', 0)
        names._query = AsyncMock(return_value=pointer(name='new.local.'))
        self.assertEqual(names.lookup('192.0.2.145'), 'old.local')
        await asyncio.gather(*list(names.tasks.values()))
        self.assertEqual(names.lookup('192.0.2.145'), 'new.local')
        self.assertIsNone(names._name(pointer(name='bad\nname.local.')))
        await names.close()

    async def test_http_keeps_numeric_address_and_adds_source_name(self):
        app = create_app({'groups':[], 'discovery':False, 'source_names':False})
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            names = app[SOURCE_NAMES]
            names.running = True
            names.resolvers = [object()]
            names._query = AsyncMock(return_value=pointer())
            group = dict(id='test', address='239.1.2.3', port=5004, payload_types=[111])
            app[HUB].ingest_status(group, beacon(), '192.0.2.145')
            response = await client.get('/api/channels')
            self.assertEqual((await response.json())['channels'][0]['source'], '192.0.2.145')
            await asyncio.gather(*list(names.tasks.values()))
            response = await client.get('/api/channels')
            channel = (await response.json())['channels'][0]
            self.assertEqual(channel['source_name'], 'receiver-test.local')
            self.assertEqual(channel['source'], '192.0.2.145')
            self.assertEqual(channel['ssrc'], 1234)
        finally:
            await client.close()

    async def test_actual_multicast_ptr_query_and_response(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('224.0.0.251', 5353))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        socket.inet_aton('224.0.0.251') + socket.inet_aton('127.0.0.1'))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton('127.0.0.1'))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setblocking(False)
        names = SourceNames({'enabled':True, 'interfaces':['127.0.0.1']})
        observed = []
        publisher = AsyncZeroconf(interfaces=['127.0.0.1'], ip_version=IPVersion.V4Only)
        async def responder():
            while True:
                data, _ = await asyncio.get_running_loop().sock_recvfrom(sock, 65535)
                message = DNSIncoming(data)
                for question in message.questions:
                    if question.name.lower() == pointer().name.lower() and question.type == 12:
                        observed.append(question.name)
                        answer = DNSOutgoing(0x8400)
                        answer.add_answer_at_time(pointer(), 0)
                        publisher.zeroconf.async_send(answer)
        task = asyncio.create_task(responder())
        try:
            await names.start()
            self.assertIsNone(names.lookup('192.0.2.145'))
            await asyncio.gather(*list(names.tasks.values()))
            self.assertEqual(names.lookup('192.0.2.145'), 'receiver-test.local')
            self.assertTrue(observed, 'no actual mDNS PTR question was received')
        finally:
            await names.close()
            task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                await publisher.async_close()
                sock.close()

if __name__ == '__main__':
    unittest.main()
