"""Discovery validation, memberships, and actual local mDNS announcement."""
import asyncio
import socket
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf
from core import Hub
from discovery import OpusDiscovery, SERVICE, discovery_options, groups_from_info, local_interfaces
from test_monitor import rtp


class DiscoveryTests(unittest.TestCase):
    def test_linux_skips_down_and_nonmulticast_links(self):
        adapters = [SimpleNamespace(name=name, ips=[SimpleNamespace(ip=address)])
                    for name, address in [('eth0','192.0.2.1'), ('wg0','44.1.2.3'),
                                          ('eth1','192.0.2.2'), ('lo','127.0.0.1')]]
        flags = {'eth0':'0x1003', 'wg0':'0x00c1', 'eth1':'0x1002', 'lo':'0x0049'}
        with patch('discovery.sys.platform', 'linux'), patch('discovery.ifaddr.get_adapters', return_value=adapters), \
             patch('pathlib.Path.read_text', autospec=True, side_effect=lambda path: flags[path.parent.name]):
            self.assertEqual(local_interfaces(), ['192.0.2.1'])

    def test_defaults_and_invalid_config(self):
        self.assertTrue(discovery_options({})['enabled'])
        self.assertFalse(discovery_options({'groups':[{}]})['enabled'])
        self.assertFalse(discovery_options({'discovery':False})['enabled'])
        self.assertTrue(discovery_options({'discovery':{'interfaces':['192.0.2.1']}})['enabled'])
        for value in [{'interfaces':['eth0']}, {'max_groups':0}, {'payload_types':[]}, 'yes']:
            with self.assertRaises(ValueError):
                discovery_options({'discovery':value})

    def test_multicast_targets_only(self):
        info = SimpleNamespace(name='Local receivers.' + SERVICE, port=5004,
            parsed_addresses=lambda version: ['192.168.1.2', '239.1.2.3', '239.1.2.3'])
        groups = groups_from_info(info, '192.168.1.10', [111])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['address'], '239.1.2.3')
        self.assertEqual(groups[0]['name'], 'Local receivers')
        self.assertEqual(groups[0]['interface'], '192.168.1.10')
        info.port = 0
        self.assertFalse(groups_from_info(info, '192.168.1.10', [111]))

    def test_reference_counts_limit_and_manual_override(self):
        class FakeHub:
            endpoint = staticmethod(Hub.endpoint)
            config = {'groups':[dict(address='239.1.2.9', port=5004, interface='0.0.0.0')]}
            def __init__(self): self.joined = set()
            def add_group(self, group): self.joined.add(self.endpoint(group))
            def remove_group(self, endpoint): self.joined.remove(endpoint)
        hub = FakeHub()
        manager = OpusDiscovery(hub, {'max_groups':1})
        group = dict(address='239.1.2.3', port=5004, interface='192.0.2.1', name='First')
        manager.replace('a', [group])
        manager.replace('b', [group])
        self.assertEqual(len(hub.joined), 1)
        manager.replace('a', [])
        self.assertEqual(len(hub.joined), 1)
        manager.replace('c', [dict(group, address='239.1.2.4')])
        self.assertEqual(len(hub.joined), 1)
        manager.replace('b', [])
        self.assertFalse(hub.joined)
        manager.replace('manual', [dict(group, address='239.1.2.9')])
        self.assertFalse(hub.joined)


class MdnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_mdns_to_multicast_audio_and_withdrawal(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        address = '239.255.87.24'
        hub = Hub({'groups':[]})
        hub.start()
        manager = OpusDiscovery(hub, discovery_options({'discovery':{'interfaces':['127.0.0.1']}}))
        publisher = AsyncZeroconf(interfaces=['127.0.0.1'], ip_version=IPVersion.V4Only)
        name = 'web-monitor-test-' + uuid.uuid4().hex[:8]
        info = ServiceInfo(SERVICE, name + '.' + SERVICE, addresses=[socket.inet_aton(address)],
                           port=port, properties={}, server=name + '.local.')
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton('127.0.0.1'))
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        try:
            await manager.start()
            await publisher.async_register_service(info)
            for _ in range(100):
                if manager.dynamic:
                    break
                await asyncio.sleep(.1)
            self.assertEqual(len(manager.dynamic), 1, 'mDNS advertisement was not resolved/joined')
            for _ in range(20):
                sender.sendto(rtp(), (address, port))
                await asyncio.sleep(.05)
                if hub.channels:
                    break
            self.assertEqual(len(hub.channels), 1)
            channel = next(iter(hub.channels.values()))
            self.assertEqual(channel.group_name, name)
            self.assertEqual(channel.ssrc, 1234)
            await publisher.async_unregister_service(info)
            for _ in range(50):
                if not manager.dynamic:
                    break
                await asyncio.sleep(.1)
            self.assertFalse(manager.dynamic, 'withdrawn service retained a membership')
            self.assertFalse(hub.sockets)
        finally:
            sender.close()
            await publisher.async_close()
            await manager.close()
            hub.close()

if __name__ == '__main__':
    unittest.main()
