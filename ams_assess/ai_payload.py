"""Explicit, recursive projections for AI input; unknown sources/fields fail closed.

Inventories, review authors, credentials, bindings and lifecycle history have no projection.
Connector evidence is reduced to coverage counts even if a saved aggregate is poisoned.
"""

import json
from typing import Any

from ams_assess.data_boundary import configured_secrets, sanitise
from ams_assess.models import Context, Evidence, Finding

FINDING_FIELDS = frozenset(
    {
        "id",
        "title",
        "category",
        "severity",
        "kind",
        "confidence",
        "evidence_ids",
        "detail",
        "recommendation",
        "urls",
        "likely_cause",
        "business_impact",
        "priority",
        "ams_service",
        "urgency",
        "dependencies",
        "information_required",
    }
)
OUTPUT_FIELDS = FINDING_FIELDS - {"urls"}
# True admits scalar values only; lists and objects require their own declared shape.
PAGE = {
    "title": True,
    "lang": True,
    "canonical": True,
    "visible_text_summary": True,
    "word_count": True,
    "h1": [True],
    "h2": [True],
    "meta": [{"name": True, "property": True, "content": True}],
    "images": [{"alt": True, "width": True, "height": True}],
    "forms": [{"action": True, "method": True, "inputs": True}],
}
METRIC = {"score": True, "numericValue": True, "numericUnit": True, "p75": True}
POLICIES: dict[str, Any] = {
    "browser": {"url": True, "viewport": True, "status": True, "page": PAGE},
    "siteone": {
        "url": True,
        "status": True,
        "type": True,
        "title": True,
        "reason": True,
        "successful_html_pages": True,
        "limit": True,
        "over_limit": True,
        "urls": [True],
        "resolved": True,
        "error": True,
    },
    "security": {
        "status": True,
        "url": True,
        "hostname_match": True,
        "protocol": True,
        "expires": True,
        "days_to_expiry": True,
        "error": True,
        "headers": {
            k: True
            for k in (
                "strict-transport-security",
                "content-security-policy",
                "x-frame-options",
                "x-content-type-options",
                "referrer-policy",
                "permissions-policy",
            )
        },
    },
    "observatory": {
        "grade": True,
        "score": True,
        "tests_passed": True,
        "tests_failed": True,
        "tests_quantity": True,
    },
    "performance": {
        "outcome": True,
        "detail": True,
        "url": True,
        "scope": True,
        "strategy": True,
        "score": True,
        "fetch_time": True,
        "metrics": {
            k: METRIC
            for k in (
                "largest_contentful_paint",
                "first_contentful_paint",
                "interaction_to_next_paint",
                "cumulative_layout_shift",
                "largest-contentful-paint",
                "first-contentful-paint",
                "total-blocking-time",
                "speed-index",
                "cumulative-layout-shift",
            )
        },
    },
    "domain_email": {
        "status": True,
        "record": True,
        "valid": True,
        "error": True,
        "records": [True],
        "zone": True,
        "spf": {"valid": True, "record": True, "error": True},
        "dmarc": {"valid": True, "record": True, "error": True},
    },
    "site_checks": {
        "key": True,
        "title": True,
        "status": True,
        "checked": True,
        "why": True,
        "recommendation": True,
        "scopes": [True],
    },
    "presence": {
        "platform": True,
        "status": True,
        "loaded": True,
        "urls": [True],
        "pages_sampled": [True],
        "pages_scanned": True,
        "source_evidence_ids": [True],
        "browser_evidence_ids": [True],
    },
    "profile": {
        "name": True,
        "industry": True,
        "buying_model": True,
        "service_area": [True],
        "websites": [True],
    },
    "discovery": {
        "section": True,
        "question": True,
        "answer": True,
        "statement": True,
        "note": True,
    },
    # Legacy connected/free-text provider payloads have no safe inventory projection.
    "gsc": {},
    "dataforseo": {},
    "ga4": {},
    "gbp": {},
    "bing_wmt": {},
    "workspace": {},
    "m365": {},
    "wp_cli": {},
}
AGGREGATE = {"status": True, "rows_returned": True, "truncated": True}


def project(value: Any, policy: Any) -> Any:
    if policy is True:
        return value if type(value) in {str, int, float, bool, type(None)} else None
    if isinstance(policy, list):
        return [project(v, policy[0]) for v in value[:100]] if isinstance(value, list) else []
    if isinstance(policy, dict):
        return (
            {k: project(value[k], p) for k, p in policy.items() if k in value}
            if isinstance(value, dict)
            else {}
        )
    raise ValueError("Invalid AI field policy")


def compact_evidence(evidence: list[Evidence]) -> list[dict[str, Any]]:
    compact = []
    for item in evidence:
        data = item.data
        if isinstance(data, dict) and data.get("connector"):
            data = project(data.get("ai_aggregate", {}), AGGREGATE)
        else:
            data = project(data, POLICIES.get(item.source, {}))
        serialized = json.dumps(sanitise(data, ai=True), ensure_ascii=False)
        compact.append(
            {
                "id": item.id,
                "source": item.source,
                "url": item.url,
                "description": item.description,
                "provenance": item.provenance,
                "data": serialized[:10000],
                "truncated": len(serialized) > 10000,
            }
        )
    return sanitise(compact, ai=True)


def payload(ctx: Context, findings: list[Finding], evidence: list[Evidence]) -> dict[str, Any]:
    result = {
        "customer": ctx.customer_name,
        "target": ctx.target,
        "resolved_url": ctx.site_url,
        "categories": sorted(ctx.categories),
        "local_fixture": ctx.local,
        "collection": {
            "successful_html_pages": len(ctx.pages),
            "modules": [
                {
                    "name": r.name,
                    "status": r.status,
                    "reason": r.reason,
                    "evidence_ids": [e.id for e in r.evidence],
                }
                for r in ctx.results
            ],
        },
        "findings": [
            {k: v for k, v in f.model_dump().items() if k in FINDING_FIELDS} for f in findings
        ],
        "evidence": compact_evidence(evidence),
    }
    return sanitise(result, configured_secrets(ctx.config), ai=True)
