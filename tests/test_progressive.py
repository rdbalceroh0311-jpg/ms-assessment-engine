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


def assess(tmp_path, monkeypatch, customer="acme", registry=None, connections=(), **module):
    monkeypatch.setattr(runner, "REGISTRY", registry or {"security": fake_module(**module)})
    root = tmp_path / "data/customers"
    ctx = Context(
        customer_id=customer,
        customer_name=customer.title(),
        target="https://example.com/",
        categories={"security"},
        connections=set(connections),
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
    assert data["store_status"] == "failed"
    assert "store_status" not in json.loads((ctx.run_dir / "results.json").read_text())
    out = capsys.readouterr().out
    assert "merge failed" in out and "secret-token-123" not in out


def test_cli_exits_3_when_store_merge_fails(home, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "config", lambda: {})
    monkeypatch.setattr(runner, "REGISTRY", {"security": fake_module()})
    monkeypatch.setattr(store, "merge_run", lambda *a, **k: 1 / 0)
    args = ["run", "--customer", "acme", "--target", "example.com", "--categories", "security"]
    assert cli.main([*args, "--no-ai", "--formats", "md"]) == 3


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


def gsc_module():
    def run(ctx):
        result = ModuleResult(name="gsc")
        eid = result.add_evidence("https://example.test/", "Search Console clicks", {"clicks": 5})
        result.finding("gsc-clicks", "Low clicks", "security", [eid], "d", "r", severity="low")
        return result

    return SimpleNamespace(categories={"security"}, run=run)


@pytest.mark.parametrize("order", ["123", "132", "213"])
def test_connected_stage_sequences_render(home, tmp_path, monkeypatch, order):
    registry = {"security": fake_module(), "gsc": gsc_module()}
    for step in order:
        if step == "3":
            cli.main(["discover", "--customer", "acme", "--notes", str(notes_file(tmp_path))])
        elif step == "2":
            assess(tmp_path, monkeypatch, registry=registry, connections={"gsc"})
        else:
            assess(tmp_path, monkeypatch, registry={"security": fake_module()})
    assert cli.main(["report", "--customer", "acme", "--formats", "md"]) == 0
    saved = must_load(home, "acme")
    assert saved["stages_assessed"] == ["public", "connected", "discovery"]
    report = (home / "acme/report.md").read_text()
    assert "Low clicks" in report and "Business discovery" in report
    known = {e["id"] for e in saved["evidence"]}
    assert len(known) == len(saved["evidence"])
    assert set(re.findall(r"ev-[a-z_]+-r\d+-\d+", report)) <= known


def test_report_refuses_symlinked_outputs_and_never_overwrites_other_customers(
    home, tmp_path, monkeypatch
):
    assess(tmp_path, monkeypatch, customer="acme")
    cli.main(["discover", "--customer", "beta", "--notes", str(notes_file(tmp_path))])
    beta_store = (home / "beta/assessment.json").read_text()
    for ext in ("md", "html", "pdf"):
        (home / f"acme/report.{ext}").symlink_to(home / "beta/assessment.json")
        with pytest.raises(SystemExit):
            cli.main(["report", "--customer", "acme", "--formats", ext])
        (home / f"acme/report.{ext}").unlink()
    assert (home / "beta/assessment.json").read_text() == beta_store
    # Run-dir mode enforces the same rule.
    _, _, ctx = assess(tmp_path, monkeypatch, customer="acme")
    (ctx.run_dir / "report.html").symlink_to(home / "beta/assessment.json")
    with pytest.raises(SystemExit):
        cli.main(["report", str(ctx.run_dir), "--formats", "html"])
    assert (home / "beta/assessment.json").read_text() == beta_store


def test_collision_rename_stays_within_id_limit_and_store_stays_valid(tmp_path):
    root = tmp_path
    long_id = "a" * 64
    evidence = [
        Evidence(id="ev-security-001", source="security", url="u", description="d", data={})
    ]

    def data_for(category, fid, key):
        finding = Finding(
            id=fid,
            title="t",
            category=category,
            evidence_ids=["ev-security-001"],
            detail="d",
            recommendation="r",
            check_key=key,
        )
        return {
            "created_at": "2026-01-01T00:00:00+00:00",
            "status": "complete",
            "target": "https://example.com/",
            "selected_categories": [category],
            "assessed_categories": [category],
            "ai": {"status": "skipped"},
            "modules": [],
            "pages": [],
            "max_pages": 1,
            "limitations": [],
            "future": [],
            "report_files": [],
            "findings": [finding.model_dump()],
        }

    store.merge_run(root, "acme", "Acme", data_for("seo", long_id, "k0"), evidence, "r-0", [])
    for n in range(1, 8):
        # Each run reuses the same 64-char ID for a different check, in different categories.
        saved = store.merge_run(
            root, "acme", "Acme", data_for(f"cat{n}", long_id, f"k{n}"), evidence, f"r-{n}", []
        )
    ids = [f["id"] for f in saved["findings"]]
    assert len(ids) == len(set(ids)) == 8
    assert all(len(i) <= 64 for i in ids)
    for f in saved["findings"]:
        Finding.model_validate(f)
    must_load(root, "acme")
    store.report_data(saved)  # later renders/merges still validate


def test_collision_rename_preserves_fallback_key_so_repeat_merges_consolidate(tmp_path):
    evidence = [Evidence(id="ev-x-001", source="x", url="u", description="d", data={})]

    def data_for(category):
        finding = Finding(
            id="same",
            title="t",
            category=category,
            evidence_ids=["ev-x-001"],
            detail="d",
            recommendation="r",
        )
        return {
            "created_at": "2026-01-01T00:00:00+00:00",
            "status": "complete",
            "target": "t",
            "selected_categories": [category],
            "assessed_categories": [category],
            "ai": {"status": "skipped"},
            "modules": [],
            "pages": [],
            "max_pages": 1,
            "limitations": [],
            "future": [],
            "report_files": [],
            "findings": [finding.model_dump()],
        }

    store.merge_run(tmp_path, "acme", "Acme", data_for("seo"), evidence, "a", [])
    store.merge_run(tmp_path, "acme", "Acme", data_for("security"), evidence, "b", [])
    saved = store.merge_run(tmp_path, "acme", "Acme", data_for("security"), evidence, "c", [])
    assert sorted(f["id"] for f in saved["findings"]) == ["same", "same-r2"]
    security = next(f for f in saved["findings"] if f["category"] == "security")
    assert len(security["evidence_ids"]) == 2  # r2 and r3 evidence consolidated into one record


def test_ai_narratives_cannot_present_customer_statements_as_technical(caplog):
    technical = Evidence(id="ev-a-001", source="a", url="u", description="d", data={})
    statement = Evidence(
        id="ev-discovery-r1-001",
        source="discovery",
        url="u",
        description="d",
        data={},
        provenance="customer_statement",
    )
    bad = "Technical checks confirm 20 hours lost weekly (ev-a-001, ev-discovery-r1-001)."
    plain = "20 hours are lost weekly (ev-a-001, ev-discovery-r1-001)."
    good = "The customer reported 20 hours lost weekly (ev-discovery-r1-001)."
    analysis, _, warnings = validate_analysis(
        {
            "executive_summary": bad,
            "sections": {"seo": plain, "geo": good},
        },
        [technical, statement],
        {"seo", "geo"},
        [],
    )
    assert analysis["executive_summary"] == ""
    assert "seo" not in analysis["sections"]
    assert analysis["sections"]["geo"].startswith("Customer-reported, not technically verified: ")
    assert sum("unqualified customer statement" in w for w in warnings) == 2


def test_discovery_refresh_supersedes_evidence_and_keeps_cited_originals(home, tmp_path):
    notes = notes_file(tmp_path)
    cli.main(["discover", "--customer", "acme", "--notes", str(notes)])
    before = must_load(home, "acme")
    [finding] = before["findings"]
    old_ids = list(finding["evidence_ids"])
    originals = {e["id"]: e for e in before["evidence"] if e["id"] in old_ids}
    # Refresh a statement the finding cites, plus an uncited one, without re-deriving findings.
    data = json.loads(notes.read_text())
    data["sections"]["sales_quoting"] = [
        {
            "statement": "Quotes are built by hand in spreadsheets",
            "frequency": "weekly",
            "impact": "one hour",
        }
    ]
    data["sections"]["enquiries_communication"][0]["frequency"] = "weekly"
    data["findings"] = []
    notes.write_text(json.dumps(data))
    cli.main(["discover", "--customer", "acme", "--notes", str(notes)])
    saved = must_load(home, "acme")
    by_id = {e["id"]: e for e in saved["evidence"]}
    # The old finding still cites its original evidence, with the original values intact.
    [finding] = saved["findings"]
    assert finding["evidence_ids"] == old_ids
    for eid in old_ids:
        assert by_id[eid] == originals[eid]
    quoting = next(e for e in by_id.values() if e["id"] == old_ids[0] or e["id"] in old_ids)
    assert "weekly" not in json.dumps(quoting["data"])
    # The discovery index points at new evidence that supersedes the old.
    entry = saved["discovery"]["sections"]["sales_quoting"][0]
    assert entry["evidence_id"] not in old_ids and entry["frequency"] == "weekly"
    assert by_id[entry["evidence_id"]]["data"]["supersedes"] in old_ids
    assert by_id[entry["evidence_id"]]["data"]["impact"] == "one hour"
    comms = saved["discovery"]["sections"]["enquiries_communication"][0]
    assert by_id[comms["evidence_id"]]["data"]["frequency"] == "weekly"
    assert by_id[comms["evidence_id"]]["provenance"] == "customer_statement"
    # A third, identical ingestion is a no-op.
    cli.main(["discover", "--customer", "acme", "--notes", str(notes)])
    assert len(must_load(home, "acme")["evidence"]) == len(saved["evidence"])
    # Re-deriving the finding now cites the refreshed evidence as well as the original.
    data["findings"] = json.loads(notes_file(tmp_path).read_text())["findings"]
    notes.write_text(json.dumps(data))
    cli.main(["discover", "--customer", "acme", "--notes", str(notes)])
    [merged] = must_load(home, "acme")["findings"]
    assert set(old_ids) < set(merged["evidence_ids"])


def test_narrative_label_follows_weakest_provenance_and_revalidates():
    technical = Evidence(id="ev-a-001", source="a", url="u", description="d", data={})
    stated = technical.model_copy(
        update={"id": "ev-discovery-r1-001", "provenance": "customer_statement"}
    )
    assumed = technical.model_copy(update={"id": "ev-discovery-r1-002", "provenance": "assumption"})
    evidence = [technical, stated, assumed]

    def check(summary):
        analysis, _, warnings = validate_analysis(
            {"executive_summary": summary}, evidence, {"seo"}, []
        )
        return analysis["executive_summary"], warnings

    assert check("Titles are missing (ev-a-001).") == ("Titles are missing (ev-a-001).", [])
    reported, _ = check("The customer reported 20 hours lost (ev-a-001, ev-discovery-r1-001).")
    assert reported.startswith("Customer-reported, not technically verified: ")
    assumption, _ = check("We assume quoting costs 20 hours weekly (ev-discovery-r1-002).")
    assert assumption.startswith("Assumption, not verified: ")
    assert "Customer-reported" not in assumption
    # Mixed statement + assumption takes the weaker (assumption) label.
    mixed, _ = check("We assume 20 hours is lost (ev-discovery-r1-001, ev-discovery-r1-002).")
    assert mixed.startswith("Assumption, not verified: ")
    # Accepted, prefixed narratives revalidate cleanly and are not double-prefixed.
    for text in (reported, assumption, mixed):
        again, warnings = check(text)
        assert again == text and not warnings
    # Disclaimers are not confirmation claims; affirmative claims still are.
    ok, warnings = check(
        "The customer reported 20 hours, not independently verified (ev-discovery-r1-001)."
    )
    assert ok and not warnings
    for bad in (
        "The customer reported it and technical checks confirm it (ev-discovery-r1-001).",
        "We assume it, and it is verified (ev-discovery-r1-002).",
        "20 hours are lost (ev-discovery-r1-002).",
    ):
        assert check(bad)[0] == ""


def test_run_exit_message_reflects_actual_status(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "config", lambda: {})
    monkeypatch.setattr(runner, "REGISTRY", {"security": fake_module()})
    monkeypatch.setattr(store, "merge_run", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(
        cli,
        "run",
        lambda ctx, **k: {"status": "incomplete", "findings": [], "store_status": "failed"},
    )
    args = ["run", "--customer", "acme", "--target", "example.com", "--categories", "security"]
    assert cli.main(args) == 3
    out = capsys.readouterr().out
    assert "status incomplete" in out and "is complete" not in out


def test_narrative_guard_handles_double_negatives_and_noun_forms():
    stated = Evidence(
        id="ev-discovery-r1-001",
        source="discovery",
        url="u",
        description="d",
        data={},
        provenance="customer_statement",
    )

    def accepted(body):
        analysis, _, _ = validate_analysis(
            {"executive_summary": f"The customer reported this: {body} (ev-discovery-r1-001)."},
            [stated],
            {"seo"},
            [],
        )
        return analysis["executive_summary"]

    for claim in (
        "it is not unverified",
        "independent confirmation establishes this as fact",
        "this has been corroborated",
        "validation shows it holds",
        "there is proof of the loss",
        "technical evidence shows it",
        "it is not untested and not unconfirmed",
    ):
        assert accepted(claim) == "", claim
    for disclaimer in (
        "this is not independently verified",
        "this is not verified",
        "there is no independent confirmation",
        "the figure is unverified",
        "it has not been validated yet",
        "it isn't confirmed",
    ):
        text = accepted(disclaimer)
        assert text.startswith("Customer-reported, not technically verified: "), disclaimer
        # Accepted prefixed output revalidates unchanged.
        analysis, _, warnings = validate_analysis(
            {"executive_summary": text}, [stated], {"seo"}, []
        )
        assert analysis["executive_summary"] == text and not warnings


def test_same_text_in_different_contexts_is_stable_and_both_rows_render(home, tmp_path):
    def write(frequency_b="weekly"):
        notes = notes_file(
            tmp_path,
            sections={
                "manual_duplicated_tasks": [
                    {
                        "statement": "Quotes are copied manually",
                        "frequency": "daily",
                        "owner": "Sales",
                        "systems": ["Xero"],
                    },
                    {
                        "statement": "Quotes are copied manually",
                        "frequency": frequency_b,
                        "owner": "Finance",
                        "systems": ["MYOB"],
                    },
                ]
            },
            findings=[],
        )
        return notes

    notes = write()
    counts = []
    for _ in range(4):
        assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
        counts.append(len(must_load(home, "acme")["evidence"]))
    assert counts == [2, 2, 2, 2]
    saved = must_load(home, "acme")
    rows = saved["discovery"]["sections"]["manual_duplicated_tasks"]
    assert {r["owner"] for r in rows} == {"Sales", "Finance"} and len(rows) == 2
    assert all("supersedes" not in r for r in rows)
    # Changing only one context's detail supersedes only that context's evidence.
    cli.main(["discover", "--customer", "acme", "--notes", str(write("monthly"))])
    after = must_load(home, "acme")
    assert len(after["evidence"]) == 3
    by_owner = {r["owner"]: r for r in after["discovery"]["sections"]["manual_duplicated_tasks"]}
    assert "supersedes" not in by_owner["Sales"] and by_owner["Finance"]["frequency"] == "monthly"
    cli.main(["discover", "--customer", "acme", "--notes", str(write("monthly"))])
    assert len(must_load(home, "acme")["evidence"]) == 3
    # Identical statements differing only in frequency are stable by their order in the notes.
    twins = notes_file(
        tmp_path,
        sections={
            "sales_quoting": [
                {"statement": "Chasing quotes", "frequency": "daily"},
                {"statement": "Chasing quotes", "frequency": "weekly"},
            ]
        },
        findings=[],
    )
    for _ in range(3):
        cli.main(["discover", "--customer", "beta", "--notes", str(twins)])
        assert len(must_load(home, "beta")["evidence"]) == 2
    cli.main(["report", "--customer", "acme", "--formats", "md"])
    report = (home / "acme/report.md").read_text()
    assert report.count("Quotes are copied manually") >= 3  # two rows + evidence index
    assert "Owner: Sales" in report and "Owner: Finance" in report


def test_compound_negation_fails_closed_and_intent_is_not_a_claim():
    stated = Evidence(
        id="ev-discovery-r1-001",
        source="discovery",
        url="u",
        description="d",
        data={},
        provenance="customer_statement",
    )

    def accepted(body):
        analysis, _, _ = validate_analysis(
            {"executive_summary": f"The customer reported this: {body} (ev-discovery-r1-001)."},
            [stated],
            {"seo"},
            [],
        )
        return analysis["executive_summary"]

    for claim in (
        "There is no lack of proof of the loss.",
        "This is not without independent confirmation.",
        "We cannot not confirm the loss.",
        "it is not unverified",
        "independent confirmation establishes this as fact",
    ):
        assert accepted(claim) == "", claim
    for fine in (
        "authentication failures increased",
        "we need to verify the estimate before acting",
        "we should confirm the figure and plan to validate it",
        "this is not independently verified",
        "there is no independent confirmation",
        "the figure is unverified",
        "it has not been validated yet",
        "it isn't confirmed",
        "an established customer lost 20 hours",
    ):
        assert accepted(fine).startswith("Customer-reported, not technically verified: "), fine


def _statements(tmp_path, *rows, name="notes.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {"sections": {"sales_quoting": [{"statement": "Chasing quotes", **r} for r in rows]}}
        )
    )
    return path


def test_discovery_identity_is_order_independent_and_subset_safe(home, tmp_path):
    daily, weekly = {"frequency": "daily"}, {"frequency": "weekly"}

    def ingest(*rows):
        notes = _statements(tmp_path, *rows)
        assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
        saved = must_load(home, "acme")
        return saved, saved["discovery"]["sections"]["sales_quoting"]

    saved, rows = ingest(daily, weekly)
    assert len(saved["evidence"]) == 2
    ids = {r["frequency"]: r["evidence_id"] for r in rows}
    # (a) Reordering never changes anything, however often it alternates.
    for order in ((weekly, daily), (daily, weekly), (weekly, daily)):
        saved, rows = ingest(*order)
        assert len(saved["evidence"]) == 2
        assert {r["frequency"]: r["evidence_id"] for r in rows} == ids
    # (b) A subset is idempotent and does not overwrite the other version.
    for subset in ([weekly], [daily]):
        saved, rows = ingest(*subset)
        assert len(saved["evidence"]) == 2
        assert {r["frequency"]: r["evidence_id"] for r in rows} == ids
        assert not any("supersedes" in r for r in rows)
    # (c) Ambiguous change fails closed: a third version is added, nothing is superseded.
    saved, rows = ingest({"frequency": "monthly"})
    assert len(saved["evidence"]) == 3 and len(rows) == 3
    assert not any("supersedes" in r for r in rows)
    saved, rows = ingest({"frequency": "monthly"})
    assert len(saved["evidence"]) == 3


def test_unambiguous_change_supersedes_and_refs_update_explicitly(home, tmp_path):
    def ingest(*rows):
        notes = _statements(tmp_path, *rows)
        assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
        saved = must_load(home, "acme")
        return saved, saved["discovery"]["sections"]["sales_quoting"]

    saved, rows = ingest({"frequency": "daily"})
    old = rows[0]["evidence_id"]
    saved, rows = ingest({"frequency": "weekly"})
    assert len(rows) == 1 and rows[0]["supersedes"] == old and len(saved["evidence"]) == 2
    # Both versions in one file are distinct statements, not a correction of the stored one.
    saved, rows = ingest({"frequency": "weekly"}, {"frequency": "daily"})
    assert len(saved["evidence"]) == 3 and len(rows) == 2
    # An explicit ref updates exactly that statement, even when text and owner change.
    saved, rows = ingest({"ref": "chase", "frequency": "monthly", "owner": "Ops"})
    assert len(rows) == 3
    saved, rows = ingest({"ref": "chase", "frequency": "yearly", "owner": "Ops"})
    ref_row = next(r for r in rows if r.get("ref") == "chase")
    assert ref_row["frequency"] == "yearly" and "supersedes" in ref_row and len(rows) == 3
    count = len(saved["evidence"])
    saved, rows = ingest({"ref": "chase", "frequency": "yearly", "owner": "Ops"})
    assert len(saved["evidence"]) == count
    duplicate = _statements(tmp_path, {"ref": "x"}, {"ref": "x"}, name="dup.json")
    with pytest.raises(SystemExit):
        cli.main(["discover", "--customer", "acme", "--notes", str(duplicate)])


def test_narrative_allowlist_rejects_scope_bypasses_and_accepts_pending():
    stated = Evidence(
        id="ev-discovery-r1-001",
        source="discovery",
        url="u",
        description="d",
        data={},
        provenance="customer_statement",
    )

    def accepted(body):
        analysis, _, _ = validate_analysis(
            {"executive_summary": f"The customer reported this: {body} (ev-discovery-r1-001)."},
            [stated],
            {"seo"},
            [],
        )
        return analysis["executive_summary"]

    for claim in (
        "The loss is not, in the opinion of the customer, unverified.",
        "There is no doubt the loss was verified.",
        "The figures we need confirm the loss.",
        "Describing this as unverified is false.",
        "It is not the case that the loss is not verified.",
    ):
        assert accepted(claim) == "", claim
    for fine in (
        "the loss is pending verification",
        "the loss is awaiting verification",
        "the figure needs verification",
        "the figure is yet to be verified and is unverified",
        "it is to be verified before any work starts",
        "we need to verify the estimate before acting",
        "we should confirm the figure",
    ):
        text = accepted(fine)
        assert text.startswith("Customer-reported, not technically verified: "), fine
        again, _, warnings = validate_analysis({"executive_summary": text}, [stated], {"seo"}, [])
        assert again["executive_summary"] == text and not warnings


def test_discovery_citations_do_not_depend_on_notes_order(home, tmp_path):
    def write(rows, name, cite=None):
        notes = {
            "sections": {"sales_quoting": [{"statement": "Chasing quotes", **r} for r in rows]}
        }
        if cite is not None:
            notes["findings"] = [
                {
                    "title": "Chasing",
                    "detail": "d",
                    "recommendation": "r",
                    "ams_service": "AI & Automation",
                    "cites": [f"sales_quoting:{cite}"],
                }
            ]
        path = tmp_path / name
        path.write_text(json.dumps(notes))
        return path

    seed = write([{"ref": "quotes", "frequency": "daily"}], "seed.json")
    ref_weekly = {"ref": "quotes", "frequency": "weekly"}
    plain_daily = {"frequency": "daily"}
    # The finding always cites the plain "daily" statement, wherever it sits in the notes.
    for customer, rows, cite in (
        ("acme", [ref_weekly, plain_daily], 1),
        ("beta", [plain_daily, ref_weekly], 0),
    ):
        cli.main(["discover", "--customer", customer, "--notes", str(seed)])
        notes = write(rows, f"{customer}.json", cite)
        assert cli.main(["discover", "--customer", customer, "--notes", str(notes)]) == 0
        saved = must_load(home, customer)
        by_id = {e["id"]: e for e in saved["evidence"]}
        [finding] = saved["findings"]
        assert [by_id[i]["data"]["frequency"] for i in finding["evidence_ids"]] == ["daily"]
        assert len(saved["evidence"]) == 3  # seed + weekly update + distinct plain daily
        rows_ = saved["discovery"]["sections"]["sales_quoting"]
        assert {(r["ref"], r["frequency"]) for r in rows_} == {("quotes", "weekly"), ("", "daily")}
        plain = next(r for r in rows_ if not r["ref"])
        assert finding["evidence_ids"] == [plain["evidence_id"]]


def test_explicit_ref_never_falls_through_to_content_matching(home, tmp_path):
    def ingest(*rows):
        path = tmp_path / "n.json"
        path.write_text(
            json.dumps(
                {
                    "sections": {
                        "sales_quoting": [{"statement": "Chasing quotes", **r} for r in rows]
                    }
                }
            )
        )
        assert cli.main(["discover", "--customer", "acme", "--notes", str(path)]) == 0
        saved = must_load(home, "acme")
        return saved, saved["discovery"]["sections"]["sales_quoting"]

    saved, rows = ingest({"ref": "first", "frequency": "daily"})
    first_id = rows[0]["evidence_id"]
    saved, rows = ingest(
        {"ref": "first", "frequency": "daily"}, {"ref": "second", "frequency": "daily"}
    )
    assert [r["ref"] for r in rows] == ["first", "second"] and len(saved["evidence"]) == 2
    assert rows[0]["evidence_id"] == first_id
    saved, rows = ingest({"ref": "second", "frequency": "weekly"})
    by_ref = {r["ref"]: r for r in rows}
    assert by_ref["second"]["frequency"] == "weekly" and "supersedes" in by_ref["second"]
    assert by_ref["first"]["frequency"] == "daily" and by_ref["first"]["evidence_id"] == first_id
    assert "supersedes" not in by_ref["first"] and len(saved["evidence"]) == 3
