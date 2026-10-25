import httpx

from ams_assess.models import Context, ModuleResult
from ams_assess.network import public_post, validate_public_url

name = "observatory"
categories = {"security"}
requires = ["public target", "MDN HTTP Observatory v2"]


def run(ctx: Context) -> ModuleResult:
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="Observatory cannot scan local targets"
        )
    result = ModuleResult(name=name)
    try:
        validate_public_url(ctx.site_url)
        response = public_post(
            "https://observatory-api.mdn.mozilla.net/api/v2/scan",
            params={"host": ctx.site_host},
            timeout=45,
        )
        data = response.json()
        result.raw_paths = [ctx.raw(name, data)]
        response.raise_for_status()
        if data.get("error") or "grade" not in data:
            raise ValueError("No completed grade")
        eid = result.add_evidence(ctx.site_url, "MDN Observatory public security grade", data)
        if data.get("tests_failed", 0):
            result.finding(
                "observatory-review",
                "Review the Observatory security assessment",
                "security",
                [eid],
                f"Grade {data['grade']}; {data['tests_failed']} tests failed.",
                "Review the detailed Observatory scan with the hosting team.",
                confidence="needs_verification",
            )
        result.reason = (
            "Public API v2 supplies an aggregate grade, not individual header test details"
        )
    except (httpx.HTTPError, ValueError) as exc:
        result.status, result.reason = "skipped", f"Observatory unavailable ({type(exc).__name__})"
    return result
