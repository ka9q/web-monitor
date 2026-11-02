"""Discover multicast Opus services directly over mDNS, without unicast DNS."""
import asyncio
from collections import OrderedDict
import ipaddress
import logging
import pathlib
import sys
import time

import ifaddr
from zeroconf import (DNSOutgoing, DNSPointer, DNSQuestion, IPVersion,
                      RecordUpdateListener, ServiceStateChange, current_time_millis)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

LOG = logging.getLogger('web-monitor.discovery')
SERVICE = '_opus._udp.local.'


def discovery_options(config):
    value = config.get('discovery', not bool(config.get('groups')))
    if isinstance(value, bool):
        return {'enabled': value}
    if not isinstance(value, dict):
        raise ValueError('discovery must be a boolean or object')
    options = dict(value)
    options.setdefault('enabled', True)
    interfaces = options.get('interfaces')
    if interfaces is not None:
        if not isinstance(interfaces, list) or not interfaces:
            raise ValueError('discovery.interfaces must be a nonempty list of local IPv4 addresses')
        for address in interfaces:
            if ipaddress.IPv4Address(address).is_unspecified or ipaddress.IPv4Address(address).is_multicast:
                raise ValueError('discovery interfaces must be concrete local IPv4 addresses')
    for name, default in [('max_groups', 64), ('max_services', 128)]:
        options.setdefault(name, default)
        if not isinstance(options[name], int) or not 1 <= options[name] <= 1024:
            raise ValueError(f'discovery.{name} must be between 1 and 1024')
    pts = options.setdefault('payload_types', [111])
    if not isinstance(pts, list) or not pts or any(not isinstance(p, int) or not 0 <= p <= 127 for p in pts):
        raise ValueError('invalid discovery.payload_types')
    return options


def local_interfaces():
    addresses = set()
    for adapter in ifaddr.get_adapters():
        if sys.platform.startswith('linux'):
            try:
                flags = int((pathlib.Path('/sys/class/net') / adapter.name / 'flags').read_text(), 16)
            except (OSError, ValueError):
                continue
            # IFF_UP and IFF_MULTICAST: avoid probing WireGuard and other
            # non-multicast links, particularly when their WAN is metered.
            if flags & 0x1001 != 0x1001:
                continue
        for entry in adapter.ips:
            if isinstance(entry.ip, str):
                address = ipaddress.IPv4Address(entry.ip)
                if not address.is_loopback and not address.is_unspecified:
                    addresses.add(str(address))
    return sorted(addresses)


def groups_from_info(info, interface, payload_types):
    # SRV gives the UDP port; its target's A record gives the multicast group.
    if not isinstance(info.port, int) or not 1 <= info.port <= 65535:
        return []
    name = info.name[:-len(SERVICE)].rstrip('.') if info.name.endswith(SERVICE) else info.name
    groups = []
    for value in sorted(set(info.parsed_addresses(IPVersion.V4Only))):
        address = ipaddress.IPv4Address(value)
        if address.is_multicast:
            groups.append(dict(id=f'mdns:{address}:{info.port}', name=name,
                               address=str(address), port=info.port,
                               interface=interface, payload_types=payload_types))
    return groups


class OpusDiscovery:
    def __init__(self, hub, options):
        self.hub = hub
        self.options = options
        self.browsers = []
        self.resolvers = []
        self.tasks = {}
        self.present = set()
        self.memberships = {}  # service key -> successfully joined endpoints
        self.dynamic = set()
        self.manual = {hub.endpoint(group) for group in hub.config.get('groups', [])}
        self.limiter = asyncio.Semaphore(8)
        self.closing = False

    async def start(self):
        interfaces = self.options.get('interfaces') or local_interfaces()
        try:
            # Separate browsers preserve the interface on which a service was seen.
            for interface in interfaces:
                resolver = AsyncZeroconf(interfaces=[interface], ip_version=IPVersion.V4Only)
                self.resolvers.append(resolver)

                def changed(zeroconf, service_type, name, state_change, interface=interface, resolver=resolver):
                    self.changed(interface, resolver, service_type, name, state_change)

                browser = AsyncServiceBrowser(resolver.zeroconf, SERVICE, handlers=[changed])
                self.browsers.append(browser)
                LOG.info('Browsing %s on %s', SERVICE, interface)
            if not interfaces:
                LOG.warning('No IPv4 interfaces available for Opus service discovery')
        except Exception:
            await self.close()
            raise

    def changed(self, interface, resolver, service_type, name, state):
        if self.closing:
            return
        key = (interface, name)
        if state == ServiceStateChange.Removed:
            self.present.discard(key)
            task = self.tasks.pop(key, None)
            if task:
                task.cancel()
            self.replace(key, [])
            return
        if key not in self.present and len(self.present) >= self.options.get('max_services', 128):
            return
        self.present.add(key)
        # Coalesce repeat notifications while a resolution is in flight.
        if key in self.tasks:
            return
        task = asyncio.create_task(self.resolve(key, resolver, service_type, name))
        self.tasks[key] = task

        def done(completed):
            if self.tasks.get(key) is completed:
                self.tasks.pop(key, None)
            if not completed.cancelled() and completed.exception():
                LOG.warning('Discovery failed for %s: %s', name, completed.exception())
        task.add_done_callback(done)

    async def resolve(self, key, resolver, service_type, name):
        async with self.limiter:
            info = AsyncServiceInfo(service_type, name)
            resolved = await info.async_request(resolver.zeroconf, 3000)
            if resolved and key in self.present and not self.closing:
                self.replace(key, groups_from_info(info, key[0], self.options.get('payload_types', [111])))

    def replace(self, key, groups):
        keep = set()
        for group in groups:
            endpoint = self.hub.endpoint(group)
            # A manual wildcard membership owns this group on its chosen route.
            if endpoint in self.manual or (endpoint[0], endpoint[1], '0.0.0.0') in self.manual:
                continue
            if endpoint not in self.dynamic:
                if len(self.dynamic) >= self.options.get('max_groups', 64):
                    continue
                try:
                    self.hub.add_group(group)
                except OSError as error:
                    LOG.warning('Cannot join discovered %s: %s', endpoint, error)
                    continue
                self.dynamic.add(endpoint)
                LOG.info('Discovered %s at %s:%s on %s', group['name'], *endpoint)
            keep.add(endpoint)
        self.memberships[key] = keep
        referenced = set().union(*self.memberships.values()) if self.memberships else set()
        for endpoint in self.dynamic - referenced:
            self.hub.remove_group(endpoint)
            self.dynamic.remove(endpoint)
            LOG.info('Left expired/withdrawn Opus group %s', endpoint)
        if not keep:
            self.memberships.pop(key, None)

    async def close(self):
        self.closing = True
        for browser in self.browsers:
            await browser.async_cancel()
        self.browsers.clear()
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        self.tasks.clear()
        for resolver in self.resolvers:
            await resolver.async_close()
        self.resolvers.clear()
        for endpoint in list(self.dynamic):
            self.hub.remove_group(endpoint)
        self.dynamic.clear()
        self.memberships.clear()
        self.present.clear()


# Reverse names are presentation metadata, never a stream identity or an
# authorization decision. These are direct mDNS PTR queries, not NSS/DNS calls.

def source_name_options(config):
    value = config.get('source_names', True)
    if isinstance(value, bool):
        return {'enabled': value}
    if not isinstance(value, dict):
        raise ValueError('source_names must be a boolean or object')
    options = dict(value)
    options.setdefault('enabled', True)
    if not isinstance(options['enabled'], bool):
        raise ValueError('source_names.enabled must be true or false')
    if 'interfaces' in options:
        discovery_options({'discovery': {'interfaces': options['interfaces']}})
    return options


class _PointerListener(RecordUpdateListener):
    def __init__(self, name, future):
        self.name = name.lower()
        self.future = future

    def async_update_records(self, zc, now, records):
        for update in records:
            record = update.new
            if (isinstance(record, DNSPointer) and record.name.lower() == self.name
                    and not record.is_expired(now) and not self.future.done()):
                self.future.set_result(record)


class SourceNames:
    """Bounded, nonblocking, positive/negative cache for source mDNS PTR names."""
    def __init__(self, options):
        self.options = options
        self.cache = OrderedDict()
        self.tasks = {}
        self.resolvers = []
        self.owned = []
        self.running = False
        self.max_entries = 512
        self.max_pending = 4
        self.negative_ttl = 60
        self.max_ttl = 300
        self.timeout = 2

    async def start(self, shared=None, interfaces=None):
        if not self.options.get('enabled', True):
            return
        if shared and 'interfaces' not in self.options:
            self.resolvers = list(shared)
            for resolver in self.resolvers:
                await resolver.zeroconf.async_wait_for_start()
        else:
            addresses = self.options.get('interfaces', interfaces)
            if addresses is None:
                addresses = local_interfaces()
            for address in addresses:
                resolver = AsyncZeroconf(interfaces=[address], ip_version=IPVersion.V4Only)
                self.owned.append(resolver)
                self.resolvers.append(resolver)
                await resolver.zeroconf.async_wait_for_start()
        self.running = True

    @staticmethod
    def _name(record):
        name = record.alias.rstrip('.')
        if not name or len(name) > 253 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            return None
        return name

    def lookup(self, address):
        if not self.running or not self.resolvers:
            return None
        try:
            address = str(ipaddress.IPv4Address(address))
        except ipaddress.AddressValueError:
            return None
        entry = self.cache.get(address)
        if entry:
            self.cache.move_to_end(address)
            if entry[1] > time.monotonic():
                return entry[0]
        if address not in self.tasks and len(self.tasks) < self.max_pending:
            if address not in self.cache:
                if len(self.cache) >= self.max_entries:
                    victim = next((key for key in self.cache if key not in self.tasks), None)
                    if victim is None:
                        return None
                    self.cache.pop(victim)
                self.cache[address] = (None, 0)
            self.tasks[address] = asyncio.create_task(self._resolve(address))
        return entry[0] if entry else None

    async def _resolve(self, address):
        name, ttl = None, self.negative_ttl
        try:
            record = await self._query(ipaddress.IPv4Address(address).reverse_pointer + '.')
            if record:
                name = self._name(record)
                if name:
                    ttl = max(1, min(self.max_ttl, record.get_remaining_ttl(current_time_millis())))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.debug('Source mDNS lookup failed for %s: %s', address, error)
        finally:
            self.tasks.pop(address, None)
            if self.running:
                self.cache[address] = (name, time.monotonic() + ttl)

    async def _query(self, reverse):
        now = current_time_millis()
        # Reuse answers already heard through service discovery when available.
        for resolver in self.resolvers:
            for record in resolver.zeroconf.cache.get_all_by_details(reverse, 12, 1):
                if isinstance(record, DNSPointer) and not record.is_expired(now) and self._name(record):
                    return record
        future = asyncio.get_running_loop().create_future()
        listener = _PointerListener(reverse, future)
        question = DNSQuestion(reverse, 12, 1)  # PTR / IN; request multicast replies.
        registered = []
        try:
            for resolver in self.resolvers:
                resolver.zeroconf.async_add_listener(listener, question)
                registered.append(resolver)
            for _ in range(2):
                if future.done():
                    return future.result()
                outgoing = DNSOutgoing(0)  # Standard DNS question, sent to mDNS.
                outgoing.add_question(question)
                for resolver in self.resolvers:
                    resolver.zeroconf.async_send(outgoing)
                try:
                    return await asyncio.wait_for(asyncio.shield(future), self.timeout / 2)
                except asyncio.TimeoutError:
                    continue
            return None
        finally:
            for resolver in registered:
                resolver.zeroconf.async_remove_listener(listener)
            if not future.done():
                future.cancel()

    async def close(self):
        self.running = False
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        self.tasks.clear()
        for resolver in self.owned:
            await resolver.async_close()
        self.owned.clear()
        self.resolvers.clear()
        self.cache.clear()
