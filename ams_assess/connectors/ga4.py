"""GA4 Admin/Data APIs: read-only configuration and bounded aggregate reports."""

import math
import re
from typing import Any

from ams_assess.connectors.auth import google_authenticate, google_authorize
from ams_assess.connectors.core import (
    Connector,
    ConnectorContext,
    ConnectorError,
    Endpoint,
    Period,
    ReadOperation,
)
from ams_assess.connectors.privacy import redact, safe_url
from ams_assess.lifecycle import observe, site_key
from ams_assess.models import ModuleResult

ADMIN = "https://analyticsadmin.googleapis.com/v1beta/"
DATA = "https://analyticsdata.googleapis.com/v1beta/"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


def normalize_resource(value: str) -> str:
    if value.isdigit():
        value = "properties/" + value
    if not re.fullmatch(r"properties/[1-9][0-9]{0,19}", value):
        raise ValueError("GA4 resource must be properties/<numeric property ID>")
    return value


def listing(ctx: ConnectorContext, key: str, path: str, field: str, project) -> Endpoint:
    def read(result: Endpoint) -> None:
        rows: list[Any] = []
        result.data["rows"] = rows
        token, seen = "", set()
        for _ in range(ctx.max_pages):
            params: dict[str, Any] = {"pageSize": ctx.page_size}
            if token:
                params["pageToken"] = token
            body = ctx.http.request("GET", ADMIN + path, params=params)
            for raw in body.get(field, []):
                rows.extend(project(raw))
            result.rows = len(rows)
            token = body.get("nextPageToken", "")
            if len(rows) >= ctx.max_pages * ctx.page_size:
                result.truncated = bool(token) or len(rows) > ctx.max_pages * ctx.page_size
                del rows[ctx.max_pages * ctx.page_size :]
                result.rows = len(rows)
                return
            if not token:
                return
            if token in seen:
                result.truncated = True
                return
            seen.add(token)
        result.truncated = bool(token)

    return ctx.capture(key, ADMIN + path, read)


def list_resources(ctx: ConnectorContext) -> Endpoint:
    return listing(
        ctx,
        "resources",
        "accountSummaries",
        "accountSummaries",
        lambda account: [
            {"id": normalize_resource(p["property"]), "label": redact(p.get("displayName", ""))}
            for p in account.get("propertySummaries", [])
        ],
    )


def test(ctx: ConnectorContext) -> Endpoint:
    def read(result: Endpoint) -> None:
        body = ctx.http.request("GET", ADMIN + ctx.resource)
        if body.get("name") != ctx.resource:
            raise ValueError("Unexpected resource")
        result.data = {
            k: body.get(k, "")
            for k in (
                "name",
                "displayName",
                "timeZone",
                "currencyCode",
                "industryCategory",
                "createTime",
            )
        }
        result.rows = 1
        if body.get("timeZone"):
            try:
                ctx.period = Period.make(
                    days=(ctx.period.end - ctx.period.start).days + 1,
                    start="" if ctx.period.automatic else str(ctx.period.start),
                    end="" if ctx.period.automatic else str(ctx.period.end),
                    zone=body["timeZone"],
                )
            except ValueError:
                raise ConnectorError(
                    "not_configured", "Period must use complete days in the property time zone"
                ) from None
        result.data["permission_evidence"] = (
            "Authenticated property GET succeeded with analytics.readonly"
        )

    return ctx.capture("property", ADMIN + ctx.resource, read)


def report(
    ctx: ConnectorContext,
    key: str,
    dimensions: list[str],
    metrics: list[str],
    *,
    limit: int | None = None,
    event_names: list[str] | None = None,
) -> Endpoint:
    endpoint = DATA + ctx.resource + ":runReport"

    def read(result: Endpoint) -> None:
        rows: list[dict[str, Any]] = []
        result.data["rows"] = rows
        offset = 0
        cap = limit or ctx.max_pages * ctx.page_size
        for _ in range(ctx.max_pages):
            body: dict[str, Any] = {
                "dateRanges": [
                    {"startDate": str(ctx.period.start), "endDate": str(ctx.period.end)}
                ],
                "dimensions": [{"name": d} for d in dimensions],
                "metrics": [{"name": m} for m in metrics],
                "offset": str(offset),
                "limit": str(min(ctx.page_size, cap - offset)),
                "returnPropertyQuota": True,
                "keepEmptyRows": True,
            }
            if dimensions:
                body["orderBys"] = (
                    [{"metric": {"metricName": "sessions"}, "desc": True}]
                    if key == "landing_pages"
                    else []
                ) + [{"dimension": {"dimensionName": d}} for d in dimensions]
            if event_names is not None:
                body["dimensionFilter"] = {
                    "filter": {"fieldName": "eventName", "inListFilter": {"values": event_names}}
                }
            page = ctx.http.request("POST", endpoint, json=body)
            batch = page.get("rows", [])
            if len(batch) > min(ctx.page_size, cap - offset):
                result.truncated = True
                batch = batch[: min(ctx.page_size, cap - offset)]
            for raw in batch:
                dims, values = raw.get("dimensionValues", []), raw.get("metricValues", [])
                if len(dims) != len(dimensions) or len(values) != len(metrics):
                    raise ValueError("Invalid report shape")
                row = {d: v["value"] for d, v in zip(dimensions, dims, strict=True)}
                for metric, value in zip(metrics, values, strict=True):
                    number = float(value["value"])
                    if not math.isfinite(number) or number < 0:
                        raise ValueError("Invalid metric")
                    row[metric] = number
                if "landingPagePlusQueryString" in row:
                    row["landingPagePlusQueryString"] = safe_url(row["landingPagePlusQueryString"])
                if "userEngagementDuration" in row:
                    active = row["activeUsers"]
                    row["averageEngagementTimeSecondsPerActiveUser"] = (
                        row["userEngagementDuration"] / active if active else 0
                    )
                rows.append(redact(row))
            offset += len(batch)
            result.rows = len(rows)
            result.data["row_count"] = int(page.get("rowCount", len(rows)))
            result.data["quota"] = page.get("propertyQuota", {})
            metadata = page.get("metadata", {})
            result.data["quality"] = {
                k: metadata[k]
                for k in ("subjectToThresholding", "dataLossFromOtherRow", "samplingMetadatas")
                if k in metadata
            }
            result.truncated |= any(result.data["quality"].values())
            if offset >= result.data["row_count"]:
                return
            if not batch or offset >= cap:
                result.truncated = True
                return
        result.truncated = True

    return ctx.capture(key, endpoint, read)


def collect(ctx: ConnectorContext) -> None:
    listing(
        ctx,
        "streams",
        ctx.resource + "/dataStreams",
        "dataStreams",
        lambda r: [
            {
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "measurement_id": r.get("webStreamData", {}).get("measurementId", ""),
                "default_uri": safe_url(r.get("webStreamData", {}).get("defaultUri", "")),
            }
        ],
    )
    events = listing(
        ctx,
        "key_events",
        ctx.resource + "/keyEvents",
        "keyEvents",
        lambda r: [{"event_name": r["eventName"], "create_time": r.get("createTime", "")}],
    )
    for key, field in (("custom_dimensions", "customDimensions"), ("ads_links", "googleAdsLinks")):
        result = listing(ctx, key, ctx.resource + "/" + field, field, lambda r: [1])
        result.data = {"count": result.rows}  # Counts only; no account IDs or inventories.

    def retention(result: Endpoint) -> None:
        body = ctx.http.request("GET", ADMIN + ctx.resource + "/dataRetentionSettings")
        result.data = {
            k: body[k]
            for k in ("eventDataRetention", "userDataRetention", "resetUserDataOnNewActivity")
            if k in body
        }
        result.rows = 1

    ctx.capture("retention", ADMIN + ctx.resource + "/dataRetentionSettings", retention)
    report(
        ctx,
        "daily",
        ["date"],
        [
            "sessions",
            "totalUsers",
            "activeUsers",
            "engagedSessions",
            "engagementRate",
            "userEngagementDuration",
        ],
    )
    report(
        ctx,
        "acquisition",
        ["sessionDefaultChannelGroup", "sessionSourceMedium"],
        ["sessions", "totalUsers", "engagedSessions", "keyEvents"],
    )
    report(
        ctx,
        "landing_pages",
        ["landingPagePlusQueryString"],
        ["sessions", "totalUsers", "keyEvents"],
        limit=ctx.top_n,
    )
    report(ctx, "events", ["eventName"], ["eventCount", "keyEvents"])
    report(ctx, "event_totals", [], ["eventCount"])
    names = [r["event_name"] for r in events.data.get("rows", [])]
    if names:
        report(
            ctx, "key_event_counts", ["eventName"], ["eventCount", "keyEvents"], event_names=names
        )


def analyse(ctx: ConnectorContext, result: ModuleResult) -> None:
    endpoints = {e.key: e for e in ctx.endpoints}
    resource_site = "ga4://" + ctx.resource

    def check(key, failed, title, detail, refs, *, inferred=False, component=""):
        component = ctx.resource + (":" + component if component else "")
        outcome = observe(
            "ga4", "ga4:" + key, "cro_ux", component, "fail" if failed else "pass", refs
        )
        outcome.site = resource_site
        result.outcomes.append(outcome)
        if failed:
            result.finding(
                "ga4:" + key,
                title,
                "cro_ux",
                refs,
                detail,
                "Verify measurement with the customer and recheck the same property and period.",
                site=resource_site,
                component=component,
                coverage=[component],
                confidence="needs_verification" if inferred else "confirmed",
                severity="low" if inferred else "medium",
            )

    totals = endpoints.get("event_totals")
    if totals and totals.complete:
        total = sum(r["eventCount"] for r in totals.data.get("rows", []))
        check(
            "no-events",
            total == 0,
            "GA4 property recorded no events in the period",
            "The Data API returned zero events for the selected period; this does not establish "
            "whether the site received traffic.",
            [totals.evidence_id],
        )
    configured, recorded = endpoints.get("key_events"), endpoints.get("key_event_counts")
    if configured and configured.complete:
        names = [r["event_name"] for r in configured.data.get("rows", [])]
        enquiry = any(
            re.search(r"generate_lead|form_submit|contact|quote|enquir|inquir", n, re.I)
            for n in names
        )
        check(
            "enquiry-config",
            not enquiry,
            "No enquiry-related key event configured (inferred)",
            "Names do not suggest a completed enquiry. Verify event semantics; clicks and "
            "form_start are not completed enquiries.",
            [configured.evidence_id],
            inferred=True,
        )
        if recorded and recorded.complete:
            counts = {r["eventName"]: r["eventCount"] for r in recorded.data.get("rows", [])}
            for name in names:
                check(
                    "unrecorded:" + name,
                    counts.get(name, 0) == 0,
                    "Key event configured but not recorded: " + name,
                    "This currently configured key event has zero recorded events in the period. "
                    "Check configuration date and instrumentation; "
                    "no completed enquiries are inferred.",
                    [configured.evidence_id, recorded.evidence_id],
                    component=name,
                )
    streams = endpoints.get("streams")
    if not streams or not streams.complete:
        return
    hosts = {site_key(w) for w in ctx.websites}
    public = [
        e
        for e in ctx.public_evidence
        if e.source == "presence" and isinstance(e.data, dict) and site_key(e.url) in hosts
    ]
    for row in streams.data.get("rows", []):
        if row.get("type") != "WEB_DATA_STREAM":
            continue
        identifier = row["measurement_id"]
        if hosts and row["default_uri"]:
            check(
                "stream-site:" + row["name"],
                site_key(row["default_uri"]) not in hosts,
                "GA4 web stream default URI differs from the customer website",
                "Selected stream default URI: " + row["default_uri"] + ". Verify the binding.",
                [streams.evidence_id],
                component=row["name"],
            )
        stream_public = [e for e in public if site_key(e.url) == site_key(row["default_uri"])]
        signals = [e for e in stream_public if e.data.get("platform") == "ga4"]
        sampled = [e for e in stream_public if e.data.get("pages_sampled")]
        if not identifier or not (signals or sampled):
            continue
        refs = [e.id for e in signals or sampled]
        seen = any(identifier in e.data.get("ids", []) for e in signals)
        related = [
            f["id"] for f in ctx.public_findings if set(f.get("evidence_ids", [])) & set(refs)
        ]
        if seen or not related:
            check(
                "stream-tag:" + row["name"],
                not seen,
                "Selected GA4 measurement ID not seen in the public sample",
                "The bound stream's measurement ID differs from the available public sample. "
                "Consent or sampling may explain this; verify before changing configuration.",
                [streams.evidence_id],
                inferred=True,
                component=row["name"],
            )
        # Reuse an existing public finding instead of creating a duplicate missing-tag finding.
        streams.data.setdefault("public_comparisons", []).append(
            {
                "measurement_id": identifier,
                "seen_in_public_sample": seen,
                "evidence_ids": refs,
                "related_finding_ids": related,
                "detail": "Measurement ID seen in public evidence"
                if seen
                else "No matching measurement ID in the public sample; "
                "consent or sampling may explain this. "
                "Needs verification. See the referenced public observation.",
            }
        )


CONNECTOR = Connector(
    key="ga4",
    label="Google Analytics 4",
    provider="google",
    auth_kind="google_oauth",
    scopes=(SCOPE,),
    resource_type="property",
    read_allowlist=(
        ReadOperation("GET", re.escape(ADMIN) + r"accountSummaries"),
        ReadOperation(
            "GET",
            re.escape(ADMIN) + r"properties/[1-9][0-9]{0,19}(?:/"
            r"(?:dataStreams|keyEvents|customDimensions|googleAdsLinks|dataRetentionSettings))?",
        ),
        ReadOperation("POST", re.escape(DATA) + r"properties/[1-9][0-9]{0,19}:runReport"),
    ),
    normalize_resource=normalize_resource,
    list_resources=list_resources,
    test=test,
    collect=collect,
    analysers=(analyse,),
    authenticate=google_authenticate,
    authorize=google_authorize,
)
