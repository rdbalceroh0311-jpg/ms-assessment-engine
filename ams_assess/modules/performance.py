"""Bounded public field/lab measurements; history is read through the customer store."""

import json
import math
from datetime import UTC, datetime
from typing import Any, TypeGuard
from urllib.parse import urlsplit

import httpx

from ams_assess import store
from ams_assess.egress import api_request
from ams_assess.models import Connection, Context, ModuleResult, normalize_target
from ams_assess.network import validate_public_url

name = "performance"
categories = {"performance"}
requires = ["public target", "optional PAGESPEED_API_KEY, CRUX_API_KEY/GOOGLE_API_KEY"]
MAX_KEY_PAGES = 10
LAB_METRICS = {
    "largest-contentful-paint": "ms",
    "cumulative-layout-shift": "unitless",
    "total-blocking-time": "ms",
    "first-contentful-paint": "ms",
    "speed-index": "ms",
}
NO_FIELD_NOTE = (
    "Possible reasons include URL/origin eligibility, sample coverage and the collection window; "
    "the API does not establish the cause."
)
LAB_NOTE = (
    "Lighthouse/PageSpeed LAB fallback: a synthetic snapshot, not real-user field data. "
    "Total Blocking Time (TBT) is a lab metric, not field Interaction to Next Paint (INP)."
)


def origin(url: str) -> str:
    parts = urlsplit(normalize_target(url))
    port = parts.port
    host = (parts.hostname or "").removesuffix(".")
    if ":" in host:
        host = f"[{host}]"
    if port and (parts.scheme, port) not in {("https", 443), ("http", 80)}:
        host += f":{port}"
    return f"{parts.scheme}://{host}"


def representative_pages(ctx: Context, dropped_explicit: list[str] | None = None) -> list[str]:
    """Homepage plus a stable, bounded sample; explicit key pages take precedence."""
    limit = int(ctx.config.get("AMS_PERFORMANCE_KEY_PAGES", "3"))
    if not 0 <= limit <= MAX_KEY_PAGES:
        raise ValueError("Performance key-page limit must be between 0 and 10")
    base = origin(ctx.site_url)
    explicit = json.loads(ctx.config.get("AMS_PERFORMANCE_PAGES", "[]"))
    if not isinstance(explicit, list) or not all(isinstance(p, str) for p in explicit):
        raise ValueError("Performance pages must be a list of URLs")
    pages = [base + "/"]

    def page_rank(url: str) -> tuple[bool, str]:
        # Prefer common customer journeys; explicit URLs always remain first.
        segments = set(urlsplit(url).path.lower().strip("/").split("/"))
        key_page = bool(
            segments & {"services", "products", "pricing", "contact", "about", "booking"}
        )
        return not key_page, url

    candidates = [*explicit, ctx.site_url, *sorted(set(ctx.pages), key=page_rank)]
    for value in candidates:
        try:
            url = normalize_target(value)
            if origin(url) not in {base, origin(ctx.target)}:
                raise ValueError("Performance pages must use the website origin")
            url = base + urlsplit(url).path
        except ValueError:
            if value in explicit:
                raise
            continue
        if url not in pages and len(pages) <= limit:
            pages.append(url)
        elif (
            url not in pages
            and value in explicit
            and dropped_explicit is not None
            and value not in dropped_explicit
        ):
            dropped_explicit.append(value)
    return pages


def number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def query_crux(scope: str, url: str, key: str) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "scope": scope,
        "url": url,
        "form_factor": "ALL",
        "outcome": "not_configured",
        "queried_at_utc": datetime.now(UTC).isoformat(),
        "detail": "CrUX field data status unknown: separate API not configured "
        "(missing CRUX_API_KEY/GOOGLE_API_KEY)",
    }
    if not key:
        return outcome
    try:
        validate_public_url(url)
        response = api_request(
            "crux",
            "POST",
            "https://chromeuxreport.googleapis.com/v1/records:queryRecord",
            params={"key": key},
            json={scope: url},
            timeout=30,
        )
        status = outcome["http_status"] = response.status_code
        # Never retain remote error messages or request URLs: either may echo an API key.
        if status in {401, 403}:
            state, detail = "permission", f"CrUX permission required (HTTP {status})"
        elif status == 429:
            state, detail = "quota", "CrUX quota exceeded (HTTP 429)"
        else:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            if (
                status == 404
                and isinstance(error, dict)
                and all(
                    error.get(k) == v
                    for k, v in {
                        "code": 404,
                        "status": "NOT_FOUND",
                        "message": "chrome ux report data not found",
                    }.items()
                )
            ):
                state = "no_eligible_data"
                detail = f"No eligible CrUX field data for this {scope}. {NO_FIELD_NOTE}"
            elif status == 200 and isinstance(body, dict):
                record = body.get("record", {})
                if (
                    isinstance(record, dict)
                    and isinstance(record.get("metrics"), dict)
                    and record["metrics"]
                    and all(isinstance(v, dict) and v for v in record["metrics"].values())
                ):
                    outcome["record"] = record
                    state, detail = "data", "CrUX field data present (all form factors)"
                else:
                    state, detail = "error", "CrUX returned no usable field metrics"
            else:
                state, detail = "error", f"CrUX API error (HTTP {status})"
        outcome.update(outcome=state, detail=detail)
    except (httpx.HTTPError, ValueError) as exc:
        outcome.update(outcome="error", detail=f"CrUX API error ({type(exc).__name__})")
    if outcome["outcome"] in {"permission", "quota", "error"}:
        outcome["detail"] = "CrUX field data status unknown: " + outcome["detail"]
    return outcome


def has_psi_field(data: dict[str, Any]) -> bool:
    return any(data.get(key, {}).get("metrics") for key in ("field_url", "field_origin"))


def measure_lab(url: str, strategy: str, key: str) -> dict[str, Any]:
    measured: dict[str, Any] = {
        "url": url,
        "strategy": strategy,
        "outcome": "error",
        "kind": "LAB",
        "conditions": {
            "strategy": strategy,
            "requested_at_utc": datetime.now(UTC).isoformat(),
            "url_tested": url,
            "final_url": None,
            "fetch_time": None,
            "lighthouse_version": None,
            "form_factor": None,
            "throttling_method": None,
            "throttling": None,
        },
    }
    params = {"url": url, "strategy": strategy, "category": "performance"}
    if key:
        params["key"] = key
    try:
        validate_public_url(url)
        response = api_request(
            "pagespeed",
            "GET",
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params=params,
            timeout=90,
        )
        status = measured["http_status"] = response.status_code
        if status != 200:
            measured["outcome"] = (
                "permission" if status in {401, 403} else "quota" if status == 429 else "error"
            )
            measured["detail"] = f"PageSpeed {strategy}: HTTP {status}"
            return measured
        body = response.json()
        # Keep PSI's free field data in the existing shape consumed by site_rules.field_vitals,
        # even when Lighthouse itself fails. It is separate from the lab measurement.
        for field_key, source in [
            ("field_url", "loadingExperience"),
            ("field_origin", "originLoadingExperience"),
        ]:
            field = body.get(source, {})
            measured[field_key] = field if isinstance(field, dict) else {}
        measured["field_data_label"] = "PSI-reported field data"
        lighthouse = body.get("lighthouseResult", {})
        config = lighthouse.get("configSettings", {})
        measured["conditions"].update(
            {
                "final_url": lighthouse.get("finalUrl"),
                "fetch_time": lighthouse.get("fetchTime"),
                "lighthouse_version": lighthouse.get("lighthouseVersion"),
                "form_factor": config.get("formFactor", config.get("emulatedFormFactor")),
                "throttling_method": config.get("throttlingMethod"),
                "throttling": config.get("throttling"),
                "screen_emulation": config.get("screenEmulation"),
                "environment": lighthouse.get("environment"),
            }
        )
        score = lighthouse.get("categories", {}).get("performance", {}).get("score")
        if lighthouse.get("runtimeError") or not number(score) or not 0 <= score <= 1:
            measured["detail"] = f"PageSpeed {strategy}: no usable Lighthouse measurement"
            return measured
        metrics = {
            k: {"numericValue": a["numericValue"], "unit": unit}
            for k, unit in LAB_METRICS.items()
            if isinstance(a := lighthouse.get("audits", {}).get(k), dict)
            and number(a.get("numericValue"))
        }
        measured.update(outcome="data", score=score, metrics=metrics, detail=LAB_NOTE)
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        measured["detail"] = f"PageSpeed {strategy} unavailable ({type(exc).__name__})"
    return measured


def prior_measurements(ctx: Context) -> tuple[str, list[dict[str, Any]], str]:
    """Read only the latest matching assessment with lab data, including legacy evidence."""
    if ctx.store_root is None:
        return "", [], "Prior-run comparison unavailable: no customer store configured."
    try:
        saved = store.load(ctx.store_root, ctx.customer_id)
        if saved:
            for run in reversed(saved["runs"]):
                if (
                    run["kind"] != "assessment"
                    or run["status"] == "incomplete"
                    or run.get("run_dir") == ctx.run_dir.name
                    or origin(run["target"]) != origin(ctx.target)
                ):
                    continue
                rows = []
                prefix = f"ev-performance-{run['run_id']}-"
                for evidence in saved["evidence"]:
                    if evidence["source"] != name or not evidence["id"].startswith(prefix):
                        continue
                    data = evidence["data"]
                    if not isinstance(data, dict) or "strategy" not in data:
                        continue
                    if data.get("kind") not in {None, "LAB"} or not number(data.get("score")):
                        continue
                    rows.append(
                        {
                            **data,
                            "url": data.get("url", evidence["url"]),
                            "evidence_id": evidence["id"],
                        }
                    )
                if rows:
                    return run["run_id"], rows, ""
    except (OSError, ValueError, KeyError, TypeError):
        return "", [], "Prior-run comparison unavailable: customer store could not be read."
    return "", [], "No prior lab measurements for this customer and site."


def compare_labs(ctx: Context, labs: list[dict[str, Any]]) -> dict[str, Any]:
    run_id, previous, note = prior_measurements(ctx)
    indexed = {(r["url"], r["strategy"]): r for r in previous}
    comparisons = []
    for after in labs:
        before = indexed.get((after["url"], after["strategy"]))
        if before is None or after["outcome"] != "data":
            continue
        metrics = {}
        for key in ["score", *LAB_METRICS]:
            a = (
                before.get("score")
                if key == "score"
                else before.get("metrics", {}).get(key, {}).get("numericValue")
            )
            b = (
                after.get("score")
                if key == "score"
                else after["metrics"].get(key, {}).get("numericValue")
            )
            if number(a) and number(b):
                metrics[key] = {
                    "before": a,
                    "after": b,
                    "delta": round(b - a, 6),
                    "unit": "0-1" if key == "score" else LAB_METRICS[key],
                }
        conditions = [
            "final_url",
            "lighthouse_version",
            "form_factor",
            "throttling_method",
            "throttling",
            "screen_emulation",
            "environment",
        ]
        old, new = before.get("conditions", {}), after["conditions"]
        differences = [k for k in conditions if old.get(k) != new.get(k)]
        missing = [k for k in conditions if old.get(k) is None or new.get(k) is None]
        comparisons.append(
            {
                "url": after["url"],
                "strategy": after["strategy"],
                "before_run_id": run_id,
                "before_evidence_id": before["evidence_id"],
                "after_evidence_id": after["evidence_id"],
                "metrics": metrics,
                "conditions_changed": differences,
                "conditions_unrecorded": missing,
                "before_conditions": old,
                "after_conditions": new,
            }
        )
    return {
        "prior_run_id": run_id,
        "rows": comparisons,
        "detail": note
        or (
            "Matched page and strategy; deltas are after minus before. "
            "Lab variation and changed or unrecorded conditions limit conclusions."
            if comparisons
            else "No matching page + strategy in the prior run."
        ),
    }


def run(ctx: Context) -> ModuleResult:
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="PageSpeed/CrUX cannot access localhost"
        )
    dropped_explicit: list[str] = []
    pages = representative_pages(ctx, dropped_explicit)
    result = ModuleResult(name=name)
    labs, fields, limitations = [], [], []
    psi = Connection(
        key="pagespeed",
        service="PageSpeed Insights",
        status="Not assessed",
        detail="No usable lab measurement",
    )
    crux = Connection(
        key="crux",
        service="CrUX (separate API)",
        status="Not connected",
        detail="Separate API requires CRUX_API_KEY/GOOGLE_API_KEY",
    )
    result.connections = [psi, crux]
    key = ctx.config.get("CRUX_API_KEY") or ctx.config.get("GOOGLE_API_KEY", "")
    for scope, url in [("url", p) for p in pages] + [("origin", origin(ctx.site_url))]:
        field = query_crux(scope, url, key)
        eid = result.add_evidence(url, f"CrUX {scope}: {field['outcome']}", field.copy())
        field["evidence_id"] = eid
        fields.append(field)
        crux.evidence_ids.append(eid)
        if field["outcome"] != "data":
            limitations.append(f"{scope} {url}: {field['detail']}")
    for page in pages:
        for strategy in ["mobile", "desktop"]:
            lab = measure_lab(page, strategy, ctx.config.get("PAGESPEED_API_KEY", ""))
            eid = result.add_evidence(
                page,
                f"PageSpeed {strategy} LAB: {lab['outcome']}"
                + ("; PSI-reported field data" if has_psi_field(lab) else ""),
                lab.copy(),
            )
            lab["evidence_id"] = eid
            labs.append(lab)
            psi.evidence_ids.append(eid)
            if lab["outcome"] != "data":
                limitations.append(f"{page}: {lab['detail']}")
            elif lab["score"] < 0.9:
                result.finding(
                    f"lighthouse-{strategy}:{page}",
                    f"Improve {strategy} lab performance",
                    "performance",
                    [eid],
                    f"{page}: Lighthouse {strategy} LAB performance score: "
                    f"{lab['score'] * 100:.0f}/100.",
                    "Prioritise measured rendering bottlenecks and retest; compare field data.",
                    urls=[page],
                )
    for connection, rows in [(psi, labs), (crux, fields)]:
        states = {r["outcome"] for r in rows}
        if "data" in states:
            connection.status = "Connected successfully"
        elif "permission" in states:
            connection.status = "Permission required"
        elif states != {"not_configured"}:
            connection.status = "Not assessed"
        connection.detail = "; ".join(dict.fromkeys(r["detail"] for r in rows))
    psi_field_available = any(has_psi_field(lab) for lab in labs)
    if psi_field_available:
        psi.status = "Connected successfully"
        psi.detail += "; PSI-reported field data available"
    absent = [f for f in fields if f["outcome"] == "no_eligible_data"]
    recommendation = (
        "If these pages support important customer journeys and field coverage is worth the "
        "ongoing effort, seek customer approval for privacy-appropriate real-user monitoring "
        "(RUM), including consent, access and retention. Recommendation only; nothing installed."
        if absent
        else ""
    )
    summary = {
        "schema_version": 1,
        "pages": pages,
        "dropped_explicit_pages": dropped_explicit,
        "strategies": ["mobile", "desktop"],
        "selection": (
            "Homepage, explicit key pages, target page, then stable customer-journey page sample"
        ),
        "max_key_pages": int(ctx.config.get("AMS_PERFORMANCE_KEY_PAGES", "3")),
        "tests_per_page_strategy": 1,
        "psi_request_cap": len(pages) * 2,
        "crux_request_cap": len(pages) + 1 if key else 0,
        "lab_note": LAB_NOTE,
        "lab": labs,
        "field": fields,
        "comparison": compare_labs(ctx, labs),
        "rum_recommendation": recommendation,
    }
    result.add_evidence(
        ctx.target, "Performance interpretation and prior-run comparison", {"performance": summary}
    )
    result.reason = "; ".join(dict.fromkeys(limitations))
    # Only selected response fields are retained, and configured keys are removed even if echoed.
    secrets = [
        ctx.config[k]
        for k in ("PAGESPEED_API_KEY", "CRUX_API_KEY", "GOOGLE_API_KEY")
        if ctx.config.get(k)
    ]
    encoded = result.model_dump_json()
    for secret in secrets:
        encoded = encoded.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    result = ModuleResult.model_validate_json(encoded)
    summary = result.evidence[-1].data["performance"]
    result.raw_paths = [ctx.raw(name, {"performance": summary})]
    if not psi_field_available and not any(r["outcome"] == "data" for r in [*labs, *fields]):
        result.status = "skipped"
    return result
