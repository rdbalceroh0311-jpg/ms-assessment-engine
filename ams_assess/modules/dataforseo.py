import re
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
# Per-call SERP cost estimates, checked against the REMAINING cap before every call.
# Source: DataForSEO published pricing, checked 2026-10-23:
# - https://dataforseo.com/pricing/serp/google-organic-serp-api: Live mode $0.002 per SERP of
#   up to 10 organic results; "x5 base price per operator used" (site:, inurl:, intitle:, ...).
# - https://dataforseo.com/pricing/serp/google-maps-serp-api: Live mode $0.002 per SERP of up to
#   100 results; x5 when the keyword contains a search operator.
# depth=10 is one billed SERP on both endpoints. Each estimate is the published price x1.5 for
# headroom; a query with more than one operator is refused rather than estimated.
# (endpoint, operators): (published live price, estimate booked before the call)
SERP_PRICING: dict[tuple[str, int], tuple[Decimal, Decimal]] = {
    ("organic", 0): (Decimal("0.002"), Decimal("0.003")),  # name and keyword queries
    ("organic", 1): (Decimal("0.010"), Decimal("0.015")),  # site: profile queries
    ("maps", 0): (Decimal("0.002"), Decimal("0.003")),  # Google Maps business search
    ("maps", 1): (Decimal("0.010"), Decimal("0.015")),
}
SERP_DEPTH = 10
MAX_SERP_CALLS = 12
# Search operators as DataForSEO might bill them; quoted phrases count, to stay conservative.
OPERATOR = re.compile(
    r"\b(?i:site|inurl|allinurl|intitle|allintitle|intext|allintext|inanchor|filetype|ext"
    r"|related|cache|define|before|after|source|location):|\"[^\"]*\"|\bOR\b|\bAND\b|\|"
    r"|(?:^|\s)-\S|\bAROUND\("
)


def operators(keyword: str) -> int:
    return len(OPERATOR.findall(keyword))


def serp_estimate(kind: str, keyword: str) -> Decimal | None:
    """Conservative pre-call estimate, or None when the query cannot be estimated safely."""
    price = SERP_PRICING.get((kind, operators(keyword)))
    return price[1] if price else None


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
    estimate = serp_estimate(kind, task["keyword"])
    out: dict[str, Any] = {"kind": kind, "query": task, "estimated_usd": str(estimate or "")}
    limit = cap(ctx)
    remaining = (limit or Decimal(0)) - ctx.dataforseo_spent_usd
    if reason := serp_refusal(ctx):
        return out | {"status": "refused", "reason": reason}
    if estimate is None:
        return out | {
            "status": "refused",
            "reason": f"{operators(task['keyword'])} search operators; cost cannot be estimated "
            "safely",
        }
    if ctx.dataforseo_serp_calls >= MAX_SERP_CALLS:
        return out | {"status": "refused", "reason": f"query cap of {MAX_SERP_CALLS} reached"}
    if estimate > remaining:
        return out | {
            "status": "refused",
            "reason": f"estimated ${estimate} exceeds the remaining cap "
            f"${max(remaining, Decimal(0))}",
        }
    left = deadline - time.monotonic()
    if left <= 1:
        return out | {"status": "refused", "reason": "discovery deadline reached"}
    # The full estimate is booked before the call and stays booked unless the provider reports
    # the cost of a completed task: a timeout or unknown outcome may still have been charged.
    ctx.dataforseo_spent_usd += estimate
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
    try:
        cost: Decimal | None = Decimal(str(data["cost"])) if "cost" in data else None
    except (ArithmeticError, ValueError):
        cost = None
    tasks = data.get("tasks") or []
    done = data.get("status_code") == 20000 and tasks and tasks[0].get("status_code") == 20000
    if cost is not None and cost.is_finite() and cost >= 0:
        out["cost_usd"] = str(cost)
        # A reported cost replaces the reservation only for a completed task, and only upwards
        # otherwise.
        ctx.dataforseo_spent_usd += (cost if done else max(cost, estimate)) - estimate
        if cost > estimate:
            # Charged above the estimate: the estimates cannot be trusted, so stop paid calls.
            ctx.dataforseo_serp_calls = MAX_SERP_CALLS
    if not done:
        return out | {"status": "error", "reason": "DataForSEO task did not complete"}
    first = (tasks[0].get("result") or [{}])[0] or {}
    items = [i for i in first.get("items") or [] if isinstance(i, dict)]
    return out | {"status": "ok", "items": items, "check_url": first.get("check_url", "")}
