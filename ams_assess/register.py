"""Systems register projections and validated edits; persistence belongs to store.py.

Public observations, customer control, delegated access and tested engine connections are
independent. No connector or credential handling lives here.
"""

import copy
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from ams_assess.lifecycle import site_key
from ams_assess.modules.presence import EMAIL_PLATFORMS, SIGNATURES
from ams_assess.platforms import BY_KEY, NA, prioritised

SIGNAL_KEYS = {k: f"{k}_tag" if k in BY_KEY else k for k in SIGNATURES}
PLATFORMS = {
    **{k: p.label for k, p in BY_KEY.items()},
    **{SIGNAL_KEYS[k]: v[0] for k, v in SIGNATURES.items()},
    **{k: v[0] for k, v in EMAIL_PLATFORMS.items()},
    "hosting": "Hosting",
    "dns_provider": "DNS provider",
    "domain_registrar": "Domain registrar",
    "wp_cli": "WP-CLI (TOOLING; requires server access, not an account)",
}
REQUIRED_SYSTEMS = (
    "gsc",
    "ga4",
    "bing_wmt",
    "gtm",
    "clarity",
    "google_workspace",
    "m365",
    "hosting",
    "dns_provider",
    "domain_registrar",
    "wordpress",
    "wp_cli",
)
CHOICES = {
    "presence": (
        "publicly_observed",
        "customer_reported",
        "confirmed_absent",
        "not_found_in_bounded_searches",
        "unknown",
    ),
    "ownership": ("customer_confirmed", "authorised_account_evidence", "unconfirmed"),
    "ams_access": ("not_requested", "requested", "granted", "denied", "unknown"),
}
EDIT_FIELDS = {*CHOICES, "next_action", "responsible"}
METHODS = ("manual", "customer statement")
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY|\bBearer\s+\S+|"
    r"\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"client[_ -]?secret|authorization|credential|token)[\"']?\s*[=:]\s*\S+|"
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|ya29\.[\w.-]+)|"
    r"\beyJ[\w-]+\.[\w-]+\.[\w-]+",
    re.I,
)
SECRET_KEY = re.compile(
    r"password|passwd|secret|token|credential|authorization|api.?key|signature|^(key|auth|code|sig)$",
    re.I,
)


def safe_text(value: str, *, identifier: bool = False) -> str:
    """Reject likely credentials without echoing the submitted value in errors."""
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Register text must be at most 2000 characters")
    decoded = value
    for _ in range(4):
        decoded = unquote(decoded)
    if any(ord(c) < 32 and c not in "\n\t" for c in decoded) or SECRET.search(decoded):
        raise ValueError("Secret-looking values are not allowed in the register")
    for candidate in re.findall(r"https?://[^\s<>]+", decoded, re.I):
        try:
            parts = urlsplit(candidate)
            if (
                parts.username
                or parts.password
                or any(SECRET_KEY.search(k) for k, _ in parse_qsl(parts.query))
                or SECRET_KEY.search(parts.fragment)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("Credential-bearing URLs are not allowed") from None
    # Long opaque values are not useful account identifiers; known public IDs have a
    # recognisable prefix/URL and remain accepted. This is deliberately conservative.
    if identifier and re.fullmatch(r"[A-Za-z0-9_+/=-]{32,}", decoded):
        raise ValueError("Opaque secret-looking identifier is not allowed")
    return value.strip()


def site_name(value: str) -> str:
    value = safe_text(value)
    if not value:
        return ""
    parts = urlsplit(value if "://" in value else "https://" + value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ValueError("Site must be an http(s) website without credentials")
    return f"{parts.scheme}://{parts.netloc.lower()}/"


def validate_edit(changes: dict[str, Any]) -> dict[str, str]:
    allowed = EDIT_FIELDS | {
        "evidence_url",
        "identifier",
        "method",
        "note",
        "tested",
        "who",
        "date",
    }
    if set(changes) - allowed:
        raise ValueError("Unsupported register field; integration is engine-controlled")
    checked = {k: safe_text(v, identifier=k == "identifier") for k, v in changes.items()}
    for field, options in CHOICES.items():
        if field in checked and checked[field] not in options:
            raise ValueError(f"Invalid {field}; choose one of: {', '.join(options)}")
    if checked.get("method", "manual") not in METHODS:
        raise ValueError("Method must be manual or customer statement")
    url = checked.get("evidence_url", "")
    if url:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or any(c.isspace() for c in url)
        ):
            raise ValueError("Evidence URL must be http(s) without credentials or whitespace")
    if checked.get("date"):
        try:
            datetime.fromisoformat(checked["date"])
        except ValueError:
            raise ValueError("Date must be ISO 8601") from None
    if not any(
        checked.get(k) for k in EDIT_FIELDS | {"tested", "note", "identifier", "evidence_url"}
    ):
        raise ValueError("Supply a status, evidence or verification update")
    return checked


def action(entry: dict[str, Any]) -> tuple[str, str]:
    if entry["platform"] == "wp_cli":
        return (
            "Confirm a need for WP-CLI tooling and authorised least-privilege server access.",
            "AMS",
        )
    if entry["presence"] == "confirmed_absent":
        return (
            "After confirming no existing account or recoverable profile, agree whether creation "
            "is justified; the customer owns the account and recovery methods.",
            "customer",
        )
    if entry["presence"] in {"unknown", "not_found_in_bounded_searches"}:
        return (
            "Find an existing account/profile with the customer; "
            "bounded searches do not prove absence.",
            "customer",
        )
    if entry["ownership"] == "unconfirmed":
        return (
            "Confirm customer control and customer-owned recovery methods "
            "for the existing account.",
            "customer",
        )
    if entry["ams_access"] == "denied":
        return (
            "Review the delegated-access decision with the customer; "
            "agree an authorised next step.",
            "customer",
        )
    if entry["ams_access"] in {"unknown", "not_requested"}:
        return (
            "Request named, revocable, least-privilege delegated access; never request passwords.",
            "AMS",
        )
    if entry["ams_access"] == "requested":
        return (
            "Customer to grant agreed delegated access while retaining "
            "ownership and recovery methods.",
            "customer",
        )
    if entry["integration"] == "connected":
        return (
            "Review the recorded connection sample and agree any further operational verification.",
            "AMS",
        )
    if entry["integration"] == "unsupported":
        return "Arrange an authorised manual check; this engine has no connector.", "AMS"
    return (
        "Run an authorised connection test in the CLI; access granted does not mean connected.",
        "AMS",
    )


def registry(store: dict[str, Any]) -> dict[str, Any]:
    return store.setdefault("systems_register", {"entries": [], "history": [], "processed": []})


def entry_for(
    store: dict[str, Any], platform: str, site: str, location: str = ""
) -> dict[str, Any]:
    if platform not in PLATFORMS:
        raise ValueError("Unknown platform; use a register platform key")
    site, location = site_name(site), safe_text(location)
    reg = registry(store)
    for entry in reg["entries"]:
        if (entry["platform"], site_key(entry["site"]), entry["location"]) == (
            platform,
            site_key(site),
            location,
        ):
            return entry
    entry = {
        "platform": platform,
        "label": PLATFORMS[platform],
        "site": site,
        "location": location,
        "kind": "tooling" if platform == "wp_cli" else "system",
        "presence": "unknown",
        "ownership": "unconfirmed",
        "ams_access": "not_requested",
        "integration": "supported" if platform == "gsc" else "unsupported",
        "operational_verification": {},
        "evidence": [],
        "identifiers": [],
        "human_fields": [],
        "conflicts": [],
        "needs_review": False,
    }
    entry["next_action"], entry["responsible"] = action(entry)
    reg["entries"].append(entry)
    record(store, None, entry, "engine", "initialise")
    return entry


def edit_scope(store, platform, site, location):
    """Prefer an existing exact scope, then the sole unambiguous platform entry."""
    site = site_name(site) if site is not None else None
    location = safe_text(location)
    entries = [e for e in registry(store)["entries"] if e["platform"] == platform]
    candidates = [e for e in entries if not site or site_key(e["site"]) == site_key(site)]
    exact = [
        e
        for e in candidates
        if site is not None and site_key(e["site"]) == site_key(site) and e["location"] == location
    ]
    located = [e for e in candidates if location and e["location"] == location]
    if len(entries) > 1 and site and location and not (exact or located):
        # Multi-scope registers can still add an explicitly named site/location pair.
        return site, location
    matches = exact or located or candidates
    if len(matches) == 1:
        return matches[0]["site"], matches[0]["location"]
    if len(matches) > 1:
        if len({site_key(e["site"]) for e in matches}) > 1:
            raise ValueError("Several websites match this platform; specify --site and --location")
        raise ValueError("Several entries match this platform; specify --site and --location")
    if site is None:
        sites = list(dict.fromkeys(e["site"] for e in registry(store)["entries"] if e["site"]))
        if len(sites) > 1:
            raise ValueError("Several websites are recorded; specify --site")
        site = sites[0] if sites else ""
    return site, location


def record(store, old, entry, who, source):
    if old != entry:
        registry(store)["history"].append(
            {
                "who": who,
                "when": datetime.now(UTC).isoformat(),
                "source": source,
                "old": old,
                "new": copy.deepcopy(entry),
            }
        )


def apply(store, entry, updates, observation, source, who="engine", human=False):
    old = copy.deepcopy(entry)
    for key, value in updates.items():
        if not human and key in entry["human_fields"]:
            if value != entry[key] and value != "unknown":
                conflict = {
                    "field": key,
                    "retained": entry[key],
                    "observed": value,
                    "source": source,
                    "date": observation["date"],
                }
                if conflict not in entry["conflicts"]:
                    entry["conflicts"].append(conflict)
            continue
        entry[key] = value
        if human and key not in entry["human_fields"]:
            entry["human_fields"].append(key)
        if human:
            entry["conflicts"] = [c for c in entry["conflicts"] if c["field"] != key]
    entry["needs_review"] = bool(entry["conflicts"])
    if observation and observation not in entry["evidence"]:
        entry["evidence"].append(observation)
        if observation.get("identifier") and observation["identifier"] not in entry["identifiers"]:
            entry["identifiers"].append(observation["identifier"])
        if observation.get("tested") and (
            human or "operational_verification" not in entry["human_fields"]
        ):
            entry["operational_verification"] = {
                k: observation[k] for k in ("tested", "date", "method", "reference")
            }
            if human and "operational_verification" not in entry["human_fields"]:
                entry["human_fields"].append("operational_verification")
    todo, responsible = action(entry)
    if "next_action" not in entry["human_fields"]:
        entry["next_action"] = todo
    if "responsible" not in entry["human_fields"]:
        entry["responsible"] = responsible
    record(store, old, entry, who, source)


def manual_edit(store, platform, site, location, changes, *, source="register"):
    changes = validate_edit(changes)
    entry = entry_for(store, platform, site, location)
    method = changes.get("method", "manual")
    stamp = changes.get("date") or datetime.now(UTC).isoformat()
    observation = {
        "reference": changes.get("evidence_url", "") or source,
        "identifier": changes.get("identifier", ""),
        "method": method,
        "date": stamp,
        "site": entry["site"],
        "location": entry["location"],
        "note": changes.get("note", ""),
        "tested": changes.get("tested", ""),
        "basis": "customer statement" if method == "customer statement" else "manual verification",
    }
    apply(
        store,
        entry,
        {k: v for k, v in changes.items() if k in EDIT_FIELDS},
        observation,
        source,
        changes.get("who") or "local operator",
        human=True,
    )
    return entry


def refresh(store: dict[str, Any]) -> bool:
    """Replay only unseen evidence sources. Human fields always win; conflicts remain visible."""
    before = copy.deepcopy(store.get("systems_register"))
    reg = registry(store)
    profile = store.get("profile", {}).get("fields", {})
    profile_source = store.get("profile", {}).get("evidence_id")
    if profile_source and profile_source not in reg["processed"]:
        keys = [
            p.key
            for p, level in prioritised(
                profile.get("industry") or None, profile.get("buying_model") or None
            )
            if level != NA
        ] + list(REQUIRED_SYSTEMS)
        for site in profile.get("websites") or [""]:
            for key in keys:
                entry_for(store, key, site)
        reg["processed"].append(profile_source)
    for run in store.get("runs", []):
        if run.get("kind") != "assessment" or run["run_id"] in reg["processed"]:
            continue
        refresh_run(store, run, profile)
        reg["processed"].append(run["run_id"])
    refresh_statements(store, profile)
    for platform, item in store.get("presence_verifications", {}).items():
        source = item["evidence_id"]
        if source in reg["processed"]:
            continue
        sites = list(
            dict.fromkeys(site_name(r["target"]) for r in store.get("runs", []) if r.get("target"))
        ) or profile.get("websites", [])
        # Existing presence-verify records lack site/location. Never spread one verification
        # across several websites: keep an explicitly unscoped customer entry instead.
        site = sites[0] if len(sites) == 1 else ""
        states = {
            "Found — likely match": "publicly_observed",
            "Found — needs verification": "publicly_observed",
            "Inconsistent": "publicly_observed",
            "Not found in searches": "not_found_in_bounded_searches",
        }
        try:
            manual_edit(
                store,
                platform,
                site,
                "",
                {
                    "presence": states.get(item["state"], "unknown"),
                    "evidence_url": item["profile_url"],
                    "date": item["date"],
                    "who": item["verified_by"],
                    "note": item["note"],
                    "tested": "Manual public profile verification: " + item["state"],
                },
                source=source,
            )
        except ValueError:
            # Legacy evidence can contain arbitrary notes. Do not copy credential-like text.
            pass
        reg["processed"].append(source)
    return before != reg


def refresh_statements(store, profile):
    """Only explicit structured systems-in-use names; never infer inventory from prose."""
    reg = registry(store)
    aliases = {
        re.sub(r"\W", "", name).lower(): key
        for key, label in PLATFORMS.items()
        for name in (key, label)
    }
    aliases.update(
        {
            "searchconsole": "gsc",
            "googleanalytics": "ga4",
            "ga4": "ga4",
            "microsoft365": "m365",
            "office365": "m365",
            "wpcli": "wp_cli",
        }
    )
    sites = list(
        dict.fromkeys(site_name(r["target"]) for r in store.get("runs", []) if r.get("target"))
    ) or profile.get("websites", [])
    site = sites[0] if len(sites) == 1 else ""
    for item in store.get("discovery", {}).get("sections", {}).get("systems_in_use", []):
        source = item["evidence_id"]
        if source in reg["processed"]:
            continue
        run = next((r for r in store["runs"] if f"-{r['run_id']}-" in source), {})
        for system in item.get("systems", []):
            key = aliases.get(re.sub(r"\W", "", system).lower())
            if not key:
                continue
            try:
                note = safe_text(item["statement"])
            except ValueError:
                continue
            entry = entry_for(store, key, site)
            observation = {
                "reference": source,
                "identifier": "",
                "method": "customer statement",
                "date": run.get("created_at", store.get("updated_at", "unknown")),
                "site": site,
                "location": "",
                "tested": "",
                "note": note,
                "basis": "customer statement",
            }
            apply(
                store,
                entry,
                {"presence": "customer_reported"},
                observation,
                source,
                who="customer (discovery)",
                human="presence" not in entry["human_fields"],
            )
        reg["processed"].append(source)


def snapshot(data, evidence, previous=None):
    """Per-run report view, preserving human fields without committing unfinished runs."""
    current = {"evidence": [e.model_dump(mode="json") for e in evidence]}
    if previous:
        current["systems_register"] = copy.deepcopy(registry(previous))
    refresh_run(
        current,
        {**data, "run_id": "current-run", "register_snapshot": True},
        (data.get("profile") or {}).get("fields", {}),
    )
    return registry(current)["entries"]


def refresh_run(store, run, profile):
    site = site_name(run["target"])
    source, stamp = run["run_id"], run["created_at"]
    relevant = [
        p.key
        for p, level in prioritised(
            profile.get("industry") or None, profile.get("buying_model") or None
        )
        if level != NA
    ]
    for key in dict.fromkeys([*relevant, *REQUIRED_SYSTEMS]):
        entry_for(store, key, site)

    def observe(platform, updates, refs=(), urls=(), ids=(), tested="", basis="public evidence"):
        if platform not in PLATFORMS:
            return
        entry = entry_for(store, platform, site)
        values = {
            "reference": "; ".join([*refs, *urls]),
            "identifier": "; ".join(ids),
            "method": "automated",
            "date": stamp,
            "site": site,
            "location": "",
            "tested": tested,
            "note": "",
            "basis": basis,
        }
        try:
            for key, value in values.items():
                safe_text(value, identifier=key == "identifier")
        except ValueError:
            return
        apply(store, entry, updates, values, source)

    presence = run.get("digital_presence", {})
    for row in presence.get("platforms", []):
        if row.get("evidence_ids") and (
            row["key"] in REQUIRED_SYSTEMS or row["status"] == "Detected"
        ):
            observe(
                SIGNAL_KEYS.get(row["key"], row["key"]),
                {"presence": "publicly_observed" if row["status"] == "Detected" else "unknown"},
                row["evidence_ids"],
                ids=row.get("ids", []),
                tested="Public platform signal check: " + row["status"],
            )
    for row in presence.get("listings", []):
        if row.get("urls"):
            observe(
                row["key"],
                {"presence": "publicly_observed"},
                row["evidence_ids"],
                row["urls"],
                tested="Website links to public profile; control not tested",
            )
    for row in presence.get("discovery", {}).get("platforms", []):
        if row.get("candidates") or row["state"] == "Not found in searches":
            observe(
                row["key"],
                {
                    "presence": "publicly_observed"
                    if row.get("candidates")
                    else "not_found_in_bounded_searches"
                },
                row["evidence_ids"],
                [c["url"] for c in row.get("candidates", [])],
                tested=(
                    "Bounded public profile search: "
                    if not row.get("candidates")
                    else "Public profile candidates from existing evidence: "
                )
                + row["state"],
            )
    evidence = {e["id"]: e for e in store.get("evidence", [])}
    for ev in evidence.values():
        if (f"-{source}-" not in ev["id"] and not run.get("register_snapshot")) or not isinstance(
            ev.get("data"), dict
        ):
            continue
        infra = ev["data"].get("infrastructure", {})
        for field, key in (
            ("registrar", "domain_registrar"),
            ("dns_provider", "dns_provider"),
            ("website_provider", "hosting"),
        ):
            claim = infra.get(field, {})
            if claim.get("value"):
                identifiers = claim["value"] if field == "dns_provider" else []
                if field == "registrar":
                    identifiers = [name for item in claim["value"] for name in item["names"]]
                if field == "website_provider":
                    identifiers = [item["provider"] for item in claim["value"]]
                observe(
                    key,
                    {"presence": "publicly_observed"},
                    [ev["id"]],
                    ids=identifiers,
                    tested="Public infrastructure attribution: " + claim.get("detail", ""),
                )
        for route in infra.get("email_provider", {}).get("value", []) or []:
            label = route.get("provider", "")
            key = {"Google Workspace": "google_workspace", "Microsoft 365": "m365"}.get(label)
            if key:
                observe(
                    key,
                    {"presence": "publicly_observed"},
                    [ev["id"]],
                    tested="Public MX routing clue; not a full software inventory",
                )
    for module in run.get("modules", []):
        if module["name"] != "gsc":
            continue
        for connection in module.get("connections", []):
            refs = [
                eid
                for eid in connection.get("evidence_ids", [])
                if eid in evidence and evidence[eid]["source"] == "gsc"
            ]
            properties = [evidence[eid]["data"].get("property", "") for eid in refs]
            if (
                module["status"] == "ok"
                and connection["status"] == "Connected successfully"
                and refs
                and all(properties)
            ):
                observe(
                    "gsc",
                    {"integration": "connected"},
                    refs,
                    ids=properties,
                    tested="Authorised GSC property access, queries/pages and sitemap list",
                    basis="authorised connection",
                )
            elif connection["status"] == "Permission required" or module["status"] == "error":
                observe(
                    "gsc",
                    {
                        "integration": "permission_required"
                        if connection["status"] == "Permission required"
                        else "failed"
                    },
                    tested="GSC connection attempt: " + connection["status"],
                    basis="authorised connection attempt",
                )
