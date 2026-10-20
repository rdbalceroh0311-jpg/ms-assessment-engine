import html
import json
import re
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.sync_api import sync_playwright

from ams_assess.ai import PHASES
from ams_assess.models import CATEGORIES, SEVERITIES, Evidence, Finding

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
    formats = formats or {"md", "html", "pdf"}
    data = json.loads((run_dir / "results.json").read_text())
    evidence = [
        Evidence.model_validate(e) for e in json.loads((run_dir / "evidence.json").read_text())
    ]
    findings = [Finding.model_validate(f) for f in data["findings"]]
    known = {e.id for e in evidence}
    if any(not set(f.evidence_ids) <= known for f in findings):
        raise ValueError("Report refused: a finding references missing evidence")
    findings.sort(key=lambda f: (f.priority, SEVERITIES[f.severity], f.category, f.id))
    by_id = {f.id: f for f in findings}
    ai = data["ai"]
    proposed = ai.get("recommendations", [])
    order = list(dict.fromkeys([i for i in proposed if i in by_id] + [f.id for f in findings]))
    roadmap = {phase: [] for phase in PHASES}
    assigned = set()
    for phase in PHASES:
        for fid in ai.get("roadmap", {}).get(phase, []):
            if fid in by_id and fid not in assigned:
                roadmap[phase].append(by_id[fid])
                assigned.add(fid)
    for finding in findings:
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
        sections={k: title for k, title in CATEGORIES.items() if k in shown},
        recommendations=[by_id[i] for i in order],
        roadmap=roadmap,
        scope_titles=[CATEGORIES[c] for c in data["selected_categories"]],
        collected_sources={e.source for e in evidence},
        browser_observations=sum(e.source == "browser" for e in evidence),
    )
    written = []
    if "md" in formats:
        (run_dir / "report.md").write_text(content)
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
        (run_dir / "report.html").write_text(document)
        written.append("report.html")
    if "pdf" in formats:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content(document, wait_until="load")
                page.pdf(
                    path=str(run_dir / "report.pdf"),
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
