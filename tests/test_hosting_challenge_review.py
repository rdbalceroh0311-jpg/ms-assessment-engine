"""Review regressions: ordinary Cloudflare pages, evidence scope and bounded analysis."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_hosting_challenges import REASON, URL, response
from test_security_siteone_pinning import fake_crawler
from test_site_checks import context, crawl, run_checks, sitemap, source

from ams_assess import challenges, network, store
from ams_assess.lifecycle import scoped, transition
from ams_assess.models import Evidence, Finding, ModuleResult
from ams_assess.modules import browser, security, site_checks, siteone

JSD = (
    "<script>var a=document.createElement('script');"
    "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>"
)
CACHED = '<a href="/cached?__cf_chl_jschl_tk__=cached-token">Cached link</a>'
TITLE = "<title>Just a moment...</title>Enable JavaScript and cookies to continue"
PAGE = (
    '<!doctype html><html lang="en"><head><title>Real page</title></head>'
    "<body><h1>One</h1><h1>Two</h1><button></button>{}</body></html>"
)


@pytest.mark.parametrize(
    "status,body",
    [
        (200, JSD),
        (200, CACHED),
        (200, TITLE),
        (200, "<script>window._cf_chl_opt = {}</script>"),
        (403, JSD),
        (503, CACHED),
    ],
    ids=["200-jsd", "200-cached-link", "200-title", "200-opt", "403-jsd", "503-cached-link"],
)
def test_cloudflare_markers_alone_are_not_challenges(status, body):
    assert not challenges.challenged(response(status=status, headers={}, body=body))


@pytest.mark.parametrize(
    "sample",
    [
        response(status=200, headers={"cf-mitigated": "challenge"}, body=""),
        response(status=403, headers={}, body="<script>var _cf_chl_opt = {};</script>"),
        response(status=503, headers={}, body="<script>var _cf_chl_opt = {};</script>"),
        response(status=503, headers={}, body=TITLE),
    ],
    ids=["explicit-header", "403-opt", "503-opt", "non200-title-verification"],
)
def test_cloudflare_challenge_shapes(sample):
    assert challenges.challenged(sample)


@pytest.mark.parametrize("marker", [JSD, CACHED], ids=["jsd", "cached-link"])
@pytest.mark.parametrize("noindex", [False, True], ids=["indexable", "noindex"])
def test_ordinary_cloudflare_site_checks_keep_real_findings(tmp_path, monkeypatch, marker, noindex):
    headers = {"content-type": "text/html", **({"x-robots-tag": "noindex"} if noindex else {})}
    ctx = context(tmp_path, categories={"seo"})
    result, rows = run_checks(
        ctx,
        monkeypatch,
        {"normal": response(status=200, headers=headers, body=PAGE.format(marker))},
    )
    assert REASON not in result.reason
    assert rows["page-quality"]["status"] == "WARNING"  # Real duplicate H1s.
    assert any(f.check_key == "site:noindex" for f in result.findings) is noindex


@pytest.mark.parametrize("marker", [JSD, CACHED], ids=["jsd", "cached-link"])
@pytest.mark.parametrize("noindex", [False, True], ids=["indexable", "noindex"])
def test_ordinary_cloudflare_security_keeps_missing_headers(tmp_path, monkeypatch, marker, noindex):
    url = "http://example.com/"
    headers = {"content-type": "text/html", **({"x-robots-tag": "noindex"} if noindex else {})}
    monkeypatch.setattr(
        security,
        "public_get",
        lambda *a, **k: httpx.Response(
            200, headers=headers, content=PAGE.format(marker), request=httpx.Request("GET", url)
        ),
    )
    result = security.run(context(tmp_path, target=url))
    assert REASON not in result.reason
    assert any(f.check_key == "header-missing:content-security-policy" for f in result.findings)
    assert not any(
        e.data.get("hosting_challenge") for e in result.evidence if isinstance(e.data, dict)
    )


@pytest.mark.parametrize("marker", [JSD, CACHED], ids=["jsd", "cached-link"])
@pytest.mark.parametrize("noindex", [False, True], ids=["indexable", "noindex"])
def test_ordinary_cloudflare_browser_keeps_page_and_axe_findings(
    tmp_path, http_server, monkeypatch, marker, noindex
):
    headers = {"content-type": "text/html", **({"x-robots-tag": "noindex"} if noindex else {})}
    fixture = http_server(lambda request: (200, headers, PAGE.format(marker).encode()))
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    url = fixture.origin + "/"
    result = browser.run(
        context(
            tmp_path,
            target=url,
            pages=[url],
            tools_dir=Path(__file__).resolve().parents[1] / "tools",
        )
    )
    assert result.status == "ok", result.reason
    assert REASON not in result.reason
    pages = [e for e in result.evidence if e.description.startswith("Rendered page evidence")]
    assert len(pages) == 2
    assert all(e.data["page"]["title"] == "Real page" for e in pages)
    assert any(f.check_key == "axe:button-name" for f in result.findings)


def load_finding(tmp_path, modules, eid):
    finding = Finding(
        id="stable-id",
        title="Noindex",
        category="seo",
        check_key="site:noindex",
        evidence_ids=[eid],
        detail="Observed noindex",
        recommendation="Review",
    )
    saved = store.new_store("test", "Test")
    saved["evidence"] = [e.model_dump() for m in modules for e in m.evidence]
    saved["findings"] = [finding.model_dump()]
    store.save(tmp_path, "test", saved)
    loaded = store.load(tmp_path, "test", prune_backups=False)
    assert loaded is not None
    return loaded, finding.model_dump()


@pytest.mark.parametrize(
    "support", ["aggregate-detail-only", "aggregate-also-cited", "other-citation", "other-probe"]
)
def test_migration_retains_genuine_support_alongside_challenged_hit(tmp_path, support):
    crawl_module = source(
        "siteone",
        [
            (
                URL + "real",
                "SiteOne HTTP response",
                response(
                    url=URL + "real", status=200, headers={"x-robots-tag": "noindex"}, body="Real"
                ),
            )
        ],
    )
    aggregate = crawl_module.add_evidence(
        URL, "SiteOne headers-values table", [{"header": "X-Robots-Tag", "value": "noindex"}]
    )
    checks = ModuleResult(name="site_checks")
    probe = checks.add_evidence(URL, "Bounded site-check HTTP probes", {"normal": response()})
    if support == "other-probe":
        checks.evidence[0].data["search"] = response(
            status=200, headers={"x-robots-tag": "noindex"}, body="Real page"
        )
    refs = [probe]
    detail = {"pages": [{"url": URL, "source": "X-Robots-Tag: noindex"}]}
    if support.startswith("aggregate"):
        detail["aggregate_header_evidence"] = [aggregate]
        if support == "aggregate-also-cited":
            refs.append(aggregate)
    elif support == "other-citation":
        refs.append(crawl_module.evidence[0].id)
    row = checks.add_evidence(
        URL,
        "Site check: Page noindex",
        {"site_check": "noindex", "source_evidence_ids": refs, "detail": detail},
    )
    loaded, original = load_finding(tmp_path, [checks, crawl_module], row)
    assert loaded["findings"] == [original]
    assert not loaded["finding_history"]
    assert not list(store.store_file(tmp_path, "test").parent.glob("assessment.json.backup-*"))


def test_migration_retains_genuine_jsd_noindex(tmp_path):
    checks = ModuleResult(name="site_checks")
    probe = checks.add_evidence(
        URL,
        "Bounded site-check HTTP probes",
        {
            "normal": response(
                status=200, headers={"x-robots-tag": "noindex"}, body=PAGE.format(JSD)
            )
        },
    )
    row = checks.add_evidence(
        URL,
        "Site check: Page noindex",
        {
            "site_check": "noindex",
            "source_evidence_ids": [probe],
            "detail": {"pages": [{"url": URL, "source": "X-Robots-Tag: noindex"}]},
        },
    )
    loaded, original = load_finding(tmp_path, [checks], row)
    assert loaded["findings"] == [original]
    assert not loaded["finding_history"]


def test_challenge_basis_scales_linearly_and_keeps_run_url_boundaries():
    def sample(count):
        evidence = []
        expected = set()
        for i in range(count // 2):
            url = URL + str(i)
            run = i % 2 + 1
            blocked = i % 3 == 0
            eid = f"ev-siteone-r{run}-{i * 2:05d}"
            mid = f"ev-siteone-r{run}-{i * 2 + 1:05d}"
            evidence.extend(
                [
                    Evidence(
                        id=eid,
                        source="siteone",
                        url=url,
                        description="SiteOne HTTP response",
                        data=response() if blocked else {"status": 200},
                    ),
                    Evidence(
                        id=mid,
                        source="siteone",
                        url=url,
                        description="SiteOne SEO metadata",
                        data={"robotsIndex": "0"},
                    ),
                ]
            )
            if blocked:
                expected.update([eid, mid])
        start = time.perf_counter()
        assert challenges.challenge_basis(evidence) == expected
        return time.perf_counter() - start

    small, large = sample(2000), sample(6000)
    # Slack for shared CI hosts; the old quadratic scan takes ~9x for 3x the data.
    assert large < max(2.0, small * 5), (small, large)


def test_challenge_basis_groups_same_url_per_run_and_mixed_responses():
    evidence = [
        Evidence(
            id=f"ev-siteone-r{run}-{i}",
            source="siteone",
            url=URL,
            description=description,
            data=data,
        )
        for run, i, description, data in [
            (1, 1, "SiteOne HTTP response", response()),
            (1, 2, "SiteOne SEO metadata", {"robotsIndex": "0"}),
            (2, 1, "SiteOne HTTP response", {"status": 200}),
            (2, 2, "SiteOne SEO metadata", {"robotsIndex": "0"}),
            (3, 1, "SiteOne HTTP response", response()),
            (3, 2, "SiteOne HTTP response", {"status": 200}),
            (3, 3, "SiteOne SEO metadata", {"robotsIndex": "0"}),
        ]
    ]
    assert challenges.challenge_basis(evidence) == {
        "ev-siteone-r1-1",
        "ev-siteone-r1-2",
        "ev-siteone-r3-1",
    }


def test_unrelated_robots_check_passes_and_resolves_with_challenged_homepage(tmp_path):
    ctx = context(tmp_path, categories={"seo"}, results=[sitemap("User-agent: *\nDisallow: /")])
    before = site_checks.Checks(ctx, {})
    before.robots()
    old = scoped(before.result.findings, URL, "test")
    assert len(old) == 1
    ctx.results[0].evidence[0].data["robots_body"] = "User-agent: *\nAllow: /"
    after = site_checks.Checks(ctx, {"normal": response()})
    after.robots()
    assert after.result.checks[0]["status"] == "PASS"
    for outcome in after.result.outcomes:
        outcome.site = old[0].site
    transitioned = transition(
        old, [], after.result.outcomes, after.result.evidence, "r2", "2026-11-01"
    )
    assert transitioned[0].lifecycle_status == "resolved"


def test_unrelated_llms_challenge_does_not_invalidate_homepage_checks(tmp_path, monkeypatch):
    ctx = context(
        tmp_path,
        categories={"seo", "technical"},
        results=[crawl({URL: 200}, {URL: {"robotsIndex": "1"}}), sitemap()],
    )
    normal = response(status=200, headers={"content-type": "text/html"}, body="<html>Real</html>")
    _, rows = run_checks(
        ctx,
        monkeypatch,
        {"normal": normal, "search": normal, "llms": response(url=URL + "llms.txt")},
    )
    assert rows["noindex"]["status"] == rows["firewall"]["status"] == "PASS"
    # A challenge in the actually used normal probe still makes noindex inconclusive.
    _, rows = run_checks(ctx, monkeypatch, {"normal": response()})
    assert rows["noindex"]["status"] == "NOT ASSESSED" and REASON in rows["noindex"]["checked"]


def test_site_checks_reason_has_no_leading_separator(tmp_path, monkeypatch):
    psi = {
        "strategy": "mobile",
        "field_url": {
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2500},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 200},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 10},
            }
        },
    }
    ctx = context(
        tmp_path,
        categories={"performance"},
        results=[source("performance", [(URL, "PageSpeed", psi)])],
    )
    result, rows = run_checks(ctx, monkeypatch, {"normal": response()})
    assert rows["cwv"]["status"] == "PASS"
    assert result.reason == "1 pages " + REASON


def run_siteone_summary(tmp_path, monkeypatch, *, real_page=True, blocked_robots=False):
    ctx = context(tmp_path)
    rows = [{"url": URL + "real", "status": 200, "type": 1}] if real_page else []
    rows.append(response(type=1))
    if blocked_robots:
        rows.append(response(url=URL + "robots.txt", type=0))
    tables = {"seo": {"rows": [{"urlPathAndQuery": "/", "robotsIndex": "0"}]}}
    fake_crawler(monkeypatch, ctx, rows, tables)
    run = siteone.subproc.run

    def with_summary(*a, **kw):
        proc = run(*a, **kw)
        data = json.loads(proc.stdout)
        data["summary"] = {
            "items": [
                {"aplCode": "robots-txt-loaded", "text": "robots.txt status code 200"},
                {"aplCode": "seo-canonical-missing", "status": "WARN", "text": "Missing canonical"},
            ]
        }
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(data))

    monkeypatch.setattr(siteone.subproc, "run", with_summary)
    return siteone.run(ctx)


def test_siteone_challenge_discards_have_accurate_reason(tmp_path, monkeypatch):
    result = run_siteone_summary(tmp_path, monkeypatch)
    assert not any("unvalidated destinations discarded" in e.description for e in result.evidence)
    discarded = [e for e in result.evidence if "table rows" in e.description]
    assert discarded and all("hosting bot protection" in e.description for e in discarded)


@pytest.mark.parametrize("real_page", [True, False], ids=["mixed-pages", "all-pages-challenged"])
def test_siteone_preserves_independent_robots_summary(tmp_path, monkeypatch, real_page):
    result = run_siteone_summary(tmp_path, monkeypatch, real_page=real_page)
    assert any(o.check_id == "robots-txt-missing" and o.status == "pass" for o in result.outcomes)
    assert not any(f.check_key == "canonical-missing" for f in result.findings)
    assert not any(o.check_id == "canonical-missing" for o in result.outcomes)


def test_siteone_does_not_pass_challenged_robots_summary(tmp_path, monkeypatch):
    result = run_siteone_summary(tmp_path, monkeypatch, blocked_robots=True)
    assert not any(
        o.check_id == "robots-txt-missing" and o.status == "pass" for o in result.outcomes
    )
