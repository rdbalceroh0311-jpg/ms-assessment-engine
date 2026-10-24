"""Synthetic Admin SDK HTTP only. No customer data or live provider requests."""

import copy
import json
import logging
import stat
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from ams_assess import ai, cli, store
from ams_assess.connectors import auth, service, workspace
from ams_assess.connectors.core import AuthMaterial, ConnectorContext, ConnectorError, Period
from ams_assess.connectors.http import ReadClient
from ams_assess.models import Evidence
from ams_assess.report import render_customer
from ams_assess.ui import LocalServer

CID = "C01synthetic"
DOMAIN = "example.test"
RESOURCE = CID + "/" + DOMAIN
ACCESS = "synthetic-workspace-access-never-print"
REFRESH = "synthetic-workspace-refresh-never-print"
SECRET = "synthetic-workspace-client-secret-never-print"


def user(identifier="10001", **changes):
    return {
        "id": identifier,
        "customerId": CID,
        "suspended": False,
        "archived": False,
        "isAdmin": False,
        "isDelegatedAdmin": False,
        "isEnrolledIn2Sv": True,
        "isEnforcedIn2Sv": True,
        "primaryEmail": "private-person@example.test",
        "name": {"fullName": "Private Person"},
        "recoveryPhone": "+61400000123",
        "password": SECRET,
        **changes,
    }


class Provider:
    def __init__(self):
        self.calls = []
        self.override = lambda request: None
        self.users = [
            user("10001", isAdmin=True, isEnrolledIn2Sv=False),
            user("10002", isDelegatedAdmin=True, suspended=True, isEnforcedIn2Sv=False),
            user("10003", archived=True, isEnrolledIn2Sv=False, isEnforcedIn2Sv=False),
            user("10004"),
        ]
        self.groups = [{"id": "20001", "email": "private-group@example.test"}]
        self.roles = [
            {
                "roleAssignmentId": "30001",
                "roleId": "40001",
                "scopeType": "CUSTOMER",
                "assigneeType": "USER",
                "assignedTo": "10001",
            },
            {
                "roleAssignmentId": "30002",
                "roleId": "40002",
                "scopeType": "ORG_UNIT",
                "assigneeType": "GROUP",
                "assignedTo": "20001",
            },
        ]

    def __call__(self, request):
        self.calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "admin.googleapis.com"
        assert request.headers["Authorization"] == "Bearer " + ACCESS
        assert max(request.extensions["timeout"].values()) <= 10
        response = self.override(request)
        if response is not None:
            return response
        path = request.url.path
        if "/customers/" in path:
            assert path.endswith(("/" + CID, "/my_customer"))
            return httpx.Response(
                200,
                json={
                    "id": CID,
                    "customerDomain": DOMAIN,
                    "alternateEmail": "billing@example.test",
                },
            )
        if path.endswith("/domains"):
            assert "/" + CID + "/" in path
            return httpx.Response(
                200,
                json={
                    "domains": [
                        {"domainName": DOMAIN, "verified": True, "isPrimary": True},
                        {"domainName": "secondary.test", "verified": False, "isPrimary": False},
                    ]
                },
            )
        if path.endswith("/users"):
            assert request.url.params["customer"] == CID
            assert request.url.params["viewType"] == "admin_view"
            assert (
                request.url.params["fields"] == "nextPageToken,users(" + workspace.USER_FIELDS + ")"
            )
            return httpx.Response(200, json={"users": self.users})
        if path.endswith("/groups"):
            assert request.url.params["customer"] == CID
            assert request.url.params["fields"] == "nextPageToken,groups(id)"
            return httpx.Response(200, json={"groups": self.groups})
        if path.endswith("/roleassignments"):
            assert "/" + CID + "/" in path
            assert "assignedTo" not in request.url.params["fields"]
            return httpx.Response(200, json={"items": self.roles})
        assert "/usage/dates/" in path
        assert request.url.params["customerId"] == CID
        assert request.url.params["parameters"] == ",".join(workspace.USAGE_METRICS)
        return httpx.Response(
            200,
            json={
                "usageReports": [
                    {
                        "date": path.rsplit("/", 1)[1],
                        "entity": {"customerId": CID},
                        "parameters": [
                            {"name": name, "intValue": str(index + 10)}
                            for index, name in enumerate(workspace.USAGE_METRICS)
                        ],
                    }
                ]
            },
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    root, private = tmp_path / "customers", tmp_path / ".config/ams-assess"
    private.mkdir(parents=True, mode=0o700)
    token = private / "workspace-synthetic-token.json"
    auth.atomic_private(
        token,
        {
            "token": ACCESS,
            "refresh_token": REFRESH,
            "client_secret": SECRET,
            "client_id": "synthetic.apps.googleusercontent.com",
            "type": "authorized_user",
            "scopes": list(workspace.SCOPES),
            "expiry": (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    auth.register_reference("synthetic", workspace.CONNECTOR, file=token, directory=private)
    monkeypatch.setattr(service, "config_dir", lambda: private)
    monkeypatch.setattr(auth, "config_dir", lambda: private)
    monkeypatch.setattr(cli, "data_root", lambda: root)
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", lambda seconds: None)
    provider = Provider()
    monkeypatch.setattr(
        service,
        "ReadClient",
        lambda *a, transport=None, **kw: ReadClient(
            *a, transport=httpx.MockTransport(provider), **kw
        ),
    )
    store.set_profile(
        root,
        "synthetic",
        {
            "name": "Synthetic Customer",
            "websites": ["https://example.test"],
        },
    )
    return SimpleNamespace(root=root, private=private, token=token, provider=provider)


def bind(env, resource=RESOURCE):
    return service.set_binding(env.root, "synthetic", "workspace", "synthetic", resource)


def collect(env, **kwargs):
    return service.execute(env.root, "synthetic", "workspace", collect=True, **kwargs)


def current(env):
    return store.load(env.root, "synthetic")


def payload(env, operation):
    return next(
        e["data"]
        for e in reversed(current(env)["evidence"])
        if e["source"] == "workspace" and e["data"]["operation"] == operation
    )


@contextmanager
def ui_server(root):
    with LocalServer(root, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=server.origin, trust_env=False) as client:
                yield server, client
        finally:
            server.shutdown()
            thread.join(5)


def test_success_distinguishes_enrolment_enforcement_and_reported_protection(env):
    bind(env)
    state = collect(env, start="2026-09-01", end="2026-09-28")
    assert state["status"] == "connected"
    assert {row["endpoint"] for row in state["coverage"]} == {
        "customer",
        "domains",
        "users",
        "groups",
        "role_assignments",
        "usage_2sv",
    }
    users = payload(env, "users")["data"]
    assert users["statuses"] == {"active": 2, "suspended": 1, "archived": 1, "unknown": 0}
    assert users["admins"] == {"super": 1, "delegated": 1, "any": 2, "unknown": 0}
    assert users["enrolment"] == {"yes": 2, "no": 2, "unknown": 0}
    assert users["enforcement"] == {"yes": 2, "no": 2, "unknown": 0}
    assert users["admins_without_enrolment"][0]["role"] == "super admin"
    assert users["admins_without_enrolment"][0]["id"].startswith("admin-")
    assert users["suspended_admins"] == 1
    assert payload(env, "groups")["data"]["count"] == 1
    assert payload(env, "groups")["data"]["externally_shared_count"] is None
    assert payload(env, "role_assignments")["data"]["by_scope"] == {"CUSTOMER": 1, "ORG_UNIT": 1}
    usage = payload(env, "usage_2sv")["data"]
    assert usage["date"] == "2026-09-28"
    assert usage["metrics"]["accounts:num_users_2sv_protected"] == 14
    assert "protected" not in users
    for evidence in current(env)["evidence"]:
        if evidence["source"] != "workspace":
            continue
        data = evidence["data"]
        assert data["connector"] == "workspace" and data["resource_id"] == RESOURCE
        assert data["collected_at"] and data["endpoint"].startswith("https://admin.googleapis.com/")
        assert data["period"] == {"start": "2026-09-01", "end": "2026-09-28"}
        assert "coverage" in data
    assert len(current(env)["findings"]) == 3
    assert all(f["confidence"] == "confirmed" for f in current(env)["findings"])
    assert all(f["ams_service"] == "Managed IT & Technology" for f in current(env)["findings"])


def test_listing_resolves_my_customer_without_binding_or_assessment(env):
    before = copy.deepcopy(current(env))
    result = service.list_resources("workspace", "synthetic")
    assert result.status == "connected"
    assert result.data["rows"] == [{"id": RESOURCE, "label": DOMAIN}]
    assert len(env.provider.calls) == 2
    assert env.provider.calls[0].url.path.endswith("/customers/my_customer")
    assert current(env) == before
    assert workspace.normalize_resource(CID + "/EXAMPLE.TEST.") == RESOURCE


@pytest.mark.parametrize(
    "value",
    [
        "my_customer",
        "my_customer/example.test",
        CID,
        CID + "/other.test/path",
        CID + "/https://example.test",
        CID + "/person@example.test",
        CID + "/a..test",
        CID + "/evil.test?token=x",
        CID + "/evil.test#fragment",
        "../../users/example.test",
    ],
)
def test_invalid_resource_refused_before_auth_or_write(env, value):
    before = current(env)
    with pytest.raises(ValueError):
        bind(env, value)
    assert current(env) == before
    assert not env.provider.calls


@pytest.mark.parametrize(
    "problem", ["customer_id", "primary", "domain", "secondary", "unverified", "empty"]
)
def test_customer_domain_mismatch_refuses_all_collection(env, problem):
    bind(env)

    def override(request):
        if "/customers/" in request.url.path and problem in {"customer_id", "primary"}:
            return httpx.Response(
                200,
                json={
                    "id": "C02other" if problem == "customer_id" else CID,
                    "customerDomain": "wrong.test" if problem == "primary" else DOMAIN,
                },
            )
        if request.url.path.endswith("/domains"):
            return httpx.Response(
                200,
                json={
                    "domains": []
                    if problem == "empty"
                    else [
                        {
                            "domainName": "wrong.test" if problem == "domain" else DOMAIN,
                            "isPrimary": problem != "secondary",
                            "verified": problem != "unverified",
                        }
                    ]
                },
            )

    env.provider.override = override
    state = collect(env)
    assert state["status"] in {"not_found", "permission_required"}
    assert "last_successful_test" not in state
    assert len(env.provider.calls) <= 2
    assert not current(env)["findings"]


def test_binding_isolation_canonical_domain_and_force_reason(env):
    bind(env, CID + "/EXAMPLE.TEST.")
    with pytest.raises(ValueError, match="different customer"):
        service.set_binding(env.root, "other", "workspace", "other-ref", RESOURCE)
    with pytest.raises(ValueError, match="requires"):
        service.set_binding(env.root, "other", "workspace", "other-ref", RESOURCE, force=True)
    service.set_binding(
        env.root,
        "other",
        "workspace",
        "other-ref",
        RESOURCE,
        force=True,
        reason="Synthetic shared tenant approved",
    )
    other = store.load(env.root, "other")
    assert other is not None
    history = other["connector_history"][-1]
    assert history["forced"] and history["other_customers"] == ["synthetic"]
    assert history["reason"] == "Synthetic shared tenant approved"
    collect(env)
    assert not (store.load(env.root, "other") or {})["evidence"]


def test_oauth_uses_exact_six_readonly_scopes_and_private_storage(env, monkeypatch, capsys):
    from google.oauth2.credentials import Credentials

    client = env.private / "desktop.json"
    auth.atomic_private(
        client,
        {
            "installed": {
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
    )
    observed = {}

    class Flow:
        def run_local_server(self, **kwargs):
            observed.update(kwargs)
            return Credentials(
                ACCESS,
                refresh_token=REFRESH,
                client_id="synthetic",
                client_secret=SECRET,
                token_uri="https://oauth2.googleapis.com/token",
                scopes=workspace.SCOPES,
            )

    def flow(path, scopes):
        assert scopes == list(workspace.SCOPES)
        assert len(scopes) == 6 and all(s.endswith(".readonly") for s in scopes)
        assert not any(service in s for s in scopes for service in ("gmail", "drive", "calendar"))
        return Flow()

    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", flow)
    assert (
        cli.main(
            [
                "connect",
                "workspace",
                "auth",
                "--credential-ref",
                "new-ref",
                "--client-secrets",
                str(client),
            ]
        )
        == 0
    )
    assert observed["include_granted_scopes"] == "false"
    assert observed["timeout_seconds"] == 180
    assert stat.S_IMODE(env.private.stat().st_mode) == 0o700
    assert stat.S_IMODE((env.private / "workspace-new-ref-token.json").stat().st_mode) == 0o600
    assert not current(env).get("connectors")
    assert ACCESS not in capsys.readouterr().out


@pytest.mark.parametrize("change", ["extra", "missing", "wrong_connector"])
def test_auth_refuses_wider_missing_or_other_connector_credentials(env, change):
    data = json.loads(env.token.read_text())
    if change == "extra":
        data["scopes"].append("https://www.googleapis.com/auth/admin.directory.user")
    elif change == "missing":
        data["scopes"].pop()
    else:
        refs = auth.references(env.private)
        refs["synthetic"]["connector"] = "ga4"
        auth.atomic_private(env.private / "connectors.json", refs)
    auth.atomic_private(env.token, data)
    bind(env)
    assert collect(env)["status"] == (
        "credential_missing" if change == "wrong_connector" else "permission_required"
    )
    assert not env.provider.calls


@pytest.mark.parametrize("revoked", [False, True])
def test_expired_credentials_use_mocked_oauth_refresh(env, monkeypatch, revoked):
    data = json.loads(env.token.read_text())
    data["expiry"] = "2020-01-01T00:00:00Z"
    auth.atomic_private(env.token, data)
    requests = []

    def oauth(request):
        requests.append(request)
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(
            400 if revoked else 200,
            json={"error": "invalid_grant"}
            if revoked
            else {"access_token": ACCESS, "expires_in": 3600, "scope": " ".join(workspace.SCOPES)},
        )

    def refresh(url, method, body, headers, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(oauth)) as client:
            response = client.request(method, url, content=body, headers=headers)
        return SimpleNamespace(
            status=response.status_code, data=response.content, headers=response.headers
        )

    monkeypatch.setattr(auth, "BoundedRequest", lambda: refresh)
    bind(env)
    assert collect(env)["status"] == ("credential_expired" if revoked else "connected")
    assert len(requests) == 1
    if revoked:
        assert not env.provider.calls
    else:
        assert json.loads(env.token.read_text())["expiry"] != data["expiry"]


@pytest.mark.parametrize(
    "code,reason,status",
    [
        (401, "", "credential_expired"),
        (403, "forbidden", "permission_required"),
        (403, "SERVICE_DISABLED", "api_not_enabled"),
        (403, "API_NOT_APPROVED", "approval_required"),
        (404, "", "not_found"),
        (400, "", "failed"),
        (429, "", "provider_unavailable"),
        (503, "", "provider_unavailable"),
    ],
)
def test_initial_failure_distinguishes_status_and_redacts_provider_error(env, code, reason, status):
    bind(env)
    env.provider.override = lambda r: httpx.Response(
        code,
        json={
            "error": {
                "message": ACCESS + " private-person@example.test",
                "errors": [{"reason": reason}],
            }
        },
    )
    state = collect(env)
    assert state["status"] == status
    assert len(env.provider.calls) == (3 if code in {429, 503} else 1)
    assert not current(env)["findings"]
    assert ACCESS not in json.dumps(current(env))
    if status == "permission_required":
        assert "Domain Settings" in state["detail"]


def test_empty_collections_are_not_permission_errors_or_security_passes(env):
    env.provider.users = env.provider.groups = env.provider.roles = []
    bind(env)
    assert collect(env)["status"] == "connected"
    assert not current(env)["findings"]
    assert payload(env, "users")["coverage"]["rows_returned"] == 0
    assert payload(env, "groups")["data"]["count"] == 0
    assert payload(env, "role_assignments")["data"]["count"] == 0
    assert not current(env)["runs"][-1].get("outcomes")


@pytest.mark.parametrize(
    "field", ["isEnrolledIn2Sv", "isEnforcedIn2Sv", "isAdmin", "suspended", "archived"]
)
def test_missing_flags_are_unknown_and_cannot_prove_a_pass(env, field):
    env.provider.users = [user(isAdmin=True)]
    del env.provider.users[0][field]
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    data = payload(env, "users")["data"]
    assert data["users_with_missing_flags"] == 1
    if field == "isEnrolledIn2Sv":
        assert data["enrolment"] == {"yes": 0, "no": 0, "unknown": 1}
        assert data["admins_without_enrolment"] == []
    assert not current(env)["findings"]


@pytest.mark.parametrize(
    "suffix,key,field",
    [
        ("/users", "users", "users"),
        ("/groups", "groups", "groups"),
        ("/roleassignments", "role_assignments", "items"),
    ],
)
def test_pagination_preserves_first_page_after_second_page_permission_failure(
    env, suffix, key, field
):
    bind(env)
    rows = {
        "users": env.provider.users[:1],
        "groups": env.provider.groups,
        "items": env.provider.roles[:1],
    }[field]

    def override(request):
        if request.url.path.endswith(suffix):
            if request.url.params.get("pageToken"):
                return httpx.Response(403, json={"error": {}})
            return httpx.Response(200, json={field: rows, "nextPageToken": "synthetic-page"})

    env.provider.override = override
    state = collect(env)
    assert state["status"] == "connected_partial"
    endpoint = payload(env, key)
    assert endpoint["status"] == "permission_required"
    assert endpoint["coverage"] == {"rows_returned": 1, "truncated": True}
    assert workspace.PERMISSIONS[key] in state["detail"]
    assert payload(env, "usage_2sv")["status"] == "connected"


@pytest.mark.parametrize("mode", ["complete", "cap", "repeat", "overflow"])
def test_user_pagination_and_caps(env, monkeypatch, mode):
    monkeypatch.setattr(
        service,
        "ConnectorContext",
        lambda *a, **kw: ConnectorContext(
            *a,
            **kw,
            max_pages=2,
            page_size=2,
        ),
    )

    def override(request):
        if not request.url.path.endswith("/users"):
            return None
        second = bool(request.url.params.get("pageToken"))
        body = {"users": [user("11001" if not second else "11002")]}
        if mode == "overflow":
            body["users"] = [user(str(i + 12000)) for i in range(5)]
        elif not second or mode in {"cap", "repeat"}:
            body["nextPageToken"] = "next" if not second or mode == "repeat" else "last"
        return httpx.Response(200, json=body)

    env.provider.override = override
    bind(env)
    state = collect(env)
    endpoint = payload(env, "users")
    assert state["status"] == ("connected" if mode == "complete" else "connected_partial")
    assert endpoint["coverage"]["truncated"] == (mode != "complete")
    assert endpoint["coverage"]["rows_returned"] == (4 if mode == "overflow" else 2)
    assert len([r for r in env.provider.calls if r.url.path.endswith("/users")]) <= 2


@pytest.mark.parametrize("problem", ["empty", "missing", "warning", "date", "customer", "invalid"])
def test_usage_missing_warning_or_mismatch_never_inferred_from_directory(env, problem):
    def override(request):
        if "/usage/dates/" not in request.url.path:
            return None
        day = request.url.path.rsplit("/", 1)[1]
        parameters = [{"name": name, "intValue": "2"} for name in workspace.USAGE_METRICS]
        if problem == "missing":
            parameters.pop()
        if problem == "invalid":
            parameters[0]["intValue"] = "-1"
        body = {
            "usageReports": []
            if problem == "empty"
            else [
                {
                    "date": "2020-01-01" if problem == "date" else day,
                    "entity": {"customerId": "C02other" if problem == "customer" else CID},
                    "parameters": parameters,
                }
            ]
        }
        if problem == "warning":
            body["warnings"] = [{"code": "PARTIAL_DATA_AVAILABLE", "message": SECRET}]
        return httpx.Response(200, json=body)

    env.provider.override = override
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    endpoint = payload(env, "usage_2sv")
    assert (
        not endpoint["data"]["metrics"]
        if problem in {"empty", "date", "customer", "invalid"}
        else True
    )
    if problem in {"empty", "missing"}:
        assert endpoint["data"]["unavailable_metrics"]
    assert SECRET not in json.dumps(current(env))


def test_usage_pagination_merges_metrics_without_summing_or_retaining_entities(env):
    def override(request):
        if "/usage/dates/" not in request.url.path:
            return None
        second = bool(request.url.params.get("pageToken"))
        metrics = workspace.USAGE_METRICS[3:] if second else workspace.USAGE_METRICS[:3]
        return httpx.Response(
            200,
            json={
                **({} if second else {"nextPageToken": "usage-next"}),
                "usageReports": [
                    {
                        "date": request.url.path.rsplit("/", 1)[1],
                        "entity": {"customerId": CID, "userEmail": "private@example.test"},
                        "parameters": [{"name": name, "intValue": "1"} for name in metrics],
                    }
                ],
            },
        )

    env.provider.override = override
    bind(env)
    assert collect(env)["status"] == "connected"
    assert payload(env, "usage_2sv")["data"]["metrics"] == dict.fromkeys(workspace.USAGE_METRICS, 1)
    assert payload(env, "usage_2sv")["coverage"]["rows_returned"] == 2


@pytest.mark.parametrize("failure", [429, 500, "timeout"])
def test_retry_recovers_then_preserves_independent_endpoints_on_outage(env, failure):
    count = 0

    def override(request):
        nonlocal count
        if request.url.path.endswith("/groups"):
            count += 1
            if count == 2:
                return None
            if failure == "timeout":
                raise httpx.ReadTimeout("private error " + SECRET, request=request)
            return httpx.Response(failure, json={})

    env.provider.override = override
    bind(env)
    assert collect(env)["status"] == "connected"
    assert count == 2
    assert collect(env)["status"] == "connected_partial"
    assert count == 5
    assert payload(env, "groups")["status"] == "provider_unavailable"
    assert payload(env, "users")["status"] == "connected"


@pytest.mark.parametrize(
    "method,url",
    [
        (method, workspace.DIRECTORY + path)
        for method in ("POST", "PUT", "PATCH", "DELETE")
        for path in ("users", "groups", "customers/" + CID, "customer/" + CID + "/roleassignments")
    ]
    + [
        ("GET", workspace.DIRECTORY + "users/10001"),
        ("GET", workspace.DIRECTORY + "users/10001/tokens"),
        ("POST", workspace.DIRECTORY + "users/10001/twoStepVerification/turnOff"),
        ("GET", workspace.DIRECTORY + "users?customer=" + CID),
        ("GET", "https://admin.googleapis.com.evil.test/admin/directory/v1/users"),
        ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages"),
        ("GET", "https://www.googleapis.com/groups/v1/groups/private@example.test"),
    ],
)
def test_read_allowlist_refuses_before_http(method, url):
    client = ReadClient(
        workspace.CONNECTOR,
        AuthMaterial(),
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Forbidden request reached HTTP")
        ),
    )
    try:
        with pytest.raises(ConnectorError, match="allowlist"):
            client.request(method, url)
    finally:
        client.close()


@pytest.mark.parametrize(
    "status", ["connected", "permission_required", "connected_partial", "api_not_enabled"]
)
def test_cli_ui_report_and_register_status_consistency(env, capsys, status):
    def override(request):
        if status == "connected":
            return None
        if status == "connected_partial" and not request.url.path.endswith("/groups"):
            return None
        return httpx.Response(
            403,
            json={
                "error": {
                    "errors": [
                        {
                            "reason": "SERVICE_DISABLED"
                            if status == "api_not_enabled"
                            else "forbidden",
                        }
                    ]
                }
            },
        )

    env.provider.override = override
    with ui_server(env.root) as (server, client):
        form = {
            "customer": "synthetic",
            "connector": "workspace",
            "csrf": server.csrf,
            "credential_ref": "synthetic",
            "resource": RESOURCE,
        }
        for action in ("bind", "test", "collect"):
            response = client.post(
                server.url("/connect"),
                data={**form, "action": action},
                headers={"Origin": server.origin},
            )
            assert response.status_code == 303
        assert current(env)["connectors"]["workspace"]["status"] == status
        assert cli.main(["connectors", "--customer", "synthetic"]) == 0
        output = capsys.readouterr().out
        html = client.get(server.url(customer="synthetic")).text
        render_customer(env.root, "synthetic", {"md"})
        report = (env.root / "synthetic/report.md").read_text().replace("\\_", "_")
        assert all(status in text and RESOURCE in text for text in (output, html, report))
        assert "## 7. Connected business-system findings" in report
        if status == "connected":
            assert "Workspace administrators without 2SV enrolment" in report.split("## 7.")[1]
    assert all(
        e["integration"] == status
        for e in current(env)["systems_register"]["entries"]
        if e["platform"] == "workspace"
    )


def test_personal_data_and_secrets_never_reach_persistence_cli_ui_report_or_ai(env, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    bind(env)
    collect(env)
    snapshot = current(env)
    render_customer(env.root, "synthetic", {"md"})
    cli.main(["connectors", "--customer", "synthetic"])
    with ui_server(env.root) as (server, client):
        html = client.get(server.url(customer="synthetic")).text
    output = json.dumps(snapshot) + html + (env.root / "synthetic/report.md").read_text()
    output += caplog.text + capsys.readouterr().out
    for value in (
        ACCESS,
        REFRESH,
        SECRET,
        "private-person@",
        "private-group@",
        "Private Person",
        "+61400000123",
        '"10001"',
        '"20001"',
        "billing@example.test",
    ):
        assert value not in output
    compact = ai.compact_evidence([Evidence.model_validate(e) for e in snapshot["evidence"]])
    ai_output = json.dumps(compact)
    assert "rows_returned" in ai_output
    assert "admin-" not in ai_output and "admins_without_enrolment" not in ai_output
    assert "metrics" not in ai_output


def test_only_complete_nonempty_recheck_resolves_and_recurrence_reopens(env):
    bind(env)
    collect(env)
    original = {f["id"] for f in current(env)["findings"]}
    env.provider.users = []
    collect(env)
    assert not any(f["lifecycle_status"] == "resolved" for f in current(env)["findings"])
    env.provider.users = [user(isEnrolledIn2Sv=None)]
    collect(env)
    assert not any(f["lifecycle_status"] == "resolved" for f in current(env)["findings"])
    env.provider.users = [user()]
    collect(env)
    assert not any(f["lifecycle_status"] == "resolved" for f in current(env)["findings"])
    # Complete results for a smaller OU/population cannot clear the original finding.
    env.provider.users = [user(row["id"]) for row in Provider().users]
    collect(env)
    assert all(f["lifecycle_status"] == "resolved" for f in current(env)["findings"])
    env.provider.users = Provider().users
    collect(env)
    findings = current(env)["findings"]
    assert {f["id"] for f in findings} == original
    assert all(f["lifecycle_status"] == "current" for f in findings)


def test_missing_credential_unbound_and_invalid_period_never_make_reads(env):
    assert collect(env)["status"] == "not_configured"
    bind(env)
    env.token.unlink()
    assert collect(env)["status"] == "credential_missing"
    before = current(env)
    with pytest.raises(ValueError):
        collect(env, days=367)
    assert current(env) == before
    assert not env.provider.calls


def test_no_redirects_or_reads_after_deadline(env):
    bind(env)
    env.provider.override = lambda request: httpx.Response(
        302, headers={"Location": "https://evil.test"}
    )
    assert collect(env)["status"] == "failed"
    assert len(env.provider.calls) == 1
    env.provider.calls.clear()
    assert collect(env, seconds=0)["status"] == "provider_unavailable"
    assert not env.provider.calls


def test_direct_collection_requires_verified_binding():
    ctx = ConnectorContext(None, RESOURCE, Period.make())
    with pytest.raises(ConnectorError, match="Verify"):
        workspace.collect(ctx)


@pytest.mark.parametrize("problem", ["customer", "boolean", "duplicate"])
def test_bad_user_rows_cannot_inflate_counts_or_cross_tenants(env, problem):
    first = user("10001", isAdmin=True, isEnrolledIn2Sv=False)
    second = user("10002", isAdmin=True, isEnrolledIn2Sv=False)
    if problem == "customer":
        second["customerId"] = "C02other"
    elif problem == "boolean":
        second["isEnrolledIn2Sv"] = "false"
    else:
        second["id"] = first["id"]
    env.provider.users = [first, second]
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    users = payload(env, "users")
    assert users["status"] == ("not_found" if problem == "customer" else "failed")
    assert users["coverage"] == {"rows_returned": 1, "truncated": True}
    assert len(users["data"]["admins_without_enrolment"]) == 1
    assert users["data"]["admins"]["any"] == 1
    assert "C02other" not in json.dumps(current(env))


def test_second_page_overflow_cannot_look_complete(env, monkeypatch):
    monkeypatch.setattr(
        service,
        "ConnectorContext",
        lambda *a, **kw: ConnectorContext(
            *a,
            **kw,
            max_pages=2,
            page_size=2,
        ),
    )

    def override(request):
        if request.url.path.endswith("/users"):
            if request.url.params.get("pageToken"):
                return httpx.Response(200, json={"users": [user(str(i + 11000)) for i in range(4)]})
            return httpx.Response(200, json={"users": [user()], "nextPageToken": "next"})

    env.provider.override = override
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    assert payload(env, "users")["coverage"] == {"rows_returned": 4, "truncated": True}


def test_period_uses_complete_reporting_days_even_at_utc_date_boundary(env, monkeypatch):
    from ams_assess.connectors import core

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 24, 3, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(core, "datetime", Clock)
    bind(env)
    state = collect(env, days=1)
    assert state["status"] == "connected"
    assert state["period"] == {"start": "2026-10-22", "end": "2026-10-22"}
    assert payload(env, "usage_2sv")["data"]["date"] == "2026-10-22"
    env.provider.calls.clear()
    state = collect(env, start="2026-10-23", end="2026-10-23")
    assert state["status"] == "not_configured"
    assert "UTC-08:00" in state["detail"]
    assert not env.provider.calls
