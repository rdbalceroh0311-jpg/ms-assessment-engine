"""Credential egress attacks use FAKE canaries and actual local receiving servers."""

import base64
import json

import httpx
import pytest
import requests
import requests.adapters
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from requests_oauthlib import OAuth2Session

from ams_assess import egress
from ams_assess.connectors import bing_wmt, ga4, gbp, gsc, m365, workspace
from ams_assess.connectors.auth import BoundedRequest
from ams_assess.connectors.core import (
    AuthMaterial,
    ConnectorContext,
    ConnectorError,
    Endpoint,
    Period,
)
from ams_assess.connectors.http import ReadClient

CANARY = "FAKE-TRACK-B-CREDENTIAL"
READS = [
    (gsc.CONNECTOR, gsc.BASE),
    (ga4.CONNECTOR, ga4.ADMIN + "accountSummaries"),
    (gbp.CONNECTOR, gbp.ACCOUNTS + "accounts"),
    (workspace.CONNECTOR, workspace.DIRECTORY + "users"),
    (m365.CONNECTOR, m365.GRAPH + "/users"),
    (bing_wmt.CONNECTOR, bing_wmt.BASE + "GetUserSites"),
]


@pytest.fixture
def provider_wire(monkeypatch):
    """Route official HTTPS hostnames to fixtures after product destination validation."""
    routes = {}
    send = httpx.HTTPTransport.handle_request

    def wire(transport, request):
        fixture = routes.get(request.url.host)
        if fixture:
            request = httpx.Request(
                request.method,
                fixture.origin + request.url.raw_path.decode(),
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
        return send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", wire)
    return routes


@pytest.mark.parametrize("connector,url", READS, ids=[r[0].key for r in READS])
@pytest.mark.parametrize("status", [302, 307])
def test_connector_redirects_and_arbitrary_urls_cannot_receive_token(
    connector,
    url,
    status,
    http_server,
    provider_wire,
):
    attacker = http_server()
    provider = http_server(lambda r: (status, {"Location": attacker.origin}, b""))
    provider_wire[httpx.URL(url).host] = provider
    material = AuthMaterial({"Authorization": "Bearer " + CANARY}, (CANARY,))
    client = ReadClient(connector, material)
    try:
        with pytest.raises(ConnectorError):
            client.request("GET", url)
        for hostile in (
            attacker.origin,
            url.replace("https:", "http:"),
            url.replace(".com", ".com.evil.test"),
        ):
            with pytest.raises(ConnectorError):
                client.request("GET", hostile)
    finally:
        client.close()
    assert len(provider.received) == 1
    assert provider.received[0]["headers"]["Authorization"] == "Bearer " + CANARY
    assert attacker.received == []


@pytest.mark.parametrize("provider", ["dataforseo", "pagespeed", "crux"])
def test_basic_and_query_credentials_do_not_follow_provider_redirects(
    provider,
    http_server,
    provider_wire,
):
    attacker = http_server()
    fixture = http_server(lambda r: (307, {"Location": attacker.origin}, b""))
    host = next(iter(egress.HOSTS[provider]))
    provider_wire[host] = fixture
    options = (
        {"auth": ("FAKE-user", CANARY)} if provider == "dataforseo" else {"params": {"key": CANARY}}
    )
    response = egress.api_request(provider, "POST", f"https://{host}/fixture", **options)
    assert response.status_code == 307
    assert len(fixture.received) == 1
    if provider == "dataforseo":
        value = fixture.received[0]["headers"]["Authorization"].removeprefix("Basic ")
        assert base64.b64decode(value).decode() == "FAKE-user:" + CANARY
    else:
        assert CANARY in fixture.received[0]["path"]
    assert attacker.received == []


@pytest.mark.parametrize("provider", list(egress.HOSTS))
def test_api_hosts_are_exact_https_without_userinfo_ports_or_suffixes(provider):
    host = next(iter(egress.HOSTS[provider]))
    egress.validate_api_url(provider, "https://" + host + "/fixture")
    for url in [
        f"http://{host}/",
        f"https://{host}:444/",
        f"https://{host}.evil.test/",
        f"https://{host}./",
        f"https://FAKE@{host}/",
        f"https://@{host}/",
        f"https://{host}%2f@evil.test/",
        f"https://{host}/#fragment",
    ]:
        with pytest.raises(ValueError, match="destination refused"):
            egress.validate_api_url(provider, url)


def test_graph_pagination_attack_preserves_first_page_without_leaking_token(
    http_server, provider_wire
):
    attacker = http_server()
    body = {"value": [{"id": "synthetic"}], "@odata.nextLink": attacker.origin + "/steal"}
    provider = http_server(
        lambda r: (200, {"Content-Type": "application/json"}, json.dumps(body).encode())
    )
    provider_wire["graph.microsoft.com"] = provider
    client = ReadClient(m365.CONNECTOR, AuthMaterial({"Authorization": "Bearer " + CANARY}))
    try:
        ctx = ConnectorContext(client, "tenant:synthetic", Period.make())
        outcome = m365._listing(ctx, "users", {}, {}, lambda out, row: out.update(row))
        assert outcome.status == "failed" and outcome.rows == 1
    finally:
        client.close()
    assert len(provider.received) == 1
    assert CANARY in provider.received[0]["headers"]["Authorization"]
    assert attacker.received == []


@pytest.mark.parametrize("mode", ["redirect", "pagination"])
def test_bing_query_key_is_not_sent_to_redirect_or_nextlink(mode, http_server, provider_wire):
    attacker = http_server()
    response = (
        (302, {"Location": attacker.origin}, b"")
        if mode == "redirect"
        else (200, {}, json.dumps({"d": [], "__next": attacker.origin}).encode())
    )
    provider = http_server(lambda r: response)
    provider_wire["ssl.bing.com"] = provider
    client = ReadClient(bing_wmt.CONNECTOR, AuthMaterial(secrets=(CANARY,), private_data=CANARY))
    try:
        ctx = ConnectorContext(client, "https://example.test/", Period.make())
        with pytest.raises(ConnectorError):
            bing_wmt.request_rows(ctx, "GetUserSites")
    finally:
        client.close()
    assert len(provider.received) == 1 and "apikey=" + CANARY in provider.received[0]["path"]
    assert attacker.received == []


@pytest.mark.parametrize("key", ["ga4", "gbp", "workspace"])
def test_url_shaped_google_pagination_tokens_remain_on_official_host(
    key, http_server, provider_wire
):
    attacker = http_server()
    response = {"rows": [], "nextPageToken": attacker.origin + "/steal"}
    provider = http_server(lambda r: (200, {}, json.dumps(response).encode()))
    connector, url = next((c, u) for c, u in READS if c.key == key)
    provider_wire[httpx.URL(url).host] = provider
    client = ReadClient(connector, AuthMaterial({"Authorization": "Bearer " + CANARY}))
    ctx = ConnectorContext(client, "synthetic", Period.make(), max_pages=2)
    try:
        if key == "ga4":
            ga4.listing(ctx, "fixture", "accountSummaries", "rows", lambda row: [])
        elif key == "gbp":
            list(gbp.pages(ctx, Endpoint("fixture", url), url, "rows", 10))
        else:
            workspace.pages(
                ctx, Endpoint("fixture", url), "users", url, {}, "rows", lambda row: None
            )
    finally:
        client.close()
    assert len(provider.received) == 2
    assert all(CANARY in r["headers"]["Authorization"] for r in provider.received)
    assert attacker.received == []


@pytest.fixture
def requests_wire(monkeypatch):
    routes = {}
    send = requests.adapters.HTTPAdapter.send

    def wire(adapter, request, **kwargs):
        assert kwargs["verify"] is True
        parsed = httpx.URL(request.url)
        if parsed.host in routes:
            request.url = routes[parsed.host].origin + parsed.raw_path.decode()
        return send(adapter, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", wire)
    return routes


def test_real_google_auth_refresh_and_oauthlib_exchange_refuse_redirects(
    http_server, requests_wire
):
    attacker = http_server()
    provider = http_server(lambda r: (307, {"Location": attacker.origin}, b"{}"))
    requests_wire["oauth2.googleapis.com"] = provider
    creds = Credentials(
        None,
        refresh_token=CANARY,
        client_id="FAKE-client",
        client_secret="FAKE-secret",
        token_uri="https://oauth2.googleapis.com/token",
    )
    with pytest.raises(RefreshError):
        creds.refresh(BoundedRequest())
    session = OAuth2Session("FAKE-client", redirect_uri="http://127.0.0.1/callback")
    egress.pin_session(session, "google_auth")
    with pytest.raises(requests.TooManyRedirects):
        session.fetch_token(
            "https://oauth2.googleapis.com/token", code=CANARY, client_secret="FAKE-secret"
        )
    assert len(provider.received) == 2
    assert all(CANARY.encode() in r["body"] for r in provider.received)
    assert attacker.received == []


def test_msal_discovered_token_endpoint_is_validated_before_canary_egress(
    http_server, requests_wire
):
    attacker = http_server()
    tenant = "11111111-1111-4111-8111-111111111111"
    metadata = {
        "authorization_endpoint": f"https://login.microsoftonline.com/{tenant}/authorize",
        "token_endpoint": "https://attacker.example/tenant/token",
        "issuer": f"https://login.microsoftonline.com/{tenant}",
    }
    provider = http_server(lambda r: (200, {}, json.dumps(metadata).encode()))
    requests_wire["login.microsoftonline.com"] = provider
    requests_wire["attacker.example"] = attacker
    app, _ = m365._application(
        {"client_id": "33333333-3333-4333-8333-333333333333", "tenant_id": tenant}
    )
    with pytest.raises(ValueError, match="destination refused"):
        app.acquire_token_by_refresh_token(CANARY, scopes=["https://graph.microsoft.com/User.Read"])
    assert len(provider.received) == 1
    assert attacker.received == []
