import json
import re
from pathlib import Path

import pytest

from ams_assess import cli, store
from ams_assess.discovery import DISCOVERY_SECTIONS, load_notes

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/discovery"
NEW_FIELDS = {
    "staff_time": "Staff time: ",
    "errors_rework": "Errors/rework: ",
    "cost": "Cost (as stated): ",
    "workaround": "Current workaround: ",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    return tmp_path / "data/customers"


def must_load(root, customer):
    loaded = store.load(root, customer)
    assert loaded is not None
    return loaded


def write_notes(tmp_path, sections, name="notes.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"sections": sections}))
    return path


def test_example_notes_validate_cover_every_section_and_run_through_discover(home):
    path = EXAMPLES / "notes.example.json"
    notes = load_notes(path)
    assert set(notes.sections) == DISCOVERY_SECTIONS.keys()
    # Findings span several AMS categories: AI is not forced into every assessment.
    assert len({f.ams_service for f in notes.findings}) >= 3
    assert cli.main(["discover", "--customer", "example", "--notes", str(path)]) == 0
    saved = must_load(home, "example")
    assert len(saved["findings"]) == len(notes.findings)
    assert all(f["provenance"] == "customer_statement" for f in saved["findings"])
    assert cli.main(["report", "--customer", "example", "--formats", "md"]) == 0
    report = (home / "example/report.md").read_text()
    for title in DISCOVERY_SECTIONS.values():
        assert f"### {title.replace('&', '&amp;')}" in report
    for label in NEW_FIELDS.values():
        assert label in report


def test_template_has_10_to_15_questions_each_mapped_to_a_section_key():
    text = (EXAMPLES / "discovery-template.md").read_text()
    questions = re.findall(r"^\d+\. \*\*(.+?)\*\*.*?→ `([a-z_]+)`", text, re.M | re.S)
    assert 10 <= len(questions) <= 15
    keys = {key for _, key in questions}
    assert keys == DISCOVERY_SECTIONS.keys()
    for field in ("frequency", *NEW_FIELDS, "impact"):
        assert f"`{field}`" in text


def test_new_statement_fields_are_stored_and_render_only_when_present(home, tmp_path):
    full = {"statement": "Invoices are re-keyed", **{k: f"{k[:4]} value" for k in NEW_FIELDS}}
    bare = {"statement": "Quotes are typed up"}
    notes = write_notes(tmp_path, {"manual_duplicated_tasks": [full, bare]})
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    [row, plain] = must_load(home, "acme")["discovery"]["sections"]["manual_duplicated_tasks"]
    assert {k: row[k] for k in NEW_FIELDS} == {k: f"{k[:4]} value" for k in NEW_FIELDS}
    assert cli.main(["report", "--customer", "acme", "--formats", "md"]) == 0
    lines = (home / "acme/report.md").read_text().splitlines()
    [full_line] = [line for line in lines if line.startswith("- Invoices are re-keyed")]
    [bare_line] = [line for line in lines if line.startswith("- Quotes are typed up")]
    for key, label in NEW_FIELDS.items():
        assert f"{label}{key[:4]} value." in full_line
        assert label not in bare_line
    # A changed detail field supersedes the stored statement like frequency/impact do.
    full["cost"] = "revised"
    notes = write_notes(tmp_path, {"manual_duplicated_tasks": [full, bare]})
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    rows = must_load(home, "acme")["discovery"]["sections"]["manual_duplicated_tasks"]
    assert len(rows) == 2 and rows[0]["cost"] == "revised" and "supersedes" in rows[0]


def test_entries_stored_before_the_new_fields_still_render_and_update(home, tmp_path):
    notes = write_notes(
        tmp_path, {"sales_quoting": [{"statement": "Quotes", "frequency": "daily"}]}
    )
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    saved = must_load(home, "acme")
    [row] = saved["discovery"]["sections"]["sales_quoting"]
    for key in NEW_FIELDS:
        del row[key]  # simulate a store written by the previous schema
    store.save(home, "acme", saved)
    assert cli.main(["report", "--customer", "acme", "--formats", "md"]) == 0
    # Re-ingesting the identical statement reuses it; a changed one supersedes it.
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    assert len(must_load(home, "acme")["evidence"]) == 1
    notes = write_notes(tmp_path, {"sales_quoting": [{"statement": "Quotes", "staff_time": "2h"}]})
    assert cli.main(["discover", "--customer", "acme", "--notes", str(notes)]) == 0
    [row] = must_load(home, "acme")["discovery"]["sections"]["sales_quoting"]
    assert row["frequency"] == "daily" and row["staff_time"] == "2h" and "supersedes" in row
