"""Conservative response signatures, shared by collectors and stored-evidence analysis.

A status, noindex header, CDN name or prose alone is never a challenge. The generic
fallback requires a non-200 success, a complete tiny body and an HTML meta refresh.
"""

import re
from collections import defaultdict, deque
from html.parser import HTMLParser
from typing import Any

REASON = "blocked by hosting bot protection (challenge served)"


class _Refresh(HTMLParser):
    def __init__(self, body: str):
        super().__init__()
        self.targets: list[str] = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and str(values.get("http-equiv", "")).lower() == "refresh":
            self.targets.append(str(values.get("content", "")).lower())


def challenged(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("hosting_challenge") is True:
        return True
    raw_headers = response.get("headers", {})
    headers = (
        {str(k).lower(): str(v).lower() for k, v in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )
    if headers.get("sg-captcha") or headers.get("cf-mitigated") == "challenge":
        return True
    body = response.get("body", "")
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if not isinstance(body, str):
        return False
    refresh = _Refresh(body).targets
    if any("/.well-known/sgcaptcha/" in target for target in refresh):
        return True
    try:
        status = int(str(response.get("status", 0)).split()[0])
    except (ValueError, IndexError):
        status = 0
    # JSD scripts and cached __cf_chl_ links also occur on ordinary pages. Only
    # an explicit header or a non-success interstitial establishes a CF challenge.
    if status in {403, 503} and re.search(r"\b_cf_chl_opt\b", body):
        return True
    if (
        status != 200
        and status != 0
        and re.search(r"<title>\s*Just a moment(?:\.{3}|…)?\s*</title>", body, re.I)
        and re.search(
            r"enable javascript and cookies|checking your browser|verifying you are human",
            body,
            re.I,
        )
    ):
        return True
    return bool(
        200 < status < 300
        and refresh
        and len(body.encode()) <= 2048
        and response.get("complete", True)
    )


def blocked_urls(evidence) -> set[str]:
    """Distinct requested URLs, including the separately stored bounded probes."""
    urls = set()
    for item in evidence:
        data = item.data
        if challenged(data):
            urls.add(item.url)
        if item.description == "Bounded site-check HTTP probes" and isinstance(data, dict):
            urls.update(row.get("url", item.url) for row in data.values() if challenged(row))
        if isinstance(data, dict):
            urls.update(data.get("hosting_blocked_urls", []))
    return urls


class _RobotsMeta(HTMLParser):
    def __init__(self, body: str):
        super().__init__()
        self.directives: list[str] = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and str(values.get("name", "")).lower() in {
            "robots",
            "googlebot",
            "bingbot",
        }:
            self.directives.append(str(values.get("content", "")))


def _has_noindex(data: dict[str, Any]) -> bool:
    # Reuse the collector's exact directive/crawler scoping semantics. Import locally
    # because site_rules imports presence, which imports the collector/store modules.
    from ams_assess.modules.site_rules import noindex

    headers = data.get("headers", {})
    if isinstance(headers, dict) and any(
        str(name).lower() == "x-robots-tag" and noindex(value) for name, value in headers.items()
    ):
        return True
    page = data.get("page", {})
    if isinstance(page, dict) and any(
        str(meta.get("name", "")).lower() in {"robots", "googlebot", "bingbot"}
        and noindex(meta.get("content", ""))
        for meta in page.get("meta", [])
        if isinstance(meta, dict)
    ):
        return True
    body = data.get("body", "")
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    return isinstance(body, str) and any(noindex(value) for value in _RobotsMeta(body).directives)


def _noindex_basis(data, items, blocked) -> bool:
    """Every explicit hit and supporting citation must be explained by challenges.

    Known indexable SEO metadata, successful HTTP responses and rendered pages
    without a robots noindex directive are coverage only. Unknown citations retain
    a finding; actual directives and aggregate support always keep it active.
    """
    detail = data.get("detail", {})
    if not isinstance(detail, dict):
        return False
    aggregates = set(detail.get("aggregate_header_evidence", []))
    refs = set(data.get("source_evidence_ids", [])) | aggregates
    if not refs or not refs <= items.keys() or not aggregates <= blocked:
        return False
    if refs <= blocked:
        return True
    headers: dict[tuple[str, str], bool] = {}
    for eid in refs:
        e = items[eid]
        value = e.data
        if not isinstance(value, dict):
            if eid not in blocked:
                return False
            continue
        response = value
        if e.description == "Bounded site-check HTTP probes":
            # A secondary probe only supports the claim through an actual directive,
            # never incidental prose, CSS values or unrelated header values.
            if any(
                isinstance(probe, dict) and not challenged(probe) and _has_noindex(probe)
                for probe in value.values()
            ):
                return False
            response = value.get("normal", {})
            if not challenged(response):
                return False
        elif eid not in blocked:
            if _has_noindex(value):
                return False
            if e.description == "SiteOne SEO metadata" and str(value.get("robotsIndex")) == "1":
                continue
            if e.description == "SiteOne HTTP response" and str(value.get("status")) == "200":
                continue
            if e.description.startswith("Rendered page evidence") and isinstance(
                value.get("page"), dict
            ):
                continue
            return False
        for header, value in response.get("headers", {}).items():
            if header.lower() == "x-robots-tag":
                token = (response.get("url", e.url), f"X-Robots-Tag: {value}")
                headers[token] = headers.get(token, True) and challenged(response)
    hits = detail.get("pages", [])
    return bool(hits) and all(headers.get((h.get("url"), h.get("source")), False) for h in hits)


def challenge_basis(evidence) -> set[str]:
    """Challenge-only IDs in O(items + citation edges + explicit noindex hits).

    Group SiteOne responses once by run and URL. A queue propagates explicit citations
    without repeatedly rescanning the store. Unknown/mixed evidence stays untouched.
    """
    items = {e.id: e for e in evidence}
    blocked = {eid for eid, e in items.items() if challenged(e.data)}
    by_run: dict[str, bool] = {}
    by_url: dict[tuple[str, str], bool] = {}
    runs: dict[str, str] = {}
    for eid, e in items.items():
        match = re.search(r"-r[0-9]+-", eid)
        run = runs[eid] = match.group() if match else "current"
        if e.source == "siteone" and e.description == "SiteOne HTTP response":
            by_run[run] = by_run.get(run, True) and eid in blocked
            key = (run, e.url)
            by_url[key] = by_url.get(key, True) and eid in blocked
        if e.description == "Bounded site-check HTTP probes" and isinstance(e.data, dict):
            responses = [r for r in e.data.values() if isinstance(r, dict) and "status" in r]
            if responses and all(challenged(r) for r in responses):
                blocked.add(eid)
    for eid, e in items.items():
        if e.source != "siteone":
            continue
        if e.description in {"SiteOne SEO metadata", "SiteOne seo-headings table"}:
            only_challenges = by_url.get((runs[eid], e.url), False)
        elif e.description in {
            "SiteOne header analysis (crawl aggregate)",
            "SiteOne header outcome (crawl aggregate)",
            "SiteOne headers-values table",
            "SiteOne canonical link summary",
        }:
            only_challenges = by_run.get(runs[eid], False)
        else:
            continue
        if only_challenges:
            blocked.add(eid)

    dependents: dict[str, list[str]] = defaultdict(list)
    pending: dict[str, int] = {}
    noindex = {}
    for eid, e in items.items():
        if not isinstance(e.data, dict):
            continue
        if e.data.get("site_check") == "noindex":
            noindex[eid] = e.data
            continue  # Its aggregate citations and explicit hits need the stricter rule.
        refs = set(e.data.get("source_evidence_ids", []))
        if refs:
            pending[eid] = len(refs)
            for ref in refs:
                dependents[ref].append(eid)

    def propagate(seeds):
        queue = deque(seeds)
        while queue:
            for eid in dependents[queue.popleft()]:
                pending[eid] -= 1
                if pending[eid] == 0 and eid not in blocked:
                    blocked.add(eid)
                    queue.append(eid)

    propagate(list(blocked))
    derived = [
        eid
        for eid, data in noindex.items()
        if eid not in blocked and _noindex_basis(data, items, blocked)
    ]
    blocked.update(derived)
    propagate(derived)
    return blocked
