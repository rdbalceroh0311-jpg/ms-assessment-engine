"""Regressions from validation of the integrated connector release; synthetic data only."""

import json
import re

import pytest
from test_lifecycle import merge

from ams_assess import platforms, presence_discovery, register, store, targets
from ams_assess.connectors import service
from ams_assess.connectors.connected import site_scope
from ams_assess.connectors.gbp import website
from ams_assess.lifecycle import observe
from ams_assess.models import is_local, site_authority
from ams_assess.modules import gsc, performance, presence
from ams_assess.report import mdtext, render_customer


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("conflicting", [False, True])
def test_binding_checks_legacy_customers_without_migration(tmp_path, version, conflicting):
    other = store.new_store("other", "Other")
    other["schema_version"] = version
    if conflicting:
        other["connectors"] = {"ga4": {"resource_id": "properties/123"}}
    directory = tmp_path / "other"
    directory.mkdir()
    path = directory / "assessment.json"
    original = json.dumps(other).encode()
    path.write_bytes(original)
    if conflicting:
        with pytest.raises(ValueError, match="different customer"):
            service.set_binding(tmp_path, "synthetic", "ga4", "synthetic", "properties/123")
        assert not (tmp_path / "synthetic/assessment.json").exists()
    else:
        binding = service.set_binding(tmp_path, "synthetic", "ga4", "synthetic", "properties/123")
        assert binding["status"] == "not_configured"
    assert path.read_bytes() == original
    assert not list(directory.glob("assessment.json.backup-*"))


@pytest.mark.parametrize(
    "changes",
    [{"schema_version": 999}, {"customer_id": "wrong"}, {"connectors": []}],
)
def test_binding_isolation_fails_closed_on_invalid_store(tmp_path, changes):
    other = store.new_store("other", "Other") | changes
    directory = tmp_path / "other"
    directory.mkdir()
    (directory / "assessment.json").write_text(json.dumps(other))
    with pytest.raises(ValueError):
        service.set_binding(tmp_path, "synthetic", "ga4", "synthetic", "properties/123")
    assert not (tmp_path / "synthetic/assessment.json").exists()


def test_section_seven_has_connector_table_separate_from_public_findings(tmp_path):
    original = merge(tmp_path)
    findings = original["findings"]
    for key in service.REGISTRY:
        assert service.execute(tmp_path, "synthetic", key)["status"] == "not_configured"
    render_customer(tmp_path, "synthetic", {"md", "html"})
    saved = store.load(tmp_path, "synthetic")
    assert saved is not None and saved["findings"] == findings
    text = (tmp_path / "synthetic/report.md").read_text()
    section = text.split("## 7. Connected business-system findings", 1)[1].split("\n## ", 1)[0]
    assert "| Selected resource | Access status | Connection status |" in section
    assert "| Last successful collection | Coverage | Next action |" in section
    for key, spec in service.REGISTRY.items():
        assert f"| {spec.label} | Not bound |" in section
        assert mdtext(f"Bind a resource, then ams-assess connect {key} test") in section
    html = (tmp_path / "synthetic/report.html").read_text()
    tables = re.findall(r"<table>.*?</table>", html, re.S)
    table = next(t for t in tables if "<th>Connection status</th>" in t)
    assert len(re.findall(r"<th>", table)) == 7
    assert table.count("<td>not_configured</td>") == len(service.REGISTRY)


@pytest.mark.parametrize("port", ["", ":8443"])
def test_trailing_dot_register_scope_survives_persisted_lifecycle(tmp_path, port):
    target = f"https://www.example.test.{port}/"
    normalized = f"https://example.test{port}/"
    assert store.site_key(target) == store.site_key(register.site_name(target))
    first = merge(tmp_path, target=target)
    # Simulate pre-fix persisted identity and component, retaining original evidence URLs.
    finding = first["findings"][0]
    finding["site"] = f"example.test.{port}"
    finding["component"] = target + "a"
    finding["coverage"] = [target + "a"]
    store.save(tmp_path, "synthetic", first)
    second = merge(tmp_path, run=2, target=normalized)
    assert len(second["findings"]) == 1
    assert second["findings"][0]["id"] == finding["id"]
    passed = observe(
        "siteone", "missing-title", "seo", normalized + "a", "pass", ["ev-siteone-001"]
    )
    third = merge(tmp_path, run=3, target=normalized, failures=(), outcomes=[passed])
    assert third["findings"][0]["lifecycle_status"] == "resolved"
    assert third["evidence"][0]["url"] == target


def test_trailing_dot_site_comparisons_preserve_scope_boundaries():
    dotted, plain = "https://example.test./a", "https://example.test/a"
    assert site_scope(dotted) == site_scope(plain)
    assert site_scope("sc-domain:example.test.") == site_scope("sc-domain:example.test")
    assert website(dotted) == website(plain)
    assert site_authority(dotted) == site_authority(plain)
    assert site_authority(dotted) != site_authority("https://www.example.test/a")
    assert presence.origin_of(dotted) == presence.origin_of(plain)
    assert performance.origin(dotted) == performance.origin(plain)
    assert presence_discovery.host_of(dotted) == presence_discovery.host_of(plain)
    assert presence_discovery.url_key(dotted) == presence_discovery.url_key(plain)
    assert platforms.classify("https://facebook.com./synthetic") == "facebook"
    assert targets.same_site("http://localhost.:8000", "http://127.0.0.1:8000")
    assert not targets.same_site("http://localhost.:8000", "http://127.0.0.1:8001")
    assert targets.crawl_hosts("https://example.com.", "https://example.com") == {
        "example.com",
        "www.example.com",
    }
    assert is_local("localhost.") and is_local("example.test.")
    assert gsc.matches_target("sc-domain:example.test", dotted)
    assert gsc.matches_target("https://example.test/", dotted)
    for other in [
        "https://other.test/a",
        "https://www.example.test/a",
        "http://example.test/a",
        "https://example.test:8443/a",
        "https://example.test/b",
    ]:
        assert site_scope(dotted) != site_scope(other)
    assert not gsc.matches_target("sc-domain:example.test", "https://example.test.evil.com")
    assert store.site_key("https://example.test.:8443") != store.site_key("https://example.test")
    assert store.site_key("https://example.test..") != store.site_key("https://example.test")


def test_connector_next_actions_include_required_customer():
    current = store.new_store("synthetic", "Synthetic")
    for row in service.rows(current):
        assert row["next_action"].endswith(f"connect {row['key']} test --customer synthetic")
    current["connectors"] = {"gsc": {"status": "connected"}}
    row = next(row for row in service.rows(current) if row["key"] == "gsc")
    assert row["next_action"].endswith("connect gsc collect --customer synthetic")
