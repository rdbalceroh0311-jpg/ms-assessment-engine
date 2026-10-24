"""Bounded target resolution, using the bundled public suffix list (no network lookup)."""

import ipaddress
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx
from publicsuffixlist import PublicSuffixList

from ams_assess.models import is_local, normalize_target
from ams_assess.network import UnsafeURL, public_client, validate_public_url

MAX_REDIRECTS = 5
RESOLUTION_SECONDS = 20
REQUEST_SECONDS = 8
# An explicit tool identity avoids misleading browser signatures rejected by some hosts.
NORMAL_UA = "AMSAssessment/1.0"
LOOPBACK_ALIASES = {"localhost", "127.0.0.1", "::1"}


@lru_cache(maxsize=1)
def suffixes() -> PublicSuffixList:
    return PublicSuffixList()


def registrable(host: str) -> str | None:
    host = host.lower().removesuffix(".")
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        value = suffixes().privatesuffix(host)
        return value if isinstance(value, str) else None


def same_site(first: str, second: str) -> bool:
    a, b = urlsplit(first), urlsplit(second)
    hosts = [(p.hostname or "").removesuffix(".") for p in (a, b)]
    if hosts[0] == hosts[1]:
        return True
    # Explicit fixture exception; private addresses in general are not interchangeable.
    if all(host in LOOPBACK_ALIASES for host in hosts):
        return a.port == b.port
    domain = registrable(hosts[0])
    return bool(domain and domain == registrable(hosts[1]))


def crawl_hosts(requested: str, resolved: str) -> set[str]:
    hosts = {(urlsplit(u).hostname or "").removesuffix(".") for u in (requested, resolved)}
    for host in list(hosts):
        domain = registrable(host)
        if domain and host in {domain, "www." + domain} and not is_local(host):
            hosts.update({domain, "www." + domain})
    return hosts


def resolve_target(target: str) -> dict[str, Any]:
    """GET headers only, at most five redirects, without following an out-of-site Location."""
    try:
        validate_public_url(target if "://" in target else f"https://{target}")
    except UnsafeURL:
        return {
            "requested": target,
            "resolved": None,
            "chain": [],
            "unsafe": True,
            "error": "Unsafe target destination",
        }
    requested = normalize_target(target)
    current = requested
    parts = urlsplit(requested)
    # Bare domains already normalise to HTTPS. Prefer HTTPS for an explicit public HTTP URL
    # too, retaining HTTP only if that initial HTTPS connection cannot be established.
    upgraded = parts.scheme == "http" and not is_local(parts.hostname or "") and parts.port is None
    if upgraded:
        current = urlunsplit(("https", parts.netloc, parts.path, "", ""))
    chain: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {
        "requested": requested,
        "resolved": None,
        "chain": chain,
        "unsafe": False,
    }
    visited: set[str] = set()
    deadline = time.monotonic() + RESOLUTION_SECONDS
    redirects = 0
    with public_client(follow_redirects=False) as client:
        while True:
            outcome["last_safe_url"] = current
            if current in visited:
                outcome["error"] = "Target redirect loop"
                outcome["unsafe"] = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome["error"] = "Target resolution deadline reached"
                break
            visited.add(current)
            try:
                # Stream and close without downloading a potentially unbounded page body.
                with client.stream(
                    "GET",
                    current,
                    timeout=min(REQUEST_SECONDS, remaining),
                    headers={"User-Agent": NORMAL_UA},
                ) as response:
                    status = response.status_code
                    location = response.headers.get("location")
            except httpx.HTTPError as exc:
                chain.append({"url": current, "error": type(exc).__name__})
                if isinstance(exc, UnsafeURL):
                    outcome["unsafe"] = True
                if upgraded and len(chain) == 1 and isinstance(exc, httpx.ConnectError):
                    current = requested
                    continue
                outcome["error"] = f"Target resolution failed ({type(exc).__name__})"
                break
            entry: dict[str, Any] = {"url": current, "http_status": status}
            chain.append(entry)
            if status not in {301, 302, 303, 307, 308}:
                if upgraded and len(chain) == 1 and not 200 <= status < 300:
                    # Only undo our speculative upgrade, never an actual HTTPS redirect.
                    outcome["upgrade_limitation"] = f"Preferred HTTPS returned HTTP {status}"
                    current = requested
                    continue
                if 200 <= status < 300:
                    outcome["resolved"] = current
                else:
                    outcome["error"] = f"Target resolution returned HTTP {status}"
                break
            if not location:
                outcome["error"] = "Target redirect has no Location"
                break
            try:
                candidate = urldefrag(urljoin(current, location))[0]
                validate_public_url(candidate)
                following = normalize_target(candidate)
            except (ValueError, UnsafeURL):
                # Do not retain a Location containing credentials or query secrets.
                outcome["error"] = "Target redirect has an unsupported or unsafe Location"
                outcome["unsafe"] = True
                break
            entry["location"] = following
            if not same_site(requested, following):
                outcome["error"] = "Target redirect leaves the registrable domain; not followed"
                outcome["unsafe"] = True
                break
            if current.startswith("https:") and following.startswith("http:"):
                outcome["error"] = "Target redirect downgrades HTTPS to HTTP; not followed"
                outcome["unsafe"] = True
                break
            if redirects >= MAX_REDIRECTS:
                outcome["error"] = "Target redirect limit reached (5 hops)"
                outcome["unsafe"] = True
                break
            redirects += 1
            current = following
    return outcome
