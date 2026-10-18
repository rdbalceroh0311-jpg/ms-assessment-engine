from decimal import Decimal

import httpx

from ams_assess.models import Context, ModuleResult

name = "dataforseo"
categories = {"seo", "local_seo", "entity"}
requires = ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "--dataforseo-max-usd"]
# One task and one result: $0.01 task + $0.0001 result, without paid modifiers.
ESTIMATE_USD = Decimal("0.0101")


def run(ctx: Context) -> ModuleResult:
    if not all(ctx.config.get(k) for k in ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"]):
        return ModuleResult(
            name=name, status="skipped", reason="Missing DataForSEO credentials; disabled"
        )
    if ctx.dataforseo_max_usd is None:
        return ModuleResult(
            name=name, status="skipped", reason="Disabled: explicit USD cap is required"
        )
    cap = Decimal(str(ctx.dataforseo_max_usd))
    if not cap.is_finite() or cap < ESTIMATE_USD:
        return ModuleResult(
            name=name,
            status="skipped",
            reason=f"Refused: estimated ${ESTIMATE_USD} exceeds the explicit cap",
        )
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="DataForSEO requires a public domain"
        )
    market = {
        "location_code": int(ctx.config.get("DATAFORSEO_LOCATION_CODE", "2036")),
        "language_code": "en",
    }
    budget = {"estimated_usd": str(ESTIMATE_USD), "cap_usd": str(cap), "calls": 1, **market}
    ctx.raw("dataforseo-budget", budget)
    # Exactly one bounded request; no retries or pagination that could incur extra charges.
    response = httpx.post(
        "https://api.dataforseo.com/v3/dataforseo_labs/google/domain_rank_overview/live",
        auth=(ctx.config["DATAFORSEO_LOGIN"], ctx.config["DATAFORSEO_PASSWORD"]),
        json=[{"target": ctx.host, **market, "limit": 1}],
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    result = ModuleResult(name=name, raw_paths=[ctx.raw(name, data), "raw/dataforseo-budget.json"])
    tasks = data.get("tasks", [])
    if data.get("status_code") != 20000 or not tasks or tasks[0].get("status_code") != 20000:
        result.status, result.reason = "error", "DataForSEO task did not complete successfully"
        return result
    if Decimal(str(data.get("cost", 0))) > cap:
        result.status, result.reason = (
            "error",
            "Provider charged above estimate/cap; no further calls made",
        )
        return result
    coverage = f"location_code={market['location_code']}, language_code={market['language_code']}"
    result.add_evidence(
        ctx.target,
        f"DataForSEO domain rank overview ({coverage})",
        {**market, "result": tasks[0].get("result")},
    )
    result.reason = (
        f"Database: {coverage}. Estimates depend on provider pricing; "
        "no traffic or revenue guarantee."
    )
    return result
