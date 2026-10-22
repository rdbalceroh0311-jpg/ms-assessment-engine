"""Fifteen bounded checks over existing evidence plus the three permitted HTTP probes."""

import re
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from ams_assess.models import Context, ModuleResult
from ams_assess.modules import presence
from ams_assess.modules.site_rules import (
    CRAWLERS,
    LOCATION,
    HomeHTML,
    applicable_rules,
    conversion_events,
    field_vitals,
    near_duplicates,
    noindex,
    robots_allowed,
    robots_groups,
    structured_data,
    tool_signals,
)

name = "site_checks"
categories = {"technical", "seo", "geo", "performance", "cro_ux", "entity", "local_seo", "presence"}
requires = ["Existing crawl/browser/performance/presence evidence; bounded public GET probes"]
MAX_BYTES = 65_536
PROBE_SECONDS = 10
PROBE_REQUEST_SECONDS = 3
NORMAL_UA = "Mozilla/5.0 (compatible; AMSAssessment/1.0)"
# Documented token: https://platform.openai.com/docs/bots (UA simulation, not a verified bot IP).
SEARCH_UA = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.3; +https://openai.com/searchbot"
LIMITS = (
    "Sampled evidence only; no duplicate crawl or form submission. No external canonical or "
    "sitemap "
    "target fetches. One normal and one AI-search UA homepage GET plus one llms.txt GET at most, "
    "no redirects, 64 KiB raw bytes each, 3-second request timeout capped by the remaining "
    "10-second deadline. Robots/sitemap reuse presence evidence "
    "(or its bounded collector when presence did not run). Header attribution, iframe coverage, "
    "link graph and above-the-fold CTA positions may be incomplete. CWV requires p75 field data; "
    "lab Lighthouse is not CWV. Heuristics and UA comparisons need manual verification. "
    "AI-search readiness is not measured visibility or a ranking guarantee."
)


def clean_url(value: str) -> str:
    return urldefrag(value)[0]


def probe(client: httpx.Client, url: str, ua: str, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"url": url, "error": "DeadlineExceeded"}
    try:
        with client.stream(
            "GET",
            url,
            headers={"User-Agent": ua, "Accept-Encoding": "identity"},
            timeout=min(remaining, PROBE_REQUEST_SECONDS),
        ) as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return {
                    "url": url,
                    "status": response.status_code,
                    "complete": False,
                    "body": "",
                    "error": "UnexpectedContentEncoding",
                }
            body = bytearray()
            complete = True
            for chunk in response.iter_raw():
                room = MAX_BYTES - len(body)
                body.extend(chunk[:room])
                if len(chunk) > room or time.monotonic() >= deadline:
                    complete = False
                    break
            return {
                "url": url,
                "status": response.status_code,
                "complete": complete,
                "body": body.decode("utf-8", "replace"),
                "headers": {
                    k: response.headers.get_list(k) if k == "x-robots-tag" else response.headers[k]
                    for k in ("x-robots-tag", "content-type")
                    if k in response.headers
                },
            }
    except (httpx.HTTPError, ValueError) as exc:
        return {"url": url, "error": type(exc).__name__}


def collect_probes(ctx: Context, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + PROBE_SECONDS
    home = urljoin(ctx.target, "/")
    out = {}
    with httpx.Client(transport=transport, follow_redirects=False) as client:
        if ctx.categories & (categories - {"performance"}):
            out["normal"] = probe(client, home, NORMAL_UA, deadline)
        if ctx.categories & {"technical", "geo"}:
            out["search"] = probe(client, home, SEARCH_UA, deadline)
        if "geo" in ctx.categories:
            out["llms"] = probe(client, urljoin(home, "llms.txt"), NORMAL_UA, deadline)
    return out


class Checks:
    def __init__(self, ctx: Context, probes: dict[str, Any]):
        self.ctx = ctx
        self.result = ModuleResult(name=name)
        self.evidence = [e for r in ctx.results for e in r.evidence]
        self.modules = {r.name: r for r in ctx.results}
        self.probes = probes
        self.pages: dict[str, dict[str, Any]] = {}
        self.responses: dict[str, dict[str, Any]] = {}
        self.seo: dict[str, dict[str, Any]] = {}
        self.source_ids: dict[str, list[str]] = defaultdict(list)
        for e in self.evidence:
            if e.source == "siteone" and isinstance(e.data, dict):
                if e.description == "SiteOne HTTP response":
                    self.responses[clean_url(e.url)] = e.data
                    self.source_ids["crawl"].append(e.id)
                if e.description == "SiteOne SEO metadata":
                    self.seo[clean_url(e.url)] = e.data
                    self.source_ids["seo"].append(e.id)
        records = presence.page_evidence(self.modules.get("browser"))
        for eid, record in records:
            url = clean_url(record.get("url", ""))
            if not url:
                continue
            self.source_ids["pages"].append(eid)
            page = record.get("page", {})
            # Keep desktop/mobile differences in noindex, tags and schema; one text per URL.
            if url not in self.pages:
                self.pages[url] = {**page, "url": url}
            else:
                for key in (
                    "meta",
                    "json_ld",
                    "script_srcs",
                    "tag_snippets",
                    "links",
                    "gated_scripts",
                    "iframes",
                ):
                    merged = list(self.pages[url].get(key, []))
                    merged.extend(v for v in page.get(key, []) if v not in merged)
                    self.pages[url][key] = merged
        home = probes.get("normal", {})
        self.probe_eid = self.result.add_evidence(
            ctx.target, "Bounded site-check HTTP probes", probes
        )
        if (
            home.get("status") == 200
            and home.get("complete")
            and "html" in (home.get("headers", {}).get("content-type", "").lower())
        ):
            parser = HomeHTML(home["url"])
            parser.feed(home["body"])
            url = clean_url(home["url"])
            if url not in self.pages:
                self.pages[url] = {"url": url, **parser.page, "static_only": True}
            else:
                for key in ("iframes", "script_srcs", "links", "tag_snippets", "gated_scripts"):
                    self.pages[url][key] = list(
                        dict.fromkeys(self.pages[url].get(key, []) + parser.page[key])
                    )
        self.sitemap, self.sitemap_ids = self.get_sitemap()
        self.noindex_pages, self.indexing_coverage = self.indexing()

    def get_sitemap(self) -> tuple[dict[str, Any], list[str]]:
        for e in self.evidence:
            if (
                e.source == "presence"
                and e.description == "Bounded robots.txt and XML sitemap fetch"
            ):
                return e.data, [e.id]
        # No duplicate retrieval if presence already collected it. SEO/GEO-only runs can reuse
        # the same bounded collector without selecting the entire presence category.
        crawl = self.modules.get("siteone")
        if self.ctx.categories & {"seo", "geo", "technical"} and crawl and crawl.status == "ok":
            data = presence.sitemap_check(self.ctx)
            eid = self.result.add_evidence(self.ctx.target, "Reused robots/sitemap collector", data)
            return data, [eid]
        return {}, []

    def refs(self, *groups: str) -> list[str]:
        return sorted({eid for group in groups for eid in self.source_ids[group]})

    def row(
        self,
        key: str,
        title: str,
        scopes: list[str],
        status: str,
        checked: str,
        why: str,
        correction: str,
        detail: Any,
        refs: list[str],
        confidence: str = "confirmed",
    ) -> None:
        category = next((c for c in scopes if c in self.ctx.categories), None)
        if category is None:
            return
        row = {
            "site_check": key,
            "area": title,
            "category": category,
            "status": status,
            "checked": checked,
            "why": why,
            "recommendation": correction,
            "detail": detail,
            "source_evidence_ids": sorted(set(refs)),
            "confidence": confidence,
        }
        eid = self.result.add_evidence(self.ctx.target, f"Site check: {title}", row)
        self.result.checks.append({**row, "evidence_ids": [eid]})
        if status in {"WARNING", "ERROR"}:
            self.result.finding(
                "site:" + key,
                title,
                category,
                [eid],
                checked,
                correction,
                severity="high" if status == "ERROR" else "low",
                confidence=confidence,
                urls=sorted({u for u in re.findall(r"https?://[^\s;]+", checked)}),
            )

    def robots(self) -> None:
        data = self.sitemap
        complete = data.get("robots_complete") and data.get("robots_status") == 200
        absent = data.get("robots_status") in {404, 410}
        groups = robots_groups(data.get("robots_body", "")) if complete else []
        broad = []
        # Evaluate rules locally: a homepage-only Allow must not hide a site-wide block.
        paths_checked = ["/", "/ams-site-wide-probe"]
        if complete:
            for bot in ("*", "Googlebot", "Bingbot"):
                rules = applicable_rules(groups, bot)
                if any(not robots_allowed(rules, path) for path in paths_checked):
                    broad.append(bot)
        self.row(
            "robots",
            "Robots rules / site-wide Disallow",
            ["seo", "technical"],
            "ERROR" if broad else "PASS" if complete else "INFO" if absent else "NOT ASSESSED",
            f"Parsed {len(groups)} robots groups. Root or representative public path blocked "
            f"for {', '.join(broad)}."
            if broad
            else (
                f"Parsed {len(groups)} groups; root and representative public path allowed "
                "for *, Googlebot and Bingbot."
                if complete
                else "robots.txt absent (404/410); no rules to parse."
                if absent
                else "Complete robots.txt body unavailable; rules unknown."
            ),
            "Broad rules can prevent search engines crawling public pages.",
            "Confirm intended visibility, remove unintended broad Disallow rules, then retest.",
            {
                "groups": groups,
                "broad_blocks": broad,
                "paths_checked": paths_checked if complete else [],
            },
            self.sitemap_ids,
        )
        paths = sorted({urlsplit(u).path or "/" for u in [self.ctx.target, *self.ctx.pages]})[:100]
        bots: list[dict[str, Any]] = []
        for purpose, tokens in CRAWLERS.items():
            for bot in tokens:
                rules = applicable_rules(groups, bot)
                denied = [p for p in paths if not robots_allowed(rules, p)] if complete else []
                bots.append(
                    {
                        "crawler": bot,
                        "purpose": purpose,
                        "rules": rules,
                        "blocked_paths": denied,
                        "status": ("WARNING" if purpose != "training" else "INFO")
                        if denied
                        else "PASS"
                        if complete
                        else "INFO"
                        if absent
                        else "NOT ASSESSED",
                    }
                )
        blocked = [b["crawler"] for b in bots if b["status"] == "WARNING"]
        self.row(
            "ai-robots",
            "AI crawler rules by purpose",
            ["geo"],
            "WARNING" if blocked else "PASS" if complete else "INFO" if absent else "NOT ASSESSED",
            ("Search/user crawlers blocked on sampled paths: " + ", ".join(blocked))
            if blocked
            else (
                "Search/user crawl paths allowed; training restrictions are customer choices."
                if complete
                else "No robots policy available; crawler permissions are unknown."
            ),
            "Search and user-initiated access differ from training consent; robots rules alone "
            "do not establish crawler access or visibility.",
            "Review search/user restrictions with the customer; retain intentional "
            "training choices. "
            "User-initiated agents may not obey robots rules; verify the journey manually.",
            {"crawlers": bots, "paths_checked": paths},
            self.sitemap_ids,
            "needs_verification" if blocked else "confirmed",
        )

    def indexing(self) -> tuple[list[dict[str, str]], set[str]]:
        hits = []
        covered = set()
        for url, row in self.seo.items():
            if str(row.get("robotsIndex", "")) in {"0", "1"}:
                covered.add(url)
            if str(row.get("robotsIndex", "")) == "0":
                hits.append({"url": url, "source": "SiteOne effective robotsIndex=0"})
        for url, page in self.pages.items():
            for meta in page.get("meta", []):
                agent = str(meta.get("name", "")).lower()
                if agent in {"robots", "googlebot", "bingbot"} and noindex(meta.get("content")):
                    hits.append({"url": url, "source": f"meta {agent}: {meta['content']}"})
        for url, row in self.responses.items():
            for header, value in row.get("headers", {}).items():
                if header.lower() == "x-robots-tag" and noindex(value):
                    hits.append({"url": url, "source": f"X-Robots-Tag: {value}"})
        home = self.probes.get("normal", {})
        value = home.get("headers", {}).get("x-robots-tag", "")
        if noindex(value):
            hits.append({"url": home["url"], "source": f"X-Robots-Tag: {value}"})
        return hits, covered

    def indexing_rows(self) -> None:
        hits = self.noindex_pages

        def page_role(url: str) -> str:
            parts = urlsplit(url)
            if presence.origin_of(url) == presence.origin_of(self.ctx.target) and parts.path in {
                "",
                "/",
            }:
                return "homepage"
            segments = {re.sub(r"\.html?$", "", s.lower()) for s in parts.path.split("/") if s}
            if segments & {"thank-you", "thankyou", "confirmation", "success"}:
                return "other"
            return (
                "key page candidate"
                if segments
                & {
                    "contact",
                    "contact-us",
                    "service",
                    "services",
                    "about",
                    "about-us",
                    "quote",
                    "request-quote",
                    "book",
                    "booking",
                    "location",
                    "locations",
                }
                else "other"
            )

        home_hits = [h for h in hits if page_role(h["url"]) == "homepage"]
        key_hits = [h for h in hits if page_role(h["url"]) == "key page candidate"]
        aggregate = [
            e
            for e in self.evidence
            if e.description == "SiteOne headers-values table"
            and any(
                str(r.get("header", "")).lower() == "x-robots-tag" and noindex(r.get("value"))
                for r in e.data
            )
        ]
        status = (
            "ERROR"
            if home_hits
            else "WARNING"
            if key_hits or aggregate
            else "INFO"
            if hits
            else ("PASS" if self.indexing_coverage else "NOT ASSESSED")
        )
        self.row(
            "noindex",
            "Page noindex / X-Robots-Tag",
            ["seo", "technical"],
            status,
            "; ".join(f"{h['url']}: {h['source']}" for h in hits)
            or (
                "Crawl-wide X-Robots-Tag noindex observed; affected pages are unknown."
                if aggregate
                else f"No noindex in {len(self.indexing_coverage)} crawl indexing records. "
                "Meta directives inspected where available; individual HTTP-header attribution may "
                "be unavailable outside the homepage."
            ),
            "Unintended noindex on home or key pages prevents those pages appearing in search.",
            "Confirm intended indexing per URL; inspect response headers on unsampled pages. "
            "Remove noindex only on pages intended for search.",
            {
                "pages": [{**h, "page_role": page_role(h["url"])} for h in hits],
                "crawl_indexing_urls": sorted(self.indexing_coverage),
                "aggregate_header_evidence": [e.id for e in aggregate],
            },
            self.refs("seo", "pages", "crawl") + [self.probe_eid] + [e.id for e in aggregate],
            "needs_verification" if (key_hits or aggregate) and not home_hits else "confirmed",
        )
        entries = self.sitemap.get("sitemap_urls", [])
        rows = []
        for loc in entries:
            url = presence.resolve(loc, self.sitemap.get("sitemap_url", self.ctx.target))
            issues = []
            unavailable_reason = ""
            if not url:
                issues.append("invalid URL")
            else:
                row = self.responses.get(clean_url(url))
                if row is None:
                    unavailable_reason = "not crawled"
                else:
                    status_code = int(row.get("status", 0))
                    if status_code <= 0:
                        unavailable_reason = "no HTTP response (NOT ASSESSED)"
                    elif status_code == 429 or status_code >= 500:
                        unavailable_reason = f"unavailable (HTTP {status_code}; retry)"
                    elif 300 <= status_code < 400:
                        issues.append(f"redirected (HTTP {status_code})")
                    elif status_code != 200:
                        issues.append(f"non-200 (HTTP {status_code})")
                if any(h["url"] == clean_url(url) for h in hits):
                    issues.append("noindex")
                if not urlsplit(loc).scheme and not self.ctx.local:
                    issues.append("relative sitemap URL")
            if unavailable_reason:
                issues.append(unavailable_reason)
            rows.append({"url": url or loc, "issues": issues, "unavailable": unavailable_reason})
        bad = [r for r in rows if any(i != r["unavailable"] for i in r["issues"])]
        unknown = [r for r in rows if r["unavailable"]]
        truncated = self.sitemap.get("sitemap_urls_truncated", False)
        self.row(
            "sitemap-urls",
            "Sitemap URL validity",
            ["seo"],
            "WARNING" if bad else "NOT ASSESSED" if not rows or unknown or truncated else "PASS",
            f"Compared {len(rows)} sitemap URLs with existing crawl results: {len(bad)} with "
            f"observed issues, {len(unknown)} uncrawled/unavailable (NOT ASSESSED). "
            + (
                "Sitemap index children are not fetched. "
                if self.sitemap.get("kind") == "sitemapindex"
                else ""
            )
            + ("URL sample capped. " if truncated else "")
            + "; ".join(f"{r['url']}: {', '.join(r['issues'])}" for r in rows if r["issues"]),
            "Sitemaps should list final, successful, indexable URLs; uncrawled URLs are unknown.",
            "Replace redirects/non-200/noindex URLs; manually validate uncrawled entries. "
            "Use absolute HTTP(S) sitemap URLs in production.",
            {"urls": rows, "truncated": truncated},
            self.sitemap_ids + self.refs("crawl", "seo"),
            "needs_verification" if unknown else "confirmed",
        )
        canonicals: list[dict[str, Any]] = []
        for url, page in self.pages.items():
            canonical = page.get("canonical")
            if not canonical:
                continue
            target = presence.resolve(canonical, url)
            response = self.responses.get(clean_url(target or ""))
            issues = []
            if not target:
                issues.append("invalid canonical URL")
            elif urlsplit(target).netloc != urlsplit(url).netloc:
                issues.append("different host; confirm intended canonical")
            target_status = int(response.get("status", 0)) if response else 0
            unavailable = target_status <= 0 or target_status == 429 or target_status >= 500
            if response and not unavailable and target_status != 200:
                issues.append(f"target HTTP {target_status}")
            if target and any(h["url"] == clean_url(target) for h in hits):
                issues.append("target noindex")
            target_page = self.pages.get(clean_url(target or ""), {})
            onward = presence.resolve(target_page.get("canonical"), target or url)
            if onward and clean_url(onward) != clean_url(target or ""):
                issues.append("canonical chain / conflicting target canonical")
            canonicals.append(
                {
                    "page": url,
                    "target": target or canonical,
                    "issues": issues,
                    "accessibility": (
                        "NOT ASSESSED: target not in crawl"
                        if not response
                        else "NOT ASSESSED: no HTTP response"
                        if target_status <= 0
                        else f"NOT ASSESSED: target unavailable (HTTP {target_status}; retry)"
                        if unavailable
                        else f"HTTP {target_status}"
                    ),
                }
            )
        bad_canon = [r for r in canonicals if r["issues"]]
        unknown_canon = [r for r in canonicals if r["accessibility"].startswith("NOT ASSESSED")]
        self.row(
            "canonicals",
            "Canonical accessibility / consistency",
            ["seo"],
            "WARNING"
            if bad_canon
            else "NOT ASSESSED"
            if not canonicals or unknown_canon
            else "PASS",
            f"{len(canonicals)} declarations; {len(bad_canon)} need review; "
            f"{len(unknown_canon)} targets NOT ASSESSED. "
            + "; ".join(
                f"{r['page']} -> {r['target']}: {r['accessibility']} {', '.join(r['issues'])}"
                for r in canonicals
            ),
            "Canonical hints should consistently identify accessible, indexable preferred pages.",
            "Review conflicting targets; use final indexable URLs. "
            "Manually verify uncrawled targets.",
            canonicals,
            self.refs("pages", "crawl", "seo") + [self.probe_eid],
            "needs_verification" if bad_canon or unknown_canon else "confirmed",
        )

    def page_quality(self) -> None:
        duplicates = {}
        for field in ("title", "description"):
            values: dict[str, list[str]] = defaultdict(list)
            for url in set(self.seo) | set(self.pages):
                page = self.pages.get(url, {})
                value = self.seo.get(url, {}).get(field, page.get(field, ""))
                if field == "description" and not value:
                    value = next(
                        (
                            m.get("content", "")
                            for m in page.get("meta", [])
                            if str(m.get("name", "")).lower() == "description"
                        ),
                        "",
                    )
                value = " ".join(str(value).lower().split())
                if value:
                    values[value].append(url)
            duplicates[field] = [sorted(urls) for urls in values.values() if len(urls) > 1]
        headings = {
            url: [
                int(h["level"][-1])
                for h in page.get("headings", [])
                if re.fullmatch(r"[hH][1-6]", str(h.get("level", "")))
            ]
            for url, page in self.pages.items()
        }
        heading_ids = []
        redirects: dict[str, str] = {}
        edges: set[tuple[str, str]] = set()
        broken = []
        unavailable_links = []
        for e in self.evidence:
            if e.description == "SiteOne seo-headings table":
                heading_ids.append(e.id)
                for row in e.data:
                    headings[urljoin(self.ctx.target, row["urlPathAndQuery"])] = [
                        int(n) for n in re.findall(r"<h([1-6])>", row.get("headings", ""), re.I)
                    ]
            if e.description == "SiteOne redirects table":
                heading_ids.append(e.id)
                for row in e.data:
                    # SiteOne redirect tables use source/target URL fields.
                    source = row.get("url") or row.get("sourceUrl")
                    target = row.get("redirectUrl") or row.get("targetUrl") or row.get("location")
                    if source and target:
                        redirects[urljoin(self.ctx.target, source)] = urljoin(
                            self.ctx.target, target
                        )
                        if row.get("sourceUqId"):
                            edges.add(
                                (
                                    urljoin(self.ctx.target, row["sourceUqId"]),
                                    urljoin(self.ctx.target, source),
                                )
                            )
            if e.description == "SiteOne 404 table":
                heading_ids.append(e.id)
                for row in e.data:
                    if presence.origin_of(row.get("url", "")) == presence.origin_of(
                        self.ctx.target
                    ):
                        status = int(row.get("statusCode", 0))
                        entry = {
                            "source": row.get("sourceUqId", "unknown"),
                            "target": row["url"],
                            "status": status,
                        }
                        if status <= 0 or status == 429 or status >= 500:
                            unavailable_links.append(entry)
                        elif 400 <= status < 500:
                            broken.append(entry)
        problems = []
        for url, levels in headings.items():
            if levels.count(1) > 1:
                problems.append({"url": url, "issue": "multiple H1"})
            if any(b > a + 1 for a, b in zip([0, *levels], levels, strict=False)):
                problems.append({"url": url, "issue": "skipped heading level"})
        inbound: dict[str, set[str]] = defaultdict(set)
        chains = []
        for url, page in self.pages.items():
            for href in page.get("links", []):
                target = presence.resolve(href, url)
                if target:
                    edges.add((url, clean_url(target)))
        for url, target in sorted(edges):
            if any(
                presence.origin_of(u) != presence.origin_of(self.ctx.target) for u in (url, target)
            ):
                continue
            if target != url:
                inbound[target].add(url)
            status = int(self.responses.get(target, {}).get("status", 0))
            if target in self.responses and (status <= 0 or status == 429 or status >= 500):
                unavailable_links.append({"source": url, "target": target, "status": status})
            elif 400 <= status < 500:
                broken.append({"source": url, "target": target, "status": status})
            if 300 <= status < 400 or target in redirects:
                chain = [target]
                while chain[-1] in redirects and len(chain) < 10:
                    onward = redirects[chain[-1]]
                    chain.append(onward)
                    if onward in chain[:-1]:
                        break
                final_status = int(self.responses.get(chain[-1], {}).get("status", 0))
                chains.append(
                    {
                        "source": url,
                        "chain": chain,
                        "complete": len(chain) > 1 and 200 <= final_status < 300,
                    }
                )
        # The browser's link list caps at 300. Even an apparently complete page sample can be
        # truncated, and a crawl cap invalidates site-wide orphan conclusions.
        crawl = self.modules.get("siteone")
        graph_complete = (
            bool(self.ctx.pages)
            and set(self.ctx.pages) <= self.pages.keys()
            and bool(crawl and crawl.status == "ok" and not crawl.reason)
            and all(
                "links" in self.pages[u] and len(self.pages[u]["links"]) < 300
                for u in self.ctx.pages
            )
        )
        no_inbound = (
            [u for u in self.ctx.pages if u != self.ctx.target and not inbound[u]]
            if (graph_complete)
            else []
        )
        issues = bool(any(duplicates.values()) or problems or broken or chains or no_inbound)
        self.row(
            "page-quality",
            "Duplicates, headings / internal links",
            ["seo", "technical"],
            "WARNING"
            if issues
            else "NOT ASSESSED"
            if unavailable_links
            else "PASS"
            if self.seo or headings
            else "NOT ASSESSED",
            f"Duplicate title groups: {len(duplicates['title'])}; duplicate description groups: "
            f"{len(duplicates['description'])}; heading issues: {len(problems)}; broken internal "
            f"links: {len(broken)}; unavailable links: {len(unavailable_links)}; "
            f"internal redirects: {len(chains)}. Inbound links: "
            + (
                f"{len(no_inbound)} sampled pages without inbound links."
                if graph_complete
                else "NOT ASSESSED (incomplete sampled link graph)."
            ),
            "Distinct metadata, logical headings and direct internal links improve navigation "
            "and help crawlers interpret pages.",
            "Use distinct titles/descriptions, fix heading jumps and broken links, link directly "
            "to final targets. Retry unavailable links before classifying them. "
            "Verify possible orphan pages with a complete link graph.",
            {
                "duplicates": duplicates,
                "heading_issues": problems,
                "broken_links": broken,
                "unavailable_links": unavailable_links,
                "redirect_chains": chains,
                "no_inbound": no_inbound,
                "inbound_status": "assessed sample" if graph_complete else "NOT ASSESSED",
            },
            self.refs("seo", "pages", "crawl") + heading_ids,
            "needs_verification" if unavailable_links else "confirmed",
        )

    def schema_and_locations(self) -> None:
        schemas = [
            {"url": url, **structured_data(page.get("json_ld", []))}
            for url, page in self.pages.items()
        ]
        errors = [f"{s['url']}: {e}" for s in schemas for e in s["errors"]]
        checked = sum(len(s["checked"]) for s in schemas)
        recommended = [f"{s['url']}: {e}" for s in schemas for e in s["recommended"]]
        self.row(
            "structured-data",
            "Organization / LocalBusiness / BreadcrumbList",
            ["entity", "seo"],
            "ERROR"
            if errors
            else "INFO"
            if recommended or (self.pages and not checked)
            else "PASS"
            if checked
            else "NOT ASSESSED",
            f"Validated {checked} supported JSON-LD nodes. "
            + (
                "; ".join(errors)
                if errors
                else "No invalid supported properties found. "
                if checked
                else "No supported optional schema detected. "
            )
            + f" {len(recommended)} applicable-property suggestions; absence is not a failure.",
            "Consistent structured data clarifies entities; it does not guarantee rich results.",
            "Fix invalid JSON-LD and required properties; review applicable recommended properties "
            "with Google's Rich Results Test. Organization has no required properties.",
            schemas,
            self.refs("pages") + [self.probe_eid],
        )
        pages = list(self.pages.values())
        candidates = [
            p
            for p in pages
            if LOCATION.search(urlsplit(p["url"]).path)
            and len(re.findall(r"\w+", p.get("visible_text_summary", ""))) >= 80
        ]
        pairs = near_duplicates(pages)
        self.row(
            "location-duplicates",
            "Near-duplicate location pages",
            ["local_seo", "seo"],
            "WARNING" if pairs else "INFO" if len(candidates) >= 2 else "NOT ASSESSED",
            f"Compared {len(candidates)} location-like URLs with >=80 sampled words using "
            f"5-word shingle Jaccard >=0.8: {len(pairs)} similar pairs. "
            "Heuristic only; shared navigation and truncated text can influence similarity.",
            "Near-duplicate location pages may warrant review for doorway-page risk; this is "
            "never a confirmed doorway-page finding.",
            "Manually compare unique local value and remove shared boilerplate before deciding "
            "whether to improve or consolidate pages.",
            pairs,
            self.refs("pages"),
            "needs_verification",
        )
        return None

    def performance(self) -> None:
        metrics, ids = [], []
        for e in self.evidence:
            if e.source == "performance" and isinstance(e.data, dict):
                rows = field_vitals(e.data)
                if rows:
                    metrics.extend(rows)
                    ids.append(e.id)
        known = [m for m in metrics if m["grade"] != "NOT ASSESSED"]
        failing = [m for m in known if m["grade"] != "good"]
        self.row(
            "cwv",
            "Core Web Vitals (field p75)",
            ["performance"],
            "WARNING"
            if failing
            else "PASS"
            if metrics and len(known) == len(metrics)
            else "NOT ASSESSED",
            "; ".join(
                f"{m['scope']} / {m['device']} {m['metric']} p75={m['p75']} "
                f"{m['unit']}: {m['grade']}"
                for m in metrics
            )
            or "No usable CrUX/PSI p75 field data. Lighthouse lab scores are not CWV.",
            "Field LCP, INP and CLS describe real-user experience; origin and URL scopes differ.",
            "Investigate non-good field metrics and verify all three at p75. Good: LCP <=2500 ms, "
            "INP <=200 ms, CLS <=0.1. Poor: LCP >4000 ms, INP >500 ms, CLS >0.25. "
            "Missing field data requires manual verification; preserve separate lab findings.",
            metrics,
            ids,
        )

    def conversion(self) -> None:
        tools = [{"page": u, **hit} for u, p in self.pages.items() for hit in tool_signals(p, u)]
        self.row(
            "booking-tools",
            "Booking / CRM / chat tools",
            ["cro_ux", "presence"],
            "INFO" if self.pages else "NOT ASSESSED",
            "Detected markup signals: "
            + (", ".join(sorted({t["tool"] for t in tools})) or "none")
            + ". Script/link/iframe URLs and recognised inline API calls; prose excluded. "
            "Iframes outside the normal-UA homepage probe may be unavailable.",
            "Public integration signals help identify systems to discuss; they do not prove "
            "account existence, ownership or working integration.",
            "Confirm customer-owned systems and test authorised journeys before proposing changes.",
            tools,
            self.refs("pages") + [self.probe_eid],
        )
        paths = []
        for url, page in self.pages.items():
            contacts = page.get("contacts", [])
            ctas = [
                c
                for c in page.get("ctas", [])
                if re.search(
                    r"\b(book|enquir\w*|contact|quote|call|schedule|request|start)\b",
                    c.get("text", ""),
                    re.I,
                )
            ]
            supporting = [
                u
                for u in page.get("links", [])
                if presence.origin_of(u) == presence.origin_of(self.ctx.target)
                and re.search(r"case[-_/]?stud|portfolio|team|about", urlsplit(u).path, re.I)
            ]
            paths.append(
                {
                    "url": url,
                    "enquiry_forms": sum(
                        bool(re.search(r"enquir|contact|quote", f.get("action", ""), re.I))
                        or any(
                            field.get("type") in {"email", "tel"} for field in f.get("fields", [])
                        )
                        for f in page.get("forms", [])
                        if not any(field.get("type") == "password" for field in f.get("fields", []))
                    ),
                    "forms_inspected": len(page.get("forms", [])),
                    "booking_links": sorted(
                        set(
                            [
                                t["url"]
                                for t in tools
                                if t["page"] == url
                                and t["context"] in {"links", "iframes"}
                                and t["tool"]
                                in {
                                    "Calendly",
                                    "Acuity",
                                    "Square Appointments",
                                    "SimplyBook.me",
                                    "Setmore",
                                    "Fresha",
                                    "Timely",
                                }
                            ]
                            + [
                                u
                                for u in page.get("links", [])
                                if re.search(
                                    r"(?:^|/)(?:book|booking|appointments?|schedule|meetings)(?:/|\.|$)",
                                    urlsplit(u).path,
                                    re.I,
                                )
                            ]
                        )
                    ),
                    "tel_mailto": contacts,
                    "ctas": ctas,
                    "above_fold": [c for c in ctas if c.get("above_fold") is True],
                    "above_fold_status": "assessed"
                    if ctas and all("above_fold" in c for c in ctas)
                    else "NOT ASSESSED",
                    "trust_signals": page.get("trust_signals", []),
                    "supporting_pages": supporting,
                    "form_submissions": "NOT TESTED",
                }
            )
        rendered = [p for p in self.pages.values() if not p.get("static_only")]
        self.row(
            "conversion-pathways",
            "Conversion pathways / forms NOT TESTED",
            ["cro_ux"],
            "INFO" if rendered else "NOT ASSESSED",
            f"{len(rendered)} rendered pages inspected for forms, booking links, tel/mailto, "
            "CTA wording, trust signals and case-study/portfolio/team/about links. "
            "Above-the-fold placement NOT ASSESSED where positions are unavailable. "
            "Form submissions: NOT TESTED. Trust claims and journey outcomes are unverified.",
            "Visible enquiry options help plan journey validation; their presence does not prove "
            "forms, booking, lead delivery or conversion performance work.",
            "Review missing paths and CTA prominence manually; arrange an authorised safe journey "
            "test with the customer. No submissions were made.",
            paths,
            self.refs("pages"),
        )
        records = presence.page_evidence(self.modules.get("browser"))
        hits = presence.detect(
            [(eid, r.get("url", ""), presence.page_lines(r)) for eid, r in records]
        )
        tags = [k for k in hits if presence.SIGNATURES[k][1] in presence.TAG_GROUPS]
        consent = presence.consent_context(records)
        gated = bool(
            consent["managers"]
            or consent["gated_scripts"]
            or any(p.get("gated_scripts") for p in self.pages.values())
        )
        events = [
            {"page": u, **event} for u, p in self.pages.items() for event in conversion_events(p)
        ]
        platform_rows = next(
            (
                c["value"]
                for c in self.modules.get("presence", ModuleResult(name="presence")).checks
                if c.get("key") == "platforms"
            ),
            [],
        )
        self.row(
            "conversion-events",
            "Analytics / conversion-event code signals",
            ["cro_ux", "presence"],
            "NOT ASSESSED" if gated or not self.pages else "INFO",
            (
                "Unknown: consent manager/gated scripts prevent confirming execution. "
                if gated
                else "Code signals only; execution and conversion configuration were not verified. "
            )
            + f"Analytics/advertising tags: {', '.join(tags) or 'none observed'}; "
            f"visible event calls: {len(events)}. Events are not proof of measured conversions.",
            "An event call can indicate measurement intent; consent, delivery and key-event "
            "configuration need separate verification.",
            "Use customer-authorised tag diagnostics and analytics access to verify post-consent "
            "events and conversion configuration.",
            {
                "events": events,
                "state": "Unknown" if gated else "code signals only",
                "presence_platforms": [
                    p for p in platform_rows if p.get("group") in presence.TAG_GROUPS
                ],
                "tag_keys": tags,
                "consent": consent,
            },
            self.refs("pages") + [self.probe_eid],
            "needs_verification",
        )

    def access_and_answers(self) -> None:
        normal, search = self.probes.get("normal", {}), self.probes.get("search", {})
        blocked_status = {401, 403, 429, 503}
        suspected = normal.get("status") == 200 and search.get("status") in blocked_status
        same_ok = normal.get("status") == search.get("status") == 200
        self.row(
            "firewall",
            "Suspected CDN / firewall crawler blocking",
            ["technical", "geo"],
            "WARNING" if suspected else "PASS" if same_ok else "NOT ASSESSED",
            f"Normal UA HTTP {normal.get('status', 'unknown')}; AI-search UA HTTP "
            f"{search.get('status', 'unknown')}. "
            + (
                "Suspected differential blocking; cause unconfirmed."
                if suspected
                else "No differential HTTP block observed in this pair."
                if same_ok
                else "Comparison inconclusive; no access controls were bypassed."
            ),
            "A differing response may indicate a CDN/firewall policy, rate limit or transient "
            "failure; spoofing a UA does not reproduce a verified crawler IP.",
            "Review CDN/server logs and allow intentional search access after customer approval. "
            "Verify with the real crawler; do not bypass challenges.",
            {
                "normal": {k: v for k, v in normal.items() if k != "body"},
                "search": {k: v for k, v in search.items() if k != "body"},
            },
            [self.probe_eid],
            "needs_verification" if suspected else "confirmed",
        )
        llms = self.probes.get("llms", {})
        status = llms.get("status")
        content_type = llms.get("headers", {}).get("content-type", "")
        present = (
            status == 200
            and llms.get("complete")
            and bool(llms.get("body", "").strip())
            and (
                "html" not in content_type.lower()
                and not re.search(
                    r"<(!doctype\s+html|html|head|body)\b", llms.get("body", ""), re.I
                )
            )
        )
        state = "present" if present else "absent" if status in {404, 410} else "unknown"
        self.row(
            "llms-txt",
            "llms.txt (informational)",
            ["geo"],
            "INFO" if state != "unknown" else "NOT ASSESSED",
            f"llms.txt {state}; HTTP {status or 'unavailable'}. One bounded fetch; "
            "HTML fallbacks, redirects, empty or incomplete responses are not confirmed files.",
            "llms.txt is an optional informational convention, not an indexing requirement "
            "or evidence of ranking or AI-search visibility.",
            "No correction required for absence. Review accuracy if choosing to publish this file.",
            {"state": state, "http_status": status},
            [self.probe_eid],
        )
        signals = []
        for url, page in self.pages.items():
            text = page.get("visible_text_summary", "")
            headings = [str(h.get("text", "")) for h in page.get("headings", [])]
            types = structured_data(page.get("json_ld", []))["types"]
            questions = [
                h
                for h in headings
                if h.endswith("?")
                or re.match(r"^(who|what|when|where|why|how|can|do|does|is|are)\b", h, re.I)
            ]
            signals.append(
                {
                    "url": url,
                    "faq_qa_schema": sorted(set(types) & {"FAQPage", "QAPage"}),
                    "question_headings": questions,
                    "faq_section": any(
                        re.search(r"faq|frequently asked", h, re.I) for h in headings
                    ),
                    "service_statement": bool(
                        re.search(
                            r"\b(we (?:offer|provide|help|repair)|our services|services include)\b",
                            text,
                            re.I,
                        )
                    ),
                    "location_statement": bool(
                        re.search(
                            r"\b(?:serv(?:e|es|ing)|based in|located in|service area)\s+\w+",
                            text,
                            re.I,
                        )
                    ),
                    "contact_statement": bool(page.get("contacts"))
                    or bool(re.search(r"\bcontact (?:us|our team)|\bcall us\b", text, re.I)),
                }
            )
        self.row(
            "answer-readiness",
            "Answer readiness (heuristic)",
            ["geo"],
            "INFO" if self.pages else "NOT ASSESSED",
            f"{len(signals)} pages checked for FAQPage/QAPage, question headings, FAQ sections, "
            "and service/location/contact statements. Signals are heuristic; clarity, correctness "
            "and AI-search visibility are not established.",
            "Explicit answers and business context can make content easier to interpret.",
            "Review accurate answers, service scope, locations and contact details "
            "with the customer; "
            "do not add unsupported schema or claim AI-search rankings.",
            signals,
            self.refs("pages") + [self.probe_eid],
            "needs_verification",
        )


def run(ctx: Context) -> ModuleResult:
    checks = Checks(ctx, collect_probes(ctx))
    checks.robots()
    checks.indexing_rows()
    checks.page_quality()
    checks.schema_and_locations()
    checks.performance()
    checks.conversion()
    checks.access_and_answers()
    unavailable = sum(r["status"] == "NOT ASSESSED" for r in checks.result.checks)
    if unavailable:
        checks.result.reason = (
            f"{unavailable} of {len(checks.result.checks)} checks NOT ASSESSED; "
            "see status rows for missing evidence and manual verification"
        )
        if unavailable == len(checks.result.checks):
            checks.result.status = "skipped"
    checks.result.raw_paths = [ctx.raw(name, {"checks": checks.result.checks, "limits": LIMITS})]
    return checks.result
