"""Relay refusals are transport failures, never fabricated origin HTTP responses."""

import contextlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_security_siteone_pinning import context

from ams_assess import network
from ams_assess.modules import siteone
from ams_assess.siteone_transport import crawl_proxy

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("external", ["facebook", "local_receiver"])
def test_real_siteone_preserves_external_http_redirect(
    tmp_path, http_server, monkeypatch, external
):
    receiver = http_server()
    location = (
        "https://www.facebook.com/somepage"
        if external == "facebook"
        else receiver.origin + "/somepage"
    )

    def respond(request):
        if request["path"] == "/fb":
            return 302, {"Location": location}, b""
        return 200, {"Content-Type": "text/html"}, b"<html><a href='/fb'>Social</a></html>"

    fixture = http_server(respond)
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    ctx = context(tmp_path, fixture.origin + "/", tools=ROOT / "tools")
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason
    response = next(
        e
        for e in result.evidence
        if e.description == "SiteOne HTTP response" and e.url == fixture.origin + "/fb"
    )
    assert siteone.status_code(response.data["status"]) == 302
    assert not any(f.check_key == "broken-links" for f in result.findings)
    assert receiver.received == []


@pytest.mark.parametrize("failure", ["private_dns", "dns_error"])
def test_real_siteone_refused_http_forward_is_not_a_broken_url(
    tmp_path, http_server, monkeypatch, failure
):
    served = False

    def respond(request):
        nonlocal served
        if request["path"] == "/":
            served = True
        return 200, {"Content-Type": "text/html"}, b"<html><a href='/blocked'>Next</a></html>"

    fixture = http_server(respond)
    origin = fixture.origin.replace("127.0.0.1", "localhost")
    monkeypatch.setattr(network, "_fixture_origins", frozenset({origin}))
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    lookup = socket.getaddrinfo

    def dns(host, *args, **kwargs):
        if host == "localhost" and served:
            if failure == "dns_error":
                raise socket.gaierror("Temporary synthetic DNS failure")
            return lookup("10.0.0.1", *args, **kwargs)
        return lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    ctx = context(tmp_path, origin + "/", tools=ROOT / "tools")
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason
    response = next(
        e
        for e in result.evidence
        if e.description == "SiteOne HTTP response" and e.url == origin + "/blocked"
    )
    assert siteone.status_code(response.data["status"]) < 0
    assert not any(f.check_key == "broken-links" for f in result.findings)
    assert not any(r["path"] == "/blocked" for r in fixture.received)
    unavailable = next(f for f in result.findings if f.check_key == "unavailable-urls")
    assert unavailable.confidence == "needs_verification"


@pytest.mark.parametrize("status", [200, 302, 404])
def test_siteone_discards_fetched_results_without_relay_traffic(tmp_path, monkeypatch, status):
    ctx = context(tmp_path, "https://example.com/")
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    output = {
        "results": [{"url": ctx.target, "status": str(status), "type": 1}],
        "tables": {"seo": {"rows": [{"urlPathAndQuery": "/", "title": "BYPASSED RELAY"}]}},
        "summary": {"items": [{"text": "BYPASSED RELAY"}]},
    }
    monkeypatch.setattr(
        siteone.subproc,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(output)),
    )
    result = siteone.run(ctx)
    assert result.status == "error"
    assert "without relay traffic" in result.reason
    assert ctx.pages == [] and result.findings == [] and result.outcomes == []
    assert not any(e.description == "SiteOne HTTP response" for e in result.evidence)
    assert not (ctx.run_dir / "raw/siteone.json").exists()
    assert not (ctx.run_dir / "raw/siteone.stderr.txt").exists()
    assert "BYPASSED RELAY" not in json.dumps(result.model_dump())


def test_real_siteone_proxy_bypass_fails_closed(tmp_path, http_server, monkeypatch):
    fixture = http_server(lambda _: (200, {"Content-Type": "text/html"}, b"<html>Fixture</html>"))
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    run = siteone.subproc.run

    def bypass(command, **kwargs):
        # Simulate the vendor dropping its proxy. This target is our own loopback fixture.
        return run([arg for arg in command if not arg.startswith("--proxy=")], **kwargs)

    monkeypatch.setattr(siteone.subproc, "run", bypass)
    ctx = context(tmp_path, fixture.origin + "/", tools=ROOT / "tools")
    result = siteone.run(ctx)
    assert fixture.received
    assert result.status == "error" and "without relay traffic" in result.reason
    assert not ctx.pages and not result.findings and not result.outcomes
    assert not (ctx.run_dir / "raw/siteone.json").exists()
    assert not (ctx.run_dir / "raw/siteone.stderr.txt").exists()


@pytest.mark.parametrize("target", ["host", "port", "body"])
def test_http_forward_refusals_send_no_response(http_server, monkeypatch, target):
    fixture = http_server()
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    with crawl_proxy({"127.0.0.1": "127.0.0.1"}, (fixture.server.server_port,)) as proxy:
        url = fixture.origin
        if target == "host":
            url = url.replace("127.0.0.1", "localhost")
        elif target == "port":
            url = "http://127.0.0.1:1/"
        with httpx.Client(proxy=f"http://127.0.0.1:{proxy.server_port}") as client:
            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                client.request("GET", url, content=b"body" if target == "body" else b"")
        assert proxy.carried_requests == 0
    assert fixture.received == []


def test_successful_http_crawl_records_relay_traffic(tmp_path, http_server, monkeypatch):
    fixture = http_server(lambda _: (200, {"Content-Type": "text/html"}, b"<html>Fixture</html>"))
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    proxies = []

    @contextlib.contextmanager
    def capture(pins, ports):
        with crawl_proxy(pins, ports) as proxy:
            proxies.append(proxy)
            yield proxy

    monkeypatch.setattr(siteone, "crawl_proxy", capture)
    ctx = context(tmp_path, fixture.origin + "/", tools=ROOT / "tools")
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason
    assert proxies[0].carried_requests == len(fixture.received) > 0
