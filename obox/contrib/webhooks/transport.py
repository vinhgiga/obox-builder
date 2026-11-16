"""Pin HTTPS connections to public, validated DNS answers while preserving TLS identity."""

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit


def destination(url: str, allowed_hosts: tuple[str, ...] = ()) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode().lower()
        if (
            parsed.scheme != "https"
            or not host
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "\\" in url
            or len(url) > 4096
            or (allowed_hosts and host not in allowed_hosts)
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or address.is_multicast):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Use an allowed public HTTPS destination on port 443.") from exc
    return host, parsed.path or "/"


async def resolve(url: str, allowed_hosts: tuple[str, ...] = ()) -> tuple[str, str]:
    host, _ = destination(url, allowed_hosts)
    answers = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    if not addresses or any(
        not address.is_global
        or address.is_multicast
        or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None)
        for address in addresses
    ):
        raise ValueError("The webhook destination must resolve only to public addresses.")
    return host, str(addresses[0])


async def send(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    allowed_hosts: tuple[str, ...],
    transport: Any = None,
) -> int:
    import httpx

    async with asyncio.timeout(20):
        host, address = await resolve(url, allowed_hosts)
        target = httpx.URL(url).copy_with(host=address)
        async with (
            httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client,
            client.stream(
                "POST",
                target,
                content=body,
                headers={**headers, "Host": f"[{host}]" if ":" in host else host},
                extensions={"sni_hostname": host},
            ) as response,
        ):
            return response.status_code
