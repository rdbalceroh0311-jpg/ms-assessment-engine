"""Ephemeral browser contexts; Chromium never connects directly to page destinations."""

from typing import TypedDict

import httpx
from playwright.async_api import WebSocketRoute as AsyncWebSocketRoute

from ams_assess.network import public_client, validate_public_url


class LaunchOptions(TypedDict):
    args: list[str]


LAUNCH_OPTIONS: LaunchOptions = {
    "args": [
        "--disable-extensions",
        "--disable-background-networking",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ],
}
CONTEXT_OPTIONS = {
    "accept_downloads": False,
    "permissions": [],
    "service_workers": "block",
    "ignore_https_errors": False,
    "storage_state": {"cookies": [], "origins": []},
}


async def deny_websocket(ws):
    # Playwright dispatches this callback on its event loop even for the sync API.
    # Closing through the async facade avoids blocking that loop on a sync close.
    await AsyncWebSocketRoute(ws._impl_obj).close(code=1008, reason="Assessment policy")


def isolate_context(browser, *, offline=False, **kwargs):
    session = browser.new_context(**kwargs, **CONTEXT_OPTIONS)
    session.route_web_socket("**/*", deny_websocket)
    if offline:
        session.set_offline(True)
        session.route("**/*", lambda route: route.abort())
    else:
        session.route("**/*", public_route)
    return session


def public_route(route):
    """Fetch through the IP-pinning transport, including redirects and subresources.

    Do not forward browser cookies, authorization, referrers or site-supplied headers.
    Requests carrying writes, credentials, or WebSocket traffic are never necessary
    for this observational collector. Response cookies are discarded as well.
    """
    request = route.request
    if request.method not in {"GET", "HEAD"}:
        route.abort()
        return
    try:
        validate_public_url(request.url)
        with public_client(
            follow_redirects=True,
            event_hooks={"request": [strip_browser_credentials]},
        ) as client:
            with client.stream(request.method, request.url, timeout=15) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 10_000_000:
                        route.abort()
                        return
                headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k
                    not in {
                        "set-cookie",
                        "www-authenticate",
                        "content-encoding",
                        "content-length",
                        "transfer-encoding",
                        "x-ams-resolved-url",
                    }
                }
                headers["x-ams-resolved-url"] = str(response.url)
                route.fulfill(status=response.status_code, headers=headers, body=bytes(body))
    except (httpx.HTTPError, ValueError, OSError):
        route.abort()


def strip_browser_credentials(request: httpx.Request) -> None:
    for header in ("authorization", "cookie", "referer"):
        request.headers.pop(header, None)
