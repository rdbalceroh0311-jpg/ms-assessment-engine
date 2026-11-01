"""Customer-store checklists use synthetic, customer/site-scoped connection evidence."""

import copy

import pytest
from test_presence import context

from ams_assess import store
from ams_assess.models import ModuleResult
from ams_assess.modules import presence
from ams_assess.report import render_customer, render_markdown

SITE = "https://example.test/"
OTHER = "https://unrelated.test/"
CONNECTED_AT = "2026-01-15T12:00:00Z"
PLATFORMS = {
    "gsc": ("Google Search Console", SITE),
    "ga4": ("Google Analytics 4", "properties/123456"),
    "gbp": ("Google Business Profile", "locations/123456"),
    "bing_wmt": ("Bing Webmaster Tools", SITE),
}


def seed(root, customer="synthetic", site=SITE):
    store.set_profile(root, customer, {"name": "Synthetic business", "websites": [site]})
    ctx = context(root, AMS_ACCESS_EMAIL="access@ams.example")
    ctx.customer_id, ctx.target = customer, site
    result = ModuleResult(name="presence")
    summary = presence.build(ctx, [], result)
    data = {
        "target": site,
        "created_at": CONNECTED_AT,
        "status": "complete",
        "selected_categories": ["presence"],
        "assessed_categories": [],
        "findings": [],
        "modules": [result.model_dump()],
        "pages": [],
        "max_pages": 0,
        "limitations": [],
        "future": [],
        "report_files": [],
        "ai": {"status": "skipped", "reason": "Synthetic fixture"},
        "digital_presence": summary,
    }
    return store.merge_run(root, customer, "Synthetic business", data, result.evidence, site, [])


def connect(root, key, *, partial=False, customer="synthetic"):
    snapshot = store.load(root, customer)
    assert snapshot is not None
    run = next(r for r in reversed(snapshot["runs"]) if r["kind"] == "assessment")
    result = ModuleResult(name=key)
    coverage = []
    endpoints = [("resource", "connected")]
    if partial:
        endpoints += [("daily_metrics", "permission_required")]
    for endpoint, status in endpoints:
        eid = result.add_evidence(
            SITE,
            "Synthetic authenticated read",
            {
                "connector": key,
                "resource_id": PLATFORMS[key][1],
                "operation": endpoint,
                "status": status,
                "collected_at": CONNECTED_AT,
                "period": {"start": "2026-01-01", "end": "2026-01-14"},
                "coverage": {"rows_returned": int(status == "connected"), "truncated": False},
                "detail": "Synthetic endpoint result",
                "data": {},
            },
        )
        eid = store.namespaced(eid, run["run_id"])
        result.evidence[-1].id = eid
        coverage.append(
            {
                "endpoint": endpoint,
                "status": status,
                "rows": int(status == "connected"),
                "truncated": False,
                "detail": "Synthetic endpoint result",
                "evidence_id": eid,
            }
        )
    snapshot["evidence"].extend(e.model_dump() for e in result.evidence)
    snapshot.setdefault("connectors", {})[key] = {
        "resource_id": PLATFORMS[key][1],
        "status": "connected_partial" if partial else "connected",
        "last_successful_test": CONNECTED_AT,
        "coverage": coverage,
        "detail": "Synthetic connection",
    }
    store.save(root, customer, snapshot)


def checklist(root, key, *, customer="synthetic", site=SITE):
    render_customer(root, customer, {"md"})
    text = (root / customer / "report.md").read_text().replace("\\_", "_")
    section = text.split(f"### Customer access requirements: {site}\n", 1)[1].split("### ", 1)[0]
    return next(line for line in section.splitlines() if f"**{PLATFORMS[key][0]}**" in line)


@pytest.mark.parametrize("key", PLATFORMS)
@pytest.mark.parametrize("site", [SITE, "https://www.example.test/"])
def test_connected_checklist_does_not_request_access(tmp_path, key, site):
    seed(tmp_path, site=site)
    store.set_register(tmp_path, "synthetic", key, {"ams_access": "granted"}, site=site)
    connect(tmp_path, key)
    line = checklist(tmp_path, key, site=site)
    assert "Access in place" in line and "no action needed" in line
    assert "access@ams.example" not in line
    assert "Restricted user" not in line and "Viewer" not in line and "Manager" not in line


def test_partial_checklist_only_requests_missing_endpoint(tmp_path):
    seed(tmp_path)
    store.set_register(tmp_path, "synthetic", "gbp", {"ams_access": "granted"}, site=SITE)
    connect(tmp_path, "gbp", partial=True)
    line = checklist(tmp_path, "gbp")
    assert "Access in place" in line
    assert "daily_metrics: permission_required" in line
    assert "resource: connected" not in line
    assert "Manager" not in line and "no action needed" not in line


@pytest.mark.parametrize("key", PLATFORMS)
@pytest.mark.parametrize("bound", [False, True], ids=["unconfigured", "bound-untested"])
def test_granted_untested_checklist_is_not_connected(tmp_path, key, bound):
    seed(tmp_path)
    store.set_register(tmp_path, "synthetic", key, {"ams_access": "granted"}, site=SITE)
    if bound:
        snapshot = store.load(tmp_path, "synthetic")
        assert snapshot is not None
        snapshot["connectors"] = {
            key: {"resource_id": PLATFORMS[key][1], "status": "not_configured"}
        }
        store.save(tmp_path, "synthetic", snapshot)
    line = checklist(tmp_path, key)
    assert "Access reported granted; connection not yet tested" in line
    assert "Access in place" not in line and "no action needed" not in line
    assert "access@ams.example" not in line


@pytest.mark.parametrize("key", PLATFORMS)
@pytest.mark.parametrize("scope", ["website", "customer"])
@pytest.mark.parametrize("reported", [False, True], ids=["unrequested", "granted-untested"])
def test_other_scope_does_not_satisfy_checklist(tmp_path, key, scope, reported):
    seed(tmp_path)
    store.set_register(tmp_path, "synthetic", key, {"ams_access": "granted"}, site=SITE)
    connect(tmp_path, key)
    # The same customer's second site, or another customer using the same URL.
    customer, site = ("synthetic", OTHER) if scope == "website" else ("other", SITE)
    seed(tmp_path, customer, site)
    if reported:
        store.set_register(tmp_path, customer, key, {"ams_access": "granted"}, site=site)
    assert "Access in place" in checklist(tmp_path, key)
    line = checklist(tmp_path, key, customer=customer, site=site)
    assert "Access in place" not in line
    if reported:
        assert "Access reported granted; connection not yet tested" in line
    else:
        assert "access@ams.example" in line and "Access reported granted" not in line


def test_checklist_uses_register_platform_alias(tmp_path):
    seed(tmp_path)
    store.set_register(
        tmp_path, "synthetic", "google_business_profile", {"ams_access": "granted"}, site=SITE
    )
    assert "Access reported granted; connection not yet tested" in checklist(tmp_path, "gbp")


@pytest.mark.parametrize("key", PLATFORMS)
def test_unscoped_grant_applies_only_to_sole_website(tmp_path, key):
    seed(tmp_path)
    store.set_register(tmp_path, "synthetic", key, {"ams_access": "granted"}, site="")
    assert "Access reported granted; connection not yet tested" in checklist(tmp_path, key)
    seed(tmp_path, site=OTHER)
    assert "Access reported granted" not in checklist(tmp_path, key, site=OTHER)


@pytest.mark.parametrize("key", PLATFORMS)
def test_unrequested_checklist_preserves_existing_wording(tmp_path, key):
    snapshot = seed(tmp_path)
    original = next(
        row
        for row in snapshot["runs"][-1]["digital_presence"]["checklist"]
        if row["service"] == PLATFORMS[key][0]
    )
    assert original["action"] in checklist(tmp_path, key)


def test_checklist_projection_does_not_change_saved_summary(tmp_path):
    seed(tmp_path)
    connect(tmp_path, "gbp", partial=True)
    snapshot = store.load(tmp_path, "synthetic")
    assert snapshot is not None
    data, evidence, discovery = store.report_data(snapshot)
    before = copy.deepcopy(data)
    render_markdown(data, evidence, discovery)
    assert data == before
