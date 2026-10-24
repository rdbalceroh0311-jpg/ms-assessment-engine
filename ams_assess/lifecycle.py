"""Explicit check observations and conservative, evidence-backed finding transitions."""

import hashlib
from urllib.parse import urldefrag, urlsplit

from ams_assess.models import CheckOutcome, Evidence, Finding


def site_key(url: str) -> str:
    parts = urlsplit(url)
    return (parts.hostname or "").lower() + (f":{parts.port}" if parts.port else "")


def key(finding: Finding) -> tuple[str, str, str]:
    return finding.site, finding.check_key or finding.id, finding.component


def scoped(findings: list[Finding], target: str, customer: str) -> list[Finding]:
    """Split page findings BEFORE consolidation; never merge host aliases implicitly."""
    result = []
    for finding in findings:
        if finding.category == "discovery" or finding.provenance != "technical_evidence":
            result.append(finding)
            continue
        check_id = finding.check_key or f"{finding.category}:{finding.id}"
        components = [finding.component] if finding.component else finding.urls or [target]
        for component in dict.fromkeys(components):
            component = urldefrag(component)[0] if not finding.component else component
            site = finding.site or site_key(component) or site_key(target)
            digest = hashlib.sha256(
                repr((customer, site, check_id, component)).encode()
            ).hexdigest()[:24]
            result.append(
                finding.model_copy(
                    update={
                        "id": f"tech-{digest}",
                        "check_key": check_id,
                        "site": site,
                        "component": component,
                        "coverage": finding.coverage or [component],
                        "urls": [component] if component in finding.urls else finding.urls,
                    },
                    deep=True,
                )
            )
    return result


def observe(
    module: str,
    check: str,
    category: str,
    component: str,
    status: str,
    evidence_ids: list[str],
    *,
    coverage: list[str] | None = None,
    reason: str = "",
) -> CheckOutcome:
    return CheckOutcome.model_validate(
        {
            "module": module,
            "check_id": check,
            "category": category,
            "site": site_key(component),
            "component": component,
            "status": status,
            "evidence_ids": evidence_ids,
            "coverage": coverage
            if coverage is not None
            else ([component] if status in {"pass", "fail"} else []),
            "reason": reason,
        }
    )


def transition(
    existing: list[Finding],
    incoming: list[Finding],
    outcomes: list[CheckOutcome],
    evidence: list[Evidence],
    run_id: str,
    date: str,
) -> list[Finding]:
    """Absence is never a pass. A pass must cover every previously failing component."""
    known = {e.id: e for e in evidence}
    rows: dict[tuple[str, str, str], list[CheckOutcome]] = {}
    for outcome in outcomes:
        if not set(outcome.evidence_ids) <= known.keys():
            raise ValueError("Check outcome references missing evidence")
        rows.setdefault((outcome.site, outcome.check_id, outcome.component), [])
        rows[(outcome.site, outcome.check_id, outcome.component)].append(outcome)
    fresh = {key(f): f for f in incoming}
    failing_keys = set(fresh)
    result = []
    for old in existing:
        current = fresh.pop(key(old), None)
        observations = rows.get(key(old), [])
        if current is not None:
            current.first_observed = old.first_observed or old.last_observed or date
            current.last_observed = date
            current.last_run_id = run_id
            current.events = [
                *old.events,
                {
                    "status": "reopened" if old.lifecycle_status == "resolved" else "observed",
                    "run_id": run_id,
                    "date": date,
                    "evidence_ids": current.evidence_ids,
                    "previous_severity": old.severity,
                    "observation": current.model_dump(exclude={"events", "resolution"}),
                },
            ]
            # Preserve old citations/history without retaining the highest historical severity.
            current.evidence_ids = list(dict.fromkeys(old.evidence_ids + current.evidence_ids))
            current.lifecycle_status = "current"
            current.resolution = None
            result.append(current)
            continue
        valid_passes = [
            o
            for o in observations
            if o.status == "pass"
            and o.evidence_ids
            and set(old.coverage) <= set(o.coverage)
            and all(known[i].provenance == "technical_evidence" for i in o.evidence_ids)
        ]
        failures = [o for o in observations if o.status == "fail"]
        if failures and valid_passes:
            old.lifecycle_status = "needs_review"
        elif failures:
            event_status = "reopened" if old.lifecycle_status == "resolved" else "observed"
            old.lifecycle_status = "current"
            old.last_observed, old.last_run_id = date, run_id
            old.resolution = None
            old.events.append(
                {
                    "status": event_status,
                    "run_id": run_id,
                    "date": date,
                    "evidence_ids": sorted({i for o in failures for i in o.evidence_ids}),
                }
            )
        elif valid_passes and old.lifecycle_status != "resolved":
            old.resolution = {
                "date": date,
                "run_id": run_id,
                "evidence_ids": sorted({i for o in valid_passes for i in o.evidence_ids}),
            }
            old.events.append({"status": "resolved", **old.resolution})
            old.lifecycle_status = "resolved"
        elif old.lifecycle_status not in {"resolved", "needs_review"}:
            old.lifecycle_status = "not_rechecked"
        result.append(old)
    for current in fresh.values():
        current.first_observed = current.last_observed = date
        current.last_run_id = run_id
        current.events = [
            {
                "status": "observed",
                "run_id": run_id,
                "date": date,
                "evidence_ids": current.evidence_ids,
                "observation": current.model_dump(exclude={"events", "resolution"}),
            }
        ]
        current.lifecycle_status = "current"
        result.append(current)
    # Conflicting explicit results stay out of actionable recommendations, including new issues.
    for finding in result:
        statuses = {o.status for o in rows.get(key(finding), [])}
        if key(finding) in failing_keys:
            statuses.add("fail")
        if {"pass", "fail"} <= statuses:
            finding.lifecycle_status = "needs_review"
    return result
