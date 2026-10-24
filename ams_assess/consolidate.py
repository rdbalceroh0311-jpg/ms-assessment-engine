from ams_assess.lifecycle import site_key
from ams_assess.models import SEVERITIES, Evidence, Finding, worst_provenance


def consolidate_security_headers(
    findings: list[Finding], evidence: list[Evidence]
) -> list[Finding]:
    """One policy action per run, with individual header importance and original citations.

    Run only before AI interpretation/storage; module findings remain available to health
    checks, and historical store records are not merged across websites or rewritten.
    """
    scopes = {(f.site, f.component) for f in findings}
    if len(scopes) > 1:
        return [
            f
            for site, component in sorted(scopes)
            for f in consolidate_security_headers(
                [f for f in findings if (f.site, f.component) == (site, component)], evidence
            )
        ]
    headers = [
        f
        for f in findings
        if f.category == "security"
        and (
            f.check_key.startswith(("header-missing:", "header-review:"))
            and f.check_key.split(":", 1)[1] not in {"server", "set-cookie"}
            or f.check_key == "framing-policy"
        )
    ]
    if not headers:
        return findings
    first = headers[0].model_copy(deep=True)
    first.check_key = "security-headers"
    first.title = "Recommended security headers missing or need review"
    first.recommendation = (
        "Have the hosting team review the listed policies together, prioritising the higher "
        "importance items. Test content loading, embedding and HTTPS compatibility before "
        "deployment, then recheck the affected HTML responses and Observatory assessment."
    )
    first.detail = " ".join(
        f"{f.check_key.split(':', 1)[1] if ':' in f.check_key else 'Framing protection'} "
        f"(importance: {f.severity}): {f.detail} Evidence: {', '.join(f.evidence_ids)}."
        for f in headers
    )
    first.evidence_ids = sorted({eid for f in headers for eid in f.evidence_ids})
    first.urls = sorted({url for f in headers for url in f.urls})
    first.coverage = sorted({part for f in headers for part in f.coverage})
    first.severity = min((f.severity for f in headers), key=SEVERITIES.__getitem__)
    first.priority = min(f.priority for f in headers)
    first.provenance = worst_provenance(f.provenance for f in headers)
    first.confidence = (
        "confirmed" if all(f.confidence == "confirmed" for f in headers) else "needs_verification"
    )
    # An aggregate grade supports the review; it cannot prove which header failed.
    for e in evidence:
        if (
            e.source == "observatory"
            and isinstance(e.data, dict)
            and e.data.get("grade")
            and (not first.site or site_key(e.url) == first.site)
        ):
            if e.id not in first.evidence_ids:
                first.evidence_ids.append(e.id)
            first.detail += (
                f" Observatory grade {e.data['grade']} ({e.id}) is aggregate supporting evidence; "
                "it does not identify individual header failures."
            )
    removed = {f.id for f in headers}
    removed.update(f.id for f in findings if f.check_key == "observatory-review")
    return consolidate([f for f in findings if f.id not in removed] + [first], evidence)


def consolidate(findings: list[Finding], evidence: list[Evidence]) -> list[Finding]:
    provenance = {e.id: e.provenance for e in evidence}
    merged: dict[tuple[str, str, str, str], Finding] = {}
    for finding in findings:
        if not finding.evidence_ids or not set(finding.evidence_ids) <= provenance.keys():
            raise ValueError(f"Finding {finding.id} has missing evidence")
        key = (finding.site, finding.component, finding.category, finding.check_key or finding.id)
        if key not in merged:
            merged[key] = finding.model_copy(deep=True)
            continue
        current = merged[key]
        current.evidence_ids = sorted(set(current.evidence_ids + finding.evidence_ids))
        current.urls = sorted(set(current.urls + finding.urls))
        current.coverage = sorted(set(current.coverage + finding.coverage))
        if finding.detail not in current.detail:
            current.detail += " " + finding.detail
        if SEVERITIES[finding.severity] < SEVERITIES[current.severity]:
            current.severity = finding.severity
        current.priority = min(current.priority, finding.priority)
        if finding.confidence != "confirmed":
            current.confidence = finding.confidence
        current.provenance = worst_provenance([current.provenance, finding.provenance])
    for current in merged.values():
        # Worst-of the cited evidence; a claimed weaker provenance is kept, never upgraded.
        current.provenance = worst_provenance(
            [current.provenance, *(provenance[eid] for eid in current.evidence_ids)]
        )
        if current.provenance != "technical_evidence" and current.confidence == "confirmed":
            # "confirmed" means directly observed by a deterministic check.
            current.confidence = "needs_verification"
    return sorted(merged.values(), key=lambda f: (SEVERITIES[f.severity], f.category, f.id))
