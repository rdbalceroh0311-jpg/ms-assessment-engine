"""Synthetic Graph/MSAL regressions; no authorised tenant or live provider IO."""

import base64
import copy
import json
import logging
import stat
import time
from types import SimpleNamespace

import httpx
import msal
import pytest
from test_connectors import ui_server

from ams_assess import ai, cli, store
from ams_assess.connectors import auth, m365, service
from ams_assess.connectors.core import AuthMaterial, ConnectorContext, ConnectorError, Period
from ams_assess.connectors.http import ReadClient
from ams_assess.models import Evidence
from ams_assess.report import render_customer

TENANT = "11111111-1111-4111-8111-111111111111"
RESOURCE = "tenant:" + TENANT
OTHER = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"
ROLE = "62e90394-69f5-4237-9190-012177145e10"
ACCESS = "synthetic-microsoft-access-never-print"
REFRESH = "synthetic-microsoft-refresh-never-print"
ACCOUNT = "synthetic-home-account"


class Cache:
    def __init__(self):
        self.has_state_changed = False
        self.value = "{}"

    def deserialize(self, value):
        self.value = value

    def serialize(self):
        return self.value


class Microsoft:
    """Mock the SDK boundary, including silent refresh and the native device-code API."""

    def __init__(self):
        self.calls = []
        self.interactive = {
            "access_token": ACCESS,
            "scope": " ".join(m365.SCOPES) + " openid profile offline_access",
            "id_token_claims": {"tid": TENANT},
        }
        self.silent = copy.deepcopy(self.interactive)
        self.accounts = [{"realm": TENANT, "home_account_id": ACCOUNT}]
        self.flow = {
            "device_code": "private-device-code",
            "user_code": "ABCD12345",
            "expires_at": time.time() + 900,
            "message": ACCESS + REFRESH,
        }
        self.sdk = SimpleNamespace(SerializableTokenCache=Cache, PublicClientApplication=self.app)

    def app(self, client_id, **kwargs):
        self.calls.append(("app", client_id, kwargs))
        cache = kwargs["token_cache"]

        def initiate(**kw):
            self.calls.append(("initiate", kw))
            return self.flow.copy()

        def device(flow):
            self.calls.append(("device", flow))
            cache.value = json.dumps({"refresh_token": REFRESH})
            return self.interactive

        def silent(scopes, account):
            self.calls.append(("silent", scopes, account))
            cache.value = json.dumps({"refresh_token": REFRESH, "refreshed": True})
            cache.has_state_changed = True
            return self.silent

        return SimpleNamespace(
            get_accounts=lambda: self.accounts,
            initiate_device_flow=initiate,
            acquire_token_by_device_flow=device,
            acquire_token_silent_with_error=silent,
        )


class Graph:
    def __init__(self):
        self.calls = []
        self.override = lambda request: None
        self.payloads = {
            "organization": {
                "value": [
                    {
                        "id": TENANT,
                        "verifiedDomains": [
                            {"name": "example.test"},
                            {"name": "example.onmicrosoft.com"},
                        ],
                    }
                ]
            },
            "domains": {
                "value": [
                    {"id": "example.test", "isVerified": True, "authenticationType": "Managed"}
                ]
            },
            "users": {
                "value": [
                    {
                        "id": USER,
                        "displayName": "Private Name",
                        "userPrincipalName": "person@example.test",
                        "accountEnabled": True,
                        "userType": "Member",
                        "assignedLicenses": [{"skuId": OTHER}],
                    },
                    {"accountEnabled": False, "userType": "Guest", "assignedLicenses": []},
                ]
            },
            "licences": {
                "value": [
                    {
                        "consumedUnits": 2,
                        "prepaidUnits": {
                            "enabled": 5,
                            "suspended": 0,
                            "warning": 1,
                            "lockedOut": 0,
                        },
                    }
                ]
            },
            "role_assignments": {
                "value": [
                    {
                        "principalId": USER,
                        "roleDefinitionId": ROLE,
                        "roleDefinition": {"templateId": ROLE, "isBuiltIn": True},
                    }
                ]
            },
            "mfa_registration": {
                "value": [
                    {
                        "id": USER,
                        "displayName": "Private Name",
                        "userPrincipalName": "person@example.test",
                        "isMfaRegistered": False,
                        "isMfaCapable": False,
                        "isAdmin": True,
                    }
                ]
            },
            "security_defaults": {"isEnabled": True},
            "conditional_access": {
                "value": [
                    {
                        "state": "enabled",
                        "displayName": "Private Name",
                        "grantControls": {"operator": "AND", "builtInControls": ["mfa"]},
                    }
                ]
            },
        }

    def __call__(self, request):
        self.calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["Authorization"] == "Bearer " + ACCESS
        response = self.override(request)
        if response is not None:
            return response
        key = next(k for k, path in m365.PATHS.items() if request.url.path == "/v1.0" + path)
        return httpx.Response(200, json=self.payloads[key])


@pytest.fixture
def env(tmp_path, monkeypatch):
    root, private = tmp_path / "customers", tmp_path / "private"
    private.mkdir(mode=0o700)
    microsoft, graph = Microsoft(), Graph()
    monkeypatch.setattr(m365, "_sdk", lambda: microsoft.sdk)
    monkeypatch.setattr(m365, "config_dir", lambda: private)
    monkeypatch.setattr(service, "config_dir", lambda: private)
    monkeypatch.setattr(cli, "data_root", lambda: root)
    token = private / "m365-synthetic-token.json"
    auth.atomic_private(
        token,
        {
            "client_id": CLIENT,
            "tenant_id": TENANT,
            "account_id": ACCOUNT,
            "cache": "{}",
            "scopes": list(m365.SCOPES),
        },
    )
    auth.register_reference("synthetic", m365.CONNECTOR, file=token, directory=private)
    real_client = service.ReadClient
    monkeypatch.setattr(
        service,
        "ReadClient",
        lambda *a, transport=None, **kw: real_client(
            *a, transport=httpx.MockTransport(graph), **kw
        ),
    )
    sleeps = []
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", sleeps.append)
    store.set_profile(
        root,
        "synthetic",
        {
            "name": "Synthetic Customer",
            "websites": ["https://www.example.test"],
        },
    )
    return SimpleNamespace(
        root=root,
        private=private,
        token=token,
        microsoft=microsoft,
        graph=graph,
        sleeps=sleeps,
    )


def bind(env, resource=TENANT):
    return service.set_binding(
        env.root, "synthetic", "m365", "synthetic", "tenant:" + resource.removeprefix("tenant:")
    )


def collect(env):
    return service.execute(env.root, "synthetic", "m365", collect=True)


def current(env):
    return store.load(env.root, "synthetic")


def payloads(env):
    return {
        e["data"]["operation"]: e["data"] for e in current(env)["evidence"] if e["source"] == "m365"
    }


def error(code, message="private provider message"):
    return {"error": {"code": code, "message": message}}


def test_auth_device_code_scope_reference_and_private_cache(env, capsys):
    assert (
        cli.main(
            [
                "connect",
                "m365",
                "auth",
                "--credential-ref",
                "new-ref",
                "--tenant-id",
                TENANT,
                "--client-id",
                CLIENT,
            ]
        )
        == 0
    )
    app, initiate, device = env.microsoft.calls
    assert app[1] == CLIENT
    assert app[2]["authority"] == "https://login.microsoftonline.com/" + TENANT
    assert app[2]["instance_discovery"] is False
    assert app[2]["timeout"] == 10 and app[2]["enable_pii_log"] is False
    assert initiate == ("initiate", {"scopes": list(m365.SCOPES)})
    assert device[0] == "device"
    assert 175 <= device[1]["expires_at"] - time.time() <= 180
    assert device[1]["device_code"] == "private-device-code"
    data = json.loads((env.private / "m365-new-ref-token.json").read_text())
    assert data["client_id"] == CLIENT and data["tenant_id"] == TENANT
    assert data["account_id"] == ACCOUNT and REFRESH in data["cache"]
    assert auth.references(env.private)["new-ref"]["connector"] == "m365"
    assert stat.S_IMODE(env.private.stat().st_mode) == 0o700
    for path in env.private.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not env.graph.calls
    assert not current(env).get("connectors")
    output = capsys.readouterr().out
    assert "https://microsoft.com/devicelogin" in output and "ABCD12345" in output
    assert all(secret not in output for secret in [ACCESS, REFRESH, "private-device-code"])


@pytest.mark.parametrize("change", ["tenant", "mail", "missing", "empty_scopes", "no_claims"])
def test_auth_refuses_wrong_tenant_and_scope_sets(env, change):
    result = env.microsoft.interactive
    if change == "tenant":
        result["id_token_claims"]["tid"] = OTHER
    elif change == "no_claims":
        result.pop("id_token_claims")
    elif change == "mail":
        result["scope"] += " Mail.Read Files.Read"
    elif change == "missing":
        result["scope"] = "Organization.Read.All"
    else:
        result.pop("scope")
    with pytest.raises(ConnectorError) as exc:
        m365.authorize(
            m365.CONNECTOR, "new-ref", tenant_id=TENANT, client_id=CLIENT, directory=env.private
        )
    assert exc.value.status == "permission_required"
    assert "new-ref" not in auth.references(env.private)
    assert not (env.private / "m365-new-ref-token.json").exists()


def test_auth_cannot_retarget_an_existing_reference(env):
    with pytest.raises(ConnectorError, match="new reference"):
        m365.authorize(
            m365.CONNECTOR, "synthetic", tenant_id=OTHER, client_id=CLIENT, directory=env.private
        )
    assert not env.microsoft.calls
    assert json.loads(env.token.read_text())["tenant_id"] == TENANT


@pytest.mark.parametrize(
    "failure, status",
    [
        (None, "connected"),
        ("invalid_grant", "credential_expired"),
        ("interaction_required", "credential_expired"),
        ("consent_required", "permission_required"),
        ("temporarily_unavailable", "provider_unavailable"),
    ],
)
def test_silent_refresh_or_revocation(env, failure, status):
    if failure:
        env.microsoft.silent = {"error": failure, "error_description": ACCESS + REFRESH}
    bind(env)
    state = collect(env)
    assert state["status"] == status
    assert "refreshed" in json.loads(env.token.read_text())["cache"]
    assert env.microsoft.calls[1] == ("silent", list(m365.SCOPES), env.microsoft.accounts[0])
    assert bool(env.graph.calls) is (failure is None)
    assert ACCESS not in json.dumps(state) and REFRESH not in json.dumps(state)


@pytest.mark.parametrize("kind", ["missing", "wrong_provider", "wrong_account", "wrong_realm"])
def test_bad_credentials_do_not_read_graph(env, kind):
    if kind == "missing":
        env.token.unlink()
    elif kind == "wrong_provider":
        refs = auth.references(env.private)
        refs["synthetic"]["provider"] = "google"
        auth.atomic_private(env.private / "connectors.json", refs)
    elif kind == "wrong_account":
        env.microsoft.accounts[0]["home_account_id"] = "different"
    else:
        env.microsoft.accounts[0]["realm"] = OTHER
    bind(env)
    assert collect(env)["status"] in {"credential_missing", "credential_expired"}
    assert not env.graph.calls


def test_resources_only_lists_configured_tenant_and_requires_explicit_binding(env):
    result = service.list_resources("m365", "synthetic")
    assert result.data["resources"][0]["id"] == RESOURCE
    assert len(env.graph.calls) == 1
    assert not current(env).get("connectors")
    assert collect(env)["status"] == "not_configured"
    assert len(env.graph.calls) == 1
    binding = bind(env, TENANT.upper())
    assert binding["resource_id"] == RESOURCE
    assert binding["status"] == "not_configured"


@pytest.mark.parametrize("kind", ["binding", "organization", "domain", "suffix", "no_profile"])
def test_tenant_and_customer_domain_mismatch_fail_closed(env, kind):
    bind(env, OTHER if kind == "binding" else TENANT)
    org = env.graph.payloads["organization"]["value"][0]
    if kind == "organization":
        org["id"] = OTHER
    elif kind in {"domain", "suffix"}:
        org["verifiedDomains"] = [
            {"name": "unrelated.test" if kind == "domain" else "notexample.test"}
        ]
    elif kind == "no_profile":
        store.set_profile(env.root, "synthetic", {"websites": []})
    state = collect(env)
    assert state["status"] == "permission_required"
    assert len(env.graph.calls) == (0 if kind in {"binding", "no_profile"} else 1)
    assert not current(env)["findings"]
    assert "last_successful_test" not in state


def test_success_collects_every_endpoint_with_provenance_and_minimization(env):
    bind(env)
    state = collect(env)
    assert state["status"] == "connected"
    assert len(env.graph.calls) == 8
    evidence = payloads(env)
    assert set(evidence) == set(m365.PATHS)
    for key, item in evidence.items():
        assert item["connector"] == "m365" and item["resource_id"] == RESOURCE
        assert item["endpoint"] == m365.GRAPH + m365.PATHS[key]
        assert item["collected_at"] and item["period"]["start"] and item["period"]["end"]
        assert item["coverage"]["truncated"] is False
    assert evidence["users"]["data"]["enabled_yes"] == 1
    assert evidence["users"]["data"]["enabled_no"] == 1
    assert evidence["users"]["data"]["guest_yes"] == 1
    assert evidence["users"]["data"]["licensed_yes"] == 1
    assert evidence["licences"]["data"]["prepaid_enabled"] == 5
    assert evidence["role_assignments"]["data"]["privileged_role_count"] == 1
    assert evidence["role_assignments"]["data"]["holder_count"] == 1
    assert evidence["mfa_registration"]["data"]["registered_no"] == 1
    assert evidence["security_defaults"]["data"]["enabled"] is True
    assert evidence["conditional_access"]["data"]["enabled_requiring_mfa"] == 1
    finding = next(f for f in current(env)["findings"] if f["check_key"].startswith("m365:"))
    assert finding["confidence"] == "confirmed"
    assert "registration" in finding["title"]
    assert "not MFA enforcement" in finding["detail"]
    for request in env.graph.calls:
        assert "Mail" not in str(request.url) and "Files" not in str(request.url)
        if request.url.path.endswith("userRegistrationDetails"):
            assert not request.url.query  # $select and $top are not documented on this endpoint.
        if request.url.path.endswith("subscribedSkus"):
            assert set(request.url.params) == {"$select"}


@pytest.mark.parametrize(
    "key,status,code,expected",
    [
        ("users", 403, "Authorization_RequestDenied", "permission_required"),
        (
            "mfa_registration",
            403,
            "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
            "licence_required",
        ),
        ("conditional_access", 403, "TenantNotLicensed", "licence_required"),
        ("users", 401, "InvalidAuthenticationToken", "credential_expired"),
        ("domains", 404, "Request_ResourceNotFound", "not_found"),
        ("users", 429, "TooManyRequests", "rate_limited"),
        ("licences", 503, "ServiceUnavailable", "provider_unavailable"),
    ],
)
def test_endpoint_failures_preserve_successful_evidence(env, key, status, code, expected):
    env.graph.override = lambda r: (
        httpx.Response(status, json=error(code))
        if (r.url.path == "/v1.0" + m365.PATHS[key])
        else None
    )
    bind(env)
    state = collect(env)
    assert state["status"] == "connected_partial"
    assert payloads(env)[key]["status"] == expected
    assert payloads(env)["security_defaults"]["status"] == "connected"
    assert expected in state["detail"] and key in state["detail"]
    if expected == "permission_required":
        assert m365.PERMISSIONS[key][0] in payloads(env)[key]["detail"]
    assert not state.get("last_successful_collection")
    attempts = [r for r in env.graph.calls if r.url.path == "/v1.0" + m365.PATHS[key]]
    assert len(attempts) == (3 if status in {429, 503} else 1)
    if key == "mfa_registration":
        assert not current(env)["findings"]


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Tenant does not have a premium license", "licence_required"),
        ("Required licenses are missing", "licence_required"),
        ("Insufficient privileges to complete operation", "permission_required"),
    ],
)
def test_licence_diagnostic_is_distinct_from_permission(env, message, expected):
    env.graph.override = lambda r: (
        httpx.Response(403, json=error("AccessDenied", message))
        if (r.url.path.endswith("userRegistrationDetails"))
        else None
    )
    bind(env)
    collect(env)
    assert payloads(env)["mfa_registration"]["status"] == expected
    assert not current(env)["findings"]


def test_expired_graph_token_on_connection_stops_collection(env):
    env.graph.override = lambda r: httpx.Response(401, json=error("InvalidAuthenticationToken"))
    bind(env)
    assert collect(env)["status"] == "credential_expired"
    assert len(env.graph.calls) == 1


def test_rate_limit_backoff_then_success(env):
    attempts = []

    def response(request):
        if request.url.path.endswith("/users"):
            attempts.append(request)
            if len(attempts) < 3:
                return httpx.Response(
                    429, json=error("TooManyRequests"), headers={"Retry-After": "1"}
                )

    env.graph.override = response
    bind(env)
    assert collect(env)["status"] == "connected"
    assert env.sleeps == [1.0, 1.0]


def test_empty_collections_are_not_missing_permissions_or_passing_mfa_checks(env):
    for body in env.graph.payloads.values():
        if "value" in body and body is not env.graph.payloads["organization"]:
            body["value"] = []
    bind(env)
    assert collect(env)["status"] == "connected"
    assert payloads(env)["users"]["coverage"]["rows_returned"] == 0
    assert not current(env)["findings"]
    env.graph.payloads["organization"]["value"] = []
    assert collect(env)["status"] == "not_found"


@pytest.mark.parametrize(
    "key",
    ["domains", "users", "licences", "role_assignments", "mfa_registration", "conditional_access"],
)
def test_odata_nextlink_all_collections(env, key):
    path = "/v1.0" + m365.PATHS[key]

    def pages(request):
        if request.url.path != path:
            return None
        body = copy.deepcopy(env.graph.payloads[key])
        if "$skiptoken" not in request.url.params:
            body["@odata.nextLink"] = m365.GRAPH + m365.PATHS[key] + "?$skiptoken=opaque%2Bpage"
        else:
            assert request.url.params["$skiptoken"] == "opaque+page"
        return httpx.Response(200, json=body)

    env.graph.override = pages
    bind(env)
    assert collect(env)["status"] == "connected"
    assert payloads(env)[key]["coverage"]["rows_returned"] == (
        len(env.graph.payloads[key]["value"]) * 2
    )
    assert "opaque" not in json.dumps(current(env))


@pytest.mark.parametrize("fallback_page", [0, 1])
def test_role_expansion_fallback_preserves_pagination_and_redaction(env, fallback_page):
    path = "/v1.0" + m365.PATHS["role_assignments"]
    nested = "roleDefinition($select=templateId,isBuiltIn)"

    def pages(request):
        if request.url.path != path:
            return None
        params = dict(request.url.params)
        page = int("$skiptoken" in params)
        if page == fallback_page and params["$expand"] == nested:
            return httpx.Response(400, json=error("Request_BadRequest"))
        assert params["$select"] == "principalId,roleDefinitionId"
        body = copy.deepcopy(env.graph.payloads["role_assignments"])
        body["value"][0]["roleDefinition"].update(
            displayName="private-role-name", description="private-role-description"
        )
        if page == 0:
            body["@odata.nextLink"] = str(
                httpx.URL(
                    m365.GRAPH + m365.PATHS["role_assignments"],
                    params={**params, "$skiptoken": "second"},
                )
            )
        return httpx.Response(200, json=body)

    env.graph.override = pages
    bind(env)
    assert collect(env)["status"] == "connected"
    requests = [r for r in env.graph.calls if r.url.path == path]
    assert [r.url.params["$expand"] for r in requests] == (
        [nested, "roleDefinition", "roleDefinition"]
        if fallback_page == 0
        else [nested, nested, "roleDefinition"]
    )
    roles = payloads(env)["role_assignments"]
    assert roles["coverage"]["rows_returned"] == 2
    assert roles["data"]["holder_count"] == 1
    assert roles["data"]["privileged_assignments"] == 2
    snapshot = json.dumps(current(env))
    for private in [USER, "private-role-name", "private-role-description"]:
        assert private not in snapshot


@pytest.mark.parametrize(
    "status_code, code, status, attempts",
    [
        (400, "Request_BadRequest", "failed", 2),
        (400, "TenantNotLicensed", "licence_required", 1),
        (400, "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "permission_required", 1),
        (403, "Authorization_RequestDenied", "permission_required", 1),
    ],
)
def test_role_expansion_fallback_is_bounded_and_preserves_other_evidence(
    env, status_code, code, status, attempts
):
    path = "/v1.0" + m365.PATHS["role_assignments"]
    env.graph.override = lambda r: (
        httpx.Response(status_code, json=error(code)) if r.url.path == path else None
    )
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    assert payloads(env)["role_assignments"]["status"] == status
    assert len([r for r in env.graph.calls if r.url.path == path]) == attempts
    assert payloads(env)["users"]["status"] == "connected"
    assert payloads(env)["mfa_registration"]["status"] == "connected"


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.test/v1.0/users?$skiptoken=secret",
        "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=secret",
        "https://graph.microsoft.com:443/v1.0/users?$skiptoken=secret",
        "https://graph.microsoft.com/v1.0/users?$expand=manager",
        "https://graph.microsoft.com/v1.0/users?$skiptoken=one&$skiptoken=two",
        "https://graph.microsoft.com/v1.0/users?$select=mail",
        "https://graph.microsoft.com/v1.0/users?$skiptoken=x#fragment",
    ],
)
def test_hostile_nextlinks_never_get_requested(env, link):
    env.graph.payloads["users"]["@odata.nextLink"] = link
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    user = payloads(env)["users"]
    assert user["status"] == "failed" and user["coverage"]["truncated"]
    assert user["data"]["enabled_yes"] == 1  # The first page survives.
    assert len(env.graph.calls) == 8
    assert "secret" not in json.dumps(current(env))


def test_second_page_permission_failure_preserves_counts(env):
    env.graph.payloads["users"]["@odata.nextLink"] = m365.GRAPH + "/users?$skiptoken=page2"
    env.graph.override = lambda r: (
        httpx.Response(403, json=error("AccessDenied")) if ("$skiptoken" in r.url.params) else None
    )
    bind(env)
    collect(env)
    evidence = payloads(env)["users"]
    assert evidence["status"] == "permission_required"
    assert evidence["coverage"] == {"rows_returned": 2, "truncated": True}
    assert evidence["data"]["enabled_yes"] == 1


def test_row_and_page_caps_and_sample_caps(env):
    env.graph.payloads["mfa_registration"]["value"] *= 501
    env.graph.payloads["role_assignments"]["value"] *= 101
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    report = payloads(env)["mfa_registration"]
    assert report["coverage"] == {"rows_returned": 500, "truncated": True}
    assert len(report["data"]["redacted_admins_not_registered"]) == 100
    assert report["data"]["holder_sample_truncated"] is True
    assert payloads(env)["role_assignments"]["data"]["holder_sample_truncated"] is True
    assert not current(env)["findings"]  # Partial reports never supply a passing/failing recheck.


def test_repeated_nextlink_fails_without_unbounded_reads(env):
    env.graph.payloads["users"]["@odata.nextLink"] = m365.GRAPH + "/users?$skiptoken=repeat"
    bind(env)
    collect(env)
    assert payloads(env)["users"]["status"] == "failed"
    assert sum(r.url.path.endswith("/users") for r in env.graph.calls) == 2


@pytest.mark.parametrize(
    "state,controls,operator,other,required,option",
    [
        ("enabled", ["mfa"], "OR", {}, 1, 0),
        ("enabled", ["mfa", "compliantDevice"], "AND", {}, 1, 0),
        ("enabled", ["mfa", "compliantDevice"], "OR", {}, 0, 1),
        ("enabled", ["mfa"], "OR", {"termsOfUse": [OTHER]}, 0, 1),
        ("enabled", ["mfa", "block"], "AND", {}, 0, 0),
        ("enabledForReportingButNotEnforced", ["mfa"], "AND", {}, 0, 0),
        ("disabled", ["mfa"], "AND", {}, 0, 0),
        ("enabled", [], "AND", {"authenticationStrength": {"id": OTHER}}, 0, 0),
    ],
)
def test_conditional_access_does_not_overstate_mfa(
    env, state, controls, operator, other, required, option
):
    env.graph.payloads["conditional_access"]["value"] = [
        {
            "state": state,
            "grantControls": {"operator": operator, "builtInControls": controls, **other},
        }
    ]
    bind(env)
    collect(env)
    data = payloads(env)["conditional_access"]["data"]
    assert data["enabled_requiring_mfa"] == required
    assert data["enabled_with_mfa_option"] == option
    assert "tenant-wide" in data["note"]


def test_custom_roles_and_redaction_are_customer_specific(env):
    env.graph.payloads["role_assignments"]["value"].append(
        {
            "principalId": OTHER,
            "roleDefinitionId": CLIENT,
            "roleDefinition": {"isBuiltIn": False, "templateId": CLIENT},
        }
    )
    bind(env)
    collect(env)
    data = payloads(env)["role_assignments"]["data"]
    assert data["role_count"] == 2 and data["privileged_role_count"] == 1
    assert data["unclassified_assignments"] == 1
    ctx = ConnectorContext(None, TENANT, Period.make(), customer="one")
    first = m365._holder(ctx, USER)
    ctx.customer = "two"
    assert first != m365._holder(ctx, USER)


def test_customer_isolation_and_forced_binding_history(env):
    bind(env)
    store.set_profile(env.root, "other", {"websites": ["https://other.test"]})
    with pytest.raises(ValueError, match="different customer"):
        service.set_binding(env.root, "other", "m365", "synthetic", RESOURCE)
    with pytest.raises(ValueError):
        service.set_binding(env.root, "other", "m365", "synthetic", RESOURCE, force=True)
    service.set_binding(
        env.root,
        "other",
        "m365",
        "synthetic",
        RESOURCE,
        force=True,
        reason="Synthetic deliberate shared tenant",
    )
    assert (store.load(env.root, "other") or {})["connector_history"][-1]["forced"] is True
    # Even an explicit forced cross-customer binding cannot bypass the domain check.
    assert (
        service.execute(env.root, "other", "m365", collect=True)["status"] == "permission_required"
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/users"),
        ("PATCH", "/organization"),
        ("DELETE", "/users"),
        ("GET", "/me"),
        ("GET", "/users/" + USER),
        ("GET", "/me/messages"),
        ("GET", "/sites"),
        ("GET", "/users?$expand=manager"),
    ],
)
def test_read_allowlist_refuses_unapproved_operations(method, path):
    calls = []
    client = ReadClient(
        m365.CONNECTOR,
        AuthMaterial(),
        transport=httpx.MockTransport(
            lambda request: (calls.append(request), httpx.Response(200, json={}))[1]
        ),
    )
    try:
        with pytest.raises(ConnectorError, match="allowlist"):
            client.request(method, m365.GRAPH + path)
        assert not calls
    finally:
        client.close()


@pytest.mark.parametrize("partial", [False, True])
def test_cli_ui_report_register_and_ai_have_consistent_private_results(
    env, capsys, caplog, partial
):
    caplog.set_level(logging.DEBUG)
    if partial:
        env.graph.override = lambda r: (
            httpx.Response(403, json=error("TenantNotLicensed"))
            if (r.url.path.endswith("/policies"))
            else None
        )
    store.set_register(env.root, "synthetic", "m365", {"ams_access": "granted"})
    assert all(e["integration"] != "connected" for e in current(env)["systems_register"]["entries"])
    with ui_server(env.root) as (server, client):
        data = {
            "customer": "synthetic",
            "connector": "m365",
            "csrf": server.csrf,
            "credential_ref": "synthetic",
            "resource": RESOURCE,
        }
        for action in ("reference", "bind", "test", "collect"):
            response = client.post(
                server.url("/connect"),
                data={**data, "action": action},
                headers={"Origin": server.origin},
            )
            assert response.status_code == 303
        html = client.get(server.url(customer="synthetic")).text
    assert cli.main(["connectors", "--customer", "synthetic"]) == 0
    output = capsys.readouterr().out
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text()
    status = "connected_partial" if partial else "connected"
    for text in [TENANT, status, "users: connected, 2 rows"]:
        assert text in html and text in output and text in report
    assert "Microsoft reports administrators" in report.split("## 7.")[1]
    snapshot = current(env)
    assert any(e["integration"] == status for e in snapshot["systems_register"]["entries"])
    sinks = json.dumps(snapshot) + html + output + report + caplog.text
    for private in [ACCESS, REFRESH, USER, ACCOUNT, "Private Name", "person@example.test"]:
        assert private not in sinks
    evidence = [Evidence.model_validate(e) for e in snapshot["evidence"] if e["source"] == "m365"]
    aggregate = json.dumps(ai.compact_evidence(evidence))
    assert "rows_returned" in aggregate
    for private in ["example.test", "holder-", ROLE, "redacted_assignments", "redacted_admins"]:
        assert private not in aggregate


def test_inaccessible_or_empty_report_does_not_resolve_a_finding(env):
    bind(env)
    collect(env)
    env.graph.override = lambda r: (
        httpx.Response(403, json=error("TenantNotLicensed"))
        if (r.url.path.endswith("userRegistrationDetails"))
        else None
    )
    collect(env)
    assert current(env)["findings"][0]["lifecycle_status"] != "resolved"
    env.graph.override = lambda r: None
    env.graph.payloads["mfa_registration"]["value"] = []
    collect(env)
    assert current(env)["findings"][0]["lifecycle_status"] != "resolved"
    env.graph.payloads["mfa_registration"]["value"] = [
        {"id": USER, "isAdmin": True, "isMfaRegistered": True, "isMfaCapable": True}
    ]
    collect(env)
    assert current(env)["findings"][0]["lifecycle_status"] == "resolved"


@pytest.mark.parametrize("revoked", [False, True])
def test_real_msal_refresh_with_mocked_oauth_http(env, monkeypatch, caplog, revoked):
    import requests

    monkeypatch.setattr(m365, "_sdk", lambda: msal)
    token_endpoint = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    cache = msal.SerializableTokenCache()
    info = base64.urlsafe_b64encode(json.dumps({"uid": USER, "utid": TENANT}).encode()).decode()
    cache.add(
        {
            "client_id": CLIENT,
            "scope": list(m365.SCOPES),
            "token_endpoint": token_endpoint,
            "response": {
                "access_token": "expired-access",
                "refresh_token": REFRESH,
                "expires_in": -3600,
                "client_info": info,
            },
        }
    )
    data = json.loads(env.token.read_text())
    data.update(account_id=USER + "." + TENANT, cache=cache.serialize())
    auth.atomic_private(env.token, data)
    calls = []

    def send(session, request, **kwargs):
        calls.append(request)
        assert kwargs["timeout"] == 10
        if request.method == "GET":
            assert (
                request.url
                == f"https://login.microsoftonline.com/{TENANT}/v2.0/.well-known/openid-configuration"
            )
            body = {
                "authorization_endpoint": token_endpoint.replace("token", "authorize"),
                "token_endpoint": token_endpoint,
                "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            }
            code = 200
        else:
            assert request.method == "POST" and request.url == token_endpoint
            assert "grant_type=refresh_token" in request.body
            assert REFRESH in request.body
            body = (
                {"error": "invalid_grant", "error_description": "revoked " + REFRESH}
                if revoked
                else {
                    "access_token": ACCESS,
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                    "scope": " ".join(m365.SCOPES),
                    "token_type": "Bearer",
                }
            )
            code = 400 if revoked else 200
        response = requests.Response()
        response.status_code = code
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    caplog.set_level(logging.DEBUG)
    bind(env)
    assert collect(env)["status"] == ("credential_expired" if revoked else "connected")
    assert [r.method for r in calls] == ["GET", "POST"]
    if not revoked:
        assert "rotated-refresh" in json.loads(env.token.read_text())["cache"]
    assert ACCESS not in caplog.text and REFRESH not in caplog.text


@pytest.fixture
def real_msal_device_cache(env, monkeypatch, capsys, caplog):
    import requests

    monkeypatch.setattr(m365, "_sdk", lambda: msal)
    authority = "https://login.microsoftonline.com/" + TENANT
    calls = []

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    def send(session, request, **kwargs):
        calls.append(request)
        assert kwargs["timeout"] == 10
        if request.method == "GET":
            assert request.url == authority + "/v2.0/.well-known/openid-configuration"
            body = {
                "authorization_endpoint": authority + "/oauth2/v2.0/authorize",
                "token_endpoint": authority + "/oauth2/v2.0/token",
                "device_authorization_endpoint": authority + "/oauth2/v2.0/devicecode",
                "issuer": authority + "/v2.0",
            }
        elif request.url.endswith("/devicecode"):
            assert request.method == "POST" and request.url.startswith(authority)
            assert "AuditLog.Read.All" in request.body and "Mail.Read" not in request.body
            body = {
                "user_code": "ABCD12345",
                "device_code": "private-device-code",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 5,
                "message": "private-device-code",
            }
        else:
            assert request.url == authority + "/oauth2/v2.0/token"
            assert "device_code" in request.body and "private-device-code" in request.body
            claims = {
                "aud": CLIENT,
                "iss": authority + "/v2.0",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "tid": TENANT,
                "oid": USER,
                "sub": USER,
            }
            body = {
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "expires_in": 3600,
                "scope": " ".join(m365.SCOPES),
                "token_type": "Bearer",
                "id_token": encode({"alg": "RS256"}) + "." + encode(claims) + ".synthetic",
                "client_info": encode({"uid": USER, "utid": TENANT}),
            }
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    caplog.set_level(logging.DEBUG)
    m365.authorize(
        m365.CONNECTOR, "native", tenant_id=TENANT, client_id=CLIENT, directory=env.private
    )
    return SimpleNamespace(calls=calls, output=capsys.readouterr().out + caplog.text)


def test_real_msal_device_code_http_and_cache(env, real_msal_device_cache, capsys, caplog):
    calls = real_msal_device_cache.calls
    stored = json.loads((env.private / "m365-native-token.json").read_text())
    assert stored["tenant_id"] == TENANT and stored["client_id"] == CLIENT
    assert REFRESH in stored["cache"] and ACCESS in stored["cache"]
    assert [r.method for r in calls] == ["GET", "POST", "POST"]
    material = m365.authenticate(m365.CONNECTOR, "native", directory=env.private)
    assert material.headers == {"Authorization": "Bearer " + ACCESS}
    assert material.private_data["tenant_id"] == TENANT
    # Construction does discovery; silent cache hits must not redeem/refresh a token.
    assert [r.method for r in calls] == ["GET", "POST", "POST", "GET"]
    assert not env.graph.calls
    output = real_msal_device_cache.output + capsys.readouterr().out + caplog.text
    for secret in [ACCESS, REFRESH, "private-device-code", USER]:
        assert secret not in output
    assert "ABCD12345" in output


@pytest.mark.parametrize("extra_scope", ["Mail.Read", "Files.Read"])
def test_real_msal_cache_hit_refuses_extra_scopes(env, real_msal_device_cache, caplog, extra_scope):
    path = env.private / "m365-native-token.json"
    stored = json.loads(path.read_text())
    cache = msal.SerializableTokenCache()
    cache.deserialize(stored["cache"])
    (entry,) = cache.search(cache.CredentialType.ACCESS_TOKEN)
    cache.modify(
        cache.CredentialType.ACCESS_TOKEN, entry, {"target": entry["target"] + " " + extra_scope}
    )
    auth.atomic_private(path, {**stored, "cache": cache.serialize()})
    with pytest.raises(ConnectorError) as exc:
        m365.authenticate(m365.CONNECTOR, "native", directory=env.private)
    assert exc.value.status == "permission_required"
    assert "exactly these Graph scopes" in exc.value.detail
    assert [r.method for r in real_msal_device_cache.calls] == ["GET", "POST", "POST", "GET"]
    assert not env.graph.calls
    assert ACCESS not in caplog.text and REFRESH not in caplog.text


@pytest.mark.parametrize(
    "change",
    [
        "missing_entry",
        "missing_target",
        "empty_target",
        "ambiguous",
        "secret",
        "home_account_id",
        "realm",
        "client_id",
        "environment",
    ],
)
def test_cache_hit_requires_scopes_for_the_returned_token_and_identity(
    env, real_msal_device_cache, monkeypatch, change
):
    stored = json.loads((env.private / "m365-native-token.json").read_text())
    serialized = json.loads(stored["cache"])
    entries = serialized["AccessToken"]
    (entry,) = entries.values()
    if change == "missing_entry":
        entries.clear()
    elif change == "missing_target":
        entry.pop("target")
    elif change == "empty_target":
        entry["target"] = ""
    elif change == "ambiguous":
        entries["second-entry-for-same-token"] = {**entry, "target": entry["target"] + " Mail.Read"}
    else:
        entry[change] = "different"
    cache = msal.SerializableTokenCache()
    cache.deserialize(json.dumps(serialized))
    # Model a cache result whose corresponding entry cannot be safely identified.
    app = SimpleNamespace(
        get_accounts=lambda: [{"home_account_id": stored["account_id"], "realm": TENANT}],
        acquire_token_silent_with_error=lambda *args: {
            "access_token": ACCESS,
            "token_source": "cache",
            "token_type": "Bearer",
        },
    )
    monkeypatch.setattr(m365, "_application", lambda *args: (app, cache))
    with pytest.raises(ConnectorError) as exc:
        m365.authenticate(m365.CONNECTOR, "native", directory=env.private)
    assert exc.value.status == "permission_required"
    assert not env.graph.calls


@pytest.mark.parametrize(
    "result, accepted",
    [
        ({"access_token": ACCESS}, True),
        ({"access_token": ACCESS, "token_source": "identity_provider"}, False),
        ({"access_token": ACCESS, "scope": " ".join(m365.SCOPES) + " Mail.Read"}, False),
    ],
)
def test_cache_scope_fallback_does_not_replace_network_scope_validation(
    env, real_msal_device_cache, monkeypatch, result, accepted
):
    stored = json.loads((env.private / "m365-native-token.json").read_text())
    cache = msal.SerializableTokenCache()
    cache.deserialize(stored["cache"])
    app = SimpleNamespace(
        get_accounts=lambda: [{"home_account_id": stored["account_id"], "realm": TENANT}],
        acquire_token_silent_with_error=lambda *args: result,
    )
    monkeypatch.setattr(m365, "_application", lambda *args: (app, cache))
    if accepted:
        material = m365.authenticate(m365.CONNECTOR, "native", directory=env.private)
        assert material.headers == {"Authorization": "Bearer " + ACCESS}
    else:
        with pytest.raises(ConnectorError) as exc:
            m365.authenticate(m365.CONNECTOR, "native", directory=env.private)
        assert exc.value.status == "permission_required"


def test_native_auth_flags_and_expired_device_challenge(env, capsys):
    assert m365.CONNECTOR.effective_auth_flow == "device_code"
    assert "--tenant-id" in m365.CONNECTOR.auth_command
    assert "--client-secrets" not in m365.CONNECTOR.auth_command
    env.microsoft.interactive = {"error": "expired_token", "error_description": REFRESH}
    assert (
        cli.main(
            [
                "connect",
                "m365",
                "auth",
                "--credential-ref",
                "new-ref",
                "--tenant-id",
                TENANT,
                "--client-id",
                CLIENT,
            ]
        )
        == 1
    )
    assert "new-ref" not in auth.references(env.private)
    output = capsys.readouterr()
    assert "credential_expired" in output.out + output.err
    assert REFRESH not in output.out + output.err


def test_graph_timeout_keeps_prior_evidence(env):
    def timeout(request):
        if request.url.path.endswith("/users"):
            raise httpx.ReadTimeout("private transport message " + ACCESS)

    env.graph.override = timeout
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    assert payloads(env)["users"]["status"] == "provider_unavailable"
    assert payloads(env)["domains"]["data"]["domains"][0]["verified"] is True
    assert ACCESS not in json.dumps(current(env))


def test_run_connect_uses_same_collection_and_report(env, monkeypatch, tmp_path):
    from ams_assess import runner
    from ams_assess.connectors.adapter import ConnectorModule
    from ams_assess.models import Context, create_run_dir

    bind(env)
    monkeypatch.setattr(runner, "REGISTRY", {"m365": ConnectorModule(m365.CONNECTOR)})
    monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
    context = Context(
        "synthetic",
        "Synthetic",
        "https://example.test",
        {"seo"},
        {"m365"},
        {},
        create_run_dir(env.root, "synthetic"),
        tmp_path,
        store_root=env.root,
    )
    result = runner.run(context, no_ai=True, formats={"md"})
    assert result["connector_results"]["m365"]["status"] == "connected"
    assert current(env)["connectors"]["m365"]["status"] == "connected"
    assert "Microsoft reports administrators" in (context.run_dir / "report.md").read_text()


@pytest.mark.parametrize("body", [{}, {"value": None}, {"value": [{}]}])
def test_malformed_data_is_not_empty_or_insecure(env, body):
    env.graph.payloads["mfa_registration"] = body
    bind(env)
    collect(env)
    assert not current(env)["findings"]
    data = payloads(env)["mfa_registration"]
    assert data["status"] == "failed" or data["data"]["registered_unknown"] == 1


def test_page_cap_preserves_coverage(env):
    def pages(request):
        if request.url.path.endswith("/users"):
            page = int(request.url.params.get("$skip", "0"))
            return httpx.Response(
                200,
                json={
                    "value": env.graph.payloads["users"]["value"],
                    "@odata.nextLink": m365.GRAPH + "/users?$skip=" + str(page + 1),
                },
            )

    env.graph.override = pages
    bind(env)
    collect(env)
    assert sum(r.url.path.endswith("/users") for r in env.graph.calls) == 5
    assert payloads(env)["users"]["coverage"] == {"rows_returned": 10, "truncated": True}
