"""Synthetic GBP HTTP/OAuth tests. No Google or customer data is accessed."""

import copy
import json
import logging
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.oauth2.credentials import Credentials

from ams_assess import ai, cli, runner, store
from ams_assess.connectors import auth, gbp, service
from ams_assess.connectors.adapter import ConnectorModule
from ams_assess.connectors.core import (
    AuthMaterial,
    ConnectorContext,
    ConnectorError,
    EvidenceView,
    Period,
)
from ams_assess.connectors.http import ReadClient
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir
from ams_assess.report import render_customer
from ams_assess.ui import LocalServer

RESOURCE = "locations/123456"
ACCOUNT = "accounts/123"
START, END = "2026-01-01", "2026-01-31"
ACCESS, REFRESH, SECRET = "synthetic-access", "synthetic-refresh", "synthetic-client-secret"
PROFILE: dict[str, Any] = {
    "name": "Synthetic Plumbing",
    "phones": ["02 9999 0000"],
    "websites": ["https://example.test"],
    "service_area": ["Sydney"],
    "locations": [
        {"street": "1 Example Street", "suburb": "Sydney", "state": "NSW", "postcode": "2000"}
    ],
}


class Provider:
    def __init__(self):
        self.calls = []
        self.override: Callable[[httpx.Request], httpx.Response | None] = lambda request: None
        self.location = {
            "name": RESOURCE,
            "title": "Synthetic Plumbing",
            "websiteUri": "https://example.test",
            "phoneNumbers": {"primaryPhone": "+61 2 9999 0000"},
            "storefrontAddress": {
                "addressLines": ["1 Example Street"],
                "locality": "Sydney",
                "administrativeArea": "NSW",
                "postalCode": "2000",
            },
            "serviceArea": {"places": {"placeInfos": [{"placeName": "Sydney"}]}},
            "categories": {"primaryCategory": {"name": "gcid:plumber", "displayName": "Plumber"}},
            "metadata": {"placeId": "ChIJSynthetic", "mapsUri": "https://maps.google.com/?cid=987"},
            "profile": {"description": "Synthetic business description"},
        }

    def __call__(self, request):
        self.calls.append(request)
        assert request.method == "GET"
        overridden = self.override(request)
        if overridden is not None:
            return overridden
        path = request.url.path
        if request.url.host == "mybusinessaccountmanagement.googleapis.com":
            assert path == "/v1/accounts" and request.url.params["pageSize"] == "20"
            return httpx.Response(200, json={"accounts": [{"name": ACCOUNT}]})
        if path == "/v1/" + ACCOUNT + "/locations":
            assert request.url.params["pageSize"] == "100"
            return httpx.Response(
                200,
                json={
                    "locations": [
                        self.location,
                        {"name": "locations/999999", "title": "Unselected location"},
                    ]
                },
            )
        if path == "/v1/" + RESOURCE:
            assert request.url.params["readMask"] == gbp.READ_MASK
            return httpx.Response(200, json=self.location)
        if path == "/v4/" + ACCOUNT + "/" + RESOURCE + "/reviews":
            assert request.url.params["pageSize"] == "50"
            return httpx.Response(
                200,
                json={
                    "reviews": [
                        {
                            "reviewId": "one",
                            "starRating": "FIVE",
                            "reviewer": {"displayName": "Jane"},
                            "comment": "Personal information",
                            "reviewReply": {"comment": "Private reply"},
                        },
                        {"reviewId": "two", "starRating": "THREE"},
                    ],
                    "totalReviewCount": 2,
                    "averageRating": 4,
                },
            )
        if path == "/v1/" + RESOURCE + ":fetchMultiDailyMetricsTimeSeries":
            assert set(request.url.params.get_list("dailyMetrics")) == set(gbp.METRICS)
            assert request.url.params["dailyRange.start_date.year"] == "2026"
            start = datetime(
                int(request.url.params["dailyRange.start_date.year"]),
                int(request.url.params["dailyRange.start_date.month"]),
                int(request.url.params["dailyRange.start_date.day"]),
            )
            end = datetime(
                int(request.url.params["dailyRange.end_date.year"]),
                int(request.url.params["dailyRange.end_date.month"]),
                int(request.url.params["dailyRange.end_date.day"]),
            )
            days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            return httpx.Response(
                200,
                json={
                    "multiDailyMetricTimeSeries": [
                        {
                            "dailyMetricTimeSeries": [
                                {
                                    "dailyMetric": metric,
                                    "timeSeries": {
                                        "datedValues": [
                                            {
                                                "date": {
                                                    "year": day.year,
                                                    "month": day.month,
                                                    "day": day.day,
                                                },
                                                "value": "2",
                                            }
                                            for day in days
                                        ]
                                    },
                                }
                                for metric in gbp.METRICS
                            ],
                        }
                    ]
                },
            )
        assert path == "/v1/" + RESOURCE + "/searchkeywords/impressions/monthly"
        assert request.url.params["monthlyRange.start_month.month"] == "1"
        return httpx.Response(
            200,
            json={
                "searchKeywordsCounts": [
                    {"searchKeyword": "synthetic plumber", "insightsValue": {"value": "30"}},
                    {"searchKeyword": "rare term", "insightsValue": {"threshold": "15"}},
                ]
            },
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    root, private = tmp_path / "customers", tmp_path / "private"
    private.mkdir(mode=0o700)
    token = private / "gbp-synthetic-token.json"
    auth.atomic_private(
        token,
        {
            "token": ACCESS,
            "refresh_token": REFRESH,
            "client_id": "synthetic.apps.googleusercontent.com",
            "client_secret": SECRET,
            "scopes": [gbp.SCOPE],
            "type": "authorized_user",
            "expiry": (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    auth.register_reference("synthetic", gbp.CONNECTOR, file=token, directory=private)
    monkeypatch.setattr(service, "config_dir", lambda: private)
    monkeypatch.setattr(cli, "data_root", lambda: root)
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", lambda seconds: None)
    provider = Provider()
    real_client = service.ReadClient
    monkeypatch.setattr(
        service,
        "ReadClient",
        lambda *a, transport=None, **kw: real_client(
            *a, transport=httpx.MockTransport(provider), **kw
        ),
    )
    store.set_profile(root, "synthetic", PROFILE)
    return SimpleNamespace(root=root, private=private, token=token, provider=provider)


def bind(env, customer="synthetic", ref="synthetic", **kwargs):
    return service.set_binding(env.root, customer, "gbp", ref, RESOURCE, **kwargs)


def collect(env, **kwargs):
    return service.execute(
        env.root, "synthetic", "gbp", collect=True, start=START, end=END, **kwargs
    )


def current(env):
    return store.load(env.root, "synthetic")


@contextmanager
def context(provider, *, start=START, end=END, **kwargs):
    http = ReadClient(gbp.CONNECTOR, AuthMaterial(), transport=httpx.MockTransport(provider))
    if "public_evidence" in kwargs:
        kwargs["public_evidence"] = tuple(EvidenceView.of(e) for e in kwargs["public_evidence"])
    ctx = ConnectorContext(http, RESOURCE, Period.make(start=start, end=end), **kwargs)
    try:
        yield ctx
    finally:
        http.close()


def analyse(ctx, profile=None):
    result = ModuleResult(name="gbp")
    for endpoint in ctx.endpoints:
        endpoint.evidence_id = result.add_evidence("", endpoint.key, endpoint.data)
    gbp.analyse_profile(ctx, result, PROFILE if profile is None else profile, "ev-profile-r1-001")
    return result


def test_auth_scope_and_private_storage(env, monkeypatch, capsys):
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
                client_secret=SECRET,
                scopes=[gbp.SCOPE],
            )

    monkeypatch.setattr(auth, "InstalledAppFlow", Flow)
    auth.google_authorize(gbp.CONNECTOR, "new-ref", client, directory=env.private)
    assert seen["scopes"] == [gbp.SCOPE]
    assert seen["include_granted_scopes"] == "false"
    assert seen["timeout_seconds"] == 180
    assert stat.S_IMODE((env.private / "gbp-new-ref-token.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(env.private.stat().st_mode) == 0o700
    assert ACCESS not in capsys.readouterr().out


@pytest.mark.parametrize("scopes", [[], [gbp.SCOPE, "https://www.googleapis.com/auth/drive"]])
def test_wrong_scopes_never_reach_provider(env, scopes):
    data = json.loads(env.token.read_text())
    data["scopes"] = scopes
    auth.atomic_private(env.token, data)
    bind(env)
    assert collect(env)["status"] == "permission_required"
    assert not env.provider.calls


@pytest.mark.parametrize("revoked", [False, True])
def test_refresh_and_revocation_mocked_http(env, monkeypatch, revoked):
    data = json.loads(env.token.read_text())
    data["expiry"] = "2020-01-01T00:00:00Z"
    auth.atomic_private(env.token, data)
    calls = []

    def oauth(request):
        calls.append(request)
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(
            400 if revoked else 200,
            json={"error": "invalid_grant"}
            if revoked
            else {"access_token": ACCESS, "expires_in": 3600, "scope": gbp.SCOPE},
        )

    def request(url, method, body, headers, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(oauth)) as client:
            response = client.request(method, url, content=body, headers=headers)
        return SimpleNamespace(
            status=response.status_code, data=response.content, headers=response.headers
        )

    monkeypatch.setattr(auth, "BoundedRequest", lambda: request)
    bind(env)
    assert collect(env)["status"] == ("credential_expired" if revoked else "connected")
    assert len(calls) == 1
    if revoked:
        assert not env.provider.calls


def test_missing_credentials_and_selection(env):
    assert collect(env)["status"] == "not_configured"
    bind(env, ref="missing")
    assert collect(env)["status"] == "credential_missing"
    assert not env.provider.calls


@pytest.mark.parametrize(
    "value",
    [
        "123456",
        "cid=123456",
        "ChIJSynthetic",
        "locations/01",
        "accounts/123/locations/123456",
        "locations/123?x=1",
        "locations/123/../456",
        "locations/123,locations/456",
    ],
)
def test_binding_rejects_ambiguous_or_unsafe_ids(value):
    with pytest.raises(ValueError):
        gbp.normalize_resource(value)


def test_listing_selection_identity_and_customer_isolation(env):
    listing = service.list_resources("gbp", "synthetic")
    assert {row["id"] for row in listing.data["rows"]} == {RESOURCE, "locations/999999"}
    assert listing.data["rows"][0]["metadata"]["placeId"] == "ChIJSynthetic"
    assert listing.data["rows"][0]["metadata"]["maps_cid"] == "987"
    assert not current(env).get("connectors")
    assert all(
        "reviews" not in str(r.url) and "performance" not in r.url.host for r in env.provider.calls
    )
    bind(env)
    with pytest.raises(ValueError, match="different customer"):
        bind(env, customer="other", ref="other-ref")
    with pytest.raises(ValueError, match="reason"):
        bind(env, customer="other", force=True)
    bind(env, customer="other", force=True, reason="Synthetic shared resource approval")
    other = store.load(env.root, "other")
    assert other and other["connector_history"][-1]["forced"]
    env.provider.calls.clear()
    state = collect(env)
    assert state["status"] == "connected"
    assert all("999999" not in str(r.url) for r in env.provider.calls)
    assert any(r.url.params.get("readMask") == "name" for r in env.provider.calls)


def test_success_preserves_metrics_metadata_and_keyword_thresholds(env):
    bind(env)
    state = collect(env)
    assert state["status"] == "connected"
    evidence = [e for e in current(env)["evidence"] if e["source"] == "gbp"]
    payloads = {e["data"]["operation"]: e["data"] for e in evidence}
    assert payloads["reviews"]["data"]["star_counts"]["FIVE"] == 1
    assert payloads["reviews"]["data"]["unanswered"] == 1
    assert payloads["reviews"]["data"]["average_rating"] == 4
    assert payloads["monthly_keywords"]["data"]["rows"][1] == {
        "keyword": "rare term",
        "threshold": 15,
    }
    assert payloads["daily_metrics"]["coverage"]["rows_returned"] == 31 * len(gbp.METRICS)
    for e in evidence:
        assert e["data"]["connector"] == "gbp"
        assert e["data"]["resource_id"] == RESOURCE
        assert e["data"]["period"] == {"start": START, "end": END}
        assert e["data"]["collected_at"] and e["data"]["endpoint"]


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (401, "", "credential_expired"),
        (403, "PERMISSION_DENIED", "permission_required"),
        (403, "SERVICE_DISABLED", "api_not_enabled"),
        (404, "", "not_found"),
        (429, "RATE_LIMIT_EXCEEDED", "rate_limited"),
        (503, "", "provider_unavailable"),
        (400, "", "failed"),
    ],
)
def test_statuses_and_bounded_retries(env, status, reason, expected):
    env.provider.override = lambda r: httpx.Response(
        status, json={"error": {"details": [{"reason": reason}], "message": SECRET}}
    )
    bind(env)
    state = collect(env)
    assert state["status"] == expected
    assert len(env.provider.calls) == (3 if status in {429, 503} else 1)
    assert SECRET not in json.dumps(current(env))


def test_identity_mismatch_and_malformed_response(env):
    env.provider.location["name"] = "locations/999999"
    bind(env)
    assert collect(env)["status"] == "failed"
    assert len(env.provider.calls) == 1
    env.provider.override = lambda r: httpx.Response(200, json=[])
    assert collect(env)["status"] == "failed"


def test_timeout_deadline_and_redirect_are_bounded(env):
    def timeout(request):
        raise httpx.ReadTimeout(SECRET)

    bind(env)
    env.provider.override = timeout
    assert collect(env)["status"] == "provider_unavailable"
    assert len(env.provider.calls) == 3
    env.provider.calls.clear()
    assert collect(env, seconds=-1)["status"] == "provider_unavailable"
    assert not env.provider.calls
    env.provider.override = lambda r: httpx.Response(302, headers={"Location": "https://evil.test"})
    assert collect(env)["status"] == "failed"
    assert len(env.provider.calls) == 1


@pytest.mark.parametrize("kind", ["accounts", "locations", "reviews", "keywords"])
def test_pagination_and_repeated_token_caps(kind):
    provider = Provider()
    count = 0

    def paginated(request):
        nonlocal count
        field, fragment, raw = {
            "accounts": ("accounts", "/v1/accounts", {"name": ACCOUNT}),
            "locations": ("locations", ACCOUNT + "/locations", provider.location),
            "reviews": ("reviews", "/reviews", {"reviewId": str(count), "starRating": "ONE"}),
            "keywords": (
                "searchKeywordsCounts",
                "/monthly",
                {"searchKeyword": "term", "insightsValue": {"value": "1"}},
            ),
        }[kind]
        if not request.url.path.endswith(fragment):
            return None
        count += 1
        return httpx.Response(200, json={field: [raw], "nextPageToken": "repeat"})

    provider.override = paginated
    with context(provider) as ctx:
        endpoint = {
            "accounts": lambda: gbp.accounts(ctx),
            "locations": lambda: gbp.locations(ctx, ACCOUNT, discovery=True),
            "reviews": lambda: gbp.reviews(ctx, ACCOUNT + "/" + RESOURCE),
            "keywords": lambda: gbp.keywords(ctx),
        }[kind]()
        assert endpoint.truncated and count == 2
        assert endpoint.rows >= 1


def test_partial_reviews_keep_first_page_and_other_endpoints(env):
    def paginated(request):
        if not request.url.path.endswith("/reviews"):
            return None
        if "pageToken" in request.url.params:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "reviews": [{"reviewId": "first", "starRating": "ONE"}],
                "nextPageToken": "next",
                "totalReviewCount": 2,
            },
        )

    env.provider.override = paginated
    bind(env)
    state = collect(env)
    assert state["status"] == "connected_partial"
    reviews = next(e for e in state["coverage"] if e["endpoint"] == "reviews")
    assert reviews["rows"] == 1 and reviews["status"] == "provider_unavailable"
    assert next(e for e in state["coverage"] if e["endpoint"] == "daily_metrics")["rows"] == 248


def test_empty_reviews_and_performance_have_distinct_coverage():
    provider = Provider()
    provider.override = lambda r: httpx.Response(200, json={})
    with context(provider) as ctx:
        reviews = gbp.reviews(ctx, ACCOUNT + "/" + RESOURCE)
        daily = gbp.daily(ctx)
        keywords = gbp.keywords(ctx)
        assert reviews.complete and reviews.rows == 0
        assert keywords.complete and keywords.rows == 0
        assert daily.status == "connected" and daily.truncated
        assert daily.data["missing_metrics"] == sorted(gbp.METRICS)
        assert "not zero" in daily.detail


def test_months_are_complete_and_within_requested_period():
    provider = Provider()
    with context(provider, start="2026-01-02", end="2026-01-30") as ctx:
        result = gbp.keywords(ctx)
        assert result.truncated and "No complete calendar month" in result.detail
        assert not provider.calls


ALLOWED = [
    gbp.ACCOUNTS + "accounts",
    gbp.INFO + ACCOUNT + "/locations",
    gbp.INFO + RESOURCE,
    gbp.REVIEWS + ACCOUNT + "/" + RESOURCE + "/reviews",
    gbp.PERFORMANCE + RESOURCE + ":fetchMultiDailyMetricsTimeSeries",
    gbp.PERFORMANCE + RESOURCE + "/searchkeywords/impressions/monthly",
]


@pytest.mark.parametrize("url", ALLOWED)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_business_manage_refuses_every_non_get_method(url, method):
    with context(lambda r: pytest.fail("Write reached HTTP")) as ctx:
        with pytest.raises(ConnectorError, match="allowlist"):
            ctx.http.request(method, url)


@pytest.mark.parametrize(
    "url",
    [
        gbp.INFO + RESOURCE + ":delete",
        gbp.INFO + RESOURCE + "/admins",
        gbp.REVIEWS + ACCOUNT + "/" + RESOURCE + "/reviews/abc/reply",
        gbp.INFO + RESOURCE + "?alt=json",
        gbp.INFO + "locations/123%2f..%2f456",
        "https://evil.test/v1/" + RESOURCE,
        gbp.INFO + RESOURCE + "/",
        gbp.PERFORMANCE + RESOURCE + ":getDailyMetricsTimeSeries",
    ],
)
def test_unknown_get_endpoints_refused(url):
    with context(lambda r: pytest.fail("Disallowed endpoint reached HTTP")) as ctx:
        with pytest.raises(ConnectorError, match="allowlist"):
            ctx.http.request("GET", url)


def test_profile_mismatch_findings_and_missing_profile_does_not_pass():
    provider = Provider()
    with context(provider, websites=PROFILE["websites"]) as ctx:
        gbp.test(ctx)
        wrong = copy.deepcopy(PROFILE)
        wrong.update(
            name="Different business",
            phones=["0299991111"],
            websites=["https://different.test"],
            service_area=["Melbourne"],
        )
        wrong["locations"][0]["street"] = "2 Different Road"
        result = analyse(ctx, wrong)
        findings = {f.check_key: f for f in result.findings}
        for field in ("name", "phone", "website", "address", "service-area"):
            finding = findings["gbp:profile-" + field]
            assert finding.confidence == "confirmed"
            assert "not which value is correct" in finding.detail
        assert "gbp:hours-missing" in findings
        missing = analyse(ctx, {})
        assert not any(o.check_id.startswith("gbp:profile-") for o in missing.outcomes)


def test_public_findings_are_referenced_not_duplicated():
    provider = Provider()
    evidence = Evidence(
        id="ev-presence-r1-001",
        source="presence",
        url="https://example.test",
        description="NAP comparison",
        data={"entities": [{"page": "https://example.test/contact", "telephone": ["0299991111"]}]},
    )
    with context(
        provider,
        websites=PROFILE["websites"],
        public_evidence=[evidence],
        public_findings=[{"id": "public-nap", "evidence_ids": [evidence.id]}],
    ) as ctx:
        location = gbp.test(ctx)
        result = analyse(ctx)
        assert not any(f.check_key.startswith("gbp:public-") for f in result.findings)
        comparison = location.data["website_comparisons"][0]
        assert comparison["related_finding_ids"] == ["public-nap"]
        assert comparison["evidence_ids"] == [evidence.id]


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


def test_cli_ui_report_status_and_privacy(env, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    bind(env)
    collect(env)
    assert cli.main(["connectors", "--customer", "synthetic"]) == 0
    output = capsys.readouterr().out
    with ui_server(env.root) as (server, client):
        page = client.get(server.url(customer="synthetic")).text
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text()
    for value in ("connected", RESOURCE, "reviews: connected, 2 rows"):
        assert value in output and value in page and value in report
    assert "## 7. Connected business-system findings" in report
    snapshot = current(env)
    dumped = json.dumps(snapshot) + output + page + report + caplog.text
    for value in (ACCESS, REFRESH, SECRET, "Jane", "Personal information", "Private reply"):
        assert value not in dumped
    compact = ai.compact_evidence(
        [Evidence.model_validate(e) for e in snapshot["evidence"] if e["source"] == "gbp"]
    )
    assert "rows_returned" in json.dumps(compact)
    assert "synthetic plumber" not in json.dumps(compact)


def test_redaction_of_keyword_emails_and_location_urls(env):
    env.provider.location["websiteUri"] = "https://person:password@example.test/?token=" + ACCESS
    env.provider.location["metadata"]["mapsUri"] += "&access_token=" + ACCESS
    env.provider.location["storefrontAddress"]["recipients"] = ["Private recipient"]

    def keyword(request):
        if request.url.path.endswith("/monthly"):
            return httpx.Response(
                200,
                json={
                    "searchKeywordsCounts": [
                        {"searchKeyword": "person@example.test", "insightsValue": {"value": "5"}}
                    ]
                },
            )

    env.provider.override = keyword
    bind(env)
    collect(env)
    data = json.dumps(current(env))
    assert "[email:" in data
    for value in ("person@example.test", "password", ACCESS, "Private recipient"):
        assert value not in data


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "categories",
        "regularHours",
        "specialHours",
        "websiteUri",
        "phoneNumbers",
        "storefrontAddress",
        "serviceArea",
        "profile.description",
        "metadata",
    ],
)
def test_location_read_mask_contains_required_fields(field):
    assert field in gbp.READ_MASK.split(",")


@pytest.mark.parametrize("response_size", [1, 101])
def test_location_page_bounds_even_when_provider_ignores_limit(response_size):
    provider = Provider()
    count = 0

    def listing(request):
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={
                "locations": [
                    {"name": f"locations/{count * 1000 + i}", "title": "Synthetic"}
                    for i in range(response_size)
                ],
                "nextPageToken": str(count),
            },
        )

    provider.override = listing
    with context(provider) as ctx:
        result = gbp.locations(ctx, ACCOUNT, discovery=True)
        assert result.truncated
        assert result.rows == (5 if response_size == 1 else 100)
        assert count == (5 if response_size == 1 else 1)


def test_resource_listing_retains_successful_account_after_later_failure(env):
    def override(request):
        if request.url.host == "mybusinessaccountmanagement.googleapis.com":
            return httpx.Response(
                200, json={"accounts": [{"name": ACCOUNT}, {"name": "accounts/4"}]}
            )
        if "/accounts/4/" in request.url.path:
            return httpx.Response(403)

    env.provider.override = override
    listing = service.list_resources("gbp", "synthetic")
    assert listing.status == "connected_partial" and listing.rows == 2
    assert "permission_required" in listing.detail
    assert not current(env).get("connectors")


@pytest.mark.parametrize("failure", [False, True])
def test_missing_review_parent_does_not_turn_empty_listing_into_permission_error(env, failure):
    env.provider.override = lambda r: (
        (httpx.Response(503) if failure else httpx.Response(200, json={"accounts": []}))
        if r.url.host == "mybusinessaccountmanagement.googleapis.com"
        else None
    )
    bind(env)
    state = collect(env)
    review = next(row for row in state["coverage"] if row["endpoint"] == "reviews")
    assert review["status"] == ("provider_unavailable" if failure else "not_found")
    assert not any("/reviews" in r.url.path for r in env.provider.calls)
    assert state["status"] == "connected_partial"
    assert any(":fetchMulti" in r.url.path for r in env.provider.calls)


@pytest.mark.parametrize("problem", ["missing-bookings", "zero", "bad-date", "bad-count"])
def test_daily_metric_availability_and_strict_parsing(problem):
    provider = Provider()

    def daily(request):
        metrics = gbp.METRICS[:-1] if problem == "missing-bookings" else gbp.METRICS
        return httpx.Response(
            200,
            json={
                "multiDailyMetricTimeSeries": [
                    {
                        "dailyMetricTimeSeries": [
                            {
                                "dailyMetric": metric,
                                "timeSeries": {
                                    "datedValues": [
                                        {
                                            "date": {
                                                "year": 2026,
                                                "month": 2 if problem == "bad-date" else 1,
                                                "day": 1,
                                            },
                                            **({"value": "-5"} if problem == "bad-count" else {}),
                                        }
                                    ]
                                },
                            }
                            for metric in metrics
                        ]
                    }
                ]
            },
        )

    provider.override = daily
    with context(provider, start=START, end=START) as ctx:
        result = gbp.daily(ctx)
        if problem.startswith("bad-"):
            assert result.status == "failed"
        elif problem == "zero":
            assert result.complete
            assert result.data["series"]["CALL_CLICKS"][0]["value"] == 0
        else:
            assert result.truncated
            assert result.data["missing_metrics"] == ["BUSINESS_BOOKINGS"]
            assert "BUSINESS_BOOKINGS" not in result.data["series"]


def test_keyword_period_uses_only_contained_months_and_preserves_failed_page():
    calls = []

    def keywords(request):
        calls.append(request)
        params = request.url.params
        assert params["monthlyRange.start_month.month"] == "2"
        assert params["monthlyRange.end_month.month"] == "2"
        if "pageToken" in params:
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "searchKeywordsCounts": [
                    {"searchKeyword": "example", "insightsValue": {"value": "0"}}
                ],
                "nextPageToken": "next",
            },
        )

    with context(keywords, start="2026-01-15", end="2026-03-15") as ctx:
        result = gbp.keywords(ctx)
        assert result.status == "permission_required" and result.truncated
        assert result.rows == 1
        assert result.data["coverage_period"] == {"start": "2026-02-01", "end": "2026-02-28"}
    assert len(calls) == 2


def test_request_budget_and_response_size_limits():
    client = ReadClient(
        gbp.CONNECTOR,
        AuthMaterial(),
        max_requests=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    try:
        client.request("GET", ALLOWED[0])
        with pytest.raises(ConnectorError, match="Request cap"):
            client.request("GET", ALLOWED[0])
    finally:
        client.close()
    with context(lambda r: httpx.Response(200, content=b"x" * 2_000_001)) as ctx:
        result = gbp.test(ctx)
        assert result.status == "failed" and "size cap" in result.detail


def test_transient_retry_backoff_and_timeout_parameters(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", sleeps.append)

    def retry(request):
        calls.append(request)
        assert max(request.extensions["timeout"].values()) <= 10
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "999"})
        return httpx.Response(200, json=Provider().location)

    with context(retry) as ctx:
        assert gbp.test(ctx).complete
    assert len(calls) == 2 and sleeps == [2]


def test_partial_review_sample_cannot_emit_a_passing_outcome():
    provider = Provider()
    with context(provider) as ctx:
        gbp.test(ctx)
        review = gbp.reviews(ctx, ACCOUNT + "/" + RESOURCE)
        review.truncated = True
        review.data["unanswered"] = 0
        result = analyse(ctx)
        assert not any(o.check_id == "gbp:unanswered-reviews" for o in result.outcomes)
        review.data["unanswered"] = 1
        assert any(f.check_key == "gbp:unanswered-reviews" for f in analyse(ctx).findings)


def test_public_ambiguous_entities_are_not_treated_as_complete_passes():
    provider = Provider()
    entity = {"page": "https://example.test", "telephone": ["0299990000"]}
    evidence = Evidence(
        id="ev-presence-r1-001",
        source="presence",
        url="https://example.test",
        description="NAP",
        data={"entities": [entity, {**entity, "name": "Other"}]},
    )
    with context(provider, websites=PROFILE["websites"], public_evidence=[evidence]) as ctx:
        location = gbp.test(ctx)
        result = analyse(ctx)
        assert not any(o.check_id.startswith("gbp:public-") for o in result.outcomes)
        assert "needs verification" in location.data["website_comparisons"][0]["detail"]


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize(
    "reason,quota",
    [
        ("API_NOT_APPROVED", None),
        ("APPROVAL_REQUIRED", None),
        ("RESOURCE_EXHAUSTED", "0"),
        ("RATE_LIMIT_EXCEEDED", 0),
    ],
)
def test_approval_required_never_retries_or_loses_next_action(env, status, reason, quota):
    detail = {"reason": reason, "metadata": {"quota_limit_value": quota}}
    env.provider.override = lambda r: httpx.Response(
        status, json={"error": {"status": reason, "details": [detail], "message": SECRET}}
    )
    bind(env)
    state = collect(env)
    assert state["status"] == "approval_required"
    assert "Application for Basic API Access" in state["detail"]
    assert "quota increase" in state["detail"]
    assert len(env.provider.calls) == 1
    assert SECRET not in json.dumps(current(env))


def test_approval_failure_on_reviews_preserves_other_endpoint_results(env):
    env.provider.override = lambda r: (
        httpx.Response(
            429,
            json={
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"metadata": {"quota_limit_value": "0"}}],
                }
            },
        )
        if r.url.path.endswith("/reviews")
        else None
    )
    bind(env)
    state = collect(env)
    assert state["status"] == "connected_partial"
    assert next(e for e in state["coverage"] if e["endpoint"] == "reviews")["status"] == (
        "approval_required"
    )
    assert next(e for e in state["coverage"] if e["endpoint"] == "daily_metrics")["rows"] == 248
    assert len([r for r in env.provider.calls if r.url.path.endswith("/reviews")]) == 1


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (403, "PERMISSION_DENIED", "permission_required"),
        (403, "SERVICE_DISABLED", "api_not_enabled"),
        (429, "API_NOT_APPROVED", "approval_required"),
        (429, "RATE_LIMIT_EXCEEDED", "rate_limited"),
        (403, "LICENSE_REQUIRED", "licence_required"),
    ],
)
def test_failure_statuses_agree_across_cli_ui_register_and_report(
    env, capsys, status, reason, expected
):
    env.provider.override = lambda r: httpx.Response(status, json={"error": {"status": reason}})
    bind(env)
    assert collect(env)["status"] == expected
    assert cli.main(["connectors", "--customer", "synthetic"]) == 0
    output = capsys.readouterr().out
    with ui_server(env.root) as (server, client):
        page = client.get(server.url(customer="synthetic")).text
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text()
    assert expected in output and expected in page and expected in report
    entry = next(e for e in current(env)["systems_register"]["entries"] if e["platform"] == "gbp")
    assert entry["integration"] == expected
    if expected == "permission_required":
        assert "Manager" in page and "Manager" in report


def test_cli_auth_bind_test_collect_and_ui_post_controls(env, capsys):
    assert (
        cli.main(
            [
                "connect",
                "gbp",
                "bind",
                "--customer",
                "synthetic",
                "--credential-ref",
                "synthetic",
                "--resource",
                RESOURCE,
            ]
        )
        == 0
    )
    assert cli.main(["connect", "gbp", "test", "--customer", "synthetic"]) == 0
    assert (
        cli.main(
            ["connect", "gbp", "collect", "--customer", "synthetic", "--start", START, "--end", END]
        )
        == 0
    )
    assert "connected" in capsys.readouterr().out
    with ui_server(env.root) as (server, client):
        payload = {
            "customer": "synthetic",
            "connector": "gbp",
            "csrf": server.csrf,
            "credential_ref": "synthetic",
            "resource": RESOURCE,
            "days": "28",
        }
        for action in ("reference", "bind", "test", "collect"):
            response = client.post(
                server.url("/connect"),
                data={**payload, "action": action},
                headers={"Origin": server.origin},
            )
            assert response.status_code == 303
        assert current(env)["connectors"]["gbp"]["status"] in {"connected", "connected_partial"}
        assert (
            client.post(
                server.url("/connect"),
                data={**payload, "action": "collect", "csrf": "wrong"},
                headers={"Origin": server.origin},
            ).status_code
            == 403
        )


@pytest.mark.parametrize("path", ["collect", "run"])
def test_real_analyser_uses_latest_profile_in_collect_and_run(env, monkeypatch, path):
    bind(env)
    stale = current(env)["profile"]["evidence_id"]
    _, profile_id, _ = store.set_profile(env.root, "synthetic", {"phones": ["0299991111"]})
    assert stale != profile_id
    store.set_profile(env.root, "other", {"phones": ["0299992222"]})
    public = Evidence(
        id="ev-presence-001",
        source="presence",
        url="https://example.test",
        description="NAP sample",
        data={
            "entities": [
                {
                    "name": PROFILE["name"],
                    "page": "https://example.test",
                    "telephone": ["0299993333"],
                }
            ]
        },
    )
    if path == "collect":
        snapshot = current(env)
        public.id = "ev-presence-r2-001"
        snapshot["evidence"].append(public.model_dump())
        store.save(env.root, "synthetic", snapshot)
        collect(env)
    else:

        class PublicModule:
            name = "presence"
            categories = {"presence"}

            def run(self, ctx):
                return ModuleResult(name="presence", evidence=[public], collection_started=True)

        monkeypatch.setattr(
            runner, "REGISTRY", {"presence": PublicModule(), "gbp": ConnectorModule(gbp.CONNECTOR)}
        )
        monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
        ctx = Context(
            "synthetic",
            "Synthetic",
            "https://example.test",
            {"presence"},
            {"gbp"},
            {},
            create_run_dir(env.root, "synthetic"),
            env.root,
            store_root=env.root,
        )
        outcome = runner.run(ctx, no_ai=True, formats={"md"})
        assert outcome.get("store_status") != "failed"
    saved = current(env)
    location = next(
        e
        for e in saved["evidence"]
        if e["source"] == "gbp" and e["data"]["operation"] == "location"
    )["data"]["data"]
    comparison = next(c for c in location["profile_comparisons"] if c["field"] == "phone")
    assert comparison["profile_reference"] == profile_id
    assert comparison["source_provenance"] == "customer_statement"
    assert comparison["supplied_values"] == ["0299991111"]
    assert comparison["differs"]
    assert any(c.get("field") == "phone" for c in location["website_comparisons"])
    keys = {f["check_key"] for f in saved["findings"]}
    assert "gbp:profile-phone" in keys
    assert any(k.startswith("gbp:public-phone:") for k in keys)
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text().split("## 7.")[1]
    assert "GBP phone differs from supplied profile" in report
    assert "not which value is correct" in report


def test_finding_lifecycle_failure_missing_pass_and_recurrence(env):
    bind(env)
    collect(env)

    def hours():
        return next(f for f in current(env)["findings"] if f["check_key"] == "gbp:hours-missing")

    first_id = hours()["id"]
    assert hours()["lifecycle_status"] == "current"
    env.provider.override = lambda r: httpx.Response(403)
    collect(env)
    assert hours()["lifecycle_status"] == "not_rechecked"
    env.provider.override = lambda r: None
    env.provider.location["regularHours"] = {
        "periods": [
            {
                "openDay": "MONDAY",
                "closeDay": "MONDAY",
                "openTime": {"hours": 9},
                "closeTime": {"hours": 17},
            }
        ]
    }
    collect(env)
    assert hours()["id"] == first_id and hours()["lifecycle_status"] == "resolved"
    assert hours()["resolution"]["evidence_ids"]
    del env.provider.location["regularHours"]
    collect(env)
    assert hours()["id"] == first_id and hours()["lifecycle_status"] == "current"
    assert hours()["events"][-1]["status"] == "reopened"


def test_invalid_period_and_rebinding_never_reuse_old_results(env):
    bind(env)
    before = (env.root / "synthetic/assessment.json").read_bytes()
    with pytest.raises(ValueError):
        service.execute(env.root, "synthetic", "gbp", collect=True, days=367)
    assert not env.provider.calls
    assert before == (env.root / "synthetic/assessment.json").read_bytes()
    collect(env)
    service.set_binding(env.root, "synthetic", "gbp", "synthetic", "locations/999999")
    state = current(env)["connectors"]["gbp"]
    assert state["status"] == "not_configured" and not state.get("last_successful_collection")
    assert any(e["source"] == "gbp" for e in current(env)["evidence"])
