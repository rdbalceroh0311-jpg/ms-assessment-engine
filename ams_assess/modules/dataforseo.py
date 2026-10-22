import time
from decimal import Decimal
from typing import Any

import httpx

from ams_assess.models import Context, ModuleResult

name = "dataforseo"
categories = {"seo", "local_seo", "entity"}
requires = ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "--dataforseo-max-usd"]
# One task and one result: $0.01 task + $0.0001 result, without paid modifiers.
ESTIMATE_USD = Decimal("0.0101")
API = "https://api.dataforseo.com/v3"
SERP_ENDPOINTS = {
    "organic": f"{API}/serp/google/organic/live/advanced",
    "maps": f"{API}/serp/google/maps/live/advanced",
}
# Conservative ceiling for one live SERP page (depth 10, no paid modifiers): about twice the
# list price, so the pre-call check errs towards refusing. The provider's reported cost is
# booked once known.
SERP_ESTIMATE_USD = Decimal("0.004")
SERP_DEPTH = 10
MAX_SERP_CALLS = 12


def cap(ctx: Context) -> Decimal | None:
    if ctx.dataforseo_max_usd is None:
        return None
    value = Decimal(str(ctx.dataforseo_max_usd))
    return value if value.is_finite() else None


def market(ctx: Context) -> dict[str, Any]:
    return {
        "location_code": int(ctx.config.get("DATAFORSEO_LOCATION_CODE", "2036")),
        "language_code": "en",
    }


def run(ctx: Context) -> ModuleResult:
    if not all(ctx.config.get(k) for k in ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"]):
        return ModuleResult(
            name=name, status="skipped", reason="Missing DataForSEO credentials; disabled"
        )
    if ctx.dataforseo_max_usd is None:
        return ModuleResult(
            name=name, status="skipped", reason="Disabled: explicit USD cap is required"
        )
    limit = cap(ctx)
    if limit is None or limit - ctx.dataforseo_spent_usd < ESTIMATE_USD:
        return ModuleResult(
            name=name,
            status="skipped",
            reason=f"Refused: estimated ${ESTIMATE_USD} exceeds the explicit cap",
        )
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="DataForSEO requires a public domain"
        )
    where = market(ctx)
    budget = {"estimated_usd": str(ESTIMATE_USD), "cap_usd": str(limit), "calls": 1, **where}
    ctx.raw("dataforseo-budget", budget)
    # Booked before the call: a request whose outcome is unknown still counts against the cap.
    ctx.dataforseo_spent_usd += ESTIMATE_USD
    # Exactly one bounded request; no retries or pagination that could incur extra charges.
    response = httpx.post(
        f"{API}/dataforseo_labs/google/domain_rank_overview/live",
        auth=(ctx.config["DATAFORSEO_LOGIN"], ctx.config["DATAFORSEO_PASSWORD"]),
        json=[{"target": ctx.host, **where, "limit": 1}],
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    cost = Decimal(str(data.get("cost", ESTIMATE_USD)))
    ctx.dataforseo_spent_usd += cost - ESTIMATE_USD
    result = ModuleResult(name=name, raw_paths=[ctx.raw(name, data), "raw/dataforseo-budget.json"])
    tasks = data.get("tasks", [])
    if data.get("status_code") != 20000 or not tasks or tasks[0].get("status_code") != 20000:
        result.status, result.reason = "error", "DataForSEO task did not complete successfully"
        return result
    if cost > limit:
        result.status, result.reason = (
            "error",
            "Provider charged above estimate/cap; no further calls made",
        )
        return result
    coverage = f"location_code={where['location_code']}, language_code={where['language_code']}"
    result.add_evidence(
        ctx.target,
        f"DataForSEO domain rank overview ({coverage})",
        {**where, "result": tasks[0].get("result")},
    )
    result.reason = (
        f"Database: {coverage}. Estimates depend on provider pricing; "
        "no traffic or revenue guarantee."
    )
    return result


def serp_refusal(ctx: Context) -> str:
    """Why paid SERP calls cannot run at all for this run ('' when they can)."""
    if ctx.dataforseo_max_usd is None:
        return "no --dataforseo-max-usd cap was passed"
    if not all(ctx.config.get(k) for k in ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"]):
        return "DataForSEO credentials are not configured"
    if cap(ctx) is None:
        return "the DataForSEO cap is not a finite amount"
    if ctx.local:
        return "local fixture target (no public search)"
    return ""


def serp(ctx: Context, kind: str, keyword: str, deadline: float) -> dict[str, Any]:
    """One bounded live Google SERP call (organic or maps) booked against the shared cap.

    The estimated cost is checked against the REMAINING cap before the call, the query count
    is capped per run, and nothing is retried or paginated. Returns status ok, refused or
    error; refused calls make no request.
    """
    where = market(ctx)
    task = {"keyword": " ".join(keyword.split())[:700], **where, "depth": SERP_DEPTH}
    out: dict[str, Any] = {"kind": kind, "query": task, "estimated_usd": str(SERP_ESTIMATE_USD)}
    limit = cap(ctx)
    remaining = (limit or Decimal(0)) - ctx.dataforseo_spent_usd
    if reason := serp_refusal(ctx):
        return out | {"status": "refused", "reason": reason}
    if ctx.dataforseo_serp_calls >= MAX_SERP_CALLS:
        return out | {"status": "refused", "reason": f"query cap of {MAX_SERP_CALLS} reached"}
    if SERP_ESTIMATE_USD > remaining:
        return out | {
            "status": "refused",
            "reason": f"estimated ${SERP_ESTIMATE_USD} exceeds the remaining cap "
            f"${max(remaining, Decimal(0))}",
        }
    left = deadline - time.monotonic()
    if left <= 1:
        return out | {"status": "refused", "reason": "discovery deadline reached"}
    ctx.dataforseo_spent_usd += SERP_ESTIMATE_USD
    ctx.dataforseo_serp_calls += 1
    try:
        response = httpx.post(
            SERP_ENDPOINTS[kind],
            auth=(ctx.config["DATAFORSEO_LOGIN"], ctx.config["DATAFORSEO_PASSWORD"]),
            json=[task],
            timeout=min(30.0, left),
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Exception text can carry request details; only the type is kept.
        return out | {"status": "error", "reason": f"request failed ({type(exc).__name__})"}
    cost = Decimal(str(data.get("cost", SERP_ESTIMATE_USD)))
    ctx.dataforseo_spent_usd += cost - SERP_ESTIMATE_USD
    out["cost_usd"] = str(cost)
    if cost > SERP_ESTIMATE_USD:
        # Charged above the estimate: the estimate cannot be trusted, so stop paid calls.
        ctx.dataforseo_serp_calls = MAX_SERP_CALLS
    tasks = data.get("tasks") or []
    if data.get("status_code") != 20000 or not tasks or tasks[0].get("status_code") != 20000:
        return out | {"status": "error", "reason": "DataForSEO task did not complete"}
    first = (tasks[0].get("result") or [{}])[0] or {}
    items = [i for i in first.get("items") or [] if isinstance(i, dict)]
    return out | {"status": "ok", "items": items, "check_url": first.get("check_url", "")}
