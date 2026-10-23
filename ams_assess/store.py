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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ams_assess.consolidate import consolidate
from ams_assess.discovery import DISCOVERY_SECTIONS, Notes, slug
from ams_assess.models import (
    DISCOVERY_CATEGORY,
    Evidence,
    Finding,
    customer_path,
)
from ams_assess.platforms import BY_KEY
from ams_assess.profile import (
    Verification,
    apply_verifications,
    current_findings,
    merge_profile,
)

SCHEMA_VERSION = 2
STORE_FILE = "assessment.json"
LOCK_FILE = ".assessment.lock"
LOCK_TIMEOUT = 30.0
STAGES = ("public", "connected", "discovery")
EVIDENCE_ID = re.compile(r"ev-[a-z_]+-\d+")
LOCAL_ID = re.compile(r"^ev-([a-z_]+)-(\d+)$")
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
    }


def load(root: Path, customer: str) -> dict[str, Any] | None:
    """Read only this customer's store; None when it does not exist yet."""
    path = store_file(root, customer)
    if not path.is_file():
        return None
    store = json.loads(path.read_text())
    if store.get("schema_version") != SCHEMA_VERSION or store.get("customer_id") != customer:
        raise ValueError("Unsupported or mismatched customer store")
    return store


def save(root: Path, customer: str, store: dict[str, Any]) -> None:
    """Atomic write: a crash leaves either the old or the new store, never a partial file."""
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
        yield
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
) -> dict[str, Any]:
    """Merge one finished assessment run (its results.json dict + evidence) into the store."""
    with locked(root, customer):
        return _merge_run(root, customer, customer_name, data, evidence, run_name, stages)


def _merge_run(
    root: Path,
    customer: str,
    customer_name: str,
    data: dict[str, Any],
    evidence: list[Evidence],
    run_name: str,
    stages: list[str],
) -> dict[str, Any]:
    store, run_id = _begin(root, customer, customer_name)
    if any(r["run_dir"] == run_name for r in store["runs"]):
        return store
    mapping = {e.id: namespaced(e.id, run_id) for e in evidence}
    stored = [Evidence.model_validate(remap(e.model_dump(mode="json"), mapping)) for e in evidence]
    incoming = [Finding.model_validate(remap(f, mapping)) for f in data["findings"]]
    all_evidence = [Evidence.model_validate(e) for e in store["evidence"]] + stored
    findings, id_map = _combine(store, incoming, all_evidence, run_id)
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
    _finish(store, findings, all_evidence, stages if data["status"] != "incomplete" else [])
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
        save(root, customer, store)
    return {"entries": len(records), "new": added, "unchanged": len(records) - added}


def report_data(
    store: dict[str, Any],
) -> tuple[dict[str, Any], list[Evidence], dict[str, Any] | None]:
    """Compose the dict the report template expects from the store, whatever stages ran."""
    runs = [r for r in store["runs"] if r["kind"] == "assessment"]
    evidence = [Evidence.model_validate(e) for e in store["evidence"]]
    if not evidence:
        raise ValueError("Customer store holds no evidence to report")
    live = [r for r in runs if r["status"] != "incomplete"]
    assessed = sorted({c for r in live for c in r["assessed_categories"]})
    selected = sorted({c for r in runs for c in r["selected_categories"]})
    ai_runs = [r for r in live if r["ai"].get("status") == "ok"]
    ai: dict[str, Any] = dict(ai_runs[-1]["ai"]) if ai_runs else dict(NO_AI)
    if ai_runs:
        # Narrative sections accumulate across runs; the newest run wins per category.
        ai["sections"] = {k: v for r in ai_runs for k, v in r["ai"].get("sections", {}).items()}
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
                "created_at": run["created_at"],
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
    data: dict[str, Any] = {
        "customer_name": store["customer_name"],
        "profile": store.get("profile"),
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
                "created_at": r["created_at"],
                "pages": len(r["pages"]),
                "max_pages": r["max_pages"],
                "crawl_assets_excluded": r.get("crawl_assets_excluded", False),
                "status": r["status"],
            }
            for r in runs
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
    sections = [
        {"title": title, "statements": store["discovery"]["sections"][key]}
        for key, title in DISCOVERY_SECTIONS.items()
        if store["discovery"].get("sections", {}).get(key)
    ]
    discovery = {"sections": sections} if sections else None
    return data, evidence, discovery
