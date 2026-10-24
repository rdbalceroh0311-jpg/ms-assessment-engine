import html
import json
import re
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markdown.inlinepatterns import InlineProcessor
from markdown.treeprocessors import Treeprocessor
from playwright.sync_api import sync_playwright

from ams_assess.ai import PHASES
from ams_assess.connectors import REGISTRY as CONNECTORS
from ams_assess.connectors.service import project_presence
from ams_assess.consolidate import consolidate
from ams_assess.discovery import DISCOVERY_SECTIONS
from ams_assess.lifecycle import site_key
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
    rows = [
        {
            "key": r["key"],
            "service": r["label"],
            "target": data.get("target", ""),
            "status": r["status"],
            "detail": r["detail"],
            "evidence_ids": [e["evidence_id"] for e in r["coverage"]],
        }
        for r in data.get("connector_rows", [])
    ]
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
            rows.append(
                {
                    "key": r["key"],
                    "service": r["service"],
                    "target": target,
                    "status": r["access"] if r["key"] in {"gsc", *CONNECTORS} else "Unsupported",
                    "detail": r["insights"],
                    "evidence_ids": r["evidence_ids"],
                }
            )
    for row in rows:
        if row["key"] == "gsc" and AI_REPORT_NOTE not in row["detail"]:
            row["detail"] += ". " + AI_REPORT_NOTE
    return rows


LAB_CHECKS = {"lighthouse-mobile", "lighthouse-desktop"}


def rule_key(check_key: str) -> str:
    """The repeated check of a per-page key: lighthouse-mobile:<page> -> lighthouse-mobile."""
    prefix = check_key.split(":", 1)[0]
    return prefix if prefix in LAB_CHECKS else check_key


def finding_views(findings: list[Finding]) -> tuple[list[Finding], dict[str, list[Finding]]]:
    """Group repeated rule observations for display only; every original remains a member."""
    groups: dict[tuple, list[Finding]] = {}
    for f in findings:
        site = site_key(f.site if "://" in f.site else f"//{f.site}") or site_key(f.component)
        # Legacy runs may record only URLs. Unknown scopes must not merge unrelated findings.
        sites = (site,) if site else tuple(sorted({site_key(url) for url in f.urls} - {""}))
        repeated_rule = f.category == "accessibility" or rule_key(f.check_key) in {
            "canonical-mismatch",
            "console-errors",
            *LAB_CHECKS,
        }
        identity = (
            (
                sites or (f.id,),
                f.category,
                rule_key(f.check_key),
                f.lifecycle_status,
                f.provenance,
                () if f.check_key.startswith("axe:") else (f.kind, f.basis, f.ams_service),
            )
            if repeated_rule and f.check_key
            else (f.id,)
        )
        groups.setdefault(identity, []).append(f)
    views, members = [], {}
    for rows in groups.values():
        f = rows[0].model_copy(deep=True)
        for field in ("evidence_ids", "urls", "dependencies", "information_required"):
            setattr(f, field, list(dict.fromkeys(v for r in rows for v in getattr(r, field))))
        for field in ("recommendation", "business_impact", "likely_cause", "urgency"):
            setattr(
                f,
                field,
                " ".join(dict.fromkeys(getattr(r, field) for r in rows if getattr(r, field))),
            )
        f.severity = min((r.severity for r in rows), key=SEVERITIES.__getitem__)
        f.priority = min(r.priority for r in rows)
        f.confidence = max(
            (r.confidence for r in rows),
            key={
                "confirmed": 0,
                "needs_verification": 1,
                "subjective": 2,
            }.__getitem__,
        )
        views.append(f)
        members[f.id] = rows
    return views, members


def evidence_index(evidence: list[Evidence]) -> list[dict[str, Any]]:
    """Print each observation description and URL once, retaining all evidence IDs."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in evidence:
        group = groups.setdefault(
            (e.source, e.description, e.provenance),
            {
                "source": e.source,
                "description": e.description,
                "provenance": e.provenance,
                "urls": {},
            },
        )
        group["urls"].setdefault(e.url, []).append(e.id)
    return list(groups.values())


def counted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical coverage records without hiding their occurrence count."""
    counts = Counter(json.dumps(r, sort_keys=True) for r in rows)
    return [{"row": json.loads(row), "count": count} for row, count in counts.items()]


def site_summaries(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One latest summary per canonical website; earlier scope/limitations remain explicit."""
    latest: dict[str, dict[str, Any]] = {}
    history = []

    def run_number(row):
        match = re.fullmatch(r"r(\d+)", row.get("run_id", ""))
        return int(match[1]) if match else 0

    for row in sorted(rows, key=run_number):
        identity = site_key(row.get("target", ""))
        if identity in latest:
            history.append(latest[identity])
        latest[identity] = row
    return list(latest.values()), history


def coverage_groups(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    groups = {}
    for row in coverage["discovered"]:
        url = row["url"]
        identity = (
            tuple(row["sources"]),
            *(
                url in coverage[key]
                for key in (
                    "crawled",
                    "browser_tested",
                    "untested",
                    "sitemap_not_reached",
                )
            ),
        )
        group = groups.setdefault(
            identity,
            {
                "sources": row["sources"],
                "crawled": identity[1],
                "tested": identity[2],
                "untested": identity[3],
                "unreached": identity[4],
                "urls": [],
            },
        )
        group["urls"].append(url)
    return list(groups.values())


def register_actions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = {}
    for r in entries:
        group = groups.setdefault(
            (r["next_action"], r["responsible"]),
            {
                "action": r["next_action"],
                "responsible": r["responsible"],
                "scopes": {},
            },
        )
        group["scopes"].setdefault((r["site"], r["location"]), []).append(r["label"])
    return list(groups.values())


def register_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = {}
    for row in rows:
        shared = {k: v for k, v in row.items() if k not in {"date", "reference"}}
        group = groups.setdefault(json.dumps(shared, sort_keys=True), {**shared, "citations": []})
        citation = {"date": row["date"], "reference": row["reference"]}
        if citation not in group["citations"]:
            group["citations"].append(citation)
    return list(groups.values())


def url_groups(urls: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Factor repeated origins out of coverage lists; every link keeps its full URL."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for url in urls:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
        path = url.removeprefix(origin) or "/"
        groups.setdefault(origin, []).append((path, url))
    return groups


def standard_requirements(findings: list[Finding], field: str) -> list[str]:
    values = Counter(tuple(getattr(f, field)) for f in findings)
    return list(values.most_common(1)[0][0]) if values else []


CSS = """@page { size: A4; margin: 16mm 15mm 18mm; }
body { font-family: Arial, sans-serif; color: #20303d; font-size: 9.5pt; line-height: 1.25;
       max-width: 960px; margin: 32px auto; padding: 0 20px; }
h1 { color: #143c52; font-size: 29pt; line-height: 1.12; border-bottom: 4px solid #16a59b;
     padding-bottom: 14px; margin-bottom: 20px; }
h2 { color: #143c52; font-size: 16pt; margin-top: 26px; border-bottom: 1px solid #cbd8df;
     padding-bottom: 6px; break-after: avoid; }
h3 { color: #195b68; font-size: 12pt; margin-top: 20px; break-after: avoid; }
h4 { break-after: avoid; }
h3 + p, h3 + p + p { break-after: avoid; }
p,li { orphans: 3; widows: 3; overflow-wrap: anywhere; }
p { margin: 6px 0; } ul,ol { margin: 6px 0; padding-left: 20px; }
a { color: #166c7c; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; font-size: 8pt; table-layout: fixed; }
table.coverage th:first-child { width: 58%; }
table.lifecycle th:first-child { width: 16%; }
table.sources th:first-child { width: 30%; }
table.sources th:nth-child(2) { width: 14%; }
table.register th:first-child, table.discovery th:first-child { width: 32%; }
table.signals th:first-child { width: 22%; }
table.signals th:nth-child(2) { width: 12%; }
table.signals th:nth-child(3) { width: 46%; }
table.infrastructure th:first-child { width: 17%; }
table.infrastructure th:nth-child(2) { width: 12%; }
table.infrastructure th:nth-child(3) { width: 41%; }
th,td { border: 1px solid #cfdae0; padding: 4px; text-align: left; vertical-align: top;
        overflow-wrap: anywhere; }
th { background: #eaf2f5; } tr { break-inside: avoid; }
code { font-size: 8pt; overflow-wrap: anywhere; }
h2#appendix-evidence-index ~ p, h2#appendix-evidence-index ~ ul {
    font-size: 8.5pt; line-height: 1.25; margin-top: 2px; margin-bottom: 2px; }
blockquote { margin: 15px 0; padding: 8px 14px; border-left: 4px solid #16a59b;
 background: #f3f8fa; }
@media print { body { margin: 0; padding: 0; max-width: none; }
  h2 { margin-top: 22px; } a { text-decoration: none; } }
"""


def mdtext(value: object) -> str:
    value = html.unescape(" ".join(str(value).split()))
    return re.sub(r"([\\`*_{}\[\]#|<>])", r"\\\1", value)


def mdlink(label: str, url: str) -> str:
    """Keep long search URLs clickable without repeating the query in visible text."""
    try:
        parts = urlsplit(url)
        if parts.scheme in {"https", "http"} and parts.hostname and not parts.username:
            return f"[{mdtext(label)}]({quote(url, safe=':/?&=#%+-._~')})"
    except ValueError:
        pass
    return f"{mdtext(label)} ({mdtext(url)})"


class ReportLineBreak(InlineProcessor):
    def handleMatch(self, m, data):
        return ElementTree.Element("br"), m.start(0), m.end(0)


class ReportTableLayout(Treeprocessor):
    def run(self, root):
        layouts = {
            "Known URL": "coverage",
            "State": "lifecycle",
            "Source / website / date": "sources",
            "Platform / website / location": "register",
            "Platform / relevance / state": "discovery",
            "Signal": "signals",
            "Area": "infrastructure",
        }
        for table in root.iter("table"):
            header = table.find("./thead/tr/th")
            if header is not None and header.text in layouts:
                table.set("class", layouts[header.text])


def markdown_html(content: str) -> str:
    # HTML escaping belongs here, not in the editable Markdown master. Disable raw HTML
    # (including in edited reports); retain only the template's attribute-free line breaks.
    renderer = markdown.Markdown(extensions=["tables", "toc"])
    renderer.preprocessors.deregister("html_block")
    renderer.inlinePatterns.deregister("html")
    renderer.ESCAPED_CHARS = [*renderer.ESCAPED_CHARS, "<", ">"]
    renderer.inlinePatterns.register(ReportLineBreak(r"<br\s*/?>"), "report-br", 175)
    renderer.treeprocessors.register(ReportTableLayout(renderer), "report-tables", 5)
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
        from ams_assess import register

        register.refresh(store)
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
        if content is None:
            data, evidence, discovery = report_data(store)
            content = render_markdown(data, evidence, discovery)
        # Publish a revision only after every requested format has rendered successfully.
        # TemporaryDirectory also removes partial outputs if PDF generation fails.
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=reports) as staging:
            written = write_outputs(Path(staging), content, formats)
            Path(staging).rename(revision)
        try:
            for name in written:
                (revision / name).chmod(0o400)  # a revision is never rewritten
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
        except Exception:
            shutil.rmtree(revision)
            raise
        # Keep the previous latest copies intact if persistence fails. Once committed,
        # the immutable revision remains available even if a convenience copy fails.
        for name in written:
            output_path(directory, name).write_bytes((revision / name).read_bytes())
            output_path(directory, name).chmod(0o600)
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
    if any(
        not set(f.evidence_ids) <= known
        or (f.resolution and not set(f.resolution.get("evidence_ids", [])) <= known)
        for f in findings
    ):
        raise ValueError("Report refused: a finding references missing evidence")
    findings.sort(key=lambda f: (f.priority, SEVERITIES[f.severity], f.category, f.id))
    # Findings whose basis changed or conflicts belong only in section 10.
    review = {
        fid
        for r in data.get("needs_review", [])
        for fid in [r.get("finding_id"), *r.get("finding_ids", [])]
        if fid
    }
    technical_findings = [
        f
        for f in findings
        if f.provenance == "technical_evidence"
        and f.category != "discovery"
        and (f.site or f.component or f.lifecycle_status != "current")
    ]
    lifecycle_findings, lifecycle_members = finding_views(
        [f for f in technical_findings if f.lifecycle_status != "current"]
    )
    lifecycle_domains = {
        f.id: f.site
        or next(
            (
                e.url.removeprefix("dns:")
                for e in evidence
                if e.id in f.evidence_ids and e.url.startswith("dns:")
            ),
            site_key(data.get("target", "")) or "Domain not recorded",
        )
        for f in technical_findings
    }
    current = [f for f in findings if f.id not in review and f.lifecycle_status == "current"]
    findings, members = finding_views(current)
    aliases = {f.id: group_id for group_id, rows in members.items() for f in rows}
    by_id = {f.id: f for f in findings}
    site_finding_eids = {
        eid for f in findings if f.check_key.startswith("site:") for eid in f.evidence_ids
    }
    represented_site_checks = {
        (e.url, e.data["site_check"])
        for e in evidence
        if e.id in site_finding_eids
        and e.source == "site_checks"
        and isinstance(e.data, dict)
        and e.data.get("site_check")
    }
    ai = data["ai"]
    proposed = ai.get("recommendations", [])
    order = list(dict.fromkeys([aliases[i] for i in proposed if i in aliases] + list(by_id)))
    roadmap = {phase: [] for phase in PHASES}
    assigned = set()
    for phase in PHASES:
        for fid in ai.get("roadmap", {}).get(phase, []):
            fid = aliases.get(fid, fid)
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
    from ams_assess.figures import customer_statement

    env.filters["customer_statement"] = customer_statement
    env.filters["text"] = mdtext
    env.filters["discovery_rows"] = discovery_rows
    env.filters["counted_rows"] = counted_rows
    env.filters["coverage_groups"] = coverage_groups
    env.filters["site_key"] = site_key
    env.filters["register_actions"] = register_actions
    env.filters["register_observations"] = register_observations
    env.filters["link"] = mdlink
    env.filters["url_groups"] = url_groups
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
    if any(
        e.source == "performance" and isinstance(e.data, dict) and e.data.get("performance")
        for e in evidence
    ):
        shown.add("performance")
    connected_ids = {
        e.id
        for e in evidence
        if e.source == "gsc" or (isinstance(e.data, dict) and e.data.get("connector"))
    }
    connected = [f for f in findings if connected_ids.intersection(f.evidence_ids)]
    public = [f for f in findings if f not in connected]
    sources = {e.id: e.source for e in evidence}
    env.filters["source_name"] = sources.__getitem__
    # Latest coverage per website; earlier runs (and www/apex aliases) are summarised.
    coverage_sites, coverage_history = site_summaries(data.get("website_coverage_runs", []))
    earlier_coverage: dict[str, list[dict[str, Any]]] = {}
    for row in coverage_history:
        earlier_coverage.setdefault(site_key(row.get("target", "")), []).append(row)
    presence_sites = data.get("digital_presence_sites") or [data.get("digital_presence")]
    presence_sites = [p for p in presence_sites if p]
    for summary in presence_sites:
        project_presence(summary, data.get("connector_rows", []))
    presence_sites, earlier_presence = site_summaries(presence_sites)
    health_sites, earlier_health = site_summaries(
        data.get("domain_email_health_sites")
        or ([data["domain_email_health"]] if data.get("domain_email_health") else [])
    )
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
    connections = connection_rows({**data, "digital_presence_sites": presence_sites})
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
        evidence_index=evidence_index(evidence),
        findings=findings,
        finding_count=len(current),
        finding_members=members,
        standard_dependencies=standard_requirements(findings, "dependencies"),
        standard_information=standard_requirements(findings, "information_required"),
        technical_count=len(technical_findings),
        lifecycle_findings=lifecycle_findings,
        lifecycle_members=lifecycle_members,
        lifecycle_domains=lifecycle_domains,
        public_findings=public,
        finding_categories={f.category for f in public},
        connector_rows=data.get("connector_rows", []),
        connected_findings=connected,
        connected_evidence=[e for e in evidence if e.id in connected_ids],
        critical_findings=[f for f in findings if f.severity in {"critical", "high"}],
        represented_site_checks=represented_site_checks,
        sources=sources,
        banner=DRAFT_BANNER,
        profile=profile,
        connections=connections,
        presence_sites=presence_sites,
        coverage_sites=coverage_sites,
        earlier_coverage=earlier_coverage,
        health_sites=health_sites,
        earlier_presence=earlier_presence,
        earlier_health=earlier_health,
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
        browser_observations=sum(
            e.source == "browser" and isinstance(e.data, dict) and bool(e.data.get("page"))
            for e in evidence
        ),
        show_verification=bool(
            connections
            or data.get("systems_register")
            or discovery
            or data.get("needs_review")
            or data.get("finding_history")
            or data.get("limitations")
            or any(f.confidence != "confirmed" for f in findings)
            or any(e.source in {"browser", "site_checks"} for e in evidence)
        ),
    )
    return content.rstrip() + "\n"


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
                    margin={"top": "16mm", "bottom": "18mm", "left": "15mm", "right": "15mm"},
                )
            finally:
                browser.close()
        written.append("report.pdf")
    return written
