import base64
import re
import time
from decimal import Decimal
from typing import Any
from urllib.parse import quote, quote_plus, unquote

import httpx

from ams_assess.models import Connection, ConnectionStatus, Context, ModuleResult

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
# Operators are counted on the URL-decoded query, conservatively: EVERY colon counts as one
# (site:, link:, info:, id:, allinanchor:, definition:, ... the documented list keeps growing,
# so no list is trusted), plus quoted phrases, OR/AND/|, a leading minus and AROUND(.
OTHER_OPERATOR = re.compile(r"[\"“”][^\"“”]*[\"“”]?|\bOR\b|\bAND\b|\||(?:^|\s)-\S|\bAROUND\(")


def decoded(keyword: str) -> str:
    """The query as the provider may read it: percent-decoded until stable (3 rounds at most)."""
    text = keyword
    for _ in range(3):
        decoded_text = unquote(text)
        if decoded_text == text:
            break
        text = decoded_text
    return text


def operators(keyword: str) -> int | None:
    """Billed-operator count, or None when the query cannot be read safely (still encoded)."""
    text = decoded(keyword)
    if "%" in text:
        return None
    return text.count(":") + len(OTHER_OPERATOR.findall(text))


def serp_estimate(kind: str, keyword: str) -> Decimal | None:
    """Conservative pre-call estimate, or None when the query cannot be estimated safely."""
    count = operators(keyword)
    price = SERP_PRICING.get((kind, count)) if count is not None else None
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


def safe_message(value: Any, ctx: Context) -> str:
    """Keep only provider status prose, never an echoed request or credentials."""
    if not isinstance(value, str):
        return ""
    login, password = (ctx.config.get(k, "") for k in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"))
    secrets = {login, password, f"{login}:{password}"}
    if login or password:
        secrets.add(base64.b64encode(f"{login}:{password}".encode()).decode())
    text = value
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = re.sub(re.escape(variant), "[redacted]", text, flags=re.I)
    text = re.sub(r"https?://\S+", "[link removed]", text)
    text = re.sub(
        r"(?i)\b(authorization|password|token|api[_-]?key|secret)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    return " ".join(text.split())[:500]


def paid_request(
    ctx: Context, endpoint: str, task: dict[str, Any], estimate: Decimal, timeout: float
) -> dict[str, Any]:
    """Book once; stop on access rejection; retain uncertain charges on timeout/5xx."""
    ctx.dataforseo_spent_usd += estimate
    try:
        response = httpx.post(
            endpoint,
            auth=(ctx.config["DATAFORSEO_LOGIN"], ctx.config["DATAFORSEO_PASSWORD"]),
            json=[task],
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return {"status": "error", "reason": f"DataForSEO request failed ({type(exc).__name__})"}
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict)]

    def status(entry: dict[str, Any]) -> dict[str, Any]:
        code = entry.get("status_code")
        return {
            "status_code": code if isinstance(code, int) else None,
            "status_message": safe_message(entry.get("status_message"), ctx),
        }

    provider = {
        "http_status": response.status_code,
        **status(data),
        "tasks": [status(t) for t in tasks],
    }
    codes = [s["status_code"] for s in [provider, *provider["tasks"]]]
    denied = response.status_code in {401, 402, 403} or any(
        isinstance(c, int) and c // 100 in {401, 402, 403} for c in codes
    )
    done = (
        response.is_success
        and data.get("status_code") == 20000
        and bool(tasks)
        and all(t.get("status_code") == 20000 for t in tasks)
    )
    reason = f"DataForSEO HTTP {response.status_code}"
    details = [
        f"provider {s['status_code']}: {s['status_message'] or 'No status message'}"
        for s in [provider, *provider["tasks"]]
        if s["status_code"] != 20000 and (s["status_code"] is not None or s["status_message"])
    ]
    reason += "; " + "; ".join(
        details or [safe_message(response.reason_phrase, ctx), "task did not complete"]
    )
    if denied:
        ctx.dataforseo_stopped = reason
        ctx.dataforseo_failure = provider
    try:
        cost = Decimal(str(data["cost"])) if "cost" in data else None
    except (ArithmeticError, ValueError):
        cost = None
    if cost is not None and (not cost.is_finite() or cost < 0):
        cost = None
    # A definitive rejection did not run the task. A timeout (including HTTP 408), 5xx
    # or unrecognised task failure can still have been charged: retain its reservation.
    rejected = (400 <= response.status_code < 500 and response.status_code != 408) or (
        response.is_success and denied
    )
    reserved = (
        (cost or Decimal(0))
        if rejected
        else cost
        if done and cost is not None
        else max(cost or Decimal(0), estimate)
    )
    ctx.dataforseo_spent_usd += reserved - estimate
    if cost is not None and cost > estimate and not denied:
        ctx.dataforseo_stopped = f"a call was charged ${cost}, above its ${estimate} estimate"
    out = {
        "status": "ok" if done else "error",
        "reason": "" if done else reason,
        "provider_status": provider,
        "reserved_usd": str(reserved),
    }
    if cost is not None:
        out["cost_usd"] = str(cost)
    if done:
        out["data"] = data
    return out


def failure_evidence(ctx: Context, result: ModuleResult, outcome: dict[str, Any]) -> str:
    eid = result.add_evidence(
        ctx.target,
        "DataForSEO request unavailable",
        {k: v for k, v in outcome.items() if k != "data"},
    )
    if ctx.dataforseo_failure:
        # Presence may encounter a denial after a successful domain overview. Update its
        # shared readiness row, or create one when only presence was selected.
        rows = [c for r in ctx.results for c in r.connections if c.key == name]
        if not rows:
            result.connections.append(
                Connection(
                    key=name,
                    service="DataForSEO",
                    status="Permission required",
                    detail=ctx.dataforseo_stopped,
                    evidence_ids=[eid],
                )
            )
        for row in rows:
            row.status, row.detail = "Permission required", ctx.dataforseo_stopped
            row.evidence_ids.append(eid)
    return eid


def run(ctx: Context) -> ModuleResult:
    state: ConnectionStatus = "Not assessed"
    try:
        result = collect(ctx)
        if ctx.dataforseo_failure:
            state = "Permission required"
        elif result.evidence and result.status == "ok":
            state = "Connected successfully"
        elif "credentials" in result.reason:
            state = "Not connected"
    except httpx.HTTPStatusError as exc:
        state = "Permission required" if exc.response.status_code in {401, 403} else "Not assessed"
        result = ModuleResult(
            name=name, status="error", reason=f"DataForSEO HTTP {exc.response.status_code}"
        )
    except Exception as exc:
        result = ModuleResult(
            name=name, status="error", reason=f"DataForSEO unavailable ({type(exc).__name__})"
        )
    result.connections = [
        Connection(
            key=name,
            service="DataForSEO",
            status=state,
            detail=result.reason,
            evidence_ids=[e.id for e in result.evidence],
        )
    ]
    # Keep any paid response/budget already written when response parsing fails.
    for filename in ("dataforseo.json", "dataforseo-budget.json"):
        path = "raw/" + filename
        if (ctx.run_dir / path).is_file() and path not in result.raw_paths:
            result.raw_paths.append(path)
    return result


def collect(ctx: Context) -> ModuleResult:
    if ctx.dataforseo_stopped:
        return ModuleResult(name=name, status="skipped", reason=ctx.dataforseo_stopped)
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
    # Exactly one bounded request; no retries or pagination that could incur extra charges.
    outcome = paid_request(
        ctx,
        f"{API}/dataforseo_labs/google/domain_rank_overview/live",
        {"target": ctx.host, **where, "limit": 1},
        ESTIMATE_USD,
        30,
    )
    ctx.raw("dataforseo-budget", {**budget, "spent_usd_run_total": str(ctx.dataforseo_spent_usd)})
    data = outcome.get("data", outcome)
    result = ModuleResult(name=name, raw_paths=[ctx.raw(name, data), "raw/dataforseo-budget.json"])
    if outcome["status"] != "ok":
        result.status, result.reason = "error", outcome["reason"]
        failure_evidence(ctx, result, outcome)
        return result
    tasks = data["tasks"]
    cost = Decimal(outcome.get("cost_usd", str(ESTIMATE_USD)))
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
    if ctx.dataforseo_stopped:
        return out | {
            "status": "refused",
            "reason": f"paid calls stopped: {ctx.dataforseo_stopped}",
        }
    if estimate is None:
        return out | {
            "status": "refused",
            "reason": "more than one search operator or unreadable encoding; cost cannot be "
            "estimated safely",
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
    ctx.dataforseo_serp_calls += 1
    outcome = paid_request(ctx, SERP_ENDPOINTS[kind], task, estimate, min(30.0, left))
    out.update({k: v for k, v in outcome.items() if k != "data"})
    if outcome["status"] != "ok":
        return out
    tasks = outcome["data"]["tasks"]
    first = (tasks[0].get("result") or [{}])[0] or {}
    items = [i for i in first.get("items") or [] if isinstance(i, dict)]
    return out | {"status": "ok", "items": items, "check_url": first.get("check_url", "")}
