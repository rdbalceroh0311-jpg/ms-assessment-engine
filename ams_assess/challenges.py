"""Conservative response signatures, shared by collectors and stored-evidence analysis.

A status, noindex header, CDN name or prose alone is never a challenge. The generic
fallback requires a non-200 success, a complete tiny body and an HTML meta refresh.
"""

import re
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
    if re.search(r"(?:window\s*\.\s*_cf_chl_opt|__cf_chl_|/cdn-cgi/challenge-platform/)", body):
        return True
    # Cloudflare's interstitial title plus its browser verification text, not an article
    # which happens to mention 'Just a moment'.
    if re.search(r"<title>\s*Just a moment(?:\.{3}|…)?\s*</title>", body, re.I) and re.search(
        r"enable javascript and cookies|checking your browser|verifying you are human", body, re.I
    ):
        return True
    try:
        status = int(str(response.get("status", 0)).split()[0])
    except (ValueError, IndexError):
        status = 0
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


def challenge_basis(evidence) -> set[str]:
    """IDs supported solely by challenges; follow explicit derivation references only.

    Unknown/missing references and mixed real/challenge evidence remain untouched.
    """
    items = {e.id: e for e in evidence}
    blocked = {eid for eid, e in items.items() if challenged(e.data)}
    for eid, e in items.items():
        if e.description == "Bounded site-check HTTP probes" and isinstance(e.data, dict):
            responses = [r for r in e.data.values() if isinstance(r, dict) and "status" in r]
            if responses and all(challenged(r) for r in responses):
                blocked.add(eid)

    # SiteOne's old metadata tables carry no evidence-ID references. Attribute only
    # within the same stored run and exact URL, never across runs or website aliases.
    def run_id(eid: str) -> str:
        match = re.search(r"-r[0-9]+-", eid)
        return match.group() if match else "current"

    for eid, e in items.items():
        if e.source != "siteone":
            continue
        responses = [
            r
            for r in items.values()
            if r.source == "siteone"
            and r.description == "SiteOne HTTP response"
            and run_id(r.id) == run_id(eid)
        ]
        if e.description in {"SiteOne SEO metadata", "SiteOne seo-headings table"}:
            responses = [r for r in responses if r.url == e.url]
        elif e.description not in {
            "SiteOne header analysis (crawl aggregate)",
            "SiteOne header outcome (crawl aggregate)",
            "SiteOne headers-values table",
            "SiteOne canonical link summary",
        }:
            continue
        if responses and all(r.id in blocked for r in responses):
            blocked.add(eid)
    # The legacy noindex row cites broad coverage (including healthy crawl pages).
    # Its explicit header hits identify the actual supporting response. Other probe UAs
    # and coverage-only records must not keep a false homepage-header finding alive.
    for eid, e in items.items():
        data = e.data
        if not isinstance(data, dict) or data.get("site_check") != "noindex":
            continue
        hits = data.get("detail", {}).get("pages", [])
        refs = [items[i] for i in data.get("source_evidence_ids", []) if i in items]
        decisions = []
        for hit in hits:
            candidates = []
            for ref in refs:
                if not isinstance(ref.data, dict):
                    continue
                response = (
                    ref.data.get("normal", {})
                    if ref.description == "Bounded site-check HTTP probes"
                    else ref.data
                    if ref.description == "SiteOne HTTP response"
                    else {}
                )
                if response.get("url", ref.url) != hit.get("url"):
                    continue
                for header, value in response.get("headers", {}).items():
                    if (
                        header.lower() == "x-robots-tag"
                        and hit.get("source") == f"X-Robots-Tag: {value}"
                    ):
                        candidates.append(challenged(response))
            decisions.append(bool(candidates) and all(candidates))
        if decisions and all(decisions):
            blocked.add(eid)
    while True:
        derived = {
            eid
            for eid, e in items.items()
            if isinstance(e.data, dict)
            and e.data.get("source_evidence_ids")
            and set(e.data["source_evidence_ids"]) <= blocked
        }
        if derived <= blocked:
            return blocked
        blocked |= derived
