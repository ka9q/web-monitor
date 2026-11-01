"""Discover multicast Opus services directly over mDNS, without unicast DNS."""
import asyncio
import ipaddress
import logging
import pathlib
import sys

import ifaddr
from zeroconf import IPVersion, ServiceStateChange
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
