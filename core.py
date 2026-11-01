"""RTP parsing and multicast channel catalogue, independent of WebRTC."""
import asyncio
import dataclasses
import hashlib
import ipaddress
import socket
import struct
import time


@dataclasses.dataclass(frozen=True)
class AudioPacket:
    payload: bytes
    timestamp: int
    sequence: int
    ssrc: int
    payload_type: int


def parse_rtp(data):
    if len(data) < 12 or data[0] >> 6 != 2:
        raise ValueError('invalid RTP header')
    # RTCP packet types occupy this range in the full second octet.
    if 192 <= data[1] <= 223:
        raise ValueError('RTCP, not RTP')
    offset = 12 + 4 * (data[0] & 15)
    if offset > len(data):
        raise ValueError('truncated CSRC list')
    if data[0] & 16:
        if offset + 4 > len(data):
            raise ValueError('truncated extension')
        words = struct.unpack_from('!H', data, offset + 2)[0]
        offset += 4 + 4 * words
    end = len(data)
    if data[0] & 32:
        padding = data[-1]
        if not padding or padding > end - offset:
            raise ValueError('invalid RTP padding')
        end -= padding
    if offset >= end:
        raise ValueError('missing RTP payload')
    seq, timestamp, ssrc = struct.unpack_from('!HII', data, 2)
    return AudioPacket(data[offset:end], timestamp, seq, ssrc, data[1] & 127)


class Channel:
    def __init__(self, group, source, ssrc, metadata=None):
        metadata = metadata or {}
        self.id = hashlib.sha256(f'{group["id"]}/{source}/{ssrc}'.encode()).hexdigest()[:24]
        self.group = group['id']
        self.group_name = group.get('name', group['id'])
        self.source = source
        self.ssrc = ssrc
        self.name = metadata.get('name', f'{self.group_name} · SSRC {ssrc}')
        self.frequency = metadata.get('frequency', '')
        self.mode = metadata.get('mode', '')
        self.last_seen = 0
        self.packets = 0
        self.bytes = 0
        self.listeners = set()
        self.last_sequence = None
        self.last_timestamp = None
        self.pts = 0

    def ingest(self, packet):
        now = time.monotonic()
        if self.last_sequence is not None:
            distance = (packet.sequence - self.last_sequence) & 0xffff
            if (distance == 0 or distance >= 0x8000) and now - self.last_seen < 2:
                return  # Duplicate or late packet: don't play old audio.
            delta = ((packet.timestamp - self.last_timestamp + 0x80000000) & 0xffffffff) - 0x80000000
            # A restarted producer may reset its clock while retaining its SSRC.
            if delta <= 0:
                delta = max(960, int((now - self.last_seen) * 48000))
            self.pts += delta
        self.last_sequence = packet.sequence
        self.last_timestamp = packet.timestamp
        self.last_seen = now
        self.packets += 1
        self.bytes += len(packet.payload)
        for listener in tuple(self.listeners):
            listener.push(packet.payload, self.pts)

    def describe(self):
        age = time.monotonic() - self.last_seen if self.last_seen else None
        return dict(id=self.id, name=self.name, frequency=self.frequency, mode=self.mode,
                    group=self.group_name, source=self.source, ssrc=self.ssrc,
                    age=round(age, 1) if age is not None else None,
                    active=age is not None and age < 2, packets=self.packets,
                    bytes=self.bytes, listeners=sum(t.enabled for t in self.listeners))


class Hub:
    def __init__(self, config):
        self.config = config
        self.channels = {}
        self.sockets = []
        self.invalid = 0
        self.ignored = 0
        self.max_channels = config.get('max_channels', 256)
        self.loop = None
        self._definitions = {}
        for group in config.get('groups', []):
            for definition in group.get('channels', []):
                ssrc = definition['ssrc']
                self._definitions[(group['id'], int(ssrc, 0) if isinstance(ssrc, str) else ssrc,
                                   definition.get('source'))] = definition
                # An explicit source lets a silent configured stream appear before its first packet.
                if definition.get('source'):
                    self._channel(group, definition['source'], int(ssrc, 0) if isinstance(ssrc, str) else ssrc)

    def _channel(self, group, source, ssrc):
        key = (group['id'], source, ssrc)
        if key not in self.channels:
            if len(self.channels) >= self.max_channels:
                return None
            metadata = self._definitions.get((group['id'], ssrc, source),
                       self._definitions.get((group['id'], ssrc, None)))
            self.channels[key] = Channel(group, source, ssrc, metadata)
        return self.channels[key]

    def ingest(self, group, data, source):
        try:
            packet = parse_rtp(data)
        except ValueError:
            self.invalid += 1
            return
        if packet.payload_type not in group['payload_types']:
            self.ignored += 1
            return
        channel = self._channel(group, source, packet.ssrc)
        if channel:
            channel.ingest(packet)

    def by_id(self):
        return {channel.id: channel for channel in self.channels.values()}

    def start(self):
        self.loop = asyncio.get_running_loop()
        try:
            for group in self.config.get('groups', []):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                self.sockets.append(sock)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
                # Binding to the group isolates same-port groups on Linux and macOS.
                sock.bind((group['address'], group['port']))
                membership = socket.inet_aton(group['address']) + socket.inet_aton(group.get('interface', '0.0.0.0'))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                if hasattr(socket, 'IP_MULTICAST_ALL'):
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_ALL, 0)
                sock.setblocking(False)
                self.loop.add_reader(sock.fileno(), self._read, sock, group)
        except Exception:
            self.close()
            raise

    def _read(self, sock, group):
        # Limit each callback so a busy receiver cannot starve HTTP / ICE.
        for _ in range(256):
            try:
                data, source = sock.recvfrom(65535)
            except BlockingIOError:
                break
            self.ingest(group, data, source[0])

    def close(self):
        for sock in self.sockets:
            if self.loop:
                self.loop.remove_reader(sock.fileno())
            sock.close()
        self.sockets.clear()


def validate_config(config):
    ids = set()
    endpoints = set()
    for group in config.get('groups', []):
        if not isinstance(group.get('id'), str) or not group['id'] or group['id'] in ids:
            raise ValueError('each group needs a unique nonempty id')
        ids.add(group['id'])
        address = ipaddress.IPv4Address(group['address'])
        if not address.is_multicast:
            raise ValueError(f'{address} is not an IPv4 multicast address')
        ipaddress.IPv4Address(group.get('interface', '0.0.0.0'))
        if not 1 <= group['port'] <= 65535:
            raise ValueError('invalid multicast UDP port')
        endpoint = (group['address'], group['port'], group.get('interface', '0.0.0.0'))
        if endpoint in endpoints:
            raise ValueError('duplicate multicast endpoint')
        endpoints.add(endpoint)
        pts = group.get('payload_types')
        if not isinstance(pts, list) or not pts or any(not isinstance(p, int) or not 0 <= p <= 127 for p in pts):
            raise ValueError('set payload_types to the actual Opus RTP payload types for each group')
    return config
