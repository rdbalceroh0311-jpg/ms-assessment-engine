import json
import os
import re
import stat
from types import SimpleNamespace
from typing import Any

import pytest

from ams_assess import cli, runner, store
from ams_assess.ai import validate_analysis
from ams_assess.consolidate import consolidate
from ams_assess.models import (
    AMS_SERVICES,
    Context,
    Evidence,
    Finding,
    ModuleResult,
    create_run_dir,
)

EVIDENCE_ID = re.compile(r"ev-[a-z_]+(?:-r\d+)?-\d+")


def fake_module(severity="medium", confidence="confirmed", name="security", extra=False):
    def run(ctx):
        result = ModuleResult(name=name)
        first = result.add_evidence("https://example.test/", "Header observed", {"h": 1})
        result.finding(
            "headers",
            "Missing header",
            "security",
            [first],
            "Header absent",
            "Add it",
            severity=severity,
            confidence=confidence,
        )
        if extra:
            second = result.add_evidence("https://example.test/b", "Other", {})
            result.finding("cookies", "Cookie flags", "security", [second], "d", "r")
        return result

    return SimpleNamespace(categories={"security"}, run=run)


def assess(tmp_path, monkeypatch, customer="acme", registry=None, **module):
    monkeypatch.setattr(runner, "REGISTRY", registry or {"security": fake_module(**module)})
    root = tmp_path / "data/customers"
    ctx = Context(
        customer_id=customer,
        customer_name=customer.title(),
        target="https://example.com/",
        categories={"security"},
        connections=set(),
        config={},
        run_dir=create_run_dir(root, customer),
        tools_dir=tmp_path,
        store_root=root,
    )
    return root, runner.run(ctx, no_ai=True, formats={"md"}), ctx


def notes_file(tmp_path, **changes):
    notes = {
        "sections": {
            "enquiries_communication": [
                {"statement": "Enquiries arrive by phone and email", "frequency": "daily"}
            ],
            "sales_quoting": ["Quotes are built by hand in spreadsheets"],
            "systems_in_use": [
                {"statement": "Xero and a shared Google Calendar", "systems": ["Xero"]}
            ],
            "manual_duplicated_tasks": [
                {"statement": "Jobs are re-keyed into Xero", "owner": "Office manager"}
            ],
        },
        "findings": [
            {
                "title": "Quote automation",
                "kind": "opportunity",
                "detail": "Quotes are built manually",
                "recommendation": "Automate quote generation",
                "ams_service": "AI & Automation",
                "cites": ["sales_quoting:0", "manual_duplicated_tasks"],
            }
        ],
        **changes,
    }
    path = tmp_path / "notes.json"
    path.write_text(json.dumps(notes))
    return path


def must_load(root, customer):
    loaded = store.load(root, customer)
    assert loaded is not None
    return loaded


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    return tmp_path / "data/customers"


def test_evidence_ids_are_namespaced_per_run_without_collisions(tmp_path, monkeypatch):
    assess(tmp_path, monkeypatch)
    root, _, _ = assess(tmp_path, monkeypatch, extra=True)
    saved = must_load(root, "acme")
    ids = [e["id"] for e in saved["evidence"]]
    assert len(ids) == len(set(ids)) == 3
    assert {"ev-security-r1-001", "ev-security-r2-001", "ev-security-r2-002"} == set(ids)
    known = set(ids)
    for finding in saved["findings"]:
        assert finding["evidence_ids"] and set(finding["evidence_ids"]) <= known
    # The same finding seen in both runs is one finding citing both runs' evidence.
    headers = next(f for f in saved["findings"] if f["check_key"] == "headers")
    assert headers["evidence_ids"] == ["ev-security-r1-001", "ev-security-r2-001"]
    assert [r["run_id"] for r in saved["runs"]] == ["r1", "r2"]


def test_remap_is_complete_across_every_citing_structure(tmp_path):
    root = tmp_path
    evidence = [
        Evidence(id="ev-security-001", source="security", url="u", description="d", data={}),
        Evidence(
            id="ev-security-002",
            source="security",
            url="u",
            description="see ev-security-001",
            data={"related": ["ev-security-001"]},
        ),
    ]
    finding = Finding(
        id="security-001",
        title="t",
        category="security",
        evidence_ids=["ev-security-001", "ev-security-002"],
        detail="d",
        recommendation="r",
        check_key="k",
    )
    data = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "status": "complete",
        "target": "https://example.com/",
        "selected_categories": ["security"],
        "assessed_categories": ["security"],
        "ai": {
            "status": "ok",
            "executive_summary": "Summary cites ev-security-001.",
            "sections": {"security": "Section cites ev-security-002."},
            "recommendations": ["security-001"],
            "roadmap": {"0-30 days": ["security-001"]},
        },
        "modules": [],
        "pages": [],
        "max_pages": 1,
        "limitations": [],
        "future": [],
        "report_files": [],
        "findings": [finding.model_dump()],
        "digital_presence": {"signals": [{"evidence_ids": ["ev-security-002"]}]},
        "domain_email_health": {"rows": [{"checks": [{"evidence_ids": ["ev-security-001"]}]}]},
    }
    store.merge_run(root, "acme", "Acme", data, evidence, "run-a", ["public"])
    saved = store.merge_run(root, "acme", "Acme", data, evidence, "run-b", ["public"])
    known = {e["id"] for e in saved["evidence"]}
    assert len(known) == 4
    for run in saved["runs"]:
        text = json.dumps(run)
        assert set(EVIDENCE_ID.findall(text)) <= known
        assert not re.findall(r"ev-security-\d+", text)
    assert set(EVIDENCE_ID.findall(json.dumps(saved["evidence"]))) <= known
    # Merging the same run directory twice is a no-op, not a duplicate.
    again = store.merge_run(root, "acme", "Acme", data, evidence, "run-b", ["public"])
    assert len(again["runs"]) == 2


def test_run_writes_run_dir_and_merges_into_store(tmp_path, monkeypatch):
    root, data, ctx = assess(tmp_path, monkeypatch)
    for name in ("evidence.json", "findings.json", "results.json", "report.md"):
        assert (ctx.run_dir / name).is_file()
    assert json.loads((ctx.run_dir / "evidence.json").read_text())[0]["id"] == "ev-security-001"
    saved = must_load(root, "acme")
    assert saved["schema_version"] == 2 and saved["customer_name"] == "Acme"
    assert saved["stages_assessed"] == ["public"]
    run = saved["runs"][0]
    assert run["run_dir"] == ctx.run_dir.name and run["kind"] == "assessment"
    assert run["ai"]["status"] == "skipped" and run["summary"]["evidence"] == 1
    assert saved["discovery"] == {} and saved["report_files"] == {}


def test_store_merge_worst_severity_and_confidence_degradation(tmp_path, monkeypatch):
    assess(tmp_path, monkeypatch, severity="low")
    assess(tmp_path, monkeypatch, severity="high", confidence="needs_verification")
    root, _, _ = assess(tmp_path, monkeypatch, severity="medium")
    finding = next(f for f in must_load(root, "acme")["findings"] if f["check_key"] == "headers")
    assert finding["severity"] == "high" and finding["priority"] == "P1"
    assert finding["confidence"] == "needs_verification"
    assert len(finding["evidence_ids"]) == 3


def test_finding_id_collision_between_different_checks_is_renamed(tmp_path, monkeypatch):
    assess(tmp_path, monkeypatch)

    def other(ctx):
        result = ModuleResult(name="security")
        eid = result.add_evidence("https://example.test/", "Different", {})
        result.finding("tls", "Weak TLS", "security", [eid], "d", "r")
        return result

    registry = {"security": SimpleNamespace(categories={"security"}, run=other)}
    root, data, _ = assess(tmp_path, monkeypatch, registry=registry)
    assert data["findings"][0]["id"] == "security-001"  # the run's own output is untouched
    findings = must_load(root, "acme")["findings"]
    assert len({f["id"] for f in findings}) == len(findings) == 2
    assert {f["check_key"] for f in findings} == {"headers", "tls"}


def test_store_permissions_and_atomic_write(tmp_path, monkeypatch):
    root, _, _ = assess(tmp_path, monkeypatch)
    path = store.store_file(root, "acme")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    before = path.read_text()

    def boom(*args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save(root, "acme", {**json.loads(before), "customer_name": "Changed"})
    monkeypatch.undo()
    assert json.loads(path.read_text())["customer_name"] == "Acme"
    assert not list(path.parent.glob(".assessment-*"))


def test_store_failure_never_fails_the_run(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("secret-token-123")

    monkeypatch.setattr(store, "merge_run", boom)
    _, data, ctx = assess(tmp_path, monkeypatch)
    assert data["status"] == "complete" and (ctx.run_dir / "results.json").is_file()
    out = capsys.readouterr().out
    assert "merge failed" in out and "secret-token-123" not in out


def test_provenance_is_worst_of_cited_evidence_and_never_confirmed():
    technical = Evidence(id="ev-a-001", source="a", url="u", description="d", data={})
    statement = technical.model_copy(
        update={"id": "ev-discovery-r1-001", "provenance": "customer_statement"}
    )
    assumption = technical.model_copy(update={"id": "ev-x-001", "provenance": "assumption"})
    base: dict[str, Any] = {
        "title": "t",
        "category": "seo",
        "detail": "d",
        "recommendation": "r",
    }
    mixed = Finding(id="f-1", evidence_ids=[technical.id, statement.id], check_key="k", **base)
    assert mixed.provenance == "technical_evidence" and mixed.confidence == "confirmed"
    [result] = consolidate([mixed], [technical, statement])
    assert result.provenance == "customer_statement"
    assert result.confidence == "needs_verification"
    both = Finding(id="f-2", evidence_ids=[statement.id, assumption.id], check_key="j", **base)
    [result] = consolidate([both], [statement, assumption])
    assert result.provenance == "assumption"
    # A claimed weaker provenance is never upgraded back by consolidation.
    claimed = Finding(
        id="f-3", evidence_ids=[technical.id], check_key="m", provenance="assumption", **base
    )
    assert consolidate([claimed], [technical])[0].provenance == "assumption"
    # Merging a technical and a statement-backed duplicate takes the worse one.
    dup_a = Finding(id="f-4", evidence_ids=[technical.id], check_key="same", **base)
    dup_b = Finding(id="f-5", evidence_ids=[statement.id], check_key="same", **base)
    [result] = consolidate([dup_a, dup_b], [technical, statement])
    assert result.provenance == "customer_statement"


def test_ai_cannot_upgrade_provenance():
    technical = Evidence(id="ev-a-001", source="a", url="u", description="d", data={})
    statement = Evidence(
        id="ev-discovery-r1-001",
        source="discovery",
        url="u",
        description="d",
        data={},
        provenance="customer_statement",
    )
    original = Finding(
        id="disc-1",
        title="t",
        category="seo",
        evidence_ids=[statement.id],
        detail="d",
        recommendation="r",
        check_key="disc",
        provenance="customer_statement",
        confidence="needs_verification",
    )
    ai_new = {
        "id": "ai-1",
        "title": "AI claim",
        "category": "seo",
        "evidence_ids": [technical.id, statement.id],
        "detail": "d",
        "recommendation": "r",
        "provenance": "technical_evidence",
        "confidence": "confirmed",
    }
    ai_enrich = {**original.model_dump(), "provenance": "technical_evidence", "priority": "P1"}
    _, findings, _ = validate_analysis(
        {"findings": [ai_new, ai_enrich]}, [technical, statement], {"seo"}, [original]
    )
    by_id = {f.id: f for f in findings}
    assert by_id["disc-1"].provenance == "customer_statement" and by_id["disc-1"].priority == "P1"
    assert by_id["ai-1"].provenance == "customer_statement"
    assert by_id["ai-1"].confidence == "needs_verification"


def test_discovery_ingestion_exits_zero_and_findings_cite_statement_evidence(home, tmp_path):
    notes = notes_file(tmp_path)
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    saved = must_load(home, "acme")
    assert saved["stages_assessed"] == ["discovery"]
    run = saved["runs"][0]
    assert run["kind"] == "discovery" and run["status"] == "complete"
    assert run["ai"]["status"] == "skipped"
    evidence = {e["id"]: e for e in saved["evidence"]}
    assert len(evidence) == 4
    assert all(e["provenance"] == "customer_statement" for e in evidence.values())
    [finding] = saved["findings"]
    assert finding["category"] == "discovery" and finding["ams_service"] == "AI & Automation"
    assert finding["provenance"] == "customer_statement"
    assert finding["confidence"] == "needs_verification"
    assert len(finding["evidence_ids"]) == 2 and set(finding["evidence_ids"]) <= evidence.keys()
    texts = [evidence[i]["data"]["statement"] for i in finding["evidence_ids"]]
    assert "Quotes are built by hand in spreadsheets" in texts
    assert saved["discovery"]["sections"]["enquiries_communication"][0]["frequency"] == "daily"
    # Re-ingesting identical notes neither duplicates evidence nor findings.
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    again = must_load(home, "acme")
    assert len(again["evidence"]) == 4 and len(again["findings"]) == 1


@pytest.mark.parametrize("service", AMS_SERVICES)
def test_discovery_findings_can_use_all_four_ams_services(home, tmp_path, service):
    finding = {
        "title": "Item",
        "detail": "d",
        "recommendation": "r",
        "ams_service": service,
        "cites": ["sales_quoting"],
    }
    notes = notes_file(tmp_path, findings=[finding])
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    assert must_load(home, "acme")["findings"][0]["ams_service"] == service


@pytest.mark.parametrize(
    "changes",
    [
        {
            "findings": [
                {
                    "title": "t",
                    "detail": "d",
                    "recommendation": "r",
                    "ams_service": "Website improvement",
                    "cites": ["sales_quoting"],
                }
            ]
        },
        {
            "findings": [
                {
                    "title": "t",
                    "detail": "d",
                    "recommendation": "r",
                    "ams_service": "AI & Automation",
                    "cites": ["sales_quoting:9"],
                }
            ]
        },
        {
            "findings": [
                {
                    "title": "t",
                    "detail": "d",
                    "recommendation": "r",
                    "ams_service": "AI & Automation",
                    "cites": [],
                }
            ]
        },
        {"sections": {"astrology": ["x"]}},
        {"sections": {}},
    ],
)
def test_invalid_discovery_notes_are_rejected_without_writing(home, tmp_path, changes):
    notes = notes_file(tmp_path, **changes)
    with pytest.raises(SystemExit) as exc:
        cli.main(["discover", "--customer", "acme", "--notes", str(notes)])
    assert exc.value.code == 2
    assert store.load(home, "acme") is None


def test_report_from_store_stage1_only_has_no_discovery_section(home, tmp_path, monkeypatch):
    assess(tmp_path, monkeypatch)
    assert cli.main(["report", "--customer", "acme", "--formats", "md,html"]) == 0
    report = (home / "acme/report.md").read_text()
    assert "Business discovery" not in report and "Customer statement" not in report
    assert "Missing header" in report
    saved = must_load(home, "acme")
    assert saved["report_files"] == {"md": "report.md", "html": "report.html"}


def test_report_from_store_discovery_only_renders_with_labels(home, tmp_path):
    cli.main(["discover", "--customer", "acme", "--notes", str(notes_file(tmp_path))])
    assert cli.main(["report", "--customer", "acme", "--formats", "md,html,pdf"]) == 0
    report = (home / "acme/report.md").read_text()
    assert "## Business discovery: customer-provided information" in report
    assert "Quotes are built by hand in spreadsheets" in report
    assert "Customer statement, not independently verified" in report
    assert "No website assessed" in report
    assert "customer statement" in report  # evidence index label
    assert "**Suggested AMS service:** AI &amp; Automation" in report
    assert "Quote automation" in report.split("## 8. Prioritised recommendations")[1]
    assert (home / "acme/report.pdf").stat().st_size > 1000
    assert set(re.findall(r"ev-[a-z_]+-r\d+-\d+", report)) <= {
        e["id"] for e in must_load(home, "acme")["evidence"]
    }


@pytest.mark.parametrize("order", ["13", "31", "132", "123"])
def test_report_renders_for_every_stage_sequence(home, tmp_path, monkeypatch, order):
    for step in order:
        if step == "3":
            cli.main(["discover", "--customer", "acme", "--notes", str(notes_file(tmp_path))])
        else:
            assess(tmp_path, monkeypatch, extra=step == "2")
    assert cli.main(["report", "--customer", "acme", "--formats", "md,html"]) == 0
    report = (home / "acme/report.md").read_text()
    assert "Business discovery" in report and "Missing header" in report
    saved = must_load(home, "acme")
    known = {e["id"] for e in saved["evidence"]}
    assert set(re.findall(r"ev-[a-z_]+-r\d+-\d+", report)) <= known
    assert saved["stages_assessed"] == ["public", "discovery"]
    headings = re.findall(r"^## (.+)$", report, re.M)
    assert headings.index("Business discovery: customer-provided information") < headings.index(
        "8. Prioritised recommendations"
    )


def test_report_cli_modes_and_validation(home, tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        cli.main(["report"])
    with pytest.raises(SystemExit):
        cli.main(["report", str(tmp_path), "--customer", "acme"])
    with pytest.raises(SystemExit):
        cli.main(["report", "--customer", "nobody"])
    _, _, ctx = assess(tmp_path, monkeypatch)
    # Positional run-dir mode keeps working and keeps the run's own IDs.
    assert cli.main(["report", str(ctx.run_dir), "--formats", "md"]) == 0
    assert "ev-security-001" in (ctx.run_dir / "report.md").read_text()


def test_customer_stores_are_isolated(tmp_path, monkeypatch, home):
    assess(tmp_path, monkeypatch, customer="acme")
    cli.main(["discover", "--customer", "beta", "--notes", str(notes_file(tmp_path))])
    acme, beta = must_load(home, "acme"), must_load(home, "beta")
    assert acme["customer_id"] == "acme" and beta["customer_id"] == "beta"
    assert not any(e["source"] == "discovery" for e in acme["evidence"])
    assert not any(e["source"] == "security" for e in beta["evidence"])
    assert acme["discovery"] == {} and beta["stages_assessed"] == ["discovery"]
    # A store file copied into another customer's directory is refused, not read.
    (home / "gamma").mkdir()
    (home / "gamma/assessment.json").write_text((home / "acme/assessment.json").read_text())
    with pytest.raises(ValueError, match="mismatched"):
        store.load(home, "gamma")
    cli.main(["report", "--customer", "beta", "--formats", "md"])
    assert not (home / "acme/report.md").exists()
    assert "Missing header" not in (home / "beta/report.md").read_text()


def test_store_rejects_symlinked_customer_and_bad_slugs(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    for customer in ("linked", "../x", "UPPER", ""):
        with pytest.raises(ValueError):
            store.load(tmp_path, customer)
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme/assessment.json").symlink_to(real / "x.json")
    with pytest.raises(ValueError, match="symlink"):
        store.load(tmp_path, "acme")


def test_discovery_findings_survive_the_assessed_category_filter(tmp_path, monkeypatch):
    original = runner.consolidate

    def with_discovery(findings, evidence):
        return original(findings, evidence)

    monkeypatch.setattr(runner, "consolidate", with_discovery)
    monkeypatch.setattr(
        runner,
        "analyse",
        lambda ctx, findings, evidence, no_ai: (
            {"status": "skipped", "provider": None, "reason": "x"},
            [
                *findings,
                Finding(
                    id="discovery-x",
                    title="t",
                    category="discovery",
                    evidence_ids=[evidence[0].id],
                    detail="d",
                    recommendation="r",
                    check_key="x",
                ),
            ],
        ),
    )
    _, data, _ = assess(tmp_path, monkeypatch)
    assert "discovery-x" in {f["id"] for f in data["findings"]}
