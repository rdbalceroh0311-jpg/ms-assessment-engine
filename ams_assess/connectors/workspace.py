"""Bound Google Workspace Admin SDK reads; retain aggregates, not a user directory.

Official endpoint/scope references are linked in README's Google Workspace section.
Directory fields: https://developers.google.com/workspace/admin/directory/reference/rest/v1/users
Usage metrics: https://developers.google.com/workspace/admin/reports/v1/appendix/usage/customer/accounts
"""

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from ams_assess.connectors.auth import google_authenticate, google_authorize
from ams_assess.connectors.core import (
    Connector,
    ConnectorContext,
    ConnectorError,
    Endpoint,
    Period,
    ReadOperation,
)
from ams_assess.lifecycle import observe
from ams_assess.models import ModuleResult

DIRECTORY = "https://admin.googleapis.com/admin/directory/v1/"
REPORTS = "https://admin.googleapis.com/admin/reports/v1/usage/dates/"
CUSTOMER_ID = r"C[a-zA-Z0-9]{1,63}"
SCOPES = tuple(
    "https://www.googleapis.com/auth/" + scope
    for scope in (
        "admin.directory.customer.readonly",
        "admin.directory.domain.readonly",
        "admin.directory.user.readonly",
        "admin.directory.group.readonly",
        "admin.directory.rolemanagement.readonly",
        "admin.reports.usage.readonly",
    )
)
USAGE_METRICS = tuple(
    "accounts:num_users_2sv_" + suffix
    for suffix in (
        "enrolled",
        "not_enrolled",
        "enforced",
        "not_enforced",
        "protected",
        "not_protected",
    )
)
USER_FIELDS = (
    "id,customerId,suspended,archived,isAdmin,isDelegatedAdmin,isEnrolledIn2Sv,isEnforcedIn2Sv"
)
PERMISSIONS = {
    "customer": "Domain Settings/customer read access",
    "domains": "Domain Management/domain read access",
    "users": "Users > Read across all organisational units; a super admin for admin security flags",
    "groups": "Groups > Read API privilege across all groups",
    "role_assignments": "role-assignment read access; ask the customer's super administrator",
    "usage_2sv": "Reports administrator privilege",
}


def domain_name(value: str) -> str:
    domain = value.rstrip(".").encode("idna").decode("ascii").lower()
    if (
        len(domain) > 253
        or "." not in domain
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise ValueError("Invalid primary domain")
    return domain


def normalize_resource(value: str) -> str:
    # Resolve my_customer ONLY during explicit discovery; never persist an alias whose
    # identity changes with credentials, or bypass the framework's resource isolation.
    parts = value.split("/")
    if len(parts) != 2 or not re.fullmatch(CUSTOMER_ID, parts[0]):
        raise ValueError("Use <customer-id>/<primary-domain> from workspace list-resources")
    return parts[0] + "/" + domain_name(parts[1])


def read(ctx: ConnectorContext, key: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return ctx.http.request("GET", url, params=params)
    except ConnectorError as exc:
        if exc.status == "permission_required":
            raise ConnectorError("permission_required", "Required: " + PERMISSIONS[key]) from None
        raise


def flag(row: dict[str, Any], name: str) -> bool | None:
    value = row.get(name)
    if value is not None and type(value) is not bool:
        raise ValueError("Invalid boolean")
    return value


def object_list(body: dict[str, Any], field: str) -> list[dict[str, Any]]:
    rows = body.get(field, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid collection")
    return rows


def customer(ctx: ConnectorContext, identifier: str) -> Endpoint:
    url = DIRECTORY + "customers/" + identifier

    def fetch(result: Endpoint) -> None:
        body = read(ctx, "customer", url, {"fields": "id,customerDomain"})
        resource = normalize_resource(body["id"] + "/" + body["customerDomain"])
        if identifier != "my_customer" and resource != ctx.resource:
            raise ConnectorError(
                "not_found", "Customer ID/primary domain mismatch; collection refused"
            )
        result.data = {"customer_id": body["id"], "primary_domain": resource.split("/")[1]}
        result.rows = 1

    return ctx.capture("customer", url, fetch)


def domains(ctx: ConnectorContext, identity: Endpoint) -> Endpoint:
    identifier, primary = identity.data["customer_id"], identity.data["primary_domain"]
    url = DIRECTORY + "customer/" + identifier + "/domains"

    def fetch(result: Endpoint) -> None:
        body = read(ctx, "domains", url, {"fields": "domains(domainName,verified,isPrimary)"})
        rows = object_list(body, "domains")
        projected = [
            {
                "domain": domain_name(row["domainName"]),
                "verified": flag(row, "verified"),
                "primary": flag(row, "isPrimary"),
            }
            for row in rows
        ]
        matches = [row for row in projected if row["primary"] is True]
        if len(matches) != 1 or matches[0]["domain"] != primary:
            raise ConnectorError(
                "not_found", "Primary domain is not in this customer's domains; refused"
            )
        if matches[0]["verified"] is not True:
            raise ConnectorError(
                "permission_required", "Bound primary domain must be verified by its owner"
            )
        cap = ctx.max_pages * ctx.page_size
        result.data = {"domains": projected[:cap], "snapshot": "collection time"}
        result.rows = min(len(projected), cap)
        result.truncated = len(projected) > cap
        result.data["permission_evidence"] = (
            "Authenticated customer ID and verified primary domain match"
        )

    result = ctx.capture("domains", url, fetch)
    if result.status != "connected":
        # A successful metadata GET alone cannot label an unverified binding connected.
        identity.status = result.status
        identity.detail = "Binding verification failed at domains; collection refused"
    return result


def list_resources(ctx: ConnectorContext) -> Endpoint:
    identity = customer(ctx, "my_customer")
    if not identity.complete:
        return identity
    result = domains(ctx, identity)
    if result.status == "connected":
        result.data = {
            "rows": [
                {
                    "id": identity.data["customer_id"] + "/" + identity.data["primary_domain"],
                    "label": identity.data["primary_domain"],
                }
            ]
        }
        result.rows = 1
    return result


def test(ctx: ConnectorContext) -> Endpoint:
    ctx.resource = normalize_resource(ctx.resource)
    try:
        ctx.period = Period.make(
            days=(ctx.period.end - ctx.period.start).days + 1,
            start="" if ctx.period.automatic else str(ctx.period.start),
            end="" if ctx.period.automatic else str(ctx.period.end),
            zone="Etc/GMT+8",  # Reports specifies fixed UTC-08:00, not DST-adjusted LA time.
        )
    except ValueError:
        raise ConnectorError(
            "not_configured", "Use complete reporting days in UTC-08:00; today is not available"
        ) from None
    identity = customer(ctx, ctx.resource.split("/")[0])
    return domains(ctx, identity) if identity.complete else identity


def pages(
    ctx: ConnectorContext,
    result: Endpoint,
    key: str,
    url: str,
    params: dict[str, Any],
    field: str,
    consume: Callable[[dict[str, Any]], None],
) -> None:
    token, seen = "", set()
    cap = ctx.max_pages * ctx.page_size
    for _ in range(ctx.max_pages):
        body = read(ctx, key, url, {**params, **({"pageToken": token} if token else {})})
        batch = object_list(body, field)
        # Do not store provider warnings/messages: they may contain personal data.
        if body.get("warnings"):
            result.truncated = True
            result.detail = "Provider reports incomplete usage data; retry an older reporting date"
        available = cap - result.rows
        result.truncated |= len(batch) > available
        for row in batch[:available]:
            consume(row)
            result.rows += 1
        token = body.get("nextPageToken", "")
        if not isinstance(token, str):
            raise ValueError("Invalid pagination")
        if result.rows >= cap:
            result.truncated |= bool(token)
            return
        if not token:
            return
        if token in seen:
            result.truncated = True
            return
        seen.add(token)
    result.truncated = True


def user_counts(ctx: ConnectorContext) -> Endpoint:
    url = DIRECTORY + "users"

    def fetch(result: Endpoint) -> None:
        data: dict[str, Any] = {
            "snapshot": "collection time; non-deleted users visible to the authorised admin",
            "statuses": dict.fromkeys(("active", "suspended", "archived", "unknown"), 0),
            "admins": dict.fromkeys(("super", "delegated", "any", "unknown"), 0),
            "enrolment": dict.fromkeys(("yes", "no", "unknown"), 0),
            "enforcement": dict.fromkeys(("yes", "no", "unknown"), 0),
            "admins_without_enrolment": [],
            "suspended_admins": 0,
            "admin_enrolment_unknown": 0,
            "users_with_missing_flags": 0,
        }
        result.data = data
        seen = set()

        def consume(row: dict[str, Any]) -> None:
            if row.get("customerId") != ctx.resource.split("/")[0]:
                raise ConnectorError("not_found", "User customer ID mismatch; row refused")
            identifier = row["id"]
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise ValueError("Missing/duplicate user ID")
            suspended, archived, super_admin, delegated, enrolled, enforced = (
                flag(row, name)
                for name in (
                    "suspended",
                    "archived",
                    "isAdmin",
                    "isDelegatedAdmin",
                    "isEnrolledIn2Sv",
                    "isEnforcedIn2Sv",
                )
            )
            seen.add(identifier)
            data["users_with_missing_flags"] += any(
                value is None
                for value in (suspended, archived, super_admin, delegated, enrolled, enforced)
            )
            admin = (
                True
                if super_admin or delegated
                else (False if super_admin is False and delegated is False else None)
            )
            status = (
                "archived"
                if archived
                else "suspended"
                if suspended
                else ("active" if archived is False and suspended is False else "unknown")
            )
            data["statuses"][status] += 1
            data["admins"]["super"] += super_admin is True
            data["admins"]["delegated"] += delegated is True
            data["admins"]["any"] += admin is True
            data["admins"]["unknown"] += super_admin is None or delegated is None
            for label, value in (("enrolment", enrolled), ("enforcement", enforced)):
                data[label]["unknown" if value is None else "yes" if value else "no"] += 1
            data["suspended_admins"] += admin is True and suspended is True
            data["admin_enrolment_unknown"] += admin is True and enrolled is None
            if admin and enrolled is False:
                digest = hashlib.sha256(
                    (ctx.customer + ":" + ctx.resource + ":" + identifier).encode()
                )
                data["admins_without_enrolment"].append(
                    {
                        "id": "admin-" + digest.hexdigest()[:24],
                        "role": "super admin" if super_admin else "delegated admin",
                    }
                )

        try:
            pages(
                ctx,
                result,
                "users",
                url,
                {
                    "customer": ctx.resource.split("/")[0],
                    "maxResults": min(ctx.page_size, 500),
                    "viewType": "admin_view",
                    "projection": "full",
                    "fields": "nextPageToken,users(" + USER_FIELDS + ")",
                },
                "users",
                consume,
            )
        finally:
            # One opaque digest is enough to prevent a narrower OU view from resolving
            # an earlier finding. No additional per-user inventory reaches persistence.
            data["population_fingerprint"] = hashlib.sha256(
                json.dumps([ctx.customer, ctx.resource, sorted(seen)]).encode()
            ).hexdigest()
        if data["users_with_missing_flags"]:
            result.status = "connected_partial"
            result.detail = (
                "Some user flags are unavailable; unknown is not false. " + PERMISSIONS["users"]
            )

    return ctx.capture("users", url, fetch)


def group_counts(ctx: ConnectorContext) -> Endpoint:
    url = DIRECTORY + "groups"

    def fetch(result: Endpoint) -> None:
        result.data = {
            "count": 0,
            "externally_shared_count": None,
            "external_sharing": "unavailable with approved read-only Directory scopes",
        }
        seen = set()

        def consume(row: dict[str, Any]) -> None:
            identifier = row["id"]
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise ValueError("Missing/duplicate group ID")
            seen.add(identifier)
            result.data["count"] += 1

        pages(
            ctx,
            result,
            "groups",
            url,
            {
                "customer": ctx.resource.split("/")[0],
                "maxResults": min(ctx.page_size, 200),
                "fields": "nextPageToken,groups(id)",
            },
            "groups",
            consume,
        )
        result.detail = (
            "External group sharing is not exposed by Directory groups.list; not assessed"
        )

    return ctx.capture("groups", url, fetch)


def role_counts(ctx: ConnectorContext) -> Endpoint:
    url = DIRECTORY + "customer/" + ctx.resource.split("/")[0] + "/roleassignments"

    def fetch(result: Endpoint) -> None:
        result.data = {
            "count": 0,
            "by_role": {},
            "by_scope": {},
            "by_assignee_type": {},
            "coverage": "direct assignments only; group membership not expanded",
        }
        seen = set()

        def consume(row: dict[str, Any]) -> None:
            role = row["roleId"]
            assignment = row["roleAssignmentId"]
            if not isinstance(assignment, str) or not assignment or assignment in seen:
                raise ValueError("Missing/duplicate assignment ID")
            if not isinstance(role, str) or not re.fullmatch(r"[0-9]{1,30}", role):
                raise ValueError("Invalid role ID")
            seen.add(assignment)
            scope = row.get("scopeType", "unknown")
            assignee = row.get("assigneeType", "unknown")
            if scope not in {"CUSTOMER", "ORG_UNIT", "unknown"}:
                scope = "unknown"
            if assignee not in {"USER", "GROUP", "unknown"}:
                assignee = "unknown"
            result.data["count"] += 1
            for field, label in (
                ("by_role", role),
                ("by_scope", scope),
                ("by_assignee_type", assignee),
            ):
                counts = result.data[field]
                counts[label] = counts.get(label, 0) + 1

        pages(
            ctx,
            result,
            "role_assignments",
            url,
            {
                "maxResults": ctx.page_size,
                "fields": "nextPageToken,items(roleAssignmentId,roleId,scopeType,assigneeType)",
            },
            "items",
            consume,
        )

    return ctx.capture("role_assignments", url, fetch)


def usage_counts(ctx: ConnectorContext) -> Endpoint:
    # One dated snapshot, not a sum of users over time. Directory flags are current;
    # Reports counts are Google's separately reported historical protection measures.
    day = str(ctx.period.end)
    url = REPORTS + day

    def fetch(result: Endpoint) -> None:
        metrics: dict[str, int] = {}
        result.data = {"date": day, "timezone": "UTC-08:00", "metrics": metrics}

        def consume(row: dict[str, Any]) -> None:
            if row.get("entity", {}).get("customerId") != ctx.resource.split("/")[0]:
                raise ConnectorError("not_found", "Usage customer ID mismatch; row refused")
            if row.get("date") != day:
                raise ValueError("Usage date mismatch")
            projected = {}
            for parameter in object_list(row, "parameters"):
                name = parameter.get("name")
                if name not in USAGE_METRICS:
                    continue
                value = parameter.get("intValue")
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
                    raise ValueError("Invalid usage count")
                if name in projected or name in metrics:
                    raise ValueError("Duplicate usage metric")
                projected[name] = int(value)
            metrics.update(projected)

        pages(
            ctx,
            result,
            "usage_2sv",
            url,
            {
                "customerId": ctx.resource.split("/")[0],
                "parameters": ",".join(USAGE_METRICS),
                "fields": "nextPageToken,warnings(code),usageReports(date,entity(customerId),"
                "parameters(name,intValue))",
            },
            "usageReports",
            consume,
        )
        missing = [name for name in USAGE_METRICS if name not in metrics]
        result.data["unavailable_metrics"] = missing
        if missing:
            result.status = "connected_partial"
            result.detail = (
                "Requested 2SV usage metrics unavailable; reporting lag/coverage, not zero"
            )

    return ctx.capture("usage_2sv", url, fetch)


def collect(ctx: ConnectorContext) -> None:
    if not any(e.key == "domains" and e.status == "connected" for e in ctx.endpoints):
        raise ConnectorError(
            "not_configured", "Verify the customer and primary domain before collection"
        )
    user_counts(ctx)
    group_counts(ctx)
    role_counts(ctx)
    usage_counts(ctx)


def analyse(ctx: ConnectorContext, result: ModuleResult) -> None:
    users = next((e for e in ctx.endpoints if e.key == "users"), None)
    if not users or not users.rows:
        return  # Empty/inaccessible data cannot resolve earlier security findings.
    data = users.data
    coverage = [ctx.resource + ":population:" + data["population_fingerprint"]]
    checks = (
        (
            "admins-without-2sv",
            len(data["admins_without_enrolment"]),
            "Workspace administrators without 2SV enrolment",
            "admin accounts explicitly not enrolled",
            "Enrol administrators in 2SV and verify recovery methods with the customer.",
            "high",
        ),
        (
            "2sv-not-enforced",
            data["enforcement"]["no"],
            "Workspace users without 2SV enforcement",
            "user accounts with enforcement explicitly false",
            "Review rollout exceptions and enforce 2SV through the customer's approved "
            "change process.",
            "medium",
        ),
        (
            "suspended-admins",
            data["suspended_admins"],
            "Suspended Workspace accounts retain administrator status",
            "suspended accounts still flagged as super or delegated administrators",
            "Review and remove unnecessary admin privileges through the customer's "
            "approved process.",
            "high",
        ),
    )
    for key, count, title, description, recommendation, severity in checks:
        if not count and not users.complete:
            continue
        check = "workspace:" + key
        site = "workspace://" + ctx.resource
        outcome = observe(
            "workspace",
            check,
            "security",
            ctx.resource,
            "fail" if count else "pass",
            [users.evidence_id],
            coverage=coverage,
        )
        outcome.site = site
        result.outcomes.append(outcome)
        if count:
            result.finding(
                check,
                title,
                "security",
                [users.evidence_id],
                f"At least {count} {description} in the returned Directory snapshot. "
                "Enrolment, enforcement and Reports protection are separate measures. "
                "This is not a claim about successful sign-ins or the security of unseen accounts.",
                recommendation,
                confidence="confirmed",
                severity=severity,
                ams_service="Managed IT & Technology",
                site=site,
                component=ctx.resource,
                coverage=coverage,
            )


CONNECTOR = Connector(
    key="workspace",
    label="Google Workspace",
    provider="google",
    auth_kind="google_oauth",
    scopes=SCOPES,
    resource_type="workspace_customer_primary_domain",
    categories=frozenset({"security", "domain_email"}),
    read_allowlist=(
        ReadOperation("GET", re.escape(DIRECTORY) + rf"customers/(?:{CUSTOMER_ID}|my_customer)"),
        ReadOperation(
            "GET", re.escape(DIRECTORY) + rf"customer/{CUSTOMER_ID}/(?:domains|roleassignments)"
        ),
        ReadOperation("GET", re.escape(DIRECTORY) + r"(?:users|groups)"),
        ReadOperation("GET", re.escape(REPORTS) + r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
    ),
    normalize_resource=normalize_resource,
    list_resources=list_resources,
    test=test,
    collect=collect,
    analysers=(analyse,),
    authenticate=google_authenticate,
    authorize=google_authorize,
)
