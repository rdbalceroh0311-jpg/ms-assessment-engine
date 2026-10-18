import httpx

from ams_assess.models import Context, ModuleResult

name = "performance"
categories = {"performance"}
requires = ["public target", "optional PAGESPEED_API_KEY, CRUX_API_KEY/GOOGLE_API_KEY"]


def run(ctx: Context) -> ModuleResult:
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="PageSpeed/CrUX cannot access localhost"
        )
    result, raw, limitations = ModuleResult(name=name), {}, []
    for strategy in ["mobile", "desktop"]:
        params = {"url": ctx.target, "strategy": strategy, "category": "performance"}
        if ctx.config.get("PAGESPEED_API_KEY"):
            params["key"] = ctx.config["PAGESPEED_API_KEY"]
        try:
            response = httpx.get(
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                params=params,
                timeout=90,
            )
            if response.status_code != 200:
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
                ctx.target, f"PageSpeed {strategy}: lab and available field data", summary
            )
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
                json={"url": ctx.target},
                timeout=30,
            )
            response.raise_for_status()
            raw["crux"] = response.json()
            result.add_evidence(ctx.target, "CrUX field data (separate API)", raw["crux"])
        except (httpx.HTTPError, ValueError) as exc:
            limitations.append(f"Separate CrUX API unavailable ({type(exc).__name__})")
    else:
        limitations.append("Separate CrUX API skipped: missing CRUX_API_KEY/GOOGLE_API_KEY")
    result.raw_paths = [ctx.raw(name, raw)]
    result.reason = "; ".join(limitations)
    if not result.evidence:
        result.status = "skipped"
    return result
