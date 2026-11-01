"""RTP parsing and multicast channel catalogue, independent of WebRTC."""
import asyncio
import dataclasses
import hashlib
import ipaddress
import math
import socket
import struct
import time
import weakref


@dataclasses.dataclass(frozen=True)
class AudioPacket:
    payload: bytes
    timestamp: int
    sequence: int
    ssrc: int
    payload_type: int


@dataclasses.dataclass(frozen=True)
class RadioStatus:
    ssrc: int
    frequency_hz: float | None
    destination: tuple[str, int] | None


def parse_status(data):
    """Decode radiod STATUS + TLVs; see ka9q-radio src/status.{h,c}.

    OUTPUT_SSRC=18, RADIO_FREQUENCY=33, OUTPUT_DATA_DEST_SOCKET=17.
    Integers and IEEE double bit patterns are big-endian, with leading zero
    bytes omitted. TLV lengths use the long form when their high bit is set.
    Parse the whole message before returning so truncated data cannot update
    just part of a channel's metadata. Commands are never accepted here.
    """
    if not data or data[0] != 0:
        raise ValueError('not a radiod status packet')
    pos = 1
    ssrc = None
    frequency = None
    destination = None
    while pos < len(data):
        field = data[pos]
        pos += 1
        if field == 0:
            return RadioStatus(ssrc or 0, frequency, destination)
        if pos == len(data):
            raise ValueError('missing TLV length')
        size = data[pos]
        pos += 1
        if size & 128:
            count = size & 127
            if not 1 <= count <= 4 or pos + count > len(data):
                raise ValueError('invalid extended TLV length')
            size = int.from_bytes(data[pos:pos + count], 'big')
            pos += count
        if pos + size > len(data):
            raise ValueError('truncated TLV value')
        value = data[pos:pos + size]
        pos += size
        if field == 18:
            if size > 4:
                raise ValueError('invalid SSRC length')
            ssrc = int.from_bytes(value, 'big')
        elif field == 33:
            if size > 8:
                raise ValueError('invalid frequency length')
            frequency = struct.unpack('!d', value.rjust(8, b'\0'))[0]
            if not math.isfinite(frequency) or frequency < 0:
                raise ValueError('invalid channel frequency')
        elif field == 17 and size == 6:
            destination = (socket.inet_ntoa(value[:4]), int.from_bytes(value[4:], 'big'))
    raise ValueError('missing status end marker')


def format_frequency(hz):
    if hz >= 1e6:
        value, suffix, places = hz / 1e6, 'MHz', 9
    elif hz >= 1e3:
        value, suffix, places = hz / 1e3, 'kHz', 6
    else:
        value, suffix, places = hz, 'Hz', 3
    return f'{value:.{places}f}'.rstrip('0').rstrip('.') + ' ' + suffix


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
        self.frequency_hz = self.frequency if isinstance(self.frequency, (int, float)) else None
        self.status_seen = 0
        self.status_packets = 0
        self.created_at = time.monotonic()
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
                    frequency_hz=self.frequency_hz, status_packets=self.status_packets,
                    status_age=round(time.monotonic() - self.status_seen, 1) if self.status_seen else None,
                    group=self.group_name, source=self.source, ssrc=self.ssrc,
                    age=round(age, 1) if age is not None else None,
                    active=age is not None and age < 2, packets=self.packets,
                    bytes=self.bytes, listeners=sum(t.enabled for t in self.listeners))


class Hub:
    def __init__(self, config):
        self.config = config
        self.channels = {}
        # Expired channels disappear from the catalogue, but existing WebRTC
        # tracks may still reference them. Reuse those objects on return so a
        # selected stream can resume without leaving senders on a dead object.
        self.retired = weakref.WeakValueDictionary()
        self.sockets = {}
        self.status_sockets = {}
        self.groups = {}
        self.status_packets = 0
        self.invalid_status = 0
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
                self.prune_idle()
            if len(self.channels) >= self.max_channels:
                return None
            metadata = self._definitions.get((group['id'], ssrc, source),
                       self._definitions.get((group['id'], ssrc, None)))
            channel = self.retired.pop(key, None)
            self.channels[key] = channel if channel is not None else Channel(group, source, ssrc, metadata)
        return self.channels[key]

    def prune_idle(self, now=None):
        timeout = self.config.get('channel_timeout', 300)
        if timeout == 0:
            return []
        now = time.monotonic() if now is None else now
        expired = []
        for key, channel in list(self.channels.items()):
            last_activity = max(channel.created_at, channel.last_seen, channel.status_seen)
            if now - last_activity >= timeout:
                expired.append(channel.id)
                self.retired[key] = self.channels.pop(key)
        return expired

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
        self.prune_idle()
        return {channel.id: channel for channel in self.channels.values()}

    def ingest_status(self, group, data, source):
        # Commands can be present on the status port: ignore, never execute them.
        if not data or data[0] != 0:
            return
        try:
            status = parse_status(data)
        except ValueError:
            self.invalid_status += 1
            return
        if not status.ssrc:
            return  # Front-end-only beacon, not a channel.
        if status.destination and status.destination != (group.get('address'), group.get('port')):
            return
        self.status_packets += 1
        channel = self._channel(group, source, status.ssrc)
        if channel:
            channel.status_seen = time.monotonic()
            channel.status_packets += 1
            if status.frequency_hz is not None:
                channel.frequency_hz = status.frequency_hz
                channel.frequency = format_frequency(status.frequency_hz)

    @staticmethod
    def endpoint(group):
        return (group['address'], group['port'], group.get('interface', '0.0.0.0'))

    def status_endpoint(self, group):
        return (group['address'], group.get('status_port', self.config.get('status_port', 5006)),
                group.get('interface', '0.0.0.0'))

    def start(self):
        self.loop = asyncio.get_running_loop()
        try:
            for group in self.config.get('groups', []):
                self.add_group(group)
        except Exception:
            self.close()
            raise

    def add_group(self, group):
        validate_config({'groups': [group], 'status_port': self.config.get('status_port', 5006)})
        endpoint = self.endpoint(group)
        if endpoint in self.sockets:
            return
        status_endpoint = self.status_endpoint(group)
        if status_endpoint[1] == group['port']:
            raise ValueError('audio and status ports must differ')
        sock = self._socket(endpoint)
        try:
            if status_endpoint not in self.status_sockets:
                status_socket = self._socket(status_endpoint)
                try:
                    self.loop.add_reader(status_socket.fileno(), self._read_status, status_socket, status_endpoint)
                except Exception:
                    status_socket.close()
                    raise
                self.status_sockets[status_endpoint] = status_socket
            self.loop.add_reader(sock.fileno(), self._read, sock, group)
            self.sockets[endpoint] = sock
            self.groups[endpoint] = group
        except Exception:
            sock.close()
            self._release_status(status_endpoint)
            raise

    def _socket(self, endpoint):
        address, port, interface = endpoint
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sock.bind((address, port))
            membership = socket.inet_aton(address) + socket.inet_aton(interface)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            if hasattr(socket, 'IP_MULTICAST_ALL'):
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_ALL, 0)
            sock.setblocking(False)
            return sock
        except Exception:
            sock.close()
            raise

    def _release_status(self, endpoint):
        if any(self.status_endpoint(group) == endpoint for group in self.groups.values()):
            return
        sock = self.status_sockets.pop(endpoint, None)
        if sock:
            self.loop.remove_reader(sock.fileno())
            sock.close()

    def remove_group(self, endpoint):
        sock = self.sockets.pop(endpoint, None)
        if sock:
            self.loop.remove_reader(sock.fileno())
            sock.close()  # Closing the socket drops its multicast memberships.
        group = self.groups.pop(endpoint, None)
        if group:
            self._release_status(self.status_endpoint(group))

    def _read_status(self, sock, endpoint):
        for _ in range(256):
            try:
                data, source = sock.recvfrom(65535)
            except BlockingIOError:
                break
            for group in self.groups.values():
                if self.status_endpoint(group) == endpoint:
                    self.ingest_status(group, data, source[0])

    def _read(self, sock, group):
        # Limit each callback so a busy receiver cannot starve HTTP / ICE.
        for _ in range(256):
            try:
                data, source = sock.recvfrom(65535)
            except BlockingIOError:
                break
            self.ingest(group, data, source[0])

    def close(self):
        for endpoint in list(self.sockets):
            self.remove_group(endpoint)


def validate_config(config):
    timeout = config.get('channel_timeout', 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError('channel_timeout must be a finite nonnegative number of seconds (0 disables aging)')
    status_port = config.get('status_port', 5006)
    if not isinstance(status_port, int) or not 1 <= status_port <= 65535:
        raise ValueError('invalid status UDP port')
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
        port = group.get('status_port', status_port)
        if not isinstance(port, int) or not 1 <= port <= 65535 or port == group['port']:
            raise ValueError('status port must be valid and different from audio port')
        endpoint = (group['address'], group['port'], group.get('interface', '0.0.0.0'))
        if endpoint in endpoints:
            raise ValueError('duplicate multicast endpoint')
        endpoints.add(endpoint)
        pts = group.get('payload_types')
        if not isinstance(pts, list) or not pts or any(not isinstance(p, int) or not 0 <= p <= 127 for p in pts):
            raise ValueError('set payload_types to the actual Opus RTP payload types for each group')
    return config
