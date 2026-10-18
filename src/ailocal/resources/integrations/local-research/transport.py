"""Bounded HTTP with cancellable reads and DNS-pinned public page fetching."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
from urllib.parse import parse_qsl, urljoin, urlsplit

MAX_BYTES = 1024 * 1024
SECRET_PARAMETERS = {'access_token', 'api_key', 'apikey', 'token', 'secret', 'password', 'authorization', 'auth'}


class ResearchError(Exception):
    """Only constant, credential-free messages cross the MCP boundary."""


def hostname(value: str) -> str:
    try:
        host = value.rstrip('.').encode('idna').decode('ascii').lower()
        if not host or len(host) > 253 or any(
            not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
            or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in label)
            for label in host.split('.')
        ):
            raise ValueError
        return host
    except (ValueError, UnicodeError):
        raise ResearchError('INVALID_SITE: use DNS hostnames, not URLs or query syntax') from None


def allowed(host: str, sites: list[str]) -> bool:
    return not sites or any(host == s or host.endswith('.' + s) for s in sites)


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif (address.sixtofour or address.teredo
              or address in ipaddress.IPv6Network('64:ff9b::/96')):
            # Transition mechanisms can route apparently global IPv6 to an
            # embedded private IPv4 destination. Public pages need none of them.
            return False
    return address.is_global and not address.is_multicast


async def request(url: str, *, public: bool = False, sites: list[str] | None = None,
                  payload: dict | None = None, headers: dict | None = None,
                  timeout: float = 15) -> tuple[int, dict, bytes]:
    """One bounded request; redirects are checked and pinned on every hop.

    Backend endpoints are trusted operator config. Public page URLs never get
    credential headers, ambient proxies, or a second DNS lookup at connect time.
    """
    try:
        async with asyncio.timeout(timeout):
            return await _request(url, public, sites or [], payload, headers or {})
    except ResearchError:
        raise
    except TimeoutError:
        raise ResearchError('NETWORK_TIMEOUT: retry later or narrow the query') from None
    except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise ResearchError('NETWORK_FAILED: check the configured backend or source availability') from None


async def _request(url, public, sites, payload, headers):
    for _ in range(4):
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or (public and any(k.lower() in SECRET_PARAMETERS for k, _ in parse_qsl(parts.query)))
                or any(c in url for c in '\r\n\x00')):
            raise ResearchError('INVALID_URL: only HTTP(S) without credentials is supported')
        host = parts.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if public and not allowed(host, sites):
            raise ResearchError('SITE_REDIRECT_REFUSED: redirect left the requested sites')
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
        if not addresses or (public and any(not public_address(a[4][0]) for a in addresses)):
            raise ResearchError('PRIVATE_ADDRESS_REFUSED: source must resolve only to public addresses')
        address = addresses[0]
        tls = ssl.create_default_context() if parts.scheme == 'https' else None
        reader, writer = await asyncio.open_connection(
            address[4][0], port, family=address[0], ssl=tls,
            server_hostname=host if tls else None, limit=65536)
        try:
            target = (parts.path or '/') + ('?' + parts.query if parts.query else '')
            target = target.encode('ascii').decode('ascii')
            body = json.dumps(payload).encode() if payload is not None else b''
            authority = f'[{host}]' if ':' in host else host
            if parts.port:
                authority += f':{port}'
            fields = {'Host': authority, 'Connection': 'close',
                      'Accept-Encoding': 'identity', 'User-Agent': 'ailocal-local-research/1'}
            if payload is not None:
                fields.update({'Content-Type': 'application/json', 'Content-Length': str(len(body))})
            if not public:
                fields.update(headers)
            wire = f'{"POST" if payload is not None else "GET"} {target} HTTP/1.1\r\n'
            wire += ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n'
            writer.write(wire.encode('ascii') + body)
            await writer.drain()
            head = (await reader.readuntil(b'\r\n\r\n')).decode('iso-8859-1')
            lines = head.split('\r\n')
            status = int(lines[0].split()[1])
            response_headers = {}
            for line in lines[1:]:
                if ':' in line:
                    key, value = line.split(':', 1)
                    response_headers[key.lower()] = value.strip()
            if status in (301, 302, 303, 307, 308):
                if not public or 'location' not in response_headers:
                    raise ResearchError('BACKEND_REDIRECT_REFUSED: configure the final backend URL')
                url = urljoin(url, response_headers['location'])
                continue
            if response_headers.get('content-encoding', 'identity').lower() != 'identity':
                raise ResearchError('ENCODING_UNSUPPORTED: source ignored the identity encoding request')
            data = bytearray()
            chunked = response_headers.get('transfer-encoding', '').lower() == 'chunked'
            length = response_headers.get('content-length')
            remaining = int(length) if length is not None else None
            if remaining is not None and (remaining < 0 or remaining > MAX_BYTES):
                raise ResearchError('SOURCE_TOO_LARGE: response exceeds the byte limit')
            while remaining is None or remaining:
                if chunked:
                    size = int((await reader.readline()).split(b';')[0].strip(), 16)
                    if size == 0:
                        break
                    if len(data) + size > MAX_BYTES:
                        raise ResearchError('SOURCE_TOO_LARGE: response exceeds the byte limit')
                    data.extend(await reader.readexactly(size))
                    if await reader.readexactly(2) != b'\r\n':
                        raise ResearchError('INVALID_HTTP: malformed chunked response')
                else:
                    chunk = await reader.read(min(65536, remaining if remaining is not None else 65536))
                    if not chunk:
                        if remaining:
                            raise ResearchError('INVALID_HTTP: incomplete response')
                        break
                    data.extend(chunk)
                    if remaining is not None:
                        remaining -= len(chunk)
                if len(data) > MAX_BYTES:
                    raise ResearchError('SOURCE_TOO_LARGE: response exceeds the byte limit')
            response_headers['final-url'] = url
            return status, response_headers, bytes(data)
        finally:
            writer.close()
            # Do not let shutdown of a peer's TLS stream consume the deadline.
    raise ResearchError('TOO_MANY_REDIRECTS: source exceeded the redirect limit')
