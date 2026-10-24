"""Offline evidence projections and real store/CLI/report register mutations."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ams_assess import cli, register, store
from ams_assess.discovery import Notes
from ams_assess.models import Connection, Context, Evidence, ModuleResult
from ams_assess.modules import presence
from ams_assess.profile import Verification
from ams_assess.report import render_customer


def fixture_run(root, *, number=1, target="https://example.test/", connected=False):
    ctx = Context(
        customer_id="synthetic",
        customer_name="Synthetic",
        target=target,
        categories={"presence"},
        connections=set(),
        config={},
        run_dir=root / "synthetic" / f"run-{number}",
        tools_dir=root,
    )
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    browser = ModuleResult(name="browser")
    browser.add_evidence(
        target,
        "Rendered page evidence (desktop)",
        {
            "url": target,
            "page": {
                "script_srcs": [
                    "https://www.googletagmanager.com/gtag/js?id=G-ABCDEF1234",
                    "https://snap.licdn.com/li.lms-analytics/insight.min.js",
                    "https://analytics.tiktok.com/pixel.js",
                ],
                "meta": [{"name": "generator", "content": "WordPress 6.5"}],
                "anchors": [
                    {"href": "https://www.facebook.com/example-business", "text": "Facebook"}
                ],
                "links": ["https://www.facebook.com/example-business"],
            },
        },
    )
    domain = ModuleResult(name="domain_email")
    domain.add_evidence(
        target,
        "DNS MX lookup for example.test",
        {
            "status": "ok",
            "records": ["10 aspmx.l.google.com", "20 x.mail.protection.outlook.com"],
        },
    )
    domain.add_evidence(target, "DNS TXT lookup for example.test", {"status": "ok", "records": []})
    domain.add_evidence(
        target,
        "Infrastructure attribution",
        {
            "infrastructure": {
                "registrar": {"value": [{"names": ["Example registrar"]}], "detail": "RDAP"},
                "dns_provider": {"value": ["Example DNS"], "detail": "NS"},
                "website_provider": {"value": [{"provider": "Example CDN"}], "detail": "CDN clue"},
            }
        },
    )
    ctx.results = [browser, domain]
    public = presence.run(ctx)
    results = [browser, domain, public]
    if connected:
        gsc = ModuleResult(name="gsc")
        eid = gsc.add_evidence(
            target,
            "Successful authorised GSC data",
            {
                "property": "sc-domain:example.test",
                "query": {},
                "page": {},
                "sitemaps": {},
            },
        )
        gsc.connections = [
            Connection(
                key="gsc",
                service="Search Console",
                status="Connected successfully",
                detail="Authorised synthetic connection test",
                evidence_ids=[eid],
            )
        ]
        results.append(gsc)
    data = {
        "target": target,
        "created_at": f"2026-10-{number:02d}T00:00:00Z",
        "status": "complete",
        "selected_categories": ["presence"],
        "assessed_categories": ["presence"],
        "findings": [],
        "ai": {"status": "skipped"},
        "modules": [r.model_dump(mode="json") for r in results],
        "pages": [target],
        "max_pages": 10,
        "limitations": [],
        "future": [],
        "report_files": [],
        "digital_presence": {c["key"]: c["value"] for c in public.checks},
    }
    evidence = [e for r in results for e in r.evidence]
    return store.merge_run(
        root, "synthetic", "Synthetic", data, evidence, f"run-{number}", ["public"]
    )


def row(current, platform, site="https://example.test/", location=""):
    return next(
        e
        for e in current["systems_register"]["entries"]
        if (e["platform"], e["site"], e["location"]) == (platform, site, location)
    )


def test_auto_population_reuses_fixture_signals_and_infrastructure(tmp_path):
    current = fixture_run(tmp_path)
    for key in (
        "ga4",
        "wordpress",
        "google_workspace",
        "m365",
        "hosting",
        "dns_provider",
        "domain_registrar",
        "facebook",
    ):
        assert row(current, key)["presence"] == "publicly_observed", key
        assert row(current, key)["ownership"] == "unconfirmed"
        assert row(current, key)["integration"] == (
            "not_configured" if key == "ga4" else "unsupported"
        )
    assert row(current, "ga4")["identifiers"] == ["G-ABCDEF1234"]
    assert row(current, "dns_provider")["identifiers"] == ["Example DNS"]
    assert row(current, "clarity")["presence"] == "unknown"
    assert row(current, "gsc")["integration"] == "supported"
    assert row(current, "wp_cli")["kind"] == "tooling"
    assert row(current, "wp_cli")["presence"] == "unknown"
    assert row(current, "linkedin")["presence"] == "unknown"
    assert row(current, "linkedin")["label"] == "LinkedIn"
    assert row(current, "linkedin_tag")["presence"] == "publicly_observed"
    assert row(current, "tiktok")["presence"] == "unknown"
    assert row(current, "tiktok_tag")["presence"] == "publicly_observed"
    assert current["systems_register"]["history"]


def test_manual_wins_conflict_history_and_access_never_connects(tmp_path):
    fixture_run(tmp_path)
    updated = store.set_register(
        tmp_path,
        "synthetic",
        "ga4",
        {
            "presence": "confirmed_absent",
            "ams_access": "granted",
            "next_action": "Review with office manager",
            "responsible": "Pat",
            "who": "Reviewer",
            "note": "Customer checked account inventory",
        },
    )
    assert updated["integration"] == "not_configured"
    assert updated["operational_verification"]["method"] == "automated"
    current = fixture_run(tmp_path, number=2)
    entry = row(current, "ga4")
    assert entry["presence"] == "confirmed_absent"
    assert entry["ams_access"] == "granted"
    assert entry["next_action"] == "Review with office manager"
    assert entry["responsible"] == "Pat"
    assert entry["needs_review"] and entry["conflicts"][0]["observed"] == "publicly_observed"
    events = current["systems_register"]["history"]
    manual = next(e for e in events if e["who"] == "Reviewer")
    assert manual["old"]["presence"] == "publicly_observed"
    assert manual["new"]["presence"] == "confirmed_absent"
    assert manual["when"] and manual["source"] == "register"
    assert (
        store.set_register(
            tmp_path,
            "synthetic",
            "ga4",
            {
                "presence": "confirmed_absent",
            },
        )["needs_review"]
        is False
    )
    same = store.register_store(tmp_path, "synthetic")
    history = copy.deepcopy(same["systems_register"]["history"])
    assert store.register_store(tmp_path, "synthetic")["systems_register"]["history"] == history


def test_connection_requires_successful_engine_evidence(tmp_path):
    fixture_run(tmp_path)
    assert (
        store.set_register(
            tmp_path,
            "synthetic",
            "gsc",
            {
                "ams_access": "granted",
            },
        )["integration"]
        == "supported"
    )
    with pytest.raises(ValueError, match="engine-controlled"):
        store.set_register(tmp_path, "synthetic", "gsc", {"integration": "connected"})
    current = fixture_run(tmp_path, number=2, connected=True)
    gsc = row(current, "gsc")
    assert gsc["integration"] == "connected"
    assert gsc["identifiers"] == ["sc-domain:example.test"]
    assert gsc["evidence"][-1]["basis"] == "authorised connection"
    assert gsc["operational_verification"]["date"] == "2026-10-02T00:00:00Z"
    # A public connection label without actual GSC evidence cannot connect a new customer.
    fake = copy.deepcopy(current)
    fake["systems_register"] = {"entries": [], "history": [], "processed": []}
    fake["evidence"] = [e for e in fake["evidence"] if e["source"] != "gsc"]
    register.refresh(fake)
    assert row(fake, "gsc")["integration"] == "supported"


@pytest.mark.parametrize(
    "field,value",
    [
        ("identifier", "sk-abcdefghijklmnopqrstuvwxyz123456"),
        ("identifier", "aB0123456789xY012345678901234567890123456789"),
        ("note", "password=hunter2"),
        ("note", "Bearer abcdefghijk"),
        ("note", "refresh_token=example"),
        ("note", '{"password": "example"}'),
        ("evidence_url", "https://example.test/?access_token=abc"),
        ("evidence_url", "https://user:pass@example.test/"),
        ("evidence_url", "HTTPS://user:pass@example.test/"),
        ("evidence_url", "https://example.test/?%2574oken=abc"),
        ("evidence_url", "https://example.test/?key=secret-value"),
        ("identifier", "-----BEGIN PRIVATE KEY-----"),
    ],
)
def test_secrets_rejected_without_write_or_echo(tmp_path, field, value):
    store.set_profile(tmp_path, "synthetic", {"name": "Synthetic"})
    path = tmp_path / "synthetic" / "assessment.json"
    before = path.read_bytes()
    with pytest.raises(ValueError) as error:
        store.set_register(tmp_path, "synthetic", "ga4", {field: value})
    assert value not in str(error.value)
    assert path.read_bytes() == before


def test_site_location_scope_and_presence_verify_reuse(tmp_path):
    fixture_run(tmp_path)
    verification = Verification(
        platform="facebook",
        state="found_likely",
        profile_url="https://facebook.com/example",
        verified_by="Person",
        date="2026-10-03",
        note="Public page reviewed",
    )
    store.add_presence_verifications(tmp_path, "synthetic", [verification])
    current = fixture_run(tmp_path, number=2, target="https://other.test/")
    assert "presence" in row(current, "facebook")["human_fields"]
    assert "presence" not in row(current, "facebook", "https://other.test/")["human_fields"]
    with pytest.raises(ValueError, match="Several websites"):
        store.set_register(tmp_path, "synthetic", "facebook", {"ams_access": "granted"})
    entry = store.set_register(
        tmp_path,
        "synthetic",
        "facebook",
        {"ams_access": "requested"},
        site="https://other.test/",
        location="Sydney",
    )
    assert entry["location"] == "Sydney"
    assert (
        row(store.register_store(tmp_path, "synthetic"), "facebook")["ams_access"]
        == "not_requested"
    )


def test_cli_and_report_include_actions_and_separate_connection_evidence(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "data/customers"
    fixture_run(root, connected=True)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert (
        cli.main(
            [
                "register",
                "set",
                "--customer",
                "synthetic",
                "--platform",
                "gsc",
                "--access",
                "granted",
                "--tested",
                "Customer confirmed delegated access",
                "--who",
                "Person",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["register", "show", "--customer", "synthetic"]) == 0
    assert json.loads(capsys.readouterr().out)["history"]
    render_customer(root, "synthetic", {"md", "html"})
    report = (root / "synthetic/report.md").read_text()
    assert "Customer action list from the register" in report
    assert "Register evidence: authorised engine connection tests" in report
    assert "Customer confirmed delegated access" in report
    assert "never implies connected" in report
    assert "sc-domain:example.test" in report
    assert (root / "synthetic/report.html").is_file()


def test_catalogue_relevance_and_customer_only_report(tmp_path):
    store.set_profile(
        tmp_path,
        "synthetic",
        {"name": "Synthetic", "industry": "b2b_contractor", "buying_model": "b2b-contract"},
    )
    current = store.register_store(tmp_path, "synthetic")
    keys = {e["platform"] for e in current["systems_register"]["entries"]}
    assert {"buy_nsw", "austender", "icn_gateway", "wp_cli", "gsc"} <= keys
    assert "fresha" not in keys
    store.set_register(
        tmp_path,
        "synthetic",
        "ga4",
        {"presence": "customer_reported", "method": "customer statement"},
    )
    render_customer(tmp_path, "synthetic", {"md"})
    assert "customer\\_reported" in (tmp_path / "synthetic/report.md").read_text()


def test_snapshot_preserves_human_fields_for_run_reports(tmp_path):
    current = fixture_run(tmp_path)
    store.set_register(tmp_path, "synthetic", "ga4", {"presence": "confirmed_absent"})
    previous = store.register_store(tmp_path, "synthetic")
    data = current["runs"][0]
    entries = register.snapshot(
        data, [Evidence.model_validate(e) for e in current["evidence"]], previous
    )
    assert next(e for e in entries if e["platform"] == "ga4")["presence"] == "confirmed_absent"
    assert previous == store.load(tmp_path, "synthetic")


def test_customer_system_statements_preserve_human_control_and_manual_tests(tmp_path):
    fixture_run(tmp_path)
    notes = Notes.model_validate(
        {
            "sections": {
                "systems_in_use": [
                    {
                        "statement": "We use GA4 and Microsoft 365",
                        "systems": ["GA4", "Microsoft 365"],
                    },
                ]
            }
        }
    )
    current, _ = store.add_discovery(tmp_path, "synthetic", "Synthetic", notes)
    assert row(current, "ga4")["presence"] == "customer_reported"
    assert row(current, "m365")["presence"] == "customer_reported"
    store.set_register(
        tmp_path,
        "synthetic",
        "ga4",
        {
            "tested": "Checked customer account settings",
            "date": "2026-10-04",
        },
    )
    current = fixture_run(tmp_path, number=5)
    assert row(current, "ga4")["presence"] == "customer_reported"
    assert row(current, "ga4")["operational_verification"]["date"] == "2026-10-04"
    assert row(current, "ga4")["evidence"][-1]["date"] == "2026-10-05T00:00:00Z"


def test_register_concurrent_writers_preserve_both_updates_and_history(tmp_path):
    fixture_run(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.set_register, tmp_path, "synthetic", "ga4", changes)
            for changes in ({"ams_access": "requested"}, {"ownership": "customer_confirmed"})
        ]
        for future in futures:
            future.result()
    current = store.register_store(tmp_path, "synthetic")
    assert row(current, "ga4")["ams_access"] == "requested"
    assert row(current, "ga4")["ownership"] == "customer_confirmed"
    assert (
        len([h for h in current["systems_register"]["history"] if h["source"] == "register"]) == 2
    )


def test_explicit_customer_wide_edit_does_not_retarget_to_website(tmp_path):
    store.set_profile(tmp_path, "synthetic", {"name": "Synthetic"})
    fixture_run(tmp_path)
    entry = store.set_register(tmp_path, "synthetic", "ga4", {"ams_access": "granted"}, site="")
    assert entry["site"] == ""
    current = store.register_store(tmp_path, "synthetic")
    assert row(current, "ga4")["ams_access"] == "not_requested"


def test_site_alias_reuses_register_entry_and_human_fields(tmp_path):
    fixture_run(tmp_path)
    store.set_register(tmp_path, "synthetic", "ga4", {"ownership": "customer_confirmed"})
    current = fixture_run(tmp_path, number=2, target="https://www.example.test/")
    entries = [e for e in current["systems_register"]["entries"] if e["platform"] == "ga4"]
    assert len(entries) == 1
    assert entries[0]["ownership"] == "customer_confirmed"
    assert entries[0]["evidence"][-1]["site"] == "https://www.example.test/"
