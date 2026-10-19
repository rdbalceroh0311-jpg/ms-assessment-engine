import json
from types import SimpleNamespace

import pytest

from ams_assess import runner
from ams_assess.models import Context, ModuleResult, create_run_dir
from ams_assess.modules import presence
from ams_assess.modules.presence import DETECTED, NOT_ASSESSED, NOT_DETECTED, UNKNOWN

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
    ga4 = next(f for f in result.findings if f.check_key == "tracking:ga4-unconfirmed")
    assert ga4.confidence == "needs_verification"
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
    assert tables["authorised"][0]["evidence_ids"] == [gsc.evidence[0].id]


def test_report_renders_presence_sections_not_assessed_without_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REGISTRY", {"presence": presence})
    ctx = context(tmp_path, categories={"presence"})
    data = runner.run(ctx, no_ai=True, formats={"md"})
    assert data["modules"][0]["status"] == "skipped"
    assert data["digital_presence"]["status"] == NOT_ASSESSED
    assert "digital_presence" in json.loads((ctx.run_dir / "results.json").read_text())
    report = (ctx.run_dir / "report.md").read_text()
    for heading in [
        "## Local SEO & digital presence",
        "## Business listings & platform opportunities",
        "## Analytics & tracking readiness",
        "## Connected assessment opportunities",
        "### Public assessment",
        "### Authorised connection findings",
        "## Customer access requirements",
    ]:
        assert heading in report
    assert "| Google Analytics 4 | NOT ASSESSED |" in report
    assert "| Google Business Profile | NOT ASSESSED |" in report
    assert "set AMS\\_ACCESS\\_EMAIL" in report
    assert report.index("## Customer access requirements") < report.index(
        "## Prioritised recommendations"
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
    assert "Connected assessment opportunities" not in (other.run_dir / "report.md").read_text()
