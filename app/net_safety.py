"""Safe outbound HTTP helpers for untrusted news URLs."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


class UnsafeUrlError(ValueError):
    """Raised when an outbound URL could reach a non-public network target."""


Resolver = Callable[..., Iterable[tuple[Any, ...]]]


def _is_non_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def validate_public_http_url(
    url: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Validate that an HTTP(S) URL resolves only to public IP addresses."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrlError("URL contains an invalid port") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("Credentials in article URLs are not allowed")

    try:
        resolved = list(
            resolver(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        )
    except OSError as exc:
        raise UnsafeUrlError("URL hostname could not be resolved") from exc

    if not resolved:
        raise UnsafeUrlError("URL hostname did not resolve to an address")

    for entry in resolved:
        address = entry[4][0]
        if _is_non_public_ip(address):
            raise UnsafeUrlError(f"URL resolves to a non-public address: {address}")

    return url


def safe_get(
    url: str,
    *,
    timeout: float,
    proxy: str | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    max_redirects: int = 5,
) -> httpx.Response:
    """GET a public URL while validating every redirect target."""
    current_url = url
    current_params = params

    with httpx.Client(
        timeout=timeout,
        proxy=proxy,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for redirect_count in range(max_redirects + 1):
            validate_public_http_url(current_url)
            response = client.get(
                current_url,
                headers=headers,
                params=current_params,
            )
            current_params = None

            # For direct connections, verify the actual peer as well as the
            # preflight DNS result. This closes the usual DNS-rebinding gap.
            if proxy is None:
                stream = response.extensions.get("network_stream")
                if stream is not None:
                    server_address = stream.get_extra_info("server_addr")
                    if server_address and _is_non_public_ip(server_address[0]):
                        response.close()
                        raise UnsafeUrlError(
                            "Connection reached a non-public address: "
                            f"{server_address[0]}"
                        )

            if response.status_code not in {301, 302, 303, 307, 308}:
                return response

            location = response.headers.get("location")
            if not location:
                return response
            if redirect_count >= max_redirects:
                raise httpx.TooManyRedirects(
                    "Maximum redirect count exceeded",
                    request=response.request,
                )
            current_url = urljoin(str(response.url), location)

    raise RuntimeError("unreachable redirect state")
