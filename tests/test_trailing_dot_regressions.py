"""Saved pre-fix identities and collector boundaries, without real customer/provider access."""

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from test_https_crawl import mock_crawler
from test_lifecycle import merge
from test_release_defects import LEGACY_NAP, context, gsc_evidence
from test_site_checks import browser as browser_evidence
from test_site_checks import crawl, run_checks, sitemap

from ams_assess import consolidate, outcomes, store
from ams_assess.lifecycle import scoped
from ams_assess.models import CheckOutcome, Evidence, Finding, ModuleResult
from ams_assess.modules import browser, domain_email, siteone
from ams_assess.modules.site_rules import account_link, tool_signals

DOTTED = "https://example.test./"
PLAIN = "https://example.test/"


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Trailing-dot regressions must not make live HTTP calls")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def write_base_shaped_store(tmp_path, *, check="missing-title", category="seo"):
    """Write the raw dotted identities a pre-fix collector stored, bypassing load repairs."""
    current = merge(tmp_path, target=DOTTED, check=check, category=category)
    finding = current["findings"][0]
    finding.update(id="tech-legacy-dotted", site="example.test.", component=DOTTED + "a")
    finding["coverage"] = [DOTTED + "a"]
    for outcome in current["runs"][0]["outcomes"]:
        outcome.update(site="example.test.", component=DOTTED + "a", coverage=[DOTTED + "a"])
    path = tmp_path / "synthetic/assessment.json"
    path.write_text(json.dumps(current))
    return current, path


@pytest.mark.parametrize("analysed", [False, True])
def test_gsc_backfill_retires_dotted_ai_finding_and_preserves_existing_successor(
    tmp_path, analysed
):
    current, path = write_base_shaped_store(tmp_path)
    current["connected_analysis"] = 1 if analysed else 0
    current["evidence"] = [e.model_dump() for e in gsc_evidence()]
    for e in current["evidence"]:
        e["id"] = e["id"].replace("-0", "-r1-0", 1)
    legacy = current["findings"][0]
    legacy.update(check_key="site-index-sample-page-exposed", evidence_ids=["ev-gsc-r1-001"])
    legacy["events"] = [{"status": "observed", "note": "Retain the original AI observation"}]
    if analysed:
        successor = copy.deepcopy(legacy)
        successor.update(
            id="old-dotted-gsc-id", check_key="gsc:placeholder-impressions", component=DOTTED
        )
        current["findings"].append(successor)
    path.write_text(json.dumps(current))
    original = path.read_bytes()
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None
    assert not any(f["check_key"] == legacy["check_key"] for f in loaded["findings"])
    successors = [f for f in loaded["findings"] if f["check_key"] == "gsc:placeholder-impressions"]
    assert len(successors) == 1
    if analysed:
        assert successors[0] == successor
    retired = loaded["finding_history"]
    assert len(retired) == 1 and retired[0]["superseded_by"] == successors[0]["id"]
    assert all(retired[0][key] == value for key, value in legacy.items())
    assert loaded["runs"] == current["runs"] and loaded["evidence"] == current["evidence"]
    assert path.read_bytes() == original
    store.save(tmp_path, "synthetic", loaded)
    assert store.load(tmp_path, "synthetic") == loaded


@pytest.mark.parametrize(
    "target,expected", [(PLAIN, "skipped"), ("https://other.test/", "out-of-scope")]
)
def test_saved_dotted_finding_is_in_scope_on_unseen_same_site_rerun(tmp_path, target, expected):
    before, _ = write_base_shaped_store(tmp_path)
    saved = merge(tmp_path, run=2, target=target, failures=(), outcomes=[])
    rows = saved["runs"][-1]["outcomes"]
    assert len(rows) == 1 and rows[0]["status"] == expected
    assert saved["findings"][0]["id"] == before["findings"][0]["id"]
    assert saved["findings"][0]["lifecycle_status"] == "not_rechecked"


def test_dotted_explicit_outcome_does_not_gain_a_synthetic_skipped_outcome(tmp_path):
    before, _ = write_base_shaped_store(tmp_path)
    old = before["runs"][0]["outcomes"][0]
    passed = CheckOutcome.model_validate(
        old | {"status": "pass", "evidence_ids": ["ev-siteone-001"]}
    )
    saved = merge(tmp_path, run=2, failures=(), outcomes=[passed])
    assert len(saved["runs"][-1]["outcomes"]) == 1
    assert saved["findings"][0]["lifecycle_status"] == "resolved"


@pytest.mark.parametrize("mixed_aliases", [False, True])
def test_dotted_axe_history_repairs_same_audit_without_rewriting_raw_observations(
    tmp_path, mixed_aliases
):
    current, path = write_base_shaped_store(
        tmp_path, check="axe:color-contrast", category="accessibility"
    )
    finding = current["findings"][0]
    finding["lifecycle_status"] = "needs_review"
    row = current["runs"][0]["outcomes"][0]
    row["module"] = "browser"
    passed = row | {"status": "pass"}
    if mixed_aliases:
        passed.update(site="example.test", component=PLAIN + "a", coverage=[PLAIN + "a"])
    current["runs"][0]["outcomes"].append(passed)
    current["needs_review"] = [
        {"finding_id": finding["id"], "reason": "conflicting explicit check outcomes need review"}
    ]
    path.write_text(json.dumps(current))
    raw = path.read_bytes()
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None and loaded["findings"][0]["lifecycle_status"] == "current"
    assert loaded["findings"][0]["id"] == finding["id"] and not loaded["needs_review"]
    assert loaded["findings"][0]["events"][:-1] == finding["events"]
    assert loaded["runs"] == current["runs"] and loaded["evidence"] == current["evidence"]
    assert path.read_bytes() == raw


def test_unscoped_predecessor_is_retired_by_saved_dotted_successor(tmp_path):
    current, path = write_base_shaped_store(
        tmp_path, check="platform:priority-verification", category="presence"
    )
    successor = current["findings"][0]
    successor["last_run_id"] = "r2"
    current["runs"].append(current["runs"][0] | {"run_id": "r2"})
    predecessor = successor | {
        "id": "old-unscoped",
        "site": "",
        "component": "",
        "last_run_id": "r1",
    }
    current["findings"].append(predecessor)
    path.write_text(json.dumps(current))
    raw = path.read_bytes()
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None and len(loaded["findings"]) == 1
    assert loaded["findings"][0]["id"] == successor["id"]
    assert loaded["finding_history"][0]["id"] == "old-unscoped"
    assert loaded["finding_history"][0]["superseded_by"] == successor["id"]
    assert path.read_bytes() == raw


def test_nap_repair_matches_dotted_stored_finding_without_rewriting_history(tmp_path):
    current, path = write_base_shaped_store(
        tmp_path, check="platform:external-nap-inconsistent:facebook", category="presence"
    )
    rows = copy.deepcopy(LEGACY_NAP[:1])
    rows[0].update(observed="02 5550 0101", reference=["(02)%205550%200101"])
    current["runs"][0]["digital_presence"] = {"discovery": {"nap": rows}}
    path.write_text(json.dumps(current))
    raw = path.read_bytes()
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None
    data, _, _ = store.report_data(loaded)
    assert data["findings"][0]["lifecycle_status"] == "resolved"
    assert data["findings"][0]["resolution"]["run_id"] == "r1"
    assert loaded["findings"][0] == current["findings"][0]
    assert path.read_bytes() == raw


def test_scoped_explicit_dotted_site_keeps_same_generated_identity():
    finding = Finding(
        id="old",
        title="Missing title",
        detail="Missing title",
        recommendation="Add title",
        category="seo",
        check_key="title",
        evidence_ids=["ev-siteone-001"],
        site="example.test.",
        component=DOTTED + "a",
    )
    plain = finding.model_copy(update={"site": "example.test", "component": PLAIN + "a"})
    dotted_result = scoped([finding], DOTTED, "synthetic")[0]
    plain_result = scoped([plain], PLAIN, "synthetic")[0]
    assert dotted_result.id == plain_result.id and dotted_result.site == "example.test"
    assert finding.site == "example.test."


@pytest.mark.parametrize("canonical,expected", [(PLAIN, "pass"), ("https://other.test/", "fail")])
def test_browser_canonical_check_handles_dns_root_dot(tmp_path, monkeypatch, canonical, expected):
    ctx = context(tmp_path, target=DOTTED, pages=[DOTTED], categories={"seo"})
    ctx.tools_dir.mkdir()
    (ctx.tools_dir / "axe.min.js").touch()

    def noop(*args, **kwargs):
        pass

    page = SimpleNamespace(
        url=DOTTED,
        on=noop,
        goto=lambda *a, **kw: SimpleNamespace(status=200),
        screenshot=noop,
        add_script_tag=noop,
        close=noop,
    )
    page.evaluate = lambda script: (
        {"canonical": canonical}
        if script == browser.EXTRACT
        else {"testEngine": {"version": "synthetic"}, "violations": [], "incomplete": []}
    )
    session = SimpleNamespace(route=noop, route_web_socket=noop, new_page=lambda: page, close=noop)
    engine = SimpleNamespace(new_context=lambda **kw: session, close=noop)
    monkeypatch.setattr(
        browser,
        "sync_playwright",
        lambda: nullcontext(
            SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: engine))
        ),
    )
    result = browser.run(ctx)
    assert result.status == "ok" and not result.reason
    checks = [o for o in result.outcomes if o.check_id == "canonical-mismatch"]
    assert len(checks) == 2 and {o.status for o in checks} == {expected}
    assert bool(result.findings) == (expected == "fail")


def test_domain_infrastructure_reuses_dotted_host_evidence_only(tmp_path):
    ctx = context(tmp_path, target=PLAIN)
    security = ModuleResult(name="security")
    own = security.add_evidence(
        DOTTED, "HTTP response", {"status": 200, "headers": {"server": "cloudflare"}}
    )
    other = security.add_evidence(
        "https://other.test./",
        "Other response",
        {"status": 200, "headers": {"x-vercel-id": "unrelated"}},
    )
    ctx.results = [security]
    raw = {"zone": {"NS": ("ok", [])}, "CNAME": {"chain": []}, "A": {}, "AAAA": {}, "MX": {}}
    result = domain_email.infrastructure(
        ctx, raw, {"A": "a", "AAAA": "aaaa", "CNAME": "cname", "MX": "mx"}, "zone", {}, "rdap"
    )
    assert [r["url"] for r in result["website_endpoints"]["value"]["http"]] == [DOTTED]
    assert (
        own in result["website_provider"]["evidence_ids"]
        and other not in result["website_provider"]["evidence_ids"]
    )
    assert [r["provider"] for r in result["website_provider"]["value"]] == ["Cloudflare"]


def test_site_checks_canonical_does_not_report_dotted_host_as_foreign(tmp_path, monkeypatch):
    ctx = context(
        tmp_path,
        target=DOTTED,
        categories={"seo"},
        results=[
            browser_evidence({DOTTED: {"canonical": PLAIN}}),
            crawl({DOTTED: 200, PLAIN: 200}),
            sitemap(),
        ],
    )
    _, checks = run_checks(ctx, monkeypatch)
    assert checks["canonicals"]["status"] == "PASS"


def test_dotted_booking_link_is_detected_as_an_account():
    url = "https://calendly.com./synthetic/consultation"
    assert account_link("Calendly", url)
    hits = tool_signals({"links": [url]}, PLAIN)
    assert hits == [{"tool": "Calendly", "context": "links", "url": url}]
    assert not account_link("Calendly", "https://calendly.com.evil.test/consultation")


def test_siteone_keeps_successful_dotted_host_pages(tmp_path, monkeypatch):
    ctx = context(tmp_path, target=DOTTED, categories={"seo"})
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    mock_crawler(monkeypatch, ctx, [{"url": DOTTED, "status": "200", "type": 1}])
    assert siteone.run(ctx).status == "ok"
    assert ctx.pages == [DOTTED]


def test_explicit_dotted_unavailable_outcome_does_not_gain_a_failure(tmp_path):
    ctx = context(tmp_path, target=PLAIN, categories={"seo"})
    result = ModuleResult(name="synthetic")
    eid = result.add_evidence(DOTTED + "a", "Attempted check", {})
    result.finding(
        "explicit",
        "Check needs review",
        "seo",
        [eid],
        "Unavailable",
        "Retry",
        site="example.test.",
        component=DOTTED + "a",
    )
    unavailable = CheckOutcome(
        module="synthetic",
        check_id="explicit",
        category="seo",
        site="example.test.",
        component=DOTTED + "a",
        status="unavailable",
        evidence_ids=[eid],
    )
    result.outcomes = [unavailable]
    outcomes.record(ctx, result)
    assert result.outcomes == [unavailable]
    assert result.findings[0].site == "example.test"


def test_consolidation_matches_dotted_scope_and_retains_original_citations():
    evidence = [
        Evidence(
            id=f"ev-security-{n:03d}",
            source="security",
            url=url,
            description="Header check",
            data={},
        )
        for n, url in enumerate([DOTTED, PLAIN], 1)
    ]
    one = Finding(
        id="one",
        title="Header missing",
        category="security",
        check_key="header-missing:x",
        site="example.test.",
        component=DOTTED,
        evidence_ids=[evidence[0].id],
        detail="First observation",
        recommendation="Check policy",
        urls=[DOTTED],
    )
    two = one.model_copy(
        update={
            "id": "two",
            "site": "example.test",
            "component": PLAIN,
            "evidence_ids": [evidence[1].id],
            "urls": [PLAIN],
        }
    )
    combined = consolidate.consolidate([one, two], evidence)
    assert len(combined) == 1 and combined[0].evidence_ids == [e.id for e in evidence]
    assert set(combined[0].urls) == {DOTTED, PLAIN}
    two.check_key = "header-missing:y"
    grade = Evidence(
        id="ev-observatory-001",
        source="observatory",
        url=PLAIN,
        description="Grade",
        data={"grade": "F"},
    )
    combined = consolidate.consolidate_security_headers([one, two], [*evidence, grade])
    assert len(combined) == 1 and grade.id in combined[0].evidence_ids
    assert one.site == "example.test." and one.urls == [DOTTED]
