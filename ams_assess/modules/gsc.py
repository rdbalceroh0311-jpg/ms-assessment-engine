import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import credentials, service_account

from ams_assess.lifecycle import observe, site_key
from ams_assess.models import (
    CheckOutcome,
    Connection,
    ConnectionStatus,
    Context,
    Evidence,
    Finding,
    ModuleResult,
)

name = "gsc"
categories = {"seo", "geo"}
requires = [
    "--connect gsc",
    "GOOGLE_APPLICATION_CREDENTIALS or AMS_GSC_TOKEN_FILE",
    "accessible property",
]
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"
# Documentation checked 2026-10-23. The UI report exists; the API reference documents no
# separate generative-AI report type/dimension. Do not interpret aggregate web data as AI data.
AI_REPORT_NOTE = (
    "The generative-AI performance report is available in the Search Console interface. "
    "As of 2026-10-23, the documented API has no separate generative-AI report endpoint or "
    "filter; use a customer-provided UI export for manual review. Aggregate Web results "
    "cannot establish AI visibility. Sources: "
    "https://developers.google.com/webmaster-tools/v1/searchanalytics/query and "
    "https://support.google.com/webmasters/answer/16984139"
)

# Default WordPress content that should not be indexed once a site is live.
PLACEHOLDER_PATHS = {
    "/sample-page/": "WordPress sample page",
    "/hello-world/": "WordPress sample post",
    "/category/uncategorized/": "default Uncategorized category archive",
}
PUBLIC_SITEMAP = {"Bounded robots.txt and XML sitemap fetch", "Reused robots/sitemap collector"}
VERIFY = "Needs verification before any change: "


def connected_findings(
    evidence: list[Evidence], site_url: str
) -> tuple[list[Finding], list[CheckOutcome]]:
    """Deterministic findings from the returned Search Console sample.

    Each observation is confirmed from GSC and cites the GSC evidence plus the public evidence
    it relates to (redirect resolution, public sitemap, crawled page) instead of repeating it.
    Recommendations still need verification. Every analysed check also records an explicit
    outcome, so a later run without the condition can resolve the finding.
    """
    site, preferred = site_key(site_url), urlsplit(site_url)
    record = next(
        (
            e
            for e in evidence
            if e.source == name
            and site_key(e.url) == site
            and isinstance(e.data, dict)
            and isinstance(e.data.get("page"), dict)
        ),
        None,
    )
    if record is None:
        return [], []
    findings: list[Finding] = []
    outcomes: list[CheckOutcome] = []

    def number(value: Any) -> int:
        return int(value) if isinstance(value, int | float) else 0

    rows = {
        str(row["keys"][0]): row
        for row in record.data["page"].get("rows", [])
        if isinstance(row, dict) and row.get("keys")
    }
    own = {
        url: row
        for url, row in rows.items()
        if site_key(url) == site and number(row.get("impressions"))
    }

    def shown(url: str) -> str:
        impressions, clicks = number(rows[url].get("impressions")), number(rows[url].get("clicks"))
        return (
            f"{url} ({impressions} impression{'s' * (impressions != 1)}, "
            f"{clicks} click{'s' * (clicks != 1)})"
        )

    def check(key: str, status: str, finding: dict[str, Any] | None = None) -> None:
        outcomes.append(observe(name, key, "seo", site_url, status, [record.id]))
        if finding:
            findings.append(
                Finding(
                    id=f"gsc-{len(findings) + 1:03d}",
                    check_key=key,
                    category="seo",
                    component=site_url,
                    confidence="confirmed",
                    ams_service="Digital Growth",
                    **finding,
                )
            )

    def public(predicate) -> list[str]:
        return [e.id for e in evidence if e.source != name and predicate(e)]

    variants, variant_urls = [], []
    for url in own:
        parts = urlsplit(url)
        if (parts.scheme, parts.hostname) == (preferred.scheme, preferred.hostname):
            continue
        canonical = urlunsplit(
            (preferred.scheme, preferred.netloc, parts.path or "/", parts.query, "")
        )
        variant_urls.append(url)
        variants.append(
            shown(url) + (f" alongside {shown(canonical)}" if canonical in rows else "")
        )
    redirects = public(
        lambda e: (
            e.description == "Target redirect resolution before crawling"
            and isinstance(e.data, dict)
            and e.data.get("resolved") == site_url
        )
    )
    finding = None
    if variants:
        finding = {
            "title": "Search Console reports non-canonical URL variants with impressions",
            "evidence_ids": [record.id, *redirects],
            "detail": f"Confirmed from Search Console ({record.id}): "
            + "; ".join(variants)
            + (
                f". The website resolves to {site_url}"
                if redirects
                else f". The assessment's preferred URL is {site_url}"
            )
            + (f" ({', '.join(redirects)})" if redirects else "")
            + ".",
            "recommendation": VERIFY + "confirm that each variant permanently redirects (301) "
            f"to its {preferred.scheme}://{preferred.netloc} URL and that canonical tags name "
            "that URL, then recheck Search Console after Google recrawls.",
            "severity": "medium",
            "urls": variant_urls,
        }
    check("gsc:non-canonical-variants", "fail" if variants else "pass", finding)

    submitted = record.data.get("sitemaps")
    submitted = submitted.get("sitemap", []) if isinstance(submitted, dict) else None
    published = [
        e
        for e in evidence
        if e.source != name
        and e.description in PUBLIC_SITEMAP
        and isinstance(e.data, dict)
        and e.data.get("status") == "PASS"
        and site_key(str(e.data.get("sitemap_url") or e.url)) == site
    ]
    finding = None
    if submitted is None or not published:
        status = "unavailable"  # Without both lists, absence cannot be established.
    elif submitted:
        status = "pass"
    else:
        status, sitemap = "fail", published[0]
        location = sitemap.data.get("sitemap_url") or sitemap.url
        finding = {
            "title": "No sitemap submitted in Search Console although the website publishes one",
            "evidence_ids": [record.id, sitemap.id],
            "detail": f"Confirmed from Search Console ({record.id}): property "
            f"{record.data.get('property', 'not recorded')} lists no submitted sitemaps. "
            f"The website publishes {location} ({sitemap.id}).",
            "recommendation": VERIFY + "confirm with the property owner that the sitemap is "
            f"not submitted under another property, then submit {location} in Search Console.",
            "severity": "low",
            "kind": "opportunity",
        }
    check("gsc:sitemap-not-submitted", status, finding)

    placeholders = [url for url in own if urlsplit(url).path in PLACEHOLDER_PATHS]
    pages = public(
        lambda e: any(
            site_key(e.url) == site and urlsplit(e.url).path == urlsplit(url).path
            for url in placeholders
        )
    )
    finding = None
    if placeholders:
        finding = {
            "title": "Placeholder pages receive Search Console impressions",
            "evidence_ids": [record.id, *pages],
            "detail": f"Confirmed from Search Console ({record.id}): "
            + "; ".join(
                f"{shown(url)}, a {PLACEHOLDER_PATHS[urlsplit(url).path]}" for url in placeholders
            )
            + ".",
            "recommendation": VERIFY + "confirm the pages are unused, then remove them (or mark "
            "them noindex) and drop them from the sitemap.",
            "severity": "low",
            "urls": placeholders,
        }
    check("gsc:placeholder-impressions", "fail" if placeholders else "pass", finding)
    return findings, outcomes


def save_token(path: Path, auth: credentials.Credentials) -> None:
    for parent in reversed([parent for parent in path.parents if not parent.exists()]):
        parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(auth.to_json())
        # Replace existing tokens as well, so a prior permissive mode is never reused.
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def list_sites(client: httpx.Client) -> list[dict[str, str]]:
    response = client.get(SITES_URL)
    response.raise_for_status()
    return response.json().get("siteEntry", [])


def verify_token(path: Path) -> list[dict[str, str]]:
    auth = credentials.Credentials.from_authorized_user_file(str(path), scopes=[SCOPE])
    if not auth.valid:
        auth.refresh(Request())
        save_token(path, auth)
    with httpx.Client(headers={"Authorization": f"Bearer {auth.token}"}, timeout=30) as client:
        return list_sites(client)


def matches_target(prop: str, target: str) -> bool:
    host = urlsplit(target).hostname or ""
    if prop.startswith("sc-domain:"):
        domain = prop.removeprefix("sc-domain:")
        return host == domain or host.endswith("." + domain)
    p, t = urlsplit(prop), urlsplit(target)
    return (p.scheme, p.netloc) == (t.scheme, t.netloc) and t.path.startswith(p.path)


def run(ctx: Context) -> ModuleResult:
    state: ConnectionStatus = "Not connected"
    try:
        result = collect(ctx)
        if result.evidence:
            state = "Connected successfully"
        elif "permission" in result.reason.lower() or "accessible" in result.reason.lower():
            state = "Permission required"
        elif "gsc" not in ctx.connections or not (
            ctx.config.get("GOOGLE_APPLICATION_CREDENTIALS") or ctx.config.get("AMS_GSC_TOKEN_FILE")
        ):
            state = "Not connected"
        elif ctx.local:
            state = "Not assessed"
    except httpx.HTTPStatusError as exc:
        state = "Permission required" if exc.response.status_code in {401, 403} else "Not assessed"
        result = ModuleResult(
            name=name, status="skipped", reason=f"GSC HTTP {exc.response.status_code}"
        )
    except Exception as exc:
        state = "Not connected"
        result = ModuleResult(
            name=name, status="error", reason=f"GSC unavailable ({type(exc).__name__})"
        )
    result.connections = [
        Connection(
            key=name,
            service="Google Search Console",
            status=state,
            detail=result.reason + ". " + AI_REPORT_NOTE,
            evidence_ids=[e.id for e in result.evidence],
        )
    ]
    return result


def collect(ctx: Context) -> ModuleResult:
    credential = ctx.config.get("GOOGLE_APPLICATION_CREDENTIALS")
    token = ctx.config.get("AMS_GSC_TOKEN_FILE")
    if "gsc" not in ctx.connections or not (credential or token):
        return ModuleResult(
            name=name,
            status="skipped",
            reason="GSC requires --connect gsc and credentials; missing connection or credentials",
        )
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="GSC requires a verified public property"
        )
    if credential:
        auth = service_account.Credentials.from_service_account_file(
            str(Path(credential).expanduser()), scopes=[SCOPE]
        )
    else:
        auth = credentials.Credentials.from_authorized_user_file(
            str(Path(token or "").expanduser()), scopes=[SCOPE]
        )
    if not auth.valid:
        auth.refresh(Request())
    with httpx.Client(headers={"Authorization": f"Bearer {auth.token}"}, timeout=30) as client:
        sites = list_sites(client)
        desired = ctx.config.get("AMS_GSC_PROPERTY")
        if any(
            e.get("permissionLevel") == "siteUnverifiedUser"
            and matches_target(e["siteUrl"], ctx.target)
            and (not desired or desired == e["siteUrl"])
            for e in sites
        ) and not any(
            e.get("permissionLevel") in {"siteOwner", "siteFullUser", "siteRestrictedUser"}
            and matches_target(e["siteUrl"], ctx.target)
            and (not desired or desired == e["siteUrl"])
            for e in sites
        ):
            return ModuleResult(
                name=name, status="skipped", reason="GSC permission required: siteUnverifiedUser"
            )
        accessible = [
            e["siteUrl"]
            for e in sites
            if e.get("permissionLevel") in {"siteOwner", "siteFullUser", "siteRestrictedUser"}
            and matches_target(e["siteUrl"], ctx.target)
        ]
        prop = (
            desired
            if desired in accessible
            else (accessible[0] if accessible and not desired else None)
        )
        if not prop:
            return ModuleResult(
                name=name, status="skipped", reason="No accessible GSC property matches target"
            )
        endpoint = SITES_URL + "/" + quote(prop, safe="")
        end = datetime.now(UTC).date() - timedelta(days=3)
        start = end - timedelta(days=27)
        data = {"property": prop, "start_date": str(start), "end_date": str(end)}
        for dimension in ["query", "page"]:
            response = client.post(
                endpoint + "/searchAnalytics/query",
                json={
                    "startDate": str(start),
                    "endDate": str(end),
                    "dimensions": [dimension],
                    "rowLimit": 100,
                    "dataState": "final",
                },
            )
            response.raise_for_status()
            data[dimension] = response.json()
        response = client.get(endpoint + "/sitemaps")
        response.raise_for_status()
        data["sitemaps"] = response.json()
    result = ModuleResult(name=name, raw_paths=[ctx.raw(name, data)])
    result.add_evidence(
        ctx.target, "GSC top queries/pages and sitemap list (28 complete days)", data
    )
    result.reason = (
        "Top 100 rows per dimension; 3-day reporting lag; low-volume queries may be omitted"
    )
    return result
