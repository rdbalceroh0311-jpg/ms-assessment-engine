"""Shared context and pre-retry error hooks, using synthetic customer stores and HTTP."""

import json
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest
from test_connectors import ACCESS, RESOURCE, bind, collect, current, ui_server
from test_connectors import env as env

from ams_assess import cli, runner, store
from ams_assess.connectors import ga4, service
from ams_assess.connectors.adapter import ConnectorModule
from ams_assess.connectors.core import AuthMaterial, ConnectorError, ErrorClassification
from ams_assess.connectors.errors import MAX_ERROR_BODY_BYTES, MAX_ERROR_DETAIL_CHARS
from ams_assess.connectors.http import ReadClient
from ams_assess.connectors.privacy import redact
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir
from ams_assess.report import render_customer

URL = "https://analyticsadmin.googleapis.com/v1beta/" + RESOURCE


def evidence(source, revision, *, url="https://example.test", **data):
    return Evidence(
        id=f"ev-{source}-{revision}-001",
        source=source,
        url=url,
        description="Synthetic public observation",
        data=data,
    )


@pytest.mark.parametrize("path", ["collect", "run"])
def test_collect_and_analyse_receive_current_immutable_customer_context(env, monkeypatch, path):
    bind(env)
    stale_profile_id = current(env)["profile"]["evidence_id"]
    fields, profile_id, _ = store.set_profile(
        env.root,
        "synthetic",
        {
            "name": "Approved Synthetic",
            "aliases": ["Synthetic Alias"],
            "websites": ["https://example.test"],
            "abn": "51824753556",
            "phones": ["02 5550 0100"],
            "locations": [{"street": "1 Example St", "suburb": "Sydney", "state": "NSW"}],
            "service_area": ["NSW"],
            "industry": "other",
            "buying_model": "service-area",
        },
    )
    assert profile_id != stale_profile_id
    public = evidence("presence", "r21", tags=[{"name": "public-tag"}])
    older = evidence("presence", "r20", tags=[{"name": "obsolete"}])
    retained = evidence("siteone", "r22", text="Retained page sample")
    hidden = evidence("gsc", "r23", text="Other connector response")
    snapshot = current(env)
    snapshot["evidence"].extend(e.model_dump() for e in [older, public, retained, hidden])
    store.save(env.root, "synthetic", snapshot)
    # A different customer's profile must never be supplied to this connector.
    store.set_profile(env.root, "other", {"name": "Other Customer"})
    fresh = evidence("presence", "001", tags=[{"name": "fresh"}])
    expected_public = (fresh if path == "run" else public).id
    seen = []

    def inspect_context(ctx):
        assert redact(ctx.profile) == fields
        assert ctx.profile_evidence_id == profile_id
        assert ctx.profile_evidence.id == profile_id
        assert ctx.profile_evidence.provenance == "customer_statement"
        assert ctx.profile_evidence.data["name"] == fields["name"]
        assert set(ctx.public_evidence_refs) == {expected_public, retained.id}
        assert ctx.customer == "synthetic"
        assert ctx.websites == fields["websites"]
        with pytest.raises(TypeError):
            ctx.profile["name"] = "mutated"
        with pytest.raises(TypeError):
            ctx.profile["locations"][0]["street"] = "mutated"
        with pytest.raises(AttributeError):
            ctx.profile["aliases"].append("mutated")
        tag_evidence = next(e for e in ctx.public_evidence if e.source == "presence")
        with pytest.raises(TypeError):
            tag_evidence.data["tags"][0]["name"] = "mutated"
        with pytest.raises(FrozenInstanceError):
            tag_evidence.source = "mutated"
        with pytest.raises(FrozenInstanceError):
            ctx.profile_evidence.id = "mutated"
        seen.append(ctx)

    def collect_context(ctx):
        inspect_context(ctx)
        # A connector may persist selected context values and references in its own evidence.
        ctx.endpoints[0].data.update(
            {"profile_ref": ctx.profile_evidence_id, "locations": ctx.profile["locations"]}
        )

    def analyse_context(ctx, result):
        inspect_context(ctx)
        ctx.endpoints[0].data["public_refs"] = ctx.public_evidence_refs

    spec = replace(ga4.CONNECTOR, collect=collect_context, analysers=(analyse_context,))
    monkeypatch.setitem(service.REGISTRY, "ga4", spec)
    if path == "collect":
        assert cli.main(["connect", "ga4", "collect", "--customer", "synthetic"]) == 0
    else:

        class PublicModule:
            name = "presence"
            categories = {"presence"}

            def run(self, ctx):
                return ModuleResult(name="presence", evidence=[fresh], collection_started=True)

        monkeypatch.setattr(
            runner, "REGISTRY", {"presence": PublicModule(), "ga4": ConnectorModule(spec)}
        )
        monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
        ctx = Context(
            "synthetic",
            "Synthetic",
            "https://example.test",
            {"presence"},
            {"ga4"},
            {},
            create_run_dir(env.root, "synthetic"),
            env.root,
            store_root=env.root,
        )
        result = runner.run(ctx, no_ai=True, formats={"md"})
        assert result["connector_results"]["ga4"]["status"] == "connected"
        assert result.get("store_status") != "failed"
    assert len(seen) == 2 and seen[0] is seen[1]
    assert current(env)["profile"]["fields"] == fields
    assert current(env)["profile"]["evidence_id"] == profile_id
    saved = next(e for e in current(env)["evidence"] if e["source"] == "ga4")
    assert saved["data"]["data"]["profile_ref"] == profile_id
    assert saved["data"]["data"]["locations"] == fields["locations"]


def test_adapter_without_profile_does_not_invent_approved_identity(env, monkeypatch):
    bind(env)
    snapshot = current(env)
    snapshot.pop("profile")
    store.save(env.root, "synthetic", snapshot)
    seen = []
    spec = replace(ga4.CONNECTOR, collect=seen.append, analysers=())
    monkeypatch.setitem(service.REGISTRY, "ga4", spec)
    ctx = Context(
        "synthetic",
        "Synthetic",
        "https://fallback.test",
        {"cro_ux"},
        {"ga4"},
        {},
        create_run_dir(env.root, "synthetic"),
        env.root,
        store_root=env.root,
    )
    ConnectorModule(spec).run(ctx)
    assert len(seen) == 1
    assert seen[0].profile == {} and seen[0].profile_evidence_id == ""
    assert seen[0].profile_evidence is None
    assert seen[0].websites == [ctx.target]
    assert not current(env).get("profile")


@pytest.mark.parametrize(
    "code,body,status,attempts",
    [
        (403, {"error": {"details": [{"reason": "SERVICE_DISABLED"}]}}, "api_not_enabled", 1),
        (403, {"error": {"errors": [{"reason": "accessNotConfigured"}]}}, "api_not_enabled", 1),
        (400, {"error": {"status": "PERMISSION_DENIED"}}, "permission_required", 1),
        (
            403,
            {"error": {"errors": [{"reason": "insufficientPermissions"}]}},
            "permission_required",
            1,
        ),
        (
            400,
            {"error": {"details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}},
            "permission_required",
            1,
        ),
        (
            429,
            {
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"metadata": {"quota_limit_value": "0"}}],
                }
            },
            "approval_required",
            1,
        ),
        (
            429,
            {"error": {"status": "RESOURCE_EXHAUSTED", "details": [{"quota_limit_value": 0}]}},
            "approval_required",
            1,
        ),
        (
            429,
            {
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"metadata": {"quota_limit_value": "10"}}],
                }
            },
            "rate_limited",
            3,
        ),
        (429, {}, "rate_limited", 3),
        (503, {}, "provider_unavailable", 3),
        (401, {"error": {"status": "UNAUTHENTICATED"}}, "credential_expired", 1),
    ],
)
def test_default_classifier_runs_before_retries(env, code, body, status, attempts):
    bind(env)
    env.provider.override = lambda request: httpx.Response(code, json=body)
    state = collect(env)
    assert state["status"] == status
    assert len(env.provider.calls) == attempts
    assert state["coverage"][0]["status"] == status
    assert current(env)["evidence"][-1]["data"]["status"] == status
    if status == "permission_required":
        assert ga4.SCOPE in state["detail"]


@pytest.mark.parametrize(
    "provider_code,status,detail",
    [
        (
            "Authorization_RequestDenied",
            "permission_required",
            "Grant AuditLog.Read.All and Reports Reader role",
        ),
        (
            "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
            "licence_required",
            "Microsoft Entra ID P1 licence required for sign-in logs",
        ),
        ("NeedsApproval", "approval_required", "Request GBP API approval"),
        ("ApiDisabled", "api_not_enabled", "Enable the provider reporting API"),
    ],
)
def test_provider_hook_actionable_status_consistent_in_cli_ui_report(
    env, monkeypatch, capsys, provider_code, status, detail
):
    bind(env)
    seen = []

    def classify(code, body):
        seen.append((code, body["error"]["code"]))
        return ErrorClassification(status, detail)

    monkeypatch.setitem(service.REGISTRY, "ga4", replace(ga4.CONNECTOR, classify_error=classify))
    # A custom non-retryable classification must also override HTTP 429/5xx.
    env.provider.override = lambda request: httpx.Response(
        429, json={"error": {"code": provider_code}}
    )
    assert collect(env)["status"] == status
    assert seen == [(429, provider_code)]
    cli.main(["connectors", "--customer", "synthetic"])
    output = capsys.readouterr().out
    assert status in output and detail in output
    with ui_server(env.root) as (server, client):
        html = client.get(server.url(customer="synthetic")).text
        assert status in html and detail in html
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text().replace("\\_", "_")
    assert status in report and detail in report
    entries = [e for e in current(env)["systems_register"]["entries"] if e["platform"] == "ga4"]
    assert entries and all(e["integration"] == status for e in entries)
    assert not current(env)["connectors"]["ga4"].get("last_successful_test")


@pytest.mark.parametrize("status", ["rate_limited", "provider_unavailable"])
def test_custom_transient_classification_recovers(env, monkeypatch, status):
    bind(env)
    seen = []

    def classify(code, body):
        seen.append(code)
        return ErrorClassification(status, "Temporary read failure")

    monkeypatch.setitem(service.REGISTRY, "ga4", replace(ga4.CONNECTOR, classify_error=classify))
    env.provider.override = lambda request: (
        httpx.Response(400, json={"error": {"code": "TransientProviderCondition"}})
        if len(env.provider.calls) < 3
        else None
    )
    assert service.execute(env.root, "synthetic", "ga4")["status"] == "connected"
    assert seen == [400, 400] and len(env.provider.calls) == 3


def test_hook_falls_back_to_default_and_non_json_failure_is_bounded(env, monkeypatch):
    bind(env)
    seen = []

    def classify(code, body):
        seen.append((code, body))
        return None

    monkeypatch.setitem(service.REGISTRY, "ga4", replace(ga4.CONNECTOR, classify_error=classify))
    env.provider.override = lambda request: httpx.Response(503, content=b"bad gateway")
    assert collect(env)["status"] == "provider_unavailable"
    assert seen == [(503, {})] * 3


def test_error_input_and_hook_output_are_redacted_capped_at_every_sink(env, monkeypatch, caplog):
    bind(env)
    email = "synthetic.private@example.test"
    seen = []
    diagnostic = f"{ACCESS} {email} Bearer fake-sensitive-value " + "large diagnostic " * 400

    def classify(code, body):
        seen.append(body)
        assert code == 429
        assert body["error"]["status"] == "RESOURCE_EXHAUSTED"
        assert body["error"]["details"][0]["metadata"]["quota_limit_value"] == "0"
        assert len(json.dumps(body, ensure_ascii=True).encode()) <= MAX_ERROR_BODY_BYTES
        assert ACCESS not in json.dumps(body) and email not in json.dumps(body)
        assert "fake-sensitive-value" not in json.dumps(body)
        assert body["error"]["access_token"] == "[redacted]"
        # A hook's own text is also scrubbed, even when it did not come from the body.
        return ErrorClassification("licence_required", "Required P1 licence: " + diagnostic)

    monkeypatch.setitem(service.REGISTRY, "ga4", replace(ga4.CONNECTOR, classify_error=classify))
    body = {
        "error": {
            "access_token": ACCESS,
            "message": diagnostic,
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"metadata": {"quota_limit_value": "0"}}],
            "diagnostics": [{"text": diagnostic + "🧪" * 2000}] * 1000,
        },
    }
    # Keep the transport response within its independent 2 MB limit.
    body["error"]["diagnostics"] = body["error"]["diagnostics"][:50]
    env.provider.override = lambda request: httpx.Response(429, json=body)
    state = collect(env)
    assert len(seen) == 1 and state["status"] == "licence_required"
    assert len(state["detail"]) <= MAX_ERROR_DETAIL_CHARS
    assert len(state["coverage"][0]["detail"]) <= MAX_ERROR_DETAIL_CHARS
    assert len(current(env)["evidence"][-1]["data"]["detail"]) <= MAX_ERROR_DETAIL_CHARS
    cli.main(["connectors", "--customer", "synthetic"])
    render_customer(env.root, "synthetic", {"md"})
    with ui_server(env.root) as (server, client):
        html = client.get(server.url(customer="synthetic")).text
    sinks = json.dumps(current(env)) + html + caplog.text
    sinks += (env.root / "synthetic/report.md").read_text()
    for secret in (ACCESS, email, "fake-sensitive-value"):
        assert secret not in sinks
    assert "Required P1 licence" in sinks


def test_nonretryable_hook_preserves_successful_pages_and_endpoints(env, monkeypatch):
    bind(env)
    collect(env)
    old = current(env)["findings"][0]
    seen = []

    def classify(code, body):
        seen.append(code)
        return ErrorClassification("licence_required", "P1 licence required")

    monkeypatch.setitem(service.REGISTRY, "ga4", replace(ga4.CONNECTOR, classify_error=classify))
    env.provider.calls.clear()
    env.provider.override = lambda request: (
        httpx.Response(503, json={"error": {"code": "LicenseRequired"}})
        if request.method == "POST"
        else None
    )
    state = collect(env)
    assert state["status"] == "connected_partial" and len(seen) == 6
    assert any(e["endpoint"] == "streams" and e["status"] == "connected" for e in state["coverage"])
    assert all(
        e["status"] == "licence_required" for e in state["coverage"] if e["endpoint"] == "daily"
    )
    assert (
        next(f for f in current(env)["findings"] if f["id"] == old["id"])["lifecycle_status"]
        == "not_rechecked"
    )


def test_classifier_cannot_bypass_read_allowlist():
    seen = []
    spec = replace(ga4.CONNECTOR, classify_error=lambda code, body: seen.append(body))
    http = ReadClient(
        spec,
        AuthMaterial(),
        transport=httpx.MockTransport(lambda request: pytest.fail("HTTP refused")),
    )
    try:
        with pytest.raises(ConnectorError, match="allowlist"):
            http.request("POST", URL)
        assert not seen
    finally:
        http.close()
