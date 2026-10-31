"""Sanitised hosting interstitials; all HTTP is local or MockTransport."""

import json
import time
from pathlib import Path

import httpx
import pytest
from test_security_siteone_pinning import fake_crawler
from test_site_checks import browser as browser_evidence
from test_site_checks import context, run_checks, source

from ams_assess import cli, coverage, network, outcomes, store
from ams_assess.lifecycle import observe, scoped, transition
from ams_assess.models import Finding, ModuleResult
from ams_assess.modules import browser, presence, security, site_checks, siteone
from ams_assess.report import render_markdown
from ams_assess.targets import NORMAL_UA

REASON = "blocked by hosting bot protection (challenge served)"
BODY = (
    '<html><head><link rel="icon" href="data:;"><meta http-equiv="refresh" '
    'content="0;/.well-known/sgcaptcha/?r=%2F&y=ipr:fixture"></meta></head></html>'
)
HEADERS = {
    "sg-captcha": "challenge",
    "x-robots-tag": "noindex",
    "cache-control": "no-store",
    "content-type": "text/html",
}
URL = "https://example.com/"


def response(**changes):
    return {
        "url": URL,
        "status": 202,
        "headers": HEADERS,
        "body": BODY,
        "complete": True,
        **changes,
    }


@pytest.mark.parametrize(
    "sample",
    [
        response(),
        response(headers={}),
        response(status=200, headers={"cf-mitigated": "challenge"}, body=""),
        response(status=503, headers={}, body="<script>window.__cf_chl_test = 1</script>"),
        response(
            status=200,
            headers={},
            body="<title>Just a moment...</title>Enable JavaScript and cookies to continue",
        ),
        response(headers={}, body='<meta content="0;/verify" http-equiv="refresh">'),
    ],
)
def test_site_checks_challenge_not_noindex_or_pass(tmp_path, monkeypatch, sample):
    result, rows = run_checks(
        context(tmp_path, categories={"seo", "technical"}), monkeypatch, {"normal": sample}
    )
    assert rows["noindex"]["status"] == "NOT ASSESSED"
    assert REASON in rows["noindex"]["checked"]
    assert not result.findings
    assert not any(o.status == "pass" for o in result.outcomes)


def test_probe_keeps_header_challenge_evidence(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"sg-captcha": "challenge", "content-type": "text/html"},
            stream=httpx.ByteStream(b"<html></html>"),
        )
    )
    with httpx.Client(transport=transport) as client:
        sample = site_checks.probe(client, URL, NORMAL_UA, time.monotonic() + 10)
    assert sample["headers"]["sg-captcha"] == "challenge"


@pytest.mark.parametrize(
    "body",
    [
        "<h1>Real page</h1>",
        "<title>Just a moment...</title><p>Our article</p>",
        '<meta http-equiv="refresh" content="0;/new-page">',
    ],
)
def test_real_200_noindex_is_still_a_finding(tmp_path, monkeypatch, body):
    result, rows = run_checks(
        context(tmp_path, categories={"seo"}),
        monkeypatch,
        {
            "normal": response(
                status=200,
                headers={"x-robots-tag": "noindex", "content-type": "text/html"},
                body=body,
            )
        },
    )
    assert rows["noindex"]["status"] == "ERROR"
    assert any(f.check_key == "site:noindex" for f in result.findings)


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (202, HEADERS, BODY),
        (202, {"content-type": "text/html"}, BODY),
        (200, {"cf-mitigated": "challenge", "content-type": "text/html"}, "<html></html>"),
    ],
)
def test_browser_challenge_is_blocked_before_axe_and_refresh(
    tmp_path, http_server, monkeypatch, status, headers, body
):
    fixture = http_server(
        lambda request: (
            (status, headers, body.encode())
            if request["path"] == "/"
            else (200, {"content-type": "text/html"}, b"<html>Redirect destination</html>")
        )
    )
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "axe.min.js").write_text("throw new Error('axe must not run')")
    url = fixture.origin + "/"
    ctx = context(tmp_path, target=url, resolved_url=url, pages=[url], tools_dir=tools)
    result = browser.run(ctx)
    assert not result.findings
    assert all(o.status == "unavailable" and o.reason == REASON for o in result.outcomes)
    assert "1 pages blocked by hosting bot protection" in result.reason
    assert all(e.data.get("hosting_challenge") for e in result.evidence)
    assert len(fixture.received) == 2  # one original GET per viewport, no meta refresh


def test_challenge_recheck_cannot_resolve_prior_finding():
    module = source("siteone", [(URL, "SiteOne HTTP response", response())])
    eid = module.evidence[0].id
    finding = scoped(
        [
            Finding(
                id="previous",
                title="Missing title",
                category="seo",
                check_key="missing-title",
                evidence_ids=[eid],
                detail="Real prior observation",
                recommendation="Add title",
                component=URL,
            )
        ],
        URL,
        "test",
    )[0]
    # Even a legacy or imported PASS incorrectly backed by a challenge must not resolve.
    outcome = observe("siteone", "missing-title", "seo", URL, "pass", [eid])
    outcome.site = finding.site
    after = transition([finding], [], [outcome], module.evidence, "r2", "2026-11-01")
    assert after[0].lifecycle_status == "not_rechecked"
    assert after[0].resolution is None


def test_load_withdraws_only_challenge_basis_preserving_ids_backup_and_history(tmp_path):
    module = source(
        "site_checks", [(URL, "Bounded site-check HTTP probes", {"normal": response()})]
    )
    eid = module.evidence[0].id
    derived = module.add_evidence(URL, "Site check: Page noindex", {"source_evidence_ids": [eid]})
    real = module.add_evidence(
        URL,
        "Real page",
        response(status=200, headers={"x-robots-tag": "noindex"}, body="<h1>Real</h1>"),
    )

    def finding(key, refs):
        return Finding(
            id=key,
            title=key,
            category="seo",
            check_key="site:noindex",
            evidence_ids=refs,
            detail="noindex",
            recommendation="Review",
        ).model_dump()

    data = store.new_store("test", "Test")
    data["evidence"] = [e.model_dump() for e in module.evidence]
    data["findings"] = [
        finding("false-positive", [derived]),
        finding("real", [real]),
        finding("mixed", [derived, real]),
    ]
    store.save(tmp_path, "test", data)
    path = store.store_file(tmp_path, "test")
    original = path.read_bytes()
    loaded = store.load(tmp_path, "test", prune_backups=False)
    assert loaded is not None
    assert [f["id"] for f in loaded["findings"]] == ["real", "mixed"]
    assert loaded["findings"] == data["findings"][1:]
    retired = loaded["finding_history"][0]
    assert retired["id"] == "false-positive" and retired["status"] == "superseded"
    assert REASON in retired["reason"]
    assert next(path.parent.glob("assessment.json.backup-*")).read_bytes() == original
    again = store.load(tmp_path, "test", prune_backups=False)
    assert again is not None and again["finding_history"] == loaded["finding_history"]
    assert json.loads(path.read_text())["findings"] == loaded["findings"]


def test_siteone_excludes_challenge_pages_and_aggregates(tmp_path, monkeypatch):
    ctx = context(tmp_path, categories={"seo", "technical"})
    rows = [response(type=1), {"url": URL + "about", "status": "200", "type": 1}]
    tables = {
        "seo": {
            "rows": [
                {
                    "urlPathAndQuery": "/",
                    "title": "",
                    "h1": "",
                    "description": "",
                    "robotsIndex": "0",
                }
            ]
        },
        "security": {"rows": [{"header": "x-test", "critical": 1, "warning": 0}]},
    }
    fake_crawler(monkeypatch, ctx, rows, tables)
    result = siteone.run(ctx)
    assert ctx.pages == [URL + "about"]
    assert not result.findings
    assert any(o.status == "unavailable" and o.reason == REASON for o in result.outcomes)
    assert not any(o.status == "pass" and o.component == URL for o in result.outcomes)


def test_presence_does_not_parse_challenge_pages(tmp_path):
    module = browser_evidence({URL: {"title": "Challenge", "meta": []}})
    module.evidence[0].data.update(response())
    assert presence.page_evidence(module) == []
    ctx = context(tmp_path, results=[module])
    result = presence.run(ctx)
    assert result.status == "skipped" and result.reason == REASON


def test_presence_sitemap_challenge_not_invalid_xml(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=HEADERS, content=BODY)
    )
    data = presence.sitemap_check(context(tmp_path), transport)
    assert data["status"] == "NOT ASSESSED" and data["detail"] == REASON
    assert not data["robots_complete"]


def test_security_challenge_has_no_header_findings_or_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(
        security,
        "public_get",
        lambda *a, **k: httpx.Response(
            202, headers=HEADERS, content=BODY, request=httpx.Request("GET", "http://example.com/")
        ),
    )
    ctx = context(tmp_path, target="http://example.com/", resolved_url="http://example.com/")
    result = security.run(ctx)
    outcomes.record(ctx, result)
    assert not result.findings
    assert not any(o.status == "pass" for o in result.outcomes)
    assert REASON in result.reason


def test_coverage_reports_challenged_urls_separately(tmp_path, monkeypatch):
    module = ModuleResult(name="browser")
    for _ in range(2):
        module.add_evidence(URL, "Browser page load unavailable", response())
    ctx = context(tmp_path, results=[module])
    monkeypatch.setattr(
        coverage,
        "walk_sitemaps",
        lambda *a: {"locs": [], "docs": [], "stops": [], "excluded": [], "roots": []},
    )
    data = coverage.collect(ctx)
    assert data["hosting_blocked"] == [URL]
    assert not data["failed"] and not data["browser_tested"]
    assert "1 pages blocked by hosting bot protection" in data["summary"]
    saved = store.new_store("test", "Test")
    saved["evidence"] = [e.model_dump() for e in module.evidence]
    report_data, evidence, _ = store.report_data(saved)
    report_data["website_coverage"] = data
    assert REASON in render_markdown(report_data, evidence)
    report_data.pop("website_coverage")
    report_data["website_coverage_runs"] = [{**data, "run_id": "r1", "target": URL}]
    assert REASON in render_markdown(report_data, evidence)
    monkeypatch.setattr(store, "load", lambda *a: saved)
    monkeypatch.setattr(store, "report_data", lambda *a: (report_data, evidence, None))
    assert REASON in "\n".join(cli.customer_summary(tmp_path, "test"))


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (202, HEADERS, BODY),
        (202, {"content-type": "text/html"}, BODY),
        (
            200,
            {"content-type": "text/html"},
            "<title>Just a moment...</title>Enable JavaScript and cookies to continue",
        ),
    ],
)
def test_real_siteone_exports_challenge_signatures(
    tmp_path, http_server, monkeypatch, status, headers, body
):
    fixture = http_server(lambda request: (status, headers, body.encode()))
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    url = fixture.origin + "/"
    ctx = context(
        tmp_path,
        target=url,
        resolved_url=url,
        tools_dir=Path(__file__).resolve().parents[1] / "tools",
    )
    monkeypatch.setattr(siteone, "resolve_target", lambda _: {"resolved": url})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda *a: [])
    result = siteone.run(ctx)
    assert not ctx.pages and not result.findings
    assert REASON in result.reason
    assert any(o.reason == REASON for o in result.outcomes)


def test_legacy_noindex_with_healthy_coverage_is_withdrawn(tmp_path):
    module = ModuleResult(name="site_checks")
    probes = module.add_evidence(
        URL,
        "Bounded site-check HTTP probes",
        {"normal": response(), "search": response(status=200, headers={}, body="Real page")},
    )
    real = module.add_evidence(URL, "SiteOne SEO metadata", {"robotsIndex": "1"})
    row = module.add_evidence(
        URL,
        "Site check: Page noindex / X-Robots-Tag",
        {
            "site_check": "noindex",
            "source_evidence_ids": [probes, real],
            "detail": {"pages": [{"url": URL, "source": "X-Robots-Tag: noindex"}]},
        },
    )
    finding = Finding(
        id="keep-id",
        title="Page noindex",
        category="seo",
        evidence_ids=[row],
        detail="noindex",
        recommendation="Review",
    )
    saved = store.new_store("test", "Test")
    saved["evidence"] = [e.model_dump() for e in module.evidence]
    saved["findings"] = [finding.model_dump()]
    store.save(tmp_path, "test", saved)
    result = store.load(tmp_path, "test", prune_backups=False)
    assert result is not None and not result["findings"]
    assert result["finding_history"][0]["id"] == "keep-id"


def test_real_noindex_survives_challenged_other_probe(tmp_path, monkeypatch):
    result, rows = run_checks(
        context(tmp_path, categories={"seo"}),
        monkeypatch,
        {
            "normal": response(status=200, headers={"x-robots-tag": "noindex"}, body="Real page"),
            "search": response(),
        },
    )
    assert rows["noindex"]["status"] == "ERROR"
    assert any(f.check_key == "site:noindex" for f in result.findings)
