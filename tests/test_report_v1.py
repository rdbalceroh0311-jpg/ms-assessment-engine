"""Report-v1 acceptance scenarios: local fixture only; connected/paid calls are mocked."""

import functools
import http.server
import json
import re
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pypdf import PdfReader

from ams_assess import ai, cli, presence_discovery, runner, store
from ams_assess.models import AMS_SERVICES, Context, Evidence, Finding, create_run_dir
from ams_assess.modules import dataforseo, gsc, performance, presence
from ams_assess.report import (
    DRAFT_BANNER,
    discovery_rows,
    mdtext,
    render,
    render_customer,
    render_markdown,
)

REPO = Path(__file__).resolve().parents[1]
NOTES = REPO / "examples/discovery/notes.example.json"


@pytest.fixture
def local_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "config", dict)
    (tmp_path / "tools").symlink_to(REPO / "tools", target_is_directory=True)
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            requests.append(("GET", self.path))
            super().do_GET()

        def do_POST(self):
            requests.append(("POST", self.path))
            self.send_error(405)

    handler = functools.partial(Handler, directory=str(REPO / "fixtures/site"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = httpx.HTTPTransport.handle_request

    def only_local(self, request):
        assert request.url.host == "127.0.0.1", "Unexpected live assessment network call"
        return original(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", only_local)
    try:
        yield f"http://127.0.0.1:{server.server_port}/", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def saved(root, customer):
    value = store.load(root, customer)
    assert value is not None
    return value


def import_notes(customer, path=NOTES):
    assert cli.main(["discover", "--customer", customer, "--notes", str(path)]) == 0


def test_progressive_customer_scenario(local_fixture, tmp_path, monkeypatch):
    """Validation items 1-12, in one coherent two-customer scenario."""
    target, requests = local_fixture
    root = tmp_path / "data/customers"
    assert (
        cli.main(
            [
                "customer",
                "--customer",
                "alpha",
                "--name",
                "Harbour Demo Plumbing",
                "--industry",
                "trades",
                "--buying-model",
                "service-area",
                "--service-area",
                "Sydney",
                "--website",
                target,
                "--phone",
                "02 5550 0101",
            ]
        )
        == 0
    )

    def public(customer):
        assert (
            cli.main(
                [
                    "run",
                    "--customer",
                    customer,
                    "--target",
                    target,
                    "--no-ai",
                    "--max-pages",
                    "30",
                ]
            )
            == 0
        )
        run = next(p for p in (root / customer).iterdir() if p.is_dir() and p.name != "reports")
        for name in (
            "results.json",
            "findings.json",
            "evidence.json",
            "report.md",
            "report.html",
            "report.pdf",
        ):
            assert (run / name).stat().st_size > 0
        return run

    # (1) Public-only outputs; (5) old evidence and report revision are byte-stable later.
    alpha_run = public("alpha")
    assert not any(method == "POST" for method, _ in requests)
    render_customer(root, "alpha", {"md", "pdf"})
    before = saved(root, "alpha")
    original_evidence = {e["id"]: e for e in before["evidence"]}
    old_revision = root / "alpha" / before["report_revisions"][0]["directory"] / "report.md"
    old_bytes = old_revision.read_bytes()
    raw_bytes = (alpha_run / "evidence.json").read_bytes()
    public_text = (alpha_run / "report.md").read_text()
    assert "## 8. Operational-process" not in public_text
    assert "## 7. Connected business-system" not in public_text
    for service, status in (
        ("DataForSEO", "Not connected"),
        ("PageSpeed Insights", "Not assessed"),
        ("Google Analytics 4", "not_configured"),
        ("Google Search Console", "Not connected"),
    ):
        assert f"| {service}<br>{target} | {status} |" in public_text

    # (2) Discovery-only never crawls; (3, 4) both stage orders reach the same live findings.
    imported = json.loads(NOTES.read_text())
    imported["sections"]["business_overview"].append("Beta private marker")
    beta_notes = tmp_path / "beta.json"
    beta_notes.write_text(json.dumps(imported))
    seen = len(requests)
    import_notes("beta", beta_notes)
    render_customer(root, "beta", {"md", "pdf"})
    assert len(requests) == seen
    discovery_text = (root / "beta/report.md").read_text()
    assert "## 8. Operational-process" in discovery_text
    assert not any(f"## {n}." in discovery_text for n in (4, 5, 6, 7))
    assert "Reported by customer" in discovery_text
    import_notes("alpha")
    public("beta")
    for customer in ("alpha", "beta"):
        assert saved(root, customer)["stages_assessed"] == ["public", "discovery"]
    assert {f["check_key"] for f in saved(root, "alpha")["findings"]} == {
        f["check_key"] for f in saved(root, "beta")["findings"]
    }

    # (6) Re-import is idempotent; (7) conflicting frequency stays visible for human review.
    first = saved(root, "alpha")
    import_notes("alpha")
    assert saved(root, "alpha")["findings"] == first["findings"]
    assert saved(root, "alpha")["evidence"] == first["evidence"]
    conflicting = json.loads(NOTES.read_text())
    conflicting["sections"]["enquiries_communication"].append(
        {**conflicting["sections"]["enquiries_communication"][0], "frequency": "yearly"}
    )
    conflict_file = tmp_path / "contradiction.json"
    conflict_file.write_text(json.dumps(conflicting))
    import_notes("alpha", conflict_file)
    current = saved(root, "alpha")
    assert current["needs_review"]
    assert original_evidence.items() <= {e["id"]: e for e in current["evidence"]}.items()
    assert old_revision.read_bytes() == old_bytes
    assert (alpha_run / "evidence.json").read_bytes() == raw_bytes

    # (8) Paid search pipeline uses mocked responses; name-only evidence stays uncertain.
    ctx = Context(
        "alpha",
        "Harbour Demo Plumbing",
        "https://example.com/",
        {"presence"},
        set(),
        {"DATAFORSEO_LOGIN": "synthetic", "DATAFORSEO_PASSWORD": "synthetic"},
        create_run_dir(root, "alpha"),
        REPO / "tools",
        dataforseo_max_usd=0.30,
        profile={
            "name": "Harbour Demo Plumbing",
            "industry": "trades",
            "buying_model": "service-area",
            "service_area": ["Sydney"],
        },
    )
    paid_calls = []

    def paid(url, **kwargs):
        paid_calls.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "status_code": 20000,
                "cost": 0.002,
                "tasks": [
                    {
                        "status_code": 20000,
                        "result": [
                            {
                                "items": [
                                    {
                                        "type": "organic",
                                        "url": "https://hipages.com.au/connect/harbourdemo",
                                        "title": "Harbour Demo Plumbing Sydney",
                                    },
                                    {
                                        "type": "organic",
                                        "url": "https://www.instagram.com/harbourdemo/",
                                        "title": "Harbour Demo Plumbing",
                                    },
                                ]
                            }
                        ],
                    }
                ],
            },
        )

    from ams_assess.models import ModuleResult

    with monkeypatch.context() as mock:
        mock.setattr(httpx, "post", paid)
        discovered = presence_discovery.discover(
            ctx, ModuleResult(name="presence"), presence.site_identity([]), {}, True
        )
    states = {r["key"]: r["state"] for r in discovered["platforms"]}
    assert paid_calls and len(paid_calls) <= dataforseo.MAX_SERP_CALLS
    assert states["hipages"] == "Found — likely match"
    assert states["instagram"] == "Found — needs verification"

    # Authorised evidence flows through the runner into the same progressive store, without
    # depending on presence being selected. No public network: only the MockTransport runs.
    with monkeypatch.context() as mock:
        mock.setattr(runner, "REGISTRY", {"gsc": gsc})
        connect_gsc(mock, "siteFullUser")
        ctx.categories, ctx.connections = {"seo"}, {"gsc"}
        ctx.config = {"GOOGLE_APPLICATION_CREDENTIALS": "synthetic.json"}
        ctx.store_root = root
        outcome = runner.run(ctx, no_ai=True, formats={"md"})
        assert outcome["status"] == "partial"  # top-row coverage limitation
    assert "connected" in saved(root, "alpha")["stages_assessed"]
    render_customer(root, "alpha", {"md", "html", "pdf"})
    text = (root / "alpha/report.md").read_text()
    # (9, 10) States, mandatory report fields, conditional sections, provenance and limits.
    for service, website, status in (
        ("Google Search Console", "https://example.com/", "Connected successfully"),
        ("Google Search Console", target, "Not connected"),
        ("DataForSEO", target, "Not connected"),
        ("PageSpeed Insights", target, "Not assessed"),
        ("Google Analytics 4", target, "not_configured"),
    ):
        assert f"| {service}<br>{website} | {status} |" in text
    assert "## 7. Connected business-system findings" in text
    assert text.startswith("> " + DRAFT_BANNER)
    for required in (
        "No ranking, traffic, lead or revenue guarantees",
        "not measured AI visibility",
        "**Observed / reported:**",
        "**Source:**",
        "**Verification status:**",
        "**Potential business impact",
        "**Importance:**",
        "**Urgency:**",
        "**Recommended next action:**",
        "**AMS service category:**",
        "**Dependencies:**",
        "**Information still required:**",
        "Form submissions: NOT TESTED",
        "Needs review",
        "30-90 days",
        "90+ days",
        "existing systems",
    ):
        assert required in text
    # The subsequent GSC run did not recheck the website's P1 technical failures.
    # They remain visible with dates, but must not appear as current roadmap actions.
    assert "| Previously observed — not rechecked |" in text
    assert "### 0-30 days" not in text
    assert all(f["ams_service"] in AMS_SERVICES for f in saved(root, "alpha")["findings"])
    assert re.findall(r"^## (\d+)\.", text, re.M) == [str(i) for i in range(1, 11) if i != 3]
    known = {e["id"] for e in saved(root, "alpha")["evidence"]}
    assert set(re.findall(r"ev-[a-z_]+-r\d+-\d+", text)) <= known
    # (11) Separate customer state and owner-only data; revisions intentionally read-only.
    assert "Beta private marker" not in text
    assert not any(e["source"] == "gsc" for e in saved(root, "beta")["evidence"])
    for customer in ("alpha", "beta"):
        for path in [root / customer, *(root / customer).rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o700 if path.is_dir() else mode in {0o600, 0o400}
    # (12) PDF is generated from the master and text-extractable, including the final appendix.
    pages = PdfReader(root / "alpha/report.pdf").pages
    extracted = " ".join(" ".join(p.extract_text().split()) for p in pages)
    assert "Draft" in extracted and "Appendix: evidence index" in extracted
    assert "Operational-process and automation findings" in extracted
    assert "Beta private marker" not in extracted


def connect_gsc(monkeypatch, permission, status=200):
    auth = SimpleNamespace(valid=True, token="synthetic")
    monkeypatch.setattr(
        gsc.service_account.Credentials, "from_service_account_file", lambda *a, **k: auth
    )
    client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        if status != 200:
            return httpx.Response(status)
        if str(request.url) == gsc.SITES_URL:
            return httpx.Response(
                200,
                json={
                    "siteEntry": [
                        {"siteUrl": "sc-domain:example.com", "permissionLevel": permission}
                    ]
                },
            )
        return httpx.Response(
            200, json={"rows": [{"keys": ["synthetic query"], "clicks": 2}], "sitemap": []}
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs)
    )
    return calls


@pytest.mark.parametrize(
    ("permission", "http_status", "expected"),
    [
        ("siteUnverifiedUser", 200, "Permission required"),
        ("siteFullUser", 403, "Permission required"),
        ("siteRestrictedUser", 200, "Connected successfully"),
    ],
)
def test_gsc_readiness(tmp_path, monkeypatch, permission, http_status, expected):
    calls = connect_gsc(monkeypatch, permission, http_status)
    ctx = Context(
        "demo",
        "Demo",
        "https://example.com/",
        {"seo"},
        {"gsc"},
        {"GOOGLE_APPLICATION_CREDENTIALS": "synthetic.json"},
        create_run_dir(tmp_path, "demo"),
        tmp_path,
    )
    result = gsc.run(ctx)
    assert result.connections[0].status == expected
    assert "generative-AI" in result.connections[0].detail
    if expected == "Permission required":
        assert len(calls) == 1 and not result.evidence


def test_categories_ai_guards_and_modules_availability(tmp_path, monkeypatch, capsys):
    for category, expected in [
        ("seo", "Digital Growth"),
        ("security", "Managed IT & Technology"),
        ("discovery", "Business Systems"),
        ("technical", "Managed IT & Technology"),
    ]:
        f = Finding(
            id="test",
            title="Test",
            category=category,
            evidence_ids=["ev-test-001"],
            detail="Observed",
            recommendation="Review",
        )
        assert f.ams_service == expected and f.urgency and f.dependencies and f.business_impact
    for category, expected in (("seo", "Digital Growth"), ("legacy", "Business Systems")):
        for legacy in (None, "Other", "Content and copywriting", "Conversion optimisation"):
            migrated = Finding.model_validate(
                {**f.model_dump(), "category": category, "ams_service": legacy}
            )
            assert migrated.ams_service == expected
    evidence = [Evidence(id="ev-test-001", source="test", url="u", description="d", data={})]
    for claim in ("999 leads lost", "will increase revenue", "guaranteed rankings"):
        output, _, warnings = ai.validate_analysis(
            {"executive_summary": f"{claim} (ev-test-001)."}, evidence, {"seo"}, []
        )
        assert not output["executive_summary"] and warnings
    for invalid in ("Website improvement", "Other", "Content and copywriting"):
        output, findings, warnings = ai.validate_analysis(
            {"findings": [{**f.model_dump(), "category": "seo", "ams_service": invalid}]},
            evidence,
            {"seo"},
            [],
        )
        assert not findings and warnings
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["modules"]) == 0
    assert "siteone: unavailable" in capsys.readouterr().out
    binary = tmp_path / "tools/siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.write_text("synthetic")
    binary.chmod(0o700)
    assert cli.main(["modules"]) == 0
    assert "siteone: available" in capsys.readouterr().out


def test_performance_and_paid_permission_status(tmp_path, monkeypatch):
    ctx = Context(
        "demo",
        "Demo",
        "https://example.com/",
        {"performance", "seo"},
        set(),
        {
            "CRUX_API_KEY": "synthetic",
            "DATAFORSEO_LOGIN": "synthetic",
            "DATAFORSEO_PASSWORD": "synthetic",
        },
        create_run_dir(tmp_path, "demo"),
        tmp_path,
        dataforseo_max_usd=0.10,
    )

    def forbidden(url, **kwargs):
        return httpx.Response(403, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    assert {c.status for c in performance.run(ctx).connections} == {"Permission required"}
    assert dataforseo.run(ctx).connections[0].status == "Permission required"


def test_synthetic_example_snapshot_rerenders_with_discovery(tmp_path):
    example = REPO / "examples/fixture-report"
    for name in ("results.json", "evidence.json"):
        (tmp_path / name).write_bytes((example / name).read_bytes())
    render(tmp_path, {"md"})
    assert (tmp_path / "report.md").read_text() == (example / "report.md").read_text()


def test_item_5_legacy_ai_run_and_store_remain_readable(tmp_path, monkeypatch):
    """Exercise the actual pre-v1 AI labels, both in run files and an existing store."""
    run = tmp_path / "legacy-run"
    run.mkdir()
    originals = {}
    for name in ("results.json", "evidence.json"):
        originals[name] = subprocess.run(
            ["git", "show", f"33710e5:examples/fixture-report/{name}"],
            cwd=REPO,
            check=True,
            capture_output=True,
        ).stdout
        (run / name).write_bytes(originals[name])
    data = json.loads(originals["results.json"])
    evidence = [Evidence.model_validate(e) for e in json.loads(originals["evidence.json"])]
    legacy = {f["check_key"] or f["id"]: f["ams_service"] for f in data["findings"]}
    legacy["security-headers"] = next(
        (service for key, service in legacy.items() if key.startswith("header-missing:")),
        legacy.get("security-headers"),
    )
    assert {"Content and copywriting", "Conversion optimisation"} <= set(legacy.values())
    render(run, {"md", "pdf"})
    assert "Draft" in PdfReader(run / "report.pdf").pages[0].extract_text()
    assert "**AMS service category:** Digital Growth" in (run / "report.md").read_text()

    root = tmp_path / "data/customers"
    customer = "legacy"
    value = store.merge_run(root, customer, "Legacy", data, evidence, "old-run", ["public"])
    # Reproduce a pre-v1 store, rather than only testing new ingestion of old run files.
    for finding in value["findings"]:
        finding["ams_service"] = legacy[finding["check_key"]]
    store.save(root, customer, value)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["report", "--customer", customer, "--formats", "md"]) == 0
    revision = root / customer / saved(root, customer)["report_revisions"][0]["directory"]
    revision_bytes = (revision / "report.md").read_bytes()
    merged = store.merge_run(root, customer, "Legacy", data, evidence, "new-run", ["public"])
    assert len(merged["findings"]) == len(value["findings"])
    assert all(f["ams_service"] in AMS_SERVICES for f in merged["findings"])
    assert {e["id"]: e for e in value["evidence"]}.items() <= {
        e["id"]: e for e in merged["evidence"]
    }.items()
    # Discovery also validates the existing store via _combine.
    for finding in merged["findings"]:
        finding["ams_service"] = legacy[finding["check_key"]]
    store.save(root, customer, merged)
    import_notes(customer)
    assert cli.main(["report", "--customer", customer, "--formats", "md"]) == 0
    assert (revision / "report.md").read_bytes() == revision_bytes
    assert all((run / name).read_bytes() == content for name, content in originals.items())


@pytest.mark.parametrize(
    ("target", "config", "expected"),
    [
        ("https://example.com/", {}, "Not connected"),
        ("http://127.0.0.1:18765/", {}, "Not connected"),
        (
            "http://127.0.0.1:18765/",
            {"GOOGLE_APPLICATION_CREDENTIALS": "synthetic.json"},
            "Not assessed",
        ),
    ],
)
def test_gsc_missing_credentials_precede_local_skip(
    tmp_path, monkeypatch, target, config, expected
):
    def no_network(*args, **kwargs):
        pytest.fail("GSC must not make a request without credentials or on a local target")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    ctx = Context(
        "demo",
        "Demo",
        target,
        {"seo"},
        {"gsc"},
        config,
        create_run_dir(tmp_path, "demo"),
        tmp_path,
    )
    result = gsc.run(ctx)
    assert result.status == "skipped" and not result.evidence
    assert result.connections[0].status == expected


def test_report_review_basis_and_concise_customer_actions():
    example = REPO / "examples/fixture-report"
    data = json.loads((example / "results.json").read_text())
    evidence = [
        Evidence.model_validate(e) for e in json.loads((example / "evidence.json").read_text())
    ]
    held = next(f for f in data["findings"] if f["check_key"] == "broken-links")
    data["needs_review"] = [
        {
            "finding_id": held["id"],
            "title": held["title"],
            "reason": "Conflicting observations",
            "evidence_ids": held["evidence_ids"],
        }
    ]
    manual = Evidence(
        id="ev-presence_verification-r9-001",
        source="presence_verification",
        url="https://example.com/profile",
        description="Customer verified the public profile",
        data={},
        provenance="manual_verification",
    )
    evidence.append(manual)
    data["findings"].append(
        Finding(
            id="manual-profile",
            title="Profile needs a correction",
            category="presence",
            evidence_ids=[manual.id],
            detail="The verifier recorded an outdated profile",
            recommendation="Ask the profile owner to review the recorded details",
        ).model_dump()
    )
    data["customer_name"] = "Customer <script>alert('test')</script> & Company"
    original = json.dumps(data, sort_keys=True)
    text = render_markdown(data, evidence, data["discovery"])
    assert json.dumps(data, sort_keys=True) == original
    before_review, review = text.split("## 10. Items requiring customer verification", 1)
    assert held["id"] not in before_review
    assert f"**Needs review:** {held['id']}: {held['title']}" in review
    manual_block = text.split("#### manual-profile:", 1)[1].split("##", 1)[0]
    assert "**Basis:** Manually verified." in manual_block
    assert "**Basis:** Inferred" not in manual_block
    assert "not a technical measurement" in manual_block
    assert "**Manually verified** means a person's recorded observation" in text
    assert "<script>" not in text and r"\<script\>" in text
    assert "### Functionality & accessibility" in text
    assert "### Sales & quoting" in text

    roadmap = before_review.split("## 9.", 1)[1]
    assert not re.search(r"^\d+\. ", roadmap, re.M)
    assert all(f"### {phase}" in roadmap for phase in ("0-30 days", "30-90 days", "90+ days"))
    for f in data["findings"]:
        if f["id"] == held["id"]:
            continue
        block = text.split(f"#### {mdtext(f['id'])}:", 1)[1].split("##", 1)[0]
        assert "\n\n**AMS service category:**" in block
        assert ("prefer keeping and integrating existing systems" in block) == (
            f["ams_service"] in {"Business Systems", "AI & Automation"}
        )
        assert roadmap.count(f"({mdtext(f['id'])})") == 1
        assert mdtext(f["recommendation"]) in roadmap
        if f["check_key"].startswith("site:"):
            assert not re.search(rf"^\| {re.escape(mdtext(f['title']))} \(", text, re.M)

    discovery = text.split("### Platform presence discovery", 1)[1].split(
        "### Website and email", 1
    )[0]
    assert discovery.count("<br>Not applicable | ") == 1
    assert "Tripadvisor, OpenTable, Uber Eats" in discovery
    assert "| hipages (high)<br>Not assessed |" in discovery
    assert "True Local, Localsearch, Houzz" in discovery
    assert "Manual verification tasks" in review and "**hipages**" in review
    platforms = data["digital_presence"]["discovery"]["platforms"]
    assert discovery.count("| Not searched:") == len(
        {
            (r["detail"], r["relevance"])
            for r in platforms
            if r["detail"].startswith("Not searched:")
        }
    )
    # Candidate profiles and manual verifications must keep their individual rows.
    row = next(r for r in platforms if r["platform"] == "hipages")
    protected = [{**row, "manual": {"note": "Verified"}}, {**row, "candidates": [{"url": "u"}]}]
    assert discovery_rows([row, *protected]) == [row, *protected]


def test_potential_impact_is_specific_without_invented_metrics():
    base = {
        "id": "impact",
        "title": "Observed issue",
        "category": "technical",
        "severity": "high",
        "evidence_ids": ["ev-test-001"],
        "detail": "Observed",
        "recommendation": "Review",
    }
    for key, phrase in (
        ("broken-links", "Broken links"),
        ("tls-expiry", "certificate"),
        ("health:registration:expiry", "registration"),
        ("health:email_auth:spf", "impersonate"),
        ("health:mail_server:mx unresolvable", "email"),
        ("health:dnsbl:bl.spamcop.net", "spam"),
        ("site:noindex", "search results"),
        ("site:robots", "crawlers"),
        ("site:structured-data", "markup"),
        ("axe:color-contrast", "contrast"),
        ("axe:image-alt", "images"),
    ):
        impact = Finding.model_validate({**base, "check_key": key}).business_impact
        assert phrase in impact and "may" in impact.lower()
        assert not re.search(r"\d", impact)
    fallback = Finding.model_validate({**base, "check_key": "unknown-check"}).business_impact
    assert fallback == Finding.model_validate({**base, "severity": "low"}).business_impact
    supplied = Finding.model_validate({**base, "business_impact": "Potential disruption"})
    assert supplied.business_impact == "Potential disruption"
