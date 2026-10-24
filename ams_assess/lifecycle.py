"""Explicit check observations and conservative, evidence-backed finding transitions."""

import hashlib
import re
from urllib.parse import urldefrag, urlsplit

from ams_assess.models import CheckOutcome, Evidence, Finding


def site_key(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removesuffix(".").removeprefix("www.")
    port = parts.port
    return host + (
        f":{port}" if port and (parts.scheme, port) not in {("https", 443), ("http", 80)} else ""
    )


def identity_component(value: str) -> str:
    """Normalise the website alias in page, viewport and evidence-coverage tokens.

    Other subdomains, ports, paths, queries and test qualifiers remain distinct.
    Evidence URLs themselves retain the address that was actually requested.
    """

    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        parts = urlsplit(url)
        host = site_key(url)
        if ":" in (parts.hostname or ""):
            host = f"[{parts.hostname}]" + (f":{parts.port}" if parts.port else "")
        return f"{parts.scheme}://{host}"

    return re.sub(r"https?://(?:\[[0-9a-fA-F:]+\]|[\w.-]+)(?::\d+)?", replace, value)


def check_key(check: str, component: str) -> str:
    check = identity_component(check)
    # Before per-page PSI, the lab finding described the homepage. Never alias a
    # legacy check for a different path, device or website to that homepage.
    if check in {"lighthouse-mobile", "lighthouse-desktop"}:
        parts = urlsplit(component)
        if parts.scheme in {"http", "https"} and parts.path in {"", "/"} and not parts.query:
            return f"{check}:{identity_component(component).rstrip('/')}/"
    return check


def site_identity(value: str) -> str:
    """Compare persisted host[:port] keys, including keys saved before DNS-dot normalization."""
    return re.sub(r"\.(?=:\d+$|$)", "", value.lower()).removeprefix("www.")


def key(finding: Finding) -> tuple[str, str, str]:
    return (
        site_identity(finding.site),
        check_key(finding.check_key or finding.id, finding.component),
        identity_component(finding.component),
    )


def scoped(findings: list[Finding], target: str, customer: str) -> list[Finding]:
    """Split page findings BEFORE consolidation, with a stable www/apex identity."""
    result = []
    for finding in findings:
        if finding.category == "discovery" or finding.provenance != "technical_evidence":
            result.append(finding)
            continue
        components = [finding.component] if finding.component else finding.urls or [target]
        components = [
            identity_component(urldefrag(c)[0] if not finding.component else c) for c in components
        ]
        for component in dict.fromkeys(components):
            check_id = check_key(finding.check_key or f"{finding.category}:{finding.id}", component)
            site = site_identity(finding.site or site_key(component) or site_key(target))
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
                        "coverage": [identity_component(c) for c in finding.coverage]
                        or [component],
                        "urls": [u for u in finding.urls if identity_component(u) == component]
                        or finding.urls,
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
            "check_id": check_key(check, component),
            "category": category,
            "site": site_key(component),
            "component": identity_component(component),
            "status": status,
            "evidence_ids": evidence_ids,
            "coverage": [identity_component(c) for c in coverage]
            if coverage is not None
            else ([identity_component(component)] if status in {"pass", "fail"} else []),
            "reason": reason,
        }
    )


def archive_observation(finding: Finding) -> None:
    """Archive the old basis before replacing current citations, including legacy records."""
    historical_ids = {
        eid
        for event in finding.events
        for eid in [
            *event.get("evidence_ids", []),
            *event.get("observation", {}).get("evidence_ids", []),
        ]
    }
    if not set(finding.evidence_ids) <= historical_ids:
        finding.events.append(
            {
                "status": "previously-observed",
                "run_id": finding.last_run_id,
                "date": finding.last_observed,
                "evidence_ids": finding.evidence_ids,
                "observation": finding.model_dump(exclude={"events", "resolution"}),
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
    for outcome in page_axe_outcomes(outcomes):
        if not set(outcome.evidence_ids) <= known.keys():
            raise ValueError("Check outcome references missing evidence")
        identity = (
            site_identity(outcome.site),
            check_key(outcome.check_id, outcome.component),
            identity_component(outcome.component),
        )
        rows.setdefault(identity, []).append(outcome)
    fresh = {key(f): f for f in incoming}
    failing_keys = set(fresh)
    result = []
    for old in existing:
        current = fresh.pop(key(old), None)
        observations = rows.get(key(old), [])
        if current is not None:
            archive_observation(old)
            current.id = old.id
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
            current.lifecycle_status = "current"
            current.resolution = None
            result.append(current)
            continue
        valid_passes = [
            o
            for o in observations
            if o.status == "pass"
            and o.evidence_ids
            and {identity_component(c) for c in old.coverage}
            <= {identity_component(c) for c in o.coverage}
            and all(known[i].provenance == "technical_evidence" for i in o.evidence_ids)
        ]
        failures = [
            o
            for o in observations
            if o.status == "fail"
            and o.evidence_ids
            and all(known[i].provenance == "technical_evidence" for i in o.evidence_ids)
        ]
        if failures and valid_passes:
            old.lifecycle_status = "needs_review"
        elif failures:
            archive_observation(old)
            event_status = "reopened" if old.lifecycle_status == "resolved" else "observed"
            old.lifecycle_status = "current"
            old.last_observed, old.last_run_id = date, run_id
            old.resolution = None
            old.evidence_ids = sorted({i for o in failures for i in o.evidence_ids})
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


def page_axe_outcomes(outcomes: list[CheckOutcome]) -> list[CheckOutcome]:
    """Interpret legacy node-group outcomes from one run without changing stored evidence.

    Only a browser axe result citing the SAME audit can supersede its passing nodes.
    Different audits and all non-axe contradictions still require review.
    """

    def audit_key(o):
        return (o.site, o.check_id, o.component, frozenset(o.evidence_ids))

    nonpasses = {
        audit_key(o)
        for o in outcomes
        if o.module == "browser"
        and o.check_id.startswith("axe:")
        and o.category == "accessibility"
        and o.status in {"fail", "unavailable"}
        and o.evidence_ids
    }
    return [
        o
        for o in outcomes
        if not (
            o.module == "browser"
            and o.category == "accessibility"
            and o.status == "pass"
            and audit_key(o) in nonpasses
        )
    ]
