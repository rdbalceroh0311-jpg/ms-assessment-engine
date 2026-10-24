"""Exact HTTPS credential destinations, including SDK token exchange transports."""

import re
from urllib.parse import urlsplit

import httpx
import requests
from requests.adapters import HTTPAdapter

HOSTS = {
    # Virtual routes dispatched by the SSH adapter, never an HTTP destination.
    "wp_cli": frozenset({"wp-cli.invalid"}),
    "gsc": frozenset({"www.googleapis.com"}),
    "ga4": frozenset({"analyticsadmin.googleapis.com", "analyticsdata.googleapis.com"}),
    "gbp": frozenset(
        {
            "mybusinessaccountmanagement.googleapis.com",
            "mybusinessbusinessinformation.googleapis.com",
            "mybusiness.googleapis.com",
            "businessprofileperformance.googleapis.com",
        }
    ),
    "workspace": frozenset({"admin.googleapis.com"}),
    "m365": frozenset({"graph.microsoft.com"}),
    "bing_wmt": frozenset({"ssl.bing.com"}),
    "dataforseo": frozenset({"api.dataforseo.com"}),
    "pagespeed": frozenset({"www.googleapis.com"}),
    "crux": frozenset({"chromeuxreport.googleapis.com"}),
    "google_auth": frozenset({"oauth2.googleapis.com"}),
    "microsoft_auth": frozenset({"login.microsoftonline.com"}),
}


def validate_api_url(provider: str, url: str) -> None:
    try:
        parts = urlsplit(url)
        if (
            re.search(r"[\x00-\x20\x7f\\%]", parts.netloc)
            or re.search(r"[\x00-\x20\x7f\\]", url)
            or parts.scheme != "https"
            or parts.netloc not in HOSTS[provider]
            or parts.fragment
            or parts.username is not None
        ):
            raise ValueError
    except (ValueError, KeyError):
        raise ValueError("Credential destination refused") from None


def api_client(provider: str, **kwargs) -> httpx.Client:
    kwargs.update(follow_redirects=False, trust_env=False)
    kwargs["event_hooks"] = {"request": [lambda r: validate_api_url(provider, str(r.url))]}
    return httpx.Client(**kwargs)


def api_request(provider: str, method: str, url: str, **kwargs) -> httpx.Response:
    validate_api_url(provider, url)
    # Single fixed-destination requests need no reusable cookie jar or redirect state.
    kwargs.update(follow_redirects=False, trust_env=False)
    return getattr(httpx, method.lower())(url, **kwargs)


class PinnedAdapter(HTTPAdapter):
    def __init__(self, provider):
        super().__init__()
        self.provider = provider

    def send(self, request, *args, **kwargs):
        validate_api_url(self.provider, request.url)
        kwargs["verify"] = True
        return super().send(request, *args, **kwargs)


def pin_session(session: requests.Session, provider: str) -> None:
    """Retain OAuth2Session's protocol logic; deny any redirect before its next send."""
    session.trust_env = False
    session.max_redirects = 0
    session.mount("https://", PinnedAdapter(provider))
    session.mount("http://", PinnedAdapter(provider))


class PinnedSession(requests.Session):
    """For google-auth, OAuthlib and MSAL: validate before preparing/sending secrets.

    Requests normally follows redirects even on token POSTs. Deny them explicitly;
    discovered token endpoints are subject to the same check as configured endpoints.
    """

    def __init__(self, provider: str):
        super().__init__()
        self.provider = provider
        self.trust_env = False

    def send(self, request, **kwargs):
        validate_api_url(self.provider, request.url)
        kwargs.update(allow_redirects=False, verify=True)
        kwargs["timeout"] = 10
        return super().send(request, **kwargs)
