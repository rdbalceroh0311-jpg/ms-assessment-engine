"""Regressions from live validation, reproduced only with loopback and mocked providers."""

import http.server
import json
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ams_assess import ai, presence_discovery, runner, targets
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir
from ams_assess.modules import browser, dataforseo, presence, site_checks, siteone
from ams_assess.report import connection_rows, discovery_rows, mdtext, render_markdown

REPO = Path(__file__).resolve().parents[1]
CREDENTIALS = {"DATAFORSEO_LOGIN": "operator@example.invalid", "DATAFORSEO_PASSWORD": "secret-xyz"}


def context(tmp_path, **changes):
    return replace(
        Context(
            customer_id="synthetic",
            customer_name="Synthetic Plumbing",
            target="https://example.com/",
            categories={"seo", "presence"},
            connections=set(),
            config=dict(CREDENTIALS),
            run_dir=create_run_dir(tmp_path, "synthetic"),
            tools_dir=REPO / "tools",
            dataforseo_max_usd=1.0,
            profile={
                "name": "Synthetic Plumbing",
                "industry": "trades",
                "buying_model": "service-area",
            },
        ),
        **changes,
    )


def mock_resolution(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw)
    )


def test_apex_www_resolution_and_redirect_limits(monkeypatch):
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        if request.url.host == "example.co.uk":
            return httpx.Response(301, headers={"location": "https://www.example.co.uk/"})
        return httpx.Response(200)

    mock_resolution(monkeypatch, redirect)
    result = targets.resolve_target("example.co.uk")
    assert result["resolved"] == "https://www.example.co.uk/"
    assert calls == ["https://example.co.uk/", "https://www.example.co.uk/"]
    assert [r["http_status"] for r in result["chain"]] == [301, 200]
    assert targets.crawl_hosts(calls[0], calls[1]) == {"example.co.uk", "www.example.co.uk"}
    assert not targets.same_site("https://a.co.uk/", "https://b.co.uk/")
    assert not targets.same_site("https://a.github.io/", "https://b.github.io/")


@pytest.mark.parametrize(
    "destination",
    [
        "https://other.example.net/",
        "https://example.com.evil.net/",
        "http://www.example.com/",
        "https://user:secret@www.example.com/",
        "https://www.example.com/?token=secret",
    ],
)
def test_resolution_refuses_unsafe_redirect_before_request(monkeypatch, destination):
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(301, headers={"location": destination})

    mock_resolution(monkeypatch, redirect)
    result = targets.resolve_target("example.com")
    assert not result["resolved"] and result["error"]
    assert len(calls) == 1 and "secret" not in json.dumps(result)


@pytest.mark.parametrize("hops", [5, 6])
def test_resolution_follows_at_most_five_hops(monkeypatch, hops):
    calls = []

    def redirect(request):
        n = int(request.url.path.strip("/") or 0)
        calls.append(n)
        return (
            httpx.Response(302, headers={"location": f"/{n + 1}"})
            if n < hops
            else httpx.Response(200)
        )

    mock_resolution(monkeypatch, redirect)
    result = targets.resolve_target("https://example.com/0")
    assert len(calls) == 6
    if hops == 5:
        assert result["resolved"] == "https://example.com/5"
    else:
        assert not result["resolved"] and "5 hops" in result["error"]


def test_resolution_prefers_https_and_detects_loops(monkeypatch):
    calls = []
    loop = False

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "/"}) if loop else httpx.Response(200)

    mock_resolution(monkeypatch, handler)
    assert targets.resolve_target("http://example.com/")["resolved"] == "https://example.com/"
    assert calls == ["https://example.com/"]
    loop = True
    result = targets.resolve_target("example.com")
    assert not result["resolved"] and result["error"] == "Target redirect loop"
    assert len(calls) == 2


def test_redirect_host_change_crawls_both_hosts_and_feeds_downstream(tmp_path, monkeypatch):
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            host = self.headers["Host"]
            requests.append((host, self.path))
            if host.startswith("127.0.0.1") and self.path == "/":
                self.send_response(301)
                self.send_header("Location", f"http://localhost:{server.server_port}/")
                self.end_headers()
                return
            if self.path not in {"/", "/about.html"}:
                self.send_error(404)
                return
            body = (
                '<html lang="en"><head><title>Fixture</title></head><body><h1>Local fixture</h1>'
                f'<a href="http://127.0.0.1:{server.server_port}/about.html">About</a>'
                "<p>Synthetic plumbing services.</p></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = httpx.HTTPTransport.handle_request

    def local_only(self, request):
        assert request.url.host in {"localhost", "127.0.0.1"}
        return original(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", local_only)
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "siteone": siteone,
            "browser": browser,
            "presence": presence,
            "site_checks": site_checks,
        },
    )
    try:
        target = f"http://127.0.0.1:{server.server_port}/"
        ctx = context(
            tmp_path, target=target, categories={"seo", "presence"}, config={}, max_pages=10
        )
        data = runner.run(ctx, no_ai=True, formats={"md"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert ctx.target == data["target"] == target
    assert ctx.site_url == data["resolved_url"] == f"http://localhost:{server.server_port}/"
    assert any(u.startswith("http://localhost:") for u in ctx.pages)
    assert any(u.startswith("http://127.0.0.1:") and u.endswith("about.html") for u in ctx.pages)
    modules = {m["name"]: m for m in data["modules"]}
    assert all(modules[name]["status"] == "ok" for name in modules)
    chain = modules["siteone"]["evidence"][0]["data"]
    assert chain["requested"] == target and [r["http_status"] for r in chain["chain"]] == [301, 200]
    assert modules["browser"]["evidence"] and modules["presence"]["evidence"]
    assert any(row["status"] != "NOT ASSESSED" for row in modules["site_checks"]["checks"])
    assert all(e["url"] == ctx.site_url for e in modules["site_checks"]["evidence"])


@pytest.mark.parametrize("exit_code", [0, 3])
def test_siteone_redirect_only_is_an_explicit_failure(tmp_path, monkeypatch, exit_code):
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target, "chain": []})
    monkeypatch.setattr(
        siteone.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=exit_code,
            stderr="",
            stdout=json.dumps({"results": [{"url": "https://example.com/", "status": 301}]}),
        ),
    )
    ctx = context(tmp_path)
    result = siteone.run(ctx)
    assert result.status == "error" and "crawled only a redirect" in result.reason
    assert not ctx.pages


@pytest.mark.parametrize(
    ("http_status", "code", "task_level"),
    [
        (401, 40100, False),
        (402, 40200, False),
        (403, 40104, False),
        (200, 40104, False),
        (200, 40104, True),
        (200, 40200, True),
        (200, 40300, True),
    ],
)
@pytest.mark.parametrize("first", ["overview", "serp"])
def test_provider_denial_stops_all_paid_calls_and_explains_readiness(
    tmp_path,
    monkeypatch,
    http_status,
    code,
    task_level,
    first,
):
    calls = []
    message = "Please verify your account before using the API."

    def rejected(url, **kwargs):
        calls.append(url)
        status = {"status_code": code, "status_message": message}
        body = {"status_code": 20000, "tasks": [status]} if task_level else status
        return httpx.Response(http_status, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx, "post", rejected)
    ctx = context(tmp_path)
    if first == "overview":
        result = dataforseo.run(ctx)
        ctx.results = [result]
        assert result.status == "error" and message in result.reason
    else:
        result = ModuleResult(name="presence")
        presence_discovery.discover(ctx, result, presence.site_identity([]), {}, True)
        ctx.results = [result]
    assert len(calls) == 1
    assert ctx.dataforseo_stopped and ctx.dataforseo_spent_usd == 0
    assert dataforseo.serp(ctx, "organic", "another query", 1e12)["status"] == "refused"
    assert dataforseo.run(ctx).status == "skipped"
    assert len(calls) == 1
    conn = result.connections[0]
    assert (
        conn.status == "Permission required" and message in conn.detail and str(code) in conn.detail
    )
    assert str(code) in json.dumps([e.model_dump() for e in result.evidence])
    rows = connection_rows({"modules": [result.model_dump()], "target": ctx.target})
    assert rows[0]["status"] == "Permission required" and message in rows[0]["detail"]


@pytest.mark.parametrize("response_kind", ["timeout", "500", "408", "unknown_task"])
def test_uncertain_provider_outcomes_keep_reservation(tmp_path, monkeypatch, response_kind):
    def uncertain(url, **kwargs):
        if response_kind == "timeout":
            raise httpx.ReadTimeout("credential-bearing text must not appear")
        body = {"status_code": 50000, "cost": 0}
        status = int(response_kind) if response_kind.isdigit() else 200
        return httpx.Response(status, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx, "post", uncertain)
    ctx = context(tmp_path)
    result = dataforseo.run(ctx)
    assert result.status == "error"
    assert ctx.dataforseo_spent_usd == dataforseo.ESTIMATE_USD
    outcome = dataforseo.serp(ctx, "organic", "plain search", 1e12)
    assert outcome["status"] == "error"
    assert ctx.dataforseo_spent_usd == dataforseo.ESTIMATE_USD + Decimal("0.003")
    assert "credential-bearing" not in json.dumps(outcome)


def test_provider_messages_are_sanitised_and_later_denial_updates_readiness(tmp_path, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            body = {
                "status_code": 20000,
                "cost": 0.01,
                "tasks": [{"status_code": 20000, "result": []}],
            }
            return httpx.Response(200, request=httpx.Request("POST", url), json=body)
        body = {
            "status_code": 40104,
            "status_message": (
                "Please verify your account before using the API. "
                "operator@example.invalid secret-xyz password=hidden https://user:secret@example.com/"
            ),
        }
        return httpx.Response(403, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx, "post", post)
    ctx = context(tmp_path)
    overview = dataforseo.run(ctx)
    assert overview.connections[0].status == "Connected successfully"
    ctx.results = [overview]
    result = ModuleResult(name="presence")
    presence_discovery.discover(ctx, result, presence.site_identity([]), {}, True)
    assert len(calls) == 2 and ctx.dataforseo_spent_usd == Decimal("0.01")
    conn = overview.connections[0]
    assert conn.status == "Permission required" and "40104" in conn.detail
    serialised = json.dumps([overview.model_dump(), result.model_dump()])
    for secret in [*CREDENTIALS.values(), "hidden", "user:secret"]:
        assert secret not in serialised
    assert "Please verify your account before using the API." in serialised
    # Some failures have no JSON body; HTTP status prose must be sanitised too.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            403,
            request=httpx.Request("POST", url),
            text="Not JSON",
            extensions={"reason_phrase": b"operator@example.invalid secret-xyz"},
        ),
    )
    rejected = dataforseo.run(context(tmp_path))
    assert rejected.connections[0].status == "Permission required"
    assert all(secret not in rejected.model_dump_json() for secret in CREDENTIALS.values())


def measurement_evidence():
    return [
        Evidence(
            id="ev-performance-001",
            source="performance",
            url="https://example.com/",
            description="Mobile lab",
            data={
                "strategy": "mobile",
                "score": 0.67,
                "metrics": {
                    "largest-contentful-paint": {"numericValue": 3600, "numericUnit": "millisecond"}
                },
            },
        ),
        Evidence(
            id="ev-performance-002",
            source="performance",
            url="https://example.com/",
            description="Desktop lab",
            data={"strategy": "desktop", "score": 0.83},
        ),
        Evidence(
            id="ev-observatory-001",
            source="observatory",
            url="https://example.com/",
            description="Public grade",
            data={"grade": "F", "tests_failed": 5, "tests_quantity": 12},
        ),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Lighthouse lab scores were 67/100 on mobile and 83/100 on desktop. Mobile LCP was 3.6 s.",
        "Mobile LCP was 3600 ms and the lab score was 67 out of 100.",
        "Observatory graded the domain F with 5 of 12 tests failed.",
    ],
)
def test_ai_accepts_recorded_scores_durations_and_counts(text):
    assert not ai.unsupported_claim(text, measurement_evidence())


@pytest.mark.parametrize(
    "text",
    [
        "Lab scores were 68/100 and 83/100.",
        "Mobile LCP was 3.7 s.",
        "Mobile LCP was 3600 s.",
        "Observatory recorded 6 of 12 tests failed.",
        "$67 in revenue",
        "ROI was 67%",
        "Guaranteed rankings with 67/100.",
        "This will increase leads.",
        "5 lost leads",
        "67 leads generated",
        "Ranking position is 5",
    ],
)
def test_ai_rejects_invented_or_promised_measurements(text):
    assert ai.unsupported_claim(text, measurement_evidence())


def test_ai_measurements_must_be_in_cited_data_not_ids_or_unrelated_units():
    all_evidence = measurement_evidence()
    assert ai.unsupported_claim("83/100", all_evidence[:1])
    assert ai.unsupported_claim("There were 1 failed checks", all_evidence[:1])
    counts = [
        Evidence(
            id="ev-test-001",
            source="test",
            url="https://example.com/67",
            description="3600 ms",
            data={"count": 67, "other_count": 3600},
        )
    ]
    assert ai.unsupported_claim("67/100", counts)
    assert ai.unsupported_claim("3.6 seconds", counts)
    assert not ai.unsupported_claim("67 checked items", counts)


def test_valid_cro_finding_and_summary_survive_failed_crawl(tmp_path, monkeypatch, caplog):
    ctx = context(tmp_path, categories={"seo", "performance", "security", "cro_ux"}, config={})
    evidence = measurement_evidence()
    crawl = ModuleResult(name="siteone", status="error", reason="SiteOne crawled only a redirect")
    crawl.add_evidence(ctx.target, "Failed crawl", {"outcome": "redirect only; no pages assessed"})
    checks = ModuleResult(name="site_checks")
    for n in (11, 12, 13):
        checks.evidence.append(
            Evidence(
                id=f"ev-site_checks-{n:03d}",
                source="site_checks",
                url=ctx.target,
                description="Conversion journey coverage",
                data={"status": "NOT ASSESSED", "forms": "NOT TESTED"},
            )
        )
    modules = [
        crawl,
        ModuleResult(name="performance", evidence=evidence[:2]),
        ModuleResult(name="observatory", evidence=evidence[2:]),
        checks,
    ]
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {m.name: SimpleNamespace(categories=ctx.categories, run=lambda _, m=m: m) for m in modules},
    )
    valid = {
        "id": "cro-journey-unverified",
        "title": "Enquiry journey needs verification",
        "category": "cro_ux",
        "severity": "info",
        "kind": "opportunity",
        "confidence": "needs_verification",
        "evidence_ids": ["ev-site_checks-012", "ev-site_checks-013", "ev-site_checks-011"],
        "detail": "The enquiry journey and forms were not tested.",
        "recommendation": "Arrange an owner-approved journey test before proposing changes.",
    }
    summary = (
        "The crawl returned 0 successful HTML pages (ev-siteone-002). "
        "Mobile lab score was 67/100 with LCP of 3.6 s (ev-performance-001). "
        "Observatory graded the domain F with 5 of 12 tests failed (ev-observatory-001)."
    )

    def claude(*args, **kwargs):
        payload = json.loads(kwargs["input"].split("\nDATA:\n")[1])
        assert payload["collection"]["successful_html_pages"] == 0
        assert payload["collection"]["modules"][0]["status"] == "error"
        coverage = next(e for e in payload["evidence"] if e["id"] == "ev-siteone-002")
        assert json.loads(coverage["data"])["successful_html_pages"] == 0
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "structured_output": {
                        "executive_summary": summary,
                        "sections": {
                            "performance": "Mobile score was 67/100 (ev-performance-001)."
                        },
                        "findings": [
                            valid,
                            {**valid, "id": "bad", "ams_service": "unsupported category"},
                        ],
                    }
                }
            ),
        )

    monkeypatch.setattr(ai.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(ai.subprocess, "run", claude)
    data = runner.run(ctx, formats={"md"})
    assert data["ai"]["status"] == "ok" and data["ai"]["executive_summary"] == summary
    assert data["findings"][0]["id"] == valid["id"]
    assert data["findings"][0]["confidence"] == "needs_verification"
    assert summary in (ctx.run_dir / "report.md").read_text()
    assert "ValueError: AMS service must be one of the four categories" in caplog.text


def test_report_grouping_preserves_priority_and_held_site_check_evidence():
    def platform(label, relevance, state="Not assessed"):
        return {
            "platform": label,
            "relevance": relevance,
            "state": state,
            "detail": "Not searched: access required",
            "candidates": [],
            "evidence_ids": ["ev-p-001"],
        }

    rows = discovery_rows(
        [
            platform("hipages", "high"),
            platform("True Local", "medium"),
            platform("Houzz", "medium"),
            platform("YouTube", "low"),
            platform("OpenTable", "not_applicable", "Not applicable"),
        ]
    )
    assert [(r["platform"], r["relevance"]) for r in rows[:3]] == [
        ("hipages", "high"),
        ("True Local, Houzz", "medium"),
        ("YouTube", "low"),
    ]
    assert rows[-1]["detail"] == "Not relevant for the recorded industry and buying model."
    assert "results.json" not in json.dumps(rows)
    data = json.loads((REPO / "examples/fixture-report/results.json").read_text())
    evidence = [
        Evidence.model_validate(e)
        for e in json.loads((REPO / "examples/fixture-report/evidence.json").read_text())
    ]
    held = next(f for f in data["findings"] if f["check_key"].startswith("site:"))
    data["needs_review"] = [
        {
            "finding_id": held["id"],
            "title": held["title"],
            "reason": "Review",
            "evidence_ids": held["evidence_ids"],
        }
    ]
    text = render_markdown(data, evidence, data.get("discovery"))
    assert f"#### {mdtext(held['id'])}:" not in text
    assert f"| {mdtext(held['title'])} (" in text
    assert f"**Needs review:** {held['id']}:" in text
