"""Synthetic report-only grouping retains identities, citations, scope and limitations."""

import copy
import json
from typing import Any

from ams_assess.models import Evidence, Finding
from ams_assess.report import markdown_html, mdtext, render_markdown


def scenario():
    evidence = [
        Evidence(
            id=f"ev-browser-r1-{n:03d}",
            source="browser",
            url="https://example.test/a",
            description="Synthetic page audit",
            data={},
        )
        for n in range(1, 7)
    ]
    findings = [
        Finding(
            id=f"issue-{n}",
            category="accessibility",
            check_key="axe:color-contrast",
            title="Contrast needs review",
            evidence_ids=[evidence[n - 1].id],
            detail=f"Distinct observation {n}",
            recommendation="Review contrast",
            component=f"https://example.test/{n}::desktop",
            site="example.test",
            confidence="needs_verification",
            last_observed="2026-10-01",
            lifecycle_status="current" if n < 4 else "not_rechecked",
        )
        for n in range(1, 6)
    ]
    findings.append(
        Finding(
            id="issue-6",
            title="Resolved label",
            category="accessibility",
            check_key="axe:label",
            evidence_ids=[evidence[5].id],
            detail="Previous label failure",
            recommendation="Label input",
            component="https://example.test/form::mobile",
            site="example.test",
            lifecycle_status="resolved",
            last_observed="2026-10-01",
            resolution={"evidence_ids": [evidence[0].id], "run_id": "r2", "date": "2026-10-02"},
        )
    )
    findings[1].dependencies = ["Special dependency"]
    findings[1].title = "Contrast on the second component"
    findings[2].recommendation = "Check keyboard contrast too"
    data = {
        "customer_name": "Synthetic",
        "target": "https://example.test/",
        "created_at": "2026-10-01",
        "selected_categories": ["accessibility", "domain_email"],
        "assessed_categories": ["accessibility", "domain_email"],
        "modules": [{"name": "browser", "status": "ok", "reason": "Bounded sample"}],
        "ai": {
            "status": "skipped",
            "recommendations": ["issue-2", "issue-1"],
            "roadmap": {"0-30 days": ["issue-2"], "90+ days": ["issue-1", "issue-3"]},
        },
        "findings": [f.model_dump() for f in findings],
        "limitations": ["Synthetic scope limit"],
        "pages": [],
        "max_pages": 1,
    }
    return data, evidence


def test_grouped_accessibility_is_one_action_with_all_members_and_no_json_mutation():
    data, evidence = scenario()
    original = json.dumps(data, sort_keys=True)
    original_evidence = [e.model_dump() for e in evidence]
    text = render_markdown(data, evidence)
    assert text.count("#### issue-") == 1
    assert text.count("#### issue-1: Contrast needs review") == 1
    for f in data["findings"]:
        assert f["id"] in text and f["component"] in text
        assert all(eid in text for eid in f["evidence_ids"])
        if f["lifecycle_status"] == "current":
            assert f["detail"] in text and f["title"] in text
    assert "Special dependency" in text and "Check keyboard contrast too" in text
    lifecycle = text.split("### Technical finding lifecycle")[1].split("| Source /")[0]
    assert not any(f"issue-{n}" in lifecycle for n in (1, 2, 3))
    assert lifecycle.count("| Previously observed — not rechecked |") == 1
    assert "2026-10-02 / r2" in lifecycle and "ev-browser-r1-001" in lifecycle
    roadmap = text.split("## 9.")[1].split("## 10.")[0]
    assert roadmap.count("Contrast needs review") == 1
    assert "### 0-30 days" in roadmap and "### 90+ days" not in roadmap
    manual = text.split("### Manual verification items")[1].split("## Appendix")[0]
    assert manual.count("Contrast needs review") == 1
    appendix = text.split("## Appendix")[1]
    assert appendix.count("Synthetic page audit") == 1
    assert appendix.count("https://example.test/a") == 1
    assert all(appendix.count(e.id) == 1 for e in evidence)
    assert json.dumps(data, sort_keys=True) == original
    assert [e.model_dump() for e in evidence] == original_evidence


def test_compact_coverage_keeps_every_url_status_and_exclusion_count():
    data, evidence = scenario()
    data["website_coverage"] = {
        "summary": "Synthetic coverage statement",
        "limits": {"pages": 2},
        "stop_reasons": ["Synthetic collection limit"],
        "discovered": [
            {"url": f"https://example.test/{n}", "sources": ["sitemap"]} for n in range(3)
        ],
        "crawled": ["https://example.test/0"],
        "browser_tested": ["https://example.test/0"],
        "untested": ["https://example.test/1", "https://example.test/2", "https://other.test/"],
        "sitemap_not_reached": ["https://example.test/1", "https://example.test/2"],
        "robots_declarations": ["https://example.test/sitemap.xml"],
        "sitemaps": [{"url": "https://example.test/sitemap.xml", "detail": "Bounded sitemap"}],
        "excluded": [{"source": "crawl", "reason": "Outside scope"}] * 3,
        "blocked": [],
        "failed": [{"url": "https://example.test/error", "reason": "Timeout"}],
        "redirected": [],
        "wordpress": {"status": "unavailable", "reason": "No export"},
    }
    before = copy.deepcopy(data)
    text = render_markdown(data, evidence)
    coverage = text.split("### Website coverage")[1].split("### Technical finding lifecycle")[0]
    for url in [
        "https://example.test/0",
        "https://example.test/1",
        "https://example.test/2",
        "https://other.test/",
        "https://example.test/error",
    ]:
        assert url in coverage
    assert "3 ×" in coverage and coverage.count("Outside scope") == 1
    assert "Untested in browser: 3" in coverage and "Sitemap URLs not reached: 2" in coverage
    assert "browser tested: Yes" in coverage and "browser tested: No" in coverage
    for phrase in (
        "Synthetic coverage statement",
        "Synthetic collection limit",
        "Bounded sitemap",
        "Timeout",
        "No export",
    ):
        assert phrase in coverage
    assert data == before


def test_latest_alias_health_view_retains_prior_unavailable_checks_and_limits():
    data, evidence = scenario()
    health: dict[str, Any] = {
        "rows": [
            {
                "area": "DNS",
                "status": "PASS",
                "issue": "None",
                "checked": "DNS checked",
                "why": "Reliability",
                "recommendation": "Review",
                "evidence_ids": [evidence[0].id],
                "checks": [
                    {
                        "name": "NS",
                        "status": "PASS",
                        "detail": "Current observation",
                        "evidence_ids": [evidence[0].id],
                    }
                ],
            }
        ],
        "limitations": ["Current summary limit"],
        "target": "https://example.test/",
        "run_id": "r2",
    }
    old = copy.deepcopy(health)
    old.update(
        target="https://www.example.test/", run_id="r1", limitations=["Earlier summary limit"]
    )
    old["rows"][0]["checks"][0].update(status="NOT ASSESSED", detail="Earlier unavailable reason")
    data["domain_email_health"] = health
    data["domain_email_health_sites"] = [health, old]
    data["modules"].append({"name": "domain_email", "status": "ok", "reason": ""})
    before = copy.deepcopy(data)
    text = render_markdown(data, evidence)
    assert text.count("#### Domain, DNS & Email Health summary") == 1
    for detail in (
        "Current summary limit",
        "Earlier summary limit",
        "Earlier unavailable reason",
        "https://www.example.test/ (r1)",
        "Current observation",
    ):
        assert detail in text
    assert data == before


def test_register_display_deduplicates_only_equal_observations_and_actions():
    from ams_assess.report import register_actions, register_observations

    observation = {
        "method": "manual",
        "basis": "manual verification",
        "note": "Scope reviewed",
        "date": "2026-10-01",
        "reference": "ev-register-r1-001",
    }
    rows = [
        observation,
        {**observation, "date": "2026-10-02", "reference": "ev-register-r2-001"},
        {**observation, "note": "Different check"},
    ]
    before = copy.deepcopy(rows)
    grouped = register_observations(rows)
    assert len(grouped) == 2 and len(grouped[0]["citations"]) == 2
    assert grouped[0]["citations"] == [
        {k: row[k] for k in ("date", "reference")} for row in rows[:2]
    ]
    assert rows == before
    entries = [
        {
            "label": platform,
            "site": "https://example.test/",
            "location": "",
            "next_action": "Confirm control",
            "responsible": "Customer",
        }
        for platform in ("Platform A", "Platform B")
    ]
    assert register_actions(entries)[0]["scopes"][("https://example.test/", "")] == [
        "Platform A",
        "Platform B",
    ]


def test_search_link_keeps_destination_and_escapes_untrusted_input():
    from ams_assess.report import mdlink

    url = "https://search.example.test/?q=a%20b"
    assert f'href="{url}"' in markdown_html(mdlink("a b", url))
    unsafe = mdlink("<script>test</script>", "javascript:alert(1)")
    assert "<script>" not in markdown_html(unsafe)
    assert 'href="javascript:' not in markdown_html(unsafe)
    assert mdtext("javascript:alert(1)") in unsafe
