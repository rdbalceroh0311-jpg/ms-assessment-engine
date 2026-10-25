"""Per-customer onboarding projection over the existing store, register and connector state.

Read-only: nothing here persists, loads credentials or contacts a provider. Customers keep
ownership and recovery methods and grant named, least-privilege delegated access; AMS never
asks for passwords.
"""

from typing import Any

from ams_assess.connectors.service import rows as connector_rows

SEQUENCE = (
    (
        "customer",
        "Create customer",
        "ams-assess customer --customer {c} --name '<name>' --website <url>",
    ),
    (
        "discover",
        "Discover systems",
        "ams-assess authorise --customer {c} --authorised-by '<who>' --date <YYYY-MM-DD> "
        "--scope <host> --scope connectors, then run --customer {c} --target <url> --no-ai "
        "--confirm-authorisation",
    ),
    (
        "ownership",
        "Confirm ownership",
        "ams-assess register set --customer {c} --platform <key> --ownership customer_confirmed",
    ),
    (
        "access",
        "Request access",
        "ams-assess register set --customer {c} --platform <key> --access requested",
    ),
    (
        "authenticate",
        "Authenticate (CLI)",
        "ams-assess connect <connector> auth --credential-ref <name> ...",
    ),
    (
        "bind",
        "Bind resource",
        "ams-assess connect <connector> list-resources --credential-ref "
        "<name>, then bind --customer {c} --resource <id>",
    ),
    ("test", "Test", "ams-assess connect <connector> test --customer {c} --confirm-authorisation"),
    (
        "collect",
        "Collect",
        "ams-assess connect <connector> collect --customer {c} --confirm-authorisation",
    ),
    ("report", "Report", "ams-assess report --customer {c}"),
)

# Register rows describing the same platform: the connector key plus public catalogue keys.
PLATFORM_KEYS = {
    "gsc": ("gsc",),
    "bing_wmt": ("bing_wmt",),
    "ga4": ("ga4", "ga4_tag"),
    "gbp": ("gbp", "google_business_profile"),
    "workspace": ("workspace", "google_workspace"),
    "m365": ("m365",),
    "wp_cli": ("wp_cli", "wordpress"),
}
INSTRUCTIONS = {
    "gsc": "In Search Console > Settings > Users and permissions, add the AMS account as a "
    "Restricted (or Full) user of the verified property. The customer stays Owner.",
    "bing_wmt": "In Bing Webmaster Tools > Settings > User management, add the AMS account as a "
    "Read-only user of your verified site. The customer stays Administrator.",
    "ga4": "In GA4 Admin > Property access management, grant the AMS account the Viewer role on "
    "the property. The customer keeps Administrator access.",
    "gbp": "In Google Business Profile > Business Profile settings > People and access, add the "
    "AMS account as a Manager. The customer stays primary Owner.",
    "workspace": "A Workspace super admin approves the AMS read-only scopes for the AMS app, or "
    "assigns the AMS account a custom read-only admin role. No super admin role for AMS.",
    "m365": "A Microsoft Entra admin grants tenant consent to the AMS read-only app (delegated "
    "Graph read permissions only). No Global Administrator role for AMS.",
    "wp_cli": "Provide restricted read-only SSH access for an AMS key on any host (SiteGround, "
    "cPanel, Kinsta, WP Engine, VPS): host, port, user, WordPress path and the pinned host key. "
    "If the host cannot provide this, mark the internal WordPress assessment unavailable.",
}
MANUAL = {
    "bing_places": ("Bing Places / Bing Maps", "Bing Places for Business"),
    "apple_maps": ("Apple Business / Apple Maps", "Apple Business Connect"),
}
ENGINE = {
    "connected": "Connected",
    "connected_partial": "Connected (partial coverage)",
    "not_configured": "Not connected",
    "supported": "Not connected",
    "credential_missing": "Not connected — authenticate in the CLI",
    "credential_expired": "Credential expired or revoked",
    "permission_required": "Permission required",
    "licence_required": "Provider licence required",
    "rate_limited": "Rate limited — retry later",
    "not_found": "Bound resource not found",
    "provider_unavailable": "Provider unavailable — retry later",
    "api_not_enabled": "Blocked — provider API not enabled",
    "approval_required": "Blocked — provider API approval pending",
    "failed": "Connection test failed",
    "unsupported": "Unsupported",
}
PRESENCE = (
    "publicly_observed",
    "customer_reported",
    "confirmed_absent",
    "not_found_in_bounded_searches",
    "unknown",
)
OWNERSHIP = ("customer_confirmed", "authorised_account_evidence", "unconfirmed")
ACCESS = ("granted", "requested", "denied", "not_requested", "unknown")


def best(entries: list[dict[str, Any]], field: str, order: tuple[str, ...]) -> str:
    values = [e[field] for e in entries if e.get(field) in order]
    return min(values, key=order.index) if values else order[-1]


def label(value: str) -> str:
    return value.replace("_", " ")


def manual_workflow(presence: str, service: str) -> str:
    if presence in {"publicly_observed", "customer_reported"}:
        return "Found: record the listing URL and check name, address, phone, hours and website."
    if presence == "confirmed_absent":
        return (
            f"Missing and eligible: the customer creates or claims it via {service}, keeping "
            "ownership; then add AMS as a delegated user if needed."
        )
    return (
        "Not found in bounded checks: flag for customer confirmation; "
        "searches do not prove absence."
    )


def rows(current: dict[str, Any]) -> list[dict[str, Any]]:
    entries = current.get("systems_register", {}).get("entries", [])
    reported = bool(current.get("report_revisions"))
    output = []
    for row in connector_rows(current):
        key = row["key"]
        state = current.get("connectors", {}).get(key, {})
        related = [e for e in entries if e["platform"] in PLATFORM_KEYS.get(key, (key,))]
        own = [e for e in related if e["platform"] == key]
        # Only engine-recorded authenticated reads count; legacy GSC runs set the register.
        status = state.get("status") or (own[0]["integration"] if own else "not_configured")
        connected = status in {"connected", "connected_partial"} and bool(
            state.get("last_successful_test") or (not state and own)
        )
        presence = best(related, "presence", PRESENCE)
        ownership = best(related, "ownership", OWNERSHIP)
        access = best(related, "ams_access", ACCESS)
        collected = state.get("last_successful_collection", "")
        done = {
            "customer": True,
            "discover": presence != "unknown" or connected,
            "ownership": ownership != "unconfirmed",
            "access": access in {"requested", "granted"},
            "authenticate": bool(state.get("credential_ref")),
            "bind": bool(state.get("resource_id")),
            "test": connected,
            "collect": bool(collected),
            "report": reported
            and bool(collected)
            and max(r.get("created_at", "") for r in current["report_revisions"]) >= collected,
        }
        step = next((name for name, title, _ in SEQUENCE if not done[name]), "")
        output.append(
            {
                "key": key,
                "label": row["label"],
                "presence": label(presence),
                "ownership": label(ownership),
                "access": label(access),
                "engine": ENGINE.get(status, label(status)),
                "status": status,
                "connected": connected,
                "resource": row["resource"] or "Not bound",
                "credential_ref": row["credential_ref"],
                "last_successful_collection": collected or "Never",
                "missing_permission": state.get("detail", "")
                if status in {"permission_required", "credential_expired", "approval_required"}
                else "",
                "next_action": row["next_action"],
                "next_step": dict((n, t) for n, t, _ in SEQUENCE).get(step, "Complete"),
                "instructions": INSTRUCTIONS.get(key, ""),
            }
        )
    for key, (platform, service) in MANUAL.items():
        related = [e for e in entries if e["platform"] == key]
        presence = best(related, "presence", PRESENCE)
        output.append(
            {
                "key": key,
                "label": platform,
                "presence": label(presence),
                "ownership": label(best(related, "ownership", OWNERSHIP)),
                "access": label(best(related, "ams_access", ACCESS)),
                "engine": "Manual public workflow — no connector",
                "status": "unsupported",
                "connected": False,
                "resource": "Not applicable",
                "credential_ref": "",
                "last_successful_collection": "Not applicable",
                "missing_permission": "",
                "next_action": manual_workflow(presence, service),
                "next_step": "Confirm presence" if presence == "unknown" else "Record and check",
                "instructions": manual_workflow(presence, service),
            }
        )
    return output


def sequence(customer: str) -> list[dict[str, str]]:
    return [
        {"key": key, "title": title, "command": command.format(c=customer)}
        for key, title, command in SEQUENCE
    ]
