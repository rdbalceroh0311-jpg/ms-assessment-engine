import json
import re
from types import SimpleNamespace

import httpx
import pytest

from ams_assess import runner
from ams_assess.ai import validate_analysis
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir
from ams_assess.modules import presence
from ams_assess.modules.presence import (
    DETECTED,
    NOT_ASSESSED,
    NOT_DETECTED,
    PASS,
    UNKNOWN,
    WARNING,
)

GTM_SNIPPET = (
    "(function(w,d,s,l,i){j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;})"
    "(window,document,'script','dataLayer','GTM-ABC1234');"
)


def context(tmp_path, categories=None, **config):
    return Context(
        customer_id="test",
        customer_name="Test",
        target="https://example.com/",
        categories=categories or {"presence", "local_seo", "entity"},
        connections=set(),
        config=config,
        run_dir=create_run_dir(tmp_path, "test"),
        tools_dir=tmp_path,
    )


def browser(*pages):
    result = ModuleResult(name="browser")
    for page in pages:
        url = page.pop("url", "https://example.com/")
        result.add_evidence(url, "Rendered page evidence (desktop)", {"url": url, "page": page})
    return result


def domain(mx, txt):
    result = ModuleResult(name="domain_email")
    result.add_evidence("https://example.com/", "DNS MX lookup for example.com", mx)
    result.add_evidence("https://example.com/", "DNS TXT lookup for example.com", txt)
    return result


def rows(tables, key):
    return {r["key"]: r for r in tables[key]}


def test_signature_detection_records_ids_and_gtm_only_leaves_ga4_unknown(tmp_path):
    ctx = context(tmp_path)
    ctx.results = [
        browser(
            {
                "tag_snippets": [GTM_SNIPPET, "fbq('init', '1234567890');"],
                "script_srcs": ["https://example.com/wp-content/themes/a/app.js"],
                "meta": [{"name": "generator", "content": "WordPress 6.5.2"}],
            }
        )
    ]
    result = presence.run(ctx)
    platforms = rows({c["key"]: c["value"] for c in result.checks}, "platforms")
    assert platforms["gtm"]["status"] == DETECTED and platforms["gtm"]["ids"] == ["GTM-ABC1234"]
    assert platforms["meta_pixel"]["status"] == DETECTED
    assert platforms["meta_pixel"]["ids"] == ["1234567890"]
    assert platforms["wordpress"]["status"] == DETECTED
    assert "version 6.5.2 publicly exposed (info)" in platforms["wordpress"]["detail"]
    assert platforms["ga4"]["status"] == UNKNOWN
    assert platforms["clarity"]["status"] == NOT_DETECTED
    assert "may load through GTM" in platforms["clarity"]["detail"]
    # Email platforms need DNS evidence; a page-only run must not guess them.
    assert platforms["m365"]["status"] == NOT_ASSESSED
    unconfirmed = next(f for f in result.findings if f.check_key == "tracking:unconfirmed")
    assert unconfirmed.confidence == "needs_verification"
    assert "Google Analytics 4" in unconfirmed.detail
    assert not any(f.check_key == "tracking:no-analytics" for f in result.findings)
    # Every finding cites evidence this module recorded.
    known = {e.id for e in result.evidence}
    assert all(set(f.evidence_ids) <= known for f in result.findings)


def test_direct_ga4_and_legacy_ua_signatures():
    hits = presence.detect(
        [
            (
                "ev-browser-001",
                "https://example.com/",
                [
                    "https://www.googletagmanager.com/gtag/js?id=G-ABCDEF1234",
                    "ga('create', 'UA-123456-1', 'auto');",
                ],
            )
        ]
    )
    assert hits["ga4"]["ids"] == ["G-ABCDEF1234"]
    assert hits["ua"]["ids"] == ["UA-123456-1"]
    assert "gtm" not in hits


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://g.page/acme-plumbing", "google_business_profile"),
        ("https://maps.app.goo.gl/AbC123", "google_business_profile"),
        ("https://www.google.com.au/maps/place/Acme", "google_business_profile"),
        ("https://www.google.com/search?q=acme", None),
        ("https://www.facebook.com/acmeplumbing", "facebook"),
        ("https://www.facebook.com/sharer/sharer.php?u=x", None),
        ("https://www.linkedin.com/company/acme", "linkedin"),
        ("https://www.yellowpages.com.au/nsw/sydney/acme", "yellow_pages"),
        ("https://www.hipages.com.au/connect/acme", "hipages"),
        ("https://maps.apple.com/?q=acme", "apple_maps"),
        ("https://www.bing.com/maps?q=acme", "bing_places"),
        ("mailto:hello@example.com", None),
        ("https://example.com/contact", None),
    ],
)
def test_listing_link_classification(url, expected):
    assert presence.classify_listing(url) == expected


def test_listings_from_links_and_same_as_never_claim_ownership(tmp_path):
    ctx = context(tmp_path)
    ctx.results = [
        browser(
            {
                "links": ["https://www.facebook.com/acme", "https://www.yellowpages.com.au/x/acme"],
                "json_ld": [
                    {
                        "@type": "Plumber",
                        "name": "Acme",
                        "telephone": "+61 2 5550 0199",
                        "sameAs": ["https://www.facebook.com/acme"],
                    }
                ],
                "contacts": [{"href": "tel:+61255500100", "text": "02 5550 0100"}],
                "visible_text_summary": "Acme\n02 5550 0100",
            }
        )
    ]
    result = presence.run(ctx)
    tables = {c["key"]: c["value"] for c in result.checks}
    listings = rows(tables, "listings")
    assert listings["facebook"]["status"] == presence.LINKED
    assert listings["facebook"]["sources"] == ["JSON-LD sameAs", "website link"]
    assert listings["google_business_profile"]["status"] == presence.UNKNOWN_MANUAL
    assert listings["instagram"]["status"] == NOT_DETECTED
    assert {r["ownership"] for r in tables["listings"]} == {presence.OWNERSHIP}
    keys = {f.check_key: f for f in result.findings}
    assert keys["nap-inconsistent"].category == "local_seo"
    assert "5550 0199" in keys["nap-inconsistent"].detail
    assert "Yellow Pages AU" in keys["listing:sameas-incomplete"].detail
    assert "listing:gbp-not-linked" in keys


def test_readiness_and_checklist_follow_detections(tmp_path):
    ctx = context(tmp_path, AMS_ACCESS_EMAIL="access@ams.example")
    ctx.connections = {"gsc"}
    gsc = ModuleResult(name="gsc")
    gsc.add_evidence(
        ctx.target,
        "GSC top queries/pages",
        {"property": "sc-domain:example.com", "query": {"rows": [{}]}, "sitemaps": {}},
    )
    ctx.results = [
        browser({"meta": [{"name": "google-site-verification", "content": "token"}]}),
        domain(
            {"status": "ok", "records": ["0 example-com.mail.protection.outlook.com."]},
            {"status": "ok", "records": ['"MS=ms12345"', '"v=spf1 -all"']},
        ),
        gsc,
    ]
    tables = {c["key"]: c["value"] for c in presence.run(ctx).checks}
    readiness = rows(tables, "readiness")
    assert readiness["m365"]["status"] == DETECTED
    assert "google_workspace" not in readiness
    assert readiness["gsc"]["access"] == "Connected & verified (sc-domain:example.com)"
    assert readiness["ga4"]["access"] == "Not connected"
    assert readiness["wordpress"]["status"] == NOT_DETECTED
    checklist = {item["service"]: item for item in tables["checklist"]}
    assert not any("WordPress" in service for service in checklist)
    assert checklist["Microsoft 365 (ScubaGear)"]["condition"] == (
        "Only if security review is in scope"
    )
    assert checklist["Google Search Console"]["access"] == "Existing connection"
    assert "access@ams.example" in checklist["Google Analytics 4"]["action"]
    assert all("password" not in item["action"].lower() for item in tables["checklist"])
    # GSC data is authorised-connection evidence: never cited by the public tables.
    gsc_eid = gsc.evidence[0].id
    assert [r["evidence_ids"] for r in tables["authorised"]] == [[gsc_eid], [gsc_eid]]
    public = tables["platforms"] + tables["signals"] + tables["listings"]
    assert not any(gsc_eid in r["evidence_ids"] for r in public)
    assert not any(r["signal"] == "XML sitemap" for r in tables["signals"])


def test_report_renders_presence_sections_not_assessed_without_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REGISTRY", {"presence": presence})
    ctx = context(tmp_path, categories={"presence"})
    data = runner.run(ctx, no_ai=True, formats={"md"})
    assert data["modules"][0]["status"] == "skipped"
    assert data["digital_presence"]["status"] == NOT_ASSESSED
    assert "digital_presence" in json.loads((ctx.run_dir / "results.json").read_text())
    report = (ctx.run_dir / "report.md").read_text()
    for heading in [
        "## 3. Local SEO & digital presence",
        "## 4. Business listings & platform opportunities",
        "## 5. Analytics & tracking readiness",
        "## 6. Connected assessment opportunities",
        "### Public assessment",
        "### Authorised connection findings",
        "## 7. Customer access requirements",
    ]:
        assert heading in report
    assert "| Google Analytics 4 | NOT ASSESSED |" in report
    assert "| Google Business Profile | NOT ASSESSED |" in report
    assert "set AMS\\_ACCESS\\_EMAIL" in report
    assert [h.split(".")[0] for h in re.findall(r"^## (\d\.[^\n]*)", report, re.M)] == list(
        "123456789"
    )
    other = context(tmp_path, categories={"security"})
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "security": SimpleNamespace(
                categories={"security"}, run=lambda c: ModuleResult(name="x")
            )
        },
    )
    data = runner.run(other, no_ai=True, formats={"md"})
    assert "digital_presence" not in data
    other_report = (other.run_dir / "report.md").read_text()
    assert "### Public assessment" not in other_report
    assert "Digital presence was not in the selected scope." in other_report


def test_ids_outside_tag_context_are_not_detections():
    hits = presence.detect(
        [
            (
                "ev-browser-001",
                "https://example.com/",
                [
                    'var product = {"sku": "AW-12345678", "ref": "G-ABCDEF1234"};',
                    "var codes = ['GTM-ABCD123', 'UA-123456-1'];",
                    "meta:description=Part AW-12345678 in stock",
                ],
            )
        ]
    )
    assert not {"google_ads", "ga4", "gtm", "ua"} & hits.keys()


def test_consent_gated_tags_are_unknown_until_a_request_confirms_them(tmp_path):
    config = "gtag('config', 'G-ABCDEF1234');"
    page = {"tag_snippets": [config], "gated_scripts": [config]}
    ctx = context(tmp_path)
    ctx.results = [browser(dict(page))]
    platforms = rows(presence.build(ctx, ctx.results, ModuleResult(name="presence")), "platforms")
    assert platforms["ga4"]["status"] == UNKNOWN and platforms["ga4"]["ids"] == ["G-ABCDEF1234"]
    assert platforms["meta_pixel"]["status"] == UNKNOWN
    loaded = browser(dict(page))
    loaded.evidence[0].data["requests"] = [
        "https://www.googletagmanager.com/gtag/js?id=G-ABCDEF1234"
    ]
    ctx.results = [loaded]
    platforms = rows(presence.build(ctx, ctx.results, ModuleResult(name="presence")), "platforms")
    assert platforms["ga4"]["status"] == DETECTED
    # A consent manager alone also makes unseen tags Unknown, not Not detected.
    ctx.results = [browser({"script_srcs": ["https://consent.cookiebot.com/uc.js"]})]
    tables = presence.build(ctx, ctx.results, ModuleResult(name="presence"))
    assert tables["consent"]["managers"] == ["Cookiebot"]
    assert rows(tables, "platforms")["clarity"]["status"] == UNKNOWN


def test_malformed_urls_are_skipped_per_item_and_module_fails_soft(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    ctx.results = [
        browser(
            {
                "links": ["http://[broken", "https://example.com:99999/x", "/facebook-page"],
                "json_ld": [
                    {
                        "@type": "Organization",
                        "name": "Acme",
                        "address": "1 Road",
                        "sameAs": [42, None, "http://[bad", "https://www.instagram.com/acme"],
                    }
                ],
            }
        )
    ]
    result = presence.run(ctx)
    assert result.status == "ok"
    listings = rows({c["key"]: c["value"] for c in result.checks}, "listings")
    assert listings["instagram"]["status"] == presence.LINKED
    monkeypatch.setattr(presence, "build", lambda *a: 1 / 0)
    failed = presence.run(ctx)
    assert failed.status == "error" and "ZeroDivisionError" in failed.reason


def test_nap_value_absent_from_text_is_not_a_confirmed_mismatch(tmp_path):
    ctx = context(tmp_path)
    ctx.results = [
        browser(
            {
                "json_ld": [
                    {
                        "@type": "LocalBusiness",
                        "name": "Acme",
                        "telephone": "02 5550 0100",
                        "address": {"streetAddress": "99 Hidden Lane", "postalCode": "2000"},
                    }
                ],
                "contacts": [{"href": "tel:+61255500100", "text": "02 5550 0100"}],
                "visible_text_summary": "Acme plumbing",
                "visible_text_truncated": True,
            }
        )
    ]
    result = presence.run(ctx)
    keys = {f.check_key: f for f in result.findings}
    assert "nap-inconsistent" not in keys
    assert keys["nap-unverified"].confidence == "needs_verification"
    nap = presence.nap_check([("ev-browser-001", ctx.results[0].evidence[0].data)])
    assert nap["status"] == UNKNOWN
    assert {c["field"]: c["result"] for c in nap["checks"]}["telephone"] == "match"


def test_ai_cannot_create_presence_findings():
    evidence = [
        Evidence(id=i, source=i.split("-")[1], url="https://e.test/", description="x", data={})
        for i in ["ev-presence-001", "ev-browser-001"]
    ]
    base = {"title": "T", "detail": "D", "recommendation": "R", "severity": "low"}
    data = {
        "executive_summary": "",
        "findings": [
            {**base, "id": "ai-1", "category": "presence", "evidence_ids": ["ev-browser-001"]},
            {**base, "id": "ai-2", "category": "local_seo", "evidence_ids": ["ev-presence-001"]},
            {**base, "id": "ai-3", "category": "local_seo", "evidence_ids": ["ev-browser-001"]},
        ],
    }
    _, findings, warnings = validate_analysis(data, evidence, {"presence", "local_seo"}, [])
    assert [f.id for f in findings] == ["ai-3"]
    assert sum("Dropped AI finding" in w for w in warnings) == 2


URLSET = (
    b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b"<url><loc>https://example.com/</loc></url><url><loc>https://example.com/a</loc></url>"
    b"</urlset>"
)


def serve(routes, log=None, clock=None, cost=0.0):
    """MockTransport answering {url: Response | Exception}; anything else is 404."""

    def handler(request):
        if log is not None:
            log.append((str(request.url), request.extensions["timeout"]["read"]))
        if clock is not None:
            clock[0] += cost
        answer = routes.get(str(request.url), httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handler)


def test_sitemap_parse_requires_sitemaps_org_root_with_locs(monkeypatch):
    assert presence.parse_sitemap(URLSET) == ("urlset", 2, "")
    index = (
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://example.com/s1.xml</loc></sitemap></sitemapindex>"
    )
    assert presence.parse_sitemap(index) == ("sitemapindex", 1, "")
    for body in [
        b"<html><!-- <urlset><url><loc>x</loc></url></urlset> --></html>",
        b"<urlset><url><loc>no namespace</loc></url></urlset>",
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>',
        b"<urlset",
        b'<!DOCTYPE x [<!ENTITY a "b">]><urlset/>',
    ]:
        assert presence.parse_sitemap(body)[0] == "invalid"
    # A UTF-16 body hides "<!DOCTYPE" from a byte scan but ElementTree would still parse it.
    dtd = '<!DOCTYPE urlset [<!ENTITY e "https://example.com/">]>'
    utf16 = ('<?xml version="1.0" encoding="UTF-16"?>' + dtd + URLSET.decode()[21:]).replace(
        "https://example.com/</loc>", "&e;</loc>"
    )
    assert presence.parse_sitemap(utf16.encode("utf-16"))[0] == "invalid"
    assert "unsupported encoding" in presence.parse_sitemap(utf16.encode("utf-16"))[2]
    declared_utf16 = URLSET.replace(b'version="1.0"', b'version="1.0" encoding="UTF-16"')
    assert presence.parse_sitemap(declared_utf16)[2] == "unsupported encoding (UTF-16)"
    assert presence.parse_sitemap(dtd.encode() + URLSET[21:])[0] == "invalid"
    assert presence.parse_sitemap(b"\xef\xbb\xbf" + URLSET) == ("urlset", 2, "")
    # BOM-less UTF-16 is ASCII interleaved with NULs: valid UTF-8, invisible to the text scan.
    bomless = (dtd + URLSET.decode()[21:]).replace("https://example.com/</loc>", "&e;</loc>")
    for codec in ["utf-16-le", "utf-16-be"]:
        kind, _, problem = presence.parse_sitemap(bomless.encode(codec))
        assert kind == "invalid" and "unsupported encoding" in problem
    # The parser itself rejects declarations the text scan would miss.
    monkeypatch.setattr(presence.re, "search", lambda *a, **k: None)
    kind, _, problem = presence.parse_sitemap(dtd.encode() + URLSET[21:])
    assert kind == "invalid" and "document type declarations" in problem


def test_sitemap_statuses_follow_retrieval_and_robots_knowledge(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    robots = "https://example.com/robots.txt"
    declared = httpx.Response(200, text="Sitemap: /map.xml\n")
    found = presence.sitemap_check(
        ctx,
        serve(
            {robots: declared, "https://example.com/map.xml": httpx.Response(200, content=URLSET)}
        ),
    )
    assert found["status"] == PASS and found["entries"] == 2
    missing = presence.sitemap_check(ctx, serve({}))
    assert missing["status"] == WARNING and missing["confirmed"]
    assert "robots.txt declares none" in missing["detail"]
    comment = httpx.Response(200, content=b"<html><!-- <urlset><loc>a</loc></urlset> --></html>")
    invalid = presence.sitemap_check(ctx, serve({"https://example.com/sitemap.xml": comment}))
    assert invalid["status"] == WARNING and invalid["detail"].startswith("Sitemap invalid")
    monkeypatch.setattr(presence, "SITEMAP_MAX_BYTES", 50)
    big = presence.sitemap_check(
        ctx, serve({"https://example.com/sitemap.xml": httpx.Response(200, content=URLSET)})
    )
    assert big["status"] == NOT_ASSESSED and "not fully retrieved" in big["detail"]
    # robots.txt timed out: a 404 at /sitemap.xml is never a confirmed missing sitemap.
    timeout = httpx.ReadTimeout("slow")
    unknown = presence.sitemap_check(ctx, serve({robots: timeout}))
    assert unknown["status"] == WARNING and not unknown["confirmed"]
    assert "robots.txt declarations unknown" in unknown["detail"]
    assert "declares none" not in unknown["detail"]
    monkeypatch.setattr(
        presence, "sitemap_check", lambda c: presence_sitemap(c, serve({robots: timeout}))
    )
    ctx.results = [ModuleResult(name="siteone"), browser({})]
    result = ModuleResult(name="presence")
    presence.build(ctx, ctx.results, result)
    finding = next(f for f in result.findings if f.check_key == "sitemap-missing")
    assert finding.confidence == "needs_verification"


presence_sitemap = presence.sitemap_check


def test_sitemap_redirects_are_single_same_origin_and_downgrades_rejected(tmp_path):
    ctx = context(tmp_path)
    log = []
    routes = {
        # https -> http downgrade on the same host is refused.
        "https://example.com/robots.txt": httpx.Response(
            301, headers={"location": "http://example.com/robots.txt"}
        ),
        "https://example.com/sitemap.xml": httpx.Response(301, headers={"location": "/s2.xml"}),
        "https://example.com/s2.xml": httpx.Response(301, headers={"location": "/s3.xml"}),
        "https://example.com/s3.xml": httpx.Response(200, content=URLSET),
    }
    data = presence.sitemap_check(ctx, serve(routes, log))
    assert data["robots_status"] == 301 and data["declared"] == "unknown"
    assert data["status"] == NOT_ASSESSED and "/s3.xml" in data["detail"]
    assert [url for url, _ in log] == [
        "https://example.com/robots.txt",
        "https://example.com/sitemap.xml",
        "https://example.com/s2.xml",
    ]
    other_port = {
        "https://example.com/robots.txt": httpx.Response(404),
        "https://example.com/sitemap.xml": httpx.Response(
            302, headers={"location": "https://example.com:8443/sitemap.xml"}
        ),
    }
    assert presence.sitemap_check(ctx, serve(other_port))["status"] == NOT_ASSESSED


def test_sitemap_requests_share_one_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(presence, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    ctx = context(tmp_path)
    log = []
    routes = {"https://example.com/sitemap.xml": httpx.Response(200, content=URLSET)}
    data = presence.sitemap_check(ctx, serve(routes, log, clock, cost=6.0))
    # Each request's timeout is what remains of the single 10-second budget.
    assert [t for _, t in log] == [10.0, 4.0]
    assert data["status"] == NOT_ASSESSED and "not fully retrieved" in data["detail"]
    clock[0], log[:] = 0.0, []
    data = presence.sitemap_check(ctx, serve(routes, log, clock, cost=11.0))
    assert len(log) == 1 and "DeadlineExceeded" in data["detail"]


@pytest.mark.parametrize(
    "page",
    [
        # Two locations on one page: which phone belongs to which entity is ambiguous.
        {
            "json_ld": [
                {"@type": "LocalBusiness", "name": "Acme North", "telephone": "02 9999 0001"},
                {"@type": "LocalBusiness", "name": "Acme South", "telephone": "02 9999 0002"},
            ],
            "visible_text_summary": "Acme North Acme South call 02 5550 0100",
        },
        # One entity but two different phones shown.
        {
            "json_ld": [{"@type": "LocalBusiness", "name": "Acme", "telephone": "02 9999 0001"}],
            "visible_text_summary": "Acme sales 02 5550 0100 service 02 5550 0200",
        },
        # One entity, one phone, but the page text was truncated.
        {
            "json_ld": [{"@type": "LocalBusiness", "name": "Acme", "telephone": "02 9999 0001"}],
            "visible_text_summary": "Acme 02 5550 0100",
            "visible_text_truncated": True,
        },
    ],
)
def test_ambiguous_nap_is_never_a_confirmed_mismatch(tmp_path, page):
    ctx = context(tmp_path)
    ctx.results = [browser(page)]
    keys = {f.check_key: f for f in presence.run(ctx).findings}
    assert "nap-inconsistent" not in keys
    assert keys["nap-unverified"].confidence == "needs_verification"


def test_nap_values_from_different_pages_are_not_compared(tmp_path):
    ctx = context(tmp_path)
    ctx.results = [
        browser(
            {
                "url": "https://example.com/",
                "json_ld": [
                    {"@type": "LocalBusiness", "name": "Acme", "telephone": "02 9999 0001"}
                ],
                "visible_text_summary": "Acme 02 9999 0001",
            },
            {"url": "https://example.com/contact", "visible_text_summary": "Call 02 5550 0100"},
        )
    ]
    nap = presence.nap_check(presence.page_evidence(ctx.results[0]))
    assert nap["status"] == "Consistent"
