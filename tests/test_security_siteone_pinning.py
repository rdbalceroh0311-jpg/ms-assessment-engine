"""SiteOne connects only to pre-validated, pinned addresses; other evidence is discarded."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from conftest import SYNTHETIC_PUBLIC_IP, mock_siteone_traffic

from ams_assess import coverage, network
from ams_assess.models import Context, create_run_dir
from ams_assess.modules import siteone

ROOT = Path(__file__).resolve().parents[1]


def context(tmp_path, target, tools=None):
    return Context(
        "synthetic",
        "Synthetic",
        target,
        {"seo"},
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        tools or tmp_path / "tools",
        max_pages=5,
    )


def fake_crawler(monkeypatch, ctx, rows, tables=None):
    mock_siteone_traffic(monkeypatch)
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda _: {"resolved": ctx.target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    commands = []

    def crawler(command, **kwargs):
        commands.append(command)
        output = {"results": rows, "tables": tables or {}}
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(output))

    monkeypatch.setattr(siteone.subproc, "run", crawler)
    return commands


def test_every_crawl_host_is_pinned_and_self_resolving_analyzers_are_disabled(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path, "https://example.com/")
    commands = fake_crawler(monkeypatch, ctx, [{"url": ctx.target, "status": "200", "type": 1}])
    result = siteone.run(ctx)
    assert result.status == "ok"
    command = commands[0]
    assert not any(arg.startswith("--resolve=") for arg in command)
    assert any(arg.startswith("--proxy=127.0.0.1:") for arg in command)
    pinning = next(e for e in result.evidence if e.description == "SiteOne destination pinning")
    assert pinning.data["pinned"] == {
        host: SYNTHETIC_PUBLIC_IP for host in ("example.com", "www.example.com")
    }
    assert pinning.data["ports"] == [80, 443]
    analyzers = next(a for a in command if a.startswith("--analyzer-filter-regex="))
    assert "Dns" not in analyzers and "SslTls" not in analyzers
    assert "SeoAndOpenGraph" in analyzers and "Security" in analyzers
    assert "pinned per host/port" in result.reason
    assert "every connection is revalidated before sending" in result.reason


def test_host_without_validated_ipv4_is_not_crawled(tmp_path, monkeypatch):
    lookup = socket.getaddrinfo

    def dns(host, port, *args, **kwargs):
        if host == "www.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        return lookup(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    ctx = context(tmp_path, "https://example.com/")
    commands = fake_crawler(monkeypatch, ctx, [{"url": ctx.target, "status": "200", "type": 1}])
    result = siteone.run(ctx)
    assert not any("www.example.com" in arg for arg in commands[0])
    assert "Not crawled (no validated IPv4 address to pin): www.example.com" in result.reason

    # SiteOne 2.5.1 cannot pin IPv6; an IPv6-only crawl host is never handed to it.
    def v6(host, port, *args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", port, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", v6)
    ctx = context(tmp_path / "v6", "https://v6only.example/")
    commands = fake_crawler(monkeypatch, ctx, [])
    result = siteone.run(ctx)
    assert result.status == "error" and commands == []
    assert "no validated IPv4 address to pin" in result.reason


def test_responses_from_unvalidated_destinations_are_discarded(tmp_path, monkeypatch):
    ctx = context(tmp_path, "https://example.com/")
    rows = [
        {"url": "https://example.com/", "status": "200", "type": 1},
        {"url": "https://www.example.com./about", "status": "200", "type": 1},
        {"url": "http://127.0.0.1/steal?token=FAKE", "status": "200", "type": 1},
        {"url": "https://example.com:8443/other-port", "status": "200", "type": 1},
        {"url": "https://other.example/scope-escape", "status": "500", "type": 1},
    ]
    fake_crawler(monkeypatch, ctx, rows)
    result = siteone.run(ctx)
    assert result.status == "ok"
    responses = {e.url for e in result.evidence if e.description == "SiteOne HTTP response"}
    assert responses == {"https://example.com/", "https://www.example.com./about"}
    assert ctx.pages == ["https://example.com/", "https://www.example.com./about"]
    discarded = next(
        e for e in result.evidence if e.description.startswith("SiteOne responses from unvalid")
    )
    assert discarded.data["urls"] == [
        "http://127.0.0.1/steal",
        "https://example.com:8443/other-port",
        "https://other.example/scope-escape",
    ]
    assert not any("scope-escape" in u for f in result.findings for u in f.urls)
    assert "3 SiteOne responses from unvalidated destinations discarded" in result.reason


def test_explicit_port_zero_is_not_the_default_port():
    pins = {"example.com": SYNTHETIC_PUBLIC_IP}
    assert siteone.pinned_destination("https://example.com/", pins, (443,))
    assert siteone.pinned_destination("https://example.com:443/", pins, (443,))
    for url in ("https://example.com:0/", "http://example.com:0/", "https://example.com:8443/"):
        assert not siteone.pinned_destination(url, pins, (80, 443))


def security_rows():
    return [
        {
            "header": "Content-Security-Policy",
            "critical": "1",
            "warning": "0",
            "recommendation": "Content-Security-Policy header is not set.",
        }
    ]


def test_table_rows_from_unvalidated_destinations_are_discarded(tmp_path, monkeypatch):
    ctx = context(tmp_path, "https://example.com/")
    rows = [
        {"url": "https://example.com/", "status": "200", "type": 1},
        {"url": "https://other.example/x", "status": "200", "type": 1},
    ]
    page = {"title": "Home", "h1": "Home", "description": "Fixture", "robotsIndex": "1"}
    empty = {"title": "", "h1": "", "description": "", "robotsIndex": "0"}
    tables = {
        "seo": {
            "rows": [
                {"urlPathAndQuery": "/", **page},
                {"urlPathAndQuery": "https://other.example/x", **empty},
                {"urlPathAndQuery": "http://127.0.0.1:8080/admin", **empty},
            ]
        },
        "seo-headings": {
            "rows": [
                {"urlPathAndQuery": "/", "headings": "<h1> Home"},
                {"urlPathAndQuery": "https://other.example/x", "headings": "<h1> Other"},
            ]
        },
        "404": {
            "rows": [
                {"url": "https://example.com/missing", "statusCode": "404"},
                {"url": "http://127.0.0.1/admin", "statusCode": "404"},
            ]
        },
        "redirects": {
            "rows": [
                {"url": "https://www.example.com/old", "targetUrl": "https://other.example/"},
                {"url": "https://example.com:0/old", "targetUrl": "/"},
            ]
        },
        "external-urls": {
            "rows": [
                {"url": "https://other.example/", "foundOn": "https://example.com/"},
                {"url": "https://example.com/", "foundOn": "http://10.0.0.1/"},
            ]
        },
        # Crawl aggregates include the discarded response, so none of them can be used.
        "security": {"rows": security_rows()},
        "headers": {"rows": [{"header": "Server", "occurrences": "2"}]},
    }
    fake_crawler(monkeypatch, ctx, rows, tables)
    result = siteone.run(ctx)
    assert result.status == "ok"
    tabled = {e.description: e.data for e in result.evidence}
    assert tabled["SiteOne 404 table"] == [tables["404"]["rows"][0]]
    assert tabled["SiteOne redirects table"] == [tables["redirects"]["rows"][0]]
    assert tabled["SiteOne external-urls table"] == [tables["external-urls"]["rows"][0]]
    assert tabled["SiteOne seo-headings table"] == [tables["seo-headings"]["rows"][0]]
    assert "SiteOne headers table" not in tabled
    seo = [e for e in result.evidence if e.description == "SiteOne SEO metadata"]
    assert [e.url for e in seo] == ["https://example.com/"]
    assert not any(e.description.startswith("SiteOne header") for e in result.evidence)
    keys = [f.check_key for f in result.findings]
    assert not [k for k in keys if k.startswith(("missing-", "indexing-", "header-"))]
    judged = json.dumps([i.model_dump() for i in [*result.findings, *result.outcomes]])
    assert not any(host in judged for host in ("other.example", "127.0.0.1", "10.0.0.1"))
    assert tabled["SiteOne table rows from unvalidated destinations discarded"]["rows"] == [
        "seo: https://other.example/x",
        "seo: http://127.0.0.1:8080/admin",
        "seo-headings: https://other.example/x",
        "404: http://127.0.0.1/admin",
        "redirects: https://example.com:0/old",
        "external-urls: http://10.0.0.1/",
        "security table (aggregate)",
        "headers table (aggregate)",
    ]
    assert "8 SiteOne table rows from unvalidated destinations discarded" in result.reason


def test_crawl_aggregates_are_kept_when_every_response_was_pinned(tmp_path, monkeypatch):
    ctx = context(tmp_path, "https://example.com/")
    rows = [{"url": "https://example.com/", "status": "200", "type": 1}]
    fake_crawler(monkeypatch, ctx, rows, {"security": {"rows": security_rows()}})
    result = siteone.run(ctx)
    assert any(f.check_key == "header-missing:content-security-policy" for f in result.findings)
    assert "table rows" not in result.reason


class Recording(ThreadingHTTPServer):
    received: list[str]


class Recorder(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        cast(Recording, self.server).received.append(self.path)
        body = b"<html><head><title>Fixture</title></head><body><h1>Fixture</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IPv6Only(Recording):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def test_real_siteone_uses_validated_address_when_its_resolver_answers_differently(
    tmp_path, monkeypatch
):
    """Rebinding: validation sees 127.0.0.1; SiteOne's own resolver would pick the attacker.

    The OS resolver answers localhost with ::1 (the attacker) as well as 127.0.0.1. The
    collection guard is shown only 127.0.0.1, so any connection to ::1 bypassed the pin.
    """
    binary = ROOT / "tools/siteone/siteone-crawler/siteone-crawler"
    if not binary.is_file():
        pytest.skip("Run scripts/setup.sh for SiteOne")
    site = Recording(("127.0.0.1", 0), Recorder)
    try:
        attacker = IPv6Only(("::1", site.server_port), Recorder)
    except OSError:
        site.server_close()
        pytest.skip("IPv6 loopback unavailable")
    servers = [site, attacker]
    for server in servers:
        server.received = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://localhost:{site.server_port}"
    lookup = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: lookup("127.0.0.1" if host == "localhost" else host, *a, **k),
    )
    monkeypatch.setattr(network, "_fixture_origins", frozenset({origin}))
    monkeypatch.setattr(
        coverage,
        "seed_inventory",
        lambda _: {"seeds": [], "seed_limit": 10, "seeds_over_limit": []},
    )
    ctx = context(tmp_path, origin + "/", tools=ROOT / "tools")
    try:
        result = siteone.run(ctx)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert result.status == "ok", result.reason
    assert attacker.received == []
    assert "/" in site.received
    raw = json.loads((ctx.run_dir / "raw/siteone.json").read_text())
    # Self-resolving analyzers did not run, so they made no lookups or connections.
    assert "dns" not in raw.get("tables", {}) and "certificate-info" not in raw.get("tables", {})
    assert not any("ssl" in item.get("aplCode", "") for item in raw["summary"]["items"])


class Pages(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        links = "<a href='/inside'>In</a><a href='/outside'>Out</a>" if self.path == "/" else ""
        title = self.path.strip("/") or "home"
        body = f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{links}</body>"
        self.send_response(200 if self.path in {"/", "/inside", "/outside"} else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())


def test_real_siteone_relative_table_rows_are_tied_to_pinned_responses(tmp_path, monkeypatch):
    """Real SiteOne tables name only relative paths ("/", "/outside"). When a response is
    discarded, its path's rows cannot be attributed to the pinned host and are dropped."""
    binary = ROOT / "tools/siteone/siteone-crawler/siteone-crawler"
    if not binary.is_file():
        pytest.skip("Run scripts/setup.sh for SiteOne")
    site = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{site.server_port}"
    monkeypatch.setattr(network, "_fixture_origins", frozenset({origin}))
    monkeypatch.setattr(
        coverage,
        "seed_inventory",
        lambda _: {"seeds": [], "seed_limit": 10, "seeds_over_limit": []},
    )
    real, captured = siteone.subproc.run, {}

    def crawler(command, **kwargs):
        process = real(command, **kwargs)
        output = json.loads(process.stdout)
        captured["tables"] = output["tables"]
        # As if SiteOne had answered /outside from a destination that was never validated.
        for row in output["results"]:
            if row["url"].endswith("/outside"):
                row["url"] = "http://unvalidated.example/outside"
        process.stdout = json.dumps(output)
        return process

    monkeypatch.setattr(siteone.subproc, "run", crawler)
    ctx = context(tmp_path, origin + "/", tools=ROOT / "tools")
    try:
        result = siteone.run(ctx)
    finally:
        site.shutdown()
        site.server_close()
    assert result.status == "ok", result.reason
    real_paths = {row["urlPathAndQuery"] for row in captured["tables"]["seo"]["rows"]}
    assert real_paths == {"/", "/inside", "/outside"}  # SiteOne's actual relative shape
    seo = {e.url for e in result.evidence if e.description == "SiteOne SEO metadata"}
    assert seo == {origin + "/", origin + "/inside"}
    headings = next(e for e in result.evidence if e.description == "SiteOne seo-headings table")
    assert {row["urlPathAndQuery"] for row in headings.data} == {"/", "/inside"}
    discarded = next(e for e in result.evidence if "table rows from unvalidated" in e.description)
    assert {"seo: /outside", "seo-headings: /outside"} <= set(discarded.data["rows"])
    judged = json.dumps([i.model_dump() for i in [*result.findings, *result.outcomes]])
    assert "/outside" not in judged
