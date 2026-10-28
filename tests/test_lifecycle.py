"""Synthetic cumulative assessments exercise real persistence and report rendering."""

import json
import stat

import pytest

from ams_assess import store
from ams_assess.lifecycle import observe
from ams_assess.models import Evidence, Finding
from ams_assess.report import render_markdown


def merge(
    root,
    *,
    run=1,
    target="https://example.test/",
    failures=("a",),
    outcomes=None,
    check="missing-title",
    category="seo",
):
    evidence = [
        Evidence(
            id="ev-siteone-001",
            source="siteone",
            url=target,
            description="Synthetic metadata observation",
            data={"run": run},
        )
    ]
    findings = [
        Finding(
            id=f"issue-{i}",
            title="Missing title " + page,
            category=category,
            check_key=check,
            evidence_ids=[evidence[0].id],
            detail="Missing title",
            recommendation="Add title",
            urls=[target + page],
        )
        for i, page in enumerate(failures)
    ]
    rows = (
        outcomes
        if outcomes is not None
        else [
            observe("siteone", check, category, target + page, "fail", [evidence[0].id])
            for page in failures
        ]
    )
    data = {
        "target": target,
        "created_at": f"2026-10-{run:02d}T00:00:00Z",
        "status": "complete",
        "selected_categories": [category],
        "assessed_categories": [category],
        "findings": [f.model_dump() for f in findings],
        "outcomes": [o.model_dump() for o in rows],
        "ai": {"status": "skipped", "recommendations": [f.id for f in findings]},
        "modules": [{"name": "siteone", "status": "ok", "reason": ""}],
        "pages": [target + page for page in failures],
        "max_pages": 10,
        "limitations": [],
        "future": [],
        "report_files": [],
    }
    return store.merge_run(root, "synthetic", "Synthetic", data, evidence, f"run-{run}", ["public"])


def outcome(status="pass", page="a", **changes):
    row = observe(
        "siteone",
        "missing-title",
        "seo",
        "https://example.test/" + page,
        status,
        ["ev-siteone-001"],
    )
    return row.model_copy(update=changes)


def test_failure_pass_resolves_with_evidence_date_and_run(tmp_path):
    before = merge(tmp_path)
    after = merge(tmp_path, run=2, failures=(), outcomes=[outcome()])
    f = after["findings"][0]
    assert f["id"] == before["findings"][0]["id"]
    assert f["lifecycle_status"] == "resolved"
    assert f["resolution"] == {
        "date": "2026-10-02T00:00:00Z",
        "run_id": "r2",
        "evidence_ids": ["ev-siteone-r2-001"],
    }
    data, evidence, _ = store.report_data(after)
    text = render_markdown(data, evidence)
    assert "Resolved" in text and "ev-siteone-r2-001" in text and "2026-10-02" in text
    assert "Add title" not in text
    assert not data["ai"]["recommendations"]


@pytest.mark.parametrize("status", ["unavailable", "skipped", "failed-to-run", "out-of-scope"])
def test_non_pass_outcomes_never_resolve(tmp_path, status):
    merge(tmp_path)
    after = merge(tmp_path, run=2, failures=(), outcomes=[outcome(status)])
    f = after["findings"][0]
    assert f["lifecycle_status"] == "not_rechecked" and f["resolution"] is None
    assert f["last_observed"] == "2026-10-01T00:00:00Z"


def test_absence_and_uncited_pass_never_resolve(tmp_path):
    merge(tmp_path)
    after = merge(tmp_path, run=2, failures=(), outcomes=[])
    assert after["findings"][0]["lifecycle_status"] == "not_rechecked"
    after = merge(tmp_path, run=3, failures=(), outcomes=[outcome(evidence_ids=[])])
    assert after["findings"][0]["lifecycle_status"] == "not_rechecked"


def test_partial_coverage_resolves_only_retested_page(tmp_path):
    merge(tmp_path, failures=("a", "b"))
    after = merge(tmp_path, run=2, failures=(), outcomes=[outcome()])
    states = {f["component"]: f["lifecycle_status"] for f in after["findings"]}
    assert states == {
        "https://example.test/a": "resolved",
        "https://example.test/b": "not_rechecked",
    }
    data, evidence, _ = store.report_data(after)
    text = render_markdown(data, evidence)
    assert "| Previously observed — not rechecked |" in text and "2026-10-01" in text


def test_recurrence_reopens_stable_identity_and_preserves_resolution_history(tmp_path):
    first = merge(tmp_path)
    merge(tmp_path, run=2, failures=(), outcomes=[outcome()])
    after = merge(tmp_path, run=3)
    f = after["findings"][0]
    assert f["id"] == first["findings"][0]["id"] and f["lifecycle_status"] == "current"
    assert f["resolution"] is None
    assert [event["status"] for event in f["events"]] == ["observed", "resolved", "reopened"]
    assert f["events"][1]["evidence_ids"] == ["ev-siteone-r2-001"]


def test_two_websites_never_merge_or_resolve_each_other(tmp_path):
    first = merge(tmp_path)
    merge(tmp_path, run=2, target="https://other.test/")
    after = merge(tmp_path, run=3, failures=(), outcomes=[outcome()])
    assert len(after["findings"]) == 2
    assert len({f["id"] for f in after["findings"]}) == 2
    assert (
        next(f for f in after["findings"] if f["id"] == first["findings"][0]["id"])[
            "lifecycle_status"
        ]
        == "resolved"
    )
    assert (
        next(f for f in after["findings"] if f["site"] == "other.test")["lifecycle_status"]
        == "not_rechecked"
    )


def test_legacy_migration_backs_up_exact_bytes_before_change(tmp_path):
    old = merge(tmp_path)
    old["schema_version"] = 2
    for f in old["findings"]:
        for key in ("site", "component", "coverage", "events", "lifecycle_status", "last_observed"):
            f.pop(key, None)
    old["runs"][0].pop("outcomes")
    path = store.store_file(tmp_path, "synthetic")
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    loaded = store.load(tmp_path, "synthetic")
    assert loaded and loaded["schema_version"] == 3
    (backup,) = path.parent.glob("assessment.json.backup-*")
    assert backup.read_bytes() == original and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert loaded["findings"][0]["lifecycle_status"] == "not_rechecked"
    assert loaded["runs"] == old["runs"]
    store.load(tmp_path, "synthetic")
    assert len(list(path.parent.glob("assessment.json.backup-*"))) == 1
    after = merge(tmp_path, run=2, failures=(), outcomes=[outcome()])
    assert after["findings"][0]["lifecycle_status"] == "resolved"


def test_failed_backup_leaves_legacy_store_unchanged(tmp_path, monkeypatch):
    old = merge(tmp_path)
    old["schema_version"] = 2
    path = store.store_file(tmp_path, "synthetic")
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    real_open = store.os.open

    def fail_backup(path, *args, **kwargs):
        if ".backup-" in str(path):
            raise OSError("synthetic disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", fail_backup)
    with pytest.raises(OSError):
        store.load(tmp_path, "synthetic")
    assert path.read_bytes() == original


def test_conflicting_outcomes_need_review_and_exclude_roadmap(tmp_path):
    merge(tmp_path)
    after = merge(tmp_path, run=2, failures=(), outcomes=[outcome(), outcome("fail")])
    assert after["findings"][0]["lifecycle_status"] == "needs_review"
    assert after["needs_review"]
    data, evidence, _ = store.report_data(after)
    assert "Needs review" in render_markdown(data, evidence)
    assert not data["ai"]["recommendations"]


def test_pass_for_smaller_component_sample_does_not_resolve(tmp_path):
    merge(tmp_path)
    after = merge(tmp_path, run=2, failures=(), outcomes=[outcome(coverage=[])])
    assert after["findings"][0]["lifecycle_status"] == "not_rechecked"


def test_real_siteone_outcomes_drive_runner_store_and_report(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ams_assess import runner
    from ams_assess.models import Context, create_run_dir
    from ams_assess.modules import siteone

    binary = tmp_path / "tools/siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda url: {"resolved": url})
    monkeypatch.setattr("ams_assess.coverage.presence.sitemap_check", lambda ctx: {})
    monkeypatch.setattr(runner, "REGISTRY", {"siteone": siteone})
    root = tmp_path / "customers"
    target = "https://example.test/"
    for title in ("", "Restored title"):
        monkeypatch.setattr(
            siteone.subproc,
            "run",
            lambda *a, title=title, **kw: SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps(
                    {
                        "results": [{"url": target, "status": 200, "type": 1}],
                        "tables": {
                            "seo": {
                                "rows": [
                                    {
                                        "urlPathAndQuery": "/",
                                        "title": title,
                                        "h1": "Heading",
                                        "description": "Description",
                                        "robotsIndex": 1,
                                    }
                                ]
                            }
                        },
                    }
                ),
            ),
        )
        ctx = Context(
            "synthetic",
            "Synthetic",
            target,
            {"seo"},
            set(),
            {},
            create_run_dir(root, "synthetic"),
            tmp_path / "tools",
            store_root=root,
        )
        result = runner.run(ctx, no_ai=True, formats={"md"})
        assert result.get("store_status") != "failed"
        row = next(o for o in result["outcomes"] if o["check_id"] == "missing-title")
        assert row["status"] == ("pass" if title else "fail")
        assert row["coverage"] == [target] and row["evidence_ids"]
    saved = store.load(root, "synthetic")
    assert saved
    (finding,) = saved["findings"]
    assert finding["lifecycle_status"] == "resolved" and finding["resolution"]["run_id"] == "r2"
    data, evidence, _ = store.report_data(saved)
    text = render_markdown(data, evidence)
    assert "Resolved" in text and "Prioritised improvement opportunities" not in text


def test_outcomes_keep_requested_dns_host_and_performance_device(tmp_path):
    from ams_assess import outcomes
    from ams_assess.models import Context, ModuleResult

    ctx = Context(
        "synthetic",
        "Synthetic",
        "https://example.test/",
        {"domain_email", "performance"},
        set(),
        {},
        tmp_path,
        tmp_path,
        resolved_url="https://www.example.test/",
    )
    dns = ModuleResult(name="domain_email")
    eid = dns.add_evidence(ctx.target, "Synthetic expiry", {})
    dns.checks = [
        {
            "key": "registration",
            "checks": [
                {"name": "Expiry", "status": "PASS", "evidence_ids": [eid], "detail": "Valid"}
            ],
        }
    ]
    outcomes.record(ctx, dns)
    assert dns.outcomes[0].check_id == "health:registration:expiry"
    assert dns.outcomes[0].site == "example.test"
    perf = ModuleResult(name="performance")
    perf.add_evidence(
        ctx.site_url,
        "PageSpeed mobile: lab and available field data",
        {"strategy": "mobile", "score": 0.98},
    )
    outcomes.record(ctx, perf)
    assert perf.outcomes[0].check_id == "lighthouse-mobile:https://example.test/"
    assert perf.outcomes[0].status == "pass"
    assert perf.outcomes[0].site == "example.test"


def test_site_check_pass_without_same_rendered_sample_does_not_resolve(tmp_path):
    from ams_assess import outcomes
    from ams_assess.models import Context, ModuleResult
    from ams_assess.modules.site_checks import Checks

    ctx = Context(
        "synthetic", "Synthetic", "https://example.test/", {"entity"}, set(), {}, tmp_path, tmp_path
    )
    root = tmp_path / "customers"
    for run, page in [(1, "a"), (2, "b")]:
        browser = ModuleResult(name="browser")
        url = ctx.target + page
        browser.add_evidence(
            url, "Rendered page evidence (desktop)", {"url": url, "page": {"json_ld": []}}
        )
        ctx.results = [browser]
        checks = Checks(ctx, {})
        checks.row(
            "structured-data",
            "Schema",
            ["entity"],
            "WARNING" if run == 1 else "PASS",
            "Synthetic check",
            "Entity clarity",
            "Correct schema",
            [],
            checks.refs("pages"),
        )
        outcomes.record(ctx, checks.result)
        data = {
            "target": ctx.target,
            "created_at": f"2026-10-0{run}",
            "status": "complete",
            "selected_categories": ["entity"],
            "assessed_categories": ["entity"],
            "findings": [f.model_dump() for f in checks.result.findings],
            "outcomes": [o.model_dump() for o in checks.result.outcomes],
            "ai": {"status": "skipped"},
            "modules": [],
            "pages": [url],
            "max_pages": 1,
            "limitations": [],
            "future": [],
            "report_files": [],
        }
        saved = store.merge_run(
            root,
            "synthetic",
            "Synthetic",
            data,
            browser.evidence + checks.result.evidence,
            str(run),
            ["public"],
        )
    assert saved["findings"][0]["lifecycle_status"] == "not_rechecked"


def test_category_selection_does_not_change_check_identity(tmp_path):
    from ams_assess.lifecycle import scoped, transition

    evidence = [
        Evidence(
            id="ev-source-001",
            source="source",
            url="https://example.test/",
            description="Synthetic observation",
            data={},
        )
    ]
    finding = Finding(
        id="issue",
        title="Synthetic",
        category="technical",
        check_key="shared-check",
        evidence_ids=[evidence[0].id],
        detail="Observed",
        recommendation="Review",
    )
    initial = scoped([finding], evidence[0].url, "synthetic")
    changed = scoped([finding.model_copy(update={"category": "seo"})], evidence[0].url, "synthetic")
    assert initial[0].id == changed[0].id
    rows = [observe("source", "shared-check", "seo", evidence[0].url, "pass", [evidence[0].id])]
    assert (
        transition(initial, [], rows, evidence, "r2", "2026-10-24")[0].lifecycle_status
        == "resolved"
    )


def test_conflict_remains_needs_review_when_next_run_has_no_equivalent_result(tmp_path):
    merge(tmp_path)
    merge(tmp_path, run=2, failures=(), outcomes=[outcome(), outcome("fail")])
    saved = merge(tmp_path, run=3, failures=(), outcomes=[])
    assert saved["findings"][0]["lifecycle_status"] == "needs_review" and saved["needs_review"]


def test_failed_collector_records_explicit_unrun_checks(tmp_path):
    from ams_assess import outcomes
    from ams_assess.models import Context, ModuleResult

    ctx = Context(
        "synthetic", "Synthetic", "https://example.test/", {"seo"}, set(), {}, tmp_path, tmp_path
    )
    result = ModuleResult(name="siteone", status="error", reason="Synthetic launch error")
    outcomes.record(ctx, result)
    title = next(o for o in result.outcomes if o.check_id == "missing-title")
    assert title.status == "failed-to-run" and title.coverage == []
    technical = next(o for o in result.outcomes if o.check_id == "broken-links")
    assert technical.status == "out-of-scope" and not technical.coverage


@pytest.mark.parametrize("first_host", ["example.test", "www.example.test"])
def test_www_apex_flip_preserves_page_identity_resolution_and_recurrence(tmp_path, first_host):
    first = f"https://{first_host}/"
    second = (
        "https://example.test/" if first_host.startswith("www.") else "https://www.example.test/"
    )
    before = merge(tmp_path, target=first)
    passing = observe("siteone", "missing-title", "seo", second + "a", "pass", ["ev-siteone-001"])
    after = merge(tmp_path, run=2, target=second, failures=(), outcomes=[passing])
    assert len(after["findings"]) == 1
    assert after["findings"][0]["lifecycle_status"] == "resolved"
    again = merge(tmp_path, run=3, target=second)
    assert len(again["findings"]) == 1
    assert again["findings"][0]["id"] == before["findings"][0]["id"]
    assert again["findings"][0]["events"][-1]["status"] == "reopened"
    # Other subdomains remain separate websites, even under the same registrable domain.
    other = merge(tmp_path, run=4, target="https://shop.example.test/")
    assert len(other["findings"]) == 2


@pytest.mark.parametrize("schema", [2, 3])
@pytest.mark.parametrize("strategy", ["mobile", "desktop"])
def test_legacy_lighthouse_homepage_resolves_with_per_page_check_and_preserves_history(
    tmp_path, monkeypatch, schema, strategy
):
    from ams_assess import runner
    from ams_assess.models import Context, create_run_dir
    from ams_assess.modules import performance

    # Seed a genuine pre-per-page record with a www address and a historical evidence ID.
    saved = store.new_store("synthetic", "Synthetic")
    saved["schema_version"] = schema
    url = "https://www.example.com/"
    eid = "ev-performance-r1-001"
    legacy = Finding(
        id="legacy-lab",
        title="Improve lab performance",
        detail="Synthetic low score",
        recommendation="Retest rendering",
        category="performance",
        check_key=f"lighthouse-{strategy}",
        evidence_ids=[eid],
        urls=[url],
        site="www.example.com",
        component=url,
        coverage=[url],
        events=[{"status": "observed", "run_id": "r1"}],
    )
    saved["findings"] = [legacy.model_dump()]
    saved["runs"] = [
        {
            "run_id": "r1",
            "run_dir": "legacy",
            "kind": "assessment",
            "status": "complete",
            "target": url,
            "created_at": "2026-10-01",
        }
    ]
    saved["evidence"] = [
        Evidence(
            id=eid,
            source="performance",
            url=url,
            description="PageSpeed legacy lab",
            data={"strategy": strategy, "score": 0.5},
        ).model_dump()
    ]
    store.save(tmp_path, "synthetic", saved)
    original = store.store_file(tmp_path, "synthetic").read_bytes()
    migrated = store.load(tmp_path, "synthetic")
    assert migrated is not None
    old_id = migrated["findings"][0]["id"]
    old_events = migrated["findings"][0]["events"]
    if schema == 2:
        assert (
            next((tmp_path / "synthetic").glob("assessment.json.backup-*")).read_bytes() == original
        )
    else:
        assert old_id == "legacy-lab"

    monkeypatch.setattr(runner, "REGISTRY", {"performance": performance})
    monkeypatch.setattr(
        performance,
        "query_crux",
        lambda scope, url, key: {
            "scope": scope,
            "url": url,
            "outcome": "not_configured",
            "detail": "No API configured",
        },
    )
    score = 0.98
    monkeypatch.setattr(
        performance,
        "measure_lab",
        lambda url, device, key: {
            "url": url,
            "strategy": device,
            "outcome": "data",
            "kind": "LAB",
            "score": score,
            "metrics": {},
            "conditions": {
                "url_tested": url,
                "final_url": url,
                "requested_at_utc": "2026-10-24",
                "fetch_time": None,
                "lighthouse_version": None,
                "form_factor": device,
                "throttling_method": None,
                "throttling": None,
            },
            "detail": "Synthetic lab observation",
        },
    )
    for expected in ("resolved", "current"):
        ctx = Context(
            "synthetic",
            "Synthetic",
            "https://example.com/",
            {"performance"},
            set(),
            {"AMS_PERFORMANCE_KEY_PAGES": "0"},
            create_run_dir(tmp_path, "synthetic"),
            tmp_path,
            store_root=tmp_path,
        )
        result = runner.run(ctx, no_ai=True, formats={"md"})
        assert result.get("store_status") != "failed"
        assert result["status"] != "incomplete"
        updated = store.load(tmp_path, "synthetic")
        assert updated is not None
        finding = next(f for f in updated["findings"] if f["id"] == old_id)
        assert finding["lifecycle_status"] == expected
        assert finding["events"][: len(old_events)] == old_events
        assert eid in finding["evidence_ids"] or eid in json.dumps(finding["events"])
        assert len([f for f in updated["findings"] if strategy in f["check_key"]]) == 1
        if expected == "resolved":
            assert finding["resolution"]["evidence_ids"]
        else:
            assert finding["events"][-1]["status"] == "reopened"
        # The original CrUX provider statuses are retained, not converted to a vitals verdict.
        crux = [o for o in result["outcomes"] if o["check_id"].startswith("crux-availability:")]
        assert crux and all(o["status"] == "unavailable" for o in crux)
        assert any(e["data"].get("outcome") == "not_configured" for e in updated["evidence"])
        score = 0.5


@pytest.mark.parametrize("case", ["other-page", "other-device", "other-site", "unavailable"])
def test_legacy_lighthouse_requires_equivalent_passing_homepage(case):
    from ams_assess.lifecycle import transition

    url = "https://www.example.com/"
    old = Finding(
        id="old-lab",
        title="Old lab failure",
        detail="Synthetic low score",
        recommendation="Retest rendering",
        category="performance",
        check_key="lighthouse-mobile",
        site="www.example.com",
        component=url,
        coverage=[url],
        evidence_ids=["ev-performance-001"],
    )
    page = "https://example.com/"
    if case == "other-page":
        page += "contact"
    elif case == "other-site":
        page = "https://shop.example.com/"
    strategy = "desktop" if case == "other-device" else "mobile"
    row = observe(
        "performance",
        f"lighthouse-{strategy}:{page}",
        "performance",
        page,
        "unavailable" if case == "unavailable" else "pass",
        ["ev-performance-002"],
    )
    evidence = [
        Evidence(
            id="ev-performance-002", source="performance", url=page, description="Retest", data={}
        )
    ]
    result = transition([old], [], [row], evidence, "r2", "2026-10-24")
    assert result[0].lifecycle_status == "not_rechecked" and result[0].resolution is None


@pytest.mark.parametrize("final_path,expected", [("/", "pass"), ("/contact", "unavailable")])
def test_lighthouse_redirect_pass_requires_equivalent_page(final_path, expected, tmp_path):
    from ams_assess import outcomes
    from ams_assess.models import Context, ModuleResult

    ctx = Context(
        "synthetic",
        "Synthetic",
        "https://example.com/",
        {"performance"},
        set(),
        {},
        tmp_path,
        tmp_path,
    )
    result = ModuleResult(name="performance")
    result.add_evidence(
        ctx.target,
        "PageSpeed mobile LAB: data",
        {
            "strategy": "mobile",
            "score": 0.98,
            "outcome": "data",
            "conditions": {"final_url": "https://www.example.com" + final_path},
        },
    )
    outcomes.record(ctx, result)
    assert result.outcomes[0].status == expected
    assert bool(result.outcomes[0].coverage) == (expected == "pass")


@pytest.mark.parametrize("legacy_records", [1, 2])
def test_migration_deduplicates_www_apex_lighthouse_identity(tmp_path, legacy_records):
    saved = merge(tmp_path, failures=("",), check="lighthouse-mobile", category="performance")
    saved["schema_version"] = 2
    first = {**saved["evidence"][0], "id": "ev-performance-r1-001", "source": "performance"}
    second = {**first, "id": "ev-performance-r1-002", "url": "https://www.example.test/"}
    saved["evidence"] = [first, second]
    saved["findings"] = [
        {
            **saved["findings"][0],
            "id": f"legacy-{i}",
            "urls": [],
            "site": "",
            "component": "",
            "coverage": [],
            "evidence_ids": [first["id"], second["id"]],
        }
        for i in range(legacy_records)
    ]
    path = store.store_file(tmp_path, "synthetic")
    path.write_text(json.dumps(saved))
    original = path.read_bytes()
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None and len(loaded["findings"]) == 1
    finding = loaded["findings"][0]
    assert finding["site"] == "example.test" and finding["component"] == "https://example.test/"
    assert finding["check_key"] == "lighthouse-mobile:https://example.test/"
    assert set(finding["evidence_ids"]) == {first["id"], second["id"]}
    assert next(path.parent.glob("assessment.json.backup-*")).read_bytes() == original
    assert store.load(tmp_path, "synthetic") == loaded


def test_store_rejects_duplicate_finding_ids_on_load_and_save(tmp_path):
    saved = merge(tmp_path)
    saved["findings"].append(dict(saved["findings"][0]))
    path = store.store_file(tmp_path, "synthetic")
    previous = path.read_bytes()
    with pytest.raises(ValueError, match="Duplicate finding IDs"):
        store.save(tmp_path, "synthetic", saved)
    assert path.read_bytes() == previous
    path.write_text(json.dumps(saved))
    duplicate_bytes = path.read_bytes()
    with pytest.raises(ValueError, match="Duplicate finding IDs"):
        store.load(tmp_path, "synthetic")
    assert path.read_bytes() == duplicate_bytes


def test_domain_scoped_legacy_finding_is_visible_with_clean_state_and_date(tmp_path):
    saved = merge(tmp_path)
    saved["schema_version"] = 2
    saved["evidence"][0]["url"] = "dns:example.test"
    saved["findings"][0].update(
        title="Public resolvers disagree on A records",
        category="domain_email",
        check_key="resolver-consistency",
        site="",
        component="",
        coverage=[],
        urls=[],
    )
    store.save(tmp_path, "synthetic", saved)
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None
    data, evidence, _ = store.report_data(loaded)
    report = render_markdown(data, evidence)
    table = report.split("### Finding lifecycle", 1)[1].split("| Source /", 1)[0]
    assert "Public resolvers disagree on A records" in table and "example.test" in table
    assert "| Previously observed — not rechecked |" in table and "2026-10-01" in table
    assert table.lower().count("not rechecked") == 1


def test_current_findings_cite_current_observation_and_keep_history_separate(tmp_path):
    first = merge(tmp_path)
    again = merge(tmp_path, run=2)
    finding = again["findings"][0]
    assert finding["evidence_ids"] == ["ev-siteone-r2-001"]
    assert finding["events"][0]["evidence_ids"] == first["findings"][0]["evidence_ids"]
    assert finding["events"][-1]["evidence_ids"] == finding["evidence_ids"]


@pytest.mark.parametrize("historical_state", ["resolved", "not_rechecked"])
def test_ai_history_filter_preserves_shared_current_and_mixed_evidence_sentences(
    tmp_path, historical_state
):
    merge(tmp_path, failures=("a", "b"))
    saved = merge(tmp_path, run=2, failures=("b",), outcomes=[outcome(), outcome("fail", "b")])
    old = next(f for f in saved["findings"] if f["component"].endswith("/a"))
    old.update(
        lifecycle_status=historical_state, evidence_ids=["ev-siteone-r1-001", "ev-siteone-r2-001"]
    )
    saved["finding_history"].append(
        {**old, "id": "superseded-copy", "status": "superseded", "evidence_ids": ["ev-history-001"]}
    )
    saved["evidence"].append(
        Evidence(
            id="ev-history-001",
            source="history",
            url="https://example.test/",
            description="Superseded",
            data={},
        ).model_dump()
    )
    current = "Current evidence supports review. (ev-siteone-r2-001)"
    stale = "Historical title failure. (ev-siteone-r1-001)"
    mixed = "Mixed evidence supports this review (ev-siteone-r1-001, ev-siteone-r2-001)."
    superseded = "Superseded claim. Evidence: ev-history-001"
    saved["runs"][-1]["ai"] = {
        "status": "ok",
        "executive_summary": " ".join([current, stale, mixed, superseded]),
        "sections": {"seo": " ".join([current, stale, mixed]), "security": superseded},
    }
    data, _, _ = store.report_data(saved)
    summary = data["ai"]["executive_summary"]
    assert current in summary and mixed in summary and superseded not in summary
    assert (stale in summary) is (historical_state == "not_rechecked")
    assert current in data["ai"]["sections"]["seo"] and "security" not in data["ai"]["sections"]


@pytest.mark.parametrize("schema", [2, 3])
@pytest.mark.parametrize("status", ["fail", "pass"])
@pytest.mark.parametrize("predecessor", ["header-missing:csp", "framing-policy"])
def test_legacy_header_findings_have_one_consolidated_successor(
    tmp_path, schema, status, predecessor
):
    saved = merge(tmp_path, failures=("",), category="security", check=predecessor)
    saved["schema_version"] = schema
    saved["findings"][0]["check_key"] = predecessor
    saved["findings"][0]["id"] = "legacy-csp"
    second = {**saved["findings"][0], "id": "legacy-hsts", "check_key": "header-missing:hsts"}
    saved["findings"].append(second)
    original_ids = {f["id"] for f in saved["findings"]}
    store.save(tmp_path, "synthetic", saved)
    after = merge(
        tmp_path,
        run=2,
        failures=("",) if status == "fail" else (),
        category="security",
        check="security-headers",
        outcomes=[
            observe(
                "siteone",
                "security-headers",
                "security",
                "https://example.test/",
                status,
                ["ev-siteone-001"],
            )
        ],
    )
    assert len(after["findings"]) == 1
    finding = after["findings"][0]
    assert finding["check_key"] == "security-headers"
    assert finding["lifecycle_status"] == ("current" if status == "fail" else "resolved")
    assert original_ids <= {f["id"] for f in after["finding_history"]}
    data, evidence, _ = store.report_data(after)
    report = render_markdown(data, evidence)
    assert (
        "header-missing:"
        not in report.split("### Finding lifecycle", 1)[1].split("| Source /", 1)[0]
    )
    assert len(data["findings"]) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_deduplicate_technical_preserves_observation_date_bounds(reverse):
    findings = [
        Finding(
            id=f"legacy-{run}",
            title="Header review",
            category="security",
            check_key="security-headers",
            site="example.test",
            component="https://example.test/",
            evidence_ids=[f"ev-headers-{run:03d}"],
            detail="Observed",
            recommendation="Review",
            first_observed=first,
            last_observed=last,
            last_run_id=f"r{run}",
        )
        for run, first, last in [
            (1, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z"),
            (2, "2026-10-03T00:00:00Z", "2026-10-23T00:00:00Z"),
            (3, "unknown", "unknown"),
        ]
    ]
    merged = store.deduplicate_technical(findings[::-1] if reverse else findings)
    assert len(merged) == 1
    assert merged[0].first_observed == "2026-10-01T00:00:00Z"
    assert merged[0].last_observed == "2026-10-23T00:00:00Z"
    assert merged[0].last_run_id == "r2"


@pytest.mark.parametrize("missing_date", [False, True])
def test_restored_ai_narratives_identify_their_generating_run_and_date(tmp_path, missing_date):
    merge(tmp_path)
    merge(tmp_path, run=2, failures=("b",), category="security")
    saved = merge(tmp_path, run=3, failures=(), outcomes=[])
    assert all(f["lifecycle_status"] == "not_rechecked" for f in saved["findings"])
    seo = "Titles need review. (ev-siteone-r1-001)"
    security = "Security needs review. (ev-siteone-r2-001)"
    summary = "Review the observed findings. (ev-siteone-r2-001)"
    saved["runs"][0]["ai"] = {
        "status": "ok",
        "executive_summary": seo,
        "sections": {"seo": seo},
    }
    saved["runs"][1]["ai"] = {
        "status": "ok",
        "executive_summary": summary,
        "sections": {"security": security},
    }
    if missing_date:
        saved["runs"][1].pop("created_at")
    run2_date = "(date not recorded)" if missing_date else "on 2026-10-02"
    original = json.dumps(saved)
    for _ in range(2):
        data, evidence, _ = store.report_data(saved)
        narratives = [
            (data["ai"]["executive_summary"], summary, "r2", run2_date),
            (data["ai"]["sections"]["seo"], seo, "r1", "on 2026-10-01"),
            (data["ai"]["sections"]["security"], security, "r2", run2_date),
        ]
        report = render_markdown(data, evidence)
        for text, body, run, date in narratives:
            assert text.startswith(f"As observed in run {run} {date}.")
            assert text.count("As observed in run") == 1 and body in text
            assert text in report
        assert json.dumps(saved) == original


@pytest.mark.parametrize("cited", [True, False])
def test_outcome_only_recurrence_preserves_prior_basis_and_requires_current_citation(
    tmp_path, cited
):
    saved = merge(tmp_path)
    saved["findings"][0]["events"] = [{"status": "legacy-observed"}]
    store.save(tmp_path, "synthetic", saved)
    after = merge(
        tmp_path,
        run=2,
        failures=(),
        outcomes=[
            outcome("fail", evidence_ids=["ev-siteone-001"] if cited else []),
        ],
    )
    finding = after["findings"][0]
    if cited:
        assert finding["evidence_ids"] == ["ev-siteone-r2-001"]
        assert "ev-siteone-r1-001" in json.dumps(finding["events"])
        assert finding["lifecycle_status"] == "current"
    else:
        assert finding["lifecycle_status"] == "not_rechecked"
        assert finding["evidence_ids"] == ["ev-siteone-r1-001"]
