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
        "Form fixes will add approximately 20 enquiries per week",
        "Fixing the forms will add approximately 20 enquiries per week",
        "AMS could win approximately 20 enquiries per week for you",
        "You would get approximately 20 enquiries per week",
        "Approximately 20 enquiries per week can be brought in by fixing forms",
        "The forms should bring approximately 20 enquiries per week",
        "By fixing the forms, approximately 20 enquiries per week are possible",
        "Fixing forms leads to approximately 20 enquiries per week",
        "You get approximately 20 enquiries per week by fixing the forms",
        "Forms drive approximately 20 enquiries per week",
        "Approximately 20 enquiries per week will follow the changes",
        "Approximately 20 enquiries per week are expected after the redesign",
        "Approximately 20 enquiries per week are recoverable with better forms",
        "Expect approximately 20 enquiries per week once the forms are fixed",
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
    assert result[0].detail.count("aggregate supporting evidence") == 1


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


@pytest.mark.parametrize("existing", [False, True])
def test_ai_finding_titles_reject_promises_and_render_customer_figures(existing):
    evidence = customer_evidence()
    base = Finding(
        id="customer-load",
        title="Customer reports approximately 20 enquiries per week",
        category="cro_ux",
        evidence_ids=[evidence.id],
        detail="Review the enquiry workflow.",
        recommendation="Review the forms.",
    )
    bad = base.model_copy(
        update={"title": "Form fixes will add approximately 20 enquiries per week"}
    )
    _, findings, warnings = ai.validate_analysis(
        {"findings": [bad.model_dump()]},
        [evidence],
        {"cro_ux"},
        [base] if existing else [],
    )
    assert any("unsupported quantity or promised outcome" in warning for warning in warnings)
    assert not any(f.title == bad.title for f in findings)
    if not existing:
        _, findings, _ = ai.validate_analysis(
            {"findings": [base.model_dump()]},
            [evidence],
            {"cro_ux"},
            [],
        )
        assert findings[0].title == (
            "Customer reports approximately 20 enquiries per week; not independently verified. "
            "(ev-discovery-001)"
        )


@pytest.mark.parametrize(
    "claim",
    [
        "Forms could add 20 enquiries per week",
        "20 enquiries per week will result from this work",
        "By fixing forms, 20 enquiries per week are possible",
        "Form changes lead to 20 enquiries per week",
    ],
)
def test_measured_history_does_not_authorise_future_or_causal_figures(claim):
    evidence = Evidence(
        id="ev-metrics-001",
        source="metrics",
        url="https://example.test/",
        description="Historic sample",
        data={
            "measurements": [
                {"value": 20, "unit": "enquiries", "period": "week"},
            ]
        },
    )
    assert ai.unsupported_claim(claim, [evidence])


def test_figure_rendering_preserves_other_valid_sentences_and_their_citations():
    evidence = customer_evidence()
    before = "The team reports that forms need review. (ev-discovery-001)"
    after = "Staff report that the inbox is shared. (ev-discovery-001)"
    text = (
        before
        + " Customer reports approximately 20 enquiries per week. (ev-discovery-001) "
        + after
    )
    rendered = figures.render(text, [evidence])
    assert rendered.startswith(before) and rendered.endswith(after)
    assert (
        rendered.count(
            "Customer reports approximately 20 enquiries per week; not independently verified."
        )
        == 1
    )
    validated, _, warnings = ai.validate_analysis(
        {"executive_summary": text}, [evidence], set(), []
    )
    assert (
        not warnings
        and before in validated["executive_summary"]
        and after in validated["executive_summary"]
    )


@pytest.mark.parametrize("provenance", ["customer_statement", "technical_evidence"])
@pytest.mark.parametrize(
    "followup, dropped",
    [
        ("The redesign will double that.", True),
        ("That is expected after the redesign.", True),
        ("That is recoverable with better forms.", True),
        ("Expect that once the forms are fixed.", True),
        ("That could happen after the redesign.", True),
        ("The redesign will double enquiries.", True),
        ("You should review the contact form.", False),
        ("Check if the form submits.", False),
        ("Evidence from tracking shows form errors.", False),
    ],
)
def test_figure_fields_drop_separate_promise_sentences_and_keep_supported_prose(
    provenance, followup, dropped
):
    evidence = customer_evidence()
    if provenance == "technical_evidence":
        evidence = evidence.model_copy(
            update={
                "source": "metrics",
                "provenance": provenance,
                "data": {
                    "measurements": [
                        {
                            "value": 20,
                            "unit": "enquiries",
                            "period": "week",
                            "qualifier": "approximately",
                        }
                    ]
                },
            }
        )
    observed = f"Approximately 20 enquiries per week. ({evidence.id})"
    other = f"The team reports that forms need review. ({evidence.id})"
    # Check both sides of the figure; unrelated recommendations must survive.
    text = f"{followup} {observed} {followup} {other}"
    assert ai.unsupported_claim(text, [evidence]) is dropped
    rendered = figures.render(text, [evidence])
    assert (followup in rendered) is (not dropped) and other in rendered
    assert "approximately 20 enquiries per week" in rendered
    fields = ("title", "detail", "recommendation", "likely_cause", "business_impact")
    finding = Finding(
        id="enquiry-review",
        category="cro_ux",
        evidence_ids=[evidence.id],
        title=text,
        detail=text,
        recommendation=text,
        likely_cause=text,
        business_impact=text,
    )
    validated, findings, _ = ai.validate_analysis(
        {
            "executive_summary": text,
            "sections": {"cro_ux": text},
            "findings": [finding.model_dump()],
        },
        [evidence],
        {"cro_ux"},
        [],
    )
    assert len(findings) == 1
    for output in [
        validated["executive_summary"],
        validated["sections"]["cro_ux"],
        *(getattr(findings[0], field) for field in fields),
    ]:
        assert (followup in output) is (not dropped) and other in output
        assert "approximately 20 enquiries per week" in output
        if provenance == "customer_statement":
            assert "not independently verified" in output


def test_invalid_narrative_citations_cannot_be_hidden_in_removed_promises():
    value = (
        "Approximately 20 enquiries per week. (ev-discovery-001) "
        "The redesign will double that. (ev-unknown-999)"
    )
    validated, _, warnings = ai.validate_analysis(
        {"executive_summary": value, "sections": {"cro_ux": value}},
        [customer_evidence()],
        {"cro_ux"},
        [],
    )
    assert not validated["executive_summary"] and not validated["sections"]
    assert len(warnings) == 2 and all("uncited or invalid" in warning for warning in warnings)


def test_siteone_diagnostics_preserve_prose_question_marks(tmp_path):
    prose = "Did collection finish? Why? Review the response."
    result = siteone.diagnostic(
        prose + " URL: example.test/path?token=synthetic-secret", context(tmp_path)
    )
    assert result.startswith(prose) and result.endswith("URL: example.test/path")
    assert "synthetic-secret" not in result


def test_coverage_failure_retains_completed_run_and_records_unavailable(tmp_path, monkeypatch):
    from ams_assess import store

    ctx = context(tmp_path)
    ctx.store_root = tmp_path / "customers"
    module = ModuleResult(name="security")
    eid = module.add_evidence(ctx.target, "Synthetic check", {})
    module.finding(
        "header-review:server", "Review server header", "security", [eid], "Observed", "Review"
    )
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {"security": SimpleNamespace(categories={"security"}, run=lambda ctx: module)},
    )

    def fail(ctx):
        raise ValueError("synthetic-secret")

    monkeypatch.setattr(runner.coverage, "collect", fail)
    result = runner.run(ctx, no_ai=True, formats={"md"})
    assert result["status"] == "partial" and result["findings"]
    assert result["website_coverage"]["status"] == "unavailable"
    assert "ValueError" in result["website_coverage"]["reason"]
    report = (ctx.run_dir / "report.md").read_text()
    assert "Website coverage unavailable" in report and "Review server header" in report
    assert "synthetic-secret" not in report + json.dumps(result)
    saved = store.load(ctx.store_root, ctx.customer_id)
    assert saved and saved["runs"][-1]["website_coverage"]["status"] == "unavailable"
