"""Which URLs this plugin is willing to render.

A screenshot tool that renders whatever URL a model asks for is a way to read the host: file://
reaches the filesystem, and http://127.0.0.1, private ranges and the cloud metadata address reach
services that were never meant to be photographed. So only plain web URLs are allowed, and any
address that resolves to a non public host is refused unless the operator allowed it by name.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

DEFAULT_ALLOW_HOSTS = ("localhost", "127.0.0.1", "::1")

# Names that almost always point inside: cloud metadata aliases and container-local service names.
_SUSPECT_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain", ".home.arpa")
_METADATA_HOSTS = {"metadata", "metadata.google.internal", "instance-data"}


class UrlPolicyError(ValueError):
    """Raised when a URL is outside the render policy."""


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_url(url: str, allow_hosts: tuple[str, ...] | list[str] = DEFAULT_ALLOW_HOSTS) -> str:
    """Return the URL unchanged, or raise UrlPolicyError explaining why it is refused."""
    if not isinstance(url, str) or not url.strip():
        raise UrlPolicyError("empty url")

    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UrlPolicyError(f"only http and https are rendered, not {scheme or 'a bare path'}")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UrlPolicyError("url has no host")

    allowed = {h.strip().lower() for h in (allow_hosts or ())}
    if host in allowed:
        return url

    if host in _METADATA_HOSTS or host.endswith(_SUSPECT_SUFFIXES):
        raise UrlPolicyError(f"host not allowed: {host}")

    # A literal IP can be judged without asking the resolver.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(literal):
            raise UrlPolicyError(f"address is not public: {host}")
        return url

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UrlPolicyError(f"host does not resolve: {host} ({exc.strerror or exc})") from None

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UrlPolicyError(f"host does not resolve: {host}")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            raise UrlPolicyError(f"unexpected address for {host}: {raw}") from None
        if not _is_public(ip):
            raise UrlPolicyError(f"{host} resolves to a non public address: {ip}")
    return url


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
