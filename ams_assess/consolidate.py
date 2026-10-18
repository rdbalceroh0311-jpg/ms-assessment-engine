from ams_assess.models import SEVERITIES, Evidence, Finding


def consolidate(findings: list[Finding], evidence: list[Evidence]) -> list[Finding]:
    known = {e.id for e in evidence}
    merged: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        if not finding.evidence_ids or not set(finding.evidence_ids) <= known:
            raise ValueError(f"Finding {finding.id} has missing evidence")
        key = (finding.category, finding.check_key or finding.id)
        if key not in merged:
            merged[key] = finding.model_copy(deep=True)
            continue
        current = merged[key]
        current.evidence_ids = sorted(set(current.evidence_ids + finding.evidence_ids))
        current.urls = sorted(set(current.urls + finding.urls))
        if finding.detail not in current.detail:
            current.detail += " " + finding.detail
        if SEVERITIES[finding.severity] < SEVERITIES[current.severity]:
            current.severity = finding.severity
        current.priority = min(current.priority, finding.priority)
        if finding.confidence != "confirmed":
            current.confidence = finding.confidence
    return sorted(merged.values(), key=lambda f: (SEVERITIES[f.severity], f.category, f.id))
