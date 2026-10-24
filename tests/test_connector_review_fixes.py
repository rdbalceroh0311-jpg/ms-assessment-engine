"""Cross-vendor review regressions: lifecycle, stages, quota, auth and budgets."""

import json
from dataclasses import replace

import httpx
import pytest
from test_connectors import bind, collect, current, ui_server
from test_connectors import env as env
from test_lifecycle import merge as public_run

from ams_assess import cli, store
from ams_assess.connectors import commands, ga4, service
from ams_assess.connectors.core import AuthMaterial, ConnectorError
from ams_assess.connectors.errors import error_body
from ams_assess.connectors.http import ReadClient
from ams_assess.connectors.privacy import redact
from ams_assess.report import render_customer


@pytest.mark.parametrize("action", ["test", "collect"])
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("entrypoint", ["cli", "ui"])
def test_connector_operation_preserves_public_findings_ai_and_stage(env, action, bound, entrypoint):
    snapshot = public_run(env.root, category="cro_ux")
    old = snapshot["findings"][0]
    snapshot["runs"][-1]["ai"]["status"] = "ok"
    snapshot["runs"][-1]["ai"]["recommendations"] = [old["id"]]
    assert old["id"] in store.report_data(snapshot)[0]["ai"]["recommendations"]
    store.save(env.root, "synthetic", snapshot)
    if bound:
        bind(env)
    if entrypoint == "cli":
        assert cli.main(["connect", "ga4", action, "--customer", "synthetic"]) == (
            0 if bound else 1
        )
    else:
        with ui_server(env.root) as (server, client):
            response = client.post(
                server.url("/connect"),
                headers={"Origin": server.origin},
                data={
                    "customer": "synthetic",
                    "connector": "ga4",
                    "action": action,
                    "csrf": server.csrf,
                },
            )
            assert response.status_code == 303
    saved = current(env)
    assert next(f for f in saved["findings"] if f["id"] == old["id"]) == old
    assert ("connected" in saved["stages_assessed"]) is bound
    assert saved["runs"][-1]["stages"] == (["connected"] if bound else [])
    assert all(o["module"] != "siteone" for o in saved["runs"][-1]["outcomes"])
    data, _, _ = store.report_data(saved)
    assert old["id"] in data["ai"]["recommendations"]
    assert ("connected evidence" in data["stages"]) is bound
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text()
    assert old["title"] in report
    assert "Previously observed — not rechecked" not in report


def test_connector_recheck_preserves_other_connected_findings_and_updates_own(env, monkeypatch):
    bind(env)
    collect(env)
    old = current(env)["findings"][0]
    snapshot = current(env)
    other = {**old, "id": "other-connector-issue", "check_key": "gsc:sample"}
    # A comparison owned by another source can cite GA4 alongside its own evidence.
    own_evidence = next(e for e in snapshot["evidence"] if e["source"] == "ga4")
    other_evidence = {**own_evidence, "id": "ev-gsc-r1-001", "source": "gsc"}
    snapshot["evidence"].append(other_evidence)
    other["evidence_ids"] = [*old["evidence_ids"], other_evidence["id"]]
    snapshot["findings"].append(other)
    store.save(env.root, "synthetic", snapshot)
    # Failed recheck touches GA4 only. A later passing collection still resolves it.
    env.provider.override = lambda request: httpx.Response(403)
    assert collect(env)["status"] == "permission_required"
    saved = current(env)
    assert next(f for f in saved["findings"] if f["id"] == other["id"]) == other
    assert (
        next(f for f in saved["findings"] if f["id"] == old["id"])["lifecycle_status"]
        == "not_rechecked"
    )
    env.provider.override = lambda request: None
    env.provider.recorded["form_submit"] = 3
    collect(env)
    saved = current(env)
    assert (
        next(f for f in saved["findings"] if f["id"] == old["id"])["lifecycle_status"] == "resolved"
    )
    assert next(f for f in saved["findings"] if f["id"] == other["id"]) == other


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_failed_attempt_does_not_claim_connected_stage(env, code):
    bind(env)
    env.provider.override = lambda request: httpx.Response(code)
    collect(env)
    assert "connected" not in current(env)["stages_assessed"]


def test_partial_connection_records_connected_stage(env):
    bind(env)
    env.provider.override = lambda request: (
        httpx.Response(500) if request.method == "POST" else None
    )
    assert collect(env)["status"] == "connected_partial"
    assert "connected" in current(env)["stages_assessed"]


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
@pytest.mark.parametrize("recover", [False, True])
def test_legacy_google_403_quota_retries_before_permission_classification(env, reason, recover):
    bind(env)
    env.provider.override = lambda request: (
        httpx.Response(403, json={"error": {"errors": [{"reason": reason}]}})
        if not recover or len(env.provider.calls) < 3
        else None
    )
    state = service.execute(env.root, "synthetic", "ga4")
    assert state["status"] == ("connected" if recover else "rate_limited")
    assert len(env.provider.calls) == 3


@pytest.mark.parametrize("counter", ["tokensPerDay", "tokensPerMinute"])
def test_exhausted_budget_is_rate_limited_and_numeric_quota_survives_redaction(env, counter):
    bind(env)
    env.provider.override = lambda request: (
        httpx.Response(
            200,
            json={
                "rows": [],
                "rowCount": 0,
                "propertyQuota": {counter: {"consumed": 100, "remaining": 0}},
            },
        )
        if request.method == "POST"
        else None
    )
    state = collect(env)
    assert state["status"] == "connected_partial"
    assert len([r for r in env.provider.calls if r.method == "POST"]) == 1
    daily = next(
        e
        for e in current(env)["evidence"]
        if e["source"] == "ga4" and e["data"]["operation"] == "daily"
    )
    assert daily["data"]["data"]["quota"][counter] == {"consumed": 100, "remaining": 0}
    assert all(
        e["status"] == "rate_limited" for e in state["coverage"] if e["endpoint"] == "acquisition"
    )


def test_quota_counter_exemption_never_whitelists_secrets():
    payload = {
        "propertyQuota": {"tokensPerDay": {"remaining": 20, "consumed": 10}},
        "access_token": "synthetic-secret",
        "secret": "synthetic-secret",
        "nested": {
            "tokensPerDay": "synthetic-secret",
            "tokensPerHour": {"remaining": "synthetic-secret"},
            "tokensPerMinute": {"remaining": 0, "token": "synthetic-secret"},
        },
    }
    for clean in (redact(payload), error_body(payload, ())):
        assert clean["propertyQuota"]["tokensPerDay"] == {"remaining": 20, "consumed": 10}
        assert "synthetic-secret" not in json.dumps(clean)


@pytest.mark.parametrize(
    "kind,flag",
    [
        ("google_oauth", "--client-secrets"),
        ("api_key", "--env-var"),
        ("ssh", "--credential-file"),
        ("ms_oauth/app", "--credential-file"),
    ],
)
def test_auth_hints_match_auth_kind_and_custom_hint_is_supported(kind, flag):
    spec = replace(ga4.CONNECTOR, auth_kind=kind)
    assert flag in spec.auth_command
    assert ("--client-secrets" in spec.auth_command) == (kind == "google_oauth")
    assert (
        replace(spec, auth_hint="connector-specific setup command").auth_command
        == "connector-specific setup command"
    )


@pytest.mark.parametrize("kind,flag", [("api_key", "--env-var"), ("ssh", "--credential-file")])
def test_reference_auth_flags_register_without_oauth_client_secrets(env, monkeypatch, kind, flag):
    spec = replace(ga4.CONNECTOR, auth_kind=kind, authorize=None)
    monkeypatch.setitem(service.REGISTRY, "ga4", spec)
    seen = []
    monkeypatch.setattr(commands, "register_reference", lambda *a, **kw: seen.append((a, kw)))
    assert (
        cli.main(
            ["connect", "ga4", "auth", "--credential-ref", "synthetic", flag, "synthetic-value"]
        )
        == 0
    )
    assert len(seen) == 1


def test_device_code_auth_dispatches_public_ids_without_client_secrets(env, monkeypatch, capsys):
    seen = []
    spec = replace(
        ga4.CONNECTOR,
        auth_kind="ms_oauth/app",
        auth_flow="device_code",
        authorize=lambda *a, **kw: seen.append((a, kw)),
    )
    monkeypatch.setitem(service.REGISTRY, "ga4", spec)
    assert "--client-secrets" not in spec.auth_command
    assert "--tenant-id" in spec.auth_command and "--client-id" in spec.auth_command
    assert (
        cli.main(
            [
                "connect",
                "ga4",
                "auth",
                "--credential-ref",
                "synthetic",
                "--tenant-id",
                "tenant",
                "--client-id",
                "client",
            ]
        )
        == 0
    )
    assert seen[0][0] == (spec, "synthetic")
    assert seen[0][1] == {"tenant_id": "tenant", "client_id": "client"}
    assert cli.main(["connect", "ga4", "auth", "--credential-ref", "synthetic"]) == 1
    assert "--tenant-id" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "connect",
                "ga4",
                "auth",
                "--credential-ref",
                "synthetic",
                "--client-secrets",
                "unused.json",
            ]
        )
        == 1
    )
    assert len(seen) == 1


def test_google_auth_retains_desktop_client_dispatch(env, monkeypatch):
    seen = []
    spec = replace(ga4.CONNECTOR, authorize=lambda *a: seen.append(a))
    monkeypatch.setitem(service.REGISTRY, "ga4", spec)
    assert (
        cli.main(
            [
                "connect",
                "ga4",
                "auth",
                "--credential-ref",
                "synthetic",
                "--client-secrets",
                "desktop.json",
            ]
        )
        == 0
    )
    assert seen[0][:2] == (spec, "synthetic")
    assert seen[0][2].name == "desktop.json"


def test_connector_budgets_apply_to_collect_and_resource_listing(env, monkeypatch):
    spec = replace(ga4.CONNECTOR, max_requests=1)
    monkeypatch.setitem(service.REGISTRY, "ga4", spec)
    bind(env)
    assert collect(env)["status"] == "connected_partial"
    assert len(env.provider.calls) == 1
    env.provider.calls.clear()
    env.provider.override = lambda request: httpx.Response(
        200, json={"accountSummaries": [], "nextPageToken": "next"}
    )
    listing = service.list_resources("ga4", "synthetic")
    assert listing.status == "provider_unavailable" and "cap" in listing.detail
    assert len(env.provider.calls) == 1


def test_time_budget_defaults_to_connector_and_overrides_only_reduce_it(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ams_assess.connectors.http.time.monotonic", lambda: clock[0])
    spec = replace(ga4.CONNECTOR, collection_seconds=12, max_requests=4)
    for seconds, max_requests, duration, requests in [
        (None, None, 12, 4),
        (40, 80, 12, 4),
        (3, 2, 3, 2),
    ]:
        client = ReadClient(spec, AuthMaterial(), seconds=seconds, max_requests=max_requests)
        try:
            assert client.deadline == 100 + duration and client.max_requests == requests
            clock[0] = 100 + duration
            with pytest.raises(ConnectorError, match="deadline"):
                client.remaining()
        finally:
            client.close()
            clock[0] = 100
    assert ga4.CONNECTOR.collection_seconds == 45 and ga4.CONNECTOR.max_requests == 80


@pytest.mark.parametrize(
    "limits", [{"collection_seconds": 0}, {"collection_seconds": float("inf")}, {"max_requests": 0}]
)
def test_connector_limits_require_finite_positive_budgets(limits):
    with pytest.raises(ValueError, match="finite and positive"):
        replace(ga4.CONNECTOR, **limits)
