"""Review regressions exercise rendered rows, site scope and effective access guards."""

import copy
from dataclasses import replace

import pytest
from test_connector_report_consistency import bind_gsc, seed_gsc_findings
from test_gbp import current, seed_public_nap
from test_gbp import env as env
from test_presence import context

from ams_assess import store
from ams_assess.connectors import gsc, service
from ams_assess.models import Evidence, ModuleResult
from ams_assess.modules import presence
from ams_assess.modules.gsc import AI_REPORT_NOTE
from ams_assess.onboarding import PLATFORM_KEYS
from ams_assess.report import render_markdown

SITE = "https://example.test/"
OTHER = "https://unrelated.test/"


def connected_store(env, key, resource):
    seed_public_nap(env)
    snapshot = current(env)
    eid = f"ev-{key}-r90-001"
    snapshot["evidence"].append(
        Evidence(
            id=eid,
            source=key,
            url="",
            description="Synthetic authenticated read",
            data={
                "connector": key,
                "resource_id": resource,
                "operation": "resource",
                "status": "connected",
                "detail": "",
                "collected_at": store.now(),
                "period": {"start": "2026-01-01", "end": "2026-01-31"},
                "coverage": {"rows_returned": 1, "truncated": False},
                "data": {},
            },
        ).model_dump()
    )
    snapshot["connectors"] = {
        key: {
            "resource_id": resource,
            "last_successful_test": store.now(),
            "status": "connected_partial",
            "detail": "Synthetic partial coverage.",
            "coverage": [
                {
                    "endpoint": "resource",
                    "status": "connected",
                    "rows": 1,
                    "truncated": False,
                    "detail": "Synthetic read succeeded.",
                    "evidence_id": eid,
                }
            ],
        }
    }
    store.save(env.root, "synthetic", snapshot)
    return current(env)


def summary(tmp_path, site=SITE):
    ctx = context(tmp_path)
    ctx.customer_id, ctx.target = "synthetic", site
    tables = presence.build(ctx, [], ModuleResult(name="presence"))
    # Workspace readiness normally comes from DNS detection; no live DNS is needed here.
    tables["readiness"].append(
        {
            "key": "google_workspace",
            "service": "Google Workspace (ScubaGoggles)",
            "access": "Unsupported",
            "insights": "Workspace settings",
            "evidence_ids": [],
            "next_step": "Confirm access",
        }
    )
    return {**tables, "target": site, "run_id": "r1"}


def markdown(snapshot, summaries):
    data, evidence, discovery = store.report_data(snapshot)
    data["digital_presence_sites"] = summaries
    data["target"] = summaries[-1]["target"]  # Latest run may be the unrelated website.
    return render_markdown(data, evidence, discovery).replace("\\_", "_")


def readiness(text):
    return text.split("### Connection readiness\n", 1)[1].split("### Customer access", 1)[0]


@pytest.mark.parametrize(
    "key,resource",
    [
        ("gbp", "locations/123456"),
        ("wp_cli", "wp-install:" + "a" * 64),
        ("workspace", "C123456/example.test"),
    ],
)
def test_rendered_readiness_merges_platform_alias_with_connector(env, tmp_path, key, resource):
    snapshot = connected_store(env, key, resource)
    summaries = [summary(tmp_path, site) for site in (SITE, OTHER)]
    table = readiness(markdown(snapshot, summaries))
    names = {service.REGISTRY[key].label}
    names.update(
        r["service"] for p in summaries for r in p["readiness"] if r["key"] in PLATFORM_KEYS[key]
    )
    for site in (SITE, OTHER):
        rows = [
            line
            for line in table.splitlines()
            if any(line.startswith(f"| {name}<br>{site} |") for name in names)
        ]
        assert len(rows) == 1, rows
        assert "| connected_partial |" in rows[0]
        assert "Connected via authorised API" in rows[0]
        assert "Unsupported" not in rows[0]


@pytest.mark.parametrize(
    "key,resource",
    [
        ("gsc", SITE),
        ("gsc", "https://example.test.:443/"),
        ("gsc", "sc-domain:example.test"),
        ("bing_wmt", SITE),
        ("bing_wmt", "https://example.test.:443/"),
        ("site_reader", SITE),
    ],
)
def test_rendered_presence_claim_is_scoped_to_bound_site_in_same_customer(
    env, tmp_path, monkeypatch, key, resource
):
    if key == "site_reader":
        monkeypatch.setitem(
            service.REGISTRY,
            key,
            replace(
                gsc.CONNECTOR,
                key=key,
                label="Synthetic site reader",
            ),
        )
    snapshot = connected_store(env, key, resource)
    summaries = [summary(tmp_path, site) for site in (SITE, OTHER)]
    for p in summaries:
        if key == "site_reader":
            p["readiness"].append(
                {
                    **next(r for r in p["readiness"] if r["key"] == "gsc"),
                    "key": key,
                    "service": "Synthetic site reader",
                }
            )
        service.project_presence(p, service.rows(snapshot))
        row = next(r for r in p["readiness"] if r["key"] == key)
        assert bool(row.get("authenticated_read")) == (p["target"] == SITE)
    text = markdown(snapshot, summaries)
    table = readiness(text)
    label = service.REGISTRY[key].label
    bound = [line for line in table.splitlines() if line.startswith(f"| {label}<br>{SITE} |")]
    other = [line for line in table.splitlines() if line.startswith(f"| {label}<br>{OTHER} |")]
    assert len(bound) == len(other) == 1
    assert "Connected via authorised API" in bound[0]
    assert "Connected via authorised API" not in other[0]
    assert "connected_partial" not in other[0]
    assert snapshot == current(env)


@pytest.mark.parametrize("missing", ["resource_id", "last_successful_test", "status"])
def test_authenticated_claim_requires_every_gate_despite_successful_coverage(
    env, tmp_path, missing
):
    snapshot = connected_store(env, "gbp", "locations/123456")
    tables = summary(tmp_path)
    service.project_presence(tables, service.rows(snapshot))
    assert next(r for r in tables["readiness"] if r["key"] == "google_business_profile")[
        "authenticated_read"
    ]
    state = snapshot["connectors"]["gbp"]
    if missing == "status":
        state["status"] = "permission_required"
    else:
        state.pop(missing)
    # Retain successful coverage: each guard must independently suppress the claim.
    assert state["coverage"][0]["status"] == "connected"
    service.project_presence(tables, service.rows(snapshot))
    assert "Connected via authorised API" not in markdown(snapshot, [tables])


def test_presence_next_steps_use_the_actual_customer(tmp_path):
    ctx = context(tmp_path)
    ctx.customer_id = "synthetic"
    tables = presence.build(ctx, [], ModuleResult(name="presence"))
    commands = [r["next_step"] for r in tables["readiness"] if "ams-assess" in r["next_step"]]
    assert commands
    assert all(
        "<customer>" not in command and "--customer synthetic" in command for command in commands
    )


def test_gbp_search_guidance_does_not_depend_on_note_position(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, "NOTES", [*presence.NOTES, "Another presence note"])
    tables = summary(tmp_path)
    assert any(n.startswith("Google Business Profile public search") for n in tables["notes"])


def test_coverage_detail_has_one_terminal_full_stop(env, tmp_path):
    snapshot = connected_store(env, "gbp", "locations/123456")
    text = markdown(snapshot, [summary(tmp_path)])
    line = next(line for line in text.splitlines() if line.startswith("- resource:"))
    assert "Synthetic read succeeded. Evidence:" in line
    assert ".." not in line
    assert "coverage.." not in text


@pytest.mark.parametrize("missing", ["gsc", "presence"])
def test_gsc_missing_cited_evidence_is_not_reevaluated(env, monkeypatch, missing):
    seed_gsc_findings(env)
    bind_gsc(env, monkeypatch, resolved=True)
    snapshot = current(env)
    result, _ = service.prepare(snapshot, "gsc", collect=True)
    finding = next(f for f in snapshot["findings"] if f["check_key"] == "gsc:sitemap-not-submitted")
    snapshot["findings"] = [finding]
    absent = next(
        e
        for e in snapshot["evidence"]
        if e["source"] == missing and e["id"] in finding["evidence_ids"]
    )
    snapshot["evidence"].remove(absent)
    before = copy.deepcopy(snapshot)
    gsc.recheck_findings(result, snapshot, service.latest_public_evidence(snapshot))
    assert not result.findings and not result.outcomes
    assert snapshot == before


@pytest.mark.parametrize(
    "key,resource,target,connected",
    [
        pytest.param("gsc", SITE, "https://www.example.test/", True, id="gsc-apex-www"),
        pytest.param("gsc", "https://www.example.test/", SITE, True, id="gsc-www-apex"),
        pytest.param(
            "gsc", "sc-domain:example.test", "https://www.example.test/", True, id="domain-www"
        ),
        pytest.param("gsc", "sc-domain:example.test", SITE + "blog/", True, id="domain-path"),
        pytest.param("bing_wmt", SITE, "https://www.example.test/", True, id="bing-apex-www"),
        pytest.param(
            "gsc",
            "sc-domain:example.test",
            "https://deep.shop.example.test/blog/",
            True,
            id="domain-subdomains-path",
        ),
        pytest.param(
            "gsc",
            SITE + "blog/",
            "https://www.example.test/blog/post/",
            True,
            id="url-prefix-child",
        ),
        pytest.param("gsc", SITE + "blog/", SITE + "blogger/", False, id="url-prefix-lookalike"),
        pytest.param("gsc", SITE + "blog/", SITE + "other/", False, id="url-prefix-outside"),
        pytest.param("gsc", SITE, "https://shop.example.test/", False, id="url-other-subdomain"),
        pytest.param(
            "gsc",
            "sc-domain:example.test",
            "https://evil-example.test/",
            False,
            id="domain-lookalike-prefix",
        ),
        pytest.param(
            "gsc",
            "sc-domain:example.test",
            "https://example.test.evil.test/",
            False,
            id="domain-lookalike-suffix",
        ),
        pytest.param("gsc", SITE, "https://evil-example.test/", False, id="url-lookalike-prefix"),
        pytest.param(
            "bing_wmt", SITE, "https://example.test.evil.test/", False, id="bing-lookalike-suffix"
        ),
        pytest.param(
            "gsc",
            "https://EXAMPLE.test.:443/",
            "https://www.example.test./blog/",
            True,
            id="trailing-dot-default-port",
        ),
        pytest.param(
            "gsc",
            "https://exämple.test/blog/",
            "https://www.xn--exmple-cua.test/blog/post/",
            True,
            id="unicode-url-resource",
        ),
        pytest.param(
            "bing_wmt",
            "https://xn--exmple-cua.test/",
            "https://www.exämple.test/",
            True,
            id="unicode-url-target",
        ),
        pytest.param(
            "gsc",
            "sc-domain:xn--exmple-cua.test.",
            "https://shop.exämple.test/blog/",
            True,
            id="unicode-domain-target",
        ),
        pytest.param(
            "gsc", "sc-domain:www.example.test", SITE, False, id="domain-property-excludes-parent"
        ),
        pytest.param(
            "gsc",
            "sc-domain:www.example.test",
            "https://www.example.test/blog/",
            True,
            id="domain-property-retains-www",
        ),
        pytest.param("gsc", SITE, "https://example.test:8443/", False, id="url-other-port"),
    ],
)
def test_rendered_property_scope_variants(env, tmp_path, key, resource, target, connected):
    snapshot = connected_store(env, key, resource)
    text = markdown(snapshot, [summary(tmp_path, target)])
    label = service.REGISTRY[key].label
    claims = [
        line
        for line in text.splitlines()
        if line.startswith(f"- **{label} — Connected via authorised API")
    ]
    assert bool(claims) is connected, text
    rows = [
        line for line in readiness(text).splitlines() if line.startswith(f"| {label}<br>{target} |")
    ]
    assert len(rows) == 1
    assert ("Connected via authorised API" in rows[0]) is connected
    assert ("| connected_partial |" in rows[0]) is connected
    assert ("| not_configured |" in rows[0]) is not connected


def test_gsc_readiness_note_has_one_separator_full_stop(env, tmp_path):
    snapshot = connected_store(env, "gsc", SITE)
    rows = readiness(markdown(snapshot, [summary(tmp_path)])).splitlines()
    row = next(line for line in rows if line.startswith(f"| Google Search Console<br>{SITE} |"))
    assert "Synthetic partial coverage. " + AI_REPORT_NOTE in row
    assert "coverage.." not in row


@pytest.mark.parametrize(
    "key,kind",
    [
        pytest.param("gsc", "url", id="gsc-url"),
        pytest.param("gsc", "domain", id="gsc-domain"),
        pytest.param("bing_wmt", "url", id="bing-url"),
    ],
)
@pytest.mark.parametrize(
    "bound_host,target_host,connected",
    [
        pytest.param("xn--fa-hia.test", "fass.test", False, id="sharp-s-alabel-to-ss"),
        pytest.param("fass.test", "xn--fa-hia.test", False, id="ss-to-sharp-s-alabel"),
        pytest.param("faß.test", "fass.test", False, id="sharp-s-unicode-to-ss"),
        pytest.param("fass.test", "faß.test", False, id="ss-to-sharp-s-unicode"),
        pytest.param("straße.test", "strasse.test", False, id="street-to-ss"),
        pytest.param("strasse.test", "straße.test", False, id="ss-to-street"),
        pytest.param("ς.test", "σ.test", False, id="final-sigma-to-sigma"),
        pytest.param("σ.test", "ς.test", False, id="sigma-to-final-sigma"),
        pytest.param("faß.test", "xn--fa-hia.test", True, id="sharp-s-unicode-to-alabel"),
        pytest.param("xn--fa-hia.test", "faß.test", True, id="sharp-s-alabel-to-unicode"),
        pytest.param("xn--a.test", "a.test", False, id="invalid-modern-bound"),
        pytest.param("a.test", "xn--a.test", False, id="invalid-modern-target"),
    ],
)
def test_rendered_claim_uses_modern_idna(
    env, tmp_path, key, kind, bound_host, target_host, connected
):
    if kind == "domain":
        # Registered Search Console domain properties use ASCII A-labels.
        domain = {
            "faß.test": "xn--fa-hia.test",
            "straße.test": "xn--strae-oqa.test",
            "ς.test": "xn--3xa.test",
            "σ.test": "xn--4xa.test",
        }.get(bound_host, bound_host)
        resource, target = f"sc-domain:{domain}", f"https://www.{target_host}/"
    else:
        resource, target = f"https://{bound_host}/", f"https://{target_host}/"
    snapshot = connected_store(env, key, resource)
    text = markdown(snapshot, [summary(tmp_path, target)])
    label = service.REGISTRY[key].label
    claims = [
        line
        for line in text.splitlines()
        if line.startswith(f"- **{label} — Connected via authorised API")
    ]
    rows = [
        line for line in readiness(text).splitlines() if line.startswith(f"| {label}<br>{target} |")
    ]
    assert len(rows) == 1
    assert (
        bool(claims),
        "Connected via authorised API" in rows[0],
        "| connected_partial |" in rows[0],
        "| not_configured |" in rows[0],
    ) == (connected, connected, connected, not connected)
