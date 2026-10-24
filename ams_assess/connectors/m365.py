"""Bound, read-only Microsoft Graph v1.0 snapshots. See README for verified API sources.

MSAL is a declared dependency, loaded lazily when Microsoft authentication is requested.
Only this module handles Microsoft authentication; all Graph IO uses the shared ReadClient.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from requests.exceptions import RequestException

from ams_assess.connectors.auth import (
    atomic_private,
    config_dir,
    config_lock,
    private_file,
    reference_name,
    references,
)
from ams_assess.connectors.core import (
    AuthMaterial,
    Connector,
    ConnectorContext,
    ConnectorError,
    Endpoint,
    ErrorClassification,
    ReadOperation,
)
from ams_assess.connectors.privacy import private_logs
from ams_assess.lifecycle import observe
from ams_assess.models import ModuleResult

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = (
    "Organization.Read.All",
    "Domain.Read.All",
    "User.Read.All",
    "RoleManagement.Read.Directory",
    "AuditLog.Read.All",
    "Policy.Read.All",
)
PATHS = {
    "organization": "/organization",
    "domains": "/domains",
    "users": "/users",
    "licences": "/subscribedSkus",
    "role_assignments": "/roleManagement/directory/roleAssignments",
    "mfa_registration": "/reports/authenticationMethods/userRegistrationDetails",
    "security_defaults": "/policies/identitySecurityDefaultsEnforcementPolicy",
    "conditional_access": "/identity/conditionalAccess/policies",
}
PERMISSIONS = {
    "organization": ("Organization.Read.All", "Directory Readers or Global Reader"),
    "domains": ("Domain.Read.All", "Directory Readers or Security Reader"),
    "users": ("User.Read.All", "a customer-tenant member account"),
    "licences": ("Organization.Read.All", "Directory Readers or Global Reader"),
    "role_assignments": ("RoleManagement.Read.Directory", "Directory Readers or Global Reader"),
    "mfa_registration": ("AuditLog.Read.All", "Security Reader, Reports Reader or Global Reader"),
    "security_defaults": ("Policy.Read.All", "delegated tenant access"),
    "conditional_access": ("Policy.Read.All", "Security Reader or Global Reader"),
}
REAUTHORIZE = (
    "Credential expired/revoked — re-authorise with ams-assess connect m365 auth "
    "--credential-ref <name> --tenant-id <tenant-uuid> --client-id <application-uuid>"
)

# Privileged badges in Microsoft's built-in role reference, verified 2026-10-24.
# v1.0 unifiedRoleDefinition has no isPrivileged property. Custom roles stay unclassified.
# https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/permissions-reference
PRIVILEGED_ROLES = frozenset(
    {
        "db506228-d27e-4b7d-95e5-295956d6615f",  # Agent ID Administrator
        "d2562ede-74db-457e-a7b6-544e236ebb61",  # AI Administrator
        "1fe13547-53f6-408d-ac04-7f8eed167b38",  # AI Reader
        "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3",  # Application Administrator
        "cf1c38e5-3621-4004-a7cb-879624dced7c",  # Application Developer
        "ecb2c6bf-0ab6-418e-bd87-7986f8d63bbe",  # Attribute Provisioning Administrator
        "422218e4-db15-4ef9-bbe0-8afb41546d79",  # Attribute Provisioning Reader
        "c4e39bd9-1100-46d3-8c65-fb160da0071f",  # Authentication Administrator
        "25a516ed-2fa0-40ea-a2d0-12923a21473a",  # Authentication Extensibility Administrator
        "0b00bede-4072-4d22-b441-e7df02a1ef63",  # Authentication Extensibility Password Admin
        "aaf43236-0c0d-4d5f-883a-6955382ac081",  # B2C IEF Keyset Administrator
        "158c047a-c907-4556-b7ef-446551a6b5f7",  # Cloud Application Administrator
        "7698a772-787b-4ac8-901f-60d6b08affd2",  # Cloud Device Administrator
        "b1be1c3e-b65d-4f19-8427-f6fa0d97feb9",  # Conditional Access Administrator
        "9360feb5-f418-4baa-8175-e2a00bac4301",  # Directory Writers
        "8329153b-31d0-4727-b945-745eb3bc5f31",  # Domain Name Administrator
        "58f930cc-fcf4-4152-852c-1d7dbf502139",  # Entra SOC Identity Responder
        "be2f45a1-457d-42af-a067-6ec1fa63bc45",  # External Identity Provider Administrator
        "62e90394-69f5-4237-9190-012177145e10",  # Global Administrator
        "f2ef992c-3afb-46b9-b7cf-a126ee74c451",  # Global Reader
        "729827e3-9c14-49f7-bb1b-9608f156bbb8",  # Helpdesk Administrator
        "8ac3fc64-6eca-42ea-9e69-59f4c7b60eb2",  # Hybrid Identity Administrator
        "45d8d3c5-c802-45c6-b32a-1d70b5e1e86e",  # Identity Governance Administrator
        "3a2c62db-5318-420d-8d74-23affee5d9d5",  # Intune Administrator
        "59d46f88-662b-457b-bceb-5c3809e5908f",  # Lifecycle Workflows Administrator
        "4ba39ca4-527c-499a-b93d-d9b492c50246",  # Partner Tier1 Support
        "e00e864a-17c5-4a4b-9c06-f5b95a8d5bd8",  # Partner Tier2 Support
        "966707d0-3269-4727-9be2-8c3a10f19b9d",  # Password Administrator
        "7be44c8a-adaf-4e2a-84d6-ab2649e08a13",  # Privileged Authentication Administrator
        "e8611ab8-c189-46e8-94e1-60213ab1f814",  # Privileged Role Administrator
        "194ae4cb-b126-40b2-bd5b-6091b380977d",  # Security Administrator
        "5f2222b1-57c3-48ba-8ad5-d4759f1fde6f",  # Security Operator
        "5d6b6bb7-de71-4623-b4af-96380a352509",  # Security Reader
        "1981f584-96e9-4a6f-95b0-f522373f8fae",  # Tenant Governance Administrator
        "fe930be7-5e62-47db-91af-98c3a49a38b1",  # User Administrator
    }
)


def _uuid(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError("Select the explicit tenant UUID, not a domain, common or organizations")
    return str(UUID(value))


def normalize_resource(value: str) -> str:
    # Named resource IDs fit the shared register's public-identifier validation.
    return "tenant:" + _uuid(value.removeprefix("tenant:"))


def _app_config(data: Any) -> dict[str, str]:
    return {key: _uuid(data[key]) for key in ("client_id", "tenant_id")}


def _sdk():
    try:
        return importlib.import_module("msal")
    except ImportError:
        raise ConnectorError(
            "credential_missing", "Microsoft auth support is missing; run uv sync"
        ) from None


def _application(config: dict[str, str], serialized: str = ""):
    msal = _sdk()
    cache = msal.SerializableTokenCache()
    if serialized:
        cache.deserialize(serialized)
    app = msal.PublicClientApplication(
        config["client_id"],
        authority="https://login.microsoftonline.com/" + config["tenant_id"],
        token_cache=cache,
        timeout=10,
        instance_discovery=False,
        enable_pii_log=False,
    )
    return app, cache


def _validate_token(result: Any, tenant: str, *, interactive: bool = False) -> str:
    if not isinstance(result, dict) or not result.get("access_token"):
        error = result.get("error") if isinstance(result, dict) else None
        if error in {"temporarily_unavailable", "server_error"}:
            raise ConnectorError(
                "provider_unavailable", "Microsoft sign-in unavailable; retry later"
            )
        if error in {"invalid_scope", "consent_required", "access_denied"}:
            raise ConnectorError("permission_required", "Consent required: " + ", ".join(SCOPES))
        raise ConnectorError("credential_expired", REAUTHORIZE)
    scopes = {
        s.removeprefix("https://graph.microsoft.com/") for s in result.get("scope", "").split()
    } - {"openid", "profile", "offline_access"}
    if scopes != set(SCOPES):
        raise ConnectorError(
            "permission_required",
            "Use a dedicated app with exactly these Graph scopes: " + ", ".join(SCOPES),
        )
    claims = result.get("id_token_claims", {})
    if (interactive or claims) and claims.get("tid", "").lower() != tenant:
        raise ConnectorError(
            "permission_required", "Sign-in tenant does not match app configuration"
        )
    return result["access_token"]


def _entry(connector: Connector, path: Path) -> dict[str, str]:
    return {
        "provider": connector.provider,
        "connector": connector.key,
        "auth_kind": connector.auth_kind,
        "file": str(path.absolute()),
    }


@private_logs()
def authorize(
    connector: Connector,
    ref: str,
    *,
    tenant_id: str,
    client_id: str,
    directory: Path | None = None,
) -> None:
    """Native framework device-code auth; only the short-lived user challenge is displayed."""
    directory = directory or config_dir()
    reference_name(ref)
    try:
        config = _app_config({"tenant_id": tenant_id, "client_id": client_id})
        path = directory / f"m365-{ref}-token.json"
        entry = _entry(connector, path)
        with config_lock(directory):
            private_file(directory, path)
            existing = references(directory).get(ref)
            if existing is not None and existing != entry:
                raise ValueError
            if path.exists() and _app_config(json.loads(path.read_text())) != config:
                raise ValueError  # A different tenant/app requires a new reference.
        app, cache = _application(config)
        flow = app.initiate_device_flow(scopes=list(connector.scopes))
        if not flow.get("device_code"):
            _validate_token(flow, config["tenant_id"], interactive=True)
        if not re.fullmatch(r"[A-Z0-9-]{6,20}", flow.get("user_code", "")):
            raise ValueError
        # MSAL honours expires_at while polling. Never print the flow or provider message:
        # it contains the private device_code used to redeem tokens.
        flow["expires_at"] = min(flow["expires_at"], time.time() + 180)
        print(
            "Sign in at https://microsoft.com/devicelogin with code " + flow["user_code"],
            flush=True,
        )
        result = app.acquire_token_by_device_flow(flow)
        _validate_token(result, config["tenant_id"], interactive=True)
        accounts = [a for a in app.get_accounts() if a.get("realm") == config["tenant_id"]]
        if len(accounts) != 1:
            raise ValueError
        with config_lock(directory):
            private_file(directory, path)
            refs = references(directory)
            if ref in refs and refs[ref] != entry:
                raise ValueError
            if path.exists() and _app_config(json.loads(path.read_text())) != config:
                raise ValueError
            atomic_private(
                path,
                {
                    **config,
                    "account_id": accounts[0]["home_account_id"],
                    "scopes": list(connector.scopes),
                    "cache": cache.serialize(),
                },
            )
            refs[ref] = entry
            atomic_private(directory / "connectors.json", refs)
    except ConnectorError:
        raise
    except RequestException:
        raise ConnectorError(
            "provider_unavailable", "Microsoft sign-in unavailable; retry later"
        ) from None
    except Exception:
        raise ConnectorError(
            "credential_missing",
            "Authorisation failed; check the app ID, tenant, public client setting and consent. "
            "Use a new reference when changing tenant or app.",
        ) from None


@private_logs()
def authenticate(
    connector: Connector,
    ref: str,
    *,
    directory: Path | None = None,
) -> AuthMaterial:
    directory = directory or config_dir()
    reference_name(ref)
    try:
        with config_lock(directory):
            entry = references(directory).get(ref, {})
            if (entry.get("provider"), entry.get("connector"), entry.get("auth_kind")) != (
                connector.provider,
                connector.key,
                connector.auth_kind,
            ) or not entry.get("file"):
                raise ValueError
            path = private_file(directory, Path(entry["file"]))
            data = json.loads(path.read_text())
            config = _app_config(data)
            if set(data["scopes"]) != set(connector.scopes):
                raise ConnectorError("permission_required", "Re-authorise with exact m365 scopes")
            app, cache = _application(config, data["cache"])
            accounts = [
                a
                for a in app.get_accounts()
                if (
                    a.get("home_account_id") == data["account_id"]
                    and a.get("realm") == config["tenant_id"]
                )
            ]
            if len(accounts) != 1:
                raise ConnectorError("credential_expired", REAUTHORIZE)
            result = app.acquire_token_silent_with_error(list(connector.scopes), accounts[0])
            # Persist refresh/revocation cache changes even if fresh consent is necessary.
            if cache.has_state_changed:
                atomic_private(path, {**data, "cache": cache.serialize()})
            token = _validate_token(result, config["tenant_id"])
            return AuthMaterial(
                {"Authorization": "Bearer " + token},
                (token,),
                private_data={"tenant_id": config["tenant_id"]},
            )
    except ConnectorError:
        raise
    except RequestException:
        raise ConnectorError(
            "provider_unavailable", "Microsoft sign-in unavailable; retry later"
        ) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise ConnectorError(
            "credential_missing", "Microsoft credential file missing or invalid"
        ) from None
    except Exception:
        raise ConnectorError(
            "provider_unavailable", "Microsoft sign-in unavailable; retry later"
        ) from None


def classify_error(status_code: int, body: Mapping[str, Any]) -> ErrorClassification | None:
    # The framework supplies a bounded/redacted body. Never persist provider diagnostics.
    error = body.get("error", {})
    error = error if isinstance(error, Mapping) else {}
    code = str(error.get("code", "")).lower()
    message = str(error.get("message", "")).lower()
    if status_code in {400, 403} and (
        code
        in {
            "authentication_requestfromnonpremiumtenantorb2ctenant",
            "license_required",
            "licenserequired",
            "tenantnotlicensed",
        }
        or re.search(
            r"(?:requires?|missing|without|not have|does not have|not licensed).{0,80}"
            r"(?:premium|p1|p2|licen[cs]e)",
            message,
        )
        or re.search(r"licen[cs]es?.{0,30}(?:required|missing)", message)
        or "premium license" in message
        or "premium licence" in message
    ):
        return ErrorClassification(
            "licence_required",
            "This Graph endpoint requires Microsoft Entra ID P1/P2 licensing; "
            "confirm entitlement. Unavailable evidence says nothing about MFA protection.",
        )
    if status_code == 403:
        return ErrorClassification(
            "permission_required",
            "Grant the endpoint's delegated Graph permission (README) "
            "and supported reader role: Directory Readers plus Security Reader, or Global Reader. "
            "Required consent: " + ", ".join(SCOPES),
        )
    if status_code == 401:
        return ErrorClassification("credential_expired", REAUTHORIZE)
    if status_code == 429:
        return ErrorClassification(
            "rate_limited", "Graph rate limit; retry this bounded read later"
        )
    return None


def _domain(value: str) -> str:
    value = value.rstrip(".").encode("idna").decode("ascii").lower()
    if not re.fullmatch(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise ValueError
    return value


def _request(ctx: ConnectorContext, key: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        return ctx.http.request("GET", GRAPH + PATHS[key], params=params)
    except ConnectorError as exc:
        if exc.status == "permission_required":
            permission, role = PERMISSIONS[key]
            raise ConnectorError(
                "permission_required", f"{key} requires delegated {permission} and {role}"
            ) from None
        raise


def _organization(ctx: ConnectorContext) -> dict[str, Any]:
    response = _request(ctx, "organization", {"$select": "id,verifiedDomains"})
    values = response["value"]
    if not values:
        raise ConnectorError("not_found", "No organization returned for the configured tenant")
    if len(values) != 1 or response.get("@odata.nextLink"):
        raise ConnectorError("failed", "Expected exactly one Microsoft organization")
    organization = values[0]
    tenant = _uuid(organization["id"])
    if tenant != ctx.http.auth.private_data["tenant_id"]:
        raise ConnectorError("permission_required", "Graph tenant does not match credential tenant")
    return {
        "tenant_id": tenant,
        "verified_domains": sorted({_domain(d["name"]) for d in organization["verifiedDomains"]}),
    }


def list_resources(ctx: ConnectorContext) -> Endpoint:
    def read(out: Endpoint) -> None:
        organization = _organization(ctx)
        out.data = {
            "resources": [{"id": normalize_resource(organization["tenant_id"]), **organization}]
        }
        out.rows = 1

    return ctx.capture("resources", GRAPH + PATHS["organization"], read)


def test(ctx: ConnectorContext) -> Endpoint:
    def read(out: Endpoint) -> None:
        if normalize_resource(ctx.resource) != normalize_resource(
            ctx.http.auth.private_data["tenant_id"]
        ):
            raise ConnectorError(
                "permission_required", "Bound tenant does not match credential tenant"
            )
        # Only saved customer profile domains are trusted, never a previous run's target.
        domains = {
            _domain((urlsplit(site).hostname or "").removeprefix("www."))
            for site in ctx.profile.get("websites", ())
        }
        if not domains:
            raise ConnectorError(
                "permission_required",
                "Save the customer's approved website in customer profile "
                "before testing its Microsoft tenant",
            )
        organization = _organization(ctx)
        matches = sorted(domains.intersection(organization["verified_domains"]))
        if not matches:
            raise ConnectorError(
                "permission_required",
                "Tenant verified domains do not match customer profile; refusing collection",
            )
        out.data = {
            **organization,
            "matched_customer_domains": matches,
            "profile_evidence_id": ctx.profile_evidence_id,
            "permission_evidence": "Organization.Read.All authenticated tenant/domain read",
            "time_basis": "Current configuration snapshot; not historical activity",
        }
        out.rows = 1

    return ctx.capture("organization", GRAPH + PATHS["organization"], read)


def _next_params(link: str, endpoint: str, initial: dict[str, str]) -> dict[str, str]:
    parts = urlsplit(link)
    if parts.scheme + "://" + parts.netloc + parts.path != endpoint or parts.fragment:
        raise ConnectorError("failed", "Refused pagination outside the original Graph endpoint")
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) - (set(initial) | {"$skiptoken", "$skip", "$top"}):
        raise ConnectorError("failed", "Refused unsupported Graph pagination query")
    for key, value in params.items():
        if key in {"$skip", "$top"}:
            if not value.isdecimal() or len(value) > 10 or (key == "$top" and int(value) > 999):
                raise ValueError
        elif key != "$skiptoken" and value != initial[key]:
            raise ConnectorError("failed", "Refused changed Graph pagination projection")
    return params


def _listing(
    ctx: ConnectorContext,
    key: str,
    params: dict[str, str],
    initial: dict[str, Any],
    consume: Callable[[dict[str, Any], dict[str, Any]], None],
) -> Endpoint:
    endpoint = GRAPH + PATHS[key]

    def read(out: Endpoint) -> None:
        out.data = {"time_basis": "Current snapshot; period does not filter these APIs", **initial}
        query, seen = params, set()
        for _ in range(ctx.max_pages):
            body = _request(ctx, key, query)
            rows = body["value"]
            if not isinstance(rows, list):
                raise ValueError
            remaining = ctx.page_size * ctx.max_pages - out.rows
            for row in rows[:remaining]:
                consume(out.data, row)
                out.rows += 1
            next_link = body.get("@odata.nextLink")
            if len(rows) > remaining:
                out.truncated = True
                return
            if not next_link:
                return
            if not isinstance(next_link, str) or next_link in seen:
                raise ConnectorError("failed", "Invalid or repeated Graph pagination link")
            seen.add(next_link)
            query = _next_params(next_link, endpoint, params)
        out.truncated = True

    return ctx.capture(key, endpoint, read)


def _flag(data: dict[str, Any], key: str, value: Any) -> None:
    suffix = "yes" if value is True else "no" if value is False else "unknown"
    data[key + "_" + suffix] += 1


def _flags(*keys: str) -> dict[str, int]:
    return {key + "_" + suffix: 0 for key in keys for suffix in ("yes", "no", "unknown")}


def _holder(ctx: ConnectorContext, value: str) -> str:
    identity = _uuid(value)
    return (
        "holder-"
        + hashlib.sha256(f"{ctx.customer}\0{ctx.resource}\0{identity}".encode()).hexdigest()[:20]
    )


def collect(ctx: ConnectorContext) -> None:
    # The framework calls collect only after test; retain that boundary for direct callers too.
    if not any(e.key == "organization" and e.complete for e in ctx.endpoints):
        raise ConnectorError(
            "permission_required", "Test the bound tenant/domain before collection"
        )

    def domain(data, row):
        data["domains"].append(
            {
                "domain": _domain(row["id"]),
                "verified": row["isVerified"] if type(row.get("isVerified")) is bool else None,
                "authentication_type": row["authenticationType"]
                if row.get("authenticationType") in {"Managed", "Federated"}
                else "unknown",
            }
        )

    _listing(
        ctx, "domains", {"$select": "id,isVerified,authenticationType"}, {"domains": []}, domain
    )

    def user(data, row):
        _flag(data, "enabled", row.get("accountEnabled"))
        kind = row.get("userType")
        _flag(data, "guest", kind == "Guest" if kind in {"Guest", "Member"} else None)
        licences = row.get("assignedLicenses")
        _flag(data, "licensed", bool(licences) if isinstance(licences, list) else None)

    _listing(
        ctx,
        "users",
        {
            "$select": "accountEnabled,userType,assignedLicenses",
            "$top": str(ctx.page_size),
        },
        _flags("enabled", "guest", "licensed"),
        user,
    )

    def licence(data, row):
        consumed, prepaid = row["consumedUnits"], row["prepaidUnits"]
        counts = {
            "assigned_seats": consumed,
            **{
                "prepaid_" + key: prepaid[key]
                for key in ("enabled", "suspended", "warning", "lockedOut")
                if key in prepaid
            },
        }
        if not all(type(value) is int and value >= 0 for value in counts.values()):
            raise ValueError
        for key, value in counts.items():
            data[key] = data.get(key, 0) + value

    _listing(
        ctx,
        "licences",
        {"$select": "consumedUnits,prepaidUnits"},
        {
            "assigned_seats": 0,
            "prepaid_enabled": 0,
            "prepaid_suspended": 0,
            "prepaid_warning": 0,
            "prepaid_lockedOut": 0,
            "note": "Seat assignments across SKUs; one user can consume several seats",
        },
        licence,
    )

    roles: set[str] = set()
    privileged: set[str] = set()
    holders: set[str] = set()

    def role(data, row):
        role_id = _uuid(row["roleDefinitionId"])
        holder = _holder(ctx, row["principalId"])
        definition = row.get("roleDefinition") or {}
        template = definition.get("templateId", role_id)
        is_privileged = definition.get("isBuiltIn") is True and template in PRIVILEGED_ROLES
        roles.add(role_id)
        holders.add(holder)
        if is_privileged:
            privileged.add(role_id)
            data["privileged_assignments"] += 1
        if definition.get("isBuiltIn") is not True and not is_privileged:
            data["unclassified_assignments"] += 1
        data.update(
            role_count=len(roles), privileged_role_count=len(privileged), holder_count=len(holders)
        )
        if len(data["redacted_assignments"]) < ctx.top_n:
            data["redacted_assignments"].append(
                {
                    "holder": holder,
                    "role_id": role_id,
                    "documented_privileged_role": is_privileged,
                }
            )
        else:
            data["holder_sample_truncated"] = True

    _listing(
        ctx,
        "role_assignments",
        {
            "$select": "principalId,roleDefinitionId",
            "$expand": "roleDefinition($select=templateId,isBuiltIn)",
        },
        {
            "role_count": 0,
            "privileged_role_count": 0,
            "privileged_assignments": 0,
            "holder_count": 0,
            "unclassified_assignments": 0,
            "redacted_assignments": [],
            "holder_sample_truncated": False,
            "note": "Active direct assignments; principals can be users, groups "
            "or service principals. "
            "No group expansion or PIM eligibility. Privileged built-in list dated 2026-10-24; "
            "custom/unclassified roles require review.",
        },
        role,
    )

    def registration(data, row):
        registered, admin = row.get("isMfaRegistered"), row.get("isAdmin")
        _flag(data, "registered", registered)
        _flag(data, "capable", row.get("isMfaCapable"))
        _flag(data, "admin", admin)
        if admin is True and registered is False:
            data["admins_not_registered"] += 1
            if len(data["redacted_admins_not_registered"]) < ctx.top_n:
                data["redacted_admins_not_registered"].append(_holder(ctx, row["id"]))
            else:
                data["holder_sample_truncated"] = True

    # Only $filter is documented here; do not assume $select/$top support. Reduce immediately.
    _listing(
        ctx,
        "mfa_registration",
        {},
        {
            **_flags("registered", "capable", "admin"),
            "admins_not_registered": 0,
            "redacted_admins_not_registered": [],
            "holder_sample_truncated": False,
            "note": "Registration/capability is not enforcement. Disabled users are omitted; "
            "report data can lag by 36 hours or more.",
        },
        registration,
    )

    def defaults(out: Endpoint):
        body = _request(ctx, "security_defaults", {"$select": "isEnabled"})
        if type(body.get("isEnabled")) is not bool:
            raise ValueError
        out.data = {"enabled": body["isEnabled"], "time_basis": "Current configuration snapshot"}
        out.rows = 1

    ctx.capture("security_defaults", GRAPH + PATHS["security_defaults"], defaults)

    def policy(data, row):
        state = row.get("state")
        key = {
            "enabled": "enabled",
            "disabled": "disabled",
            "enabledForReportingButNotEnforced": "report_only",
        }.get(state, "unknown")
        data[key] += 1
        grant = row.get("grantControls") or {}
        controls = grant.get("builtInControls") or []
        alternatives = (
            len(controls)
            + len(grant.get("termsOfUse") or [])
            + len(grant.get("customAuthenticationFactors") or [])
            + bool(grant.get("authenticationStrength"))
        )
        has_mfa = "mfa" in controls and "block" not in controls
        required = has_mfa and (
            grant.get("operator") == "AND" or (grant.get("operator") == "OR" and alternatives == 1)
        )
        if state == "enabled":
            data["enabled_requiring_mfa"] += int(required)
            data["enabled_with_mfa_option"] += int(has_mfa and not required)
            data["enabled_with_authentication_strength"] += int(
                bool(grant.get("authenticationStrength"))
            )

    _listing(
        ctx,
        "conditional_access",
        {
            "$select": "state,grantControls",
            "$top": str(ctx.page_size),
        },
        {
            "enabled": 0,
            "disabled": 0,
            "report_only": 0,
            "unknown": 0,
            "enabled_requiring_mfa": 0,
            "enabled_with_mfa_option": 0,
            "enabled_with_authentication_strength": 0,
            "note": "Policy configuration only. Conditions, exclusions, authentication strengths, "
            "legacy per-user MFA and actual sign-in enforcement need separate verification; "
            "these counts cannot establish tenant-wide protection.",
        },
        policy,
    )


def analyse(ctx: ConnectorContext, result: ModuleResult) -> None:
    registration = next((e for e in ctx.endpoints if e.key == "mfa_registration"), None)
    if not registration or not registration.complete or not registration.rows:
        return
    data = registration.data
    if data["registered_unknown"] or data["admin_unknown"]:
        return
    count = data["admins_not_registered"]
    check = "m365:admins-not-mfa-registered"
    site = "m365://" + ctx.resource.removeprefix("tenant:")
    refs = [registration.evidence_id]
    outcome = observe("m365", check, "security", ctx.resource, "fail" if count else "pass", refs)
    outcome.site = site
    result.outcomes.append(outcome)
    if count:
        result.finding(
            check,
            "Microsoft reports administrators without MFA registration",
            "security",
            refs,
            f"Connected registration report contains {count} administrator(s) not registered "
            "for MFA. This confirms the report's registration state, not MFA enforcement or "
            "a successful bypass. Disabled accounts are omitted and reporting can lag.",
            "Review the affected administrators in Entra and verify effective sign-in policies; "
            "recheck this same tenant's complete registration report after remediation.",
            site=site,
            component=ctx.resource,
            coverage=[ctx.resource],
            confidence="confirmed",
            severity="high",
        )


CONNECTOR = Connector(
    key="m365",
    label="Microsoft 365",
    provider="microsoft",
    auth_kind="ms_oauth/app",
    scopes=SCOPES,
    resource_type="tenant",
    read_allowlist=tuple(ReadOperation("GET", re.escape(GRAPH + path)) for path in PATHS.values()),
    normalize_resource=normalize_resource,
    list_resources=list_resources,
    test=test,
    collect=collect,
    analysers=(analyse,),
    authenticate=authenticate,
    authorize=authorize,
    classify_error=classify_error,
    categories=frozenset({"security"}),
    auth_flow="device_code",
    auth_hint=REAUTHORIZE.split("with ", 1)[1],
    collection_seconds=45,
    max_requests=80,
)
