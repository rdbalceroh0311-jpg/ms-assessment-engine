"""Connected context is local to one customer and one explicit site binding."""

import json
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest
from test_connectors import ACCESS
from test_connectors import env as env

from ams_assess import cli, register, runner, store
from ams_assess.connectors import ga4, service
from ams_assess.connectors.adapter import ConnectorModule
from ams_assess.connectors.core import AuthMaterial, Period, ReadOperation
from ams_assess.connectors.privacy import redact
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir

SITE = "https://example.test/"
KEY = "site_reader"


def sample(revision=1, *, resource=SITE, legacy=False, source="gsc", status="connected"):
    period = Period.make().json()
    rows = [{"keys": [SITE + "contact"], "clicks": 4, "impressions": 40}]
    data = (
        {
            "property": resource,
            "start_date": period["start"],
            "end_date": period["end"],
            "page": {"rows": rows},
            "query": {"rows": []},
        }
        if legacy
        else {
            "connector": source,
            "resource_id": resource,
            "operation": "page",
            "period": period,
            "status": status,
            "coverage": {"rows_returned": 1, "truncated": False},
            "data": {"rows": rows},
        }
    )
    return Evidence(
        id=f"ev-{source}-r{revision}-001",
        source=source,
        url=SITE if legacy else "",
        description="Synthetic connected sample",
        data=data,
        provenance="manual_verification",
    )


def save_evidence(env, items, customer="synthetic"):
    current = store.load(env.root, customer) or store.new_store(customer, customer)
    current["evidence"].extend(item.model_dump() for item in items)
    store.save(env.root, customer, current)


@pytest.fixture
def reader(env, monkeypatch):
    seen = []

    def test(ctx):
        def read(endpoint):
            endpoint.data = ctx.http.request("GET", "https://reader.test/read")
            endpoint.rows = 1

        return ctx.capture("site", "https://reader.test/read", read)

    spec = replace(
        ga4.CONNECTOR,
        key=KEY,
        provider="synthetic",
        resource_type="site",
        normalize_resource=lambda value: value,
        authenticate=lambda *a, **kw: AuthMaterial(
            {"Authorization": "Bearer " + ACCESS}, (ACCESS,)
        ),
        test=test,
        collect=seen.append,
        analysers=(lambda ctx, result: seen.append(ctx),),
        read_allowlist=(ReadOperation("GET", r"https://reader\.test/read"),),
    )
    monkeypatch.setitem(service.REGISTRY, KEY, spec)
    monkeypatch.setitem(register.PLATFORMS, KEY, "Synthetic site reader")
    env.provider.override = lambda request: httpx.Response(200, json={"ok": True})
    service.set_binding(env.root, "synthetic", KEY, "synthetic", SITE)
    return spec, seen


def invoke(env, reader, path, monkeypatch, incoming=()):
    spec, seen = reader
    if path == "collect":
        assert cli.main(["connect", KEY, "collect", "--customer", "synthetic"]) == 0
    else:

        class GSCModule:
            categories = {"seo"}

            def run(self, ctx):
                return ModuleResult(name="gsc", evidence=list(incoming), collection_started=True)

        monkeypatch.setattr(runner, "REGISTRY", {"gsc": GSCModule(), KEY: ConnectorModule(spec)})
        monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
        ctx = Context(
            "synthetic",
            "Synthetic",
            SITE,
            {"seo"},
            {KEY},
            {},
            create_run_dir(env.root, "synthetic"),
            env.root,
            store_root=env.root,
        )
        result = runner.run(ctx, no_ai=True, formats={"md"})
        assert result["connector_results"][KEY]["status"] == "connected"
        assert result.get("store_status") != "failed"
    assert len(seen) == 2 and seen[0] is seen[1]
    return seen[0]


@pytest.mark.parametrize("path", ["collect", "run"])
@pytest.mark.parametrize("legacy", [False, True])
def test_gsc_latest_customer_snapshot_is_immutable_and_shared_with_analysis(
    env, reader, monkeypatch, path, legacy
):
    old, latest = sample(1, legacy=legacy), sample(3, legacy=legacy)
    save_evidence(env, [old, latest])
    # Identical site and higher revision in a different customer's store must never be read.
    save_evidence(env, [sample(900, legacy=legacy)], customer="other")
    saved = store.load(env.root, "synthetic")
    assert saved is not None
    before = saved["evidence"]
    ctx = invoke(env, reader, path, monkeypatch)
    assert isinstance(ctx.connected_evidence, tuple)
    assert [e.id for e in ctx.connected_evidence] == [latest.id]
    view = ctx.connected_evidence[0]
    assert view.provenance == latest.provenance
    assert redact(view.data) == latest.data
    assert all(e.source != "gsc" for e in ctx.public_evidence)
    with pytest.raises(FrozenInstanceError):
        view.source = KEY
    with pytest.raises(TypeError):
        view.data["property"] = "other"
    rows = view.data["page" if legacy else "data"]["rows"]
    with pytest.raises(TypeError):
        rows[0]["clicks"] = 100
    with pytest.raises(AttributeError):
        rows.append({})
    saved = store.load(env.root, "synthetic")
    assert saved is not None and saved["evidence"][: len(before)] == before


@pytest.mark.parametrize("path", ["collect", "run"])
def test_absent_gsc_does_not_use_other_customer_or_public_or_own_evidence(
    env, reader, monkeypatch, path
):
    save_evidence(env, [sample(900)], customer="other")
    save_evidence(env, [sample(source="presence"), sample(source=KEY)])
    assert invoke(env, reader, path, monkeypatch).connected_evidence == ()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "resource,expected",
    [
        (SITE, True),
        ("https://example.test", True),
        ("https://example.test:443/", True),
        ("sc-domain:example.test", True),
        ("https://other.test/", False),
        ("https://example.test.evil.test/", False),
        ("https://www.example.test/", False),
        ("https://shop.example.test/", False),
        ("http://example.test/", False),
        ("https://example.test:8443/", False),
        ("https://example.test/client-b/", False),
        ("sc-domain:other.test", False),
        ("properties/123", False),
        ("", False),
    ],
)
def test_site_selection_uses_explicit_resource_not_profile_websites(
    env, reader, legacy, resource, expected
):
    store.set_profile(env.root, "synthetic", {"websites": [SITE, "https://other.test/"]})
    item = sample(resource=resource, legacy=legacy)
    # Even the evidence URL cannot override an explicit different property/resource.
    item.url = SITE
    save_evidence(env, [item])
    service.execute(env.root, "synthetic", KEY, collect=True)
    assert [e.id for e in reader[1][0].connected_evidence] == ([item.id] if expected else [])


@pytest.mark.parametrize("binding", ["properties/123", "", "https://example.test/client-a/"])
def test_opaque_unbound_and_broader_properties_are_not_assumed_to_match(env, reader, binding):
    save_evidence(env, [sample(), sample(2, resource="sc-domain:example.test")])
    service.set_binding(env.root, "synthetic", KEY, "synthetic", binding)
    # Unconfigured requests still analyse, but cannot gain a site scope from the profile.
    service.execute(env.root, "synthetic", KEY, collect=True)
    assert reader[1][-1].connected_evidence == ()


def test_latest_failed_collection_replaces_old_success_but_retains_all_endpoints(env, reader):
    old = sample(1)
    latest = sample(2, status="permission_required")
    property_read = latest.model_copy(deep=True)
    property_read.id = "ev-gsc-r2-002"
    property_read.data["operation"] = "property"
    property_read.data["status"] = "connected"
    # A later collection of a different site cannot displace this site's latest collection.
    save_evidence(env, [old, latest, property_read, sample(3, resource="https://other.test/")])
    service.execute(env.root, "synthetic", KEY, collect=True)
    views = reader[1][0].connected_evidence
    assert [e.id for e in views] == [latest.id, property_read.id]
    assert views[0].data["status"] == "permission_required"


def test_current_run_gsc_overrides_stored_samples_for_same_site(env, reader, monkeypatch):
    save_evidence(env, [sample(100), sample(101, resource="https://other.test/")])
    fresh = sample(status="provider_unavailable")
    fresh.id = "ev-gsc-001"
    ctx = invoke(env, reader, "run", monkeypatch, [fresh])
    assert [e.id for e in ctx.connected_evidence] == [fresh.id]
    assert ctx.connected_evidence[0].data["status"] == "provider_unavailable"


def test_incoming_other_site_does_not_replace_current_site(env, reader, monkeypatch):
    saved = sample(2)
    save_evidence(env, [saved])
    fresh = sample(resource="https://other.test/")
    fresh.id = "ev-gsc-001"
    ctx = invoke(env, reader, "run", monkeypatch, [fresh])
    assert [e.id for e in ctx.connected_evidence] == [saved.id]


def test_connected_view_redacts_legacy_payload_without_loading_credentials(env, reader):
    item = sample(legacy=True)
    item.data["access_token"] = "synthetic-secret"
    item.data["page"]["rows"][0]["keys"] = ["person@example.test"]
    item.description = "Synthetic Bearer secret-value"
    save_evidence(env, [item])
    service.execute(env.root, "synthetic", KEY, collect=True)
    view = reader[1][0].connected_evidence[0]
    output = json.dumps(redact(view.data)) + view.description
    assert "synthetic-secret" not in output and "secret-value" not in output
    assert "person@example.test" not in output
    assert view.data["access_token"] == "[redacted]"
