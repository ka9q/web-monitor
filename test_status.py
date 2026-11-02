"""radiod STATUS TLV decoding and metadata/audio association."""
import asyncio
import math
import socket
import struct
import unittest

from core import Hub, format_frequency, parse_status
from test_monitor import rtp


def field(kind, value):
    length = bytes([len(value)]) if len(value) < 128 else b'\x82' + len(value).to_bytes(2, 'big')
    return bytes([kind]) + length + value


def integer(value):
    return value.to_bytes(8, 'big').lstrip(b'\0')


def beacon(ssrc=1234, frequency=146520000.0, extra=b''):
    message = b'\0' + field(18, integer(ssrc)) + extra
    if frequency is not None:
        message += field(33, struct.pack('!d', frequency).lstrip(b'\0'))
    return message + b'\0'


class StatusTests(unittest.TestCase):
    def test_wire_values_and_compressed_numbers(self):
        self.assertEqual(parse_status(beacon()).frequency_hz, 146520000)
        self.assertEqual(parse_status(beacon(ssrc=0x01020304)).ssrc, 0x01020304)
        self.assertEqual(parse_status(beacon(ssrc=1)).ssrc, 1)
        self.assertEqual(parse_status(beacon(frequency=0)).frequency_hz, 0)
        # A compressed double with leading zero bytes still decodes as a double.
        tiny = struct.unpack('!d', bytes.fromhex('0000000000000001'))[0]
        self.assertEqual(parse_status(beacon(frequency=tiny)).frequency_hz, tiny)
        self.assertEqual(parse_status(beacon(frequency=None)).frequency_hz, None)

    def test_skip_extended_unknown_fields(self):
        parsed = parse_status(beacon(extra=field(200, b'x' * 400)))
        self.assertEqual(parsed.ssrc, 1234)
        self.assertEqual(parsed.frequency_hz, 146520000)

    def test_malformed_packets_and_nonfinite_frequencies(self):
        bad = [b'', b'\1\0', b'\0', b'\0\x12', b'\0\x12\x80\0',
               b'\0\x12\x85abcde', b'\0\x12\x02x', beacon()[:-1],
               b'\0' + field(18, b'x' * 5) + b'\0',
               b'\0' + field(33, b'x' * 9) + b'\0']
        bad.extend(beacon(frequency=f) for f in [math.nan, math.inf, -1])
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_status(data)

    def test_status_before_audio_partial_updates_and_source_isolation(self):
        group = dict(id='test', address='239.1.2.3', port=5004, payload_types=[111])
        hub = Hub({'groups':[]})
        hub.ingest_status(group, beacon(), '192.0.2.1')
        channel = next(iter(hub.channels.values()))
        self.assertEqual(channel.frequency_hz, 146520000)
        self.assertEqual(channel.frequency, '146.52 MHz')
        self.assertFalse(channel.describe()['active'])
        self.assertEqual(channel.packets, 0)
        self.assertEqual(channel.describe()['status_packets'], 1)
        key = channel.id
        hub.ingest(group, rtp(), '192.0.2.1')
        self.assertEqual(len(hub.channels), 1)
        self.assertEqual(channel.id, key)
        self.assertTrue(channel.describe()['active'])
        hub.ingest_status(group, beacon(frequency=147000000), '192.0.2.1')
        hub.ingest_status(group, beacon(frequency=None), '192.0.2.1')
        self.assertEqual(channel.frequency_hz, 147000000)
        hub.ingest_status(group, beacon(frequency=440000000), '192.0.2.2')
        self.assertEqual(len(hub.channels), 2)
        self.assertEqual(channel.frequency_hz, 147000000)
        hub.ingest_status(group, beacon(frequency=150000000)[:-1], '192.0.2.1')
        self.assertEqual(channel.frequency_hz, 147000000)
        self.assertEqual(hub.invalid_status, 1)
        hub.ingest_status(group, beacon(ssrc=0), '192.0.2.1')
        hub.ingest_status(group, b'\1' + beacon()[1:], '192.0.2.1')
        self.assertEqual(len(hub.channels), 2)

    def test_destination_filter_and_frequency_format(self):
        group = dict(id='test', address='239.1.2.3', port=5004, payload_types=[111])
        hub = Hub({'groups':[]})
        destination = field(17, socket.inet_aton('239.1.2.3') + struct.pack('!H', 5005))
        hub.ingest_status(group, beacon(extra=destination), '192.0.2.1')
        self.assertFalse(hub.channels)
        destination = field(17, socket.inet_aton('239.1.2.3') + struct.pack('!H', 5004))
        hub.ingest_status(group, beacon(extra=destination), '192.0.2.1')
        self.assertEqual(len(hub.channels), 1)
        self.assertEqual(format_frequency(10000000), '10 MHz')
        self.assertEqual(format_frequency(146520012.5), '146.5200125 MHz')
        self.assertEqual(format_frequency(77500), '77.5 kHz')
        self.assertEqual(format_frequency(0), '0 Hz')


class StatusSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_multicast_beacon_and_shared_status_socket_cleanup(self):
        # Two audio ports in one group share a single status listener. The
        # destination socket TLV prevents metadata being assigned to both.
        def free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(('127.0.0.1', 0))
                return probe.getsockname()[1]
        audio, other, status = free_port(), free_port(), free_port()
        while len({audio, other, status}) < 3:
            audio, other, status = free_port(), free_port(), free_port()
        group = dict(id='radio', address='239.255.87.25', port=audio, status_port=status,
                     interface='127.0.0.1', payload_types=[111])
        second = dict(group, id='second', port=other)
        hub = Hub({'groups':[group, second]})
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton('127.0.0.1'))
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        try:
            hub.start()
            self.assertEqual(len(hub.status_sockets), 1)
            destination = field(17, socket.inet_aton(group['address']) + struct.pack('!H', audio))
            for _ in range(10):
                sender.sendto(beacon(extra=destination), (group['address'], status))
                await asyncio.sleep(.05)
                if hub.channels:
                    break
            self.assertEqual(len(hub.channels), 1)
            channel = next(iter(hub.channels.values()))
            self.assertEqual(channel.group, 'radio')
            self.assertEqual(channel.frequency_hz, 146520000)
            self.assertFalse(channel.describe()['active'])
            hub.remove_group(hub.endpoint(group))
            self.assertEqual(len(hub.status_sockets), 1)
            hub.remove_group(hub.endpoint(second))
            self.assertFalse(hub.status_sockets)
        finally:
            sender.close()
            hub.close()
        self.assertFalse(hub.status_sockets)

if __name__ == '__main__':
    unittest.main()
