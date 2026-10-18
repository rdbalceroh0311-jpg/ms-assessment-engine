from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import credentials, service_account

from ams_assess.models import Context, ModuleResult

name = "gsc"
categories = {"seo", "geo"}
requires = [
    "--connect gsc",
    "GOOGLE_APPLICATION_CREDENTIALS or AMS_GSC_TOKEN_FILE",
    "accessible property",
]
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def matches_target(prop: str, target: str) -> bool:
    host = urlsplit(target).hostname or ""
    if prop.startswith("sc-domain:"):
        domain = prop.removeprefix("sc-domain:")
        return host == domain or host.endswith("." + domain)
    p, t = urlsplit(prop), urlsplit(target)
    return (p.scheme, p.netloc) == (t.scheme, t.netloc) and t.path.startswith(p.path)


def run(ctx: Context) -> ModuleResult:
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
        base = "https://www.googleapis.com/webmasters/v3/sites"
        response = client.get(base)
        response.raise_for_status()
        accessible = [
            e["siteUrl"]
            for e in response.json().get("siteEntry", [])
            if e.get("permissionLevel") != "siteUnverifiedUser"
            and matches_target(e["siteUrl"], ctx.target)
        ]
        desired = ctx.config.get("AMS_GSC_PROPERTY")
        prop = (
            desired
            if desired in accessible
            else (accessible[0] if accessible and not desired else None)
        )
        if not prop:
            return ModuleResult(
                name=name, status="skipped", reason="No accessible GSC property matches target"
            )
        endpoint = base + "/" + quote(prop, safe="")
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
