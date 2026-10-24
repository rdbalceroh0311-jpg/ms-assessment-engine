"""Translate collector observations into explicit outcomes, never infer a pass from silence."""

import math

from ams_assess.lifecycle import identity_component, observe, scoped, site_key
from ams_assess.models import Context, ModuleResult

# Fixed checks remain explicit even when their collector cannot start. Axe rule IDs and
# domain-specific checks are dynamic; earlier instances are also accounted for by the store.
CHECKS = {
    "siteone": {
        **dict.fromkeys(("unavailable-urls", "broken-links", "redirects"), "technical"),
        **dict.fromkeys(
            (
                "missing-title",
                "missing-h1",
                "missing-description",
                "indexing-restricted",
                "robots-txt-missing",
                "canonical-missing",
            ),
            "seo",
        ),
    },
    "browser": {"canonical-mismatch": "seo", "console-errors": "technical", "axe": "accessibility"},
    "security": {
        **dict.fromkeys(
            (
                "security-headers",
                "header-review:server",
                "header-review:set-cookie",
                "tls-expiry",
                "tls-unverified",
            ),
            "security",
        )
    },
    "performance": {"lighthouse-mobile": "performance", "lighthouse-desktop": "performance"},
    "observatory": {"observatory-review": "security"},
}


def record(ctx: Context, result: ModuleResult) -> None:
    # The collectors which already expose status rows need no changes to their provider code.
    if result.name == "domain_email":
        for area in result.checks:
            for row in area.get("checks", []):
                check = f"health:{area['key']}:{row['name'].lower()}"
                status = {"PASS": "pass", "WARNING": "fail", "ERROR": "fail"}.get(
                    row.get("status", ""), "unavailable"
                )
                result.outcomes.append(
                    observe(
                        result.name,
                        check,
                        "domain_email",
                        ctx.target,
                        status,
                        row.get("evidence_ids", []),
                        reason=row.get("detail", ""),
                    )
                )
        # DNS/mail findings refer to the requested domain, even when the website redirects.
        for finding in result.findings:
            finding.component = ctx.target
    if result.name == "performance":
        for e in result.evidence:
            if not isinstance(e.data, dict):
                continue
            if e.description.startswith("CrUX") and e.data.get("scope") in {"url", "origin"}:
                state = e.data.get("outcome")
                result.outcomes.append(
                    observe(
                        result.name,
                        f"crux-availability:{e.data['scope']}",
                        "performance",
                        e.url,
                        "pass"
                        if state == "data"
                        else "failed-to-run"
                        if state == "error"
                        else "unavailable",
                        [e.id],
                        reason=e.data.get(
                            "detail", "Field data availability, not a vitals verdict"
                        ),
                    )
                )
            if not e.description.startswith("PageSpeed"):
                continue
            strategy = e.data.get("strategy")
            score = e.data.get("score")
            final_url = e.data.get("conditions", {}).get("final_url")
            redirected = bool(
                final_url and identity_component(final_url) != identity_component(e.url)
            )
            measured = (
                e.data.get("outcome", "data") == "data"
                and not redirected
                and isinstance(score, (float, int))
                and not isinstance(score, bool)
                and math.isfinite(score)
                and 0 <= score <= 1
            )
            if strategy in {"mobile", "desktop"}:
                status = (
                    ("fail" if score < 0.9 else "pass")
                    if measured
                    else "failed-to-run"
                    if e.data.get("outcome") == "error"
                    else "unavailable"
                )
                result.outcomes.append(
                    observe(
                        result.name,
                        f"lighthouse-{strategy}:{e.url}",
                        "performance",
                        e.url,
                        status,
                        [e.id],
                        reason="Lab redirected to a different page; no equivalent test"
                        if redirected
                        else e.data.get("detail", ""),
                    )
                )
    if result.name == "observatory":
        for e in result.evidence:
            if isinstance(e.data, dict) and e.data.get("grade"):
                result.outcomes.append(
                    observe(
                        result.name,
                        "observatory-review",
                        "security",
                        e.url,
                        "pass" if e.data.get("tests_failed") == 0 else "fail",
                        [e.id],
                    )
                )
    if result.name == "security":
        from ams_assess.modules.security import HEADERS

        for e in result.evidence:
            if e.description == "Verified TLS certificate":
                for check, failed in [
                    ("tls-expiry", e.data["days_to_expiry"] < 30),
                    ("tls-unverified", False),
                ]:
                    result.outcomes.append(
                        observe(
                            result.name,
                            check,
                            "security",
                            ctx.site_url,
                            "fail" if failed else "pass",
                            [e.id],
                        )
                    )
            if e.description == "HTTP response security headers" and 200 <= e.data["status"] < 300:
                headers = e.data["headers"]
                missing = [
                    h
                    for h in HEADERS
                    if h not in headers
                    and (h != "strict-transport-security" or e.url.startswith("https:"))
                ]
                if (
                    "x-frame-options" not in headers
                    and "frame-ancestors" not in headers.get("content-security-policy", "").lower()
                ):
                    missing.append("framing-policy")
                result.outcomes.append(
                    observe(
                        result.name,
                        "security-headers",
                        "security",
                        e.url,
                        "fail" if missing else "pass",
                        [e.id],
                    )
                )
                for h in HEADERS:
                    if h == "strict-transport-security" and not e.url.startswith("https:"):
                        status = "out-of-scope"
                    else:
                        status = "fail" if h in missing else "pass"
                    result.outcomes.append(
                        observe(
                            result.name,
                            f"header-missing:{h}",
                            "security",
                            e.url,
                            status,
                            [e.id],
                        )
                    )
    result.findings = scoped(result.findings, ctx.site_url, ctx.customer_id)
    represented = {(o.check_id, o.site, o.component) for o in result.outcomes}
    for finding in result.findings:
        if not finding.site or (finding.check_key, finding.site, finding.component) in represented:
            continue
        result.outcomes.append(
            observe(
                result.name,
                finding.check_key,
                finding.category,
                finding.component,
                "fail",
                finding.evidence_ids,
                coverage=finding.coverage,
            )
        )
    recorded = {o.check_id for o in result.outcomes}
    for check, category in CHECKS.get(result.name, {}).items():
        if check in recorded or (check == "axe" and any(k.startswith("axe:") for k in recorded)):
            continue
        if result.name == "performance" and any(k.startswith(check + ":") for k in recorded):
            continue
        status = (
            "out-of-scope"
            if category not in ctx.categories
            else {"skipped": "skipped", "error": "failed-to-run"}.get(result.status, "unavailable")
        )
        result.outcomes.append(
            observe(
                result.name,
                check,
                category,
                ctx.site_url,
                status,
                [],
                coverage=[],
                reason=result.reason or "No equivalent test observation available",
            )
        )
    if not result.outcomes:
        result.outcomes.append(
            observe(
                result.name,
                f"module:{result.name}",
                next(iter(sorted(ctx.categories))),
                ctx.site_url,
                {"skipped": "skipped", "error": "failed-to-run"}.get(result.status, "unavailable"),
                [],
                coverage=[],
                reason=result.reason or "No equivalent check outcome recorded",
            )
        )
    for outcome in result.outcomes:
        if not outcome.site:
            outcome.site = site_key(ctx.site_url)
