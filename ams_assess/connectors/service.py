"""Shared CLI/UI/run operations. Customer state contains references, never credentials."""

from __future__ import annotations

import copy
import fcntl
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ams_assess import register, store
from ams_assess.connectors import REGISTRY
from ams_assess.connectors.auth import config_dir, reference_name
from ams_assess.connectors.connected import connected_evidence
from ams_assess.connectors.core import (
    ConnectorContext,
    ConnectorError,
    Endpoint,
    EvidenceView,
    Period,
    readonly,
)
from ams_assess.connectors.http import ReadClient
from ams_assess.connectors.privacy import redact
from ams_assess.lifecycle import site_key
from ams_assess.models import CUSTOMER_RE, Evidence, ModuleResult, customer_path


class BindingError(ValueError):
    """Safe, actionable binding errors with no untrusted inputs."""


def connector(key: str):
    if key not in REGISTRY:
        raise ValueError("Unknown connector")
    return REGISTRY[key]


@contextmanager
def binding_lock(root: Path):
    """Serialize cross-customer binding checks before acquiring a customer lock."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".connector-bindings.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Connector bindings are busy; retry later") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def set_binding(
    root: Path,
    customer: str,
    key: str,
    credential_ref: str,
    resource_id: str = "",
    *,
    force: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    spec = connector(key)
    customer_path(root, customer)
    reference_name(credential_ref)
    resource = (
        spec.normalize_resource(register.safe_text(resource_id, identifier=True))
        if resource_id
        else ""
    )
    reason = redact(register.safe_text(reason))
    if force and not reason:
        raise BindingError("--force requires a recorded non-secret --reason")
    with binding_lock(root):
        conflicts = []
        for directory in root.iterdir():
            if directory.name == customer or not CUSTOMER_RE.fullmatch(directory.name):
                continue
            if directory.is_symlink():
                raise ValueError("Cannot verify binding isolation through a symlink")
            if not directory.is_dir() or not (directory / store.STORE_FILE).exists():
                continue
            for other_key, binding in store.connector_bindings(root, directory.name).items():
                other_spec = REGISTRY.get(other_key)
                if (
                    resource
                    and other_spec
                    and (other_spec.provider, other_spec.resource_type, binding.get("resource_id"))
                    == (spec.provider, spec.resource_type, resource)
                ):
                    conflicts.append(directory.name)
        if conflicts and not force:
            raise ValueError("Resource is bound to a different customer; --force requires --reason")
        with store.locked(root, customer):
            current = store.load(root, customer) or store.new_store(customer, customer)
            before = copy.deepcopy(current.setdefault("connectors", {}).get(key))
            binding = {
                "credential_ref": credential_ref,
                "resource_id": resource,
                "revision": uuid.uuid4().hex,
                "bound_at": store.now(),
                "status": "not_configured",
                "detail": "Run a bound-resource connection test",
            }
            current["connectors"][key] = binding
            current.setdefault("connector_history", []).append(
                {
                    "at": store.now(),
                    "action": "bind" if resource else "credential-reference",
                    "connector": key,
                    "old": before,
                    "new": copy.deepcopy(binding),
                    "forced": force,
                    "reason": reason,
                    "other_customers": conflicts,
                }
            )
            project_register(current, key, binding)
            store.save(root, customer, current)
            return binding


def list_resources(
    key: str, ref: str, *, directory: Path | None = None, transport=None
) -> Endpoint:
    spec = connector(key)
    auth = spec.authenticate(spec, ref, directory=directory or config_dir())
    http = ReadClient(spec, auth, transport=transport)
    try:
        result = spec.list_resources(ConnectorContext(http, "", Period.make()))
        result.data = redact(result.data, auth.secrets)
        return result
    finally:
        http.close()


def latest_public_evidence(
    current: dict[str, Any],
    incoming: list[Evidence] | None = None,
) -> list[Evidence]:
    """Only the latest sample per source/site can support a current tag comparison."""
    evidence = [Evidence.model_validate(e) for e in current.get("evidence", [])]
    evidence = [e for e in evidence if e.source not in {"profile", "discovery", *REGISTRY}]
    fresh = [e for e in incoming or [] if e.source not in {"profile", "discovery", *REGISTRY}]
    replaced = {(e.source, site_key(e.url)) for e in fresh}
    evidence = [e for e in evidence if (e.source, site_key(e.url)) not in replaced]
    latest: dict[tuple[str, str], int] = {}
    stamped = []
    for item in evidence:
        match = re.search(r"-r(\d+)-", item.id)
        revision = int(match[1]) if match else 0
        scope = (item.source, site_key(item.url))
        latest[scope] = max(latest.get(scope, -1), revision)
        stamped.append((item, scope, revision))
    return [item for item, scope, revision in stamped if latest[scope] == revision] + fresh


def prepare(
    current: dict[str, Any],
    key: str,
    *,
    collect: bool = False,
    days: int = 28,
    start: str = "",
    end: str = "",
    directory: Path | None = None,
    transport=None,
    public_evidence: list[Evidence] | None = None,
    public_findings: list[dict] | None = None,
    seconds: float | None = None,
    fallback_websites: list[str] | None = None,
) -> tuple[ModuleResult, dict[str, Any]]:
    spec = connector(key)
    period = Period.make(days, start, end)
    binding = copy.deepcopy(current.get("connectors", {}).get(key, {}))
    result = ModuleResult(name=key, collection_started=True)
    state = {**binding, "tested_at": store.now(), "operation": "collect" if collect else "test"}
    profile = current.get("profile") or {}
    profile_evidence = next(
        (
            Evidence.model_validate(e)
            for e in current.get("evidence", [])
            if e["id"] == profile.get("evidence_id") and e["source"] == "profile"
        ),
        None,
    )
    context = ConnectorContext(
        None,
        binding.get("resource_id", ""),
        period,
        customer=current.get("customer_id", ""),
        profile=readonly(profile.get("fields", {})),
        profile_evidence_id=profile.get("evidence_id", ""),
        profile_evidence=EvidenceView.of(profile_evidence) if profile_evidence else None,
        websites=list(profile.get("fields", {}).get("websites", []))
        or fallback_websites
        or list(dict.fromkeys(r["target"] for r in current.get("runs", []) if r.get("target"))),
        public_evidence=tuple(
            EvidenceView.of(e) for e in latest_public_evidence(current, public_evidence)
        ),
        connected_evidence=connected_evidence(current, key, {*REGISTRY, "gsc"}, public_evidence),
        public_findings=public_findings
        if public_findings is not None
        else current.get("findings", []),
    )
    secrets: tuple[str, ...] = ()
    http = None
    try:
        if not binding.get("resource_id") or not binding.get("credential_ref"):
            raise ConnectorError(
                "not_configured", "Select a credential reference and bind a resource"
            )
        spec.normalize_resource(context.resource)
        auth = spec.authenticate(
            spec, binding["credential_ref"], directory=directory or config_dir()
        )
        secrets = auth.secrets
        http = context.http = ReadClient(spec, auth, transport=transport, seconds=seconds)
        tested = spec.test(context)
        if tested.status == "connected":
            state["last_successful_test"] = tested.collected_at
            state["permission_evidence"] = tested.data.get(
                "permission_evidence", "Authenticated bound-resource read succeeded"
            )
            if collect:
                spec.collect(context)
    except ConnectorError as exc:
        context.endpoints.append(Endpoint("connection", "", exc.status, detail=exc.detail))
    except Exception:
        context.endpoints.append(Endpoint("connection", "", "failed", detail="Connector failed"))
    finally:
        if http:
            http.close()
    for endpoint in context.endpoints:
        endpoint.data = redact(endpoint.data, secrets)
        endpoint.evidence_id = result.add_evidence(
            "",
            f"{spec.label}: {endpoint.key}",
            endpoint_evidence(key, context, endpoint),
        )
    if collect:
        try:
            for analyser in spec.analysers:
                analyser(context, result)
        except Exception:
            endpoint = Endpoint("analysis", "", "failed", detail="Connector analysis failed")
            context.endpoints.append(endpoint)
            endpoint.evidence_id = result.add_evidence(
                "", "Analysis unavailable", endpoint_evidence(key, context, endpoint)
            )
    # Analysis may append cross-references to endpoint payloads. Redact before every sink.
    for evidence, endpoint in zip(result.evidence, context.endpoints, strict=True):
        evidence.data = redact(endpoint_evidence(key, context, endpoint), secrets)
    result.findings = [
        type(f).model_validate(redact(f.model_dump(), secrets)) for f in result.findings
    ]
    tested_ok = any(e.key != "analysis" and e.status == "connected" for e in context.endpoints)
    problems = [e for e in context.endpoints if not e.complete]
    state["status"] = (
        "connected_partial"
        if tested_ok and problems
        else "connected"
        if tested_ok
        else context.endpoints[0].status
    )
    state["period"] = context.period.json()
    state["coverage"] = [
        {
            "endpoint": e.key,
            "status": e.status,
            "rows": e.rows,
            "truncated": e.truncated,
            "detail": e.detail,
            "evidence_id": e.evidence_id,
        }
        for e in context.endpoints
    ]
    groups: dict[str, list[str]] = {}
    for endpoint in context.endpoints:
        label = endpoint.status + (" (coverage capped or sampled)" if endpoint.truncated else "")
        groups.setdefault(label, []).append(endpoint.key)
    state["detail"] = "; ".join(f"{', '.join(names)}: {status}" for status, names in groups.items())
    explanations = list(dict.fromkeys(e.detail for e in context.endpoints if e.detail))
    if explanations:
        state["detail"] += ". " + "; ".join(explanations)
    state["evidence_ids"] = [e.id for e in result.evidence]
    if collect and tested_ok:
        state["last_collection"] = store.now()
        if not problems:
            state["last_successful_collection"] = state["last_collection"]
    result.status = "ok" if tested_ok else "error" if state["status"] == "failed" else "skipped"
    result.reason = state["detail"]
    return ModuleResult.model_validate(redact(result.model_dump(), secrets)), redact(state, secrets)


def endpoint_evidence(key: str, ctx: ConnectorContext, endpoint: Endpoint) -> dict[str, Any]:
    return {
        "connector": key,
        "resource_id": ctx.resource,
        "collected_at": endpoint.collected_at,
        "period": ctx.period.json(),
        "endpoint": endpoint.endpoint,
        "operation": endpoint.key,
        "coverage": {"rows_returned": endpoint.rows, "truncated": endpoint.truncated},
        "status": endpoint.status,
        "detail": endpoint.detail,
        "data": endpoint.data,
        # AI receives only coverage aggregates, never provider payloads or inventories.
        "ai_aggregate": {
            "status": endpoint.status,
            "rows_returned": endpoint.rows,
            "truncated": endpoint.truncated,
        },
    }


def merge_states(current: dict[str, Any], states: dict[str, Any], mapping: dict[str, str]) -> None:
    for key, state in states.items():
        before = copy.deepcopy(current.get("connectors", {}).get(key, {}))
        if state.get("revision") != before.get("revision"):
            raise BindingError("Binding changed during collection; discard the result and retry")
        merged = store.remap(state, mapping)
        current.setdefault("connectors", {})[key] = merged
        current.setdefault("connector_history", []).append(
            {
                "at": store.now(),
                "action": state["operation"],
                "connector": key,
                "old": before,
                "new": copy.deepcopy(merged),
            }
        )
        project_register(current, key, merged)


def project_register(current: dict[str, Any], key: str, state: dict[str, Any]) -> None:
    # An explicit binding is customer-scoped. Update existing site rows too so no stale
    # public-derived row can disagree with the engine's current connection status.
    spec = connector(key)
    register.PLATFORMS.setdefault(key, spec.label)
    entries = [e for e in register.registry(current)["entries"] if e["platform"] == key]
    if not entries:
        entries = [register.entry_for(current, key, "")]
    for entry in entries:
        register.apply(
            current,
            entry,
            {"integration": state.get("status", "not_configured")},
            {
                "reference": ", ".join(state.get("evidence_ids", [])),
                "identifier": state.get("resource_id", ""),
                "method": "automated",
                "date": state.get("tested_at", state.get("bound_at", store.now())),
                "site": entry["site"],
                "location": entry["location"],
                "tested": state.get("permission_evidence", "")
                if state.get("status") in {"connected", "connected_partial"}
                else state.get("detail", ""),
                "note": state.get("detail", ""),
                "basis": "authorised connection"
                if state.get("status") in {"connected", "connected_partial"}
                else "authorised connection attempt",
            },
            "connector:" + key,
        )


def execute(root: Path, customer: str, key: str, **kwargs) -> dict[str, Any]:
    current = store.load(root, customer) or store.new_store(customer, customer)
    result, state = prepare(current, key, **kwargs)
    spec = connector(key)
    target = ((current.get("profile") or {}).get("fields", {}).get("websites") or [""])[0]
    data = {
        "customer_id": customer,
        "customer_name": current["customer_name"],
        "target": target,
        "created_at": store.now(),
        "status": "complete" if state["status"] == "connected" else "partial",
        "selected_categories": sorted(spec.categories),
        "assessed_categories": sorted(spec.categories) if result.status == "ok" else [],
        "findings": [f.model_dump() for f in result.findings],
        "outcomes": [o.model_dump() for o in result.outcomes],
        "modules": [result.model_dump()],
        "connector_results": {key: state},
        "ai": {"status": "skipped"},
        "pages": [],
        "max_pages": 0,
        "limitations": [],
        "future": [],
        "report_files": [],
    }
    saved = store.merge_run(
        root,
        customer,
        current["customer_name"],
        data,
        result.evidence,
        "connector-" + uuid.uuid4().hex,
        ["connected"] if state["status"] in {"connected", "connected_partial"} else [],
        lifecycle_sources=frozenset({key}),
    )
    return saved["connectors"][key]


def rows(current: dict[str, Any], *, include_unconfigured: bool = True) -> list[dict[str, Any]]:
    output = []
    for key, spec in REGISTRY.items():
        state = current.get("connectors", {}).get(key, {})
        if not state and not include_unconfigured:
            continue
        access = sorted(
            {
                e["ams_access"]
                for e in current.get("systems_register", {}).get("entries", [])
                if e["platform"] == key
            }
        )
        status = state.get("status", "not_configured")
        customer_arg = " --customer " + current.get("customer_id", "<customer>")
        next_action = {
            "not_configured": f"Bind a resource, then ams-assess connect {key} test{customer_arg}",
            "credential_missing": spec.auth_command,
            "credential_expired": "Credential expired/revoked — re-authorise with "
            + spec.auth_command,
            "permission_required": "Request "
            + ", ".join(spec.scopes)
            + " and a resource reader role",
            "licence_required": "Confirm the required provider licence for this endpoint",
            "rate_limited": "Retry later after the provider rate limit resets",
            "not_found": "Check the explicit resource ID and delegated access",
            "api_not_enabled": "Enable the provider API in the OAuth project",
            "approval_required": "Request provider API approval",
            "provider_unavailable": "Retry the bounded read later",
            "failed": "Review the connector implementation and rerun the test",
            "connected_partial": "Review endpoint coverage and retry unavailable reads",
            "connected": f"Review coverage; ams-assess connect {key} collect{customer_arg}",
        }[status]
        output.append(
            {
                "key": key,
                "label": spec.label,
                "resource": state.get("resource_id", ""),
                "credential_ref": state.get("credential_ref", ""),
                "status": status,
                "access": ", ".join(access) or "not_requested",
                "detail": state.get("detail", ""),
                "last_successful_collection": state.get("last_successful_collection", "Never"),
                "last_collection": state.get("last_collection", "Never"),
                "period": state.get("period", {}),
                "coverage": state.get("coverage", []),
                "coverage_text": "; ".join(
                    f"{e['endpoint']}: {e['status']}, {e['rows']} rows"
                    + (", truncated/sampled" if e["truncated"] else "")
                    for e in state.get("coverage", [])
                )
                or "Not collected",
                "next_action": next_action,
            }
        )
    return output


def project_presence(summary: dict[str, Any], connector_rows: list[dict[str, Any]]) -> None:
    """Overlay current engine status without changing the historical public observations."""
    states = {row["key"]: row for row in connector_rows}
    for row in summary.get("platforms", []):
        if row["key"] in states:
            row["connected"] = states[row["key"]]["status"]
    for row in summary.get("readiness", []):
        if row["key"] in states:
            state = states[row["key"]]
            row["access"] = state["status"]
            row["next_step"] = state["next_action"]
            row["insights"] = row["insights"].replace(
                ". No connector implemented; manual review only.", ""
            )
