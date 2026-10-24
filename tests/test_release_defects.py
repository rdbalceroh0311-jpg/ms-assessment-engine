"""Release defect regressions D1-D9. Synthetic data only; no live websites or paid providers."""

import copy
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ams_assess import ai, coverage, figures, register, store, ui
from ams_assess.lifecycle import scoped
from ams_assess.models import Context, Evidence, Finding, ModuleResult, create_run_dir
from ams_assess.modules import browser, gsc, presence, siteone
from ams_assess.presence_discovery import INCONSISTENT, nap_detail, repair_nap
from ams_assess.profile import phone_key
from ams_assess.report import finding_views, render_markdown

REPO = Path(__file__).resolve().parents[1]
CUSTOMER = "synthetic"


def context(tmp_path, **changes):
    return replace(
        Context(
            customer_id=CUSTOMER,
            customer_name="Synthetic",
            target="https://example.com/",
            categories={"presence"},
            connections=set(),
            config={},
            run_dir=create_run_dir(tmp_path, CUSTOMER),
            tools_dir=tmp_path / "tools",
        ),
        **changes,
    )


def save(root, current):
    with store.locked(root, CUSTOMER):
        store.save(root, CUSTOMER, current)


def report_data(findings, evidence, **extra):
    return {
        "customer_name": "Synthetic",
        "target": "https://example.test/",
        "created_at": "2026-10-01",
        "selected_categories": sorted({f.category for f in findings}),
        "assessed_categories": sorted({f.category for f in findings}),
        "modules": [{"name": "browser", "status": "ok", "reason": "Bounded sample"}],
        "ai": {"status": "skipped", "recommendations": [], "roadmap": {}},
        "findings": [f.model_dump() for f in findings],
        "limitations": [],
        "pages": [],
        "max_pages": 1,
        **extra,
    }


# D1 -------------------------------------------------------------------------------------------

PROFILE = {
    "name": "Acme Painting Pty Ltd",
    "aliases": [],
    "websites": ["https://example.com/"],
    "abn": "",
    "industry": "trades",
    "buying_model": "service-area",
    "service_area": ["Parramatta"],
    "locations": [],
    "phones": ["02 5550 0101"],
}
# Icon-only tel: links have no text, so the href is all a page offers; browsers keep it encoded.
ENCODED_PAGE = {
    "links": [],
    "json_ld": [
        {
            "@type": "HousePainter",
            "name": "Acme Painting",
            "telephone": "+61255500101",
            "address": {"addressLocality": "Parramatta", "postalCode": "2150"},
        }
    ],
    "contacts": [
        {"href": "tel:(02)%205550%200101", "text": ""},
        {"href": "tel:0400%20111%20222", "text": ""},
    ],
    "visible_text_summary": "Acme Painting. Call (02) 5550 0101 or 0400 111 222. Parramatta NSW",
}


def test_d1_encoded_tel_links_are_decoded_and_a_nine_digit_listing_stays_inconsistent(
    tmp_path, monkeypatch
):
    def post(url, **kwargs):
        keyword = kwargs["json"][0]["keyword"]
        items = []
        if "/organic/" in url and keyword == "Acme Painting Parramatta":
            # The listing itself shows one digit too few: a source inconsistency, not parsing.
            items = [
                {
                    "type": "organic",
                    "url": "https://www.facebook.com/acmepainting/",
                    "title": "Acme Painting Parramatta | Facebook",
                    "description": "Painters in Parramatta. Call 040011122 today.",
                }
            ]
        body = {
            "status_code": 20000,
            "cost": 0.002,
            "tasks": [{"status_code": 20000, "result": [{"check_url": "", "items": items}]}],
        }
        return httpx.Response(200, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx, "post", post)
    ctx = context(
        tmp_path,
        profile=PROFILE,
        dataforseo_max_usd=1.0,
        config={"DATAFORSEO_LOGIN": "synthetic", "DATAFORSEO_PASSWORD": "synthetic"},
    )
    rendered = ModuleResult(name="browser")
    rendered.add_evidence(
        "https://example.com/",
        "Rendered page evidence (desktop)",
        {"url": "https://example.com/", "page": ENCODED_PAGE},
    )
    ctx.results = [rendered]
    result = presence.run(ctx)

    site = presence.site_identity([("ev-browser-001", {"page": ENCODED_PAGE})])
    assert set(site["phones"]) == {"0255500101", "0400111222"}
    assert not any("%" in shown for shown in site["phones"].values())
    nap = next(e for e in result.evidence if e.description == "NAP consistency comparison")
    observed = [v for check in nap.data["checks"] for v in check["observed"]]
    assert "(02) 5550 0101" in observed
    assert not any("%" in v or v.startswith("02)") for v in observed)

    discovery = {c["key"]: c["value"] for c in result.checks}["discovery"]
    rows = [n for n in discovery["nap"] if n["key"] == "facebook"]
    assert [(n["observed"], n["result"]) for n in rows] == [("040011122", "differs")]
    # One reference per number: the encoded href, JSON-LD and profile forms share a key.
    assert sorted(phone_key(r) for r in rows[0]["reference"]) == ["0255500101", "0400111222"]
    assert not any("%" in r for r in rows[0]["reference"])
    states = {r["key"]: r["state"] for r in discovery["platforms"]}
    assert states["facebook"] == INCONSISTENT
    finding = next(
        f for f in result.findings if f.check_key == "platform:external-nap-inconsistent:facebook"
    )
    assert "%" not in finding.detail and "040011122" in finding.detail
    assert "9 digits" in finding.detail and finding.confidence == "needs_verification"


LEGACY_NAP = [
    {
        "key": "facebook",
        "platform": "Facebook",
        "url": "https://www.facebook.com/acmepainting",
        "field": "phone",
        "observed": "040011122",
        # As stored by earlier runs: the encoded href and its decoded twin under two keys.
        "reference": ["(02)%205550%200101", "+61255500101", "0400 111 222", "0400 111 222"],
        "result": "differs",
        "evidence_ids": ["ev-presence-r3-013"],
    },
    {
        "key": "google_business_profile",
        "platform": "Google Business Profile",
        "url": "https://maps.google.com/?cid=1",
        "field": "phone",
        "observed": "0400 111 222",
        "reference": ["(02)%205550%200101", "+61255500101", "0400 111 222", "0400 111 222"],
        "result": "match",
        "evidence_ids": ["ev-presence-r3-016"],
    },
]


def test_d1_stored_encoded_nap_rows_and_finding_render_decoded():
    rows = copy.deepcopy(LEGACY_NAP)
    repaired = repair_nap(rows)
    assert rows == LEGACY_NAP  # the stored rows are not rewritten
    assert repaired[0]["reference"] == ["(02) 5550 0101", "0400 111 222"]
    assert [r["result"] for r in repaired] == ["differs", "match"]
    assert "(business: (02) 5550 0101, 0400 111 222); the source shows 9 digits" in nap_detail(
        [repaired[0]]
    )

    legacy_detail = "Candidate x: phone 040011122 (business: (02)%205550%200101, ...)."
    findings = [
        {
            "id": f"tech-{run}",
            "check_key": "platform:external-nap-inconsistent:facebook",
            "site": "example.com",
            "last_run_id": run,
            "detail": legacy_detail,
        }
        for run in ("r3", "r2")
    ]
    summary: dict[str, Any] = {
        "run_id": "r3",
        "target": "https://www.example.com/",
        "discovery": {"nap": copy.deepcopy(LEGACY_NAP)},
    }
    data = {"digital_presence": summary, "digital_presence_sites": [summary], "findings": findings}
    store.repair_presence_nap(data)
    current, older = data["findings"]
    assert "%" not in current["detail"] and "9 digits" in current["detail"]
    # Only a finding from the same run is rebuilt; the store's own dicts are untouched.
    assert older["detail"] == legacy_detail and findings[0]["detail"] == legacy_detail
    assert data["digital_presence"]["discovery"]["nap"][0]["reference"] == [
        "(02) 5550 0101",
        "0400 111 222",
    ]


# D2 -------------------------------------------------------------------------------------------


def presence_finding(fid, check, *, site="", run="", evidence=()):
    return Finding(
        id=fid,
        title="Priority platforms still need presence verification",
        category="presence",
        check_key=check,
        evidence_ids=list(evidence),
        detail="Work through the manual tasks",
        recommendation="Record the results",
        site=site,
        component=f"https://{site}/" if site else "",
        last_run_id=run,
        provenance="customer_statement",
    ).model_dump(mode="json")


def legacy_store(root, *, legacy_target="https://www.example.com/", successor_run="r3"):
    current = store.new_store(CUSTOMER, "Synthetic")
    current["runs"] = [
        {"run_id": "r1", "kind": "assessment", "target": "https://example.com/"},
        {"run_id": "r2", "kind": "assessment", "target": legacy_target},
        {"run_id": "r3", "kind": "assessment", "target": "https://example.com/"},
    ]
    current["findings"] = [
        # Stored by runs before per-module scoping: no site, no component.
        presence_finding(
            "presence-001",
            "platform:priority-verification",
            evidence=["ev-presence-r1-004", "ev-presence-r2-005"],
        ),
        presence_finding(
            "presence-002", "platform:possible-duplicates:facebook", evidence=["ev-presence-r1-006"]
        ),
        presence_finding(
            "tech-successor",
            "platform:priority-verification",
            site="example.com",
            run=successor_run,
            evidence=["ev-presence-r3-018"],
        ),
    ]
    save(root, current)


def test_d2_website_less_presence_finding_is_superseded_by_its_later_scoped_equivalent(tmp_path):
    legacy_store(tmp_path)
    loaded = store.load(tmp_path, CUSTOMER)
    assert loaded is not None
    # presence-002 has no per-website successor, so it stays current.
    assert [f["id"] for f in loaded["findings"]] == ["presence-002", "tech-successor"]
    retired = next(h for h in loaded["finding_history"] if h["id"] == "presence-001")
    assert retired["status"] == "superseded" and retired["superseded_by"] == "tech-successor"
    successor = loaded["findings"][1]
    assert {"status": "identity-predecessor", "finding_id": "presence-001"}.items() <= (
        successor["events"][-1].items()
    )
    with store.locked(tmp_path, CUSTOMER):
        store.save(tmp_path, CUSTOMER, loaded)
    again = store.load(tmp_path, CUSTOMER)
    assert again is not None and len(again["finding_history"]) == 1  # idempotent


@pytest.mark.parametrize(
    "changes",
    [
        {"legacy_target": "https://other.test/"},  # the legacy runs span two websites
        {"successor_run": "r2"},  # the scoped finding is not a later observation
    ],
)
def test_d2_ambiguous_or_older_successors_never_retire_a_finding(tmp_path, changes):
    legacy_store(tmp_path, **changes)
    loaded = store.load(tmp_path, CUSTOMER)
    assert loaded is not None
    assert {f["id"] for f in loaded["findings"]} == {
        "presence-001",
        "presence-002",
        "tech-successor",
    }
    assert loaded["finding_history"] == []


# D3 / D4 --------------------------------------------------------------------------------------


def lab(n, page, device="mobile", **changes):
    return Finding(
        id=f"lab-{n}",
        category="performance",
        check_key=f"lighthouse-{device}:https://example.test/{page}",
        title=f"Improve {device} lab performance",
        evidence_ids=[f"ev-performance-r1-{n:03d}"],
        detail=f"https://www.example.test/{page}: Lighthouse {device} LAB performance score: "
        f"{60 + n}/100.",
        recommendation="Prioritise measured rendering bottlenecks and retest.",
        component=f"https://example.test/{page}",
        site="example.test",
        **changes,
    )


def axe(n):
    return Finding(
        id=f"axe-{n}",
        category="accessibility",
        check_key="axe:label",
        title="Form elements must have labels",
        evidence_ids=[f"ev-browser-r1-{n:03d}"],
        detail=f"mobile: 1 affected element ({n})",
        recommendation="Label the field",
        component=f"https://example.test/{n}/::mobile",
        site="example.test",
    )


def test_d3_d4_per_page_lab_findings_group_with_scores_and_check_wording():
    findings = [
        lab(1, ""),
        lab(2, "about/"),
        lab(3, "services/"),
        lab(4, "services/", "desktop"),
        lab(5, "contact/", kind="opportunity"),
        axe(6),
        axe(7),
    ]
    views, members = finding_views(findings)
    groups = {v.id: [m.id for m in members[v.id]] for v in views}
    assert groups == {
        "lab-1": ["lab-1", "lab-2", "lab-3"],
        "lab-4": ["lab-4"],
        "lab-5": ["lab-5"],
        "axe-6": ["axe-6", "axe-7"],
    }
    evidence = [
        Evidence(
            id=i, source=i.split("-")[1], url="https://example.test/", description="x", data={}
        )
        for f in findings
        for i in f.evidence_ids
    ]
    text = render_markdown(report_data(findings, evidence), evidence)
    assert text.count("#### lab-1: Improve mobile lab performance") == 1
    assert "#### lab-2:" not in text and "#### lab-3:" not in text
    block = text.split("#### lab-1: Improve mobile lab performance")[1].split("####")[0]
    assert "3 affected pages for this check:" in block and "page/viewport" not in block
    for n, page in [(1, ""), (2, "about/"), (3, "services/")]:
        assert f"- https://example.test/{page} (lab-{n}): " in block
        assert f"score: {60 + n}/100" in block
    axe_block = text.split("#### axe-6: Form elements must have labels")[1].split("####")[0]
    assert "2 affected page/viewport findings for this rule:" in axe_block
    # Display only: JSON keeps every individual finding and ID.
    assert [f.id for f in findings] == [
        f["id"] for f in report_data(findings, evidence)["findings"]
    ]


# D5 -------------------------------------------------------------------------------------------


def inventory_transport(requests):
    pages = {
        "/robots.txt": (
            "User-agent: *\nDisallow: /wp-admin/\nDisallow: /private/\n"
            "Sitemap: https://example.com/sitemap.xml\n"
        ),
        "/sitemap.xml": (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(
                f"<url><loc>{loc}</loc></url>"
                for loc in [
                    "https://example.com/",
                    "https://example.com/unlinked/",
                    "https://example.com/private/report/",
                    "https://example.com/brochure.pdf",
                    "https://example.com/wp-content/uploads/photo.jpg",
                    "https://example.com/feed/",
                    "https://other.test/elsewhere/",
                ]
            )
            + "</urlset>"
        ),
        "/wp-json/wp/v2/types": json.dumps(
            {"page": {"rest_base": "pages"}, "attachment": {"rest_base": "media"}}
        ),
        "/wp-json/wp/v2/pages": json.dumps(
            [{"link": "https://example.com/careers/"}, {"link": "https://example.com/portal/"}]
        ),
        "/wp-json/wp/v2/media": json.dumps([{"link": "https://example.com/logo-attachment/"}]),
    }

    def respond(request):
        requests.append(request.url.path)
        body = pages.get(request.url.path)
        return httpx.Response(200, text=body) if body else httpx.Response(404)

    return httpx.MockTransport(respond)


def test_d5_crawl_seeds_are_known_same_site_html_pages_within_robots_and_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(coverage, "REQUEST_INTERVAL", 0)
    requests = []
    ctx = context(tmp_path, max_pages=10)
    inventory = coverage.seed_inventory(ctx, inventory_transport(requests))
    # Sitemap-only and WordPress-inventory pages; never the start URL, robots-disallowed
    # paths, media/attachments, feeds or other origins.
    assert inventory["seeds"] == [
        "https://example.com/unlinked/",
        "https://example.com/careers/",
        "https://example.com/portal/",
    ]
    assert ctx.inventory is inventory and inventory["seed_limit"] == 5
    assert requests.count("/sitemap.xml") == 1
    small = context(tmp_path, max_pages=4)
    assert coverage.seed_inventory(small, inventory_transport([]))["seeds"] == [
        "https://example.com/unlinked/",
        "https://example.com/careers/",
    ]
    assert small.inventory["seeds_over_limit"] == ["https://example.com/portal/"]

    # Coverage reuses the cached inventory (no second fetch) and reports the seeds.
    ctx.pages = ["https://example.com/", "https://example.com/unlinked/"]
    crawl = ModuleResult(name="siteone", collection_started=True)
    crawl.add_evidence(ctx.site_url, "SiteOne HTTP response", {"type": 1, "status": 200})
    ctx.results = [crawl]
    before = len(requests)
    result = coverage.collect(ctx, inventory_transport(requests))
    assert len(requests) == before
    assert result["seeded"] == inventory["seeds"]
    assert (
        "3 sitemap/WordPress inventory pages were seeded into the crawl (1 crawled"
        in (result["summary"])
    )
    assert "https://example.com/unlinked/" not in result["sitemap_not_reached"]


def test_d5_siteone_receives_seeds_and_browser_reserves_slots_for_them(tmp_path, monkeypatch):
    ctx = context(tmp_path, max_pages=10)
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    seeds = ["https://example.com/unlinked/"]

    def seed_inventory(ctx):
        ctx.inventory = {"seeds": seeds, "seed_limit": 5, "seeds_over_limit": []}
        return ctx.inventory

    monkeypatch.setattr(coverage, "seed_inventory", seed_inventory)
    commands = []

    def process(command, **kwargs):
        commands.append(command)
        rows = [{"url": u, "status": "200", "type": 1} for u in [ctx.site_url, *seeds]]
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"results": rows}))

    monkeypatch.setattr(siteone.subprocess, "run", process)
    result = siteone.run(ctx)
    listing = next(c for c in commands[0] if c.startswith("--url-list="))
    assert Path(listing.split("=", 1)[1]).read_text().split() == [ctx.site_url, *seeds]
    assert f"--max-visited-urls={ctx.max_pages}" in commands[0]
    assert seeds[0] in ctx.pages
    recorded = next(e for e in result.evidence if e.description.startswith("Crawl seeds"))
    assert recorded.data["urls"] == seeds

    pages = ["https://example.com/"] + [
        f"https://example.com/{p}/" for p in ("about", "contact", "quote", "services", "book")
    ]
    pages.append("https://example.com/unlinked/")
    assert "https://example.com/unlinked/" not in browser.select_pages(pages, pages[0], 6)
    sample = browser.select_pages(pages, pages[0], 6, seeds)
    assert len(sample) == 6 and sample[0] == pages[0] and "https://example.com/unlinked/" in sample
    # Below three slots nothing is reserved: the key-page order is unchanged.
    assert browser.select_pages(pages, pages[0], 2, seeds) == browser.select_pages(
        pages, pages[0], 2
    )


# D6 -------------------------------------------------------------------------------------------


def coverage_run(run_id, target, summary):
    return {
        "run_id": run_id,
        "target": target,
        "summary": summary,
        "limits": {},
        "stop_reasons": [],
        "discovered": [{"url": target, "sources": ["crawl links"]}],
        "crawled": [target],
        "browser_tested": [target],
        "untested": [],
        "sitemap_not_reached": [],
        "robots_declarations": [],
        "sitemaps": [],
        "excluded": [],
        "blocked": [],
        "failed": [],
        "redirected": [],
        "wordpress": {"status": "not detected", "reason": "No WordPress signal"},
    }


def test_d6_coverage_shows_latest_per_website_and_one_line_for_earlier_runs():
    findings = [lab(1, "")]
    evidence = [
        Evidence(
            id="ev-performance-r1-001", source="performance", url="x", description="x", data={}
        )
    ]
    runs = [
        coverage_run("r7", "https://example.test/", "Seven crawled."),
        coverage_run("r18", "https://www.example.test/", "Eighteen crawled."),
        coverage_run("r19", "https://example.test/", "Nineteen crawled."),
        coverage_run("r4", "https://other.test/", "Other crawled."),
    ]
    data = report_data(findings, evidence, website_coverage_runs=copy.deepcopy(runs))
    text = render_markdown(data, evidence)
    headings = [line for line in text.splitlines() if line.startswith("### Website coverage")]
    assert headings == [
        "### Website coverage: https://other.test/ (r4)",
        "### Website coverage: https://example.test/ (r19)",
    ]
    assert (
        "Earlier coverage: r7 (https://example.test/): Seven crawled; "
        "r18 (https://www.example.test/): Eighteen crawled. Full detail" in text
    )
    assert text.count("Nineteen crawled.") == 1 and "Other crawled." in text
    assert data["website_coverage_runs"] == runs  # JSON detail retained


# D7 -------------------------------------------------------------------------------------------

SITE = "https://www.example.test/"


def gsc_evidence(sitemaps=None, rows=None, prefix="ev"):
    return [
        Evidence(
            id=f"{prefix}-gsc-001",
            source="gsc",
            url="https://example.test/",
            description="GSC top queries/pages and sitemap list (28 complete days)",
            data={
                "property": "sc-domain:example.test",
                "page": {
                    "rows": rows
                    if rows is not None
                    else [
                        {"keys": [SITE], "clicks": 50, "impressions": 400},
                        {"keys": ["http://www.example.test/"], "clicks": 19, "impressions": 321},
                        {"keys": [SITE + "sample-page/"], "clicks": 1, "impressions": 96},
                        {"keys": [SITE + "hello-world/"], "clicks": 0, "impressions": 1},
                        {"keys": [SITE + "category/uncategorized/"], "impressions": 74},
                        {"keys": ["http://shop.example.test/"], "impressions": 9},
                    ]
                },
                "sitemaps": sitemaps if sitemaps is not None else {},
            },
        ),
        Evidence(
            id=f"{prefix}-presence-011",
            source="presence",
            url=SITE + "sitemap.xml",
            description="Bounded robots.txt and XML sitemap fetch",
            data={"status": "PASS", "sitemap_url": SITE + "sitemap.xml"},
        ),
        Evidence(
            id=f"{prefix}-siteone-001",
            source="siteone",
            url="https://example.test/",
            description="Target redirect resolution before crawling",
            data={"resolved": SITE},
        ),
    ]


def test_d7_search_console_evidence_becomes_deterministic_connected_findings():
    evidence = gsc_evidence()
    findings, outcomes = gsc.connected_findings(evidence, SITE)
    by_key = {f.check_key: f for f in findings}
    assert list(by_key) == [
        "gsc:non-canonical-variants",
        "gsc:sitemap-not-submitted",
        "gsc:placeholder-impressions",
    ]
    assert {o.check_id: o.status for o in outcomes} == dict.fromkeys(by_key, "fail")
    variants = by_key["gsc:non-canonical-variants"]
    assert "http://www.example.test/ (321 impressions, 19 clicks) alongside " in variants.detail
    assert "https://www.example.test/ (400 impressions" in variants.detail
    assert "shop.example.test" not in variants.detail  # other subdomains are other websites
    assert variants.evidence_ids == ["ev-gsc-001", "ev-siteone-001"]
    assert variants.urls == ["http://www.example.test/"]
    sitemap = by_key["gsc:sitemap-not-submitted"]
    assert sitemap.evidence_ids == ["ev-gsc-001", "ev-presence-011"]
    assert "lists no submitted sitemaps" in sitemap.detail and sitemap.kind == "opportunity"
    placeholder = by_key["gsc:placeholder-impressions"]
    assert placeholder.urls == [
        SITE + "sample-page/",
        SITE + "hello-world/",
        SITE + "category/uncategorized/",
    ]
    assert "sample-page/ (96 impressions, 1 click), a WordPress sample page" in placeholder.detail
    assert "hello-world/ (1 impression, 0 clicks)" in placeholder.detail
    for finding in findings:
        assert finding.confidence == "confirmed" and finding.provenance == "technical_evidence"
        assert finding.detail.startswith("Confirmed from Search Console (ev-gsc-001)")
        assert finding.recommendation.startswith("Needs verification")

    # Section 7 shows them, labelled from authorised evidence, and nowhere else.
    shown = scoped(findings, SITE, CUSTOMER)
    text = render_markdown(report_data(shown, evidence), evidence)
    section = text.split("## 7. Connected business-system findings")[1].split("## 9.")[0]
    for finding in findings:
        assert finding.title in section
    assert text.split("## 7.")[0].count("Placeholder pages receive") == 0


def test_d7_no_condition_passes_and_missing_inputs_are_not_assessed():
    rows = [{"keys": [SITE], "clicks": 5, "impressions": 50}]
    findings, outcomes = gsc.connected_findings(
        gsc_evidence(sitemaps={"sitemap": [{"path": SITE + "sitemap.xml"}]}, rows=rows), SITE
    )
    assert findings == [] and {o.status for o in outcomes} == {"pass"}
    without_public = [e for e in gsc_evidence(rows=rows) if e.source == "gsc"]
    _, outcomes = gsc.connected_findings(without_public, SITE)
    statuses = {o.check_id: o.status for o in outcomes}
    assert statuses["gsc:sitemap-not-submitted"] == "unavailable"


def test_d7_ai_cannot_write_findings_on_connected_evidence():
    evidence = gsc_evidence()
    item = {
        "id": "ai-placeholder",
        "title": "Placeholder pages appear in search results",
        "category": "seo",
        "evidence_ids": ["ev-gsc-001"],
        "detail": "A sample page has impressions.",
        "recommendation": "Remove it.",
    }
    _, findings, warnings = ai.validate_analysis({"findings": [item]}, evidence, {"seo"}, [])
    assert findings == [] and "connected-evidence" in warnings[0]


def test_d7_stored_runs_are_analysed_once_and_replace_ai_connected_findings(tmp_path):
    current = store.new_store(CUSTOMER, "Synthetic")
    del current["connected_analysis"]  # a store written before the analysis existed
    current["runs"] = [
        {
            "run_id": "r2",
            "kind": "assessment",
            "target": "https://example.test/",
            "resolved_url": SITE,
            "created_at": "2026-10-20T00:00:00+00:00",
        }
    ]
    current["evidence"] = [e.model_dump(mode="json") for e in gsc_evidence(prefix="ev")]
    for e in current["evidence"]:
        e["id"] = e["id"].replace("-0", "-r2-0", 1)  # namespaced as the store does
    ai_finding = Finding(
        id="tech-ai",
        title="Placeholder pages appear in search results",
        category="seo",
        check_key="site-index-sample-page-exposed",
        evidence_ids=["ev-gsc-r2-001"],
        detail="AI wording",
        recommendation="Remove",
        site="example.test",
        component=SITE,
        confidence="needs_verification",
    )
    current["findings"] = [ai_finding.model_dump(mode="json")]
    save(tmp_path, current)
    loaded = store.load(tmp_path, CUSTOMER)
    assert loaded is not None and loaded["connected_analysis"] == store.CONNECTED_ANALYSIS
    keys = {f["check_key"]: f for f in loaded["findings"]}
    assert set(keys) == {
        "gsc:non-canonical-variants",
        "gsc:sitemap-not-submitted",
        "gsc:placeholder-impressions",
    }
    assert all(f["last_run_id"] == "r2" and f["site"] == "example.test" for f in keys.values())
    assert keys["gsc:sitemap-not-submitted"]["evidence_ids"] == [
        "ev-gsc-r2-001",
        "ev-presence-r2-011",
    ]
    retired = loaded["finding_history"]
    assert [h["id"] for h in retired] == ["tech-ai"] and retired[0]["status"] == "superseded"
    save(tmp_path, loaded)
    again = store.load(tmp_path, CUSTOMER)
    assert again is not None and len(again["findings"]) == 3 and len(again["finding_history"]) == 1


# D8 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Enquiries are going to soar after the fix.",
        "Bookings are set to climb.",
        "Leads should increase.",
        "Revenue is expected to grow next quarter.",
        "Calls are likely to jump once the form works.",
        "Traffic is set to double.",
        "The fix is going to lift enquiries.",
        "The fix should boost monthly enquiries.",
        "This should improve qualified leads.",
        "The fix should double our overall monthly enquiries.",
    ],
)
def test_d8_future_outcome_phrasings_are_promises(text):
    assert figures.OUTCOME_FORECAST.search(text)
    assert figures.followup_promise(text)
    assert ai.unsupported_claim(text, [])


@pytest.mark.parametrize(
    "text",
    [
        "You should test the form.",
        "You should review the contact form.",
        "Leads are expected to be recorded in the CRM.",
        "The form is set to send enquiries to the office.",
        "Enquiries rose last year.",
        "Calls should be answered within one business day.",
        "You should improve the booking page so it loads faster.",
        "You could boost the call button contrast.",
        "You can improve the enquiry form by adding labels.",
        "This should improve the leads form.",
        "We should improve how enquiries are tracked.",
        "We should improve how we handle enquiries.",
        "You should improve the tracking of enquiries.",
    ],
)
def test_d8_instructions_and_observations_are_not_promises(text):
    assert not figures.OUTCOME_FORECAST.search(text)
    assert not ai.unsupported_claim(text, [])


def test_d8_ai_findings_with_forecasts_are_dropped_and_instructions_kept():
    evidence = [
        Evidence(
            id="ev-browser-001",
            source="browser",
            url="https://example.test/",
            description="x",
            data={},
        )
    ]

    def item(fid, impact):
        return {
            "id": fid,
            "title": "Enquiry form needs testing",
            "category": "cro_ux",
            "evidence_ids": ["ev-browser-001"],
            "detail": "The form was not submitted.",
            "recommendation": "You should test the form.",
            "business_impact": impact,
        }

    _, findings, warnings = ai.validate_analysis(
        {
            "findings": [
                item("ai-1", "Enquiries are going to soar after the fix."),
                item("ai-2", "May affect enquiries; not measured."),
            ]
        },
        evidence,
        {"cro_ux"},
        [],
    )
    assert [f.id for f in findings] == ["ai-2"]
    assert findings[0].recommendation == "You should test the form."
    dropped = [w for w in warnings if w.startswith("Dropped AI finding")]
    assert len(dropped) == 1 and "promised outcome" in dropped[0]


# D9 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://javascript:alert(1)/",
        "https://x.test:bad/",
        "https://x.test:/",
        "https://x.test:99999/",
        "https://exa mple.test/",
        "https://-bad.test/",
        "https://example..test/",
        "https://example.test../",
        "https://example..test./",
        "https://./",
        "https://../",
    ],
)
def test_d9_register_site_rejects_malformed_hosts_and_ports_cleanly(tmp_path, value):
    with pytest.raises(ValueError, match="valid host name and an optional numeric port") as error:
        register.site_name(value)
    assert value not in str(error.value)
    store.set_profile(tmp_path, CUSTOMER, {"name": "Synthetic"})
    path = tmp_path / CUSTOMER / "assessment.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store.set_register(tmp_path, CUSTOMER, "ga4", {"ams_access": "granted"}, site=value)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "value, expected",
    [
        ("example.test", "https://example.test/"),
        ("https://Example.Test:8443/path", "https://example.test:8443/"),
        ("http://example.test:80/", "http://example.test:80/"),
        ("https://[::1]:8080/", "https://[::1]:8080/"),
    ],
)
def test_d9_register_site_keeps_valid_scopes(value, expected):
    assert register.site_name(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("example.test.", "https://example.test/"),
        ("https://WWW.Example.test./path", "https://www.example.test/"),
        ("http://Example.Test.:8080/path", "http://example.test:8080/"),
        ("https://bücher.de./path", "https://bücher.de/"),
    ],
)
def test_d9_register_site_normalises_a_single_trailing_dot(value, expected):
    assert register.site_name(value) == expected


def test_d9_file_read_refuses_a_file_swapped_after_the_allowlist_check(tmp_path):
    path = tmp_path / "report.html"
    path.write_text("checked")
    checked = path.stat()
    assert ui.read_checked(path, checked, directory=tmp_path) == b"checked"
    replacement = tmp_path / "other.html"
    replacement.write_text("swapped")
    os.replace(replacement, path)  # same name, different file
    assert ui.read_checked(path, checked, directory=tmp_path) is None
    path.unlink()
    (tmp_path / "secret.html").write_text("secret")
    path.symlink_to(tmp_path / "secret.html")
    assert (
        ui.read_checked(path, (tmp_path / "secret.html").stat(), directory=tmp_path) is None
    )  # O_NOFOLLOW


@contextmanager
def running(root):
    with ui.LocalServer(root, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=server.origin, trust_env=False,
                              headers={"Authorization": "Bearer " + server.token}) as client:
                yield server, client
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_d9_file_route_does_not_serve_a_swap_between_check_and_read(tmp_path, monkeypatch):
    store.set_profile(tmp_path, CUSTOMER, {"name": "Synthetic"})
    outputs = tmp_path / CUSTOMER / "reports/001-fixture"
    outputs.mkdir(parents=True)
    (outputs / "report.md").write_text("Allowed report")
    (tmp_path / "outside.md").write_text("OUTSIDE")
    current = store.load(tmp_path, CUSTOMER)
    assert current is not None
    current["report_revisions"] = [{"directory": "reports/001-fixture", "files": ["report.md"]}]
    save(tmp_path, current)
    checked = ui.checked_outputs
    swap = {"enabled": False}

    def racing(root, customer, current):
        allowed = checked(root, customer, current)
        if swap["enabled"]:  # the race: replace the file after it passed the checks
            (outputs / "report.md").unlink()
            (outputs / "report.md").symlink_to(tmp_path / "outside.md")
        return allowed

    monkeypatch.setattr(ui, "checked_outputs", racing)
    with running(tmp_path) as (server, client):
        url = server.url("/file", customer=CUSTOMER, path="reports/001-fixture/report.md")
        response = client.get(url)
        assert response.status_code == 200 and response.text == "Allowed report"
        swap["enabled"] = True
        response = client.get(url)
        assert response.status_code == 403 and "OUTSIDE" not in response.text


@pytest.mark.parametrize(
    "note, rejected",
    [
        ("api-key ABCDEFGH", True),
        ("API key 9f8e7d6c5b4a", True),
        ("password Summer2024!", True),
        ("client secret abc123def456", True),
        ("access token AbCdEfGhIj", True),
        ("The api key is stored in the vault", False),
        ("Customer will reset the password tomorrow", False),
        ("token expiry handled by IT", False),
        ("password Management policy", False),
        ("Access token rotation scheduled", False),
        ("token 2024-10-23 renewal", False),
        ("password LastPass", False),
        ("password WordPress", False),
        ("token SharePoint", False),
        ("api key OAuth2.0", False),
        ("token 12345678 is the order number", False),
        ("api key a9G7k2Q8m4Z1w6R3t0V5x9P2", True),
        ("client secret qwejvkdfnzyapohgumlrstxcbi", True),
        ("token ghp_abcdefghijklmnopqrstuvwxyz1234567890", True),
    ],
)
def test_d9_space_separated_secrets_are_rejected_and_prose_kept(note, rejected):
    if rejected:
        with pytest.raises(ValueError, match="Secret-looking") as error:
            register.safe_text(note)
        assert note not in str(error.value)
    else:
        assert register.safe_text(note) == note


def test_d1_decoding_a_matching_phone_clears_a_false_stored_inconsistency():
    rows = copy.deepcopy(LEGACY_NAP[:1])
    rows[0].update(observed="02 5550 0101", reference=["(02)%205550%200101"])
    candidate = {"url": rows[0]["url"], "state": "Found — needs verification", "inconsistent": True}
    summary: dict[str, Any] = {
        "run_id": "r3",
        "target": "https://example.com/",
        "discovery": {
            "nap": rows,
            "platforms": [{"key": "facebook", "state": INCONSISTENT, "candidates": [candidate]}],
        },
    }
    data: dict[str, Any] = {
        "digital_presence": copy.deepcopy(summary),
        "findings": [
            {
                "id": "tech-nap",
                "check_key": "platform:external-nap-inconsistent:facebook",
                "last_run_id": "r3",
                "site": "example.com",
                "detail": "False mismatch",
            }
        ],
    }
    store.repair_presence_nap(data)
    discovery = data["digital_presence"]["discovery"]
    assert discovery["nap"][0]["result"] == "match"
    assert discovery["platforms"][0]["state"] != INCONSISTENT
    assert not discovery["platforms"][0]["candidates"][0]["inconsistent"]
    assert data["findings"][0]["lifecycle_status"] == "resolved"
    assert summary["discovery"]["nap"][0]["result"] == "differs"


def test_d2_resolved_successor_also_supersedes_legacy_presence_finding(tmp_path):
    legacy_store(tmp_path)
    path = tmp_path / CUSTOMER / "assessment.json"
    current = json.loads(path.read_text())
    current["findings"][-1]["lifecycle_status"] = "resolved"
    save(tmp_path, current)
    loaded = store.load(tmp_path, CUSTOMER)
    assert loaded is not None
    assert "presence-001" not in {f["id"] for f in loaded["findings"]}
    assert loaded["findings"][-1]["lifecycle_status"] == "resolved"


def merge_module(root, run, target, result):
    from ams_assess import outcomes
    from ams_assess.consolidate import consolidate

    ctx = context(root, target=target)
    outcomes.record(ctx, result)
    findings = consolidate(result.findings, result.evidence)
    data = {
        "target": target,
        "created_at": f"2026-10-{run:02d}T00:00:00Z",
        "status": "complete",
        "selected_categories": ["presence", "seo"],
        "assessed_categories": ["presence", "seo"],
        "findings": [f.model_dump() for f in findings],
        "outcomes": [o.model_dump() for o in result.outcomes],
        "ai": {"status": "skipped"},
        "modules": [{"name": result.name, "status": "ok", "reason": ""}],
        "pages": [],
        "max_pages": 10,
        "limitations": [],
        "future": [],
        "report_files": [],
    }
    return store.merge_run(
        root, CUSTOMER, "Synthetic", data, result.evidence, f"run-{run}", ["public"]
    )


def test_d2_repeat_presence_runs_keep_one_identity_per_site_and_check(tmp_path):
    checks = ("platform:priority-verification", "platform:possible-duplicates:facebook")
    for run, target in enumerate(
        ["https://example.test/", "https://www.example.test/", "https://other.test/"], 1
    ):
        module = ModuleResult(name="presence")
        eid = module.add_evidence(target, "Customer profile", {})
        module.evidence[-1].provenance = "customer_statement"
        for check in checks:
            module.finding(check, "Verify presence", "presence", [eid], "Unverified", "Verify")
        merged = merge_module(tmp_path, run, target, module)
        if run == 1:
            original_ids = {f["id"] for f in merged["findings"]}
        elif run == 2:
            assert {f["id"] for f in merged["findings"]} == original_ids
            assert all(f["last_run_id"] == "r2" for f in merged["findings"])
            assert all(len(f["events"]) == 2 for f in merged["findings"])
            assert all(f["provenance"] == "customer_statement" for f in merged["findings"])
    assert len(merged["findings"]) == 4
    assert {f["site"] for f in merged["findings"]} == {"example.test", "other.test"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("site", "other.test"),
        ("lifecycle_status", "resolved"),
        ("kind", "opportunity"),
        ("provenance", "customer_statement"),
        ("ams_service", "Business Systems"),
    ],
)
def test_d3_lab_groups_keep_distinct_scopes_and_bases_separate(field, value):
    first, second = lab(1, ""), lab(2, "about/")
    setattr(second, field, value)
    views, _ = finding_views([first, second])
    assert len(views) == 2


def test_d4_non_axe_accessibility_group_uses_pages_wording():
    findings = [axe(1), axe(2)]
    for f in findings:
        f.check_key = "manual-label-review"
    evidence = [
        Evidence(id=e, source="browser", url="https://example.test/", description="x", data={})
        for f in findings
        for e in f.evidence_ids
    ]
    text = render_markdown(report_data(findings, evidence), evidence)
    assert "2 affected pages for this check" in text
    assert "affected page/viewport findings for this rule" not in text


def test_d5_inventory_without_wordpress_hint_and_inherited_attachments(tmp_path, monkeypatch):
    monkeypatch.setattr(coverage, "REQUEST_INTERVAL", 0)
    requests = []

    def respond(request):
        requests.append(request.url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://example.com/logo/</loc></url></urlset>"
                ),
            )
        if request.url.path == "/wp-json/wp/v2/types":
            return httpx.Response(
                200, json={"page": {"rest_base": "pages"}, "attachment": {"rest_base": "media"}}
            )
        if request.url.path == "/wp-json/wp/v2/pages":
            return httpx.Response(
                200, json=[{"link": "https://example.com/careers/", "status": "publish"}]
            )
        assert request.url.path == "/wp-json/wp/v2/media"
        assert "status" not in request.url.params  # WordPress media does not use publish.
        return httpx.Response(
            200, json=[{"link": "https://example.com/logo/", "status": "inherit"}]
        )

    inventory = coverage.seed_inventory(context(tmp_path), httpx.MockTransport(respond))
    assert inventory["seeds"] == ["https://example.com/careers/"]
    assert inventory["wordpress"]["attachment_urls"] == ["https://example.com/logo/"]


@pytest.mark.parametrize("status,body", [(503, ""), (200, "User-agent: *\nDisallow: /\n")])
def test_d5_unavailable_or_deny_all_robots_prevents_inventory_page_seeding(tmp_path, status, body):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(status, text=body)
        assert request.url.path == "/sitemap.xml"
        return httpx.Response(
            200,
            text=(
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://example.com/private/</loc></url></urlset>"
            ),
        )

    inventory = coverage.seed_inventory(context(tmp_path), httpx.MockTransport(respond))
    assert inventory["seeds"] == []
    assert not any("wp-json" in p for p in requests)


def test_d7_migration_keeps_unrelated_findings_using_the_same_gsc_evidence():
    current = store.new_store(CUSTOMER, "Synthetic")
    del current["connected_analysis"]
    current["runs"] = [
        {"run_id": "r2", "kind": "assessment", "target": SITE, "created_at": "2026-10-20T00:00:00Z"}
    ]
    current["evidence"] = [e.model_dump() for e in gsc_evidence()]
    for e in current["evidence"]:
        e["id"] = e["id"].replace("-0", "-r2-0", 1)
    unrelated = Finding(
        id="ai-other",
        check_key="unrelated-search-review",
        category="seo",
        title="Review query intent",
        detail="Different observation",
        recommendation="Review",
        evidence_ids=["ev-gsc-r2-001"],
        site="example.test",
    )
    findings = store.analyse_stored_connected(current, [unrelated])
    assert unrelated in findings
    assert len(findings) == 4 and current["finding_history"] == []


def test_d7_later_passing_gsc_sample_resolves_stored_deterministic_findings(tmp_path):
    for run in (1, 2):
        evidence = (
            gsc_evidence()
            if run == 1
            else gsc_evidence(
                rows=[{"keys": [SITE], "impressions": 50}],
                sitemaps={"sitemap": [{"path": SITE + "sitemap.xml"}]},
            )
        )
        findings, outcomes = gsc.connected_findings(evidence, SITE)
        module = ModuleResult(name="gsc", evidence=evidence, findings=findings, outcomes=outcomes)
        merged = merge_module(tmp_path, run, SITE, module)
        assert len(merged["findings"]) == 3
        assert {f["lifecycle_status"] for f in merged["findings"]} == (
            {"current"} if run == 1 else {"resolved"}
        )
    assert all(f["resolution"]["run_id"] == "r2" for f in merged["findings"])


def test_d9_allowlist_does_not_capture_replacement_identity_after_path_checks(
    tmp_path, monkeypatch
):
    path = tmp_path / CUSTOMER / "reports/001-synthetic/report.md"
    path.parent.mkdir(parents=True)
    path.write_text("original")
    replacement = path.with_name("replacement.md")
    replacement.write_text("REPLACEMENT")
    current = {"report_revisions": [{"directory": "reports/001-synthetic", "files": ["report.md"]}]}
    open_file = os.open

    def racing(name, flags, *args, **kwargs):
        fd = open_file(name, flags, *args, **kwargs)
        if name == "report.md" and replacement.exists():
            os.replace(replacement, path)
        return fd

    monkeypatch.setattr(os, "open", racing)
    allowed = ui.checked_outputs(tmp_path, CUSTOMER, current)
    assert (
        ui.read_checked(*allowed["reports/001-synthetic/report.md"], directory=tmp_path / CUSTOMER)
        is None
    )


def test_d9_checked_file_disappearing_is_refused_without_server_error(tmp_path, monkeypatch):
    path = tmp_path / CUSTOMER / "reports/001-synthetic/report.md"
    path.parent.mkdir(parents=True)
    path.write_text("original")
    current = {"report_revisions": [{"directory": "reports/001-synthetic", "files": ["report.md"]}]}
    open_file = os.open

    def disappearing(name, flags, *args, **kwargs):
        if name == "report.md":
            path.unlink()
        return open_file(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", disappearing)
    assert ui.checked_outputs(tmp_path, CUSTOMER, current) == {}


@pytest.mark.parametrize(
    "path, allowed",
    [
        ("/", False),
        ("/unlinked.html", True),
        ("/hidden-page/", False),
        ("/hidden-public/", True),
        ("/page?private=yes", False),
    ],
)
def test_d5_seed_robots_support_wildcards_specific_allow_and_end_anchor(path, allowed):
    robots = coverage.CrawlRobots()
    robots.parse(
        [
            "User-agent: *",
            "Disallow: /$",
            "Disallow: /hidden*",
            "Allow: /hidden-public/",
            "Disallow: /*?private=",
        ]
    )
    assert robots.can_fetch("AMSAssessment/1.0", "https://example.test" + path) is allowed


def test_d5_seed_list_never_bypasses_a_disallowed_start_page(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})

    def seed_inventory(ctx):
        ctx.inventory = {
            "seeds": [ctx.target + "allowed/"],
            "seed_limit": 5,
            "seeds_over_limit": [],
            "target_allowed": False,
        }
        return ctx.inventory

    monkeypatch.setattr(coverage, "seed_inventory", seed_inventory)

    def process(command, **kwargs):
        listing = next(c.split("=", 1)[1] for c in command if c.startswith("--url-list="))
        assert Path(listing).read_text().splitlines() == [ctx.target + "allowed/"]
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {"results": [{"url": ctx.target + "allowed/", "status": 200, "type": 1}]}
            ),
        )

    monkeypatch.setattr(siteone.subprocess, "run", process)
    assert siteone.run(ctx).status == "ok"
    assert ctx.target not in ctx.pages


def test_d7_runner_analyses_gsc_after_public_collectors_without_ai(tmp_path, monkeypatch):
    from ams_assess import runner

    evidence = gsc_evidence()
    registry = {}
    for name in ("gsc", "presence", "siteone"):

        def collect(ctx, name=name):
            return ModuleResult(name=name, evidence=[e for e in evidence if e.source == name])

        registry[name] = SimpleNamespace(categories={"seo"}, run=collect)
    monkeypatch.setattr(runner, "REGISTRY", registry)
    monkeypatch.setattr(coverage, "collect", lambda ctx: {})
    ctx = context(tmp_path, target=SITE, categories={"seo"}, store_root=tmp_path)
    result = runner.run(ctx, no_ai=True, formats={"md"})
    assert result["status"] == "complete" and result.get("store_status") != "failed"
    assert {f["check_key"] for f in result["findings"]} == {
        "gsc:non-canonical-variants",
        "gsc:sitemap-not-submitted",
        "gsc:placeholder-impressions",
    }
    text = (ctx.run_dir / "report.md").read_text()
    section = text.split("## 7. Connected business-system findings")[1].split("## 9.")[0]
    assert all(f["title"] in section for f in result["findings"])
    saved = store.load(tmp_path, CUSTOMER)
    assert saved is not None and len(saved["findings"]) == 3


@pytest.mark.parametrize(
    "marker", ["are going to", "are set to", "are expected to", "should", "are likely to"]
)
@pytest.mark.parametrize(
    "direction", ["rise", "increase", "grow", "soar", "climb", "jump", "double"]
)
def test_d8_structured_forecast_combinations(marker, direction):
    assert ai.unsupported_claim(f"Leads {marker} {direction} after the fix.", [])


@pytest.mark.parametrize("value", ["https://[::1]garbage/", "https://[::1", "https://[not-an-ip]/"])
def test_d9_malformed_ipv6_scope_gives_a_clean_error(value):
    with pytest.raises(ValueError) as error:
        register.site_name(value)
    assert value not in str(error.value)


def test_d5_real_crawler_respects_seed_budget_and_exclusions(tmp_path, allow_local_fixture):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    binary = REPO / "tools/siteone/siteone-crawler/siteone-crawler"
    if not binary.is_file():
        pytest.skip("Run scripts/setup.sh for SiteOne")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            base = f"http://127.0.0.1:{server.server_port}"
            if self.path == "/robots.txt":
                body, kind = "User-agent: *\nDisallow: /hidden*\n", "text/plain"
            elif self.path == "/sitemap.xml":
                body = (
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(
                        f"<url><loc>{base}/{p}</loc></url>"
                        for p in [
                            "unlinked.html",
                            "hidden-page.html",
                            "manual.pdf",
                            "inventory.html",
                            "overflow.html",
                        ]
                    )
                    + "</urlset>"
                )
                kind = "application/xml"
            elif self.path.startswith("/wp-json"):
                self.send_error(404)
                return
            else:
                body, kind = (
                    '<html><title>Page</title><h1>Page</h1><a href="/linked.html">Link</a></html>',
                    "text/html",
                )
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx = context(
            tmp_path,
            target=f"http://127.0.0.1:{server.server_port}/",
            max_pages=4,
            tools_dir=REPO / "tools",
        )
        result = siteone.run(ctx)
        assert result.status == "ok"
        assert len(ctx.pages) <= 4
        assert ctx.target + "unlinked.html" in ctx.pages
        assert ctx.target + "inventory.html" in ctx.pages
        assert "/hidden-page.html" not in requests and "/manual.pdf" not in requests
        assert "/overflow.html" not in requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "instruction",
    [
        "You should improve the booking page so it loads faster.",
        "You could boost the call button contrast.",
        "You can improve the enquiry form by adding labels.",
        "This should improve the leads form.",
        "We should improve how enquiries are tracked.",
    ],
)
def test_review_d8_keeps_instruction_findings(instruction):
    evidence = [
        Evidence(
            id="ev-browser-001",
            source="browser",
            url="https://example.test/",
            description="Form observation",
            data={},
        )
    ]
    finding = {
        "id": "ai-form",
        "title": "Review the enquiry form",
        "category": "cro_ux",
        "evidence_ids": [evidence[0].id],
        "detail": "The form needs review.",
        "recommendation": instruction,
    }
    analysis, findings, warnings = ai.validate_analysis(
        {"findings": [finding], "executive_summary": instruction + " (ev-browser-001)"},
        evidence,
        {"cro_ux"},
        [],
    )
    assert not warnings
    assert len(findings) == 1 and findings[0].recommendation == instruction
    assert analysis["executive_summary"] == instruction + " (ev-browser-001)"


@pytest.mark.parametrize(
    "probe_status,probe_body",
    [
        (404, "not found"),
        (200, "<html>Not a WordPress website</html>"),
        (200, '{"message": "This is a different API"}'),
    ],
)
def test_review_d5_non_wordpress_probe_is_not_an_access_gap(tmp_path, probe_status, probe_body):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://example.com/unlinked.html</loc></url></urlset>"
                ),
            )
        return httpx.Response(probe_status, text=probe_body)

    transport = httpx.MockTransport(respond)
    ctx = context(tmp_path)
    coverage.seed_inventory(ctx, transport)
    assert ctx.inventory["seeds"] == ["https://example.com/unlinked.html"]
    collected = coverage.collect(ctx, transport)
    assert collected["wordpress"]["status"] == "not detected"
    assert collected["wordpress"].get("stop_reasons", []) == []
    assert not any("WordPress" in r or coverage.UNAVAILABLE in r for r in collected["stop_reasons"])
    assert requests.count("/wp-json/wp/v2/types") == 1
    # A separate positive WordPress signal must still expose a missing REST inventory.
    known = ModuleResult(name="presence")
    known.add_evidence(ctx.target, "Platform signal: WordPress", {})
    ctx.results = [known]
    collected = coverage.collect(ctx, transport)
    assert collected["wordpress"]["status"] == "unavailable"
    assert coverage.UNAVAILABLE in collected["stop_reasons"]
    assert requests.count("/wp-json/wp/v2/types") == 1


def test_review_d9_parent_swap_during_allowlisting_and_read_is_refused(tmp_path, monkeypatch):
    store.set_profile(tmp_path, CUSTOMER, {"name": "Synthetic"})
    directory = tmp_path / CUSTOMER
    parent = directory / "reports/001-fixture"
    parent.mkdir(parents=True)
    target = parent / "report.md"
    target.write_text("ALLOWED")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.md").write_text("OUTSIDE")
    held = directory / "reports/held"
    current = store.load(tmp_path, CUSTOMER)
    assert current is not None
    current["report_revisions"] = [{"directory": "reports/001-fixture", "files": ["report.md"]}]
    save(tmp_path, current)
    lstat = Path.lstat
    captured = {"once": False}

    def poisoned_identity(self, *args, **kwargs):
        if self == target and not captured["once"]:
            captured["once"] = True
            parent.rename(held)
            parent.symlink_to(outside, target_is_directory=True)
            checked = lstat(self, *args, **kwargs)  # lstat follows the symlinked parent.
            parent.unlink()
            held.rename(parent)  # Restore the path before the old symlink checks.
            return checked
        return lstat(self, *args, **kwargs)

    checked_outputs = ui.checked_outputs

    def swap_after_check(root, customer, current):
        allowed = checked_outputs(root, customer, current)
        parent.rename(held)
        parent.symlink_to(outside, target_is_directory=True)
        return allowed

    monkeypatch.setattr(Path, "lstat", poisoned_identity)
    monkeypatch.setattr(ui, "checked_outputs", swap_after_check)
    with running(tmp_path) as (server, client):
        response = client.get(
            server.url("/file", customer=CUSTOMER, path="reports/001-fixture/report.md")
        )
        assert response.status_code == 403
        assert "OUTSIDE" not in response.text


def test_review_d1_customer_report_decodes_stored_tel_values(tmp_path):
    ctx = context(tmp_path, profile=PROFILE)
    rendered = ModuleResult(name="browser")
    rendered.add_evidence(ctx.target, "Rendered page evidence (desktop)", {"page": ENCODED_PAGE})
    ctx.results = [rendered]
    result = presence.run(ctx)
    assert result.status == "ok"
    summary = presence.summary([result], ctx)
    mapping = {e.id: store.namespaced(e.id, "r3") for e in result.evidence}
    summary = store.remap(summary, mapping)
    rows = copy.deepcopy(LEGACY_NAP)
    for row in rows:
        row["evidence_ids"] = [mapping[result.evidence[0].id]]
    summary["discovery"]["nap"] = rows
    current = store.new_store(CUSTOMER, "Synthetic")
    current["evidence"] = [store.remap(e.model_dump(), mapping) for e in result.evidence]
    current["runs"] = [
        {
            "run_id": "r3",
            "kind": "assessment",
            "target": ctx.target,
            "created_at": "2026-10-20T00:00:00Z",
            "status": "complete",
            "selected_categories": ["presence"],
            "assessed_categories": ["presence"],
            "ai": {"status": "skipped"},
            "modules": [{"name": "presence", "status": "ok", "reason": ""}],
            "pages": [ctx.target],
            "max_pages": 10,
            "limitations": [],
            "future": [],
            "digital_presence": summary,
        }
    ]
    current["findings"] = [
        Finding(
            id="tech-nap",
            title="Verify Facebook phone",
            category="presence",
            check_key="platform:external-nap-inconsistent:facebook",
            site="example.com",
            component=ctx.target,
            last_run_id="r3",
            evidence_ids=rows[0]["evidence_ids"],
            detail="Legacy phone comparison: (02)%205550%200101",
            recommendation="Verify listing",
            confidence="needs_verification",
        ).model_dump()
    ]
    original = copy.deepcopy(current)
    data, evidence, discovery = store.report_data(current)
    text = render_markdown(data, evidence, discovery)
    assert "(02)%205550%200101" not in text
    assert "(business: (02) 5550 0101, 0400 111 222)" in text
    assert "| (02) 5550 0101, 0400 111 222 |" in text
    assert "source shows 9 digits" in text
    assert current == original  # Report corrections never rewrite the stored observations.


@pytest.mark.parametrize("component", ["reports", "001-fixture"])
def test_review_d9_parent_symlink_is_refused_even_with_matching_file_identity(tmp_path, component):
    directory = tmp_path / CUSTOMER
    target = directory / "reports/001-fixture/report.md"
    target.parent.mkdir(parents=True)
    target.write_text("ALLOWED")
    parent = target.parent if component == "001-fixture" else target.parent.parent
    outside = tmp_path / "outside"
    external = outside / target.relative_to(parent)
    external.parent.mkdir(parents=True)
    external.write_text("OUTSIDE")
    parent.rename(tmp_path / "held")
    parent.symlink_to(outside, target_is_directory=True)
    assert ui.read_checked(target, external.stat(), directory=directory) is None


def test_review_d9_parent_swap_after_directory_open_cannot_redirect_read(tmp_path, monkeypatch):
    directory = tmp_path / CUSTOMER
    target = directory / "reports/001-fixture/report.md"
    target.parent.mkdir(parents=True)
    target.write_text("ALLOWED")
    checked = target.stat()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.md").write_text("OUTSIDE")
    open_file = os.open

    def racing(name, flags, *args, **kwargs):
        fd = open_file(name, flags, *args, **kwargs)
        if name == "001-fixture":
            target.parent.rename(tmp_path / "held")
            target.parent.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(os, "open", racing)
    assert ui.read_checked(target, checked, directory=directory) == b"ALLOWED"


def test_review_d8_keeps_qualified_customer_figure():
    evidence = Evidence(
        id="ev-discovery-001",
        source="discovery",
        url="customer-statement",
        description="Customer estimate",
        provenance="customer_statement",
        data={"statement": "Approximately 20 enquiries per week"},
    )
    claim = "Customer reports approximately 20 enquiries per week; not independently verified."
    assert not ai.unsupported_claim(claim, [evidence])
    cited = claim + " (ev-discovery-001)"
    analysis, _, warnings = ai.validate_analysis(
        {"executive_summary": cited}, [evidence], set(), []
    )
    assert not warnings and analysis["executive_summary"] == cited
