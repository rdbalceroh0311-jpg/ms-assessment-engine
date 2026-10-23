import httpx

from ams_assess.models import Connection, Context, ModuleResult

name = "performance"
categories = {"performance"}
requires = ["public target", "optional PAGESPEED_API_KEY, CRUX_API_KEY/GOOGLE_API_KEY"]


def run(ctx: Context) -> ModuleResult:
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="PageSpeed/CrUX cannot access localhost"
        )
    result, raw, limitations = ModuleResult(name=name), {}, []
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
    for strategy in ["mobile", "desktop"]:
        params = {"url": ctx.site_url, "strategy": strategy, "category": "performance"}
        if ctx.config.get("PAGESPEED_API_KEY"):
            params["key"] = ctx.config["PAGESPEED_API_KEY"]
        try:
            response = httpx.get(
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                params=params,
                timeout=90,
            )
            if response.status_code != 200:
                if psi.status != "Connected successfully":
                    psi.status = (
                        "Permission required"
                        if response.status_code in {401, 403}
                        else "Not assessed"
                    )
                psi.detail = f"PageSpeed {strategy}: HTTP {response.status_code}"
                raw[strategy] = {"http_status": response.status_code}
                limitations.append(
                    f"PageSpeed {strategy}: HTTP {response.status_code} (quota/access)"
                )
                continue
            data = response.json()
            raw[strategy] = data
            lighthouse = data.get("lighthouseResult", {})
            if lighthouse.get("runtimeError") or not lighthouse.get("categories"):
                limitations.append(f"PageSpeed {strategy}: no usable Lighthouse measurement")
                continue
            score = lighthouse["categories"]["performance"].get("score")
            metrics = {
                k: lighthouse.get("audits", {}).get(k, {})
                for k in [
                    "largest-contentful-paint",
                    "cumulative-layout-shift",
                    "total-blocking-time",
                    "first-contentful-paint",
                    "speed-index",
                ]
            }
            summary = {
                "strategy": strategy,
                "score": score,
                "metrics": metrics,
                "field_url": data.get("loadingExperience", {}),
                "field_origin": data.get("originLoadingExperience", {}),
                "fetch_time": lighthouse.get("fetchTime"),
            }
            eid = result.add_evidence(
                ctx.site_url, f"PageSpeed {strategy}: lab and available field data", summary
            )
            psi.status = "Connected successfully"
            psi.evidence_ids.append(eid)
            if score is not None and score < 0.9:
                result.finding(
                    f"lighthouse-{strategy}",
                    f"Improve {strategy} lab performance",
                    "performance",
                    [eid],
                    f"Lighthouse lab performance score: {score * 100:.0f}/100.",
                    "Prioritise the measured rendering bottlenecks and retest; compare field data.",
                )
            if not data.get("loadingExperience", {}).get("metrics"):
                limitations.append(f"No URL-level CrUX field metrics for {strategy}")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            limitations.append(f"PageSpeed {strategy} unavailable ({type(exc).__name__})")
    crux_key = ctx.config.get("CRUX_API_KEY") or ctx.config.get("GOOGLE_API_KEY")
    if crux_key:
        try:
            response = httpx.post(
                "https://chromeuxreport.googleapis.com/v1/records:queryRecord",
                params={"key": crux_key},
                json={"url": ctx.site_url},
                timeout=30,
            )
            if response.status_code == 404:
                try:
                    error = response.json().get("error", {})
                except (ValueError, AttributeError):
                    error = {}
            else:
                error = {}
            # Documented no-data envelope; an arbitrary HTTP 404 is still an API error.
            # https://developer.chrome.com/docs/crux/guides/crux-api#errors
            no_data = (
                isinstance(error, dict)
                and error.get("code") == 404
                and error.get("status") == "NOT_FOUND"
                and error.get("message") == "chrome ux report data not found"
            )
            if no_data:
                raw["crux"] = {"http_status": 404, "status": "NOT_FOUND"}
                crux.status = "Not assessed"
                crux.detail = "No CrUX field data for this origin/URL (insufficient traffic)"
                limitations.append(crux.detail)
            else:
                response.raise_for_status()
                raw["crux"] = response.json()
                if not raw["crux"].get("record", {}).get("metrics"):
                    crux.status, crux.detail = (
                        "Not assessed",
                        "CrUX returned no usable field metrics",
                    )
                    limitations.append(crux.detail)
                else:
                    eid = result.add_evidence(
                        ctx.site_url, "CrUX field data (separate API)", raw["crux"]
                    )
                    crux.status, crux.detail = "Connected successfully", "Field metrics returned"
                    crux.evidence_ids = [eid]
        except (httpx.HTTPError, ValueError) as exc:
            crux.status = (
                "Permission required"
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in {401, 403}
                )
                else "Not assessed"
            )
            detail = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            crux.detail = f"Separate CrUX API unavailable ({detail})"
            raw["crux"] = {"error": detail}
            limitations.append(crux.detail)
    else:
        limitations.append("Separate CrUX API skipped: missing CRUX_API_KEY/GOOGLE_API_KEY")
    result.raw_paths = [ctx.raw(name, raw)]
    result.reason = "; ".join(limitations)
    psi.detail = result.reason or "Mobile and desktop lab measurements returned"
    if not result.evidence:
        result.status = "skipped"
    return result
