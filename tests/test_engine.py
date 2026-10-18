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
    )
    merged = consolidate([first, second], [sample_evidence(), sample_evidence("ev-other-001")])
    assert len(merged) == 1
    assert set(merged[0].evidence_ids) == {"ev-source-001", "ev-other-001"}
    assert len(merged[0].urls) == 2 and merged[0].severity == "high"
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


def test_claude_json_transport_and_auth_failure_fallback(tmp_path, monkeypatch):
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
    monkeypatch.setattr(ai.subprocess, "run", fake_claude)
    ctx = context(tmp_path, categories={"seo"})
    state, findings = ai.analyse(ctx, [], [sample_evidence()], no_ai=False)
    assert state["status"] == "ok" and state["provider"] == "claude"
    assert len(findings) == 1 and state["recommendations"] == ["ai-001"]
    command, kwargs = calls[0]
    assert command[1] == "-p" and command[command.index("--tools") + 1] == ""
    assert "ev-source-001" in kwargs["input"] and kwargs["timeout"] == 300
    assert (ctx.run_dir / "raw/ai-prompt.txt").is_file()
    assert (ctx.run_dir / "raw/ai-claude.json").is_file()
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
    assert not result.findings and not result.evidence
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
    directory = next((cli.ROOT / "data/customers" / customer).iterdir())
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
    assert len(results["pages"]) == 5
    assert len(list((directory / "screenshots").glob("*.png"))) == 10
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
    assert report_html.count("<td>") == 4 * len(evidence) + 3 * len(results["modules"])
    for finding in results["findings"]:
        assert finding["evidence_ids"] and set(finding["evidence_ids"]) <= known
        block = report.split(f"### {finding['id']}:", 1)[1].split("###", 1)[0]
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
