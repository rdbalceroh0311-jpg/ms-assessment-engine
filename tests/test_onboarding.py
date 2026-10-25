"""Onboarding projection and UI section over the existing store; synthetic customers only."""

from test_customer_isolation import BUSINESSES, bind, execute, ui
from test_customer_isolation import world as world

from ams_assess import onboarding, register, store
from ams_assess.connectors import REGISTRY
from ams_assess.report import render_customer


def row(world, owner, key):
    current = store.load(world.root, owner)
    assert current is not None
    register.refresh(current)
    return next(r for r in onboarding.rows(current) if r["key"] == key)


def test_every_connector_has_customer_instructions_and_register_mapping():
    assert set(onboarding.INSTRUCTIONS) == set(onboarding.PLATFORM_KEYS) == set(REGISTRY)
    text = " ".join(onboarding.INSTRUCTIONS.values())
    for phrase in (
        "Restricted",
        "Read-only user",
        "Viewer",
        "Manager",
        "super admin",
        "consent",
        "SiteGround",
        "WP Engine",
        "mark the internal WordPress assessment unavailable",
    ):
        assert phrase in text
    assert "password" not in text.lower()


def test_access_granted_is_not_connected_and_steps_follow_the_sequence(world):
    site = BUSINESSES["alpha"].site
    first = row(world, "alpha", "ga4")
    assert (first["engine"], first["connected"], first["next_step"]) == (
        "Not connected",
        False,
        "Discover systems",
    )
    store.set_register(
        world.root,
        "alpha",
        "ga4",
        {"presence": "customer_reported", "ownership": "customer_confirmed", "note": "Uses GA4"},
        site,
    )
    assert row(world, "alpha", "ga4")["next_step"] == "Request access"
    store.set_register(world.root, "alpha", "ga4", {"ams_access": "granted"}, site)
    granted = row(world, "alpha", "ga4")
    assert granted["access"] == "granted"
    assert (granted["engine"], granted["connected"]) == ("Not connected", False)
    assert granted["next_step"] == "Authenticate (CLI)"

    bind(world, "alpha", "ga4")
    bound = row(world, "alpha", "ga4")
    assert (bound["resource"], bound["next_step"]) == ("properties/918273645", "Test")
    assert bound["connected"] is False

    execute(world, "alpha", "ga4")
    tested = row(world, "alpha", "ga4")
    assert (tested["engine"], tested["connected"], tested["next_step"]) == (
        "Connected",
        True,
        "Collect",
    )
    bind(world, "alpha", "gsc")
    execute(world, "alpha", "gsc", collect=True)
    collected = row(world, "alpha", "gsc")
    assert collected["last_successful_collection"] != "Never"
    # A connected read shows the platform is in use; it never confirms customer ownership.
    assert collected["next_step"] == "Confirm ownership"
    store.set_register(
        world.root, "alpha", "gsc", {"ownership": "customer_confirmed", "ams_access": "granted"}
    )
    assert row(world, "alpha", "gsc")["next_step"] == "Report"
    render_customer(world.root, "alpha", {"md"})
    assert row(world, "alpha", "gsc")["next_step"] == "Complete"


def test_permission_and_provider_blockers_are_distinct(world):
    bind(world, "alpha", "ga4")
    world.providers.failures["alpha"] = "403"
    execute(world, "alpha", "ga4")
    denied = row(world, "alpha", "ga4")
    assert denied["engine"] == "Permission required"
    assert "analytics.readonly" in denied["missing_permission"]

    bind(world, "alpha", "gbp")
    world.providers.failures["alpha"] = "approval"
    execute(world, "alpha", "gbp")
    blocked = row(world, "alpha", "gbp")
    assert blocked["engine"] == "Blocked — provider API approval pending"
    assert blocked["engine"] != denied["engine"]
    current = store.load(world.root, "alpha")
    assert current is not None
    entry = next(e for e in current["systems_register"]["entries"] if e["platform"] == "gbp")
    store.set_register(
        world.root,
        "alpha",
        "gbp",
        {
            "presence": "customer_reported",
            "ownership": "customer_confirmed",
            "ams_access": "granted",
        },
        entry["site"],
    )
    current = store.load(world.root, "alpha")
    assert current is not None
    entry = next(e for e in current["systems_register"]["entries"] if e["platform"] == "gbp")
    assert entry["integration"] == "approval_required"
    assert entry["next_action"].startswith("Blocked by the provider, not customer access")


def test_bing_places_and_apple_maps_manual_workflows(world):
    site = BUSINESSES["alpha"].site
    assert "flag for customer confirmation" in row(world, "alpha", "bing_places")["next_action"]
    store.set_register(
        world.root, "alpha", "bing_places", {"presence": "publicly_observed", "note": "Found"}, site
    )
    found = row(world, "alpha", "bing_places")
    assert found["next_action"].startswith("Found: record the listing URL")
    assert found["engine"] == "Manual public workflow — no connector"
    store.set_register(
        world.root, "alpha", "apple_maps", {"presence": "confirmed_absent", "note": "None"}, site
    )
    missing = row(world, "alpha", "apple_maps")
    assert "Apple Business Connect" in missing["next_action"]
    assert "customer creates or claims" in missing["next_action"]


def test_ui_shows_onboarding_for_the_selected_customer_only(world):
    bind(world, "alpha", "gsc")
    bind(world, "bravo", "gsc")
    with ui(world.root) as (server, client):
        page = client.get(server.url(customer="alpha")).text
    assert 'id="onboarding"' in page
    for _, title, _ in onboarding.SEQUENCE:
        assert title in page
    assert "ams-assess connect &lt;connector&gt; test --customer alpha" in page
    assert "Read-only user of your verified site" in page
    assert "Blocked" not in page or "Blocked — provider" in page
    assert BUSINESSES["bravo"].resources["gsc"] not in page
    assert "--customer bravo" not in page
