"""Synthetic mocked-HTTP contract tests; never read real credentials or customer data."""

import copy
import json
import logging
import threading
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from ams_assess import ai, cli, store
from ams_assess.connectors import auth, service
from ams_assess.connectors import bing_wmt as bing
from ams_assess.connectors.core import AuthMaterial, ConnectorContext, ConnectorError, Period
from ams_assess.connectors.http import ReadClient
from ams_assess.models import Evidence, Finding
from ams_assess.report import render_customer
from ams_assess.ui import LocalServer

SECRET = "synthetic-bing-key-never-output"
SITE = "https://example.test/"
DATES = {"start": "2026-09-01", "end": "2026-09-28"}
DAY = "2026-09-14T00:00:00-07:00"


class Provider:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.key = SECRET
        stats = {"Date": DAY, "Clicks": 5, "Impressions": 100}
        self.payloads = {
            "GetUserSites": [
                {"Url": SITE, "IsVerified": True, "AuthenticationCode": "verification-secret"},
                {"Url": "https://other.test/", "IsVerified": True},
                {"Url": "https://unverified.test/", "IsVerified": False},
            ],
            "GetRankAndTrafficStats": [stats],
            "GetQueryStats": [
                {
                    **stats,
                    "Query": "synthetic query",
                    "AvgClickPosition": 2,
                    "AvgImpressionPosition": 3,
                }
            ],
            "GetPageStats": [
                {
                    **stats,
                    "Query": SITE + "contact?email=person@example.test",
                    "AvgClickPosition": 2,
                    "AvgImpressionPosition": 3,
                }
            ],
            "GetCrawlStats": [{"Date": DAY, **dict.fromkeys(bing.CRAWL_FIELDS, 0)}],
            "GetCrawlIssues": [],
            "GetFeeds": [
                {
                    "Url": SITE + "sitemap.xml",
                    "Status": "Success",
                    "UrlCount": 10,
                    "Submitted": DAY,
                    "LastCrawled": DAY,
                }
            ],
            "GetSiteRoles": [
                {
                    "Role": 1,
                    "Expired": False,
                    "Email": "owner@example.test",
                    "DelegatedCode": "delegation-secret",
                }
            ],
            "GetBlockedUrls": [],
        }

    def __call__(self, request):
        self.calls.append(request)
        method = request.url.path.rsplit("/", 1)[1]
        assert request.method == "GET"
        assert request.url.host == "ssl.bing.com"
        assert request.url.params["apikey"] == self.key
        assert set(request.url.params) == (
            {"apikey"}
            if method == "GetUserSites"
            else {"apikey", "siteUrl", "includeAllSubdomains"}
            if method == "GetSiteRoles"
            else {"apikey", "siteUrl"}
        )
        if method != "GetUserSites":
            assert request.url.params["siteUrl"] == SITE
        if method == "GetSiteRoles":
            assert request.url.params["includeAllSubdomains"] == "false"
        if method in self.overrides:
            override = self.overrides[method]
            return override(request) if callable(override) else override
        return httpx.Response(200, json={"d": self.payloads[method]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    root, private = tmp_path / "customers", tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.delenv("BING_WMT_API_KEY", raising=False)
    (private / "credentials.env").write_text("BING_WMT_API_KEY=" + SECRET + "\n")
    auth.register_reference(
        "bing-synthetic", bing.CONNECTOR, env_var="BING_WMT_API_KEY", directory=private
    )
    monkeypatch.setattr(service, "config_dir", lambda: private)
    monkeypatch.setattr(auth, "config_dir", lambda: private)
    monkeypatch.setattr(cli, "data_root", lambda: root)
    provider = Provider()
    real_client = service.ReadClient
    monkeypatch.setattr(
        service,
        "ReadClient",
        lambda *a, transport=None, **kw: real_client(
            *a, transport=bing.BingTransport(a[1], inner=httpx.MockTransport(provider)), **kw
        ),
    )
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", lambda seconds: None)
    store.set_profile(root, "synthetic", {"name": "Synthetic", "websites": [SITE]})
    return SimpleNamespace(root=root, private=private, provider=provider)


def bind(env):
    return service.set_binding(env.root, "synthetic", "bing_wmt", "bing-synthetic", SITE)


def current(env):
    return store.load(env.root, "synthetic")


def prepare(env, **kwargs):
    return service.prepare(
        current(env), "bing_wmt", collect=True, start=DATES["start"], end=DATES["end"], **kwargs
    )


def execute(env):
    return service.execute(env.root, "synthetic", "bing_wmt", collect=True, **DATES)


def endpoint(result, key):
    return next(e.data for e in result.evidence if e.data["operation"] == key)


def test_auth_reference_setup_and_cli_commands(env, capsys):
    assert bing.CONNECTOR.auth_flow == "reference"
    assert "--env-var BING_WMT_API_KEY" in bing.CONNECTOR.auth_hint
    assert (
        cli.main(
            [
                "connect",
                "bing_wmt",
                "auth",
                "--credential-ref",
                "second",
                "--env-var",
                "BING_WMT_API_KEY",
            ]
        )
        == 0
    )
    assert not env.provider.calls
    assert cli.main(["connect", "bing_wmt", "list-resources", "--credential-ref", "second"]) == 0
    output = capsys.readouterr().out
    assert SITE in output and "unverified.test" not in output
    assert (
        cli.main(
            [
                "connect",
                "bing_wmt",
                "bind",
                "--customer",
                "synthetic",
                "--credential-ref",
                "second",
                "--resource",
                SITE,
            ]
        )
        == 0
    )
    for action in ("test", "collect"):
        assert (
            cli.main(
                [
                    "connect",
                    "bing_wmt",
                    action,
                    "--customer",
                    "synthetic",
                    "--start",
                    DATES["start"],
                    "--end",
                    DATES["end"],
                ]
            )
            == 0
        )
    assert current(env)["connectors"]["bing_wmt"]["status"] == "connected"
    assert SECRET not in output + capsys.readouterr().out
    assert (env.private / "credentials.env").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("source", ["env", "json", "missing", "empty", "wrong-connector"])
def test_auth_sources_and_missing_credentials(env, monkeypatch, source):
    if source == "env":
        monkeypatch.setenv("BING_WMT_API_KEY", SECRET)
        (env.private / "credentials.env").unlink()
    elif source == "json":
        path = env.private / "key.json"
        auth.atomic_private(path, {"api_key": SECRET})
        auth.register_reference("json-ref", bing.CONNECTOR, file=path, directory=env.private)
        material = bing.authenticate(bing.CONNECTOR, "json-ref", directory=env.private)
        assert material.private_data == SECRET and SECRET not in repr(material)
    elif source == "missing":
        (env.private / "credentials.env").unlink()
    elif source == "empty":
        (env.private / "credentials.env").write_text("BING_WMT_API_KEY=\n")
    elif source == "wrong-connector":
        from ams_assess.connectors.ga4 import CONNECTOR

        with pytest.raises(ConnectorError):
            auth.load_reference(CONNECTOR, "bing-synthetic", directory=env.private)
        return
    bind(env)
    _, state = prepare(env)
    assert state["status"] == (
        "credential_missing" if source in {"missing", "empty"} else "connected"
    )
    if state["status"] == "credential_missing":
        assert not env.provider.calls
        assert "BING_WMT_API_KEY" in state["detail"]


@pytest.mark.parametrize(
    "sites,status",
    [
        ([], "not_found"),
        ([{"Url": "http://example.test/", "IsVerified": True}], "not_found"),
        ([{"Url": SITE.rstrip("/"), "IsVerified": True}], "not_found"),
        ([{"Url": SITE, "IsVerified": False}], "permission_required"),
        ([{"Url": SITE, "IsVerified": "true"}], "permission_required"),
    ],
)
def test_exact_verified_selection_before_collection(env, sites, status):
    bind(env)
    env.provider.payloads["GetUserSites"] = sites
    result, state = prepare(env)
    assert state["status"] == status
    assert len(env.provider.calls) == 1
    assert not result.findings
    assert "no account" not in state["detail"].lower()


@pytest.mark.parametrize(
    "value",
    [
        "example.test",
        "sc-domain:example.test",
        "ftp://example.test/",
        "https://u:p@example.test/",
        SITE + "?apikey=secret",
        SITE + "#secret",
        SITE + "\n",
        SITE + "person@example.test",
    ],
)
def test_resource_rejects_secrets_and_non_site_identifiers(value):
    with pytest.raises(ValueError):
        bing.normalize_resource(value)


def test_customer_isolation_requires_force_reason_and_preserves_history(env):
    bind(env)
    before = copy.deepcopy(current(env))
    with pytest.raises(ValueError):
        service.set_binding(env.root, "other", "bing_wmt", "different-ref", SITE)
    with pytest.raises(ValueError):
        service.set_binding(env.root, "other", "bing_wmt", "different-ref", SITE, force=True)
    assert current(env) == before
    assert not env.provider.calls
    service.set_binding(
        env.root,
        "other",
        "bing_wmt",
        "different-ref",
        SITE,
        force=True,
        reason="Authorised shared site",
    )
    other = store.load(env.root, "other")
    assert other is not None
    assert other["connector_history"][-1]["reason"] == "Authorised shared site"
    assert other["connector_history"][-1]["other_customers"] == ["synthetic"]


def test_success_collects_only_bound_site_with_evidence_and_no_places(env):
    bind(env)
    result, state = prepare(env)
    assert state["status"] == "connected"
    assert {e["endpoint"] for e in state["coverage"]} == set(bing.METHODS)
    assert len(env.provider.calls) == 9
    assert not result.findings
    for evidence in result.evidence:
        record = evidence.data
        assert record["connector"] == "bing_wmt" and record["resource_id"] == SITE
        assert record["collected_at"] and record["period"] == DATES
        assert record["endpoint"] in {bing.BASE + m for m in bing.METHODS.values()}
        assert record["coverage"]["truncated"] is False
    assert endpoint(result, "traffic")["data"]["totals"] == {"Clicks": 5, "Impressions": 100}
    assert endpoint(result, "roles")["data"]["role_counts"] == {"ReadOnly": 1}
    assert "other.test" not in result.model_dump_json()
    assert "bing_places" not in json.dumps(state)


@pytest.mark.parametrize(
    "code,status",
    [
        (3, "credential_expired"),
        (6, "permission_required"),
        (13, "permission_required"),
        (14, "permission_required"),
        (10, "not_found"),
        (11, "not_found"),
        (16, "provider_unavailable"),
        (7, "failed"),
        (8, "failed"),
        (999, "failed"),
    ],
)
def test_documented_http400_faults_are_not_empty_data(env, code, status):
    bind(env)
    env.provider.overrides["GetUserSites"] = httpx.Response(
        400, json={"ErrorCode": code, "Message": SECRET}
    )
    result, state = prepare(env)
    assert state["status"] == status
    assert not result.findings and len(env.provider.calls) == 1
    assert SECRET not in json.dumps(state) + result.model_dump_json()
    if status == "permission_required":
        assert "ReadOnly" in state["detail"]
    if code == 3:
        assert "--env-var BING_WMT_API_KEY" in state["detail"]
    if code == 16:
        assert endpoint(result, "site")["data"]["availability"] == "unsupported"


@pytest.mark.parametrize("status", [401, 403, 404, 405, 410, 501, 429, 503])
def test_per_endpoint_availability_preserves_success(env, status):
    bind(env)
    env.provider.overrides["GetCrawlIssues"] = httpx.Response(status, json={"message": SECRET})
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    assert endpoint(result, "traffic")["data"]["totals"]["Clicks"] == 5
    assert endpoint(result, "feeds")["status"] == "connected"
    failed = endpoint(result, "crawl_issues")
    expected = (
        "credential_expired"
        if status == 401
        else "permission_required"
        if status == 403
        else "rate_limited"
        if status == 429
        else "provider_unavailable"
    )
    assert failed["status"] == expected
    if status in {405, 410, 501}:
        assert failed["data"]["availability"] == "unsupported"
    assert "crawl_issues" in state["detail"]
    assert not result.findings


@pytest.mark.parametrize(
    "status,code", [(429, None), (500, None), (503, None), (400, 1), (400, 2), (400, 4), (400, 5)]
)
def test_retries_backoff_and_recovery(env, monkeypatch, status, code):
    delays = []
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", delays.append)
    attempts = 0

    def retry(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                status, headers={"Retry-After": "1"}, json={"ErrorCode": code, "Message": SECRET}
            )
        return httpx.Response(200, json={"d": env.provider.payloads["GetUserSites"]})

    env.provider.overrides["GetUserSites"] = retry
    bind(env)
    assert prepare(env)[1]["status"] == "connected"
    assert attempts == 3 and delays == [1, 1]


def test_timeout_bounded_and_no_exception_leak(env):
    def timeout(request):
        assert request.extensions["timeout"]["read"] <= 10
        raise httpx.ReadTimeout(SECRET)

    env.provider.overrides["GetUserSites"] = timeout
    bind(env)
    result, state = prepare(env)
    assert state["status"] == "provider_unavailable" and len(env.provider.calls) == 3
    assert SECRET not in result.model_dump_json() + json.dumps(state)


def test_empty_data_is_success_not_no_activity_or_secure(env):
    bind(env)
    for method in bing.METHODS.values():
        if method != "GetUserSites":
            env.provider.payloads[method] = []
    result, state = prepare(env)
    assert state["status"] == "connected"
    assert [f.check_key for f in result.findings] == ["bing_wmt:no-sitemap"]
    assert not any(
        o.check_id in {"bing_wmt:crawl-errors", "bing_wmt:robots-blocked"} for o in result.outcomes
    )
    assert "does not mean" in result.findings[0].detail


def test_period_filters_provider_dates_and_preserves_snapshot_semantics(env):
    bind(env)
    env.provider.payloads["GetRankAndTrafficStats"] += [
        {"Date": "2026-08-31", "Clicks": 1000, "Impressions": 5000},
        {"Date": "2026-09-29", "Clicks": 1000, "Impressions": 5000},
    ]
    result, state = prepare(env)
    assert endpoint(result, "traffic")["coverage"]["rows_returned"] == 1
    assert endpoint(result, "traffic")["data"]["totals"]["Clicks"] == 5
    assert "weekly" in endpoint(result, "queries")["data"]["scope"]
    assert "snapshot" in endpoint(result, "feeds")["data"]["scope"]
    assert state["period"] == DATES


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/Date(1316156400000-0700)/", "2011-09-16"),
        ("/Date(1316156400000)/", "2011-09-16"),
        ("/Date(1316156400000-0800)/", "2011-09-15"),
        ("2011-09-16T00:00:00-07:00", "2011-09-16"),
    ],
)
def test_microsoft_dates(value, expected):
    assert bing.bing_date(value) == date.fromisoformat(expected)


def test_non_paginated_methods_cap_rows_and_do_not_invent_paging(env):
    bind(env)
    env.provider.payloads["GetFeeds"] *= 501
    env.provider.payloads["GetQueryStats"] *= 501
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    for key in ("feeds", "queries"):
        assert endpoint(result, key)["coverage"] == {"rows_returned": 500, "truncated": True}
    assert len(env.provider.calls) == 9
    assert all("page" not in str(r.url.params).lower() for r in env.provider.calls)
    assert not any(f.check_key == "bing_wmt:no-sitemap" for f in result.findings)


def test_resource_listing_cap_never_treats_hidden_site_as_missing(env):
    env.provider.payloads["GetUserSites"] = [
        {"Url": f"https://site{i}.test/", "IsVerified": True} for i in range(500)
    ] + [{"Url": SITE, "IsVerified": True}]
    resources = service.list_resources("bing_wmt", "bing-synthetic")
    assert resources.rows == 500 and resources.truncated
    bind(env)
    assert prepare(env)[1]["status"] == "provider_unavailable"
    assert len(env.provider.calls) == 2


@pytest.mark.parametrize(
    "body", [{}, {"d": None}, {"d": {}}, {"d": [None]}, {"d": [], "nextPageToken": "unexpected"}]
)
def test_malformed_response_is_failed_not_empty(env, body):
    bind(env)
    env.provider.overrides["GetFeeds"] = httpx.Response(200, json=body)
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    assert endpoint(result, "feeds")["status"] == "failed"
    assert not result.findings


def test_partial_rows_survive_schema_failure(env):
    bind(env)
    env.provider.payloads["GetFeeds"] += [{"Url": SITE + "bad.xml", "Status": None}]
    result, state = prepare(env)
    feeds = endpoint(result, "feeds")
    assert state["status"] == "connected_partial"
    assert feeds["status"] == "failed"
    assert feeds["coverage"] == {"rows_returned": 1, "truncated": True}
    assert feeds["data"]["rows"][0]["status"] == "Success"
    assert not result.findings


WRITES = (
    "AddBlockedUrl",
    "AddConnectedPage",
    "AddCountryRegionSettings",
    "AddDeepLinkBlock",
    "AddPagePreviewBlock",
    "AddQueryParameter",
    "AddSite",
    "AddSiteRoles",
    "FetchUrl",
    "EnableDisableQueryParameter",
    "RemoveBlockedUrl",
    "RemoveCountryRegionSettings",
    "RemoveDeepLinkBlock",
    "RemoveFeed",
    "RemovePagePreviewBlock",
    "RemoveQueryParameter",
    "RemoveSite",
    "RemoveSiteRole",
    "SaveCrawlSettings",
    "SubmitContent",
    "SubmitFeed",
    "SubmitSiteMove",
    "SubmitUrl",
    "SubmitUrlBatch",
    "UpdateDeepLink",
    "VerifySite",
)


@pytest.mark.parametrize(
    "method,url",
    [
        *((verb, bing.BASE + name) for name in WRITES for verb in ("GET", "POST")),
        *((verb, bing.BASE + "GetUserSites") for verb in ("POST", "PUT", "DELETE", "PATCH")),
        ("GET", bing.BASE + "GetUserSites?method=SubmitUrl"),
        ("GET", "https://evil.test/GetUserSites"),
        ("GET", bing.BASE + "GetUserSites/SubmitUrl"),
    ],
)
def test_read_allowlist_refuses_writes_and_unlisted_routes(method, url):
    def forbidden(request):
        pytest.fail("Forbidden request reached transport")

    client = ReadClient(bing.CONNECTOR, AuthMaterial(), transport=httpx.MockTransport(forbidden))
    try:
        with pytest.raises(ConnectorError, match="allowlist refused"):
            client.request(method, url)
    finally:
        client.close()


def test_redirect_not_followed(env):
    env.provider.overrides["GetUserSites"] = httpx.Response(
        302, headers={"Location": "https://evil.test/"}
    )
    bind(env)
    assert prepare(env)[1]["status"] == "failed"
    assert len(env.provider.calls) == 1


@contextmanager
def ui_server(root):
    with LocalServer(root, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=server.origin, trust_env=False) as client:
                yield server, client
        finally:
            server.shutdown()
            thread.join(5)


@pytest.mark.parametrize(
    "status",
    [
        "connected",
        "connected_partial",
        "credential_missing",
        "credential_expired",
        "permission_required",
        "not_found",
        "provider_unavailable",
        "failed",
    ],
)
def test_cli_ui_report_register_status_consistency(env, capsys, status):
    bind(env)
    if status == "credential_missing":
        (env.private / "credentials.env").unlink()
    elif status == "connected_partial":
        env.provider.overrides["GetSiteRoles"] = httpx.Response(501)
    elif status != "connected":
        env.provider.overrides["GetUserSites"] = httpx.Response(
            400,
            json={
                "ErrorCode": {
                    "credential_expired": 3,
                    "permission_required": 14,
                    "not_found": 11,
                    "provider_unavailable": 16,
                    "failed": 8,
                }[status]
            },
        )
    state = execute(env)
    assert state["status"] == status
    if status in {"credential_missing", "credential_expired"}:
        row = next(r for r in service.rows(current(env)) if r["key"] == "bing_wmt")
        assert "--env-var BING_WMT_API_KEY" in row["next_action"]
        assert "--client-secrets" not in row["next_action"]
    cli.main(["connectors", "--customer", "synthetic"])
    assert status in capsys.readouterr().out
    with ui_server(env.root) as (server, client):
        assert status in client.get(server.url(customer="synthetic")).text
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text().replace("\\_", "_")
    assert status in report
    assert "## 7. Connected business-system findings" in report
    assert all(
        e["integration"] == status
        for e in current(env)["systems_register"]["entries"]
        if e["platform"] == "bing_wmt"
    )


def test_ui_post_bind_test_collect_uses_same_connector(env):
    with ui_server(env.root) as (server, client):
        payload = {
            "customer": "synthetic",
            "connector": "bing_wmt",
            "csrf": server.csrf,
            "credential_ref": "bing-synthetic",
            "resource": SITE,
        }
        for action in ("bind", "test", "collect"):
            response = client.post(
                server.url("/connect"),
                data={**payload, "action": action},
                headers={"Origin": server.origin},
            )
            assert response.status_code == 303
        assert current(env)["connectors"]["bing_wmt"]["status"] == "connected"


def test_redaction_all_sinks_and_aggregate_only_ai(env, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    bind(env)
    env.provider.payloads["GetQueryStats"][0]["Query"] = "person@example.test " + SECRET
    env.provider.payloads["GetPageStats"][0]["Query"] = (
        "https://user:password@example.test/person%40example.test?apikey=" + SECRET
    )
    execute(env)
    snapshot = current(env)
    cli.main(["connectors", "--customer", "synthetic"])
    render_customer(env.root, "synthetic", {"md"})
    with ui_server(env.root) as (server, client):
        html = client.get(server.url(customer="synthetic")).text
    dumped = json.dumps(snapshot) + html + (env.root / "synthetic/report.md").read_text()
    dumped += capsys.readouterr().out + caplog.text
    for private in (
        SECRET,
        "verification-secret",
        "delegation-secret",
        "owner@example.test",
        "person@example.test",
        "person%40example.test",
        "user:password",
    ):
        assert private not in dumped
    compact = ai.compact_evidence(
        [Evidence.model_validate(e) for e in snapshot["evidence"] if e["source"] == "bing_wmt"]
    )
    assert SITE not in json.dumps(compact)
    assert "query_id" not in json.dumps(compact)
    assert "rows_returned" in json.dumps(compact)


@pytest.mark.parametrize(
    "response_kind", ["fault", "forbidden", "redirect", "timeout", "exception", "echo"]
)
def test_query_key_is_private_in_errors_urls_and_every_output(
    env, monkeypatch, capsys, caplog, response_kind
):
    key = "synthetic+bing/key=value%private"
    env.provider.key = key
    monkeypatch.setenv("BING_WMT_API_KEY", key)
    bind(env)
    caplog.set_level(logging.DEBUG)

    def respond(request):
        url = str(request.url)
        assert request.url.params["apikey"] == key
        assert quote(key, safe="") in url
        logging.getLogger("httpcore").debug("Synthetic provider request %s", url)
        if response_kind == "fault":
            return httpx.Response(400, json={"ErrorCode": 14, "Message": url})
        if response_kind == "forbidden":
            return httpx.Response(403, json={"error": {"message": url}})
        if response_kind == "redirect":
            return httpx.Response(302, headers={"Location": url})
        if response_kind == "timeout":
            raise httpx.ReadTimeout(url)
        if response_kind == "exception":
            raise RuntimeError(url)
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Url": url,
                        "Status": "Unrecognised status at " + url,
                        "UrlCount": 1,
                    }
                ]
            },
        )

    env.provider.overrides["GetFeeds"] = respond
    state = execute(env)
    assert state["status"] == ("connected" if response_kind == "echo" else "connected_partial")
    snapshot = current(env)
    cli.main(["connectors", "--customer", "synthetic"])
    render_customer(env.root, "synthetic", {"md"})
    with ui_server(env.root) as (server, client):
        html = client.get(server.url(customer="synthetic")).text
    output = json.dumps(snapshot) + html + (env.root / "synthetic/report.md").read_text()
    output += capsys.readouterr().out + caplog.text
    assert key not in output and quote(key, safe="") not in output
    assert "Synthetic provider request" not in caplog.text
    for item in snapshot["evidence"]:
        if item["source"] == "bing_wmt":
            assert "?" not in item["data"]["endpoint"]
    if response_kind == "echo":
        feed = next(
            e["data"]["data"]["rows"][0]
            for e in snapshot["evidence"]
            if e["source"] == "bing_wmt" and e["data"]["operation"] == "feeds"
        )
        assert "[redacted]" in feed["status"]
        assert "?" not in feed["url"]


def test_findings_lifecycle_complete_pass_resolves_failure_unavailable_does_not(env):
    bind(env)
    env.provider.payloads["GetFeeds"] = []
    execute(env)
    finding = next(f for f in current(env)["findings"] if f["check_key"] == "bing_wmt:no-sitemap")
    assert finding["confidence"] == "confirmed"
    env.provider.overrides["GetFeeds"] = httpx.Response(403)
    execute(env)
    assert (
        next(f for f in current(env)["findings"] if f["id"] == finding["id"])["lifecycle_status"]
        != "resolved"
    )
    del env.provider.overrides["GetFeeds"]
    env.provider.payloads["GetFeeds"] = Provider().payloads["GetFeeds"]
    execute(env)
    assert (
        next(f for f in current(env)["findings"] if f["id"] == finding["id"])["lifecycle_status"]
        == "resolved"
    )


def test_crawl_sitemap_and_blocked_findings_and_public_references(env):
    bind(env)
    env.provider.payloads["GetCrawlStats"][0].update(CrawlErrors=2, BlockedByRobotsTxt=1)
    env.provider.payloads["GetCrawlIssues"] = [{"Issues": 16 | 128, "HttpCode": 0, "Url": SITE}]
    env.provider.payloads["GetFeeds"][0]["Status"] = "Fetch error"
    env.provider.payloads["GetBlockedUrls"] = [{"Url": SITE + "private"}]
    public = Evidence(
        id="public-robots", source="site_checks", url=SITE, description="Robots", data={}
    )
    result, state = prepare(
        env,
        public_evidence=[public],
        public_findings=[
            {
                "id": "existing-robots",
                "check_key": "site:robots",
                "site": "example.test",
                "evidence_ids": [public.id],
            }
        ],
    )
    assert state["status"] == "connected"
    keys = {f.check_key for f in result.findings}
    assert keys == {
        "bing_wmt:crawl-errors",
        "bing_wmt:crawl-issues",
        "bing_wmt:sitemap-errors",
        "bing_wmt:blocked-urls",
    }
    links = endpoint(result, "crawl")["data"]["public_references"][0]
    assert links["related_finding_ids"] == ["existing-robots"]
    assert links["evidence_ids"] == [public.id]


def test_public_finding_reference_renders_in_section_seven_without_duplicate(env):
    bind(env)
    public = Evidence(
        id="ev-site_checks-r1-001",
        source="site_checks",
        url=SITE,
        description="Public robots rule",
        data={},
    )
    finding = Finding(
        id="public-robots",
        check_key="site:robots",
        title="Public robots finding",
        category="seo",
        detail="Public robots block",
        recommendation="Check intent",
        site="example.test",
        component=SITE,
        evidence_ids=[public.id],
    )
    snapshot = current(env)
    snapshot["evidence"].append(public.model_dump())
    snapshot["findings"].append(finding.model_dump())
    store.save(env.root, "synthetic", snapshot)
    env.provider.payloads["GetCrawlStats"][0]["BlockedByRobotsTxt"] = 1
    execute(env)
    execute(env)  # Reuse the public finding even after its lifecycle becomes not_rechecked.
    saved = current(env)
    assert not any(f["check_key"] == "bing_wmt:robots-blocked" for f in saved["findings"])
    render_customer(env.root, "synthetic", {"md"})
    section = (env.root / "synthetic/report.md").read_text().split("## 7.")[1]
    assert "See existing public findings: public-robots" in section
    assert public.id in section


def gsc_evidence(**changes):
    data = {
        "connector": "gsc",
        "operation": "page",
        "resource_id": SITE,
        "period": DATES,
        "status": "connected",
        "coverage": {"truncated": False, "rows_returned": 1},
        "collected_at": "2026-10-01T00:00:00Z",
        "data": {"rows": [{"value": SITE + "contact", "clicks": 20, "impressions": 200}]},
    }
    data.update(changes)
    return Evidence(id="ev-gsc-r1-page", source="gsc", url="", description="GSC page", data=data)


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"resource_id": "https://other.test/"},
        {"period": {"start": "2026-08-01", "end": "2026-08-28"}},
        {"status": "permission_required"},
        {"coverage": {"truncated": True}},
    ],
)
def test_gsc_comparison_is_scoped_labelled_and_cited(env, change):
    bind(env)
    other = gsc_evidence(**change)
    result, _ = prepare(env, public_evidence=[other])
    comparisons = [f for f in result.findings if f.check_key == "bing_wmt:gsc-comparison"]
    assert bool(comparisons) == (not change)
    if comparisons:
        assert comparisons[0].confidence == "needs_verification"
        assert other.id in comparisons[0].evidence_ids
        assert "not a like-for-like" in comparisons[0].detail


def test_gsc_latest_failure_never_falls_back_to_old_success(env):
    bind(env)
    old = gsc_evidence()
    new = gsc_evidence(status="permission_required", collected_at="2026-10-02T00:00:00Z")
    new.id = "ev-gsc-r2-page"
    result, _ = prepare(env, public_evidence=[old, new])
    assert not result.findings


@pytest.mark.parametrize("missing", ["bing", "gsc", "both"])
def test_gsc_comparison_requires_both_sources_without_absence_finding(env, missing):
    bind(env)
    if missing in {"bing", "both"}:
        env.provider.payloads["GetPageStats"] = []
    incoming = [] if missing in {"gsc", "both"} else [gsc_evidence()]
    result, state = prepare(env, public_evidence=incoming)
    assert state["status"] == "connected" and not result.findings
    assert "gsc_comparison" not in endpoint(result, "pages")["data"]


@pytest.mark.parametrize(
    "resource",
    [
        "https://other.test/",
        "http://example.test/",
        "https://example.test/section/",
        "sc-domain:test",
    ],
)
def test_gsc_comparison_rejects_unrelated_site_evidence(env, resource):
    bind(env)
    result, _ = prepare(env, public_evidence=[gsc_evidence(resource_id=resource)])
    assert not result.findings


def test_gsc_latest_failed_collection_without_page_does_not_reuse_old_page(env):
    bind(env)
    old = gsc_evidence()
    snapshot = current(env)
    snapshot["evidence"].append(old.model_dump())
    latest = gsc_evidence(operation="site", status="permission_required")
    latest.id = "ev-gsc-r2-001"
    snapshot["evidence"].append(latest.model_dump())
    store.save(env.root, "synthetic", snapshot)
    result, state = prepare(env)
    assert state["status"] == "connected" and not result.findings


def test_gsc_comparison_never_uses_another_customer_store(env):
    bind(env)
    other_customer = store.new_store("other", "Other synthetic customer")
    other_customer["evidence"].append(gsc_evidence().model_dump())
    store.save(env.root, "other", other_customer)
    result, _ = prepare(env)
    assert not result.findings


@pytest.mark.parametrize("legacy", [False, True])
def test_gsc_domain_property_filters_other_sites_and_persists_citations(env, legacy):
    bind(env)
    other = gsc_evidence(resource_id="sc-domain:example.test")
    other.data["data"]["rows"] += [
        {"value": "https://other.example.test/contact", "clicks": 900, "impressions": 9999},
        {"value": "http://example.test/contact", "clicks": 900, "impressions": 9999},
    ]
    if legacy:
        other.data = {
            "property": "sc-domain:example.test",
            "start_date": DATES["start"],
            "end_date": DATES["end"],
            "page": {"rows": [{**r, "keys": [r["value"]]} for r in other.data["data"]["rows"]]},
        }
    snapshot = current(env)
    snapshot["evidence"].append(other.model_dump())
    store.save(env.root, "synthetic", snapshot)
    execute(env)
    saved = current(env)
    finding = next(f for f in saved["findings"] if f["check_key"] == "bing_wmt:gsc-comparison")
    assert other.id in finding["evidence_ids"]
    assert "20 clicks and 200 impressions" in finding["detail"]
    assert "9999" not in finding["detail"]
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text()
    assert "Bing and GSC page traffic comparison" in report.split("## 7.")[1]


@pytest.mark.parametrize("key", [key for key in bing.METHODS if key != "site"])
def test_each_collection_endpoint_probes_availability_independently(env, key):
    bind(env)
    env.provider.overrides[bing.METHODS[key]] = httpx.Response(501)
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    assert endpoint(result, key)["data"]["availability"] == "unsupported"
    assert len(env.provider.calls) == 9
    assert all(e["status"] == "connected" for e in state["coverage"] if e["endpoint"] != key)


@pytest.mark.parametrize(
    "reason,status",
    [("SERVICE_DISABLED", "api_not_enabled"), ("API_NOT_APPROVED", "approval_required")],
)
def test_explicit_provider_api_permission_codes_use_framework_status(env, capsys, reason, status):
    bind(env)
    env.provider.overrides["GetUserSites"] = httpx.Response(
        403, json={"error": {"details": [{"reason": reason}]}}
    )
    assert execute(env)["status"] == status
    cli.main(["connectors", "--customer", "synthetic"])
    assert status in capsys.readouterr().out
    with ui_server(env.root) as (server, client):
        assert status in client.get(server.url(customer="synthetic")).text
    render_customer(env.root, "synthetic", {"md"})
    assert status in (env.root / "synthetic/report.md").read_text().replace("\\_", "_")


@pytest.mark.parametrize(
    "status", ["Pending", "Unknown", "No errors", "Previous failure resolved", ""]
)
def test_pending_unknown_feed_status_does_not_resolve_error(env, status):
    bind(env)
    env.provider.payloads["GetFeeds"][0]["Status"] = "Error"
    execute(env)
    finding = next(
        f for f in current(env)["findings"] if f["check_key"] == "bing_wmt:sitemap-errors"
    )
    env.provider.payloads["GetFeeds"][0]["Status"] = status
    execute(env)
    assert (
        next(f for f in current(env)["findings"] if f["id"] == finding["id"])["lifecycle_status"]
        != "resolved"
    )


@pytest.mark.parametrize("fault", [{}, {"ErrorCode": "3"}, {"ErrorCode": True}, [3]])
def test_malformed_faults_do_not_claim_invalid_key_or_permission(env, fault):
    bind(env)
    env.provider.overrides["GetUserSites"] = httpx.Response(400, json=fault)
    assert prepare(env)[1]["status"] == "failed"


@pytest.mark.parametrize("value", [-1, True, "10", float("inf"), float("nan")])
def test_invalid_metrics_are_software_failures(value):
    with pytest.raises(ValueError):
        bing.number(value)


def test_default_transport_factory_handles_bing_faults(env, monkeypatch):
    # Exercise the production factory path, rather than passing an already wrapped transport.
    monkeypatch.setattr(bing.httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(env.provider))
    material = bing.authenticate(bing.CONNECTOR, "bing-synthetic", directory=env.private)
    env.provider.overrides["GetUserSites"] = httpx.Response(400, json={"ErrorCode": 3})
    client = ReadClient(bing.CONNECTOR, material)
    try:
        ctx = ConnectorContext(client, SITE, Period.make())
        assert bing.test(ctx).status == "credential_expired"
    finally:
        client.close()


def test_read_request_budget_and_deadline():
    provider = Provider()
    for options in ({"max_requests": 0}, {"seconds": 0}):
        client = ReadClient(
            bing.CONNECTOR,
            AuthMaterial(private_data=SECRET),
            transport=httpx.MockTransport(provider),
            **options,
        )
        try:
            with pytest.raises(ConnectorError):
                client.request("GET", bing.BASE + "GetUserSites")
        finally:
            client.close()
    assert not provider.calls


@pytest.mark.parametrize("status,size", [(200, 2_000_001), (400, 64_001)])
def test_response_and_fault_size_caps(env, status, size):
    bind(env)
    env.provider.overrides["GetUserSites"] = httpx.Response(status, content=b"x" * size)
    assert prepare(env)[1]["status"] == "failed"


def test_direct_collection_uses_configured_cap(env):
    material = bing.authenticate(bing.CONNECTOR, "bing-synthetic", directory=env.private)
    client = ReadClient(bing.CONNECTOR, material, transport=httpx.MockTransport(env.provider))
    try:
        ctx = ConnectorContext(
            client,
            SITE,
            Period.make(start=DATES["start"], end=DATES["end"]),
            max_pages=1,
            page_size=1,
        )
        env.provider.payloads["GetFeeds"] *= 2
        bing.collect(ctx)
        feeds = next(e for e in ctx.endpoints if e.key == "feeds")
        assert feeds.rows == 1 and feeds.truncated
    finally:
        client.close()
