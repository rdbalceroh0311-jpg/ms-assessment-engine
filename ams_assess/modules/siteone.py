import json
import re
import subprocess
from urllib.parse import urljoin, urlsplit

from ams_assess.models import Context, ModuleResult
from ams_assess.targets import crawl_hosts, resolve_target

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
    "presence",
}
requires = ["tools/siteone/siteone-crawler/siteone-crawler"]


def run(ctx: Context) -> ModuleResult:
    result = ModuleResult(name=name)
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    if not binary.is_file():
        return ModuleResult(
            name=name, status="skipped", reason="SiteOne missing; run scripts/setup.sh"
        )
    resolution = resolve_target(ctx.target)
    result.raw_paths = [ctx.raw("target-resolution", resolution)]
    result.add_evidence(ctx.target, "Target redirect resolution before crawling", resolution)
    if not resolution["resolved"]:
        result.status, result.reason = "error", resolution["error"]
        return result
    hosts = crawl_hosts(ctx.target, resolution["resolved"])
    ctx.target = resolution["resolved"]
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
        *(f"--allowed-domain-for-crawling={host}" for host in sorted(hosts)),
    ]
    process = subprocess.run(
        command,
        cwd=ctx.run_dir,
        capture_output=True,
        text=True,
        timeout=max(120, min(ctx.max_pages * 10, 1800)),
    )
    (ctx.run_dir / "raw/siteone.stderr.txt").write_text(process.stderr)
    result.raw_paths += ["raw/siteone.json", "raw/siteone.stderr.txt"]
    (ctx.run_dir / "raw/siteone.json").write_text(process.stdout)
    try:
        data = json.loads(process.stdout)
    except ValueError:
        data = {}
    rows = data.get("results", [])
    if len(rows) == 1 and 300 <= int(rows[0].get("status", 0)) < 400:
        result.add_evidence(ctx.target, "SiteOne crawled only a redirect", rows[0])
        result.status = "error"
        result.reason = (
            f"SiteOne crawled only a redirect (exit {process.returncode}); "
            "no HTML pages assessed. Verify the canonical host and redirect configuration."
        )
        return result
    if process.returncode:
        result.status, result.reason = (
            "error",
            f"SiteOne exited {process.returncode}; see raw output",
        )
        return result
    for row in rows:
        url = row["url"]
        status = int(row.get("status", 0))
        if row.get("type") == 1 and 200 <= status < 300:
            if urlsplit(url).hostname in hosts:
                ctx.pages.append(url)
        eid = result.add_evidence(url, "SiteOne HTTP response", row)
        if status == 0 or status == 429 or status >= 500:
            # No response, throttling or server errors may be transient; recheck before repair.
            result.finding(
                "unavailable-urls",
                "Unavailable or throttled URLs",
                "technical",
                [eid],
                f"HTTP {status} at {url}." if status else f"No response at {url}.",
                "Recheck these URLs; restore or redirect any that remain unavailable.",
                severity="high",
                confidence="needs_verification",
                urls=[url],
            )
        elif status >= 400:
            result.finding(
                "broken-links",
                "Broken URLs",
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
    for item in data.get("summary", {}).get("items", []):
        code, text = str(item.get("aplCode", "")), str(item.get("text", ""))
        if code.startswith("robots-txt-"):
            eid = result.add_evidence(ctx.target, "SiteOne robots.txt load", item)
            status = re.search(r"status code (\d+)", text)
            if status and status.group(1) != "200":
                result.finding(
                    "robots-txt-missing",
                    "robots.txt not available",
                    "seo",
                    [eid],
                    text,
                    "Publish a robots.txt that allows public pages and declares the XML sitemap.",
                    severity="low",
                    confidence="needs_verification",
                    urls=[ctx.target],
                )
        elif code == "seo-canonical-missing":
            eid = result.add_evidence(ctx.target, "SiteOne canonical link summary", item)
            if item.get("status") != "OK":
                result.finding(
                    "canonical-missing",
                    "Pages without a canonical link",
                    "seo",
                    [eid],
                    text,
                    "Add a self-referencing rel=canonical to each indexable page.",
                    severity="low",
                    kind="opportunity",
                    urls=[ctx.target],
                )
    for table in [
        "seo-headings",
        "redirects",
        "404",
        "headers",
        "headers-values",
        "external-urls",
    ]:
        if tables.get(table, {}).get("rows"):
            result.add_evidence(ctx.target, f"SiteOne {table} table", tables[table]["rows"])
    if not ctx.pages:
        result.status, result.reason = "error", "SiteOne returned no successful HTML pages"
    elif len(rows) >= ctx.max_pages:
        result.reason = f"Crawl URL cap ({ctx.max_pages}) reached; coverage may be incomplete"
    return result
