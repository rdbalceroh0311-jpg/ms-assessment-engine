"""Bounded target resolution, using the bundled public suffix list (no network lookup)."""

import ipaddress
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx
from publicsuffixlist import PublicSuffixList

from ams_assess.models import is_local, normalize_target

MAX_REDIRECTS = 5
RESOLUTION_SECONDS = 20
LOOPBACK_ALIASES = {"localhost", "127.0.0.1", "::1"}


@lru_cache(maxsize=1)
def suffixes() -> PublicSuffixList:
    return PublicSuffixList()


def registrable(host: str) -> str | None:
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        value = suffixes().privatesuffix(host)
        return value if isinstance(value, str) else None


def same_site(first: str, second: str) -> bool:
    a, b = urlsplit(first), urlsplit(second)
    if a.hostname == b.hostname:
        return True
    # Explicit fixture exception; private addresses in general are not interchangeable.
    if a.hostname in LOOPBACK_ALIASES and b.hostname in LOOPBACK_ALIASES:
        return a.port == b.port
    domain = registrable(a.hostname or "")
    return bool(domain and domain == registrable(b.hostname or ""))


def crawl_hosts(requested: str, resolved: str) -> set[str]:
    hosts = {urlsplit(u).hostname or "" for u in (requested, resolved)}
    for host in list(hosts):
        domain = registrable(host)
        if domain and host in {domain, "www." + domain} and not is_local(host):
            hosts.update({domain, "www." + domain})
    return hosts


def resolve_target(target: str) -> dict[str, Any]:
    """GET headers only, at most five redirects, without following an out-of-site Location."""
    requested = normalize_target(target)
    current = requested
    parts = urlsplit(requested)
    # Bare domains already normalise to HTTPS. Prefer HTTPS for an explicit public HTTP URL
    # too, retaining HTTP only if that initial HTTPS connection cannot be established.
    upgraded = parts.scheme == "http" and not is_local(parts.hostname or "") and parts.port is None
    if upgraded:
        current = urlunsplit(("https", parts.netloc, parts.path, "", ""))
    chain: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {"requested": requested, "resolved": None, "chain": chain}
    visited: set[str] = set()
    deadline = time.monotonic() + RESOLUTION_SECONDS
    redirects = 0
    with httpx.Client(follow_redirects=False) as client:
        while True:
            if current in visited:
                outcome["error"] = "Target redirect loop"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome["error"] = "Target resolution deadline reached"
                break
            visited.add(current)
            try:
                # Stream and close without downloading a potentially unbounded page body.
                with client.stream("GET", current, timeout=min(5.0, remaining)) as response:
                    status = response.status_code
                    location = response.headers.get("location")
            except httpx.HTTPError as exc:
                chain.append({"url": current, "error": type(exc).__name__})
                if upgraded and len(chain) == 1 and isinstance(exc, httpx.ConnectError):
                    current = requested
                    continue
                outcome["error"] = f"Target resolution failed ({type(exc).__name__})"
                break
            entry: dict[str, Any] = {"url": current, "http_status": status}
            chain.append(entry)
            if status not in {301, 302, 303, 307, 308}:
                if 200 <= status < 300:
                    outcome["resolved"] = current
                else:
                    outcome["error"] = f"Target resolution returned HTTP {status}"
                break
            if not location:
                outcome["error"] = "Target redirect has no Location"
                break
            try:
                following = normalize_target(urldefrag(urljoin(current, location))[0])
            except ValueError:
                # Do not retain a Location containing credentials or query secrets.
                outcome["error"] = "Target redirect has an unsupported or unsafe Location"
                break
            entry["location"] = following
            if not same_site(requested, following):
                outcome["error"] = "Target redirect leaves the registrable domain; not followed"
                break
            if current.startswith("https:") and following.startswith("http:"):
                outcome["error"] = "Target redirect downgrades HTTPS to HTTP; not followed"
                break
            if redirects >= MAX_REDIRECTS:
                outcome["error"] = "Target redirect limit reached (5 hops)"
                break
            redirects += 1
            current = following
    return outcome
