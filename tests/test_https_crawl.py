"""HTTPS/redirect regressions: mocks and loopback by default; public smoke is opt-in."""

import json
import os
import socketserver
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import dns.name
import dns.resolver
import httpx
import pytest

from ams_assess import ai, network, runner, store, targets
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir
from ams_assess.modules import dataforseo, domain_email, site_checks, siteone

REPO = Path(__file__).resolve().parents[1]


def context(tmp_path, **changes):
    return replace(
        Context(
            customer_id="synthetic-https",
            customer_name="Synthetic Plumbing",
            target="https://example.com/",
            categories={"seo"},
            connections=set(),
            config={},
            run_dir=create_run_dir(tmp_path, "synthetic-https"),
            tools_dir=REPO / "tools",
        ),
        **changes,
    )


def mock_crawler(monkeypatch, ctx, rows=None, exit_code=0, error=None):
    # Unit tests need neither the installed binary nor the network.
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    if not binary.exists():
        binary.parent.mkdir(parents=True)
        binary.touch()
    commands = []

    def process(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=exit_code,
            stderr="",
            stdout=json.dumps(
                {
                    "results": rows
                    if rows is not None
                    else [{"url": ctx.site_url, "status": "200", "type": 1}],
                    "error": error,
                }
            ),
        )

    monkeypatch.setattr(siteone.subprocess, "run", process)
    return commands


def test_plain_tool_identity_is_shared_by_siteone_preflight_and_probes(tmp_path, monkeypatch):
    ctx = context(tmp_path, tools_dir=tmp_path / "tools")
    client = httpx.Client
    agents = []

    def homepage(request):
        agents.append(request.headers["user-agent"])
        # Model a host that rejects browser-shaped crawler identities.
        return httpx.Response(
            200 if request.headers["user-agent"] == "AMSAssessment/1.0" else 403,
            stream=httpx.ByteStream(b"<html><body>Synthetic fixture</body></html>"),
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda transport=None, **kw: client(transport=httpx.MockTransport(homepage), **kw),
    )
    commands = mock_crawler(monkeypatch, ctx)
    result = siteone.run(ctx)
    assert result.status == "ok" and "preflight limitation" not in result.reason
    # SiteOne strips the ! and does not append its own signature. Pin the actual option,
    # rather than asserting equality to the same constant used by the implementation.
    assert "--user-agent=AMSAssessment/1.0!" in commands[0]
    assert "--accept-invalid-certs" not in commands[0]
    assert "--ignore-robots-txt" not in commands[0]
    assert "--workers=2" in commands[0] and "--max-reqs-per-sec=5" in commands[0]
    assert site_checks.collect_probes(ctx)["normal"]["status"] == 200
    # Preflight, robots.txt, sitemap, public WordPress types, and the probe.
    assert agents == ["AMSAssessment/1.0"] * 5


@pytest.mark.parametrize("failure", [403, 503, "timeout", "connect", "deadline", "location"])
def test_preflight_failure_crawls_last_safe_url(tmp_path, monkeypatch, failure):
    ctx = context(tmp_path, tools_dir=tmp_path / "tools")
    client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["user-agent"] == "AMSAssessment/1.0"
        assert request.extensions["timeout"]["read"] == targets.REQUEST_SECONDS
        if request.url.host == "example.com":
            return httpx.Response(301, headers={"location": "https://www.example.com/"})
        if failure in {"timeout", "connect"}:
            error = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise error("potential secret must not be reflected")
        if failure == "location":
            return httpx.Response(301)
        return httpx.Response(503 if failure == "deadline" else int(failure))

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda transport=None, **kw: client(transport=httpx.MockTransport(handler), **kw),
    )
    if failure == "deadline":
        ticks = iter([0, 0, targets.RESOLUTION_SECONDS + 1])
        monkeypatch.setattr(targets.time, "monotonic", lambda: next(ticks))
    commands = mock_crawler(monkeypatch, ctx)
    result = siteone.run(ctx)
    assert result.status == "ok" and "preflight limitation" in result.reason
    assert ctx.target == "https://example.com/" and ctx.site_url == "https://www.example.com/"
    assert "--url=https://www.example.com/" in commands[0]
    assert "--user-agent=AMSAssessment/1.0!" in commands[0]
    assert f"--timeout={targets.REQUEST_SECONDS}" in commands[0]
    assert "--allowed-domain-for-crawling=example.com" in commands[0]
    assert "--allowed-domain-for-crawling=www.example.com" in commands[0]
    assert "accept-invalid-certs" not in " ".join(commands[0])
    assert result.evidence[0].data["error"] and not result.evidence[0].data["unsafe"]
    assert "potential secret" not in json.dumps(result.model_dump())


@pytest.mark.parametrize("location", ["https://elsewhere.net/", "http://example.com/", "/"])
def test_unsafe_redirect_aborts_before_siteone(tmp_path, monkeypatch, location):
    ctx = context(tmp_path, tools_dir=tmp_path / "tools")
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda transport=None, **kw: client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(301, headers={"location": location})
            ),
            **kw,
        ),
    )
    commands = mock_crawler(monkeypatch, ctx)
    result = siteone.run(ctx)
    assert result.status == "error" and result.evidence[0].data["unsafe"]
    assert not commands and ctx.target == ctx.site_url


@pytest.mark.parametrize("code", ["-4:SEND-ERROR", "-1:CONNECTION-ERROR", "-2:TIMEOUT", "unknown"])
def test_siteone_failure_preserves_status_error_and_evidence(tmp_path, monkeypatch, code):
    ctx = context(tmp_path, tools_dir=tmp_path / "tools", config={"API_KEY": "secret-value"})
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    error = "The analysis has been suspended because no working URL could be found"
    mock_crawler(monkeypatch, ctx, [{"url": ctx.target, "status": code, "type": 10}], 3, [error])
    monkeypatch.setattr(runner, "REGISTRY", {"siteone": siteone})
    data = runner.run(ctx, no_ai=True, formats={"md"})
    result = data["modules"][0]
    assert result["status"] == "error"
    assert f"SiteOne could not fetch https://example.com/: {code}" in result["reason"]
    assert error in result["reason"] and "ValueError" not in result["reason"]
    failure = next(e for e in result["evidence"] if e["description"] == "SiteOne crawl failed")
    assert failure["data"]["responses"][0]["status"] == code
    assert error in (ctx.run_dir / "report.md").read_text()


def test_real_siteone_send_error_is_not_a_parser_exception(tmp_path, monkeypatch):
    binary = REPO / "tools/siteone/siteone-crawler/siteone-crawler"
    if not binary.is_file():
        pytest.skip("SiteOne missing; run scripts/setup.sh")

    class BrokenResponse(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            self.request.sendall(b"NOT HTTP\r\n\r\n")

    with socketserver.TCPServer(("127.0.0.1", 0), BrokenResponse) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ctx = context(tmp_path, target=f"http://127.0.0.1:{server.server_address[1]}/")
            monkeypatch.setattr(network, "_fixture_origins", frozenset({ctx.target.rstrip("/")}))
            result = siteone.run(ctx)
        finally:
            server.shutdown()
            thread.join()
    assert result.status == "error" and "-4:SEND-ERROR" in result.reason
    assert "no working URL could be found" in result.reason
    assert "preflight limitation" in result.reason


@pytest.mark.parametrize("requested", ["https://example.com/", "https://www.example.com/"])
def test_redirected_website_does_not_change_email_domain(tmp_path, monkeypatch, requested):
    ctx = context(
        tmp_path, target=requested, categories={"domain_email"}, tools_dir=tmp_path / "tools"
    )
    monkeypatch.setattr(
        siteone, "resolve_target", lambda _: {"resolved": "https://www.example.com/"}
    )
    mock_crawler(monkeypatch, ctx)
    assert siteone.run(ctx).status == "ok"
    queries, checked, registrations = [], [], []

    class Resolver:
        nameservers = []

        def resolve(self, host, kind):
            queries.append((str(host).rstrip("."), kind))
            if str(host).rstrip(".") == "www.example.com" and kind == "A":
                return [SimpleNamespace(to_text=lambda: "192.0.2.1")]
            raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "Resolver", lambda **_: Resolver())

    def zone(host, **kwargs):
        assert host == "example.com"
        return dns.name.from_text("example.com")

    monkeypatch.setattr(dns.resolver, "zone_for_name", zone)

    def checkdmarc(command, **kwargs):
        checked.append(command[-1])
        return SimpleNamespace(stdout='{"spf":{"valid":true},"dmarc":{"valid":true}}')

    monkeypatch.setattr(domain_email.subprocess, "run", checkdmarc)

    def rdap(url, **kwargs):
        registrations.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(domain_email, "public_get", rdap)
    result = domain_email.run(ctx)
    dns_row = next(r for r in result.checks if r["key"] == "dns")
    assert next(c for c in dns_row["checks"] if c["name"] == "A/AAAA")["status"] == "PASS"
    assert not any(f.title == "Website host does not resolve" for f in result.findings)
    assert result.evidence and ctx.target == requested
    assert ctx.site_url == "https://www.example.com/" and ctx.site_host == "www.example.com"
    assert checked == ["example.com"] and registrations == ["https://rdap.org/domain/example.com"]
    assert ("example.com", "MX") in queries and ("example.com", "TXT") in queries
    assert ("www.example.com", "A") in queries and ("www.example.com", "AAAA") in queries
    assert not any(
        "www.example.com" in host and kind not in {"A", "AAAA", "CNAME"} for host, kind in queries
    )


def test_store_keeps_requested_identity_and_resolved_url(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REGISTRY", {"siteone": siteone})
    resolved = "https://www.example.com/"
    monkeypatch.setattr(siteone, "resolve_target", lambda _: {"resolved": resolved})
    for _ in range(2):
        ctx = context(tmp_path, tools_dir=tmp_path / "tools", store_root=tmp_path)
        mock_crawler(monkeypatch, ctx)
        data = runner.run(ctx, no_ai=True, formats={"md"})
        assert data["target"] == ctx.target == "https://example.com/"
        assert data["resolved_url"] == resolved
        resolved = "https://example.com/"
    saved = store.load(tmp_path, ctx.customer_id)
    assert saved is not None
    assert [r["target"] for r in saved["runs"]] == [ctx.target, ctx.target]
    assert [r["resolved_url"] for r in saved["runs"]] == ["https://www.example.com/", ctx.target]
    report, _, _ = store.report_data(saved)
    assert report["targets"] == [{"target": ctx.target, "run_ids": ["r1", "r2"]}]


@pytest.mark.parametrize(
    "prefix", ["Customer-reported, not technically verified: ", "The customer reported that "]
)
def test_qualified_profile_summary_survives_failed_crawl(tmp_path, monkeypatch, prefix):
    ctx = context(tmp_path)
    crawl = ModuleResult(name="siteone", status="error", reason="SiteOne -4:SEND-ERROR")
    crawl.add_evidence(ctx.target, "Crawl coverage", {"successful_html_pages": 0})
    profile = Evidence(
        id="ev-profile-r1-001",
        source="profile",
        url="customer-profile",
        description="Customer profile",
        provenance="customer_statement",
        data={"name": "Synthetic Plumbing", "industry": "trades", "buying_model": "service-area"},
    )
    ctx.results = [crawl]
    summary = (
        prefix + "Synthetic Plumbing is a service-area trades business (ev-profile-r1-001). "
        "The crawl returned no pages (ev-siteone-001)."
    )

    def claude(*args, **kwargs):
        prompt = kwargs["input"]
        assert '"Customer-reported, not technically verified: "' in prompt
        assert (
            '"The customer reported ..."' in prompt and "deterministic business-context" in prompt
        )
        assert (
            json.loads(prompt.split("\nDATA:\n")[1])["collection"]["modules"][0]["status"]
            == "error"
        )
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "structured_output": {
                        "executive_summary": summary,
                        "sections": {},
                        "findings": [],
                    }
                }
            ),
        )

    monkeypatch.setattr(ai.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(ai.subprocess, "run", claude)
    result, _ = ai.analyse(ctx, [], [profile, *crawl.evidence], False)
    assert result["status"] == "ok"
    assert (
        result["executive_summary"].endswith(summary)
        and "Customer-reported" in result["executive_summary"]
    )
    bad, _, warnings = ai.validate_analysis(
        {"executive_summary": summary.removeprefix(prefix)},
        [profile, *crawl.evidence],
        ctx.categories,
        [],
    )
    assert bad["executive_summary"] == "" and warnings


@pytest.mark.parametrize(
    "outcome",
    ["enquiries", "calls", "bookings", "jobs", "customers", "visitors", "traffic", "clicks"],
)
def test_evidence_quantity_does_not_authorise_business_outcome(outcome):
    evidence = Evidence(
        id="ev-observatory-001",
        source="observatory",
        url="https://example.com/",
        description="Security checks",
        data={"tests": 12},
    )
    assert ai.unsupported_claim(
        f"This could add 12 {outcome} a week (ev-observatory-001)", [evidence]
    )
    assert not ai.unsupported_claim(
        "There were 12 security checks (ev-observatory-001)", [evidence]
    )


@pytest.mark.parametrize("status", [400, 404, 422, 429])
def test_definitive_non_denial_4xx_stops_paid_calls(tmp_path, monkeypatch, status):
    ctx = context(
        tmp_path,
        dataforseo_max_usd=1,
        config={"DATAFORSEO_LOGIN": "synthetic", "DATAFORSEO_PASSWORD": "synthetic-password"},
    )
    calls = []

    def rejected(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            status,
            json={"status_code": 40501, "status_message": "Request rejected"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", rejected)
    result = dataforseo.run(ctx)
    ctx.results = [result]
    assert result.status == "error" and f"HTTP {status}" in result.reason
    assert result.connections[0].status == "Not assessed"
    assert ctx.dataforseo_stopped and ctx.dataforseo_spent_usd == 0
    assert dataforseo.serp(ctx, "organic", "synthetic query", 1e12)["status"] == "refused"
    assert len(calls) == 1


@pytest.mark.skipif(
    os.environ.get("AMS_LIVE_SMOKE") != "1", reason="Set AMS_LIVE_SMOKE=1 for example.com HTTPS"
)
def test_live_siteone_https_example_com(tmp_path):
    ctx = context(tmp_path, max_pages=1)
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason
    responses = [e.data for e in result.evidence if e.description == "SiteOne HTTP response"]
    assert any(siteone.status_code(row["status"]) == 200 for row in responses)
    assert ctx.pages and all(url.startswith("https://example.com/") for url in ctx.pages)


def test_website_measurements_use_resolved_host(tmp_path, monkeypatch):
    from ams_assess.modules import observatory, performance, security

    ctx = context(
        tmp_path, resolved_url="https://www.example.com/", config={"CRUX_API_KEY": "synthetic"}
    )
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        data = {"lighthouseResult": {"categories": {"performance": {"score": 0.9}}}}
        return httpx.Response(200, request=httpx.Request("GET", url), json=data)

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "record": {"metrics": {"largest_contentful_paint": {"percentiles": {"p75": 1200}}}},
                "grade": "A",
                "tests_failed": 0,
            },
        )

    def socket(address, **kwargs):
        calls.append(("tls", address))
        assert address == ("93.184.216.34", 443)
        raise OSError("mock TLS unavailable")

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(security, "public_get", get)
    monkeypatch.setattr(observatory, "public_post", post)
    monkeypatch.setattr(security.socket, "create_connection", socket)
    results = [performance.run(ctx), security.run(ctx), observatory.run(ctx)]
    assert all(r.status == "ok" for r in results)
    for result in results:
        for evidence in result.evidence:
            if "performance" in evidence.data:
                assert evidence.url == ctx.target  # summary retains the requested site identity
            elif isinstance(evidence.data, dict) and evidence.data.get("scope") == "origin":
                assert evidence.url == "https://www.example.com"
            else:
                assert evidence.url == ctx.site_url
    assert [kw["params"]["url"] for u, kw in calls if "runPagespeed" in u] == [ctx.site_url] * 2
    assert [kw["json"] for u, kw in calls if "queryRecord" in u] == [
        {"url": ctx.site_url},
        {"origin": "https://www.example.com"},
    ]
    assert next(kw["params"]["host"] for u, kw in calls if "observatory" in u) == ctx.site_host
    assert any(u == ctx.site_url for u, _ in calls) and ctx.target == "https://example.com/"


def test_transport_error_on_one_page_does_not_break_downstream_checks(tmp_path, monkeypatch):
    ctx = context(tmp_path, tools_dir=tmp_path / "tools", categories={"technical", "seo"})
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    mock_crawler(
        monkeypatch,
        ctx,
        [
            {"url": ctx.target, "status": "200", "type": 1},
            {"url": ctx.target + "contact", "status": "-4:SEND-ERROR", "type": 10},
        ],
    )
    ctx.results = [siteone.run(ctx)]
    assert ctx.results[0].status == "ok" and ctx.pages == [ctx.target]
    evidence = ctx.results[0].evidence
    assert any(e.data.get("status") == "-4:SEND-ERROR" for e in evidence)
    # Only reuse supplied evidence; no HTTP probes in this unit test.
    from ams_assess.modules import presence

    monkeypatch.setattr(presence, "sitemap_check", lambda _: {})
    monkeypatch.setattr(
        site_checks,
        "collect_probes",
        lambda _: {
            "normal": {
                "url": ctx.site_url,
                "status": 200,
                "complete": True,
                "headers": {"content-type": "text/html"},
                "body": "<html><body><h1>Synthetic</h1>"
                '<a href="/contact">Contact</a></body></html>',
            },
        },
    )
    result = site_checks.run(ctx)
    assert result.status == "ok" and result.checks
