"""Connector reporting uses only synthetic customer stores and mocked provider reads."""

import copy
import json
from dataclasses import replace

import httpx
import pytest
from test_gbp import ACCESS, RESOURCE, bind, collect, current, seed_public_nap, ui_server
from test_gbp import env as env
from test_presence import context

from ams_assess import cli, onboarding, store
from ams_assess.connectors import gbp, gsc, service
from ams_assess.connectors.core import AuthMaterial
from ams_assess.connectors.http import ReadClient
from ams_assess.models import ModuleResult
from ams_assess.modules import presence
from ams_assess.modules.gsc import connected_findings
from ams_assess.report import render_customer, render_markdown


def deny_metrics(request):
    if request.url.host == "businessprofileperformance.googleapis.com":
        return httpx.Response(
            403,
            json={"error": {"status": "PERMISSION_DENIED", "message": "Metrics access denied"}},
        )
    return None


def test_partial_collection_timestamp_agrees_in_store_cli_ui_and_report(env, capsys):
    bind(env)
    env.provider.override = deny_metrics
    state = collect(env)
    assert state["status"] == "connected_partial"
    assert state["last_successful_collection"] == state["last_collection"]
    assert state["last_successful_collection_status"] == "connected_partial"
    shown = state["last_successful_collection"] + " (partial)"
    assert (
        next(r for r in onboarding.rows(current(env)) if r["key"] == "gbp")[
            "last_successful_collection"
        ]
        == shown
    )
    assert cli.main(["connectors", "--customer", "synthetic"]) == 0
    assert shown in capsys.readouterr().out
    with ui_server(env.root) as (server, client):
        assert client.get(server.url(customer="synthetic")).text.count(shown) == 2
    render_customer(env.root, "synthetic", {"md"})
    assert shown in (env.root / "synthetic/report.md").read_text()

    # A later failed attempt or connection test must not relabel the partial collection.
    env.provider.override = lambda request: httpx.Response(403)
    assert collect(env)["last_successful_collection"] == state["last_collection"]
    env.provider.override = lambda request: None
    service.execute(env.root, "synthetic", "gbp")
    assert (
        next(r for r in service.rows(current(env)) if r["key"] == "gbp")[
            "last_successful_collection"
        ]
        == shown
    )
    complete = collect(env)
    assert complete["last_successful_collection_status"] == "connected"
    assert (
        next(r for r in service.rows(current(env)) if r["key"] == "gbp")[
            "last_successful_collection"
        ]
        == complete["last_collection"]
    )


def test_all_failed_endpoints_have_no_successful_collection(env):
    bind(env)
    env.provider.override = lambda request: httpx.Response(403)
    assert not collect(env).get("last_successful_collection")
    assert (
        next(r for r in service.rows(current(env)) if r["key"] == "gbp")[
            "last_successful_collection"
        ]
        == "Never"
    )


def test_partial_read_with_rows_records_a_partial_collection(env, monkeypatch):
    def partial(ctx):
        def read(endpoint):
            endpoint.rows = 1
            endpoint.status = "connected_partial"
            endpoint.truncated = True

        return ctx.capture("location", "", read)

    monkeypatch.setitem(service.REGISTRY, "gbp", replace(gbp.CONNECTOR, test=partial))
    bind(env)
    state = collect(env)
    assert state["last_successful_collection"] == state["last_collection"]
    assert state["last_successful_collection_status"] == "connected_partial"


def test_authenticated_gbp_read_updates_presence_and_readiness_without_public_claims(env, tmp_path):
    bind(env)
    env.provider.override = deny_metrics
    collect(env)
    ctx = context(tmp_path)
    summary = presence.build(ctx, [], ModuleResult(name="presence"), discover=True)
    before = copy.deepcopy(summary)
    snapshot = current(env)
    service.project_presence(summary, service.rows(snapshot))
    projected = copy.deepcopy(summary)
    service.project_presence(summary, service.rows(snapshot))
    assert summary == projected
    for group in (summary["listings"], summary["readiness"], summary["discovery"]["platforms"]):
        row = next(r for r in group if r["key"] == "google_business_profile")
        assert "Connected via authorised API" in row["authenticated_read"]
        assert RESOURCE in row["authenticated_read"] and "section 7" in row["authenticated_read"]
        assert row["authenticated_evidence_ids"]
    readiness = next(r for r in summary["readiness"] if r["key"] == "google_business_profile")
    assert readiness["access"] == "connected_partial"
    assert "No connector implemented" not in readiness["insights"]
    discovery = next(
        r for r in summary["discovery"]["platforms"] if r["key"] == "google_business_profile"
    )
    assert discovery["detail"].startswith("Not searched:")
    assert discovery["candidates"] == []
    assert summary["listings"][0]["ownership"] == before["listings"][0]["ownership"]
    assert "only existing integrations" not in " ".join(summary["notes"])
    for spec in service.REGISTRY.values():
        assert spec.label in " ".join(summary["notes"])
    data, findings, evidence = store.report_data(snapshot)
    data["digital_presence_sites"] = [{**summary, "target": ctx.target, "run_id": "r1"}]
    before_render = copy.deepcopy(data)
    text = render_markdown(data, findings, evidence)
    assert data == before_render
    assert "Connected via authorised API" in text
    assert "Public search:" in text
    assert "only existing integrations" not in text
    assert snapshot == current(env)


@pytest.mark.parametrize("scenario", ["unbound", "failed", "other_customer", "rebound"])
def test_presence_never_infers_authenticated_read_from_unavailable_binding(env, tmp_path, scenario):
    bind(env)
    collect(env)
    if scenario == "failed":
        env.provider.override = lambda request: httpx.Response(403)
        collect(env)
    elif scenario == "rebound":
        service.set_binding(env.root, "synthetic", "gbp", "synthetic", "locations/999999")
    snapshot = (
        current(env) if scenario in {"failed", "rebound"} else store.new_store("other", "Other")
    )
    summary = presence.build(context(tmp_path), [], ModuleResult(name="presence"), discover=True)
    service.project_presence(summary, service.rows(snapshot))
    assert "Connected via authorised API" not in json.dumps(summary)


def test_permission_detail_keeps_sanitised_provider_message_and_labels_possible_causes(env):
    bind(env)
    env.provider.override = lambda request: (
        httpx.Response(
            403,
            json={
                "error": {
                    "status": "PERMISSION_DENIED",
                    "message": "Daily metrics unavailable for this location. Bearer " + ACCESS,
                }
            },
        )
        if request.url.host == "businessprofileperformance.googleapis.com"
        else None
    )
    state = collect(env)
    assert state["status"] == "connected_partial"
    endpoint = next(e for e in state["coverage"] if e["endpoint"] == "daily_metrics")
    for text in (endpoint["detail"], state["detail"]):
        assert "PERMISSION_DENIED" in text
        assert "Daily metrics unavailable for this location" in text
        assert "Possible causes" in text
        assert ACCESS not in text
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text().replace("\\_", "_")
    line = next(line for line in report.splitlines() if line.startswith("- daily_metrics:"))
    assert "PERMISSION_DENIED" in line and "Daily metrics unavailable" in line
    assert "Possible causes" in line and ACCESS not in report


SITE = "https://example.test"


def seed_gsc_findings(env):
    seed_public_nap(env)
    result = ModuleResult(name="gsc")
    result.add_evidence(
        SITE,
        "Legacy GSC read",
        {
            "property": SITE,
            "page": {
                "rows": [
                    {
                        "keys": ["http://www.example.test/sample-page/"],
                        "impressions": 10,
                        "clicks": 1,
                    }
                ]
            },
            "sitemaps": {"sitemap": []},
        },
    )
    published = ModuleResult(name="presence")
    published.add_evidence(
        SITE,
        "Bounded robots.txt and XML sitemap fetch",
        {
            "status": "PASS",
            "sitemap_url": SITE + "/sitemap.xml",
        },
    )
    evidence = [*result.evidence, *published.evidence]
    result.findings, result.outcomes = connected_findings(evidence, SITE)
    assert len(result.findings) == 3
    store.merge_run(
        env.root,
        "synthetic",
        "Synthetic",
        {
            "target": SITE,
            "created_at": "2026-02-01T00:00:00Z",
            "status": "complete",
            "selected_categories": ["seo"],
            "assessed_categories": ["seo"],
            "findings": [f.model_dump() for f in result.findings],
            "outcomes": [o.model_dump() for o in result.outcomes],
            "modules": [result.model_dump(), published.model_dump()],
            "pages": [],
            "max_pages": 0,
            "limitations": [],
            "future": [],
            "report_files": [],
            "ai": {"status": "skipped"},
        },
        evidence,
        "legacy-gsc",
        ["connected"],
        lifecycle_sources=frozenset({"gsc"}),
    )


def bind_gsc(env, monkeypatch, resource=SITE, *, resolved=False, failure=""):
    resource = gsc.normalize_resource(resource)
    monkeypatch.setitem(
        service.REGISTRY,
        "gsc",
        replace(
            gsc.CONNECTOR,
            authenticate=lambda *args, **kwargs: AuthMaterial(),
        ),
    )
    service.set_binding(env.root, "synthetic", "gsc", "synthetic", resource)

    def response(request):
        if request.url.path.endswith("/sitemaps"):
            if failure == "sitemaps:capped":
                return httpx.Response(200, json={"sitemap": [{}] * 501})
            return (
                httpx.Response(403)
                if failure == "sitemaps"
                else httpx.Response(200, json={"sitemap": [{}] if resolved else []})
            )
        if request.method == "POST":
            dimension = json.loads(request.content)["dimensions"][0]
            if failure == dimension:
                return httpx.Response(403)
            if failure == dimension + ":capped":
                return httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "keys": [SITE + "/valid/"],
                                "impressions": 1,
                                "clicks": 0,
                                "ctr": 0,
                                "position": 1,
                            }
                        ]
                        * 100
                    },
                )
            return httpx.Response(
                200,
                json={
                    "rows": []
                    if resolved
                    else [
                        {
                            "keys": ["http://www.example.test/sample-page/"],
                            "impressions": 10,
                            "clicks": 1,
                            "ctr": 0.1,
                            "position": 3,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"siteUrl": resource, "permissionLevel": "siteOwner"})

    monkeypatch.setattr(
        service,
        "ReadClient",
        lambda *args, transport=None, **kwargs: ReadClient(
            *args,
            transport=httpx.MockTransport(response),
            **kwargs,
        ),
    )


@pytest.mark.parametrize("resolved", [False, True])
def test_gsc_collect_rechecks_same_property_findings_with_current_evidence(
    env, monkeypatch, resolved
):
    seed_gsc_findings(env)
    before = current(env)
    public = [f for f in before["findings"] if not f["check_key"].startswith("gsc:")]
    bind_gsc(env, monkeypatch, resolved=resolved)
    assert cli.main(["connect", "gsc", "collect", "--customer", "synthetic"]) == 0
    saved = current(env)
    derived = [f for f in saved["findings"] if f["check_key"].startswith("gsc:")]
    assert len(derived) == 3
    assert {f["lifecycle_status"] for f in derived} == {"resolved" if resolved else "current"}
    fresh = {e["id"] for e in saved["evidence"][len(before["evidence"]) :]}
    for finding in derived:
        ids = finding["resolution"]["evidence_ids"] if resolved else finding["evidence_ids"]
        assert fresh.intersection(ids)
    assert [f for f in saved["findings"] if not f["check_key"].startswith("gsc:")] == public
    assert saved["runs"][:-1] == before["runs"]
    assert saved["evidence"][: len(before["evidence"])] == before["evidence"]


@pytest.mark.parametrize("failure", ["sitemaps", "page", "sitemaps:capped", "page:capped"])
def test_gsc_failed_endpoint_does_not_resolve_its_checks(env, monkeypatch, failure):
    seed_gsc_findings(env)
    bind_gsc(env, monkeypatch, resolved=True, failure=failure)
    service.execute(env.root, "synthetic", "gsc", collect=True)
    for finding in current(env)["findings"]:
        if finding["check_key"].startswith("gsc:"):
            failed = (finding["check_key"] == "gsc:sitemap-not-submitted") == (
                failure.startswith("sitemaps")
            )
            assert finding["lifecycle_status"] == ("not_rechecked" if failed else "resolved")


@pytest.mark.parametrize(
    "resource", ["sc-domain:example.test", SITE + "/other/", "https://other.test"]
)
def test_gsc_different_property_cannot_resolve_old_findings(env, monkeypatch, resource):
    seed_gsc_findings(env)
    bind_gsc(env, monkeypatch, resource, resolved=True)
    service.execute(env.root, "synthetic", "gsc", collect=True)
    assert {
        f["lifecycle_status"] for f in current(env)["findings"] if f["check_key"].startswith("gsc:")
    } == {"not_rechecked"}
