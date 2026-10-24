"""Offline regressions using synthetic records only; no customer fixtures."""

import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from jinja2 import TemplateError
from test_domain_email import context as domain_context
from test_infrastructure import block, mock_domain
from test_live_run_fixes import context
from test_register import fixture_run
from test_ui import form, running

from ams_assess import cli, register, report, runner, store
from ams_assess.lifecycle import scoped, transition
from ams_assess.models import Evidence, ModuleResult
from ams_assess.modules import browser, dataforseo, domain_email


@pytest.mark.parametrize("payload", [[], [{"host": "mail.example.test"}], "text", None, 7])
def test_non_mapping_evidence_renders_run_and_combined(tmp_path, monkeypatch, payload):
    mock_domain(monkeypatch)
    ctx = domain_context(tmp_path)
    ctx.store_root = tmp_path
    result = domain_email.run(ctx)
    result.add_evidence(ctx.target, "Synthetic mail checks", payload)
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "domain_email": SimpleNamespace(categories={"domain_email"}, run=lambda ctx: result),
        },
    )
    data = runner.run(ctx, no_ai=True, formats={"md", "html"})
    assert data["status"] != "incomplete"
    assert set(data["report_files"]) == {"report.md", "report.html"}
    assert not any("Report generation failed" in line for line in data["limitations"])
    assert set(report.render_customer(tmp_path, "test", {"md", "html"})) == {
        "report.md",
        "report.html",
    }
    assert "Infrastructure attribution" in (ctx.run_dir / "report.md").read_text()
    extra = [
        Evidence(
            id=f"ev-{m}-999",
            source=m,
            url=ctx.target,
            description="Synthetic non-mapping data",
            data=payload,
        )
        for m in ("performance", "site_checks", "gsc")
    ]
    report.render_markdown(data, result.evidence + extra)


@pytest.mark.parametrize("failure", ["template", "partial-output", "store-save"])
def test_failed_report_revision_is_removed_and_number_reused(tmp_path, monkeypatch, failure):
    fixture_run(tmp_path)
    report.render_customer(tmp_path, "synthetic", {"md", "html"})
    directory = tmp_path / "synthetic"
    before = (directory / "assessment.json").read_bytes()
    revisions = sorted((directory / "reports").iterdir())
    originals = {p: p.read_bytes() for p in revisions[0].iterdir()}
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise TemplateError("synthetic-private-diagnostic")

        def partial(path, *args):
            (path / "report.md").write_text("partial")
            raise OSError("synthetic PDF failure")

        if failure == "template":
            patch.setattr(report, "render_markdown", fail)
        elif failure == "partial-output":
            patch.setattr(report, "write_outputs", partial)
        else:
            patch.setattr(report, "save", fail)
        with pytest.raises((TemplateError, OSError)):
            report.render_customer(tmp_path, "synthetic", {"md", "html", "pdf"})
    assert (directory / "assessment.json").read_bytes() == before
    assert sorted((directory / "reports").iterdir()) == revisions
    assert all(p.read_bytes() == contents for p, contents in originals.items())
    report.render_customer(tmp_path, "synthetic", {"md", "html"})
    current = store.load(tmp_path, "synthetic")
    assert current is not None
    assert [r["revision"] for r in current["report_revisions"]] == [1, 2]
    assert all(any(p.iterdir()) for p in (directory / "reports").iterdir())


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_ui_template_error_returns_fixed_500(tmp_path, monkeypatch, method):
    store.set_profile(tmp_path, "synthetic", {"name": "Synthetic"})

    def fail(*args, **kwargs):
        raise TemplateError("sensitive path and synthetic-secret")

    with running(tmp_path) as (server, client):
        if method == "GET":
            monkeypatch.setattr(server.template, "render", fail)
            response = client.get(server.url(customer="synthetic"))
        else:
            monkeypatch.setattr(store, "set_register", fail)
            response = client.post(
                server.url("/update"), data=form(server), headers={"Origin": server.origin}
            )
    assert response.status_code == 500
    assert response.text == "Unable to render local interface"


@pytest.mark.parametrize("cost,stopped", [("0.01212", False), ("0.020", True)])
def test_domain_rank_estimate_headroom_retains_overcharge_stop(
    tmp_path, monkeypatch, cost, stopped
):
    ctx = context(tmp_path)
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "status_code": 20000,
                "cost": float(cost) if len(calls) == 1 else 0.002,
                "tasks": [{"status_code": 20000, "result": []}],
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    assert dataforseo.ESTIMATE_USD >= Decimal("0.01212")
    assert dataforseo.run(ctx).status == "ok"
    assert ctx.dataforseo_spent_usd == Decimal(cost)
    assert bool(ctx.dataforseo_stopped) is stopped
    search = dataforseo.serp(ctx, "maps", "Synthetic Plumbing", 1e12)
    assert search["status"] == ("refused" if stopped else "ok")
    assert len(calls) == (1 if stopped else 2)


@pytest.mark.parametrize(
    "site,location",
    [(None, ""), ("", ""), (None, "Sydney"), ("", "Sydney"), ("https://example.test/", "Sydney")],
)
def test_register_set_reuses_sole_platform_entry(tmp_path, monkeypatch, site, location):
    fixture_run(tmp_path)
    monkeypatch.setattr(cli, "data_root", lambda: tmp_path)
    args = [
        "register",
        "set",
        "--customer",
        "synthetic",
        "--platform",
        "google_business_profile",
        "--presence",
        "customer_reported",
        "--location",
        location,
    ]
    if site is not None:
        args += ["--site", site]
    assert cli.main(args) == 0
    current = store.register_store(tmp_path, "synthetic")
    rows = [
        e
        for e in current["systems_register"]["entries"]
        if e["platform"] == "google_business_profile"
    ]
    assert len(rows) == 1 and rows[0]["site"] == "https://example.test/"
    assert rows[0]["location"] == "" and rows[0]["presence"] == "customer_reported"


def test_register_scope_selection_and_ambiguity(tmp_path):
    fixture_run(tmp_path)
    store.set_register(
        tmp_path,
        "synthetic",
        "ga4",
        {"presence": "customer_reported"},
        "https://second.example.test/",
        "Sydney",
    )
    with pytest.raises(ValueError, match="Several websites"):
        store.set_register(tmp_path, "synthetic", "ga4", {"ams_access": "requested"})
    row = store.set_register(
        tmp_path, "synthetic", "ga4", {"ams_access": "granted"}, location="Sydney"
    )
    assert row["site"] == "https://second.example.test/" and row["location"] == "Sydney"
    # A different existing customer-wide scope remains explicitly addressable.
    with store.locked(tmp_path, "synthetic"):
        current = store.load(tmp_path, "synthetic")
        assert current is not None
        register.manual_edit(current, "ga4", "", "", {"presence": "unknown"})
        store.save(tmp_path, "synthetic", current)
    row = store.set_register(tmp_path, "synthetic", "ga4", {"ams_access": "denied"}, site="")
    assert row["site"] == "" and row["ams_access"] == "denied"


def test_register_remove_exact_duplicate_preserves_history(tmp_path, monkeypatch):
    fixture_run(tmp_path)
    with store.locked(tmp_path, "synthetic"):
        current = store.load(tmp_path, "synthetic")
        assert current is not None
        duplicate = register.manual_edit(
            current,
            "google_business_profile",
            "",
            "Sydney",
            {"presence": "customer_reported", "note": "Keep history"},
        )
        store.save(tmp_path, "synthetic", current)
    before = copy.deepcopy(current)
    with pytest.raises(ValueError, match="exact"):
        store.remove_register(tmp_path, "synthetic", "google_business_profile", "")
    monkeypatch.setattr(cli, "data_root", lambda: tmp_path)
    assert (
        cli.main(
            [
                "register",
                "remove",
                "--customer",
                "synthetic",
                "--platform",
                "google_business_profile",
                "--site",
                "",
                "--location",
                "Sydney",
                "--who",
                "Reviewer",
            ]
        )
        == 0
    )
    after = store.register_store(tmp_path, "synthetic")
    assert after["evidence"] == before["evidence"]
    assert after["runs"] == before["runs"]
    reg = after["systems_register"]
    assert reg["history"][:-1] == before["systems_register"]["history"]
    assert reg["history"][-1]["old"] == duplicate and reg["history"][-1]["new"] is None
    assert reg["history"][-1]["who"] == "Reviewer"
    assert reg["entries"] == [e for e in before["systems_register"]["entries"] if e != duplicate]
    report.render_customer(tmp_path, "synthetic", {"md"})
    assert not any(
        e["site"] == ""
        for e in store.register_store(tmp_path, "synthetic")["systems_register"]["entries"]
    )


@pytest.mark.parametrize(
    "ns,expected",
    [(["synthetic.siteground.net."], ["SiteGround"]), (["siteground.net.example.test."], [])],
)
def test_siteground_dns_attribution_and_hostname_boundary(tmp_path, monkeypatch, ns, expected):
    mock_domain(monkeypatch, ns=ns)
    claim = block(domain_email.run(domain_context(tmp_path)))["dns_provider"]
    assert claim["value"] == expected and claim["label"] == "INFERRED"


def axe_result():
    result = ModuleResult(name="browser")
    audit = {
        "passes": [{"id": "color-contrast", "nodes": [{}] * 15}],
        "violations": [{"id": "color-contrast", "nodes": [{}]}],
    }
    target = "https://example.test/"
    eid = result.add_evidence(target, "Synthetic axe audit", {"axe": audit})
    component = target + "::desktop"
    result.outcomes = browser.axe_outcomes(audit, component, eid)
    result.finding(
        "axe:color-contrast",
        "Contrast failure",
        "accessibility",
        [eid],
        "One failing element",
        "Improve contrast",
        urls=[target],
        component=component,
    )
    return result


def test_axe_mixed_nodes_emit_single_fail_and_finding_stays_current(tmp_path, monkeypatch):
    result = axe_result()
    assert [o.status for o in result.outcomes] == ["fail"]
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "browser": SimpleNamespace(categories={"accessibility"}, run=lambda ctx: result),
        },
    )
    ctx = context(tmp_path, categories={"accessibility"}, store_root=tmp_path)
    runner.run(ctx, no_ai=True, formats={"md"})
    saved = store.load(tmp_path, "synthetic")
    assert saved is not None
    assert saved["findings"][0]["lifecycle_status"] == "current"
    assert not saved["needs_review"]


@pytest.mark.parametrize(
    "groups,status",
    [
        (["passes"], "pass"),
        (["passes", "incomplete"], "unavailable"),
        (["inapplicable"], "out-of-scope"),
        (["passes", "violations", "incomplete"], "fail"),
    ],
)
def test_axe_page_verdict_precedence(groups, status):
    audit = {g: [{"id": "color-contrast", "nodes": [{}]}] for g in groups}
    assert [
        o.status for o in browser.axe_outcomes(audit, "https://example.test/::mobile", "ev-1")
    ] == [status]
    assert browser.axe_outcomes({}, "https://example.test/", "ev-1") == []


@pytest.mark.parametrize("incomplete", [False, True])
@pytest.mark.parametrize("different_audit", [False, True])
def test_historical_axe_conflict_projection_preserves_raw_run(
    tmp_path, monkeypatch, different_audit, incomplete
):
    result = axe_result()
    findings = scoped(result.findings, "https://example.test/", "synthetic")
    failure = result.outcomes[0]
    passing = failure.model_copy(update={"status": "pass"})
    if different_audit:
        eid = result.add_evidence("https://example.test/", "Separate audit", {})
        passing.evidence_ids = [eid]
    transitioned = transition([], findings, [passing, failure], result.evidence, "r1", "2026-10-01")
    assert transitioned[0].lifecycle_status == ("needs_review" if different_audit else "current")
    # Simulate the old collector/store, which incorrectly held the same-audit pair for review.
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "browser": SimpleNamespace(categories={"accessibility"}, run=lambda ctx: result),
        },
    )
    ctx = context(tmp_path, categories={"accessibility"}, store_root=tmp_path)
    runner.run(ctx, no_ai=True, formats={"md"})
    path = tmp_path / "synthetic/assessment.json"
    old = json.loads(path.read_text())
    run = old["runs"][-1]
    stored_failure = next(o for o in run["outcomes"] if o["status"] == "fail")
    run["outcomes"].append(
        {
            **stored_failure,
            "status": "pass",
            "evidence_ids": ["ev-browser-r1-002"]
            if different_audit
            else stored_failure["evidence_ids"],
        }
    )
    if incomplete:
        run["outcomes"].append({**stored_failure, "status": "unavailable"})
    old["findings"][0]["lifecycle_status"] = "needs_review"
    old["needs_review"] = [
        {
            "finding_id": old["findings"][0]["id"],
            "title": old["findings"][0]["title"],
            "evidence_ids": old["findings"][0]["evidence_ids"],
            "reason": "conflicting explicit check outcomes need review",
        }
    ]
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None
    assert loaded["findings"][0]["lifecycle_status"] == (
        "needs_review" if different_audit else "current"
    )
    assert bool(loaded["needs_review"]) is different_audit
    assert path.read_bytes() == before  # A UI GET only projects the correction.
    report.render_customer(tmp_path, "synthetic", {"md"})
    after = json.loads(path.read_text())
    assert after["runs"] == old["runs"] and after["evidence"] == old["evidence"]
    assert after["findings"][0]["lifecycle_status"] == (
        "needs_review" if different_audit else "current"
    )
