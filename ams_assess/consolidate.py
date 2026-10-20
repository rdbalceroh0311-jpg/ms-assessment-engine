from ams_assess.models import SEVERITIES, Evidence, Finding, worst_provenance


def consolidate(findings: list[Finding], evidence: list[Evidence]) -> list[Finding]:
    provenance = {e.id: e.provenance for e in evidence}
    merged: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        if not finding.evidence_ids or not set(finding.evidence_ids) <= provenance.keys():
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
