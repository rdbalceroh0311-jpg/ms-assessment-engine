import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markdown.inlinepatterns import InlineProcessor
from playwright.sync_api import sync_playwright

from ams_assess.ai import PHASES
from ams_assess.consolidate import consolidate
from ams_assess.discovery import DISCOVERY_SECTIONS
from ams_assess.models import CATEGORIES, SEVERITIES, Evidence, Finding, customer_path
from ams_assess.modules.gsc import AI_REPORT_NOTE
from ams_assess.platforms import BUYING_MODELS, INDUSTRIES
from ams_assess.store import load, locked, now, report_data, save

LOCAL = {"local_seo", "entity", "presence"}
MAX_MARKDOWN_BYTES = 5_000_000
DRAFT_BANNER = (
    "Draft — AI-assisted analysis requires AMS human review before it is sent to the customer"
)


def discovery_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Condense repetitive discovery outcomes for display, retaining the stored detail."""
    output: list[dict[str, Any]] = []
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        state, detail = row["state"], row["detail"]
        group = None
        if not row.get("manual") and not row["candidates"]:
            if state == "Not applicable":
                detail = "Not relevant for the recorded industry and buying model."
                group = (state, detail, row["relevance"])
            elif state == "Not assessed" and detail.startswith("Not searched:"):
                group = (state, detail, row["relevance"])
        if group is not None and group in groups:
            summary = groups[group]
            summary["platform"] += ", " + row["platform"]
            summary["evidence_ids"] = list(
                dict.fromkeys([*summary["evidence_ids"], *row["evidence_ids"]])
            )
        else:
            summary = {**row, "detail": detail}
            output.append(summary)
            if group is not None:
                groups[group] = summary
    return output


def connection_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Use collected status, with conservative fallbacks for readable pre-v1 runs."""
    rows = []
    for module in data["modules"]:
        target = module.get("target", data.get("target", ""))
        connections = module.get("connections", [])
        if connections:
            rows.extend({**c, "target": target} for c in connections)
            continue
        services = {
            "gsc": [("gsc", "Google Search Console")],
            "performance": [("pagespeed", "PageSpeed Insights"), ("crux", "CrUX")],
            "dataforseo": [("dataforseo", "DataForSEO")],
        }
        for key, service in services.get(module["name"], []):
            rows.append(
                {
                    "key": key,
                    "service": service,
                    "target": target,
                    "status": "Not assessed",
                    "evidence_ids": [],
                    "detail": module.get("reason") or "Connection status not recorded in this run",
                }
            )
    # Unsupported connectors are opportunities for a manual review, never connected evidence.
    sites = data.get("digital_presence_sites") or [data.get("digital_presence")]
    for p in filter(None, sites):
        for r in p.get("readiness", []):
            target = p.get("target", data.get("target", ""))
            if any(c["key"] == r["key"] and c["target"] == target for c in rows):
                continue
            status = (
                r["access"]
                if r["access"]
                in {
                    "Connected successfully",
                    "Permission required",
                    "Not connected",
                    "Not assessed",
                }
                else "Not assessed"
            )
            rows.append(
                {
                    "key": r["key"],
                    "service": r["service"],
                    "target": target,
                    "status": status if r["key"] == "gsc" else "Unsupported",
                    "detail": r["insights"],
                    "evidence_ids": r["evidence_ids"],
                }
            )
    for row in rows:
        if row["key"] == "gsc" and AI_REPORT_NOTE not in row["detail"]:
            row["detail"] += ". " + AI_REPORT_NOTE
    return rows


CSS = """@page { size: A4; margin: 18mm 17mm 20mm; }
body { font-family: Arial, sans-serif; color: #20303d; font-size: 10pt; line-height: 1.48;
       max-width: 960px; margin: 32px auto; padding: 0 20px; }
h1 { color: #143c52; font-size: 29pt; line-height: 1.12; border-bottom: 4px solid #16a59b;
     padding-bottom: 14px; margin-bottom: 20px; }
h2 { color: #143c52; font-size: 16pt; margin-top: 26px; border-bottom: 1px solid #cbd8df;
     padding-bottom: 6px; break-after: avoid; }
h3 { color: #195b68; font-size: 12pt; margin-top: 20px; break-after: avoid; }
h4 { break-after: avoid; }
h3 + p, h3 + p + p { break-after: avoid; }
p,li { orphans: 3; widows: 3; overflow-wrap: anywhere; }
a { color: #166c7c; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; font-size: 8pt; table-layout: fixed; }
th:first-child { width: 20%; } th:nth-child(2) { width: 12%; }
table:has(th:nth-child(4)) th:nth-child(3) { width: 38%; }
table:has(th:nth-child(5)) th { width: auto; }
table:has(th:nth-child(6)) th:nth-child(n) { width: auto; }
table:has(th:nth-child(5)):not(:has(th:nth-child(6))) th:nth-child(1) { width: 22%; }
table:has(th:nth-child(5)):not(:has(th:nth-child(6))) th:nth-child(2) { width: 10%; }
table:has(th:nth-child(5)):not(:has(th:nth-child(6))) th:nth-child(3) { width: 24%; }
table:has(th:nth-child(5)):not(:has(th:nth-child(6))) th:nth-child(4) { width: 20%; }
table:has(th:nth-child(5)):not(:has(th:nth-child(6))) th:nth-child(5) { width: 24%; }
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
    value = html.unescape(" ".join(str(value).split()))
    return re.sub(r"([\\`*_{}\[\]#|<>])", r"\\\1", value)


class ReportLineBreak(InlineProcessor):
    def handleMatch(self, m, data):
        return ElementTree.Element("br"), m.start(0), m.end(0)


def markdown_html(content: str) -> str:
    # HTML escaping belongs here, not in the editable Markdown master. Disable raw HTML
    # (including in edited reports); retain only the template's attribute-free line breaks.
    renderer = markdown.Markdown(extensions=["tables", "toc"])
    renderer.preprocessors.deregister("html_block")
    renderer.inlinePatterns.deregister("html")
    renderer.ESCAPED_CHARS = [*renderer.ESCAPED_CHARS, "<", ">"]
    renderer.inlinePatterns.register(ReportLineBreak(r"<br\s*/?>"), "report-br", 175)
    return renderer.convert(content)


def render(run_dir: Path, formats: set[str] | None = None) -> list[str]:
    data = json.loads((run_dir / "results.json").read_text())
    evidence = [
        Evidence.model_validate(e) for e in json.loads((run_dir / "evidence.json").read_text())
    ]
    return render_data(run_dir, data, evidence, formats, data.get("discovery"))


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
            output_path(directory, name).chmod(0o600)
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
    # Recheck each basis without merging distinct customer-store findings across websites.
    findings = [consolidate([Finding.model_validate(f)], evidence)[0] for f in data["findings"]]
    known = {e.id for e in evidence}
    if any(not set(f.evidence_ids) <= known for f in findings):
        raise ValueError("Report refused: a finding references missing evidence")
    findings.sort(key=lambda f: (f.priority, SEVERITIES[f.severity], f.category, f.id))
    # Findings whose basis changed or conflicts belong only in section 10.
    review = {
        fid
        for r in data.get("needs_review", [])
        for fid in [r.get("finding_id"), *r.get("finding_ids", [])]
        if fid
    }
    by_id = {f.id: f for f in findings if f.id not in review}
    findings = list(by_id.values())
    site_finding_eids = {
        eid for f in findings if f.check_key.startswith("site:") for eid in f.evidence_ids
    }
    represented_site_checks = {
        (e.url, e.data["site_check"])
        for e in evidence
        if e.id in site_finding_eids and e.source == "site_checks" and e.data.get("site_check")
    }
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
    for fid in order:
        finding = by_id[fid]
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
    env.filters["discovery_rows"] = discovery_rows
    env.filters["discovery_heading"] = lambda title: (
        title if title in DISCOVERY_SECTIONS.values() else mdtext(title)
    )
    template = env.get_template("report.md.j2")
    shown = set(data["assessed_categories"])
    # The health summary explains NOT ASSESSED rows whenever domain_email ran, even on error.
    if data.get("domain_email_health") and any(
        m["name"] == "domain_email" and m["status"] != "skipped" for m in data["modules"]
    ):
        shown.add("domain_email")
    # Missing field/lab measurements still have an explicit performance interpretation.
    if any(e.source == "performance" and e.data.get("performance") for e in evidence):
        shown.add("performance")
    connected_ids = {e.id for e in evidence if e.source == "gsc"}
    connected = [f for f in findings if connected_ids.intersection(f.evidence_ids)]
    public = [f for f in findings if f not in connected]
    sources = {e.id: e.source for e in evidence}
    presence_sites = data.get("digital_presence_sites") or [data.get("digital_presence")]
    presence_sites = [p for p in presence_sites if p]
    assessed_presence = [
        p
        for p in presence_sites
        if p.get("status") == "assessed"
        or p.get("discovery", {}).get("status") in {"Assessed", "Partially assessed"}
        or any(r.get("manual") for r in p.get("discovery", {}).get("platforms", []))
    ]
    # GSC alone must not create empty website/SEO sections.
    public_categories = {f.category for f in public}
    if any(
        e.source in {"siteone", "browser", "site_checks", "performance", "dataforseo"}
        for e in evidence
    ):
        public_categories |= shown
    security_categories = shown & {"security", "domain_email"}
    connections = connection_rows(data)
    profile = (data.get("profile") or {}).get("fields") or {}
    profile = {
        **profile,
        "industry": INDUSTRIES.get(profile.get("industry"), profile.get("industry")),
        "buying_model": BUYING_MODELS.get(profile.get("buying_model"), profile.get("buying_model")),
    }
    content = template.render(
        data=data,
        ai=ai,
        evidence=evidence,
        findings=findings,
        public_findings=public,
        finding_categories={f.category for f in public},
        connected_findings=connected,
        connected_evidence=[e for e in evidence if e.id in connected_ids],
        critical_findings=[f for f in findings if f.severity in {"critical", "high"}],
        represented_site_checks=represented_site_checks,
        sources=sources,
        banner=DRAFT_BANNER,
        profile=profile,
        connections=connections,
        presence_sites=presence_sites,
        assessed_presence=assessed_presence,
        security_sections={k: t for k, t in CATEGORIES.items() if k in security_categories},
        web_sections={
            k: t
            for k, t in CATEGORIES.items()
            if k in shown & public_categories and k not in LOCAL | {"security", "domain_email"}
        },
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
        show_verification=bool(
            connections
            or discovery
            or data.get("needs_review")
            or data.get("finding_history")
            or data.get("limitations")
            or any(f.confidence != "confirmed" for f in findings)
            or any(e.source in {"browser", "site_checks"} for e in evidence)
        ),
    )
    return content


def write_outputs(run_dir: Path, content: str, formats: set[str]) -> list[str]:
    written = []
    targets = {ext: output_path(run_dir, f"report.{ext}") for ext in formats}
    if "md" in formats:
        targets["md"].write_text(content)
        written.append("report.md")
    body = markdown_html(content)
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
