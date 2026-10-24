"""Bounded public inventories and reconciliation; known URLs are never the whole website."""

import csv
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from ams_assess.models import Context
from ams_assess.modules import presence, siteone
from ams_assess.targets import NORMAL_UA

MAX_SITEMAPS = 10
MAX_URLS = 5000
MAX_WP_PAGES = 10
INVENTORY_SECONDS = 10
REQUEST_INTERVAL = 0.2
UNAVAILABLE = "inventory unavailable — authorised export or read-only access needed"


def public_url(value: Any, base: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urldefrag(urljoin(base, value.strip()))[0]
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.username or parts.password:
            return None
        if presence.origin_of(url) != presence.origin_of(base):
            return None
        return url
    except ValueError:
        return None


def load_export(path: Path) -> list[str]:
    """The flag is explicit authorisation to read this URL-only JSON/CSV export."""
    if path.stat().st_size > presence.SITEMAP_MAX_BYTES:
        raise ValueError("WordPress export exceeds 2 MB")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        rows: Any = list(csv.DictReader(io.StringIO(text)))
    elif path.suffix.lower() == ".json":
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("urls")
    else:
        raise ValueError("WordPress inventory must be JSON or CSV")
    if not isinstance(rows, list) or len(rows) > MAX_URLS:
        raise ValueError("WordPress export must contain at most 5000 URLs")
    values = [r.get("url", r.get("link")) if isinstance(r, dict) else r for r in rows]
    if any(
        not isinstance(url, str) or not url.startswith(("http://", "https://")) for url in values
    ):
        raise ValueError("WordPress export entries must be HTTP(S) URLs")
    return values


def inventory_client(transport: httpx.BaseTransport | None) -> httpx.Client:
    options: dict[str, Any] = {
        "follow_redirects": False,
        "trust_env": False,
        "headers": {"User-Agent": NORMAL_UA},
    }
    if transport is not None:
        options["transport"] = transport
    return httpx.Client(**options)


def wp_inventory(ctx: Context, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + INVENTORY_SECONDS
    origin = presence.origin_of(ctx.site_url)
    base = urljoin(ctx.site_url, "/wp-json/")
    urls: list[str] = []
    requests: list[dict[str, Any]] = []
    reasons: list[str] = []
    pages = 0
    with inventory_client(transport) as client:

        def fetch(url: str) -> Any:
            if requests:
                time.sleep(min(REQUEST_INTERVAL, max(0, deadline - time.monotonic())))
            response = presence.fetch(client, url, origin, deadline)
            requests.append(
                {
                    "url": url,
                    "status": response.get("status"),
                    "error": response.get("error", ""),
                    "complete": response.get("complete", False),
                }
            )
            if response.get("status") != 200 or not response.get("complete"):
                reasons.append(UNAVAILABLE)
                return None
            try:
                return json.loads(response["body"])
            except (ValueError, UnicodeError):
                reasons.append("Invalid REST JSON; " + UNAVAILABLE)
                return None

        types = fetch(base + "wp/v2/types")
        if not isinstance(types, dict):
            return {
                "status": "unavailable",
                "urls": [],
                "requests": requests,
                "reason": UNAVAILABLE,
                "stop_reasons": reasons or [UNAVAILABLE],
            }
        endpoints: list[str] = []
        # Standard collections first, then public custom types advertised by /types.
        for kind, info in sorted(
            types.items(), key=lambda item: (item[0] not in {"page", "post"}, item[0])
        ):
            if not isinstance(info, dict) or info.get("viewable") is False:
                continue
            namespace = info.get("rest_namespace", "wp/v2")
            rest_base = info.get("rest_base") or {"page": "pages", "post": "posts"}.get(kind)
            if not isinstance(namespace, str) or not isinstance(rest_base, str):
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", namespace):
                reasons.append("Unsafe REST namespace excluded")
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", rest_base):
                reasons.append("Unsafe REST collection excluded")
                continue
            endpoint = base + namespace + "/" + rest_base
            if endpoint not in endpoints:
                endpoints.append(endpoint)
        if not endpoints:
            reasons.append(UNAVAILABLE)
        for endpoint in endpoints:
            page = 1
            while True:
                if pages >= MAX_WP_PAGES or time.monotonic() >= deadline:
                    reasons.append("WordPress page/time budget reached")
                    break
                rows = fetch(
                    endpoint + f"?status=publish&context=view&per_page=100&page={page}"
                    "&_fields=link,status"
                )
                pages += 1
                if not isinstance(rows, list):
                    if rows is not None:
                        reasons.append("Invalid REST collection; " + UNAVAILABLE)
                    break
                for row in rows:
                    if isinstance(row, dict) and row.get("status", "publish") == "publish":
                        if url := public_url(row.get("link"), ctx.site_url):
                            if url not in urls:
                                urls.append(url)
                if len(rows) < 100:
                    break
                page += 1
    return {
        "status": "partial" if reasons and urls else ("unavailable" if reasons else "sampled"),
        "urls": urls,
        "requests": requests,
        "reason": "; ".join(dict.fromkeys(reasons)) or "Advertised public collections exhausted",
        "stop_reasons": list(dict.fromkeys(reasons)),
        "page_limit": MAX_WP_PAGES,
        "seconds_limit": INVENTORY_SECONDS,
    }


def wp_cli_inventory(ctx: Context) -> dict[str, Any] | None:
    """Current-run bound evidence adds known URLs only, including successful partial pages."""
    state = ctx.connector_results.get("wp_cli", {})
    if state.get("operation") != "collect" or state.get("status") not in {
        "connected", "connected_partial"
    }:
        return None
    urls: set[str] = set()
    refs: list[str] = []
    counts: dict[str, Any] = {}
    reasons: list[str] = []
    operations: set[str] = set()
    for module in ctx.results:
        if module.name != "wp_cli":
            continue
        for evidence in module.evidence:
            item = evidence.data
            if (
                evidence.source != "wp_cli" or evidence.id not in state.get("evidence_ids", [])
                or not isinstance(item, dict) or item.get("connector") != "wp_cli"
                or item.get("resource_id") != state.get("resource_id")
                or item.get("operation") not in {"posts_post", "posts_page"}
            ):
                continue
            data = item.get("data", {})
            kind = item["operation"].removeprefix("posts_")
            if (data.get("inventory_source") != "wp_cli" or data.get("post_type") != kind
                    or data.get("post_status") != "publish"):
                continue
            operations.add(kind)
            refs.append(evidence.id)
            counts[kind] = {"publish": data.get("count", 0),
                            "lower_bound": data.get("count_is_lower_bound", True)}
            if (item.get("status") != "connected" or item.get("coverage", {}).get("truncated")
                    or data.get("count_is_lower_bound") or data.get("rejected_urls")):
                reasons.append("WP-CLI published inventory is partial; retained successful pages")
            for value in data.get("urls", [])[:MAX_URLS]:
                # Absolute same-origin links only; never turn an invalid relative value into home.
                if not isinstance(value, str) or not value.startswith(("https://", "http://")):
                    reasons.append("WP-CLI unsafe or outside-site inventory URL excluded")
                    continue
                url = public_url(value, ctx.site_url)
                if not url:
                    reasons.append("WP-CLI unsafe or outside-site inventory URL excluded")
                elif len(urls) < MAX_URLS:
                    urls.add(url)
                else:
                    reasons.append("WP-CLI known URL limit reached")
            if len(data.get("urls", [])) > MAX_URLS:
                reasons.append("WP-CLI known URL limit reached")
    if operations != {"post", "page"}:
        reasons.append("WP-CLI published post/page inventory incomplete or unavailable")
    return {
        "source": "wp_cli", "status": "partial" if reasons else "sampled",
        "urls": sorted(urls), "evidence_ids": refs, "counts": counts,
        "stop_reasons": list(dict.fromkeys(reasons)),
        "scope": "Published post/page snapshot only; custom/private types excluded. "
                 "Known URLs are not crawled or browser-tested pages.",
    }


def collect(ctx: Context, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    discovered: dict[str, set[str]] = {}
    known_limit = max(MAX_URLS, ctx.max_pages)
    excluded: list[dict[str, str]] = []
    stops: list[str] = []

    def add(value: Any, source: str, base: str | None = None) -> str | None:
        url = public_url(value, base or ctx.site_url)
        if not url:
            # Do not echo rejected userinfo or arbitrary API content into the report.
            excluded.append({"source": source, "reason": "Outside site or unsafe URL"})
        elif url in discovered or len(discovered) < known_limit:
            discovered.setdefault(url, set()).add(source)
            return url
        else:
            stops.append("Known URL limit reached")
        return None

    evidence = [e for r in ctx.results for e in r.evidence]
    # Failed/redirect-only crawls retain responses even when no HTML inventory is usable.
    extra = []
    for e in evidence:
        if e.source == "siteone" and isinstance(e.data, dict):
            rows = (
                e.data.get("responses", [])
                if e.description == "SiteOne crawl failed"
                else [e.data]
                if e.description == "SiteOne crawled only a redirect"
                else []
            )
            for row in rows:
                if isinstance(row, dict) and row.get("url"):
                    extra.append(
                        e.model_copy(
                            update={
                                "url": row["url"],
                                "data": row,
                                "description": "SiteOne HTTP response",
                            }
                        )
                    )
    evidence += extra
    sitemap: dict[str, Any] = next(
        (
            e.data
            for e in evidence
            if e.description
            in {"Bounded robots.txt and XML sitemap fetch", "Reused robots/sitemap collector"}
        ),
        {},
    )
    crawl = next((r for r in ctx.results if r.name == "siteone"), None)
    if not sitemap and crawl and crawl.collection_started and crawl.status == "ok":
        # Accessibility-only runs still need sitemap reconciliation; no duplicate first fetch.
        sitemap = (
            presence.sitemap_check(ctx, transport) if transport else presence.sitemap_check(ctx)
        )
    if sitemap and sitemap.get("status") not in {None, presence.PASS}:
        stops.append("Sitemap: " + sitemap.get("detail", "Unavailable"))
    crawled = set(ctx.pages)
    browser: set[str] = set()
    failed, blocked, redirected = [], [], []
    for url in ctx.pages:
        add(url, "crawl links", url)
    for e in evidence:
        if e.source == "siteone" and e.description == "SiteOne HTTP response":
            url = add(e.url, "crawl links", e.url)
            status = siteone.status_code(e.data.get("status"))
            if url and not 200 <= status < 300:
                row = {"url": url, "status": status}
                if 300 <= status < 400:
                    redirected.append(row)
                elif status in {401, 403, 429}:
                    blocked.append(row)
                else:
                    failed.append(row)
        if e.source == "siteone" and e.description == "SiteOne SEO metadata":
            if str(e.data.get("deniedByRobotsTxt", "")).lower() == "true":
                blocked.append({"url": e.url, "reason": "robots.txt"})
        if e.source == "browser" and e.description == "Browser page load unavailable":
            if url := add(e.url, "browser sample"):
                row = {"url": url, **e.data}
                if e.data.get("redirected"):
                    redirected.append(row)
                elif e.data.get("status") in {401, 403, 429}:
                    blocked.append(row)
                else:
                    failed.append(row)
        if e.source == "browser" and isinstance(e.data, dict) and e.data.get("page"):
            browser.add(e.url)
            add(e.url, "browser sample")
            for link in e.data["page"].get("links", []):
                href = link.get("href") if isinstance(link, dict) else link
                add(href, "crawl links", e.url)
    sitemap_urls: set[str] = set()
    sitemap_docs: list[dict[str, Any]] = []
    for raw in sitemap.get("sitemap_urls", []):
        if url := add(raw, "sitemap", sitemap.get("sitemap_url", ctx.site_url)):
            sitemap_urls.add(url)
    if sitemap.get("sitemap_urls_truncated"):
        stops.append("Initial sitemap URL limit reached")
    declared = sitemap.get("declared")
    roots = declared if isinstance(declared, list) else []
    queue = list(dict.fromkeys(roots + sitemap.get("sitemap_children", [])))
    visited = {sitemap["sitemap_url"]} if sitemap.get("sitemap_url") else set()
    if sitemap:
        sitemap_docs.append(
            {
                "url": sitemap.get("sitemap_url", ""),
                "source": "robots/default",
                "status": sitemap.get("status"),
                "detail": sitemap.get("detail"),
            }
        )
    deadline = time.monotonic() + INVENTORY_SECONDS
    with inventory_client(transport) as client:
        while queue and len(visited) < MAX_SITEMAPS and time.monotonic() < deadline:
            raw = queue.pop(0)
            url = public_url(raw, ctx.site_url)
            if not url:
                excluded.append({"source": "sitemap", "reason": "Outside site or unsafe URL"})
                continue
            if url in visited:
                continue
            visited.add(url)
            response = presence.fetch(client, url, presence.origin_of(ctx.site_url), deadline)
            row = {
                "url": url,
                "source": "robots" if raw in roots else "sitemap index",
                "status": response.get("status"),
                "complete": response.get("complete", False),
            }
            sitemap_docs.append(row)
            if response.get("status") != 200 or not response.get("complete"):
                stops.append("Sitemap unavailable or body/time limit reached")
                continue
            kind, _, problem = presence.parse_sitemap(response["body"])
            if problem:
                stops.append("Invalid child sitemap")
                continue
            tree = ET.fromstring(response["body"].decode("utf-8-sig"))
            locs = [n.text or "" for n in tree.findall(".//" + presence.SITEMAP_NS + "loc")]
            if kind == "sitemapindex":
                queue.extend(locs[:MAX_URLS])
            else:
                for loc in locs:
                    if found := add(loc, "sitemap", url):
                        sitemap_urls.add(found)
        if queue:
            stops.append("Sitemap document/time limit reached")
    wp_detected = any(
        e.source == "presence" and e.description == "Platform signal: WordPress" for e in evidence
    ) or any(
        re.search(
            r"/wp-content/|/wp-includes/|/wp-json/|generator.{0,25}wordpress",
            json.dumps(e.data),
            re.I,
        )
        for e in evidence
        if e.source in {"browser", "site_checks"}
    )
    inventory = (
        wp_inventory(ctx, transport)
        if wp_detected
        else {"status": "not detected", "reason": "No WordPress signal in the sample", "urls": []}
    )
    for url in inventory["urls"]:
        add(url, "WordPress inventory")
    if ctx.wp_inventory:
        for url in load_export(ctx.wp_inventory):
            add(url, "authorised WordPress export")
        inventory["authorised_export"] = True
    connected_inventory = wp_cli_inventory(ctx)
    if connected_inventory:
        for url in connected_inventory["urls"]:
            add(url, "wp_cli")
        inventory["wp_cli"] = connected_inventory
        stops.extend(connected_inventory["stop_reasons"])
    crawl_stop = (
        (crawl.reason or "Crawl finished within configured bounds; completeness unknown")
        if crawl
        else "SiteOne not selected"
    )
    if crawl and not crawl.collection_started:
        crawl_stop = "SiteOne did not run: " + crawl.reason
    for module in ctx.results:
        if module.name == "browser" and module.reason:
            stops.append("Browser: " + module.reason)
    return {
        "discovered": [
            {"url": url, "sources": sorted(sources)} for url, sources in sorted(discovered.items())
        ],
        "known_urls": len(discovered),
        "crawled": sorted(crawled),
        "browser_tested": sorted(browser),
        "untested": sorted(set(discovered) - browser),
        "sitemap_not_reached": sorted(sitemap_urls - crawled),
        "sitemaps": sitemap_docs,
        "robots_declarations": roots,
        "excluded": excluded,
        "blocked": blocked,
        "failed": failed,
        "redirected": redirected,
        "wordpress": inventory,
        "limits": {
            "crawl_urls": ctx.max_pages,
            "browser_pages": ctx.browser_pages,
            "known_urls": known_limit,
            "sitemap_documents": MAX_SITEMAPS,
            "inventory_seconds": INVENTORY_SECONDS,
            "wp_pages": MAX_WP_PAGES,
            "response_bytes": presence.SITEMAP_MAX_BYTES,
        },
        "stop_reasons": list(
            dict.fromkeys([crawl_stop, *stops, *inventory.get("stop_reasons", [])])
        ),
        "summary": f"{len(crawled)} of {len(discovered)} known URLs crawled, "
        f"{len(browser)} browser-tested. Complete coverage is not established.",
    }
