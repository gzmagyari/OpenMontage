"""Which network addresses a box may reach: public internet addresses only."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlsplit

_URL = re.compile(r"^https?://", re.I)


def is_public_address(value: str) -> bool:
    """True for a globally routable unicast address. Private, loopback, link-local, carrier-grade
    NAT, reserved, documentation and multicast ranges are refused, IPv4 inside IPv6 included."""
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour or (address.teredo[1] if address.teredo else None)
        if embedded is not None:
            return is_public_address(str(embedded))
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return is_public_address(str(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)))
    return bool(address.is_global) and not address.is_multicast


class AddressRefused(Exception):
    """The host is not on the public internet (or does not resolve)."""


async def resolve_public(host: str, port: int) -> list[tuple[Any, ...]]:
    """Resolve a host and return its socket addresses, refusing it unless EVERY address is public.
    Callers connect to one of these addresses, never to the name again (no second lookup)."""
    host = (host or "").strip().strip("[]")
    if not host:
        raise AddressRefused("no host")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AddressRefused(f"{host} does not resolve: {exc}") from exc
    if not infos:
        raise AddressRefused(f"{host} does not resolve")
    for info in infos:
        if not is_public_address(info[4][0]):
            raise AddressRefused(f"{host} is not a public internet address")
    return infos


def urls_in(value: Any) -> list[str]:
    """Every http(s) URL among a tool call's input values, however deeply nested."""
    found: list[str] = []
    if isinstance(value, str):
        if _URL.match(value.strip()):
            found.append(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(urls_in(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(urls_in(item))
    return found


async def check_public_urls(value: Any) -> None:
    """Refuse a tool call whose inputs point at anything but the public internet."""
    for url in urls_in(value):
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        await resolve_public(parts.hostname or "", port)
