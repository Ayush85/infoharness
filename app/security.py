"""URL validation and SSRF protection.

Scans start from user-supplied URLs, so every outbound request must be checked:
only http(s), only public IP addresses, no credentials in the URL, standard ports.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from app import config

ALLOWED_PORTS = {None, 80, 443, 8080, 8443}


class UnsafeURL(ValueError):
    pass


def normalize_input_url(raw: str) -> str:
    """Turn user input like 'docs.example.com/help' into a full https URL."""
    raw = (raw or "").strip()
    if not raw:
        raise UnsafeURL("Please enter a URL.")
    if len(raw) > 2000:
        raise UnsafeURL("URL is too long.")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http and https URLs are supported.")
    if not parts.hostname:
        raise UnsafeURL("That doesn't look like a valid URL.")
    if parts.username or parts.password:
        raise UnsafeURL("URLs with credentials are not allowed.")
    host = parts.hostname.lower().rstrip(".")
    netloc = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_url_safe(url: str) -> None:
    """Raise UnsafeURL if the URL points somewhere we must not request."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"Blocked scheme: {parts.scheme}")
    if parts.username or parts.password:
        raise UnsafeURL("Credentials in URL are not allowed.")
    try:
        port = parts.port
    except ValueError as e:
        raise UnsafeURL("Invalid port.") from e
    host = parts.hostname
    if not host:
        raise UnsafeURL("Missing host.")
    if config.settings.allow_private_hosts:
        return
    if port not in ALLOWED_PORTS:
        raise UnsafeURL(f"Port {port} is not allowed.")
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeURL("Private hosts are not allowed.")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"Could not resolve host '{host}'.") from e
    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise UnsafeURL("This address resolves to a private network and cannot be scanned.")
