import functools
import http.server
import json
import re
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from ams_assess import ai, cli, runner
from ams_assess.ai import validate_analysis
from ams_assess.consolidate import consolidate
from ams_assess.models import (
    CATEGORIES,
    Context,
    Evidence,
    Finding,
    ModuleResult,
    create_run_dir,
    normalize_target,
)
from ams_assess.modules import dataforseo, gsc, performance
from ams_assess.report import render


def sample_finding(**changes):
    return Finding.model_validate(
        {
            "id": "source-001",
            "title": "Missing description",
            "category": "seo",
            "evidence_ids": ["ev-source-001"],
            "detail": "Observed on page A",
            "recommendation": "Add a useful description",
            "check_key": "description",
            "urls": ["https://example.test/a"],
            **changes,
        }
    )


def sample_evidence(eid="ev-source-001"):
    return Evidence(
        id=eid, source="source", url="https://example.test/a", description="Metadata", data={}
    )


def context(tmp_path, **changes):
    return replace(
        Context(
            customer_id="test",
            customer_name="Test",
            target="https://example.com/",
            categories={"performance"},
            connections=set(),
            config={},
            run_dir=create_run_dir(tmp_path, "test"),
            tools_dir=cli.ROOT / "tools",
        ),
        **changes,
    )


def test_consolidation_merges_pages_and_sources_without_losing_citations():
    first = sample_finding()
    second = sample_finding(
        id="other-001",
        evidence_ids=["ev-other-001"],
        urls=["https://example.test/b"],
        detail="Observed on page B",
        severity="high",
        priority="P1",
    )
    merged = consolidate([first, second], [sample_evidence(), sample_evidence("ev-other-001")])
    assert len(merged) == 1
    assert set(merged[0].evidence_ids) == {"ev-source-001", "ev-other-001"}
    assert len(merged[0].urls) == 2 and merged[0].severity == "high"
    assert merged[0].priority == "P1"
    assert first.evidence_ids == ["ev-source-001"]
    with pytest.raises(ValueError, match="missing evidence"):
        consolidate([second], [sample_evidence()])


def test_ai_drops_unreferenced_and_unknown_citations_preserves_observations(caplog):
    valid = sample_finding(id="ai-001", category="geo").model_dump()
    malicious = sample_finding(title="Everything passed", confidence="subjective").model_dump()
    data = {
        "executive_summary": "Unsupported summary",
        "sections": {"geo": "See ev-source-001"},
        "findings": [
            {**valid, "evidence_ids": []},
            {**valid, "evidence_ids": ["ev-source-001", "ev-invented-001"]},
            {**valid, "category": "cro_ux"},
            valid,
            malicious,
        ],
        "recommendations": ["ai-001", "invented"],
        "roadmap": {"0-30 days": ["ai-001", "invented"]},
    }
    analysis, findings, warnings = validate_analysis(
        data, [sample_evidence()], {"seo", "geo"}, [sample_finding()]
    )
    assert len(findings) == 2
    assert findings[0].title == "Missing description" and findings[0].confidence == "confirmed"
    assert findings[1].confidence == "needs_verification"
    assert analysis["executive_summary"] == ""
    assert analysis["recommendations"] == ["ai-001"]
    assert analysis["roadmap"]["0-30 days"] == ["ai-001"]
    assert len(warnings) == 4 and "Dropped AI finding" in caplog.text


@pytest.mark.parametrize("slug", ["../other", "a/b", "", "UPPER", "/tmp", "a" * 64])
def test_customer_path_rejects_invalid_slugs(tmp_path, slug):
    with pytest.raises(ValueError):
        create_run_dir(tmp_path, slug)


def test_customer_directories_are_separate_and_reject_symlinks(tmp_path):
    a = create_run_dir(tmp_path, "acme")
    b = create_run_dir(tmp_path, "beta")
    a2 = create_run_dir(tmp_path, "acme")
    (a / "private.txt").write_text("synthetic private data")
    assert a != a2 and a.parent != b.parent
    assert not list(b.rglob("private.txt"))
    (tmp_path / "linked").symlink_to(a.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        create_run_dir(tmp_path, "linked")
    assert normalize_target("EXAMPLE.com") == "https://example.com/"
    with pytest.raises(ValueError):
        normalize_target("https://user:password@example.com")


def test_disabled_paid_integration_never_makes_requests(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Paid endpoint must not be called")

    monkeypatch.setattr(httpx, "post", forbidden)
    ctx = context(tmp_path)
    assert dataforseo.run(ctx).status == "skipped"
    ctx.config = {"DATAFORSEO_LOGIN": "synthetic", "DATAFORSEO_PASSWORD": "synthetic"}
    assert "cap" in dataforseo.run(ctx).reason
    ctx.dataforseo_max_usd = 0.001
    assert "exceeds" in dataforseo.run(ctx).reason


@pytest.mark.parametrize(
    ("configured", "override", "expected"),
    [(None, None, 2036), ("2840", None, 2840), ("2840", "2036", 2036)],
)
def test_dataforseo_market_default_config_and_cli(
    tmp_path, monkeypatch, configured, override, expected
):
    settings = {"DATAFORSEO_LOGIN": "synthetic", "DATAFORSEO_PASSWORD": "synthetic"}
    if configured is not None:
        settings["DATAFORSEO_LOCATION_CODE"] = configured
    monkeypatch.setattr(cli, "config", lambda: settings.copy())
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    def fake_post(url, **kwargs):
        assert kwargs["json"] == [
            {"target": "example.com", "location_code": expected, "language_code": "en", "limit": 1}
        ]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "status_code": 20000,
                "cost": 0.0101,
                "tasks": [{"status_code": 20000, "result": []}],
            },
        )

    def fake_run(ctx, **kwargs):
        result = dataforseo.run(ctx)
        assert result.status == "ok"
        assert result.evidence[0].data == {
            "location_code": expected,
            "language_code": "en",
            "result": [],
        }
        assert f"location_code={expected}, language_code=en" in result.evidence[0].description
        assert f"location_code={expected}, language_code=en" in result.reason
        budget = json.loads((ctx.run_dir / "raw/dataforseo-budget.json").read_text())
        assert budget["location_code"] == expected and budget["language_code"] == "en"
        return {"status": "complete", "findings": []}

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(cli, "run", fake_run)
    args = ["run", "--customer", "test", "--target", "example.com", "--dataforseo-max-usd", "0.02"]
    if override is not None:
        args += ["--dataforseo-location-code", override]
    assert cli.main(args) == 0


def test_claude_json_transport_and_auth_failure_without_fallback(tmp_path, monkeypatch):
    calls = []
    output = {
        "structured_output": {
            "executive_summary": "Review page metadata (ev-source-001).",
            "findings": [sample_finding(id="ai-001").model_dump()],
            "recommendations": ["ai-001"],
            "roadmap": {"0-30 days": ["ai-001"]},
        }
    }

    def fake_claude(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr="")

    monkeypatch.setattr(ai.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(ai.subproc, "run", fake_claude)
    ctx = context(tmp_path, categories={"seo"}, config={"AMS_AI_PROVIDER": "claude-cli"})
    state, findings = ai.analyse(ctx, [], [sample_evidence()], no_ai=False)
    assert state["status"] == "ok" and state["provider"] == "claude-cli"
    assert len(findings) == 1 and state["recommendations"] == ["ai-001"]
    command, kwargs = calls[0]
    assert command[1] == "-p" and command[command.index("--tools") + 1] == ""
    assert "ev-source-001" in kwargs["input"] and kwargs["timeout"] == 300
    assert (ctx.run_dir / "raw/ai-prompt.txt.sha256").is_file()
    assert (ctx.run_dir / "raw/ai-claude.json.sha256").is_file()
    output.clear()
    output.update(is_error=True, result="Not logged in · Please run /login")
    state, findings = ai.analyse(ctx, [sample_finding()], [sample_evidence()], no_ai=False)
    assert state["status"] == "error" and "not authenticated" in state["reason"]
    assert findings[0].id == "source-001"
    ai.analyse(ctx, [], [], no_ai=True)
    assert len(calls) == 2


def test_gsc_property_cannot_cross_customers():
    assert gsc.matches_target("sc-domain:example.com", "https://www.example.com/")
    assert not gsc.matches_target("sc-domain:example.com", "https://badexample.com/")
    assert not gsc.matches_target("https://example.com/client-a/", "https://example.com/client-b/")


def test_pagespeed_quota_failure_is_missing_data_not_good_performance(tmp_path, monkeypatch):
    calls = []

    def quota(url, **kwargs):
        calls.append(kwargs["params"]["strategy"])
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", quota)
    ctx = context(tmp_path)
    result = performance.run(ctx)
    assert calls == ["mobile", "desktop"]
    assert result.status == "skipped" and "429" in result.reason
    assert not result.findings
    outcomes = result.evidence[-1].data["performance"]
    assert all(lab["outcome"] == "quota" for lab in outcomes["lab"])
    assert (ctx.run_dir / "raw/performance.json").is_file()


def test_module_failure_isolated_and_only_selected_modules_run(tmp_path, monkeypatch):
    def fail(ctx):
        raise RuntimeError("secret-looking value should not appear in reports")

    def forbidden(ctx):
        pytest.fail("Unselected crawler should not execute")

    def succeed(ctx):
        return ModuleResult(
            name="security",
            evidence=[
                Evidence(
                    id="ev-security-001",
                    source="security",
                    url=ctx.target,
                    description="Synthetic headers",
                    data={},
                )
            ],
        )

    def broken(ctx):
        return ModuleResult(name="broken", findings=[sample_finding()])

    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "performance": SimpleNamespace(categories={"performance"}, run=fail),
            "broken": SimpleNamespace(categories={"security"}, run=broken),
            "security": SimpleNamespace(categories={"security"}, run=succeed),
            "siteone": SimpleNamespace(categories={"seo"}, run=forbidden),
        },
    )
    ctx = context(tmp_path, categories={"performance", "security"})
    result = runner.run(ctx, no_ai=True, formats={"md", "html"})
    assert result["status"] == "partial"
    assert result["modules"][0]["status"] == "error"
    assert result["modules"][1]["status"] == "error"
    assert result["modules"][2]["status"] == "ok"
    assert "secret-looking" not in (ctx.run_dir / "report.md").read_text()
    assert "Form structure was inspected" not in (ctx.run_dir / "report.md").read_text()
    assert "Crawl coverage:" not in (ctx.run_dir / "report.md").read_text()
    assert not any("AI interpretation" in item for item in result["limitations"])
    assert (ctx.run_dir / "report.html").is_file()


def test_fixture_end_to_end_no_ai(monkeypatch):
    binary = cli.ROOT / "tools/siteone/siteone-crawler/siteone-crawler"
    with sync_playwright() as p:
        chromium = Path(p.chromium.executable_path)
    if not (binary.is_file() and chromium.is_file() and (cli.ROOT / "tools/axe.min.js").is_file()):
        pytest.skip("Run scripts/setup.sh for SiteOne, Chromium and axe-core")
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            requests.append(("GET", self.path))
            if self.path in {"/old-service", "/older-service"}:
                self.send_response(301)
                self.send_header(
                    "Location",
                    "/older-service" if self.path == "/old-service" else "/services.html",
                )
                self.end_headers()
                return
            super().do_GET()

        def do_POST(self):
            requests.append(("POST", self.path))
            self.send_error(405)

    handler = functools.partial(Handler, directory=str(cli.ROOT / "fixtures/site"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    customer = "test-fixture-" + uuid.uuid4().hex[:12]
    monkeypatch.setattr(cli, "config", lambda: {})
    try:
        code = cli.main(
            [
                "run",
                "--customer",
                customer,
                "--target",
                f"http://127.0.0.1:{server.server_port}/",
                "--no-ai",
                "--max-pages",
                "30",
            ]
        )
        assert code == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    # The customer directory also holds the store file (assessment.json); pick the run.
    directory = next(p for p in (cli.data_root() / customer).iterdir() if p.is_dir())
    for file in [
        "results.json",
        "evidence.json",
        "findings.json",
        "report.md",
        "report.html",
        "report.pdf",
    ]:
        assert (directory / file).stat().st_size > 0
    results = json.loads((directory / "results.json").read_text())
    evidence = json.loads((directory / "evidence.json").read_text())
    known = {e["id"] for e in evidence}
    assert len(known) == len(evidence)
    # /unlinked.html is only in the sitemap: seeded into the crawl and the browser sample.
    assert len(results["pages"]) == 8
    unlinked = f"http://127.0.0.1:{server.server_port}/unlinked.html"
    assert unlinked in results["pages"]
    coverage = results["website_coverage"]
    assert unlinked in coverage["seeded"] and unlinked in coverage["browser_tested"]
    assert unlinked not in coverage["sitemap_not_reached"]
    assert len(list((directory / "screenshots").glob("*.png"))) == 12
    keys = {f["check_key"] for f in results["findings"]}
    assert {
        "missing-description",
        "missing-h1",
        "broken-links",
        "axe:color-contrast",
        "axe:image-alt",
    } <= keys
    assert set(results["selected_categories"]) == set(CATEGORIES)
    report = (directory / "report.md").read_text()
    report_html = (directory / "report.html").read_text()
    assert "Crawl coverage: 8 successful HTML pages" in report
    assert "Browser coverage: 12 page/viewport observations" in report
    assert all(f["priority"] == "P1" for f in results["findings"] if f["severity"] == "high")
    assert all(f["priority"] == "P3" for f in results["findings"] if f["severity"] == "low")
    appendix = report_html.split("Appendix: evidence index", 1)[1]
    assert all(e["id"] in appendix for e in evidence)
    # Escaped cell text must never split a Markdown table row into extra cells.
    tables = re.findall(r"<table(?: [^>]*)?>.*?</table>", report_html, re.S)
    assert tables
    for table in tables:
        width = len(re.findall(r"<th[ >]", table))
        rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)[1:]
        assert rows and all(len(re.findall(r"<td[ >]", row)) == width for row in rows)
    presence = results["digital_presence"]
    platforms = {r["key"]: r for r in presence["platforms"]}
    assert platforms["gsc"]["status"] == "Detected"
    # Fixture tags are consent-gated (type="text/plain") and never load: IDs kept, status Unknown.
    for key, tag_id in [("ga4", "G-FIXTURE001"), ("gtm", "GTM-FIXTURE1"), ("clarity", "fixture01")]:
        assert platforms[key]["status"] == "Unknown" and platforms[key]["ids"] == [tag_id]
    assert platforms["google_ads"]["status"] == "Unknown"
    assert platforms["wordpress"]["status"] == "Not detected"
    assert platforms["m365"]["status"] == "NOT ASSESSED"
    listings = {r["key"]: r["status"] for r in presence["listings"]}
    assert listings["google_business_profile"] == "Detected (linked from website)"
    assert listings["yellow_pages"] == listings["instagram"] == "Detected (linked from website)"
    assert not any("WordPress" in item["service"] for item in presence["checklist"])
    # No DataForSEO cap: independent search is Not assessed, with ready-to-use manual tasks,
    # while profiles the fixture links to are still found from website evidence.
    discovery = presence["discovery"]
    assert discovery["status"] == "Not assessed" and "cap" in discovery["reason"]
    assert discovery["budget"]["serp_calls"] == 0 and discovery["identity"]["name"]
    states = {r["key"]: r["state"] for r in discovery["platforms"]}
    for key in ["google_business_profile", "facebook", "yellow_pages", "instagram"]:
        assert states[key] == "Found — likely match"
    assert states["true_local"] == states["bing_places"] == "Not assessed"
    assert not {"Not found in searches", "Inconsistent"} & set(states.values())
    tasks = {t["key"]: t for t in discovery["manual_tasks"]}
    assert tasks["true_local"]["query"] == 'site:truelocal.com.au "Harbour Demo Plumbing"'
    assert tasks["true_local"]["url"].startswith("https://www.google.com.au/search?q=site%3A")
    assert "facebook" not in tasks
    assert "### Platform presence discovery" in report and "Manual verification tasks" in report
    assert "platform:priority-verification" in keys
    sitemap = next(r for r in presence["signals"] if r["signal"] == "XML sitemap")
    assert sitemap["status"] == "PASS" and "lists 8 URLs" in sitemap["detail"]
    assert {
        "nap-inconsistent",
        "canonical-missing",
        "listing:sameas-incomplete",
        "tracking:unconfirmed",
    } <= keys
    site = next(m for m in results["modules"] if m["name"] == "site_checks")
    assert site["status"] == "ok" and len(site["checks"]) == 15
    rows = {r["site_check"]: r for r in site["checks"]}
    assert all(r["area"].replace("_", "\\_") in report for r in rows.values())
    assert rows["robots"]["status"] == "PASS"
    assert rows["ai-robots"]["status"] == "WARNING"
    assert rows["canonicals"]["status"] == "PASS"
    assert rows["location-duplicates"]["status"] == "WARNING"
    assert rows["cwv"]["status"] == "NOT ASSESSED"
    assert rows["conversion-events"]["detail"]["state"] == "Unknown"
    assert {e["event"] for e in rows["conversion-events"]["detail"]["events"]} == {
        "generate_lead",
        "Lead",
    }
    assert any(t["tool"] == "Calendly" for t in rows["booking-tools"]["detail"])
    assert rows["llms-txt"]["status"] == "INFO"
    assert rows["page-quality"]["detail"]["duplicates"]["title"]
    assert len(rows["page-quality"]["detail"]["redirect_chains"][0]["chain"]) == 3
    assert any(p["faq_qa_schema"] for p in rows["answer-readiness"]["detail"])
    assert "Form submissions: NOT TESTED" in report
    assert all(set(r["evidence_ids"]) <= known for r in rows.values())
    assert all(set(e["data"].get("source_evidence_ids", [])) <= known for e in site["evidence"])
    pdf_text = "".join(page.extract_text() for page in PdfReader(directory / "report.pdf").pages)
    numbered = [
        "1. Executive summary and business context",
        "2. Scope, sources, dates and coverage limitations",
        "3. Critical findings and business risks",
        "4. Website, SEO, performance, accessibility and AI-search readiness",
        "5. Digital presence: maps, social, directories and industry channels",
        "6. Domain, DNS, email and relevant security observations",
        "9. Prioritised improvement opportunities and recommended roadmap",
        "10. Items requiring customer verification, access or decisions",
    ]
    top_level = re.findall(r"^## (.+)$", report, re.M)
    assert top_level == ["Business assessment report", *numbered, "Appendix: evidence index"]
    for heading in numbered:
        assert heading.replace("&", "&amp;") in report_html
        assert heading.split(". ", 1)[1] in " ".join(pdf_text.split())
    blocks = re.findall(r"(?ms)^#### .*?(?=^#{1,4} |\Z)", report)
    for finding in results["findings"]:
        assert finding["evidence_ids"] and set(finding["evidence_ids"]) <= known
        escaped_id = finding["id"].replace("_", "\\_")
        matching = [
            block
            for block in blocks
            if f"#### {escaped_id}:" in block or f"({escaped_id}):" in block
        ]
        assert len(matching) == 1
        block = matching[0]
        refs = set(re.findall(r"ev-[a-z_]+-\d+", block))
        assert set(finding["evidence_ids"]) <= refs <= known
    assert set(re.findall(r"ev-[a-z_]+-\d+", report)) <= known
    assert all(method == "GET" for method, _ in requests)
    assert "AI search visibility was" in report and "NOT measured" in report
    assert "AI Managed Services" in PdfReader(directory / "report.pdf").pages[0].extract_text()
    original = (directory / "results.json").read_text()
    results["findings"][0]["evidence_ids"] = ["ev-missing-999"]
    (directory / "results.json").write_text(json.dumps(results))
    with pytest.raises(ValueError, match="missing evidence"):
        render(directory, {"md"})
    (directory / "results.json").write_text(original)
