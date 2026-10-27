"""Synthetic store history must survive report rendering regardless of provenance."""

import re

import pytest
from pypdf import PdfReader

from ams_assess import store
from ams_assess.cli import customer_summary
from ams_assess.models import Evidence, Finding
from ams_assess.report import render_customer, render_markdown


def historical_finding(provenance, state="not_rechecked"):
    evidence = Evidence(
        id=f"ev-observation-{provenance}",
        source="synthetic",
        url="https://example.test/",
        description="Synthetic historical observation",
        data={},
        provenance=provenance,
    )
    finding = Finding(
        id=f"history-{provenance}",
        title=f"Historical {provenance.replace('_', ' ')} finding",
        category="discovery" if provenance == "customer_statement" else "presence",
        detail="Synthetic historical finding",
        recommendation="STALE_ACTION_MUST_NOT_BE_RECOMMENDED",
        evidence_ids=[evidence.id],
        provenance=provenance,
        lifecycle_status=state,
        last_observed="2026-10-01",
        resolution=(
            {"date": "2026-10-02", "run_id": "r2", "evidence_ids": [evidence.id]}
            if state == "resolved"
            else None
        ),
    )
    return finding, evidence


@pytest.mark.parametrize(
    "provenance, basis",
    [
        ("technical_evidence", "Confirmed (technical evidence)"),
        ("customer_statement", "Reported by customer"),
        ("manual_verification", "Manually verified"),
        ("assumption", "Inferred (heuristic or AI, needs verification)"),
    ],
)
@pytest.mark.parametrize(
    "state, label",
    [
        ("not_rechecked", "Previously observed — not rechecked"),
        ("resolved", "Resolved"),
        ("needs_review", "Needs review"),
        ("not_rechecked", "Needs review"),
        ("superseded", "Superseded"),
    ],
)
def test_history_preserves_every_provenance(tmp_path, provenance, basis, state, label):
    finding, evidence = historical_finding(
        provenance, "not_rechecked" if state == "superseded" else state
    )
    saved = store.new_store("synthetic", "Synthetic history")
    saved["evidence"] = [evidence.model_dump()]
    if state == "superseded":
        saved["finding_history"] = [
            {**finding.model_dump(), "status": "superseded", "reason": "Newer observation"}
        ]
    else:
        saved["findings"] = [finding.model_dump()]
    if label == "Needs review":
        saved["needs_review"] = [
            {
                "finding_id": finding.id,
                "title": finding.title,
                "reason": "conflicting explicit check outcomes need review",
                "evidence_ids": finding.evidence_ids,
            }
        ]
    current = finding.model_copy(
        update={
            "id": "current-control",
            "title": "Current observation",
            "category": "presence",
            "lifecycle_status": "current",
            "resolution": None,
            "recommendation": "Review the current observation",
        }
    )
    saved["findings"].append(current.model_dump())
    store.save(tmp_path, "synthetic", saved)
    loaded = store.load(tmp_path, "synthetic")
    assert loaded is not None
    data, evidence_rows, discovery = store.report_data(loaded)
    # Stale AI ordering must not turn history back into recommendations or roadmap actions.
    data["ai"]["recommendations"] = [finding.id, current.id]
    data["ai"]["roadmap"] = {"0-30 days": [finding.id, current.id]}
    text = render_markdown(data, evidence_rows, discovery)
    if label == "Needs review":
        assert text.count(finding.id) == 1
    rows = [line for line in text.splitlines() if finding.id in line]
    assert len(rows) == 1
    row = rows[0]
    for value in (finding.title, label, evidence.id):
        assert value in row
    if label == "Needs review":
        review_section = text.split("## 10.", 1)[1].split("## Appendix:", 1)[0]
        assert finding.id in review_section
        assert saved["needs_review"][0]["reason"] in row
    else:
        assert basis in row and finding.last_observed in row
    if state == "resolved":
        assert "2026-10-02 / r2" in row
    assert finding.recommendation not in text
    roadmap = text.split("## 9.", 1)[1].split("\n## ", 1)[0]
    assert current.id in roadmap and current.recommendation in roadmap
    assert finding.id not in roadmap and finding.recommendation not in roadmap


def test_not_rechecked_report_counts_match_cli_in_all_formats(tmp_path):
    saved = store.new_store("synthetic", "Synthetic report parity")
    pairs = [historical_finding(p) for p in ("customer_statement", "technical_evidence")]
    pairs[0][0].title = "Priority platforms still need presence verification"
    pairs[0][0].category = "presence"
    for finding, evidence in pairs:
        saved["findings"].append(finding.model_dump())
        saved["evidence"].append(evidence.model_dump())
    store.save(tmp_path, "synthetic", saved)

    summary = customer_summary(tmp_path, "synthetic")[0]
    assert summary == (
        "Customer findings (all runs and discovery): 0 current, 0 resolved, "
        "2 previously observed (not rechecked), 0 needs review"
    )
    match = re.search(r"(\d+) previously observed", summary)
    assert match is not None
    expected = int(match[1])
    render_customer(tmp_path, "synthetic", {"md", "html", "pdf"})
    directory = tmp_path / "synthetic"
    texts = {
        "md": (directory / "report.md").read_text(),
        "html": (directory / "report.html").read_text(),
        "pdf": " ".join(page.extract_text() for page in PdfReader(directory / "report.pdf").pages),
    }
    for format_, content in texts.items():
        text = " ".join(content.split())
        assert text.count("Previously observed — not rechecked") == expected, format_
        for finding, evidence in pairs:
            assert text.count(finding.id) == 1, format_
            for value in (finding.title, finding.basis, finding.last_observed, evidence.id):
                assert value in text, format_
            assert finding.recommendation not in text, format_
