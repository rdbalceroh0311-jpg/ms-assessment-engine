"""Per-customer evolving assessment store: data/customers/<id>/assessment.json.

Every run and every discovery ingestion merges into one store. Evidence IDs are namespaced per
run on merge (``ev-gsc-001`` -> ``ev-gsc-r2-001``) so runs never collide, and everything that
cites an evidence ID is remapped with the same mapping.
"""

import contextlib
import copy
import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Generator
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ams_assess import register
from ams_assess.ai import CITATIONS
from ams_assess.consolidate import consolidate
from ams_assess.discovery import DISCOVERY_SECTIONS, Notes, slug
from ams_assess.figures import sentences
from ams_assess.lifecycle import key as technical_key
from ams_assess.lifecycle import observe, page_axe_outcomes, scoped, site_key, transition
from ams_assess.models import (
    DISCOVERY_CATEGORY,
    SEVERITIES,
    CheckOutcome,
    Evidence,
    Finding,
    customer_path,
)
from ams_assess.modules import gsc
from ams_assess.platforms import BY_KEY
from ams_assess.presence_discovery import nap_detail, repair_discovery_nap
from ams_assess.profile import (
    Verification,
    apply_verifications,
    current_findings,
    merge_profile,
)

SCHEMA_VERSION = 3
_HELD: ContextVar[frozenset[str]] = ContextVar("assessment_locks", default=frozenset())
STORE_FILE = "assessment.json"
LOCK_FILE = ".assessment.lock"
LOCK_TIMEOUT = 30.0
STAGES = ("public", "connected", "discovery")
EVIDENCE_ID = re.compile(r"ev-[a-z][a-z0-9_]*-\d+")
LOCAL_ID = re.compile(r"^ev-([a-z][a-z0-9_]*)-(\d+)$")
RUN_EVIDENCE = re.compile(r"^ev-[a-z][a-z0-9_]*-(r\d+)-\d+$")
# Version of the stored-evidence analysis already applied (analyse_stored_connected).
CONNECTED_ANALYSIS = 1
NO_AI = {"status": "skipped", "provider": None, "reason": "Discovery notes are not AI-interpreted"}


def now() -> str:
    return datetime.now(UTC).isoformat()


# Statement detail that can change between ingestions; entries stored before a field existed
# lack it, which compares equal to the empty default.
DETAIL_FIELDS = ("frequency", "impact", "staff_time", "errors_rework", "cost", "workaround")


def identity(fields: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """What makes a statement the same one across ingestions: its text and who/what it concerns.

    DETAIL_FIELDS are detail that can change (a new version supersedes the old evidence);
    owner and systems are context, so the same words in another context are a distinct statement.
    """
    return (
        " ".join(fields["statement"].lower().split()),
        " ".join(fields.get("owner", "").lower().split()),
        tuple(sorted(" ".join(x.lower().split()) for x in fields.get("systems", []))),
    )


def same(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Whether a stored entry already says exactly this (identity plus every detail field)."""

    def detail(f: dict[str, Any]) -> tuple[str, ...]:
        return tuple(" ".join(f.get(k, "").lower().split()) for k in DETAIL_FIELDS)

    return identity(entry) == identity(fields) and detail(entry) == detail(fields)


def remap(value: Any, mapping: dict[str, str]) -> Any:
    """Rewrite every evidence ID in any JSON structure (values, nested prose and keys)."""
    if isinstance(value, str):
        return EVIDENCE_ID.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
    if isinstance(value, list):
        return [remap(v, mapping) for v in value]
    if isinstance(value, dict):
        return {remap(k, mapping): remap(v, mapping) for k, v in value.items()}
    return value


def namespaced(evidence_id: str, run_id: str) -> str:
    match = LOCAL_ID.match(evidence_id)
    if not match:
        return f"{evidence_id}-{run_id}"
    return f"ev-{match[1]}-{run_id}-{match[2]}"


def store_file(root: Path, customer: str) -> Path:
    path = customer_path(root, customer) / STORE_FILE
    if path.is_symlink():
        raise ValueError("Customer store must not be a symlink")
    return path


def new_store(customer: str, customer_name: str) -> dict[str, Any]:
    stamp = now()
    return {
        "schema_version": SCHEMA_VERSION,
        "customer_id": customer,
        "customer_name": customer_name,
        "created_at": stamp,
        "updated_at": stamp,
        "runs": [],
        "evidence": [],
        "findings": [],
        "discovery": {},
        "report_files": {},
        "report_revisions": [],
        "finding_history": [],
        "needs_review": [],
        "stages_assessed": [],
        "connected_analysis": CONNECTED_ANALYSIS,
    }


def connector_bindings(root: Path, customer: str) -> dict[str, Any]:
    """Read binding identities without migrating or analysing another customer's findings."""
    path = store_file(root, customer)
    with locked(root, customer, create=False):
        current = json.loads(path.read_bytes())
        if (
            not isinstance(current, dict)
            or current.get("customer_id") != customer
            or current.get("schema_version") not in {1, 2, 3}
        ):
            raise ValueError("Unsupported or mismatched customer store")
        bindings = current.get("connectors", {})
        if not isinstance(bindings, dict) or any(
            not isinstance(binding, dict) or not isinstance(binding.get("resource_id", ""), str)
            for binding in bindings.values()
        ):
            raise ValueError("Invalid connector bindings")
        return bindings


def load(root: Path, customer: str, *, migrate_legacy: bool = True) -> dict[str, Any] | None:
    """Read only this customer's store; None when it does not exist yet."""
    path = store_file(root, customer)
    if not path.is_file():
        return None
    guard = (
        contextlib.nullcontext()
        if str(path.parent) in _HELD.get()
        else locked(root, customer, create=False)
    )
    with guard:
        original = path.read_bytes()
        store = json.loads(original)
        if store.get("customer_id") != customer or store.get("schema_version") not in {1, 2, 3}:
            raise ValueError("Unsupported or mismatched customer store")
        if store["schema_version"] != SCHEMA_VERSION:
            if not migrate_legacy:
                raise ValueError("Legacy store: open it with register show in the CLI to migrate")
            # Backup the exact original bytes before ANY schema conversion or saved change.
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = path.with_name(f"assessment.json.backup-{stamp}")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            migrate(store)
            save(root, customer, store)
        unique_findings(store)
        repair_axe_reviews(store)
        findings = [Finding.model_validate(f) for f in store["findings"]]
        kept = analyse_stored_connected(store, supersede_unscoped(store, findings))
        if kept != findings:
            store["findings"] = [f.model_dump(mode="json") for f in kept]
        return store


def unique_findings(store: dict[str, Any]) -> None:
    ids = [f["id"] for f in store.get("findings", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate finding IDs in customer store")


def repair_axe_reviews(store: dict[str, Any]) -> None:
    """Correct false historical axe conflicts in the current view; retain raw runs/history."""
    repaired = set()
    for index, raw in enumerate(store.get("findings", [])):
        if raw.get("lifecycle_status") != "needs_review" or not raw.get("check_key", "").startswith(
            "axe:"
        ):
            continue
        finding = Finding.model_validate(raw)
        for run in reversed(store.get("runs", [])):
            rows = [
                CheckOutcome.model_validate(o)
                for o in run.get("outcomes", [])
                if (o["site"], o["check_id"], o["component"]) == technical_key(finding)
            ]
            if not rows:
                continue
            corrected = page_axe_outcomes(rows)
            statuses = {o.status for o in corrected}
            if len(corrected) < len(rows) and "fail" in statuses and "pass" not in statuses:
                updated = transition(
                    [finding],
                    [],
                    corrected,
                    [Evidence.model_validate(e) for e in store["evidence"]],
                    run["run_id"],
                    run["created_at"],
                )[0]
                if updated.lifecycle_status == "current":
                    # transition verifies the correction, but this is not a new observation.
                    updated.events[-1]["status"] = "review-corrected"
                    updated.events[-1]["reason"] = (
                        "Axe failing nodes override same-audit passing nodes"
                    )
                    store["findings"][index] = updated.model_dump(mode="json")
                    repaired.add(finding.id)
            break
    if repaired:
        store["needs_review"] = [
            r
            for r in store.get("needs_review", [])
            if not (
                r.get("finding_id") in repaired
                and r.get("reason") == "conflicting explicit check outcomes need review"
            )
        ]


def run_number(run_id: str) -> int:
    match = re.fullmatch(r"r(\d+)", run_id or "")
    return int(match[1]) if match else 0


def supersede_unscoped(store: dict[str, Any], findings: list[Finding]) -> list[Finding]:
    """Retire findings stored without a website once a later per-website equivalent exists.

    Runs before per-module scoping stored some findings (e.g. presence findings citing the
    customer profile) with no site, so a newer run's scoped finding for the same check never
    replaced them. The predecessor is retired only when every run it cites targeted one
    website, exactly one current finding has the same category and check on that website, and
    that finding was observed in a later run.
    """
    runs = {r["run_id"]: r for r in store.get("runs", []) if r.get("kind") == "assessment"}
    replaced: dict[str, Finding] = {}
    for legacy in findings:
        if (
            legacy.site
            or legacy.component
            or legacy.category != "presence"
            or not legacy.check_key.startswith("platform:")
        ):
            continue
        cited = {m[1] for e in legacy.evidence_ids if (m := RUN_EVIDENCE.match(e))}
        if not cited or not cited <= runs.keys():
            continue
        sites = {site_key(runs[r].get("resolved_url") or runs[r]["target"]) for r in cited}
        successors = [
            f
            for f in findings
            if f.site
            and f.site in sites
            and f.category == legacy.category
            and (f.check_key or f.id) == (legacy.check_key or legacy.id)
            and f.lifecycle_status != "needs_review"
        ]
        if (
            len(sites) == 1
            and len(successors) == 1
            and run_number(successors[0].last_run_id) > max(map(run_number, cited))
        ):
            replaced[legacy.id] = successors[0]
    for legacy in [f for f in findings if f.id in replaced]:
        successor = replaced[legacy.id]
        _retire(store, legacy, "website-less predecessor of a per-website finding", successor.id)
        successor.events.append(
            {
                "status": "identity-predecessor",
                "finding_id": legacy.id,
                "evidence_ids": legacy.evidence_ids,
            }
        )
    if replaced:
        store["needs_review"] = [
            r for r in store.get("needs_review", []) if r.get("finding_id") not in replaced
        ]
    return [f for f in findings if f.id not in replaced]


def analyse_stored_connected(store: dict[str, Any], findings: list[Finding]) -> list[Finding]:
    """Derive connected-evidence findings once for runs stored before GSC analysis existed.

    Only the latest run with GSC evidence per website is analysed, from that run's own stored
    evidence, and each finding is dated to that run. Only known equivalent legacy checks are
    retired; citing GSC alone does not make an unrelated finding a duplicate.
    """
    if store.get("connected_analysis", 0) >= CONNECTED_ANALYSIS:
        return findings
    store["connected_analysis"] = CONNECTED_ANALYSIS
    evidence = [Evidence.model_validate(e) for e in store.get("evidence", [])]
    by_run: dict[str, list[Evidence]] = {}
    for e in evidence:
        if match := RUN_EVIDENCE.match(e.id):
            by_run.setdefault(match[1], []).append(e)
    latest: dict[str, dict[str, Any]] = {}
    for run in sorted(store.get("runs", []), key=lambda r: run_number(r["run_id"])):
        if run.get("kind") == "assessment" and any(
            e.source == gsc.name for e in by_run.get(run["run_id"], [])
        ):
            latest[site_key(run.get("resolved_url") or run["target"])] = run
    result = list(findings)
    for run in latest.values():
        own = by_run[run["run_id"]]
        site_url = run.get("resolved_url") or run["target"]
        derived, _ = gsc.connected_findings(own, site_url)
        derived = scoped(consolidate(derived, evidence), site_url, store["customer_id"])
        known = {technical_key(f) for f in result}
        added = []
        for finding in derived:
            if technical_key(finding) in known:
                continue
            finding.first_observed = finding.last_observed = run["created_at"]
            finding.last_run_id = run["run_id"]
            finding.events = [
                {
                    "status": "observed",
                    "run_id": run["run_id"],
                    "date": run["created_at"],
                    "evidence_ids": finding.evidence_ids,
                    "reason": "Derived from this run's stored Search Console evidence",
                }
            ]
            added.append(finding)
        connected = {e.id for e in own if e.source == gsc.name}
        successors = {f.check_key: f for f in derived}
        aliases = {"site-index-sample-page-exposed": "gsc:placeholder-impressions"}
        replaced = [
            f
            for f in result
            if connected & set(f.evidence_ids)
            and aliases.get(f.check_key) in successors
            and (not f.site or f.site == site_key(site_url))
        ]
        for finding in replaced:
            successor = successors[aliases[finding.check_key]]
            _retire(
                store,
                finding,
                "Equivalent legacy finding replaced by deterministic Search Console analysis",
                successor.id,
            )
        result = [f for f in result if f not in replaced] + added
    return result


def deduplicate_technical(findings: list[Finding]) -> list[Finding]:
    """Migration/predecessor aliases share one identity; preserve their original observations."""

    def observed_at(value: str) -> datetime:
        date = datetime.fromisoformat(value)
        return date.replace(tzinfo=date.tzinfo or UTC)

    merged: dict[tuple[str, str, str], Finding] = {}
    for finding in findings:
        identity = technical_key(finding)
        if identity not in merged:
            merged[identity] = finding
            continue
        current = merged[identity]
        current.evidence_ids = list(dict.fromkeys(current.evidence_ids + finding.evidence_ids))
        current.coverage = list(dict.fromkeys(current.coverage + finding.coverage))
        current.urls = list(dict.fromkeys(current.urls + finding.urls))
        current.severity = min((current.severity, finding.severity), key=SEVERITIES.__getitem__)
        current.priority = min(current.priority, finding.priority)
        first_dates = [
            date
            for date in (current.first_observed, finding.first_observed)
            if date not in {"", "unknown"}
        ]
        if first_dates:
            current.first_observed = min(first_dates, key=observed_at)
        if finding.last_observed not in {"", "unknown"} and (
            current.last_observed in {"", "unknown"}
            or observed_at(finding.last_observed) > observed_at(current.last_observed)
        ):
            current.last_observed = finding.last_observed
            current.last_run_id = finding.last_run_id
        current.events.extend(event for event in finding.events if event not in current.events)
        current.events.append(
            {
                "status": "identity-predecessor",
                "observation": finding.model_dump(exclude={"events"}),
            }
        )
        if finding.detail not in current.detail:
            current.detail += " " + finding.detail
    return list(merged.values())


def header_predecessors(store: dict[str, Any], findings: list[Finding]) -> list[Finding]:
    result = []
    for finding in findings:
        if finding.check_key.startswith("header-missing:") or finding.check_key == "framing-policy":
            original = finding.model_copy(deep=True)
            finding.check_key = "security-headers"
            finding = scoped([finding], "", store["customer_id"])[0]
            _retire(
                store,
                original,
                "per-header predecessor of consolidated security-headers",
                finding.id,
            )
        result.append(finding)
    return deduplicate_technical(result)


def migrate(store: dict[str, Any]) -> None:
    runs = [r for r in store.get("runs", []) if r.get("kind") == "assessment"]
    evidence = {e["id"]: e for e in store.get("evidence", [])}
    findings, technical = [], []
    checked_evidence = [Evidence.model_validate(e) for e in evidence.values()]
    for raw in store.get("findings", []):
        finding = consolidate([Finding.model_validate(raw)], checked_evidence)[0]
        if finding.category == DISCOVERY_CATEGORY or finding.provenance != "technical_evidence":
            findings.append(finding.model_dump(mode="json"))
            continue
        # Legacy consolidation may span sites. Split known affected URLs; never infer a host
        # from an unrelated run. Ambiguous legacy components cannot resolve automatically.
        cited = [evidence[i] for i in finding.evidence_ids if i in evidence]
        urls = finding.urls or list(dict.fromkeys(e["url"] for e in cited if site_key(e["url"])))
        if not urls:
            urls = ["legacy:unattributed:" + finding.id]
        finding.urls = urls
        for part in scoped([finding], "", store["customer_id"]):
            matching_runs = [
                r for r in runs if any(f"-{r['run_id']}-" in i for i in part.evidence_ids)
            ]
            part.lifecycle_status = "not_rechecked"
            part.last_observed = (
                matching_runs[-1]["created_at"]
                if matching_runs
                else store.get("updated_at", "unknown")
            )
            part.first_observed = part.last_observed
            part.last_run_id = matching_runs[-1]["run_id"] if matching_runs else "legacy"
            part.events = [
                {
                    "status": "legacy-observed",
                    "date": part.last_observed,
                    "run_id": part.last_run_id,
                    "evidence_ids": part.evidence_ids,
                }
            ]
            if (
                finding.check_key.startswith("header-missing:")
                or finding.check_key == "framing-policy"
            ):
                # Retire the original record ID when its aggregate successor is created.
                part.id = finding.id
            technical.append(part)
    store["findings"] = findings + [
        f.model_dump(mode="json") for f in header_predecessors(store, technical)
    ]
    store.setdefault("finding_history", [])
    store.setdefault("needs_review", [])
    store.setdefault("report_revisions", [])
    store["schema_version"] = SCHEMA_VERSION


def save(root: Path, customer: str, store: dict[str, Any]) -> None:
    """Atomic write: a crash leaves either the old or the new store, never a partial file."""
    unique_findings(store)
    path = store_file(root, customer)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Customer statements are private: keep the customer directory owner-only.
    path.parent.chmod(0o700)
    store["updated_at"] = now()
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".assessment-", suffix=".tmp")  # 0600
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(store, indent=2, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


@contextlib.contextmanager
def locked(root: Path, customer: str, create: bool = True) -> Generator[None]:
    """Exclusive per-customer lock spanning load -> modify -> save; waits LOCK_TIMEOUT at most."""
    directory = customer_path(root, customer)
    lock_key = str(directory)
    if not directory.is_dir():
        if not create:
            raise ValueError("No assessment store for this customer")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "Customer store is busy (another import or report is running); try again"
                    ) from None
                time.sleep(0.05)
        token = _HELD.set(_HELD.get() | {lock_key})
        try:
            yield
        finally:
            _HELD.reset(token)
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _begin(root: Path, customer: str, customer_name: str) -> tuple[dict[str, Any], str]:
    store = load(root, customer) or new_store(customer, customer_name)
    if customer_name and customer_name != customer:
        store["customer_name"] = customer_name
    seen = [int(r["run_id"][1:]) for r in store["runs"]]
    return store, f"r{max(seen, default=0) + 1}"


def _finish(
    store: dict[str, Any], findings: list[Finding], evidence: list[Evidence], stages: list[str]
) -> None:
    store["evidence"] = [e.model_dump(mode="json") for e in evidence]
    store["findings"] = [f.model_dump(mode="json") for f in _lifecycle(store, findings, evidence)]
    store["stages_assessed"] = [s for s in STAGES if s in {*store["stages_assessed"], *stages}]


def _retire(store: dict[str, Any], finding: Finding, reason: str, by: str = "") -> None:
    """Keep a finding version in history; it is no longer active or recommended."""
    entry = {**finding.model_dump(mode="json"), "status": "superseded", "reason": reason}
    entry.update(superseded_at=now(), **({"superseded_by": by} if by else {}))
    store.setdefault("finding_history", []).append(entry)


def _lifecycle(
    store: dict[str, Any], findings: list[Finding], evidence: list[Evidence]
) -> list[Finding]:
    """Retire findings resting only on superseded evidence; flag stale or conflicting basis."""
    successor = {
        e.data["supersedes"]: e.id
        for e in evidence
        if e.source == "discovery" and isinstance(e.data, dict) and e.data.get("supersedes")
    }

    def later(eid: str) -> set[str]:
        versions: set[str] = set()
        while eid in successor and successor[eid] not in versions:
            eid = successor[eid]
            versions.add(eid)
        return versions

    active, review = [], []
    for finding in findings:
        stale = [i for i in finding.evidence_ids if i in successor]
        if stale and len(stale) == len(finding.evidence_ids):
            _retire(store, finding, "every statement it cites has been superseded")
            continue
        active.append(finding)
        # Part of the basis changed and the finding was not re-derived from the new version.
        unanswered = [i for i in stale if not later(i) & set(finding.evidence_ids)]
        if unanswered:
            review.append(
                {
                    "finding_id": finding.id,
                    "title": finding.title,
                    "reason": "it cites statements that have since been superseded",
                    "evidence_ids": unanswered,
                }
            )
    for key, entries in store["discovery"].get("sections", {}).items():
        groups: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
        for entry in entries:
            groups.setdefault(identity(entry), []).append(entry["evidence_id"])
        for ids in groups.values():
            if len(ids) > 1:
                # Every active finding resting on a conflicting version is held for review too.
                affected = [f.id for f in active if set(f.evidence_ids) & set(ids)]
                review.append(
                    {
                        "title": DISCOVERY_SECTIONS[key],
                        "reason": "the same statement is recorded with conflicting details",
                        "evidence_ids": ids,
                        "finding_ids": affected,
                    }
                )
    review.extend(
        {
            "finding_id": f.id,
            "title": f.title,
            "reason": "conflicting explicit check outcomes need review",
            "evidence_ids": f.evidence_ids,
        }
        for f in active
        if f.lifecycle_status == "needs_review"
    )
    store["needs_review"] = review
    return active


def unique_id(original: str, run_id: str, taken: set[str]) -> str:
    """Deterministic collision rename that always stays within the 64-character ID limit."""
    if original not in taken:
        return original
    for n in range(len(taken) + 2):
        suffix = f"-{run_id}" + (f"x{n}" if n else "")
        candidate = original[: 64 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise ValueError("Cannot allocate a unique finding ID")


def claim(finding: Finding) -> tuple[str, ...]:
    """What a finding asserts; evidence, provenance and confidence are tracked separately."""
    return tuple(
        str(getattr(finding, k))
        for k in (
            "title",
            "kind",
            "severity",
            "detail",
            "recommendation",
            "ams_service",
            "likely_cause",
            "business_impact",
        )
    )


def _combine(
    store: dict[str, Any], incoming: list[Finding], evidence: list[Evidence], run_id: str
) -> tuple[list[Finding], dict[str, str]]:
    """Consolidate incoming findings into the store; returns findings and incoming->final ids."""
    existing = [Finding.model_validate(f) for f in store["findings"]]
    by_key = {(f.category, f.check_key or f.id): f for f in existing}
    taken = {f.id for f in existing} | {f["id"] for f in store.get("finding_history", [])}
    id_map: dict[str, str] = {}
    renamed = []
    for finding in incoming:
        key = (finding.category, finding.check_key or finding.id)
        current = by_key.get(key)
        final = current.id if current else None
        if current and finding.category == DISCOVERY_CATEGORY and claim(current) != claim(finding):
            # A re-import that changes what a finding says is a new version, never concatenated.
            final = unique_id(finding.id, run_id, taken)
            taken.add(final)
            existing.remove(current)
            del by_key[key]
            _retire(store, current, "re-imported with a changed claim", final)
        if final is None:
            final = unique_id(finding.id, run_id, taken)
            taken.add(final)
        id_map[finding.id] = final
        # Rebuild through the constructor (validated); keep the original fallback key stable.
        renamed.append(
            Finding.model_validate({**finding.model_dump(), "id": final, "check_key": key[1]})
        )
    return consolidate(existing + renamed, evidence), id_map


def merge_run(
    root: Path,
    customer: str,
    customer_name: str,
    data: dict[str, Any],
    evidence: list[Evidence],
    run_name: str,
    stages: list[str],
    *,
    lifecycle_sources: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Merge one finished assessment run (its results.json dict + evidence) into the store."""
    with locked(root, customer):
        return _merge_run(
            root,
            customer,
            customer_name,
            data,
            evidence,
            run_name,
            stages,
            lifecycle_sources=lifecycle_sources,
        )


def _merge_run(
    root: Path,
    customer: str,
    customer_name: str,
    data: dict[str, Any],
    evidence: list[Evidence],
    run_name: str,
    stages: list[str],
    *,
    lifecycle_sources: frozenset[str] | None = None,
) -> dict[str, Any]:
    store, run_id = _begin(root, customer, customer_name)
    if any(r["run_dir"] == run_name for r in store["runs"]):
        return store
    mapping = {e.id: namespaced(e.id, run_id) for e in evidence}
    stored = [Evidence.model_validate(remap(e.model_dump(mode="json"), mapping)) for e in evidence]
    all_evidence = [Evidence.model_validate(e) for e in store["evidence"]] + stored
    incoming = scoped(
        [
            consolidate([Finding.model_validate(remap(f, mapping))], all_evidence)[0]
            for f in data["findings"]
        ],
        data.get("resolved_url", data["target"]),
        customer,
    )
    previous = [Finding.model_validate(f) for f in store["findings"]]
    by_evidence = {e.id: e for e in all_evidence}
    preserved = []
    if lifecycle_sources is not None:
        # A standalone connector operation rechecks only its own findings. Cross-source
        # comparisons retain ownership through their connector-prefixed check identity.
        def owns(finding: Finding) -> bool:
            sources = {by_evidence[eid].source for eid in finding.evidence_ids}
            return finding.check_key.partition(":")[0] in lifecycle_sources or (
                bool(sources) and sources <= lifecycle_sources
            )

        preserved = [f for f in previous if not owns(f)]
        previous = [f for f in previous if owns(f)]
    technical = header_predecessors(store, [f for f in previous if f.site or f.component])
    incoming_technical = header_predecessors(store, [f for f in incoming if f.site or f.component])
    other_store = {
        **store,
        "findings": [f.model_dump() for f in previous if not (f.site or f.component)],
    }
    others, id_map = _combine(
        other_store, [f for f in incoming if not (f.site or f.component)], all_evidence, run_id
    )
    store["finding_history"] = other_store["finding_history"]
    outcomes = [CheckOutcome.model_validate(remap(o, mapping)) for o in data.get("outcomes", [])]
    represented = {(o.site, o.check_id, o.component) for o in outcomes}
    module_states = {m["name"]: m["status"] for m in data["modules"]}
    for f in technical + incoming_technical:
        if technical_key(f) in represented:
            continue
        fresh = f in incoming_technical
        sources = {by_evidence[i].source for i in f.evidence_ids}
        status = "fail" if fresh else "skipped"
        if not fresh:
            if f.site not in {
                site_key(data["target"]),
                site_key(data.get("resolved_url", data["target"])),
            }:
                status = "out-of-scope"
            elif f.category not in data["selected_categories"]:
                status = "out-of-scope"
            elif any(module_states.get(source) == "error" for source in sources):
                status = "failed-to-run"
            elif any(module_states.get(source) == "skipped" for source in sources):
                status = "unavailable"
        outcome = observe(
            next(iter(sorted(sources))),
            f.check_key,
            f.category,
            f.component,
            status,
            f.evidence_ids if fresh else [],
            coverage=f.coverage if fresh else [],
            reason="Observed failure" if fresh else "No equivalent retest recorded",
        )
        outcome.site = f.site
        outcomes.append(outcome)
        represented.add(technical_key(f))
    incoming_ids = {technical_key(f): f.id for f in incoming_technical}
    findings = (
        preserved
        + others
        + transition(
            technical, incoming_technical, outcomes, all_evidence, run_id, data["created_at"]
        )
    )
    id_map.update({incoming_ids[technical_key(f)]: f.id for f in incoming_technical})
    ai = remap(data["ai"], mapping)
    ai["recommendations"] = [id_map.get(i, i) for i in ai.get("recommendations", [])]
    ai["roadmap"] = {p: [id_map.get(i, i) for i in ids] for p, ids in ai.get("roadmap", {}).items()}
    run = {
        "run_id": run_id,
        "run_dir": run_name,
        "created_at": data["created_at"],
        "kind": "assessment",
        "status": data["status"],
        "stages": stages,
        "target": data["target"],
        "resolved_url": data.get("resolved_url", data["target"]),
        "outcomes": [o.model_dump() for o in outcomes],
        "website_coverage": remap(data.get("website_coverage", {}), mapping),
        "crawl_assets_excluded": data.get("crawl_assets_excluded", False),
        "selected_categories": data["selected_categories"],
        "assessed_categories": data["assessed_categories"],
        "ai": ai,
        "summary": {
            "evidence": len(evidence),
            "findings": len(incoming),
            "modules": len(data["modules"]),
        },
        "modules": [
            {
                "name": m["name"],
                "status": m["status"],
                "reason": m["reason"],
                "connections": remap(m.get("connections", []), mapping),
            }
            for m in data["modules"]
        ],
        "pages": data["pages"],
        "max_pages": data["max_pages"],
        "limitations": data["limitations"],
        "future": data["future"],
        "report_files": data["report_files"],
    }
    for key in ("domain_email_health", "digital_presence"):
        if key in data:
            run[key] = remap(data[key], mapping)
    store["runs"].append(run)
    findings = supersede_unscoped(store, findings)
    _finish(store, findings, all_evidence, stages if data["status"] != "incomplete" else [])
    from ams_assess.connectors.service import merge_states

    merge_states(store, data.get("connector_results", {}), mapping)
    register.refresh(store)
    save(root, customer, store)
    return store


def add_discovery(
    root: Path, customer: str, customer_name: str, notes: Notes
) -> tuple[dict[str, Any], dict[str, int]]:
    """Ingest discovery notes: mint customer_statement evidence and derive cited findings."""
    with locked(root, customer):
        return _add_discovery(root, customer, customer_name, notes)


def _add_discovery(
    root: Path, customer: str, customer_name: str, notes: Notes
) -> tuple[dict[str, Any], dict[str, int]]:
    store, run_id = _begin(root, customer, customer_name)
    stamp = now()
    discovery: dict[str, Any] = store["discovery"] or {"sections": {}}
    evidence = [Evidence.model_validate(e) for e in store["evidence"]]
    counter = sum(e.source == "discovery" for e in evidence)
    cited: dict[str, list[str]] = {}
    added = 0

    def mint(section: str, fields: dict[str, Any], supersedes: str | None) -> str:
        nonlocal counter, added
        counter += 1
        added += 1
        eid = f"ev-discovery-{run_id}-{counter:03d}"
        data = {"section": section, **fields}
        if supersedes:
            data["supersedes"] = supersedes
        evidence.append(
            Evidence(
                id=eid,
                source="discovery",
                url="customer-discovery-notes",
                description=(
                    f"Customer statement ({DISCOVERY_SECTIONS[section]}): "
                    f"{fields['statement'][:140]}"
                ),
                data=data,
                provenance="customer_statement",
            )
        )
        return eid

    for section, statements in notes.statements().items():
        stored = discovery["sections"].setdefault(section, [])
        # Every decision below reads this snapshot only, never the entries being updated, so the
        # statement -> evidence mapping cannot depend on the order of the notes.
        snapshot = copy.deepcopy(stored)
        fields_list = [st.model_dump() for st in statements]
        plan: list[tuple[str, int]] = [("new", -1)] * len(fields_list)
        claimed: set[int] = set()
        rewritten: set[int] = set()
        for i, fields in enumerate(fields_list):
            if not fields["ref"]:
                continue
            # An explicit ref always takes precedence: it updates its own entry, or creates a
            # new entry bound to that ref. It never falls through to matching by content.
            j = next((k for k, e in enumerate(snapshot) if e.get("ref") == fields["ref"]), None)
            if j is not None:
                changed = not same(snapshot[j], fields)
                plan[i] = ("supersede" if changed else "reuse", j)
                claimed.add(j)
                if changed:
                    rewritten.add(j)
        pending: list[int] = []
        for i, fields in enumerate(fields_list):
            if fields["ref"]:
                continue
            exact = next(
                (k for k, e in enumerate(snapshot) if k not in rewritten and same(e, fields)), None
            )
            if exact is not None:
                plan[i] = ("reuse", exact)
                claimed.add(exact)
            else:
                pending.append(i)
        groups: dict[tuple[str, str, tuple[str, ...]], list[int]] = {}
        for i in pending:
            groups.setdefault(identity(fields_list[i]), []).append(i)
        for base, members in groups.items():
            candidates = [
                k
                for k, e in enumerate(snapshot)
                if not e.get("ref") and identity(e) == base and k not in claimed
            ]
            # Supersede only when unambiguous (one new version, one candidate); else fail closed
            # and keep the new version as a distinct entry rather than rewrite any history.
            if len(members) == 1 and len(candidates) == 1:
                plan[members[0]] = ("supersede", candidates[0])
        for i, fields in enumerate(fields_list):
            kind, j = plan[i]
            if kind == "supersede" and not fields["ref"]:
                # Unreferenced re-ingest only adds detail; omitted fields keep their value.
                fields = {
                    k: v if k == "statement" else (v or snapshot[j].get(k, v))
                    for k, v in fields.items()
                }
                if same(snapshot[j], fields):
                    kind = "reuse"
            if kind == "reuse":
                cited.setdefault(section, []).append(snapshot[j]["evidence_id"])
                continue
            if kind == "supersede":
                eid = mint(section, fields, snapshot[j]["evidence_id"])
                stored[j].update(fields)
                stored[j].update(
                    evidence_id=eid,
                    supersedes=snapshot[j]["evidence_id"],
                    run_id=run_id,
                    ingested_at=stamp,
                )
            else:
                eid = mint(section, fields, None)
                stored.append(
                    {"evidence_id": eid, "run_id": run_id, "ingested_at": stamp, **fields}
                )
            cited.setdefault(section, []).append(eid)
    discovery["ingested_at"] = stamp
    store["discovery"] = discovery
    incoming = []
    for derived in notes.findings:
        ids: list[str] = []
        for cite in derived.cites:
            section, _, index = cite.partition(":")
            # cited[section] follows the notes' own order, so an index selects that statement.
            ids += cited[section][int(index) : int(index) + 1] if index else cited[section]
        key = derived.key or slug(derived.title)
        sev = derived.severity
        incoming.append(
            Finding(
                id=f"discovery-{key}"[:64],
                check_key=key,
                title=derived.title,
                category=DISCOVERY_CATEGORY,
                severity=sev,
                kind=derived.kind,
                confidence="needs_verification",
                evidence_ids=sorted(set(ids)),
                detail=derived.detail,
                recommendation=derived.recommendation,
                likely_cause=derived.likely_cause,
                business_impact=derived.business_impact,
                priority="P1"
                if sev in {"critical", "high"}
                else ("P2" if sev == "medium" else "P3"),
                ams_service=derived.ams_service,
                provenance="customer_statement",
            )
        )
    findings, _ = _combine(store, incoming, evidence, run_id)
    statements = sum(len(v) for v in discovery["sections"].values())
    store["runs"].append(
        {
            "run_id": run_id,
            "run_dir": None,
            "created_at": stamp,
            "kind": "discovery",
            "status": "complete",
            "stages": ["discovery"],
            "ai": dict(NO_AI),
            "summary": {"evidence": added, "findings": len(incoming), "statements": statements},
        }
    )
    _finish(store, findings, evidence, ["discovery"])
    register.refresh(store)
    save(root, customer, store)
    return store, {"statements": statements, "new": added, "findings": len(incoming)}


def _record(store: dict[str, Any], run_id: str, kind: str, summary: dict[str, Any]) -> None:
    """Book a non-assessment change as a run so the next one gets a fresh run ID."""
    store["runs"].append(
        {
            "run_id": run_id,
            "run_dir": None,
            "created_at": now(),
            "kind": kind,
            "status": "complete",
            "stages": [],
            "ai": dict(NO_AI),
            "summary": summary,
        }
    )


def set_profile(
    root: Path, customer: str, changes: dict[str, Any]
) -> tuple[dict[str, Any], str, bool]:
    """Create or update the business identity profile; returns (profile, evidence ID, changed).

    Values are customer-supplied, so the evidence is a customer statement. An unchanged
    profile writes nothing; a changed one mints new evidence and keeps the old evidence.
    """
    with locked(root, customer):
        return _set_profile(root, customer, changes)


def _set_profile(
    root: Path, customer: str, changes: dict[str, Any]
) -> tuple[dict[str, Any], str, bool]:
    store, run_id = _begin(root, customer, customer)
    current = store.get("profile") or {}
    profile = merge_profile(current.get("fields"), changes).model_dump(mode="json")
    if current and current["fields"] == profile:
        return profile, current["evidence_id"], False
    eid = f"ev-profile-{run_id}-001"
    data = {**profile, "supersedes": current.get("evidence_id")}
    store["evidence"].append(
        Evidence(
            id=eid,
            source="profile",
            url="customer-profile",
            description="Customer business profile (customer statement)",
            data=data,
            provenance="customer_statement",
        ).model_dump(mode="json")
    )
    store["profile"] = {"fields": profile, "evidence_id": eid, "updated_at": now()}
    # A new or changed business name becomes the report name; an unchanged one leaves any
    # explicitly given --customer-name alone.
    if profile["name"] and profile["name"] != (current.get("fields") or {}).get("name"):
        store["customer_name"] = profile["name"]
    _record(store, run_id, "profile", {"evidence": 1})
    register.refresh(store)
    save(root, customer, store)
    return profile, eid, True


def add_presence_verifications(
    root: Path, customer: str, records: list[Verification]
) -> dict[str, int]:
    """Import human-verified presence facts as manual_verification evidence.

    Each platform keeps one current verification that supersedes the automated state in
    reports; a changed record mints new evidence and keeps the earlier evidence. Importing the
    same records again changes nothing.
    """
    with locked(root, customer):
        return _add_presence_verifications(root, customer, records)


def _add_presence_verifications(
    root: Path, customer: str, records: list[Verification]
) -> dict[str, int]:
    store, run_id = _begin(root, customer, customer)
    current: dict[str, Any] = store.setdefault("presence_verifications", {})
    added = 0
    for record in records:
        fields = record.model_dump(mode="json")
        previous = current.get(record.platform)
        if previous and {k: previous[k] for k in fields} == fields:
            continue
        added += 1
        eid = f"ev-presence_verification-{run_id}-{added:03d}"
        store["evidence"].append(
            Evidence(
                id=eid,
                source="presence_verification",
                url=record.profile_url or "manual-presence-verification",
                description=f"Manual presence verification: {BY_KEY[record.platform].label} "
                f"- {record.state}",
                data={**fields, "supersedes": previous["evidence_id"] if previous else None},
                provenance="manual_verification",
            ).model_dump(mode="json")
        )
        history = [*previous["history"], previous["evidence_id"]] if previous else []
        current[record.platform] = {
            **fields,
            "evidence_id": eid,
            "history": history,
            "run_id": run_id,
            "ingested_at": now(),
        }
    if added:
        _record(store, run_id, "presence_verification", {"evidence": added})
        register.refresh(store)
        save(root, customer, store)
    return {"entries": len(records), "new": added, "unchanged": len(records) - added}


def register_store(root: Path, customer: str) -> dict[str, Any]:
    """Load and backfill existing evidence under the same lock as every other writer."""
    with locked(root, customer, create=False):
        current = load(root, customer)
        if current is None:
            raise ValueError("No assessment store for this customer")
        if register.refresh(current):
            save(root, customer, current)
        return current


def set_register(
    root: Path,
    customer: str,
    platform: str,
    changes: dict[str, Any],
    site: str | None = None,
    location: str = "",
) -> dict[str, Any]:
    """Shared CLI/UI validation, history and atomic persistence boundary."""
    checked = register.validate_edit(changes)
    with locked(root, customer, create=False):
        current = load(root, customer)
        if current is None:
            raise ValueError("No assessment store for this customer")
        register.refresh(current)
        site, location = register.edit_scope(current, platform, site, location)
        entry = register.manual_edit(current, platform, site, location, checked)
        # Manual register evidence also supports reports before any technical run.
        run_id = f"r{max((int(r['run_id'][1:]) for r in current['runs']), default=0) + 1}"
        eid = f"ev-register-{run_id}-001"
        current["evidence"].append(
            Evidence(
                id=eid,
                source="register",
                url=checked.get("evidence_url") or "manual-register-update",
                description=f"Register update: {entry['label']}",
                data=checked,
                provenance="customer_statement"
                if checked.get("method") == "customer statement"
                else "manual_verification",
            ).model_dump(mode="json")
        )
        _record(current, run_id, "register", {"evidence": 1})
        save(root, customer, current)
        return entry


def remove_register(
    root: Path,
    customer: str,
    platform: str,
    site: str,
    location: str = "",
    who: str = "local operator",
) -> None:
    """Remove exactly one scoped row, retaining its full snapshot and all evidence/history."""
    site, location, who = (
        register.site_name(site),
        register.safe_text(location),
        register.safe_text(who),
    )
    with locked(root, customer, create=False):
        current = load(root, customer)
        if current is None:
            raise ValueError("No assessment store for this customer")
        register.refresh(current)
        reg = register.registry(current)
        matches = [
            e
            for e in reg["entries"]
            if (e["platform"], site_key(e["site"]), e["location"])
            == (platform, site_key(site), location)
        ]
        if len(matches) != 1:
            raise ValueError("No unique entry at that exact platform/site/location scope")
        entry = matches[0]
        default_integration = register.default_integration(platform)
        if (
            entry["integration"] != default_integration
            or not entry["human_fields"]
            or not entry["evidence"]
            or any(
                e.get("method") not in register.METHODS
                or e.get("basis") not in {"manual verification", "customer statement"}
                for e in entry["evidence"]
            )
        ):
            raise ValueError(
                "Cannot remove engine-observed entries or integration state; "
                "only purely manual entries can be removed"
            )
        reg["entries"].remove(entry)
        register.record(current, entry, None, who, "register remove")
        save(root, customer, current)


def repair_presence_nap(data: dict[str, Any]) -> None:
    """Display stored NAP rows decoded; rebuild the NAP finding detail from the same rows.

    Only the latest presence summary per website is shown, and only a finding last observed
    in that run is rebuilt, so the detail always describes the rows printed beside it.
    """
    summaries = [data.get("digital_presence"), *data.get("digital_presence_sites", [])]
    # Copies: the composed report data must never rewrite the stored records.
    data["findings"] = [dict(f) for f in data["findings"]]
    for summary in [s for s in summaries if s and s.get("discovery", {}).get("nap")]:
        summary["discovery"] = repair_discovery_nap(summary["discovery"])
        for finding in data["findings"]:
            platform = finding.get("check_key", "").removeprefix(
                "platform:external-nap-inconsistent:"
            )
            if (
                platform == finding.get("check_key")
                or finding.get("last_run_id") != summary.get("run_id")
                or finding.get("site") != site_key(summary.get("target", ""))
            ):
                continue
            differs = [
                n
                for n in summary["discovery"]["nap"]
                if n["key"] == platform and n["result"] == "differs"
            ]
            if differs:
                finding["detail"] = nap_detail(differs)
            elif any(n["key"] == platform for n in summary["discovery"]["nap"]):
                # This is a corrected interpretation of stored evidence, not a fresh retest.
                finding["lifecycle_status"] = "resolved"
                finding["resolution"] = {
                    "reason": "Decoded stored phone values match; no NAP difference remains",
                    "run_id": summary["run_id"],
                }


def report_data(
    store: dict[str, Any],
) -> tuple[dict[str, Any], list[Evidence], dict[str, Any] | None]:
    """Compose the dict the report template expects from the store, whatever stages ran."""
    repair_axe_reviews(store)
    runs = [r for r in store["runs"] if r["kind"] == "assessment"]
    evidence = [Evidence.model_validate(e) for e in store["evidence"]]
    if not evidence:
        raise ValueError("Customer store holds no evidence to report")
    live = [r for r in runs if r["status"] != "incomplete"]
    assessed = sorted({c for r in live for c in r["assessed_categories"]})
    selected = sorted({c for r in runs for c in r["selected_categories"]})
    ai_runs = [r for r in live if r["ai"].get("status") == "ok"]
    ai: dict[str, Any] = dict(ai_runs[-1]["ai"]) if ai_runs else dict(NO_AI)
    section_runs = {key: run for run in ai_runs for key in run["ai"].get("sections", {})}
    if ai_runs:
        # Narrative sections accumulate across runs; the newest run wins per category.
        ai["sections"] = {key: run["ai"]["sections"][key] for key, run in section_runs.items()}
    elif live:
        ai = dict(live[-1]["ai"])
    # Results stay attributed to the website and run that produced them.
    latest_modules: dict[tuple[str, str], dict[str, Any]] = {}
    targets: dict[str, list[str]] = {}
    for run in runs:
        targets.setdefault(run["target"], []).append(run["run_id"])
        for module in run["modules"]:
            latest_modules[(run["target"], module["name"])] = {
                **module,
                "target": run["target"],
                "created_at": run.get("created_at") or "",
                "run_id": run["run_id"],
            }
    limitations = [
        line
        for line in dict.fromkeys(x for r in runs for x in r["limitations"])
        if not any(
            line == f"{c}: no successful evidence collection; category was not assessed"
            for c in assessed
        )
    ]
    if "discovery" in store["stages_assessed"] and not live:
        limitations.append(
            "No technical assessment has been run; findings rest on customer statements only"
        )

    def latest(key: str, default: Any) -> Any:
        return next((r[key] for r in reversed(runs) if r.get(key)), default)

    def attributed(run: dict[str, Any], key: str) -> dict[str, Any]:
        return {**run[key], "target": run["target"], "run_id": run["run_id"]}

    stages = {
        "public": "public technical assessment",
        "connected": "connected evidence",
        "discovery": "business discovery",
    }
    from ams_assess.connectors.service import rows as connector_rows

    data: dict[str, Any] = {
        "connector_rows": connector_rows(store, include_unconfigured=False),
        "customer_name": store["customer_name"],
        "profile": store.get("profile"),
        "systems_register": store.get("systems_register", {}).get("entries", []),
        "reported_sources": [
            {"source": r["kind"], "created_at": r["created_at"], "run_id": r["run_id"]}
            for r in store["runs"]
            if r["kind"] != "assessment"
        ],
        "coverage": [
            {
                "target": r["target"],
                "resolved_url": r.get("resolved_url", r["target"]),
                "run_id": r["run_id"],
                "created_at": r.get("created_at") or "",
                "pages": len(r["pages"]),
                "max_pages": r["max_pages"],
                "crawl_assets_excluded": r.get("crawl_assets_excluded", False),
                "status": r["status"],
            }
            for r in runs
        ],
        "website_coverage_runs": [
            {**r["website_coverage"], "run_id": r["run_id"], "target": r["target"]}
            for r in runs
            if r.get("website_coverage")
        ],
        "created_at": store["updated_at"],
        "target": latest("target", ""),
        "resolved_url": latest("resolved_url", latest("target", "")),
        "targets": [{"target": t, "run_ids": ids} for t, ids in targets.items() if t],
        "selected_categories": selected,
        "assessed_categories": assessed,
        "stages": [stages[s] for s in store["stages_assessed"]],
        "max_pages": latest("max_pages", 0),
        "crawl_assets_excluded": next(
            (r.get("crawl_assets_excluded", False) for r in reversed(runs) if r.get("pages")),
            False,
        ),
        "pages": latest("pages", []),
        "pages_target": next((r["target"] for r in reversed(runs) if r.get("pages")), ""),
        "modules": list(latest_modules.values()),
        "ai": ai,
        "findings": store["findings"],
        "finding_history": store.get("finding_history", []),
        "needs_review": store.get("needs_review", []),
        "limitations": limitations,
        "future": latest("future", []),
    }
    for key in ("domain_email_health", "digital_presence"):
        # The newest summary per website, each labelled with its website and run.
        sites = {r["target"]: attributed(r, key) for r in runs if key in r}
        found = next((r for r in reversed(runs) if key in r), None)
        if found is not None:
            data[key] = attributed(found, key)
            data[f"{key}_sites"] = list(sites.values())
    repair_presence_nap(data)
    verified = store.get("presence_verifications") or {}
    if verified and data.get("digital_presence", {}).get("discovery"):
        # Manual verification supersedes the automated state; the automated state stays shown.
        data["digital_presence"] = apply_verifications(data["digital_presence"], verified)
        data["digital_presence_sites"] = [
            data["digital_presence"] if p["run_id"] == data["digital_presence"]["run_id"] else p
            for p in data.get("digital_presence_sites", [])
        ]
        data["findings"] = current_findings(data["findings"], data["digital_presence"], verified)
    elif verified:
        limitations.append(
            "Manual presence verifications are recorded (see the evidence index) but no "
            "presence discovery run exists to compare them with"
        )
    actionable = {
        f["id"] for f in data["findings"] if f.get("lifecycle_status", "current") == "current"
    }
    review_ids = {
        fid
        for row in data["needs_review"]
        for fid in [row.get("finding_id"), *row.get("finding_ids", [])]
    }
    actionable -= review_ids
    active_eids = {
        eid for f in data["findings"] if f["id"] in actionable for eid in f["evidence_ids"]
    }
    inactive_eids = {
        eid
        for f in [*data["findings"], *data["finding_history"]]
        if f.get("lifecycle_status") == "resolved" or f.get("status") == "superseded"
        for eid in f["evidence_ids"]
    } - active_eids

    def retain_supported(text: str) -> str:
        refs = set(CITATIONS.findall(text))
        if refs and refs <= inactive_eids:
            return ""
        return " ".join(
            sentence
            for sentence in sentences(text)
            if not ((refs := set(CITATIONS.findall(sentence))) and refs <= inactive_eids)
        )

    ai["executive_summary"] = retain_supported(ai.get("executive_summary", ""))
    ai["sections"] = {
        key: retained
        for key, text in ai.get("sections", {}).items()
        if (retained := retain_supported(text))
    }
    if ai_runs:

        def dated_narrative(text: str, run: dict[str, Any]) -> str:
            if not text:
                return ""
            date = run.get("created_at")
            when = f"on {str(date)[:10]}" if date else "(date not recorded)"
            return f"As observed in run {run['run_id']} {when}. {text}"

        ai["executive_summary"] = dated_narrative(ai["executive_summary"], ai_runs[-1])
        ai["sections"] = {
            key: dated_narrative(text, section_runs[key]) for key, text in ai["sections"].items()
        }
    ai["recommendations"] = [i for i in ai.get("recommendations", []) if i in actionable]
    ai["roadmap"] = {
        p: [i for i in ids if i in actionable] for p, ids in ai.get("roadmap", {}).items()
    }
    sections = [
        {"title": title, "statements": store["discovery"]["sections"][key]}
        for key, title in DISCOVERY_SECTIONS.items()
        if store["discovery"].get("sections", {}).get(key)
    ]
    discovery = {"sections": sections} if sections else None
    return data, evidence, discovery
