import json
import re
import subprocess
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from ams_assess.lifecycle import observe
from ams_assess.models import Context, ModuleResult
from ams_assess.targets import NORMAL_UA, REQUEST_SECONDS, crawl_hosts, resolve_target

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


def status_code(value: Any) -> int:
    """SiteOne serialises transport errors as e.g. '-4:SEND-ERROR', not HTTP codes."""
    try:
        return int(str(value).split(":", 1)[0])
    except ValueError:
        return 0


def diagnostic(value: Any, ctx: Context) -> str:
    """Keep provider diagnostics readable without reflecting URL secrets or credentials."""
    text = " ".join(str(value).split())

    def safe_url(match: re.Match[str]) -> str:
        try:
            parts = urlsplit(match.group())
            # Retain ports (including local fixture ports), but never userinfo/query/fragment.
            return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        except ValueError:
            return "[invalid URL]"

    text = re.sub(r"https?://\S+", safe_url, text)
    # Scheme-less paths/hosts can carry the same credentials, query and fragment secrets.
    text = re.sub(
        r"(?<![\w])(?:(?://)?(?:[^\s/@<>\"']+@)?"
        r"(?:localhost|(?:[\w-]+\.)+[\w-]+|\[[0-9a-fA-F:]+\])(?::\d+)?"
        r"(?:/[^\s<>\"'?#]*)?|(?:/|\./|\.\./|[\w.-]+/)[^\s<>\"'?#]+)"
        r"[?#][^\s<>\"']*",
        lambda m: m.group().split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1],
        text,
    )
    for secret in ctx.config.values():
        if len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return text[:1000]


def crawl_seeds(ctx: Context, result: ModuleResult) -> list[str]:
    """Known same-site pages from the sitemap/WordPress inventory, crawled besides the links."""
    from ams_assess import coverage  # coverage reuses this module's response parsing

    try:
        inventory = coverage.seed_inventory(ctx)
    except Exception as exc:
        # Inventory failure must not stop the link crawl; coverage reports the gap.
        reason = f"Crawl seeds unavailable ({type(exc).__name__}); link crawl only"
        result.add_evidence(
            ctx.site_url,
            "Crawl seeds from sitemap/WordPress inventory",
            {"urls": [], "reason": reason},
        )
        return []
    result.add_evidence(
        ctx.site_url,
        "Crawl seeds from sitemap/WordPress inventory",
        {
            "urls": inventory["seeds"],
            "limit": inventory["seed_limit"],
            "over_limit": len(inventory["seeds_over_limit"]),
            "reason": "Same-site HTML pages published in the sitemap or WordPress inventory, "
            "robots.txt applied; attachments, media and feeds excluded",
        },
    )
    return inventory["seeds"]


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
    if resolution.get("unsafe"):
        result.status, result.reason = "error", resolution["error"]
        return result
    ctx.site_url = resolution["resolved"] or resolution.get("last_safe_url", ctx.target)
    hosts = crawl_hosts(ctx.target, ctx.site_url)
    limitation = (
        f"Redirect preflight limitation: {resolution['error']}; crawling the last safe URL"
        if resolution.get("error")
        else ""
    )
    seeds = crawl_seeds(ctx, result)
    command = [
        str(binary),
        f"--url={ctx.site_url}",
        # Keep our exact tool identity; ! suppresses SiteOne's appended signature.
        f"--user-agent={NORMAL_UA}!",
        "--output=json",
        "--no-color",
        "--no-cache",
        "--disable-all-assets",
        f"--max-visited-urls={ctx.max_pages}",
        f"--rows-limit={ctx.max_pages}",
        "--workers=2",
        "--max-reqs-per-sec=5",
        f"--timeout={REQUEST_SECONDS}",
        "--ignore-html-comments",
        "--output-html-report=",
        "--output-json-file=",
        "--output-text-file=",
        *(f"--allowed-domain-for-crawling={host}" for host in sorted(hosts)),
    ]
    if seeds:
        # SiteOne URL-list seeds bypass its robots check, including the first URL.
        # Retain the start URL as the crawl base only when robots allows it too.
        listing = ctx.run_dir / "tmp/siteone-seeds.txt"
        listing.parent.mkdir(parents=True, exist_ok=True)
        start = [ctx.site_url] if ctx.inventory.get("target_allowed", True) else []
        listing.write_text("\n".join([*start, *seeds]) + "\n")
        command.append(f"--url-list={listing}")
    try:
        process = subprocess.run(
            command,
            cwd=ctx.run_dir,
            capture_output=True,
            text=True,
            timeout=max(120, min(ctx.max_pages * 10, 1800)),
        )
        result.collection_started = True
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.collection_started = isinstance(exc, subprocess.TimeoutExpired)
        result.status = "error"
        result.reason = f"SiteOne could not crawl {ctx.site_url}: {type(exc).__name__}"
        result.add_evidence(ctx.site_url, "SiteOne process failure", {"reason": result.reason})
        return result
    (ctx.run_dir / "raw/siteone.stderr.txt").write_text(process.stderr)
    result.raw_paths += ["raw/siteone.json", "raw/siteone.stderr.txt"]
    (ctx.run_dir / "raw/siteone.json").write_text(process.stdout)
    try:
        data = json.loads(process.stdout)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    rows = data.get("results", [])
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if len(rows) == 1 and 300 <= status_code(rows[0].get("status")) < 400:
        result.add_evidence(ctx.site_url, "SiteOne crawled only a redirect", rows[0])
        result.status = "error"
        result.reason = (
            f"SiteOne crawled only a redirect (exit {process.returncode}); "
            "no HTML pages assessed. Verify the canonical host and redirect configuration."
        )
        return result
    successful = any(r.get("type") == 1 and 200 <= status_code(r.get("status")) < 300 for r in rows)
    if process.returncode or not successful:
        failure = next((r for r in rows if not 200 <= status_code(r.get("status")) < 300), {})
        error = data.get("error") or "No successful HTML pages"
        message = diagnostic("; ".join(map(str, error)) if isinstance(error, list) else error, ctx)
        result.status = "error"
        result.reason = (
            f"SiteOne could not fetch {diagnostic(failure.get('url', ctx.site_url), ctx)}: "
            f"{diagnostic(failure.get('status', 'no response status'), ctx)} "
            f"(exit {process.returncode}); {message}"
        )
        if limitation:
            result.reason += "; " + limitation
        result.add_evidence(
            ctx.site_url,
            "SiteOne crawl failed",
            {
                "exit_code": process.returncode,
                "responses": rows,
                "error": message,
                "reason": result.reason,
            },
        )
        return result
    for row in rows:
        url = row["url"]
        status = status_code(row.get("status"))
        if row.get("type") == 1 and 200 <= status < 300:
            if urlsplit(url).hostname in hosts:
                ctx.pages.append(url)
        eid = result.add_evidence(url, "SiteOne HTTP response", row)
        for check, failed in [
            ("unavailable-urls", status <= 0 or status == 429 or status >= 500),
            ("broken-links", 400 <= status < 500 and status != 429),
            ("redirects", 300 <= status < 400),
        ]:
            outcome = "fail" if failed else ("pass" if 200 <= status < 300 else "unavailable")
            result.outcomes.append(observe(name, check, "technical", url, outcome, [eid]))
        if status <= 0 or status == 429 or status >= 500:
            # No response, throttling or server errors may be transient; recheck before repair.
            result.finding(
                "unavailable-urls",
                "Unavailable or throttled URLs",
                "technical",
                [eid],
                f"HTTP {status} at {url}." if status > 0 else f"No response at {url}.",
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
        url = urljoin(ctx.site_url, row.get("urlPathAndQuery", "/"))
        eid = result.add_evidence(url, "SiteOne SEO metadata", row)
        for field, label in [("description", "meta description"), ("h1", "H1"), ("title", "title")]:
            result.outcomes.append(
                observe(
                    name,
                    f"missing-{field}",
                    "seo",
                    url,
                    ("fail" if not str(row[field]).strip() else "pass")
                    if field in row
                    else "unavailable",
                    [eid],
                )
            )
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
        restricted = str(row.get("robotsIndex")) == "0" or row.get("deniedByRobotsTxt") == "true"
        result.outcomes.append(
            observe(
                name,
                "indexing-restricted",
                "seo",
                url,
                "fail" if restricted else ("pass" if "robotsIndex" in row else "unavailable"),
                [eid],
            )
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
        # Aggregate counts do not identify a cookie flag defect or a disclosed version.
        # The direct security collector reports those only from concrete response values.
        if header in {"set-cookie", "server"}:
            continue
        missing = "not set" in row.get("recommendation", "").lower()
        key = f"header-missing:{header}" if missing else f"header-review:{header}"
        eid = result.add_evidence(ctx.site_url, "SiteOne header analysis (crawl aggregate)", row)
        result.finding(
            key,
            f"Review {header} header",
            "security",
            [eid],
            row.get("recommendation", "Header policy needs review."),
            "Review and test the header policy for the affected responses.",
            confidence="needs_verification",
            coverage=list(ctx.pages),
            severity="low"
            if ctx.local or header in {"referrer-policy", "permissions-policy"}
            else "medium",
            urls=[ctx.site_url],
        )
    header_rows = tables.get("security", {}).get("rows", [])
    header_ids = []
    healthy = set()
    for row in header_rows:
        header = str(row.get("header", "")).lower()
        if "critical" not in row or "warning" not in row:
            continue
        eid = result.add_evidence(ctx.site_url, "SiteOne header outcome (crawl aggregate)", row)
        header_ids.append(eid)
        passed = int(row["critical"]) + int(row["warning"]) == 0
        if passed:
            healthy.add(header)
        for prefix in ("header-missing:", "header-review:"):
            # Failure type is represented by the finding. A non-missing header is not
            # automatically a pass for every policy rule.
            if passed:
                result.outcomes.append(
                    observe(
                        name,
                        prefix + header,
                        "security",
                        ctx.site_url,
                        "pass",
                        [eid],
                        coverage=list(ctx.pages),
                    )
                )
    required = {
        "content-security-policy",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
        "x-frame-options",
    }
    if ctx.site_url.startswith("https:"):
        required.add("strict-transport-security")
    if required <= healthy:
        result.outcomes.append(
            observe(
                name,
                "security-headers",
                "security",
                ctx.site_url,
                "pass",
                header_ids,
                coverage=list(ctx.pages),
            )
        )
    for item in data.get("summary", {}).get("items", []):
        code, text = str(item.get("aplCode", "")), str(item.get("text", ""))
        if code.startswith("robots-txt-"):
            eid = result.add_evidence(ctx.site_url, "SiteOne robots.txt load", item)
            status = re.search(r"status code (\d+)", text)
            if status:
                result.outcomes.append(
                    observe(
                        name,
                        "robots-txt-missing",
                        "seo",
                        ctx.site_url,
                        "pass" if status.group(1) == "200" else "fail",
                        [eid],
                    )
                )
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
                    urls=[ctx.site_url],
                )
        elif code == "seo-canonical-missing":
            eid = result.add_evidence(ctx.site_url, "SiteOne canonical link summary", item)
            result.outcomes.append(
                observe(
                    name,
                    "canonical-missing",
                    "seo",
                    ctx.site_url + "::crawl-canonicals",
                    "pass" if item.get("status") == "OK" else "fail",
                    [eid],
                    coverage=list(ctx.pages),
                )
            )
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
                    component=ctx.site_url + "::crawl-canonicals",
                    coverage=list(ctx.pages),
                    urls=[ctx.site_url],
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
            result.add_evidence(ctx.site_url, f"SiteOne {table} table", tables[table]["rows"])
    if not ctx.pages:
        result.status, result.reason = "error", "SiteOne returned no successful HTML pages"
    elif len(rows) >= ctx.max_pages:
        result.reason = (
            f"{len(ctx.pages)} successful HTML pages; page crawl budget {ctx.max_pages} URLs "
            "with static assets excluded. "
            "Redirects and failed/non-HTML responses can consume slots."
        )
        result.reason += " Crawl cap reached; coverage may be incomplete."
    if limitation:
        result.reason = "; ".join(filter(None, [result.reason, limitation]))
    return result
