import json
import stat
import threading

import pytest

from ams_assess import cli, store
from ams_assess.discovery import Notes
from ams_assess.models import Evidence
from ams_assess.report import render_markdown


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    return tmp_path / "data/customers"


def must_load(root, customer):
    loaded = store.load(root, customer)
    assert loaded is not None
    return loaded


def discover(tmp_path, customer, sections, findings=(), name="notes.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"sections": sections, "findings": list(findings)}))
    return cli.main(["discover", "--customer", customer, "--notes", str(path)])


def finding(detail="Quotes are built manually", cites=("sales_quoting:0",)):
    return {
        "title": "Quote automation",
        "detail": detail,
        "recommendation": "Automate quote generation",
        "ams_service": "AI & Automation",
        "cites": list(cites),
    }


def report(home, customer="acme", *extra):
    assert cli.main(["report", "--customer", customer, "--formats", "md,html", *extra]) == 0
    return (home / customer / "report.md").read_text()


# Issue 1: every report generation is an immutable, indexed revision.
def test_report_revisions_are_immutable_indexed_and_md_is_the_master(home, tmp_path):
    discover(tmp_path, "acme", {"sales_quoting": ["Quotes are built by hand"]})
    first = report(home)
    discover(tmp_path, "acme", {"pain_points_bottlenecks": ["Invoices go out late"]}, name="b")
    second = report(home)
    saved = must_load(home, "acme")
    revisions = saved["report_revisions"]
    assert [r["revision"] for r in revisions] == [1, 2]
    assert [r["assessment_revision"] for r in revisions] == ["r1", "r2"]
    old, new = (home / "acme" / r["directory"] for r in revisions)
    assert old != new and old.parent == home / "acme/reports"
    # The earlier report is still readable and unchanged; the latest copy is the newest.
    assert (old / "report.md").read_text() == first and "Invoices go out late" not in first
    assert (new / "report.md").read_text() == second and "Invoices go out late" in second
    assert (old / "report.html").is_file()
    assert stat.S_IMODE((old / "report.md").stat().st_mode) == 0o400
    assert saved["report_files"] == {"md": "report.md", "html": "report.html"}
    # A human edits the latest markdown; html is re-rendered from it as a new revision.
    edited = second.replace("Invoices go out late", "Invoices go out late (edited by AMS)")
    (home / "acme/report.md").write_text(edited)
    report(home, "acme", "--from-md", str(home / "acme/report.md"))
    third = must_load(home, "acme")["report_revisions"][-1]
    assert third["revision"] == 3 and third["source"] == "edited markdown"
    assert third["based_on"] == 2
    directory = home / "acme" / third["directory"]
    assert (directory / "report.md").read_text() == edited
    assert "edited by AMS" in (directory / "report.html").read_text()
    assert (new / "report.md").read_text() == second
    with pytest.raises(SystemExit):
        cli.main(["report", "--customer", "acme", "--from-md", str(tmp_path / "missing.md")])
    assert len(must_load(home, "acme")["report_revisions"]) == 3


# Issue 2: superseded and contradictory findings never stay silently active.
def test_finding_resting_on_superseded_statement_is_retired_not_recommended(home, tmp_path):
    manual = {"ref": "quotes", "statement": "Quotes are built manually"}
    discover(tmp_path, "acme", {"sales_quoting": [manual]}, [finding()])
    [active] = must_load(home, "acme")["findings"]
    automated = {"ref": "quotes", "statement": "Quotes are fully automated"}
    discover(tmp_path, "acme", {"sales_quoting": [automated]}, name="b")
    saved = must_load(home, "acme")
    assert saved["findings"] == []
    [old] = saved["finding_history"]
    assert old["id"] == active["id"] and old["status"] == "superseded"
    text = report(home)
    recommendations = text.split("## 8. Prioritised recommendations")[1].split("## 9.")[0]
    assert "Quote automation" not in recommendations
    assert f"**Superseded:** {active['id']}: Quote automation" in text


def test_changed_claim_on_reimport_is_a_new_version_not_concatenated(home, tmp_path):
    sections = {"sales_quoting": ["Quotes are built manually"]}
    discover(tmp_path, "acme", sections, [finding()])
    [v1] = must_load(home, "acme")["findings"]
    discover(tmp_path, "acme", sections, [finding(detail="Quotes are now automated")], name="b")
    saved = must_load(home, "acme")
    [v2] = saved["findings"]
    assert v2["detail"] == "Quotes are now automated" and v2["id"] != v1["id"]
    [old] = saved["finding_history"]
    assert old["detail"] == v1["detail"] and old["superseded_by"] == v2["id"]
    # Re-importing the same claim again is not a new version.
    discover(tmp_path, "acme", sections, [finding(detail="Quotes are now automated")], name="c")
    assert len(must_load(home, "acme")["finding_history"]) == 1


def test_partly_superseded_or_conflicting_basis_is_flagged_for_review(home, tmp_path):
    sections = {
        "sales_quoting": [{"ref": "quotes", "statement": "Quotes are built manually"}],
        "manual_duplicated_tasks": ["Jobs are re-keyed into Xero"],
    }
    cites = ("sales_quoting:0", "manual_duplicated_tasks:0")
    discover(tmp_path, "acme", sections, [finding(cites=cites)])
    changed = {"sales_quoting": [{"ref": "quotes", "statement": "Quotes are fully automated"}]}
    discover(tmp_path, "acme", changed, name="b")
    saved = must_load(home, "acme")
    [active] = saved["findings"]
    [flag] = saved["needs_review"]
    assert flag["finding_id"] == active["id"] and len(flag["evidence_ids"]) == 1
    text = report(home)
    assert f"**Needs review:** {active['id']}: Quote automation" in text
    section_8 = text.split("## 8. Prioritised recommendations")[1].split("## 9.")[0]
    assert "Quote automation" not in section_8
    # Two current versions of the same statement are conflicting evidence.
    rows = [{"statement": "Chasing quotes", "frequency": f} for f in ("daily", "weekly")]
    discover(tmp_path, "beta", {"sales_quoting": rows})
    [conflict] = must_load(home, "beta")["needs_review"]
    assert "conflicting" in conflict["reason"] and len(conflict["evidence_ids"]) == 2


# Issue 3: concurrent imports never lose data.
def test_concurrent_imports_serialise_and_keep_every_statement(home):
    errors = []

    def ingest(n):
        try:
            notes = Notes.model_validate({"sections": {"sales_quoting": [f"Statement {n}"]}})
            store.add_discovery(home, "acme", "Acme", notes)
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=ingest, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    saved = must_load(home, "acme")
    statements = {r["statement"] for r in saved["discovery"]["sections"]["sales_quoting"]}
    assert statements == {f"Statement {n}" for n in range(8)}
    assert sorted(r["run_id"] for r in saved["runs"]) == sorted(f"r{n}" for n in range(1, 9))
    assert stat.S_IMODE((home / "acme" / store.LOCK_FILE).stat().st_mode) == 0o600


def test_lock_wait_is_bounded_with_a_clear_error(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.1)
    discover(tmp_path, "acme", {"sales_quoting": ["Quotes"]})
    before = (home / "acme/assessment.json").read_text()
    with store.locked(home, "acme"):
        with pytest.raises(SystemExit) as exc:
            discover(tmp_path, "acme", {"sales_quoting": ["Other"]}, name="b")
        assert exc.value.code == 2
        assert "busy" in capsys.readouterr().err
        with pytest.raises(SystemExit):
            cli.main(["report", "--customer", "acme", "--formats", "md"])
    assert (home / "acme/assessment.json").read_text() == before


# Issue 4: multi-site stores keep each result attributed to its website.
def run_data(target, category, health=None, modules=()):
    data = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "status": "complete",
        "target": target,
        "selected_categories": [category],
        "assessed_categories": [category],
        "ai": {"status": "skipped", "provider": None, "reason": "no AI"},
        "modules": [{"name": m, "status": "ok", "reason": ""} for m in modules],
        "pages": [],
        "max_pages": 1,
        "limitations": [],
        "future": [],
        "report_files": [],
        "findings": [],
    }
    if health:
        data["domain_email_health"] = health
    return data


def test_multi_site_report_attributes_checks_to_their_website(home):
    health = {
        "rows": [
            {
                "area": "SPF",
                "status": "PASS",
                "issue": "-",
                "evidence_ids": ["ev-domain_email-001"],
                "checked": "SPF record",
                "why": "Spoofing.",
                "recommendation": "None.",
                "checks": [],
            }
        ],
        "limitations": [],
    }
    site_a, site_b = "https://a.example.com/", "https://b.example.com/"
    email = Evidence(
        id="ev-domain_email-001", source="domain_email", url=site_a, description="SPF", data={}
    )
    header = Evidence(
        id="ev-security-001", source="security", url=site_b, description="Header", data={}
    )
    store.merge_run(
        home,
        "acme",
        "Acme",
        run_data(site_a, "domain_email", health, ["domain_email"]),
        [email],
        "run-a",
        ["public"],
    )
    saved = store.merge_run(
        home,
        "acme",
        "Acme",
        run_data(site_b, "security", None, ["security"]),
        [header],
        "run-b",
        ["public"],
    )
    data, evidence, discovery = store.report_data(saved)
    assert [t["target"] for t in data["targets"]] == [site_a, site_b]
    [summary] = data["domain_email_health_sites"]
    assert summary["target"] == site_a and summary["run_id"] == "r1"
    text = render_markdown(data, evidence, discovery)
    assert f"**Targets:** {site_a} (runs r1); {site_b} (runs r2)" in text
    assert f"Domain, DNS & Email Health summary: {site_a} (run r1)" in text
    assert f"| domain\\_email ({site_a}) | ok |" in text
    assert f"| security ({site_b}) | ok |" in text
