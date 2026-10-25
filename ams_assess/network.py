"""Public collection boundary: validate every hop and connect only to checked addresses.

No environment or customer setting can enable local access. Tests may monkeypatch the
private exact-origin set for their own ephemeral HTTP fixtures.
"""

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

_fixture_origins: frozenset[str] = frozenset()


class UnsafeURL(httpx.RequestError):
    def __init__(self):
        # Never reflect a rejected URL (which may contain credentials).
        super().__init__("Network destination refused by public collection policy")


@dataclass(frozen=True)
class Destination:
    host: str
    addresses: tuple[str, ...]


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address in ipaddress.ip_network("::/96"):
            return False
        # Translation/transition addresses must not hide a private IPv4 destination.
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return public_address(str(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)))
        if address in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
        if address.sixtofour or address.teredo:
            return False
    return True


def validate_public_url(url: str) -> Destination:
    try:
        if re.search(r"[\x00-\x20\x7f\\]", url):
            raise ValueError
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        default = 443 if parts.scheme == "https" else 80
        port = default if parts.port is None else parts.port  # an explicit :0 is refused
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or "%" in parts.netloc
            or not 0 < port < 65536
        ):
            raise ValueError
        fixture = f"{parts.scheme}://{parts.netloc}" in _fixture_origins
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError from None
            # inet_aton accepts decimal, shortened, octal and hex IPv4 forms. Refuse
            # all of these rather than letting different URL stacks interpret them.
            try:
                socket.inet_aton(host)
            except OSError:
                pass
            else:
                raise ValueError from None
            if not fixture and (
                "." not in host
                or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal"))
            ):
                raise ValueError from None
        addresses = (
            (str(literal),)
            if literal
            else tuple(
                dict.fromkeys(
                    str(row[4][0])
                    for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                )
            )
        )
        if not addresses or any(
            not public_address(ip) and not (fixture and ipaddress.ip_address(ip).is_loopback)
            for ip in addresses
        ):
            raise ValueError
        return Destination(host, addresses)
    except (ValueError, UnicodeError, OSError):
        raise UnsafeURL() from None


class PublicTransport(httpx.BaseTransport):
    def __init__(self):
        # Pooling by numeric origin could reuse TLS authenticated for a different
        # hostname on the same IP. Verify SNI/certificate anew on each connection.
        self.inner = httpx.HTTPTransport(
            verify=True, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        checked = validate_public_url(str(request.url))
        # A numeric URL makes a second DNS resolution unable to change the peer.
        # Host and TLS SNI/certificate verification retain the original hostname.
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=checked.addresses[0]),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": checked.host},
        )
        return self.inner.handle_request(pinned)

    def close(self) -> None:
        self.inner.close()


def public_client(**kwargs) -> httpx.Client:
    # Explicit injected transports are for unit fixtures, never customer configuration.
    if kwargs.get("transport") is None:
        kwargs["transport"] = PublicTransport()
    kwargs["trust_env"] = False
    kwargs.setdefault("follow_redirects", False)
    kwargs.setdefault("max_redirects", 5)
    hooks = kwargs.setdefault("event_hooks", {})
    hooks["request"] = [*hooks.get("request", []), lambda r: validate_public_url(str(r.url))]
    hooks["response"] = [*hooks.get("response", []), validate_redirect]
    return httpx.Client(**kwargs)


def validate_redirect(response: httpx.Response) -> None:
    if response.is_redirect and "location" in response.headers:
        location = response.headers["location"]
        if re.search(r"[\x00-\x20\x7f\\]", location):
            raise UnsafeURL()
        validate_public_url(urljoin(str(response.url), location))


def public_get(url: str, **kwargs) -> httpx.Response:
    validate_public_url(url)
    with public_client(follow_redirects=kwargs.pop("follow_redirects", False)) as client:
        return client.get(url, **kwargs)


def public_post(url: str, **kwargs) -> httpx.Response:
    validate_public_url(url)
    with public_client() as client:
        return client.post(url, **kwargs)
