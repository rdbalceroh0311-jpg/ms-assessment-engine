"""Bing Webmaster Tools read adapter; independent of Bing Places.

Official method contracts and their limitations are linked in the README. JSON faults:
https://learn.microsoft.com/en-us/bingwebmaster/getting-started
https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.apierrorcode
"""

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ams_assess.connectors.auth import load_reference
from ams_assess.connectors.core import (
    AuthMaterial,
    Connector,
    ConnectorContext,
    ConnectorError,
    Endpoint,
    ReadOperation,
)
from ams_assess.connectors.privacy import safe_url
from ams_assess.lifecycle import observe, site_identity, site_key
from ams_assess.models import ModuleResult

BASE = "https://ssl.bing.com/webmaster/api.svc/json/"
METHODS = {
    "site": "GetUserSites",
    "traffic": "GetRankAndTrafficStats",
    "queries": "GetQueryStats",
    "pages": "GetPageStats",
    "crawl": "GetCrawlStats",
    "crawl_issues": "GetCrawlIssues",
    "feeds": "GetFeeds",
    "roles": "GetSiteRoles",
    "blocked_urls": "GetBlockedUrls",
}
READ_PERMISSION = "Bing Webmaster Tools ReadOnly access to the exact verified site"
KEY_ACTION = (
    "Update BING_WMT_API_KEY in ~/.config/ams-assess/credentials.env; register with "
    "ams-assess connect bing_wmt auth --credential-ref <name> --env-var BING_WMT_API_KEY"
)
UNSUPPORTED = "Endpoint unsupported/deprecated by Bing; no conclusion about site data"
CRAWL_FIELDS = (
    "AllOtherCodes",
    "BlockedByRobotsTxt",
    "Code2xx",
    "Code301",
    "Code302",
    "Code4xx",
    "Code5xx",
    "ContainsMalware",
    "CrawlErrors",
    "CrawledPages",
    "InIndex",
    "InLinks",
)
ISSUES = {
    1: "Code301",
    2: "Code302",
    4: "Code4xx",
    8: "Code5xx",
    16: "BlockedByRobotsTxt",
    32: "ContainsMalware",
    64: "ImportantUrlBlockedByRobotsTxt",
    128: "DnsErrors",
    256: "TimeOutErrors",
}
ROLES = {0: "Administrator", 1: "ReadOnly", 2: "ReadWrite"}


def normalize_resource(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or re.search(r"[\s\\\x00-\x1f\x7f]", value)
        or safe_url(value) != value
        or len(value) > 1000
    ):
        raise ValueError("Use the exact credential-free site URL returned by GetUserSites")
    return value  # Do not guess scheme, www, path or trailing-slash aliases.


def authenticate(spec: Connector, ref: str, *, directory: Path | None = None) -> AuthMaterial:
    try:
        key = load_reference(spec, ref, directory=directory)
        if isinstance(key, dict):
            key = key.get("api_key")
        if not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,512}", key):
            raise ValueError
        return AuthMaterial(secrets=(key,), private_data=key)
    except (ConnectorError, ValueError):
        raise ConnectorError("credential_missing", KEY_ACTION) from None


class BingTransport(httpx.BaseTransport):
    """Normalize documented HTTP-400 ApiFaults; ReadClient still owns all retries.

    The framework's module-owned transport hook is used only for Bing's error dialect.
    Successful response streaming, request caps, allowlist and deadlines stay in ReadClient.
    """

    def __init__(self, auth: AuthMaterial, *, inner: httpx.BaseTransport | None = None):
        self.inner = inner if inner is not None else httpx.HTTPTransport(trust_env=False)

    def close(self) -> None:
        self.inner.close()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        if response.status_code == 501:
            response.close()
            raise ConnectorError("provider_unavailable", UNSUPPORTED)
        if response.status_code != 400:
            return response
        # Fault bodies are small. Bound this adapter's buffering as well as shared I/O.
        deadline = time.monotonic() + min(10, request.extensions["timeout"]["read"])
        content = bytearray()
        try:
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 64_000:
                    raise ConnectorError("failed", "Bing fault response exceeded size cap")
                if time.monotonic() >= deadline:
                    raise httpx.ReadTimeout("Bing fault read timeout")
        finally:
            response.close()
        try:
            body = json.loads(content)
            code = body.get("ErrorCode") if isinstance(body, dict) else None
            if type(code) is not int:
                raise ValueError
        except (ValueError, TypeError):
            raise ConnectorError("failed", "Invalid Bing fault response") from None
        if code in {1, 2, 4, 5}:
            return httpx.Response(
                429 if code in {4, 5} else 503,
                headers={"Retry-After": response.headers.get("Retry-After", "0")},
                json={},
            )
        if code == 3:
            raise ConnectorError(
                "credential_expired", "Bing API key invalid/revoked. " + KEY_ACTION
            )
        if code in {6, 13, 14}:
            raise ConnectorError("permission_required", "Required: " + READ_PERMISSION)
        if code in {10, 11}:
            raise ConnectorError("not_found", "Bing could not find the requested resource/access")
        if code == 16:
            raise ConnectorError("provider_unavailable", UNSUPPORTED)
        raise ConnectorError("failed", "Bing rejected the read; check method parameters")


def request_rows(ctx: ConnectorContext, method: str) -> list[dict[str, Any]]:
    params = {"apikey": ctx.http.auth.private_data}
    if method != "GetUserSites":
        params["siteUrl"] = ctx.resource
    if method == "GetSiteRoles":
        params["includeAllSubdomains"] = "false"
    try:
        body = ctx.http.request("GET", BASE + method, params=params)
    except ConnectorError as exc:
        if exc.status == "credential_expired":
            raise ConnectorError(
                exc.status, "Bing API key invalid/revoked. " + KEY_ACTION
            ) from None
        if exc.status == "permission_required":
            raise ConnectorError(exc.status, "Required: " + READ_PERMISSION) from None
        # An HTTP 404 here addresses a method route, not a site URL. ApiFault.NotFound
        # remains distinct and is emitted by the adapter with a different message.
        if exc.detail == "Bound resource was not found":
            raise ConnectorError(
                "provider_unavailable", "Endpoint unavailable (HTTP 404); availability unconfirmed"
            ) from None
        if exc.detail in {"Provider rejected read (HTTP 405)", "Provider rejected read (HTTP 410)"}:
            raise ConnectorError("provider_unavailable", UNSUPPORTED) from None
        raise
    rows = body.get("d")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("Invalid Bing list")
    # None of these documented methods has a paging parameter. Never invent a token call.
    if body.get("nextPageToken") or body.get("nextLink") or body.get("__next"):
        raise ConnectorError("failed", "Unexpected pagination in a non-paginated Bing method")
    return rows


def capture(ctx: ConnectorContext, key: str, read) -> Endpoint:
    endpoint = ctx.capture(key, BASE + METHODS[key], read)
    endpoint.data["availability"] = (
        "unsupported"
        if endpoint.detail == UNSUPPORTED
        else "available"
        if endpoint.status == "connected"
        else "unavailable"
    )
    return endpoint


def list_resources(ctx: ConnectorContext) -> Endpoint:
    def read(result: Endpoint) -> None:
        rows = request_rows(ctx, "GetUserSites")
        cap = ctx.max_pages * ctx.page_size
        result.truncated = len(rows) > cap
        result.data["rows"] = [
            {"id": normalize_resource(r["Url"]), "verified": True}
            for r in rows[:cap]
            if r.get("IsVerified") is True
        ]
        result.rows = len(result.data["rows"])

    return capture(ctx, "site", read)


def test(ctx: ConnectorContext) -> Endpoint:
    def read(result: Endpoint) -> None:
        rows = request_rows(ctx, "GetUserSites")
        cap = ctx.max_pages * ctx.page_size
        for row in rows[:cap]:
            if row.get("Url") != ctx.resource:
                continue
            if row.get("IsVerified") is not True:
                raise ConnectorError("permission_required", "Required: " + READ_PERMISSION)
            result.data = {
                "site": ctx.resource,
                "verified": True,
                "permission_evidence": "Exact site returned by GetUserSites with IsVerified=true",
            }
            result.rows = 1
            return
        if len(rows) > cap:
            raise ConnectorError(
                "provider_unavailable", "Site enumeration capped; selection unknown"
            )
        raise ConnectorError("not_found", "Exact bound site not returned by GetUserSites")

    return capture(ctx, "site", read)


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid metric")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid metric")
    return value


def bing_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("Invalid date")
    match = re.fullmatch(r"/Date\((-?\d+)([+-]\d{4})?\)/", value)
    if not match:
        return datetime.fromisoformat(value).date()
    stamp, offset = match.groups()
    zone = UTC
    if offset:
        hours, minutes = int(offset[1:3]), int(offset[3:5])
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid date offset")
        zone = timezone(timedelta(minutes=(hours * 60 + minutes) * (-1 if offset[0] == "-" else 1)))
    return datetime.fromtimestamp(int(stamp) / 1000, zone).date()


def collect(ctx: ConnectorContext) -> None:
    for key in ("traffic", "queries", "pages", "crawl"):

        def dated(result: Endpoint, key=key) -> None:
            result.data = {"rows": [], "date_basis": "Provider date; filtered locally"}
            if key in {"queries", "pages"}:
                result.data["scope"] = "Provider top rows, updated weekly; not all traffic"
            else:
                result.data["scope"] = "Provider daily rows; missing dates are unknown, not zero"
            result.detail = result.data["scope"]
            rows = request_rows(ctx, METHODS[key])
            selected = []
            for raw in rows:
                day = bing_date(raw["Date"])
                if ctx.period.start <= day <= ctx.period.end:
                    selected.append((day, raw))
            selected.sort(key=lambda item: item[0], reverse=True)
            cap = ctx.max_pages * ctx.page_size
            result.truncated = len(selected) > cap
            for day, raw in selected[:cap]:
                fields = CRAWL_FIELDS if key == "crawl" else ("Clicks", "Impressions")
                row: dict[str, Any] = {"date": str(day)}
                row.update({field: number(raw[field]) for field in fields})
                if key in {"queries", "pages"}:
                    query = raw["Query"]
                    if not isinstance(query, str):
                        raise ValueError("Invalid query")
                    if key == "queries":
                        row["query_id"] = hashlib.sha256(query.encode()).hexdigest()[:24]
                    else:
                        row["page"] = safe_url(query)
                    for field in ("AvgClickPosition", "AvgImpressionPosition"):
                        row[field] = number(raw[field])
                result.data["rows"].append(row)
                result.rows += 1
            result.data["totals"] = {
                field: sum(r[field] for r in result.data["rows"])
                for field in (("Clicks", "Impressions") if key != "crawl" else ())
            }
            result.data["dates_returned"] = sorted({r["date"] for r in result.data["rows"]})

        capture(ctx, key, dated)

    for key in ("crawl_issues", "feeds", "roles", "blocked_urls"):

        def snapshot(result: Endpoint, key=key) -> None:
            rows = request_rows(ctx, METHODS[key])
            cap = ctx.max_pages * ctx.page_size
            result.truncated = len(rows) > cap
            result.data = {"scope": "Current provider snapshot, not historical period", "rows": []}
            result.detail = result.data["scope"]
            for raw in rows[:cap]:
                if key == "feeds":
                    status = raw["Status"]
                    if not isinstance(status, str) or not status:
                        raise ValueError("Invalid feed status")
                    row = {
                        "url": safe_url(raw["Url"]),
                        "status": status,
                        "url_count": number(raw["UrlCount"]),
                    }
                    for source, target in (("Submitted", "submitted"), ("LastCrawled", "crawled")):
                        if raw.get(source):
                            row[target] = str(bing_date(raw[source]))
                    result.data["rows"].append(row)
                elif key == "crawl_issues":
                    mask = raw["Issues"]
                    if type(mask) is not int or mask < 0 or mask & ~511:
                        raise ValueError("Unknown crawl issue flag")
                    counts = result.data.setdefault("issue_counts", {})
                    for bit, name in ISSUES.items():
                        if mask & bit:
                            counts[name] = counts.get(name, 0) + 1
                    number(raw["HttpCode"])
                elif key == "roles":
                    role = raw["Role"]
                    expired = raw["Expired"]
                    if type(role) is not int or role not in ROLES or type(expired) is not bool:
                        raise ValueError("Invalid role")
                    counts = result.data.setdefault("role_counts", {})
                    label = ROLES[role] + (" (expired)" if expired else "")
                    counts[label] = counts.get(label, 0) + 1
                else:
                    # Counts only: blocked URL paths can include personal data.
                    if not isinstance(raw["Url"], str):
                        raise ValueError("Invalid blocked URL")
                result.rows += 1
            result.data["count"] = result.rows

        capture(ctx, key, snapshot)


def analyse(ctx: ConnectorContext, result: ModuleResult) -> None:
    endpoints = {e.key: e for e in ctx.endpoints}
    resource_site = "bing-wmt://" + ctx.resource

    def check(
        key: str,
        failed: bool,
        title: str,
        detail: str,
        endpoint: Endpoint,
        *,
        public_checks: tuple[str, ...] = (),
    ) -> None:
        if not endpoint.complete:
            return
        related = [
            f
            for f in ctx.public_findings
            if f.get("check_key") in public_checks
            and site_identity(f.get("site") or "") == site_key(ctx.resource)
            and f.get("lifecycle_status", "current") in {"current", "not_rechecked"}
        ]
        if failed and related:
            endpoint.data.setdefault("public_references", []).append(
                {
                    "related_finding_ids": [f["id"] for f in related],
                    "evidence_ids": list(
                        dict.fromkeys(eid for f in related for eid in f.get("evidence_ids", []))
                    ),
                    "detail": detail + " See the existing public finding; not duplicated.",
                }
            )
            # Endpoint detail is the framework's provider-neutral report/UI surface.
            # Its public_comparisons template is specific to GA4 measurement IDs.
            endpoint.detail += (
                " "
                + detail
                + " See existing public findings: "
                + ", ".join(f["id"] for f in related)
                + "; evidence: "
                + ", ".join(
                    dict.fromkeys(eid for f in related for eid in f.get("evidence_ids", []))
                )
                + "."
            )
            return
        outcome = observe(
            "bing_wmt",
            "bing_wmt:" + key,
            "seo",
            ctx.resource,
            "fail" if failed else "pass",
            [endpoint.evidence_id],
        )
        outcome.site = resource_site
        result.outcomes.append(outcome)
        if failed:
            result.finding(
                "bing_wmt:" + key,
                title,
                "seo",
                [endpoint.evidence_id],
                detail,
                "Review the exact site in Bing Webmaster Tools and verify intended visibility.",
                site=resource_site,
                component=ctx.resource,
                coverage=[ctx.resource],
                confidence="confirmed",
            )

    crawl = endpoints.get("crawl")
    if crawl and crawl.data.get("rows"):
        rows = crawl.data["rows"]
        check(
            "crawl-errors",
            any(r["CrawlErrors"] or r["Code4xx"] or r["Code5xx"] for r in rows),
            "Bing reports crawl errors",
            "Bing returned crawl errors or HTTP 4xx/5xx counts "
            "on the returned dates in the selected period; counts are not unique pages.",
            crawl,
        )
        check(
            "robots-blocked",
            any(r["BlockedByRobotsTxt"] for r in rows),
            "Bing reports pages blocked by robots.txt",
            "Bing returned blocked-by-robots counts in the selected period. "
            "Intentional restrictions may be appropriate.",
            crawl,
            public_checks=("site:robots",),
        )
    issues = endpoints.get("crawl_issues")
    if issues:
        counts = issues.data.get("issue_counts", {})
        check(
            "crawl-issues",
            any(counts.values()),
            "Bing reports current crawl issues",
            "Bing's current issue snapshot contains crawl issues (including redirects). "
            "Fixed issues may remain for several days; verify before remediation.",
            issues,
        )
        check(
            "issue-blocked",
            bool(counts.get("BlockedByRobotsTxt") or counts.get("ImportantUrlBlockedByRobotsTxt")),
            "Bing crawl issues include blocked pages",
            "Bing's current issue snapshot "
            "flags pages blocked by robots.txt; verify whether intentional.",
            issues,
            public_checks=("site:robots",),
        )
    feeds = endpoints.get("feeds")
    if feeds:
        rows = feeds.data.get("rows", [])
        check(
            "no-sitemap",
            not rows,
            "Bing has no submitted sitemap/feed",
            "GetFeeds returned an empty list for the exact verified site. "
            "This does not mean that the public website has no sitemap.",
            feeds,
        )
        errors = [
            r
            for r in rows
            if r["status"].strip().casefold()
            in {
                "error",
                "failed",
                "failure",
                "fetch error",
                "parse error",
                "download error",
                "download failed",
            }
        ]
        # Unknown or pending statuses cannot establish a passing recheck.
        if errors or (rows and all(r["status"].casefold() == "success" for r in rows)):
            check(
                "sitemap-errors",
                bool(errors),
                "Bing reports sitemap/feed errors",
                "Bing reports an error/failure status for a submitted sitemap/feed.",
                feeds,
                public_checks=("site:sitemap-urls",),
            )
    blocked = endpoints.get("blocked_urls")
    if blocked:
        check(
            "blocked-urls",
            bool(blocked.rows),
            "Bing has blocked URLs/directories",
            "GetBlockedUrls returned blocking entries for the selected site. "
            "Review intent; this does not establish that the blocks are inappropriate.",
            blocked,
        )
    compare_gsc(ctx, result, endpoints.get("pages"), resource_site)


def compare_gsc(
    ctx: ConnectorContext, result: ModuleResult, pages: Endpoint | None, resource_site: str
) -> None:
    from ams_assess.modules.gsc import matches_target

    if not pages or not pages.complete or not pages.rows:
        return
    candidates = []
    for evidence in ctx.connected_evidence:
        data = evidence.data
        if not isinstance(data, Mapping) or evidence.source != "gsc":
            continue
        if (
            data.get("connector") == "gsc"
            and data.get("operation") == "page"
            and matches_target(data.get("resource_id", ""), ctx.resource)
            and data.get("period") == ctx.period.json()
        ):
            candidates.append(
                (
                    evidence,
                    data.get("data", {}).get("rows", []),
                    data.get("status") == "connected"
                    and data.get("coverage", {}).get("truncated") is False,
                )
            )
        elif (
            not data.get("connector")
            and data.get("property")
            and matches_target(data["property"], ctx.resource)
            and data.get("start_date") == str(ctx.period.start)
            and data.get("end_date") == str(ctx.period.end)
        ):
            # The legacy GSC collector emits evidence only after all reads succeed.
            raw_rows = data.get("page", {}).get("rows", [])
            rows = [{**r, "value": r["keys"][0]} for r in raw_rows if r.get("keys")]
            candidates.append((evidence, rows, len(raw_rows) < 100))
    # Never fall back to a stale success when the latest matching observation failed.
    if not candidates:
        return
    other, rows, complete = max(candidates, key=lambda item: item[0].data.get("collected_at", ""))
    if not complete:
        return
    # Domain/parent-prefix GSC properties can contain other sites. Compare only page rows
    # inside the bound Bing URL prefix; never incorporate another hostname or scheme.
    rows = [
        r
        for r in rows
        if isinstance(r.get("value"), str) and matches_target(ctx.resource, r["value"])
    ]
    if not rows:
        return
    totals = {k: sum(number(r[k]) for r in rows) for k in ("clicks", "impressions")}
    pages.data["gsc_comparison"] = {
        "gsc_evidence_id": other.id,
        "gsc_page_totals": totals,
        "bing_page_totals": pages.data["totals"],
        "label": "Context only; different engines, coverage, verticals and reporting cadence",
    }
    result.finding(
        "bing_wmt:gsc-comparison",
        "Bing and GSC page traffic comparison (needs verification)",
        "seo",
        [pages.evidence_id, other.id],
        f"Bing returned {pages.data['totals']['Clicks']} clicks and "
        f"{pages.data['totals']['Impressions']} impressions in top-page records; "
        f"GSC returned {totals['clicks']} clicks and {totals['impressions']} impressions "
        "in page rows for the same site URL prefix and selected period. Bing rows update weekly; "
        "search surfaces and top-row coverage differ. This is not a like-for-like comparison "
        "and does not establish lost traffic or a ranking problem.",
        "Verify reporting coverage and cadence before interpreting differences.",
        confidence="needs_verification",
        severity="info",
        kind="opportunity",
        site=resource_site,
        component=ctx.resource,
        coverage=[ctx.resource],
    )
    outcome = observe(
        "bing_wmt",
        "bing_wmt:gsc-comparison",
        "seo",
        ctx.resource,
        "fail",
        [pages.evidence_id, other.id],
    )
    outcome.site = resource_site
    result.outcomes.append(outcome)


CONNECTOR = Connector(
    key="bing_wmt",
    label="Bing Webmaster Tools",
    provider="bing_wmt",
    auth_kind="api_key",
    auth_flow="reference",
    auth_hint=(
        "ams-assess connect bing_wmt auth --credential-ref <name> --env-var BING_WMT_API_KEY"
    ),
    collection_seconds=45,
    max_requests=27,  # Nine read methods, at most three attempts per method.
    scopes=(READ_PERMISSION,),
    resource_type="site",
    categories=frozenset({"seo", "technical"}),
    read_allowlist=tuple(
        ReadOperation("GET", re.escape(BASE + method)) for method in METHODS.values()
    ),
    normalize_resource=normalize_resource,
    list_resources=list_resources,
    test=test,
    collect=collect,
    analysers=(analyse,),
    authenticate=authenticate,
    transport_factory=BingTransport,
)
