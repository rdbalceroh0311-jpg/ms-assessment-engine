"""Exercise the real loopback HTTP server, including persistence across restarts."""

import threading
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

from ams_assess import cli, store
from ams_assess.ui import LocalServer, finding_groups


@contextmanager
def running(root):
    with LocalServer(root, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=server.origin, trust_env=False) as client:
                yield server, client
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def customer(tmp_path):
    store.set_profile(
        tmp_path,
        "synthetic",
        {
            "name": '<script>alert("profile")</script>',
            "websites": ["https://example.test"],
        },
    )
    return tmp_path


def form(server, **changes):
    return {
        "csrf": server.csrf,
        "customer": "synthetic",
        "platform": "ga4",
        "site": "https://example.test/",
        "ams_access": "granted",
        **changes,
    }


def test_loopback_and_all_request_authentication(customer):
    for host in ("0.0.0.0", "::", "localhost", "192.0.2.1"):
        with pytest.raises(ValueError, match="127.0.0.1"):
            LocalServer(customer, 0, host=host)
    with running(customer) as (server, client):
        assert server.server_address[0] == "127.0.0.1"
        for method in ("GET", "POST", "HEAD", "PUT", "OPTIONS"):
            assert client.request(method, "/").status_code == 403
        assert client.get("/?token=wrong").status_code == 403
        assert client.get(server.url(), headers={"Host": "evil.test"}).status_code == 403
        response = client.get(server.url(customer="synthetic"))
        assert response.status_code == 200
        assert "&lt;script&gt;" in response.text
        assert '<script>alert("profile")</script>' not in response.text
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    "csrf,origin", [("", "valid"), ("wrong", "valid"), ("valid", ""), ("valid", "http://evil.test")]
)
def test_posts_require_csrf_and_same_origin(customer, csrf, origin):
    before = (customer / "synthetic/assessment.json").read_bytes()
    with running(customer) as (server, client):
        data = form(server, csrf=server.csrf if csrf == "valid" else csrf)
        response = client.post(
            server.url("/update"),
            data=data,
            headers={"Origin": server.origin if origin == "valid" else origin},
        )
        assert response.status_code == 403
        assert client.get(server.url("/update")).status_code == 404
    assert (customer / "synthetic/assessment.json").read_bytes() == before


def test_manual_update_persists_after_restart_and_uses_store_history(customer):
    with running(customer) as (server, client):
        first_token = server.token
        response = client.post(
            server.url("/update"),
            data=form(
                server,
                tested="Reviewed delegated user list",
                who="Reviewer",
                ownership="customer_confirmed",
                identifier="property:123456",
                note="Manual check",
            ),
            headers={"Origin": server.origin},
        )
        assert response.status_code == 303
        assert client.get(response.headers["Location"]).status_code == 200
    with running(customer) as (server, client):
        assert server.token != first_token
        assert client.get("/?token=" + first_token).status_code == 403
        response = client.get(server.url(customer="synthetic"))
        assert "Reviewed delegated user list" in response.text
    saved = store.register_store(customer, "synthetic")["systems_register"]
    entry = next(e for e in saved["entries"] if e["platform"] == "ga4")
    assert entry["ams_access"] == "granted"
    assert entry["integration"] == "not_configured"
    assert entry["ownership"] == "customer_confirmed"
    assert entry["operational_verification"]["tested"] == "Reviewed delegated user list"
    assert saved["history"][-1]["who"] == "Reviewer"


def test_file_allowlist_traversal_symlinks_and_customer_isolation(customer):
    directory = customer / "synthetic"
    outputs = directory / "reports/001-fixture"
    outputs.mkdir(parents=True)
    (outputs / "report.html").write_text("<p>Saved report</p><script>bad()</script>")
    (outputs / "credentials.json").write_text('{"token": "never-serve"}')
    (directory / "credentials.env").write_text("NEVER=serve")
    store.set_profile(customer, "other", {"name": "Other"})
    other = customer / "other/reports/001-fixture"
    other.mkdir(parents=True)
    (other / "report.md").write_text("OTHER CUSTOMER")
    (outputs / "report.md").symlink_to(other / "report.md")
    current = store.load(customer, "synthetic")
    assert current is not None
    current["report_revisions"] = [
        {
            "directory": "reports/001-fixture",
            "files": ["report.html", "report.md", "credentials.json"],
        }
    ]
    with store.locked(customer, "synthetic"):
        store.save(customer, "synthetic", current)
    with running(customer) as (server, client):

        def get(path):
            return client.get(server.url("/file", customer="synthetic", path=path))

        response = get("reports/001-fixture/report.html")
        assert response.status_code == 200
        assert "; sandbox" in response.headers["Content-Security-Policy"]
        for path in (
            "../other/reports/001-fixture/report.md",
            "reports/001-fixture/report.md",
            "/etc/passwd",
            "assessment.json",
            "credentials.env",
            "reports/001-fixture/credentials.json",
            "reports/../assessment.json",
            "%2e%2e/other/reports/001-fixture/report.md",
        ):
            assert get(path).status_code == 403, path
        (outputs / "report.html").unlink()
        (outputs / "report.html").symlink_to(other / "report.md")
        assert get("reports/001-fixture/report.html").status_code == 403


def test_post_cannot_set_integration_or_persist_secrets(customer):
    before = (customer / "synthetic/assessment.json").read_bytes()
    with running(customer) as (server, client):
        for changes in (
            {"integration": "connected"},
            {"identifier": "password=SECRET"},
            {"platform": "not-a-platform"},
        ):
            response = client.post(
                server.url("/update"),
                data=form(server, **changes),
                headers={"Origin": server.origin},
            )
            assert response.status_code == 400
            assert "SECRET" not in response.text
    assert (customer / "synthetic/assessment.json").read_bytes() == before


def test_lifecycle_groups_use_existing_states_and_review_overrides():
    current = {
        "findings": [
            {"id": str(n), "lifecycle_status": status}
            for n, status in enumerate(
                ("current", "resolved", "not_rechecked", "needs_review", "current")
            )
        ],
        "needs_review": [{"finding_ids": ["4"]}],
    }
    groups = finding_groups(current)
    assert {k: len(v) for k, v in groups.items()} == {
        "Current": 1,
        "Resolved": 1,
        "Not rechecked": 1,
        "Needs review": 2,
    }


def test_findings_render_migration_events_and_saved_report_versions(customer):
    from ams_assess.models import Finding
    from ams_assess.report import render_customer

    current = store.load(customer, "synthetic")
    assert current is not None
    eid = current["evidence"][0]["id"]
    current["findings"] = [
        Finding(
            id="legacy-observation",
            title="Migrated check",
            category="seo",
            detail="Historical observation",
            recommendation="Recheck",
            evidence_ids=[eid],
            lifecycle_status="not_rechecked",
            events=[{"status": "identity-predecessor", "observation": {"id": "old-check"}}],
        ).model_dump(mode="json")
    ]
    with store.locked(customer, "synthetic"):
        store.save(customer, "synthetic", current)
    render_customer(customer, "synthetic", {"md", "html"})
    with running(customer) as (server, client):
        response = client.get(server.url(customer="synthetic"))
        assert response.status_code == 200
        assert "Not rechecked (1)" in response.text
        assert "identity-predecessor" in response.text
        assert "Revision 1" in response.text
        saved = store.load(customer, "synthetic")
        assert saved is not None
        path = saved["report_revisions"][0]["directory"] + "/report.html"
        assert client.get(server.url("/file", customer="synthetic", path=path)).status_code == 200


def test_ui_command_does_not_read_credentials(tmp_path, monkeypatch):
    from ams_assess import ui

    calls = []
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "config", lambda: pytest.fail("UI must never load credentials"))
    monkeypatch.setattr(ui, "serve", lambda root, port: calls.append((root, port)))
    assert cli.main(["ui", "--port", "8765"]) == 0
    assert calls == [(tmp_path / "data/customers", 8765)]


def test_get_projects_old_register_without_writes_and_refuses_legacy_migration(customer):
    current = store.load(customer, "synthetic")
    assert current is not None
    del current["systems_register"]
    with store.locked(customer, "synthetic"):
        store.save(customer, "synthetic", current)
    file = customer / "synthetic/assessment.json"
    before = file.read_bytes()
    with running(customer) as (server, client):
        assert "Google Analytics 4" in client.get(server.url(customer="synthetic")).text
    assert file.read_bytes() == before
    current["schema_version"] = 2
    with store.locked(customer, "synthetic"):
        store.save(customer, "synthetic", current)
    before = file.read_bytes()
    with running(customer) as (server, client):
        assert client.get(server.url(customer="synthetic")).status_code == 400
    assert file.read_bytes() == before
    assert not list(file.parent.glob("assessment.json.backup-*"))


def test_native_browser_form_passes_origin_checks_and_saves(customer):
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Run scripts/setup.sh for Chromium")
        with running(customer) as (server, _):
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(server.origin + server.url(customer="synthetic"))
                form = page.locator("form").filter(
                    has=page.locator('input[name="platform"][value="ga4"]')
                )
                form.locator("..").locator("summary").click()
                form.locator('select[name="ams_access"]').select_option("granted")
                form.locator('input[name="tested"]').fill("Reviewed delegated access")
                with page.expect_navigation() as navigation:
                    form.locator("button").click()
                assert navigation.value is not None and navigation.value.status == 200
                assert page.locator("h2").first.inner_text() == '<script>alert("profile")</script>'
            finally:
                browser.close()
    current = store.register_store(customer, "synthetic")
    entry = next(e for e in current["systems_register"]["entries"] if e["platform"] == "ga4")
    assert entry["ams_access"] == "granted"
    assert entry["integration"] == "not_configured"
