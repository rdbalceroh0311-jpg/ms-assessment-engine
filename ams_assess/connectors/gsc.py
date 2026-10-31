"""Bound-resource GSC adapter; legacy gsc-auth and unbound --connect gsc remain compatible."""

import re
from urllib.parse import quote

from ams_assess.connectors.auth import google_authenticate, google_authorize
from ams_assess.connectors.connected import site_scope
from ams_assess.connectors.core import (
    Connector,
    ConnectorContext,
    ConnectorError,
    Endpoint,
    ReadOperation,
)
from ams_assess.connectors.privacy import safe_url
from ams_assess.models import Evidence, ModuleResult, normalize_target

BASE = "https://www.googleapis.com/webmasters/v3/sites"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def normalize_resource(value: str) -> str:
    if value.startswith("sc-domain:"):
        host = value.removeprefix("sc-domain:")
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
            raise ValueError("Invalid GSC domain property")
        return value
    return normalize_target(value)


def list_resources(ctx: ConnectorContext) -> Endpoint:
    def read(result: Endpoint) -> None:
        rows = ctx.http.request("GET", BASE).get("siteEntry", [])
        result.data = {
            "rows": [
                {"id": normalize_resource(r["siteUrl"]), "permission": r.get("permissionLevel", "")}
                for r in rows[:500]
            ]
        }
        result.rows, result.truncated = len(result.data["rows"]), len(rows) > 500

    return ctx.capture("resources", BASE, read)


def test(ctx: ConnectorContext) -> Endpoint:
    endpoint = BASE + "/" + quote(ctx.resource, safe="")

    def read(result: Endpoint) -> None:
        data = ctx.http.request("GET", endpoint)
        if data.get("siteUrl") != ctx.resource:
            raise ValueError("Unexpected resource")
        if data.get("permissionLevel") not in {"siteOwner", "siteFullUser", "siteRestrictedUser"}:
            raise ConnectorError(
                "permission_required", "GSC property read permission required: " + SCOPE
            )
        result.data = {
            "property": ctx.resource,
            "permission_evidence": "Authenticated property read: " + data["permissionLevel"],
        }
        result.rows = 1

    return ctx.capture("property", endpoint, read)


def collect(ctx: ConnectorContext) -> None:
    base = BASE + "/" + quote(ctx.resource, safe="")
    for dimension in ("query", "page"):

        def read(result: Endpoint, dimension=dimension) -> None:
            rows = []
            result.data["rows"] = rows
            for page in range(ctx.max_pages):
                response = ctx.http.request(
                    "POST",
                    base + "/searchAnalytics/query",
                    json={
                        "startDate": str(ctx.period.start),
                        "endDate": str(ctx.period.end),
                        "dimensions": [dimension],
                        "rowLimit": ctx.page_size,
                        "startRow": page * ctx.page_size,
                        "dataState": "final",
                    },
                )
                batch = response.get("rows", [])
                rows.extend(
                    {
                        "value": safe_url(r["keys"][0]) if dimension == "page" else r["keys"][0],
                        **{k: r[k] for k in ("clicks", "impressions", "ctr", "position")},
                    }
                    for r in batch
                )
                result.rows = len(rows)
                if len(batch) < ctx.page_size:
                    return
            result.truncated = True

        ctx.capture(dimension, base + "/searchAnalytics/query", read)

    def sitemaps(result: Endpoint) -> None:
        data = ctx.http.request("GET", base + "/sitemaps").get("sitemap", [])
        result.rows = min(len(data), 500)
        result.truncated = len(data) > 500
        result.data = {"count": result.rows}

    ctx.capture("sitemaps", base + "/sitemaps", sitemaps)


def recheck_findings(result: ModuleResult, current: dict, public: list[Evidence]) -> None:
    """Re-evaluate existing GSC checks only for their exact bound property.

    Adapt complete endpoint samples to the existing analyser, citing the actual endpoint
    evidence. A failed/capped endpoint cannot resolve an old finding; public findings and
    other properties are never re-evaluated by this standalone collection.
    """
    from ams_assess.modules.gsc import connected_findings

    resource = current.get("connectors", {}).get("gsc", {}).get("resource_id", "")
    scope = site_scope(resource)
    if scope is None:
        return
    endpoints = {
        e.data["operation"]: e
        for e in result.evidence
        if e.data.get("status") == "connected"
        and not e.data.get("coverage", {}).get("truncated")
        and site_scope(e.data.get("resource_id")) == scope
    }
    previous = {e["id"]: e for e in current.get("evidence", [])}
    operations = {
        "gsc:sitemap-not-submitted": "sitemaps",
        "gsc:non-canonical-variants": "page",
        "gsc:placeholder-impressions": "page",
    }
    for finding in current.get("findings", []):
        check = finding.get("check_key", "")
        endpoint = endpoints.get(operations.get(check, ""))
        if endpoint is None or not finding.get("component"):
            continue
        cited = [previous.get(eid) for eid in finding["evidence_ids"]]
        if any(e is None for e in cited):
            continue
        own = [e for e in cited if e is not None and e["source"] == "gsc"]
        if not own or any(
            site_scope(e["data"].get("resource_id", e["data"].get("property"))) != scope
            for e in own
        ):
            continue
        payload = endpoint.data["data"]
        data = {
            "property": resource,
            "page": {"rows": [{**row, "keys": [row["value"]]} for row in payload.get("rows", [])]},
        }
        if check == "gsc:sitemap-not-submitted":
            # The existing check only needs to know whether the submitted list is empty.
            data["sitemaps"] = {"sitemap": [{}] if payload["count"] else []}
        record = endpoint.model_copy(update={"url": finding["component"], "data": data})
        findings, outcomes = connected_findings([record, *public], finding["component"])
        result.findings.extend(f for f in findings if f.check_key == check)
        result.outcomes.extend(o for o in outcomes if o.check_id == check)


CONNECTOR = Connector(
    key="gsc",
    label="Google Search Console",
    provider="google",
    auth_kind="google_oauth",
    scopes=(SCOPE,),
    resource_type="site",
    read_allowlist=(
        ReadOperation("GET", re.escape(BASE)),
        ReadOperation("GET", re.escape(BASE) + r"/[a-zA-Z0-9.%_-]+(?:/sitemaps)?"),
        ReadOperation("POST", re.escape(BASE) + r"/[a-zA-Z0-9.%_-]+/searchAnalytics/query"),
    ),
    normalize_resource=normalize_resource,
    list_resources=list_resources,
    test=test,
    collect=collect,
    analysers=(),
    authenticate=google_authenticate,
    authorize=google_authorize,
    categories=frozenset({"seo", "geo"}),
)
