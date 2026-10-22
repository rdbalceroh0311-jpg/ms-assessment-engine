import json
from dataclasses import replace

import httpx
import pytest

from ams_assess import runner, store
from ams_assess.models import CATEGORIES, Context, ModuleResult, create_run_dir
from ams_assess.modules import presence, site_checks
from ams_assess.modules.site_rules import (
    CRAWLERS,
    TOOLS,
    HomeHTML,
    applicable_rules,
    conversion_events,
    cwv_grade,
    field_vitals,
    near_duplicates,
    robots_allowed,
    robots_groups,
    structured_data,
    tool_signals,
)
from ams_assess.report import render_customer


def context(tmp_path, **changes):
    return replace(
        Context(
            customer_id="test",
            customer_name="Test",
            target="https://example.com/",
            categories=set(CATEGORIES),
            connections=set(),
            config={},
            run_dir=create_run_dir(tmp_path, "test"),
            tools_dir=tmp_path / "tools",
        ),
        **changes,
    )


def source(name, records):
    result = ModuleResult(name=name)
    for url, description, data in records:
        result.add_evidence(url, description, data)
    return result


def browser(pages):
    return source(
        "browser",
        [
            (
                url,
                "Rendered page evidence (desktop)",
                {"url": url, "viewport": "desktop", "page": page},
            )
            for url, page in pages.items()
        ],
    )


def crawl(pages, metadata=None, tables=None):
    records = [
        (url, "SiteOne HTTP response", {"status": str(status), "type": 1})
        for url, status in pages.items()
    ]
    records += [(url, "SiteOne SEO metadata", data) for url, data in (metadata or {}).items()]
    records += [
        ("https://example.com/", f"SiteOne {key} table", data)
        for key, data in (tables or {}).items()
    ]
    return source("siteone", records)


def sitemap(body="User-agent: *\nAllow: /", urls=None, **extra):
    return source(
        "presence",
        [
            (
                "https://example.com/sitemap.xml",
                "Bounded robots.txt and XML sitemap fetch",
                {
                    "robots_body": body,
                    "robots_complete": True,
                    "robots_status": 200,
                    "kind": "urlset",
                    "sitemap_urls": urls or [],
                    "sitemap_url": "https://example.com/sitemap.xml",
                    **extra,
                },
            )
        ],
    )


def run_checks(ctx, monkeypatch, probes=None):
    monkeypatch.setattr(site_checks, "collect_probes", lambda _: probes or {})
    result = site_checks.run(ctx)
    return result, {row["site_check"]: row for row in result.checks}


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    # MockTransport bypasses the real transport. Any accidental unmocked unit-test HTTP fails.
    def forbidden(*args, **kwargs):
        pytest.fail("Unit tests must not make live HTTP calls")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@pytest.mark.parametrize(
    ("body", "bot", "path", "allowed"),
    [
        ("User-agent: *\nDisallow: /", "Googlebot", "/", False),
        ("User-agent: *\nDisallow: /\nUser-agent: Googlebot\nAllow: /", "Googlebot", "/", True),
        (
            "USER-AGENT: Googlebot\nUser-agent: Bingbot\nDisallow: /private # comment",
            "Bingbot",
            "/private/a",
            False,
        ),
        (
            "User-agent: Googlebot\nDisallow: /x\nUser-agent: Googlebot\nAllow: /x",
            "Googlebot",
            "/x",
            True,
        ),
        ("User-agent: *\nDisallow: /*.pdf$", "GPTBot", "/a.pdf", False),
        ("User-agent: *\nDisallow: /*.pdf$", "GPTBot", "/a.pdf?download=1", True),
        (
            "User-agent: *\nDisallow: /private\nAllow: /private/public",
            "GPTBot",
            "/private/public",
            True,
        ),
        ("User-agent: *\nDisallow:", "GPTBot", "/", True),
        ("User-agent: *\nDisallow: /café", "GPTBot", "/caf%C3%A9", False),
        ("User-agent: *\nDisallow: /%70rivate", "GPTBot", "/private", False),
    ],
)
def test_robots_group_precedence_and_path_rules(body, bot, path, allowed):
    assert robots_allowed(applicable_rules(robots_groups(body), bot), path) is allowed


def test_robots_statuses_purpose_and_incomplete_body(tmp_path, monkeypatch):
    ctx = context(
        tmp_path,
        results=[
            sitemap(
                "User-agent: *\nAllow: /\nUser-agent: GPTBot\nDisallow: /\n"
                "User-agent: OAI-SearchBot\nDisallow: /private\n"
                "User-agent: ChatGPT-User\nDisallow: /\nUser-agent: Googlebot\nDisallow: /"
            )
        ],
        pages=["https://example.com/private"],
    )
    result, rows = run_checks(ctx, monkeypatch)
    assert rows["robots"]["status"] == "ERROR"
    assert rows["ai-robots"]["status"] == "WARNING"
    bots = {b["crawler"]: b for b in rows["ai-robots"]["detail"]["crawlers"]}
    assert len(bots) == sum(len(v) for v in CRAWLERS.values()) == 15
    assert bots["GPTBot"]["status"] == "INFO"
    assert bots["ChatGPT-User"]["purpose"] == "user-initiated"
    assert bots["OAI-SearchBot"]["blocked_paths"] == ["/private"]
    assert next(f for f in result.findings if f.check_key == "site:ai-robots").confidence == (
        "needs_verification"
    )
    ctx.results = [sitemap("User-agent: *\nDisallow: /", robots_complete=False)]
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["robots"]["status"] == rows["ai-robots"]["status"] == "NOT ASSESSED"


@pytest.mark.parametrize(
    ("metric", "good", "boundary", "poor"),
    [
        ("LCP", 2500, 4000, 4001),
        ("INP", 200, 500, 501),
        ("CLS", 0.1, 0.25, 0.2501),
    ],
)
def test_cwv_google_inclusive_boundaries(metric, good, boundary, poor):
    assert cwv_grade(metric, good) == "good"
    assert cwv_grade(metric, boundary) == "needs improvement"
    assert cwv_grade(metric, poor) == "poor"
    for invalid in (None, -1, "NaN", float("inf"), True, "missing"):
        assert cwv_grade(metric, invalid) == "NOT ASSESSED"


def test_cwv_psi_scaling_crux_scope_missing_metrics_and_lab_exclusion(tmp_path, monkeypatch):
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
    metrics = field_vitals(psi)
    assert all(r["grade"] == "good" and r["scope"] == "URL" for r in metrics)
    assert metrics[-1]["p75"] == 0.1
    crux = {
        "record": {
            "key": {"origin": "https://example.com", "formFactor": "PHONE"},
            "metrics": {"cumulative_layout_shift": {"percentiles": {"p75": ".25"}}},
        }
    }
    metrics = field_vitals(crux)
    assert metrics[-1]["p75"] == ".25" and metrics[-1]["grade"] == "needs improvement"
    assert metrics[0]["grade"] == "NOT ASSESSED" and metrics[0]["scope"] == "origin"
    assert not field_vitals({"metrics": {"largest-contentful-paint": {"numericValue": 1200}}})
    ctx = context(
        tmp_path,
        categories={"performance"},
        results=[source("performance", [("https://example.com/", "PageSpeed", psi)])],
    )
    _, rows = run_checks(ctx, monkeypatch)
    assert set(rows) == {"cwv"} and rows["cwv"]["status"] == "PASS"
    ctx.results[0].evidence[0].data = crux
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["cwv"]["status"] == "WARNING"
    ctx.results[0].evidence[0].data = {"score": 1, "metrics": {"lcp": 1000}}
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["cwv"]["status"] == "NOT ASSESSED"


@pytest.mark.parametrize(
    "subtype", ["LocalBusiness", "Plumber", "Dentist", "Store", "HVACBusiness"]
)
def test_local_business_required_and_recommended_fields(subtype):
    data = structured_data([{"@type": subtype}])
    assert any("requires name" in e for e in data["errors"])
    assert any("PostalAddress" in e for e in data["errors"])
    data = structured_data(
        [
            {
                "@type": subtype,
                "name": "Test",
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "1 Example St",
                    "addressLocality": "Sydney",
                    "addressRegion": "NSW",
                    "postalCode": "2000",
                    "addressCountry": "AU",
                },
            }
        ]
    )
    assert not data["errors"] and data["recommended"]


def test_organization_breadcrumb_invalid_jsonld_and_graph():
    assert not structured_data([{"@type": "Organization"}])["errors"]
    assert not structured_data([])["errors"]
    assert structured_data(['{"@type": bad}'])["errors"] == ["Invalid JSON-LD syntax"]
    assert structured_data([{"parse_error": True}])["errors"] == ["Invalid JSON-LD syntax"]
    crumb = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://example.com/"},
            {"@type": "ListItem", "position": 2, "name": "Services"},
        ],
    }
    assert not structured_data([{"@graph": [crumb]}])["errors"]
    crumb["itemListElement"][0]["item"] = "javascript:alert(1)"
    crumb["itemListElement"][1]["position"] = True
    errors = structured_data([crumb])["errors"]
    assert any("absolute HTTP" in e for e in errors)
    assert any("positive integer" in e for e in errors)
    assert any("consecutively" in e for e in errors)
    invalid = {"@type": "Organization", "name": [], "url": "invalid", "geo": {"latitude": 91}}
    assert len(structured_data([invalid])["errors"]) == 3


@pytest.mark.parametrize("tool", list(TOOLS))
def test_tool_contexts_and_hostname_boundaries(tool):
    signature = TOOLS[tool][0]
    url = "https://" + signature
    for key in ("script_srcs", "links", "iframes"):
        hits = tool_signals({key: [url]}, "https://example.com/")
        assert any(h["tool"] == tool for h in hits)
    assert not tool_signals(
        {"visible_text_summary": url, "tag_snippets": [f'var docs="{url}"']}, "https://example.com/"
    )
    host = signature.split("/")[0]
    for decoy in (
        f"https://{host}.evil.test/",
        f"https://example.com/?ref={url}",
        f"https://evil{host}/",
        "javascript:" + url,
    ):
        assert not tool_signals({"script_srcs": [decoy]}, "https://example.com/")


def test_home_markup_iframes_jsonld_and_event_calls_exclude_prose_comments_strings():
    parser = HomeHTML("https://example.com/")
    parser.feed("""<p>calendly.com gtag('event','prose')</p>
      <!-- <iframe src="https://calendly.com/fake"></iframe> -->
      <iframe src="https://calendly.com/real"></iframe>
      <script type="text/plain" data-consent="analytics">gtag('event','generate_lead');</script>
      <script type="application/ld+json">{"@type":"FAQPage"}</script>
      <h1>Title</h1><h4>How?</h4>""")
    assert tool_signals(parser.page, parser.base) == [
        {"tool": "Calendly", "context": "iframes", "url": "https://calendly.com/real"}
    ]
    assert conversion_events(parser.page) == [{"library": "gtag", "event": "generate_lead"}]
    assert parser.page["gated_scripts"] and parser.page["headings"][-1]["level"] == "H4"
    page = {
        "tag_snippets": [
            """// gtag('event', 'comment')
        /* fbq('track', 'Comment') */
        const example = "gtag('event', 'string')";
        const template = `fbq('track','template')`;
        gtag('event', 'generate_lead'); fbq('track', 'Lead');
        fbq('trackCustom','CustomLead'); gtag('config','G-TEST');"""
        ]
    }
    assert [e["event"] for e in conversion_events(page)] == ["generate_lead", "Lead", "CustomLead"]


def location_text():
    return " ".join(f"service{i}" for i in range(120))


def test_location_similarity_is_heuristic_bounded_and_requires_enough_text(tmp_path, monkeypatch):
    text = location_text()
    a = {"url": "https://example.com/locations/a", "visible_text_summary": text}
    b = {"url": "https://example.com/suburbs/b", "visible_text_summary": text + " Sydney"}
    pairs = near_duplicates([a, b])
    assert len(pairs) == 1 and pairs[0]["jaccard"] >= 0.8
    assert not near_duplicates([a, {**b, "visible_text_summary": "short"}])
    assert not near_duplicates(
        [a, {**b, "visible_text_summary": " ".join(f"other{i}" for i in range(120))}]
    )
    assert not near_duplicates([{**a, "url": "https://example.com/blog/a"}, b])
    ctx = context(tmp_path, results=[browser({a["url"]: a, b["url"]: b})])
    result, rows = run_checks(ctx, monkeypatch)
    assert rows["location-duplicates"]["status"] == "WARNING"
    assert next(
        f for f in result.findings if f.check_key == "site:location-duplicates"
    ).confidence == ("needs_verification")


def test_indexing_sitemap_canonical_statuses_and_no_extra_fetches(tmp_path, monkeypatch):
    home, key, moved, missing, unknown = [
        "https://example.com/" + p for p in ("", "services", "old", "broken", "unseen")
    ]
    ctx = context(
        tmp_path,
        results=[
            sitemap(urls=[home, key, moved, missing, unknown]),
            crawl(
                {home: 200, key: 200, moved: 301, missing: 404},
                {
                    home: {"robotsIndex": "1"},
                    key: {"robotsIndex": "0"},
                },
            ),
            browser(
                {
                    home: {
                        "canonical": missing,
                        "meta": [{"name": "robots", "content": "noindex"}],
                    },
                    key: {"canonical": unknown},
                }
            ),
        ],
    )
    _, rows = run_checks(
        ctx,
        monkeypatch,
        {"normal": {"url": home, "status": 200, "headers": {"x-robots-tag": "googlebot: noindex"}}},
    )
    assert rows["noindex"]["status"] == "ERROR"
    assert {h["source"].split(":")[0] for h in rows["noindex"]["detail"]["pages"]} == {
        "meta robots",
        "SiteOne effective robotsIndex=0",
        "X-Robots-Tag",
    }
    urls = {r["url"]: r["issues"] for r in rows["sitemap-urls"]["detail"]["urls"]}
    assert urls[home] == ["noindex"]
    assert urls[moved] == ["redirected (HTTP 301)"]
    assert urls[unknown] == ["not crawled"]
    assert rows["sitemap-urls"]["status"] == rows["canonicals"]["status"] == "WARNING"
    assert "NOT ASSESSED" in rows["canonicals"]["detail"][1]["accessibility"]
    ctx.results = [sitemap(urls=[unknown]), browser({home: {"canonical": unknown}})]
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["sitemap-urls"]["status"] == rows["canonicals"]["status"] == "NOT ASSESSED"


def test_duplicates_heading_jumps_broken_links_redirect_chains_and_inbound_limits(
    tmp_path, monkeypatch
):
    home, a, b, c = ["https://example.com/" + p for p in ("", "a", "b", "c")]
    ctx = context(
        tmp_path,
        pages=[home, a],
        results=[
            sitemap(),
            crawl(
                {home: 200, a: 200, b: 301, c: 301, home + "bad": 404},
                {
                    home: {"title": "Same", "description": "Same description"},
                    a: {"title": " same ", "description": "Same description"},
                },
                {"redirects": [{"url": b, "redirectUrl": c}, {"url": c, "redirectUrl": a}]},
            ),
            browser(
                {
                    home: {
                        "headings": [{"level": "H1"}, {"level": "H1"}, {"level": "H3"}],
                        "links": [b, home + "bad"],
                    },
                    a: {"links": [home]},
                }
            ),
        ],
    )
    _, rows = run_checks(ctx, monkeypatch)
    detail = rows["page-quality"]["detail"]
    assert rows["page-quality"]["status"] == "WARNING"
    assert detail["duplicates"]["title"] == [[home, a]]
    assert {p["issue"] for p in detail["heading_issues"]} == {
        "multiple H1",
        "skipped heading level",
    }
    assert detail["redirect_chains"][0]["chain"] == [b, c, a]
    assert detail["broken_links"] and detail["no_inbound"] == [a]
    ctx.pages += [home + "unobserved"]
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["page-quality"]["detail"]["inbound_status"] == "NOT ASSESSED"
    assert not rows["page-quality"]["detail"]["no_inbound"]


@pytest.mark.parametrize(
    ("normal", "bot", "verdict"),
    [
        (200, 403, "WARNING"),
        (200, 429, "WARNING"),
        (200, 503, "WARNING"),
        (403, 403, "NOT ASSESSED"),
        (200, 200, "PASS"),
        (301, 200, "NOT ASSESSED"),
    ],
)
def test_firewall_only_suspected_differential_response(tmp_path, monkeypatch, normal, bot, verdict):
    result, rows = run_checks(
        context(tmp_path),
        monkeypatch,
        {
            "normal": {"status": normal},
            "search": {"status": bot},
        },
    )
    assert rows["firewall"]["status"] == verdict
    findings = [f for f in result.findings if f.check_key == "site:firewall"]
    if verdict == "WARNING":
        assert findings[0].confidence == "needs_verification" and "Suspected" in findings[0].detail
    else:
        assert not findings


@pytest.mark.parametrize(
    ("code", "body", "complete", "state", "status"),
    [
        (200, "# Business\nHelpful information", True, "present", "INFO"),
        (404, "", True, "absent", "INFO"),
        (410, "", True, "absent", "INFO"),
        (200, "<html>Not found</html>", True, "unknown", "NOT ASSESSED"),
        (200, "", True, "unknown", "NOT ASSESSED"),
        (200, "# Huge", False, "unknown", "NOT ASSESSED"),
        (403, "denied", True, "unknown", "NOT ASSESSED"),
    ],
)
def test_llms_is_optional_and_never_a_failure(
    tmp_path, monkeypatch, code, body, complete, state, status
):
    result, rows = run_checks(
        context(tmp_path),
        monkeypatch,
        {
            "llms": {"status": code, "body": body, "complete": complete},
        },
    )
    assert rows["llms-txt"]["status"] == status and rows["llms-txt"]["detail"]["state"] == state
    assert not any(f.check_key == "site:llms-txt" for f in result.findings)


def test_conversion_paths_consent_and_heuristic_answers(tmp_path, monkeypatch):
    home = "https://example.com/"
    page = {
        "forms": [{"method": "post", "action": home + "enquire"}],
        "contacts": [{"href": "tel:123"}, {"href": "mailto:test@example.com"}],
        "ctas": [{"text": "Book now", "href": "/book"}],
        "links": [home + "about"],
        "trust_signals": ["Customer testimonials"],
        "visible_text_summary": "We provide repairs. Based in Sydney. Contact us.",
        "json_ld": [{"@type": "FAQPage"}],
        "headings": [{"level": "H2", "text": "FAQ"}, {"level": "H3", "text": "How long?"}],
        "tag_snippets": ["gtag('event','generate_lead')"],
        "gated_scripts": ["blocked"],
    }
    _, rows = run_checks(context(tmp_path, results=[browser({home: page})]), monkeypatch)
    path = rows["conversion-pathways"]["detail"][0]
    assert path["enquiry_forms"] == 1 and path["form_submissions"] == "NOT TESTED"
    assert path["above_fold_status"] == "NOT ASSESSED" and path["supporting_pages"] == [
        home + "about"
    ]
    assert rows["conversion-events"]["status"] == "NOT ASSESSED"
    assert rows["conversion-events"]["detail"]["state"] == "Unknown"
    assert len(rows["conversion-events"]["detail"]["events"]) == 1
    answer = rows["answer-readiness"]["detail"][0]
    assert answer["faq_qa_schema"] == ["FAQPage"] and answer["question_headings"] == ["How long?"]
    assert answer["faq_section"] and answer["service_statement"] and answer["location_statement"]
    assert rows["answer-readiness"]["confidence"] == "needs_verification"


def test_probes_request_body_redirect_and_deadline_bounds(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == "example.com"
        return httpx.Response(
            302,
            headers={"location": "https://forbidden.test/"},
            content=b"x" * (site_checks.MAX_BYTES + 1),
        )

    data = site_checks.collect_probes(context(tmp_path), httpx.MockTransport(handler))
    assert len(requests) == 3 and [r.url.path for r in requests] == ["/", "/", "/llms.txt"]
    assert requests[0].headers["user-agent"] != requests[1].headers["user-agent"]
    assert all(len(r["body"]) == site_checks.MAX_BYTES and not r["complete"] for r in data.values())
    requests.clear()
    clock = iter([0, 11, 12, 13])
    monkeypatch.setattr(site_checks.time, "monotonic", lambda: next(clock))
    data = site_checks.collect_probes(context(tmp_path), httpx.MockTransport(handler))
    assert not requests and all(r["error"] == "DeadlineExceeded" for r in data.values())


def test_probe_timeout_sanitised_and_performance_only_no_network(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("do not expose secret", request=request)

    data = site_checks.collect_probes(context(tmp_path), httpx.MockTransport(handler))
    assert all(r["error"] == "ReadTimeout" for r in data.values())
    assert "secret" not in json.dumps(data)
    assert site_checks.collect_probes(context(tmp_path, categories={"performance"})) == {}


def test_presence_additive_retention_and_reuse(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            text="User-agent: *\nDisallow: /private"
            if request.url.path == "/robots.txt"
            else '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>https://example.com/{i}</loc></url>" for i in range(501))
            + "</urlset>",
        )

    ctx = context(tmp_path)
    data = presence.sitemap_check(ctx, httpx.MockTransport(handler))
    assert data["robots_body"].endswith("/private") and data["robots_complete"]
    assert len(data["sitemap_urls"]) == 500 and data["sitemap_urls_truncated"]
    ctx.results = [
        source("presence", [(ctx.target, "Bounded robots.txt and XML sitemap fetch", data)])
    ]
    monkeypatch.setattr(presence, "sitemap_check", lambda _: pytest.fail("Do not refetch robots"))
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["robots"]["status"] == "PASS" and requests == ["/robots.txt", "/sitemap.xml"]


def test_fallback_only_when_collector_not_already_run_and_crawl_available(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        presence, "sitemap_check", lambda _: calls.append(1) or {"robots_status": 404}
    )
    ctx = context(tmp_path, categories={"seo"}, results=[crawl({"https://example.com/": 200})])
    _, rows = run_checks(ctx, monkeypatch)
    assert calls == [1] and rows["robots"]["status"] == "INFO"
    ctx.results = []
    _, rows = run_checks(ctx, monkeypatch)
    assert calls == [1] and rows["robots"]["status"] == "NOT ASSESSED"


def test_all_15_rows_provenance_scope_and_customer_report_survival(tmp_path, monkeypatch):
    ctx = context(tmp_path, store_root=tmp_path)
    monkeypatch.setattr(site_checks, "collect_probes", lambda _: {})
    monkeypatch.setattr(runner, "REGISTRY", {"site_checks": site_checks})
    data = runner.run(ctx, no_ai=True, formats={"md"})
    result = data["modules"][0]
    assert result["status"] == "skipped" and len(result["checks"]) == 15
    expected = {
        "robots",
        "ai-robots",
        "noindex",
        "sitemap-urls",
        "canonicals",
        "page-quality",
        "structured-data",
        "location-duplicates",
        "cwv",
        "booking-tools",
        "conversion-pathways",
        "conversion-events",
        "firewall",
        "llms-txt",
        "answer-readiness",
    }
    assert {r["site_check"] for r in result["checks"]} == expected
    known = {e["id"] for e in result["evidence"]}
    assert all(set(r["evidence_ids"]) <= known for r in result["checks"])
    assert all(r["checked"] and r["why"] and r["recommendation"] for r in result["checks"])
    assert all(r["status"] == "NOT ASSESSED" for r in result["checks"])
    assert store.load(tmp_path, "test") is not None
    render_customer(tmp_path, "test", {"md"})
    report = (tmp_path / "test/report.md").read_text()
    assert all(r["area"].replace("_", "\\_") in report for r in result["checks"])
    assert "forms NOT TESTED" in report and "NOT ASSESSED" in report


def test_inline_api_detection_and_booking_paths_do_not_confuse_tracking(tmp_path, monkeypatch):
    home = "https://example.com/"
    page = {
        "tag_snippets": [
            'const example="Calendly.initPopupWidget("; // hbspt.forms.create({});\n'
            'Calendly.initInlineWidget({url:"https://calendly.com/test"}); hbspt.forms.create({});'
        ],
        "script_srcs": ["https://js.hs-analytics.net/analytics/1.js"],
        "links": [home + "book", "https://square.site/book/demo"],
        "ctas": [{"text": "Book now", "above_fold": True}],
        "forms": [{"action": "/login", "fields": [{"type": "email"}, {"type": "password"}]}],
    }
    assert {h["tool"] for h in tool_signals(page, home)} == {
        "Calendly",
        "HubSpot",
        "Square Appointments",
    }
    _, rows = run_checks(context(tmp_path, results=[browser({home: page})]), monkeypatch)
    path = rows["conversion-pathways"]["detail"][0]
    assert path["booking_links"] == [home + "book", "https://square.site/book/demo"]
    assert path["enquiry_forms"] == 0 and path["forms_inspected"] == 1
    assert path["above_fold_status"] == "assessed" and len(path["above_fold"]) == 1


def test_crawl_redirect_chain_without_browser_evidence(tmp_path, monkeypatch):
    home, old, end = ["https://example.com/" + p for p in ("", "old", "end")]
    ctx = context(
        tmp_path,
        results=[
            sitemap(),
            crawl(
                {home: 200, old: 301, end: 200},
                tables={
                    "redirects": [{"url": old, "targetUrl": "/end", "sourceUqId": home}],
                },
            ),
        ],
    )
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["page-quality"]["detail"]["redirect_chains"] == [
        {"source": home, "chain": [old, end], "complete": True},
    ]


def test_customer_status_rows_preserve_separate_target_attribution(tmp_path, monkeypatch):
    monkeypatch.setattr(site_checks, "collect_probes", lambda _: {})
    monkeypatch.setattr(runner, "REGISTRY", {"site_checks": site_checks})
    for target in ("https://example.com/", "https://second.example.test/"):
        runner.run(
            context(tmp_path, target=target, store_root=tmp_path), no_ai=True, formats={"md"}
        )
    render_customer(tmp_path, "test", {"md"})
    report = (tmp_path / "test/report.md").read_text()
    table = report.split("### Website checks: status and evidence", 1)[1]
    assert table.count("| Core Web Vitals (field p75)") == 2
    assert "<br>https://example.com/ |" in table
    assert "<br>https://second.example.test/ |" in table
