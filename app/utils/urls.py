"""URL canonicalization and SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "ref",
    "referrer",
    "source",
    "src",
    "trk",
    "trkinfo",
    "refid",
    "trackingid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm",
    "lipi",
    "midtoken",
    "midsig",
    "eid",
    "otptoken",
    "ssid",
    "_hsenc",
    "_hsmi",
}
TRACKING_PREFIXES = ("utm_", "trk_", "mc_", "pk_", "hs_")
BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata"}


def canonicalize_url(url: str) -> str:
    """Normalize a URL so the same posting from different links compares equal."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    )
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


def is_valid_public_url(url: str) -> bool:
    """Cheap syntactic check: http(s), has a host, host is not a literal private IP or localhost."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    host = parts.hostname.lower().rstrip(".")
    if host in BLOCKED_HOSTS or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return _ip_is_public(host)


async def resolves_to_public(url: str) -> bool:
    """Full SSRF check before the server fetches a user/third-party supplied URL."""
    if not is_valid_public_url(url):
        return False
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(_ip_is_public(info[4][0]) for info in infos)
