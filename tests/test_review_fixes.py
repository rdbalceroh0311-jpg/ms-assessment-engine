"""The six OpenCode review defects: deterministic, synthetic regressions."""

import json
from types import SimpleNamespace

import pytest

from ams_assess import ai, figures, runner
from ams_assess.consolidate import consolidate_security_headers
from ams_assess.models import Context, Evidence, Finding, ModuleResult, create_run_dir
from ams_assess.modules import siteone


def customer_evidence(statement="Approximately 20 enquiries per week"):
    return Evidence(
        id="ev-discovery-001",
        source="discovery",
        url="customer-discovery-notes",
        description="Customer statement",
        data={"statement": statement},
        provenance="customer_statement",
    )


def context(tmp_path):
    return Context(
        "synthetic",
        "Synthetic",
        "https://example.test/",
        {"security"},
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        tmp_path,
    )


def test_siteone_scrubs_schemeless_secret_bearing_diagnostics(tmp_path):
    ctx = context(tmp_path)
    message = (
        "Failed example.test/path?token=supersecret#private and "
        "//user:password@example.test/a?key=hidden"
    )
    cleaned = siteone.diagnostic(message, ctx)
    assert "example.test/path" in cleaned
    assert not any(secret in cleaned for secret in ["supersecret", "password", "hidden", "private"])


def test_recorded_customer_figure_renders_with_source_units_and_verification_label():
    evidence = customer_evidence()
    value = "Customer reports approximately 20 enquiries per week. (ev-discovery-001)"
    assert not ai.unsupported_claim(value, [evidence])
    result, _, warnings = ai.validate_analysis({"executive_summary": value}, [evidence], set(), [])
    assert not warnings
    assert result["executive_summary"] == (
        "Customer reports approximately 20 enquiries per week; not independently verified. "
        "(ev-discovery-001)"
    )
    assert figures.customer_statement(evidence.data["statement"]) == (
        "Customer reports approximately 20 enquiries per week; not independently verified."
    )


def test_customer_figure_preserves_period_qualifiers_and_context():
    evidence = customer_evidence("Up to 20 enquiries per week during the busy season")
    result = figures.render("Customer reports up to 20 enquiries per week", [evidence])
    assert result == (
        "Customer reports up to 20 enquiries per week during the busy season; "
        "not independently verified. (ev-discovery-001)"
    )
    assert ai.unsupported_claim("Customer reports 20 enquiries per week", [evidence])
    assert ai.unsupported_claim("Customer reports up to 20 enquiries per month", [evidence])
    assert ai.unsupported_claim("Customer reports up to 20 bookings per week", [evidence])


@pytest.mark.parametrize(
    "claim",
    [
        "Customer reports approximately 30 enquiries per week",
        "We verified approximately 20 enquiries per week",
        "We measured approximately 20 enquiries per week",
        "Will generate approximately 20 enquiries per week",
        "Approximately 20 enquiries per week is guaranteed",
        "Approximately 20 enquiries per week of lost revenue",
        "ROI from approximately 20 enquiries per week",
        "Approximately 20 new enquiries per week",
        "Approximately 20 more enquiries per week",
    ],
)
def test_invented_upgraded_or_promised_customer_figures_are_rejected(claim):
    result, _, warnings = ai.validate_analysis(
        {"executive_summary": claim + " (ev-discovery-001)"}, [customer_evidence()], set(), []
    )
    assert not result["executive_summary"] and warnings


def test_measured_figure_requires_matching_structured_measurement():
    evidence = Evidence(
        id="ev-metrics-001",
        source="metrics",
        url="https://example.test/",
        description="Measured count",
        data={
            "measurements": [
                {
                    "value": 20,
                    "unit": "enquiries",
                    "period": "week",
                    "context": "in the recorded sample",
                }
            ]
        },
    )
    assert not ai.unsupported_claim("20 enquiries per week", [evidence])
    assert ai.unsupported_claim("20 calls per week", [evidence])
    evidence.data = {"score": 20}
    assert ai.unsupported_claim("20 enquiries per week", [evidence])


def test_observatory_supporting_evidence_is_deduplicated():
    evidence = Evidence(
        id="ev-observatory-001",
        source="observatory",
        url="https://example.test/",
        description="Grade",
        data={"grade": "D"},
    )
    finding = Finding(
        id="header",
        title="Header",
        category="security",
        check_key="header-missing:csp",
        evidence_ids=[evidence.id],
        detail="Absent",
        recommendation="Review",
    )
    result = consolidate_security_headers([finding], [evidence, evidence])
    assert result[0].evidence_ids == [evidence.id]


@pytest.mark.parametrize("state", ["not-selected", "skipped", "error", "started"])
def test_crawl_assets_excluded_only_when_siteone_started(tmp_path, monkeypatch, state):
    monkeypatch.setattr("ams_assess.coverage.presence.sitemap_check", lambda ctx: {})
    ctx = context(tmp_path)
    module = ModuleResult(name="security")
    module.add_evidence(ctx.target, "Synthetic", {})
    registry = {"security": SimpleNamespace(categories={"security"}, run=lambda ctx: module)}
    if state != "not-selected":
        result = ModuleResult(
            name="siteone",
            status="ok" if state == "started" else state,
            collection_started=state == "started",
        )
        registry["siteone"] = SimpleNamespace(categories={"security"}, run=lambda ctx: result)
    monkeypatch.setattr(runner, "REGISTRY", registry)
    data = runner.run(ctx, no_ai=True, formats={"md"})
    assert data["crawl_assets_excluded"] is (state == "started")
    assert json.loads((ctx.run_dir / "results.json").read_text())["crawl_assets_excluded"] is (
        state == "started"
    )


def test_siteone_launch_error_does_not_claim_assets_excluded(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda url: {"resolved": url})

    def cannot_start(*args, **kwargs):
        raise OSError("Synthetic process launch failed")

    monkeypatch.setattr(siteone.subprocess, "run", cannot_start)
    result = siteone.run(ctx)
    assert result.status == "error" and not result.collection_started


def test_customer_report_renders_recorded_figure_exactly(tmp_path):
    from ams_assess import store
    from ams_assess.discovery import Notes
    from ams_assess.report import render_markdown

    notes = Notes.model_validate(
        {"sections": {"enquiries_communication": ["Approximately 20 enquiries per week"]}}
    )
    saved, _ = store.add_discovery(tmp_path, "synthetic", "Synthetic", notes)
    data, evidence, discovery = store.report_data(saved)
    text = render_markdown(data, evidence, discovery)
    assert (
        "Customer reports approximately 20 enquiries per week; not independently verified." in text
    )
    assert "Evidence: ev-discovery-r1-001" in text


def test_customer_figure_modifiers_must_match_recorded_context():
    evidence = customer_evidence("Approximately 20 new enquiries per week")
    assert not ai.unsupported_claim("Approximately 20 new enquiries per week", [evidence])
    assert ai.unsupported_claim("Approximately 20 more enquiries per week", [evidence])
    assert ai.unsupported_claim("Approximately 20 enquiries per week", [evidence])
    rendered = figures.render("Approximately 20 new enquiries per week", [evidence])
    assert rendered.startswith("Customer reports approximately 20 new enquiries per week;")
