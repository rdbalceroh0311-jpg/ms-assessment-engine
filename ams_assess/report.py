import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.sync_api import sync_playwright

from ams_assess.ai import PHASES
from ams_assess.models import CATEGORIES, SEVERITIES, Evidence, Finding, customer_path
from ams_assess.store import load, locked, now, report_data, save

LOCAL = {"local_seo", "entity", "presence"}
MAX_MARKDOWN_BYTES = 5_000_000

CSS = """@page { size: A4; margin: 18mm 17mm 20mm; }
body { font-family: Arial, sans-serif; color: #20303d; font-size: 10pt; line-height: 1.48;
       max-width: 960px; margin: 32px auto; padding: 0 20px; }
h1 { color: #143c52; font-size: 29pt; line-height: 1.12; border-bottom: 4px solid #16a59b;
     padding-bottom: 14px; margin-bottom: 20px; }
h2 { color: #143c52; font-size: 16pt; margin-top: 26px; border-bottom: 1px solid #cbd8df;
     padding-bottom: 6px; break-after: avoid; }
h3 { color: #195b68; font-size: 12pt; margin-top: 20px; break-after: avoid; }
h3 + p, h3 + p + p { break-after: avoid; }
p,li { orphans: 3; widows: 3; overflow-wrap: anywhere; }
a { color: #166c7c; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; font-size: 8pt; table-layout: fixed; }
th:first-child { width: 20%; } th:nth-child(2) { width: 12%; }
table:has(th:nth-child(4)) th:nth-child(3) { width: 38%; }
th,td { border: 1px solid #cfdae0; padding: 6px; text-align: left; vertical-align: top;
        overflow-wrap: anywhere; }
th { background: #eaf2f5; } tr { break-inside: avoid; }
code { font-size: 8pt; overflow-wrap: anywhere; }
blockquote { margin: 15px 0; padding: 8px 14px; border-left: 4px solid #16a59b;
 background: #f3f8fa; }
@media print { body { margin: 0; padding: 0; max-width: none; }
  h2 { margin-top: 22px; } a { text-decoration: none; } }
"""


def mdtext(value: object) -> str:
    value = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]#|])", r"\\\1", value)


def render(run_dir: Path, formats: set[str] | None = None) -> list[str]:
    data = json.loads((run_dir / "results.json").read_text())
    evidence = [
        Evidence.model_validate(e) for e in json.loads((run_dir / "evidence.json").read_text())
    ]
    return render_data(run_dir, data, evidence, formats)


def output_path(directory: Path, name: str) -> Path:
    """A report destination inside the directory; symlinked or escaping targets are refused."""
    target = directory / name
    if target.is_symlink() or target.resolve().parent != directory.resolve():
        raise ValueError(f"Report output {name} must not be a symlink or leave its directory")
    return target


def render_customer(
    root: Path,
    customer: str,
    formats: set[str] | None = None,
    edited_md: Path | None = None,
    based_on: int | None = None,
) -> list[str]:
    """Write a new immutable report revision from the store, whichever stages have run.

    Each generation gets its own reports/<NNN-timestamp>/ directory recorded in the store's
    append-only ``report_revisions``; report.* in the customer directory is a copy of the latest.
    The markdown is the master: ``edited_md`` (a human-edited report.md) becomes a new revision
    whose html/pdf are rendered from it. Its ancestry is the revision directory it was taken
    from, else ``based_on``, else recorded explicitly as unknown.
    """
    if based_on is not None and edited_md is None:
        raise ValueError("--based-on requires --from-md")
    formats = (formats or {"md", "html", "pdf"}) | {"md"}
    content = None
    if edited_md is not None:
        if edited_md.is_symlink() or not edited_md.is_file():
            raise ValueError("Edited markdown must be a regular file")
        if edited_md.stat().st_size > MAX_MARKDOWN_BYTES:
            raise ValueError("Edited markdown is too large")
        content = edited_md.read_text()
    with locked(root, customer, create=False):
        store = load(root, customer)
        if store is None:
            raise ValueError("No assessment store for this customer")
        directory = customer_path(root, customer)
        for ext in formats:  # refuse unsafe latest-copy targets before writing anything
            output_path(directory, f"report.{ext}")
        reports = directory / "reports"
        if reports.is_symlink():
            raise ValueError("Report revisions directory must not be a symlink")
        revisions = store.setdefault("report_revisions", [])
        ancestry: dict[str, Any] = {}
        if edited_md is not None:
            ancestry = edited_ancestry(directory, revisions, edited_md, based_on)
        number = len(revisions) + 1
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        revision = reports / f"{number:03d}-{stamp}"
        reports.mkdir(exist_ok=True, mode=0o700)
        revision.mkdir(mode=0o700)
        if content is None:
            data, evidence, discovery = report_data(store)
            content = render_markdown(data, evidence, discovery)
        written = write_outputs(revision, content, formats)
        for name in written:
            (revision / name).chmod(0o400)  # a revision is never rewritten
            output_path(directory, name).write_bytes((revision / name).read_bytes())
        runs = store["runs"]
        revisions.append(
            {
                "revision": number,
                "created_at": now(),
                "directory": f"reports/{revision.name}",
                "files": written,
                "source": "edited markdown" if edited_md else "generated",
                # The assessment state this report reflects (an edit inherits its ancestor's).
                "assessment_revision": runs[-1]["run_id"] if runs else None,
                "store_updated_at": store["updated_at"],
                **ancestry,
            }
        )
        store["report_files"] = {name.rsplit(".", 1)[1]: name for name in written}
        save(root, customer, store)
    return written


def edited_ancestry(
    directory: Path, revisions: list[dict[str, Any]], edited_md: Path, based_on: int | None
) -> dict[str, Any]:
    """Which revision an edited markdown descends from; never guessed from the latest one."""
    by_number = {r["revision"]: r for r in revisions}
    by_dir = {(directory / r["directory"]).resolve(): r for r in revisions}
    source = by_dir.get(edited_md.resolve().parent)
    if source and based_on is not None and based_on != source["revision"]:
        raise ValueError("--based-on contradicts the revision directory of --from-md")
    if source is None and based_on is not None:
        source = by_number.get(based_on)
        if source is None:
            raise ValueError(f"--based-on: no report revision {based_on}")
    if source is None:
        return {"based_on": "unknown", "assessment_revision": "unknown"}
    return {
        "based_on": source["revision"],
        "assessment_revision": source["assessment_revision"],
        "store_updated_at": source["store_updated_at"],
    }


def render_data(
    run_dir: Path,
    data: dict[str, Any],
    evidence: list[Evidence],
    formats: set[str] | None = None,
    discovery: dict[str, Any] | None = None,
) -> list[str]:
    content = render_markdown(data, evidence, discovery)
    return write_outputs(run_dir, content, formats or {"md", "html", "pdf"})


def render_markdown(
    data: dict[str, Any], evidence: list[Evidence], discovery: dict[str, Any] | None = None
) -> str:
    findings = [Finding.model_validate(f) for f in data["findings"]]
    known = {e.id for e in evidence}
    if any(not set(f.evidence_ids) <= known for f in findings):
        raise ValueError("Report refused: a finding references missing evidence")
    findings.sort(key=lambda f: (f.priority, SEVERITIES[f.severity], f.category, f.id))
    # Findings whose basis changed or conflicts are listed for review, not recommended.
    review = {
        fid
        for r in data.get("needs_review", [])
        for fid in [r.get("finding_id"), *r.get("finding_ids", [])]
        if fid
    }
    by_id = {f.id: f for f in findings if f.id not in review}
    ai = data["ai"]
    proposed = ai.get("recommendations", [])
    order = list(dict.fromkeys([i for i in proposed if i in by_id] + list(by_id)))
    roadmap = {phase: [] for phase in PHASES}
    assigned = set()
    for phase in PHASES:
        for fid in ai.get("roadmap", {}).get(phase, []):
            if fid in by_id and fid not in assigned:
                roadmap[phase].append(by_id[fid])
                assigned.add(fid)
    for finding in by_id.values():
        if finding.id not in assigned:
            phase = {"P1": "0-30 days", "P2": "30-90 days", "P3": "90+ days"}[finding.priority]
            roadmap[phase].append(finding)
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["text"] = mdtext
    template = env.get_template("report.md.j2")
    shown = set(data["assessed_categories"])
    # The health summary explains NOT ASSESSED rows whenever domain_email ran, even on error.
    if data.get("domain_email_health") and any(
        m["name"] == "domain_email" and m["status"] != "skipped" for m in data["modules"]
    ):
        shown.add("domain_email")
    content = template.render(
        data=data,
        ai=ai,
        evidence=evidence,
        findings=findings,
        # Sections 2 and 3 of the report; presence renders from data["digital_presence"].
        web_sections={k: t for k, t in CATEGORIES.items() if k in shown and k not in LOCAL},
        local_sections={
            k: t for k, t in CATEGORIES.items() if k in shown & {"local_seo", "entity"}
        },
        recommendations=[by_id[i] for i in order],
        roadmap=roadmap,
        scope_titles=[CATEGORIES[c] for c in data["selected_categories"]]
        + (["Business discovery (customer statements)"] if discovery else []),
        discovery=discovery,
        collected_sources={e.source for e in evidence},
        browser_observations=sum(e.source == "browser" for e in evidence),
    )
    return content


def write_outputs(run_dir: Path, content: str, formats: set[str]) -> list[str]:
    written = []
    targets = {ext: output_path(run_dir, f"report.{ext}") for ext in formats}
    if "md" in formats:
        targets["md"].write_text(content)
        written.append("report.md")
    body = markdown.markdown(content, extensions=["tables", "toc"])
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<title>AMS Assessment Report</title><style>"
        + CSS
        + "</style></head><body>"
        + body
        + "</body></html>"
    )
    if "html" in formats:
        targets["html"].write_text(document)
        written.append("report.html")
    if "pdf" in formats:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content(document, wait_until="load")
                page.pdf(
                    path=str(targets["pdf"]),
                    format="A4",
                    print_background=True,
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template='<div style="font:9px Arial;width:100%;text-align:center;'
                    'color:#65727d">AI Managed Services | Page <span class="pageNumber"></span>'
                    ' of <span class="totalPages"></span></div>',
                    margin={"top": "18mm", "bottom": "20mm", "left": "17mm", "right": "17mm"},
                )
            finally:
                browser.close()
        written.append("report.pdf")
    return written
