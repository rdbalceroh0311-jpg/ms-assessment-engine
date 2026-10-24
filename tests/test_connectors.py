"""Synthetic HTTP regressions for the connector contract; no live provider access."""

import json
import logging
import stat
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from google.oauth2.credentials import Credentials

from ams_assess import ai, cli, register, store
from ams_assess.connectors import auth, ga4, service
from ams_assess.connectors.core import AuthMaterial, ConnectorError, Period
from ams_assess.connectors.http import ReadClient
from ams_assess.models import Evidence
from ams_assess.report import render_customer
from ams_assess.ui import LocalServer

ACCESS = "synthetic-access-value-never-print"
REFRESH = "synthetic-refresh-value-never-print"
CLIENT_SECRET = "synthetic-client-secret-never-print"
RESOURCE = "properties/123456"


class Provider:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.override = lambda request: None
        self.event_count = 4
        self.configured = ["generate_lead", "form_submit"]
        self.recorded = {"generate_lead": 4}
        self.report_rows = None

    def __call__(self, request):
        self.calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + ACCESS
        response = self.override(request)
        if response is not None:
            return response
        path = request.url.path
        if path.endswith("/accountSummaries"):
            return httpx.Response(
                200,
                json={
                    "accountSummaries": [
                        {
                            "propertySummaries": [
                                {"property": RESOURCE, "displayName": "Synthetic property"},
                                {"property": "properties/654321", "displayName": "Unselected"},
                            ]
                        }
                    ]
                },
            )
        if path == "/v1beta/" + RESOURCE:
            return httpx.Response(
                200,
                json={
                    "name": RESOURCE,
                    "timeZone": "Australia/Sydney",
                    "currencyCode": "AUD",
                    "industryCategory": "OTHER",
                    "createTime": "2020-01-01T00:00:00Z",
                },
            )
        if path.endswith("/dataStreams"):
            return httpx.Response(
                200,
                json={
                    "dataStreams": [
                        {
                            "name": RESOURCE + "/dataStreams/7",
                            "type": "WEB_DATA_STREAM",
                            "webStreamData": {
                                "measurementId": "G-SYNTHETIC1",
                                "defaultUri": "https://example.test",
                            },
                        },
                    ]
                },
            )
        if path.endswith("/keyEvents"):
            return httpx.Response(
                200, json={"keyEvents": [{"eventName": n} for n in self.configured]}
            )
        if path.endswith("/customDimensions"):
            return httpx.Response(200, json={"customDimensions": [{"name": "unused"}]})
        if path.endswith("/googleAdsLinks"):
            return httpx.Response(200, json={"googleAdsLinks": []})
        if path.endswith("/dataRetentionSettings"):
            return httpx.Response(
                200,
                json={"eventDataRetention": "FOURTEEN_MONTHS", "resetUserDataOnNewActivity": True},
            )
        assert path.endswith(RESOURCE + ":runReport")
        body = json.loads(request.content)
        assert body["returnPropertyQuota"] is True
        dims = [d["name"] for d in body["dimensions"]]
        metrics = [m["name"] for m in body["metrics"]]
        values = {
            "date": "20261001",
            "sessionDefaultChannelGroup": "Organic Search",
            "sessionSourceMedium": "google / organic",
            "landingPagePlusQueryString": "/contact",
        }
        if dims == ["eventName"]:
            names = list(self.recorded)
            rows = [
                {
                    "dimensionValues": [{"value": n}],
                    "metricValues": [{"value": str(self.recorded[n])} for _ in metrics],
                }
                for n in names
            ]
        else:
            rows = [
                {
                    "dimensionValues": [{"value": values[d]} for d in dims],
                    "metricValues": [
                        {"value": str(self.event_count if m == "eventCount" else 2)}
                        for m in metrics
                    ],
                }
            ]
        if self.report_rows is not None:
            rows = self.report_rows
        return httpx.Response(
            200,
            json={
                "rows": rows,
                "rowCount": len(rows),
                "propertyQuota": {"tokensPerDay": {"remaining": 100}},
            },
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    root, private = tmp_path / "customers", tmp_path / "private"
    private.mkdir(mode=0o700)
    token = private / "ga4-synthetic-token.json"
    auth.atomic_private(
        token,
        {
            "token": ACCESS,
            "refresh_token": REFRESH,
            "client_id": "synthetic.apps.googleusercontent.com",
            "client_secret": CLIENT_SECRET,
            "scopes": [ga4.SCOPE],
            "type": "authorized_user",
            "expiry": (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    auth.register_reference("synthetic", ga4.CONNECTOR, file=token, directory=private)
    monkeypatch.setattr(service, "config_dir", lambda: private)
    monkeypatch.setattr(cli, "data_root", lambda: root)
    provider = Provider()
    real_client = service.ReadClient
    monkeypatch.setattr(
        service,
        "ReadClient",
        lambda *a, transport=None, **kw: real_client(
            *a, transport=httpx.MockTransport(provider), **kw
        ),
    )
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", lambda seconds: None)
    store.set_profile(
        root, "synthetic", {"name": "Synthetic Customer", "websites": ["https://example.test"]}
    )
    return SimpleNamespace(root=root, private=private, token=token, provider=provider)


def bind(env):
    return service.set_binding(env.root, "synthetic", "ga4", "synthetic", RESOURCE)


def collect(env, **kwargs):
    return service.execute(env.root, "synthetic", "ga4", collect=True, **kwargs)


def current(env):
    return store.load(env.root, "synthetic")


def test_scope_specific_auth_never_widens_gsc(env, monkeypatch, capsys):
    client = env.private / "desktop.json"
    auth.atomic_private(
        client,
        {
            "installed": {
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
    )
    gsc = env.private / "gsc-token.json"
    gsc.write_text("synthetic-existing-gsc")
    seen = {}

    class Flow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            seen["scopes"] = scopes
            return Flow()

        def run_local_server(self, **kwargs):
            seen.update(kwargs)
            return Credentials(
                ACCESS,
                refresh_token=REFRESH,
                token_uri="https://oauth2.googleapis.com/token",
                client_id="synthetic",
                client_secret=CLIENT_SECRET,
                scopes=[ga4.SCOPE],
            )

    monkeypatch.setattr(auth, "InstalledAppFlow", Flow)
    auth.google_authorize(ga4.CONNECTOR, "new-ref", client, directory=env.private)
    assert seen["scopes"] == [ga4.SCOPE]
    assert seen["include_granted_scopes"] == "false"
    assert seen["authorization_prompt_message"] == ""
    assert seen["host"] == "127.0.0.1" and seen["timeout_seconds"] == 180
    assert gsc.read_text() == "synthetic-existing-gsc"
    assert stat.S_IMODE(env.private.stat().st_mode) == 0o700
    for name in ["connectors.json", "ga4-new-ref-token.json"]:
        assert stat.S_IMODE((env.private / name).stat().st_mode) == 0o600
    assert ACCESS not in capsys.readouterr().out
    assert "new-ref" in auth.references(env.private)


def test_auth_rejects_broader_or_missing_scope(env):
    data = json.loads(env.token.read_text())
    data["scopes"] = [ga4.SCOPE, "https://www.googleapis.com/auth/analytics.edit"]
    auth.atomic_private(env.token, data)
    bind(env)
    state = collect(env)
    assert state["status"] == "permission_required"
    assert ga4.SCOPE in state["detail"]
    assert not env.provider.calls


@pytest.mark.parametrize("revoked", [False, True])
def test_expired_token_refresh_uses_mocked_oauth_http(env, monkeypatch, revoked):
    data = json.loads(env.token.read_text())
    data["expiry"] = "2020-01-01T00:00:00Z"
    auth.atomic_private(env.token, data)
    requests = []

    def oauth(request):
        requests.append(request)
        assert request.url == "https://oauth2.googleapis.com/token"
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(
            400 if revoked else 200,
            json={"error": "invalid_grant"}
            if revoked
            else {"access_token": ACCESS, "expires_in": 3600, "scope": ga4.SCOPE},
        )

    def request(url, method, body, headers, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(oauth)) as client:
            response = client.request(method, url, content=body, headers=headers)
        return SimpleNamespace(
            status=response.status_code, data=response.content, headers=response.headers
        )

    monkeypatch.setattr(auth, "BoundedRequest", lambda: request)
    bind(env)
    state = collect(env)
    assert len(requests) == 1
    assert state["status"] == ("credential_expired" if revoked else "connected")
    if revoked:
        assert "re-authorise with ams-assess connect ga4 auth" in state["detail"]
        assert not env.provider.calls
    else:
        assert json.loads(env.token.read_text())["expiry"] != data["expiry"]


def test_listing_is_paginated_and_does_not_bind_or_read_properties(env):
    def paginate(request):
        assert request.url.path.endswith("/accountSummaries")
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "accountSummaries": [{"propertySummaries": [{"property": RESOURCE}]}],
                    "nextPageToken": "synthetic-next",
                },
            )
        assert request.url.params["pageToken"] == "synthetic-next"
        return httpx.Response(
            200,
            json={"accountSummaries": [{"propertySummaries": [{"property": "properties/654321"}]}]},
        )

    env.provider.override = paginate
    listing = service.list_resources("ga4", "synthetic")
    assert listing.rows == 2 and not listing.truncated
    assert not current(env).get("connectors")
    assert len(env.provider.calls) == 2
    bind(env)
    assert current(env)["connectors"]["ga4"]["status"] == "not_configured"
    assert len(env.provider.calls) == 2


def test_cross_customer_isolation_force_reason_and_history(env):
    bind(env)
    with pytest.raises(ValueError, match="different customer"):
        service.set_binding(env.root, "other", "ga4", "other-ref", "123456")
    with pytest.raises(ValueError, match="reason"):
        service.set_binding(env.root, "other", "ga4", "other-ref", RESOURCE, force=True)
    service.set_binding(
        env.root,
        "other",
        "ga4",
        "other-ref",
        RESOURCE,
        force=True,
        reason="Authorised shared reporting property",
    )
    other = store.load(env.root, "other")
    assert other is not None
    assert other["connector_history"][-1]["other_customers"] == ["synthetic"]
    assert other["connector_history"][-1]["reason"] == "Authorised shared reporting property"
    assert current(env)["connectors"]["ga4"]["credential_ref"] == "synthetic"
    assert not other["evidence"]


def test_success_has_all_endpoints_period_coverage_and_lifecycle(env):
    bind(env)
    state = collect(env)
    assert state["status"] == "connected"
    assert state["last_successful_test"] and state["last_successful_collection"]
    assert state["resource_id"] == RESOURCE
    assert state["period"] == Period.make(zone="Australia/Sydney").json()
    snapshot = current(env)
    assert set(e["endpoint"] for e in state["coverage"]) == {
        "property",
        "streams",
        "key_events",
        "custom_dimensions",
        "ads_links",
        "retention",
        "daily",
        "acquisition",
        "landing_pages",
        "events",
        "event_totals",
        "key_event_counts",
    }
    evidence = [e for e in snapshot["evidence"] if e["source"] == "ga4"]
    for e in evidence:
        assert e["data"]["resource_id"] == RESOURCE
        assert e["data"]["collected_at"]
        assert e["data"]["period"] == state["period"]
        assert "coverage" in e["data"] and "status" in e["data"]
        assert e["id"].startswith("ev-ga4-r")
    daily = next(
        e["data"]["data"]["rows"][0] for e in evidence if e["data"]["operation"] == "daily"
    )
    assert daily["averageEngagementTimeSecondsPerActiveUser"] == 1
    findings = snapshot["findings"]
    assert len(findings) == 1 and "form_submit" in findings[0]["title"]
    assert (
        findings[0]["basis"] if "basis" in findings[0] else findings[0]["confidence"] == "confirmed"
    )
    before_id = findings[0]["id"]
    env.provider.recorded["form_submit"] = 3
    collect(env)
    resolved = next(f for f in current(env)["findings"] if f["id"] == before_id)
    assert resolved["lifecycle_status"] == "resolved"
    assert resolved["resolution"]["evidence_ids"]
    del env.provider.recorded["form_submit"]
    collect(env)
    recurrent = next(f for f in current(env)["findings"] if f["id"] == before_id)
    assert recurrent["lifecycle_status"] == "current"
    assert "reopened" in [event["status"] for event in recurrent["events"]]


@pytest.mark.parametrize(
    "code,reason,status",
    [
        (403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "permission_required"),
        (403, "SERVICE_DISABLED", "api_not_enabled"),
        (403, "API_NOT_APPROVED", "approval_required"),
        (404, "", "not_found"),
        (401, "", "credential_expired"),
        (400, "", "failed"),
    ],
)
def test_explicit_connection_errors_never_claim_connected(env, code, reason, status):
    env.provider.override = lambda request: httpx.Response(
        code,
        json={
            "error": {
                "message": ACCESS,
                "details": [{"reason": reason}],
            }
        },
    )
    bind(env)
    state = collect(env)
    assert state["status"] == status
    assert not state.get("last_successful_test")
    assert ACCESS not in json.dumps(current(env))
    assert not current(env)["findings"]
    entries = [e for e in current(env)["systems_register"]["entries"] if e["platform"] == "ga4"]
    assert entries and all(e["integration"] == status for e in entries)


@pytest.mark.parametrize("code", [429, 500, 503])
def test_retry_then_partial_preserves_admin_and_does_not_resolve(env, code):
    bind(env)
    collect(env)
    old = current(env)["findings"][0]
    env.provider.calls.clear()
    env.provider.override = lambda r: (
        httpx.Response(code, json={"error": {"message": ACCESS}}) if r.method == "POST" else None
    )
    state = collect(env)
    assert state["status"] == "connected_partial"
    assert len([r for r in env.provider.calls if r.method == "POST"]) == 18
    assert any(e["endpoint"] == "streams" and e["status"] == "connected" for e in state["coverage"])
    assert any(
        e["endpoint"] == "daily" and e["status"] == "provider_unavailable"
        for e in state["coverage"]
    )
    finding = next(f for f in current(env)["findings"] if f["id"] == old["id"])
    assert finding["lifecycle_status"] == "not_rechecked"
    assert finding["resolution"] is None


def test_retry_recovers_and_timeouts_are_bounded(env):
    count = 0

    def intermittent(request):
        nonlocal count
        count += 1
        if count < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})

    env.provider.override = intermittent
    bind(env)
    assert service.execute(env.root, "synthetic", "ga4")["status"] == "connected"
    assert count == 3
    env.provider.calls.clear()

    def timeout(request):
        raise httpx.ReadTimeout(ACCESS)

    env.provider.override = timeout
    assert collect(env)["status"] == "provider_unavailable"
    assert len(env.provider.calls) == 3
    assert ACCESS not in json.dumps(current(env))
    env.provider.calls.clear()
    assert collect(env, seconds=-1)["status"] == "provider_unavailable"
    assert not env.provider.calls


def test_empty_data_is_confirmed_not_permission_error_and_enquiry_inferred(env):
    env.provider.report_rows = []
    env.provider.configured = ["click", "form_start"]
    bind(env)
    state = collect(env)
    assert state["status"] == "connected"
    findings = current(env)["findings"]
    assert (
        next(f for f in findings if f["check_key"] == "ga4:no-events")["confidence"] == "confirmed"
    )
    inferred = next(f for f in findings if f["check_key"] == "ga4:enquiry-config")
    assert inferred["confidence"] == "needs_verification"
    assert "not completed enquiries" in inferred["detail"]


def test_data_pagination_caps_and_preserves_a_failed_second_page(env):
    def paginated(request):
        if request.method != "POST":
            return None
        body = json.loads(request.content)
        if body["dimensions"] != [{"name": "eventName"}]:
            return None
        if body["offset"] != "0":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "rowCount": 200,
                "rows": [
                    {
                        "dimensionValues": [{"value": "generate_lead"}],
                        "metricValues": [{"value": "2"}, {"value": "2"}],
                    }
                ],
            },
        )

    env.provider.override = paginated
    bind(env)
    state = collect(env)
    assert state["status"] == "connected_partial"
    events = next(e for e in state["coverage"] if e["endpoint"] == "events")
    assert events["rows"] == 1 and events["status"] == "provider_unavailable"
    assert not any("configured but not recorded" in f["title"] for f in current(env)["findings"])


def test_resource_pagination_cap(env):
    env.provider.override = lambda r: httpx.Response(
        200,
        json={
            "accountSummaries": [{"propertySummaries": [{"property": RESOURCE}]}],
            "nextPageToken": str(len(env.provider.calls)),
        },
    )
    listing = service.list_resources("ga4", "synthetic")
    assert listing.rows == 5 and listing.truncated
    assert len(env.provider.calls) == 5


def test_quota_exhaustion_stops_further_data_reads(env):
    def quota(request):
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "rows": [],
                    "rowCount": 0,
                    "propertyQuota": {"tokensPerDay": {"remaining": 0}},
                },
            )

    env.provider.override = quota
    bind(env)
    state = collect(env)
    assert state["status"] == "connected_partial"
    assert len([r for r in env.provider.calls if r.method == "POST"]) == 1
    assert not any(f["check_key"] == "ga4:no-events" for f in current(env)["findings"])


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", ga4.ADMIN + RESOURCE),
        ("PATCH", ga4.ADMIN + RESOURCE),
        ("DELETE", ga4.ADMIN + RESOURCE),
        ("GET", ga4.ADMIN + RESOURCE + "/accessBindings"),
        ("GET", ga4.ADMIN + RESOURCE + "/dataStreams/7/measurementProtocolSecrets"),
        ("POST", ga4.DATA + RESOURCE + ":batchRunReports"),
        ("GET", ga4.ADMIN + RESOURCE + "?secret=hidden"),
        ("GET", "https://evil.test/v1beta/" + RESOURCE),
    ],
)
def test_read_allowlist_refuses_before_http(method, url):
    def forbidden(request):
        pytest.fail("Disallowed request reached transport")

    client = ReadClient(ga4.CONNECTOR, AuthMaterial(), transport=httpx.MockTransport(forbidden))
    try:
        with pytest.raises(ConnectorError, match="allowlist"):
            client.request(method, url)
    finally:
        client.close()


def test_no_redirect_following(env):
    env.provider.override = lambda r: httpx.Response(302, headers={"Location": "https://evil.test"})
    bind(env)
    assert collect(env)["status"] == "failed"
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


def test_cli_ui_report_same_status_and_post_controls(env, capsys):
    with ui_server(env.root) as (server, client):
        payload = {
            "customer": "synthetic",
            "connector": "ga4",
            "csrf": server.csrf,
            "credential_ref": "synthetic",
            "resource": RESOURCE,
        }
        for action in ("reference", "bind", "test", "collect"):
            response = client.post(
                server.url("/connect"),
                data={**payload, "action": action},
                headers={"Origin": server.origin},
            )
            assert response.status_code == 303
        state = current(env)["connectors"]["ga4"]
        assert state["status"] == "connected"
        assert cli.main(["connectors", "--customer", "synthetic"]) == 0
        output = capsys.readouterr().out
        page = client.get(server.url(customer="synthetic")).text
        render_customer(env.root, "synthetic", {"md"})
        report = (env.root / "synthetic/report.md").read_text()
        for text in [state["status"], RESOURCE, "daily: connected, 1 rows"]:
            assert text in output and text in page and text in report
        assert "## 7. Connected business-system findings" in report
        assert "Key event configured but not recorded" in report.split("## 7.")[1]
        for headers, data in [
            ({}, payload),
            ({"Origin": "http://evil.test"}, payload),
            ({"Origin": server.origin}, {**payload, "csrf": "wrong"}),
        ]:
            assert (
                client.post(
                    server.url("/connect"), headers=headers, data={**data, "action": "collect"}
                ).status_code
                == 403
            )
        assert client.get(server.url("/connect")).status_code == 404


def test_redaction_all_sinks_and_ai_aggregates_only(env, caplog, capsys):
    caplog.set_level(logging.DEBUG)

    def private(request):
        if request.url.path.endswith("/dataStreams"):
            return httpx.Response(
                200,
                json={
                    "dataStreams": [
                        {
                            "name": RESOURCE + "/dataStreams/7",
                            "type": "WEB_DATA_STREAM",
                            "webStreamData": {
                                "measurementId": ACCESS,
                                "defaultUri": "https://person:password@example.test/user%40example.test?token="
                                + REFRESH,
                            },
                            "secret": CLIENT_SECRET,
                        }
                    ]
                },
            )
        if request.method == "POST" and "landingPagePlusQueryString" in request.content.decode():
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "dimensionValues": [
                                {
                                    "value": "/person%40example.test?email=person@example.test"
                                    "&access_token=" + ACCESS
                                }
                            ],
                            "metricValues": [{"value": "1"}] * 3,
                        }
                    ],
                    "rowCount": 1,
                },
            )

    env.provider.override = private
    bind(env)
    collect(env)
    snapshot = current(env)
    render_customer(env.root, "synthetic", {"md"})
    cli.main(["connectors", "--customer", "synthetic"])
    with ui_server(env.root) as (server, client):
        html = client.get(server.url(customer="synthetic")).text
    dumped = json.dumps(snapshot) + (env.root / "synthetic/report.md").read_text() + html
    dumped += capsys.readouterr().out + caplog.text
    for secret in [ACCESS, REFRESH, CLIENT_SECRET, "person@example.test", "person%40example.test"]:
        assert secret not in dumped
    compact = ai.compact_evidence(
        [Evidence.model_validate(e) for e in snapshot["evidence"] if e["source"] == "ga4"]
    )
    assert "example.test" not in json.dumps(compact)
    assert "rows_returned" in json.dumps(compact)


def test_access_granted_cannot_set_engine_connected_and_rebind_resets(env):
    store.set_register(env.root, "synthetic", "ga4", {"ams_access": "granted"})
    assert all(e["integration"] != "connected" for e in current(env)["systems_register"]["entries"])
    with pytest.raises(ValueError):
        store.set_register(env.root, "synthetic", "ga4", {"integration": "connected"})
    bind(env)
    collect(env)
    service.set_binding(env.root, "synthetic", "ga4", "synthetic", "654321")
    snapshot = current(env)
    assert snapshot["connectors"]["ga4"]["status"] == "not_configured"
    assert not snapshot["connectors"]["ga4"].get("last_successful_collection")
    assert snapshot["evidence"] and snapshot["connector_history"]


def test_changed_binding_during_read_discards_result(env):
    bind(env)
    done = False

    def change(request):
        nonlocal done
        if not done:
            done = True
            service.set_binding(env.root, "synthetic", "ga4", "synthetic", "654321")

    env.provider.override = change
    with pytest.raises(ValueError, match="Binding changed"):
        collect(env)
    assert not any(e["source"] == "ga4" for e in current(env)["evidence"])
    assert current(env)["connectors"]["ga4"]["resource_id"] == "properties/654321"


def test_invalid_period_fails_before_network_or_write(env):
    bind(env)
    before = (env.root / "synthetic/assessment.json").read_bytes()
    for kwargs in [
        {"days": 0},
        {"days": 367},
        {"start": "2026-01-01"},
        {"start": "2020-02-02", "end": "2020-01-01"},
        {"start": "2099-01-01", "end": "2099-01-02"},
    ]:
        with pytest.raises(ValueError):
            collect(env, **kwargs)
    assert before == (env.root / "synthetic/assessment.json").read_bytes()
    assert not env.provider.calls


def test_missing_reference_and_unbound_resource(env):
    state = collect(env)
    assert state["status"] == "not_configured"
    service.set_binding(env.root, "synthetic", "ga4", "missing", RESOURCE)
    assert collect(env)["status"] == "credential_missing"
    assert not env.provider.calls


def test_concurrent_binding_has_only_one_winner(env):
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def attempt(customer):
        barrier.wait()
        try:
            service.set_binding(env.root, customer, "ga4", "synthetic", RESOURCE)
            return "bound"
        except ValueError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["first", "second"]))
    assert sorted(outcomes) == ["bound", "refused"]


def test_default_period_timezone_and_explicit_period(env):
    bind(env)
    state = collect(env, start="2025-01-01", end="2025-01-31")
    assert state["period"] == {"start": "2025-01-01", "end": "2025-01-31"}
    for request in env.provider.calls:
        if request.method == "POST":
            assert json.loads(request.content)["dateRanges"] == [
                {"startDate": "2025-01-01", "endDate": "2025-01-31"}
            ]


def test_data_pagination_follows_offsets_and_top_n_cap(env):
    offsets = []

    def page(request):
        if request.method != "POST":
            return None
        body = json.loads(request.content)
        if body["dimensions"] != [{"name": "landingPagePlusQueryString"}]:
            return None
        offsets.append(int(body["offset"]))
        assert body["orderBys"][0] == {"metric": {"metricName": "sessions"}, "desc": True}
        return httpx.Response(
            200,
            json={
                "rowCount": 200,
                "rows": [
                    {
                        "dimensionValues": [{"value": "/synthetic"}],
                        "metricValues": [{"value": "2"}] * 3,
                    }
                ]
                * 50,
            },
        )

    env.provider.override = page
    bind(env)
    state = collect(env)
    assert offsets == [0, 50]
    assert state["status"] == "connected_partial"
    landing = next(e for e in state["coverage"] if e["endpoint"] == "landing_pages")
    assert landing["rows"] == 100 and landing["truncated"]


def test_sampling_or_thresholding_cannot_confirm_zero(env):
    env.provider.override = lambda r: (
        httpx.Response(
            200, json={"rows": [], "rowCount": 0, "metadata": {"subjectToThresholding": True}}
        )
        if r.method == "POST"
        else None
    )
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    assert not any(f["check_key"] == "ga4:no-events" for f in current(env)["findings"])
    assert not any("unrecorded" in f["check_key"] for f in current(env)["findings"])


def test_public_tag_comparison_links_evidence_without_duplicate_findings(env):
    bind(env)
    snapshot = current(env)
    public = Evidence(
        id="ev-presence-r0-001",
        source="presence",
        url="https://example.test",
        description="Synthetic public sample",
        data={"pages_sampled": ["https://example.test"]},
    )
    snapshot["evidence"].append(public.model_dump())
    # A public observation only; supplied to the analyser to prove it references the finding.
    public_findings = [{"id": "public-missing-tag", "evidence_ids": [public.id]}]
    result, state = service.prepare(snapshot, "ga4", collect=True, public_findings=public_findings)
    streams = next(e for e in result.evidence if e.data["operation"] == "streams")
    comparison = streams.data["data"]["public_comparisons"][0]
    assert comparison["seen_in_public_sample"] is False
    assert comparison["evidence_ids"] == [public.id]
    assert comparison["related_finding_ids"] == ["public-missing-tag"]
    assert not any("tag" in f.title.lower() for f in result.findings)
    assert state["status"] == "connected"


def test_wrong_stream_website_is_reported_and_passing_recheck_resolves(env):
    bind(env)

    def different(request):
        if request.url.path.endswith("/dataStreams"):
            return httpx.Response(
                200,
                json={
                    "dataStreams": [
                        {
                            "name": RESOURCE + "/dataStreams/7",
                            "type": "WEB_DATA_STREAM",
                            "webStreamData": {
                                "measurementId": "G-SYNTHETIC1",
                                "defaultUri": "https://other.test",
                            },
                        }
                    ]
                },
            )

    env.provider.override = different
    collect(env)
    finding = next(f for f in current(env)["findings"] if "stream-site" in f["check_key"])
    assert "differs" in finding["title"] and finding["lifecycle_status"] == "current"
    env.provider.override = lambda request: None
    collect(env)
    assert (
        next(f for f in current(env)["findings"] if f["id"] == finding["id"])["lifecycle_status"]
        == "resolved"
    )


@pytest.mark.parametrize("status", ["permission_required", "api_not_enabled", "connected_partial"])
def test_failure_status_consistency_cli_ui_report_and_register(env, capsys, status):
    bind(env)

    def error(request):
        if status == "connected_partial" and request.method != "POST":
            return None
        return httpx.Response(
            403,
            json={
                "error": {
                    "details": [
                        {
                            "reason": "SERVICE_DISABLED"
                            if status == "api_not_enabled"
                            else "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
                        }
                    ]
                }
            },
        )

    env.provider.override = error
    assert collect(env)["status"] == status
    cli.main(["connectors", "--customer", "synthetic"])
    assert status in capsys.readouterr().out
    with ui_server(env.root) as (server, client):
        assert status in client.get(server.url(customer="synthetic")).text
    render_customer(env.root, "synthetic", {"md"})
    assert status in (env.root / "synthetic/report.md").read_text().replace("\\_", "_")
    assert all(
        e["integration"] == status
        for e in current(env)["systems_register"]["entries"]
        if e["platform"] == "ga4"
    )


def test_run_connect_collects_even_outside_public_categories(env, monkeypatch, tmp_path):
    from ams_assess import runner
    from ams_assess.connectors.adapter import ConnectorModule
    from ams_assess.models import Context, create_run_dir

    bind(env)
    monkeypatch.setattr(runner, "REGISTRY", {"ga4": ConnectorModule(ga4.CONNECTOR)})
    monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
    context = Context(
        "synthetic",
        "Synthetic Customer",
        "https://example.test",
        {"security"},
        {"ga4"},
        {},
        create_run_dir(env.root, "synthetic"),
        tmp_path,
        store_root=env.root,
    )
    result = runner.run(context, no_ai=True, formats={"md"})
    assert result["connector_results"]["ga4"]["status"] == "connected"
    assert result["findings"] and all(f["category"] == "cro_ux" for f in result["findings"])
    assert current(env)["connectors"]["ga4"]["status"] == "connected"
    report = (context.run_dir / "report.md").read_text()
    assert "## 7." in report and "Key event configured but not recorded" in report
    assert "## 4." not in report
    assert all(
        e["integration"] == "connected"
        for e in result["systems_register"]
        if e["platform"] == "ga4"
    )
    assert "connected" in current(env)["stages_assessed"]


def test_new_module_api_key_adapter_needs_only_registry_entry(env, monkeypatch):
    from dataclasses import replace

    from ams_assess.connectors.core import ReadOperation

    def authenticate(spec, ref, **kwargs):
        value = auth.load_reference(spec, ref, **kwargs)
        return AuthMaterial({"X-API-Key": value}, (value,))

    def test(ctx):
        def read(result):
            result.data = ctx.http.request("GET", "https://synthetic.test/sites/7")
            result.rows = 1

        return ctx.capture("site", "https://synthetic.test/sites/7", read)

    def http(request):
        assert request.headers["X-API-Key"] == "synthetic-api-key"
        return httpx.Response(200, json={"count": 2})

    spec = replace(
        ga4.CONNECTOR,
        key="synthetic",
        label="Synthetic API",
        provider="synthetic",
        auth_kind="api_key",
        scopes=("read",),
        resource_type="site",
        normalize_resource=lambda value: value,
        test=test,
        collect=lambda ctx: None,
        analysers=(),
        authenticate=authenticate,
        authorize=None,
        read_allowlist=(ReadOperation("GET", r"https://synthetic\.test/sites/7"),),
        transport_factory=lambda material: httpx.MockTransport(http),
    )
    monkeypatch.setitem(service.REGISTRY, "synthetic", spec)
    monkeypatch.setitem(register.PLATFORMS, "synthetic", spec.label)
    monkeypatch.setattr(service, "ReadClient", ReadClient)
    monkeypatch.setenv("SYNTHETIC_API_KEY", "synthetic-api-key")
    auth.register_reference("api-ref", spec, env_var="SYNTHETIC_API_KEY", directory=env.private)
    service.set_binding(env.root, "synthetic", "synthetic", "api-ref", "sites/7")
    state = service.execute(env.root, "synthetic", "synthetic", collect=True)
    assert state["status"] == "connected"
    assert "synthetic-api-key" not in json.dumps(current(env))
    assert any(row["key"] == "synthetic" for row in service.rows(current(env)))


def test_gsc_bound_adapter_and_legacy_auth_share_scope_helper(env, monkeypatch):
    from ams_assess.connectors import gsc

    data = json.loads(env.token.read_text())
    data["scopes"] = [gsc.SCOPE]
    path = env.private / "gsc-synthetic-token.json"
    auth.atomic_private(path, data)
    auth.register_reference("search-ref", gsc.CONNECTOR, file=path, directory=env.private)
    service.set_binding(env.root, "synthetic", "gsc", "search-ref", "sc-domain:example.test")
    calls = []

    def response(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"rows": []})
        if request.url.path.endswith("/sitemaps"):
            return httpx.Response(200, json={"sitemap": []})
        return httpx.Response(
            200, json={"siteUrl": "sc-domain:example.test", "permissionLevel": "siteRestrictedUser"}
        )

    monkeypatch.setattr(
        service,
        "ReadClient",
        lambda spec, material, **kwargs: ReadClient(
            spec, material, transport=httpx.MockTransport(response)
        ),
    )
    state = service.execute(env.root, "synthetic", "gsc", collect=True)
    assert state["status"] == "connected" and len(calls) == 4
    assert cli.google_desktop_flow is auth.google_desktop_flow
    assert [gsc.SCOPE] == json.loads(path.read_text())["scopes"]
    assert json.loads(env.token.read_text())["scopes"] == [ga4.SCOPE]

    from ams_assess import runner
    from ams_assess.models import Context, create_run_dir
    from ams_assess.modules import gsc as legacy

    monkeypatch.setattr(runner, "REGISTRY", {"gsc": legacy})
    monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
    ctx = Context(
        "synthetic",
        "Synthetic Customer",
        "https://example.test",
        {"security"},
        {"gsc"},
        {},
        create_run_dir(env.root, "synthetic"),
        env.root,
        store_root=env.root,
    )
    output = runner.run(ctx, no_ai=True, formats={"md"})
    assert output["connector_results"]["gsc"]["status"] == "connected"


def test_credential_symlinks_and_scope_confusion_refused(env):
    link = env.private / "linked.json"
    link.symlink_to(env.token)
    with pytest.raises(ValueError, match="private config"):
        auth.register_reference("linked", ga4.CONNECTOR, file=link, directory=env.private)
    other = env.private.parent / "outside.json"
    other.write_text("{}")
    with pytest.raises(ValueError, match="private config"):
        auth.register_reference("outside", ga4.CONNECTOR, file=other, directory=env.private)
    for ref in ["../secret", "Bearer-token", "sk-abcdefgh", "a" * 49]:
        with pytest.raises(ValueError):
            service.set_binding(env.root, "synthetic", "ga4", ref, RESOURCE)


def test_response_cap_and_invalid_json_are_safe_failures(env):
    bind(env)
    for response in [
        httpx.Response(200, content=b"x" * 2_000_001),
        httpx.Response(200, content=b"not json"),
    ]:
        env.provider.override = lambda request, response=response: response
        assert collect(env)["status"] == "failed"
        assert not current(env)["findings"]


def test_public_comparison_uses_latest_sample_and_same_stream_site(env):
    bind(env)
    snapshot = current(env)
    snapshot["profile"]["fields"]["websites"].append("https://other.test")
    snapshot["evidence"].extend(
        [
            Evidence(
                id="ev-presence-r1-001",
                source="presence",
                url="https://example.test",
                description="Older tag",
                data={"platform": "ga4", "ids": ["G-SYNTHETIC1"]},
            ).model_dump(),
            Evidence(
                id="ev-presence-r2-001",
                source="presence",
                url="https://example.test",
                description="Latest sample",
                data={"pages_sampled": ["https://example.test"]},
            ).model_dump(),
            Evidence(
                id="ev-presence-r3-001",
                source="presence",
                url="https://other.test",
                description="Other website",
                data={"platform": "ga4", "ids": ["G-SYNTHETIC1"]},
            ).model_dump(),
        ]
    )
    result, _ = service.prepare(snapshot, "ga4", collect=True)
    stream = next(e for e in result.evidence if e.data["operation"] == "streams")
    comparison = stream.data["data"]["public_comparisons"][0]
    assert comparison["seen_in_public_sample"] is False
    assert comparison["evidence_ids"] == ["ev-presence-r2-001"]
    finding = next(f for f in result.findings if "stream-tag" in f.check_key)
    assert finding.confidence == "needs_verification"
    assert finding.evidence_ids == [stream.id]


def test_explicit_dates_must_be_complete_in_property_timezone(env, monkeypatch):
    from ams_assess.connectors import core

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 1, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(core, "datetime", Frozen)
    env.provider.override = lambda r: httpx.Response(
        200,
        json={
            "name": RESOURCE,
            "timeZone": "America/Los_Angeles",
        },
    )
    bind(env)
    state = collect(env, start="2025-12-31", end="2025-12-31")
    assert state["status"] == "not_configured"
    assert "property time zone" in state["detail"]
    assert len(env.provider.calls) == 1


def test_pagination_tokens_are_not_logged(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.provider.override = lambda r: httpx.Response(
        200,
        json={
            "accountSummaries": [{"propertySummaries": [{"property": RESOURCE}]}],
            "nextPageToken": ACCESS,
        },
    )
    listing = service.list_resources("ga4", "synthetic")
    assert listing.truncated
    assert ACCESS not in caplog.text
    assert ACCESS not in json.dumps(listing.data)
