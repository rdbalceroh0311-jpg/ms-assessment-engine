import json
import subprocess
from urllib.parse import urljoin, urlsplit

from ams_assess.models import Context, ModuleResult

name = "siteone"
categories = {
    "technical",
    "seo",
    "geo",
    "local_seo",
    "entity",
    "cro_ux",
    "copywriting",
    "accessibility",
}
requires = ["tools/siteone/siteone-crawler/siteone-crawler"]


def run(ctx: Context) -> ModuleResult:
    result = ModuleResult(name=name)
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    if not binary.is_file():
        return ModuleResult(
            name=name, status="skipped", reason="SiteOne missing; run scripts/setup.sh"
        )
    command = [
        str(binary),
        f"--url={ctx.target}",
        "--output=json",
        "--no-color",
        "--no-cache",
        f"--max-visited-urls={ctx.max_pages}",
        f"--rows-limit={ctx.max_pages}",
        "--workers=2",
        "--max-reqs-per-sec=5",
        "--timeout=8",
        "--ignore-html-comments",
        "--output-html-report=",
        "--output-json-file=",
        "--output-text-file=",
    ]
    process = subprocess.run(
        command,
        cwd=ctx.run_dir,
        capture_output=True,
        text=True,
        timeout=max(120, min(ctx.max_pages * 10, 1800)),
    )
    (ctx.run_dir / "raw/siteone.stderr.txt").write_text(process.stderr)
    result.raw_paths = ["raw/siteone.json", "raw/siteone.stderr.txt"]
    (ctx.run_dir / "raw/siteone.json").write_text(process.stdout)
    if process.returncode:
        result.status, result.reason = (
            "error",
            f"SiteOne exited {process.returncode}; see raw output",
        )
        return result
    data = json.loads(process.stdout)
    rows = data.get("results", [])
    for row in rows:
        url = row["url"]
        status = int(row.get("status", 0))
        if row.get("type") == 1 and 200 <= status < 300:
            if urlsplit(url).netloc == urlsplit(ctx.target).netloc:
                ctx.pages.append(url)
        eid = result.add_evidence(url, "SiteOne HTTP response", row)
        if status >= 400 or status == 0:
            result.finding(
                "broken-links",
                "Broken or unavailable URLs",
                "technical",
                [eid],
                f"HTTP {status} at {url}.",
                "Repair internal links and restore or redirect unavailable pages.",
                severity="high",
                urls=[url],
            )
        elif 300 <= status < 400:
            result.finding(
                "redirects",
                "Review internal redirects",
                "technical",
                [eid],
                f"HTTP {status} at {url}.",
                "Link directly to the final canonical URL.",
                severity="low",
                kind="opportunity",
                urls=[url],
            )
    ctx.pages = list(dict.fromkeys(ctx.pages))[: ctx.max_pages]
    tables = data.get("tables", {})
    for row in tables.get("seo", {}).get("rows", []):
        url = urljoin(ctx.target, row.get("urlPathAndQuery", "/"))
        eid = result.add_evidence(url, "SiteOne SEO metadata", row)
        for field, label in [("description", "meta description"), ("h1", "H1"), ("title", "title")]:
            if not str(row.get(field, "")).strip():
                result.finding(
                    f"missing-{field}",
                    f"Missing {label}",
                    "seo",
                    [eid],
                    f"No {label} recorded at {url}.",
                    f"Add a descriptive, page-specific {label}.",
                    urls=[url],
                )
        if str(row.get("robotsIndex")) == "0" or row.get("deniedByRobotsTxt") == "true":
            result.finding(
                "indexing-restricted",
                "Indexing restrictions need review",
                "seo",
                [eid],
                f"SiteOne recorded indexing restrictions at {url}.",
                "Confirm intended visibility before changing robots directives.",
                confidence="needs_verification",
                urls=[url],
            )
    for row in tables.get("security", {}).get("rows", []):
        if int(row.get("critical", 0)) + int(row.get("warning", 0)) == 0:
            continue
        header = row.get("header", "unknown").lower()
        # Deprecated headers are not remediation targets. The direct security check
        # evaluates X-Frame-Options together with its CSP frame-ancestors replacement.
        if header in {"feature-policy", "x-xss-protection", "x-frame-options"}:
            continue
        missing = "not set" in row.get("recommendation", "").lower()
        key = f"header-missing:{header}" if missing else f"header-review:{header}"
        eid = result.add_evidence(ctx.target, "SiteOne header analysis (crawl aggregate)", row)
        result.finding(
            key,
            f"Review {header} header",
            "security",
            [eid],
            row.get("recommendation", "Header policy needs review."),
            "Review and test the header policy for the affected responses.",
            confidence="needs_verification",
            severity="low" if ctx.local or header == "server" else "medium",
            urls=[ctx.target],
        )
    for table in ["seo-headings", "redirects", "404", "headers", "headers-values"]:
        if tables.get(table, {}).get("rows"):
            result.add_evidence(ctx.target, f"SiteOne {table} table", tables[table]["rows"])
    if not ctx.pages:
        result.status, result.reason = "error", "SiteOne returned no successful HTML pages"
    elif len(rows) >= ctx.max_pages:
        result.reason = f"Crawl URL cap ({ctx.max_pages}) reached; coverage may be incomplete"
    return result
