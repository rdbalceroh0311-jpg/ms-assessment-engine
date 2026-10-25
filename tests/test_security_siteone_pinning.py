"""SiteOne connects only to pre-validated, pinned addresses; other evidence is discarded."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from conftest import SYNTHETIC_PUBLIC_IP

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


def fake_crawler(monkeypatch, ctx, rows):
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda _: {"resolved": ctx.target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    commands = []

    def crawler(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"results": rows}))

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
    pins = {arg for arg in command if arg.startswith("--resolve=")}
    assert pins == {
        f"--resolve={name}:{port}:{SYNTHETIC_PUBLIC_IP}"
        for name in ("example.com", "example.com.", "www.example.com", "www.example.com.")
        for port in (80, 443)
    }
    analyzers = next(a for a in command if a.startswith("--analyzer-filter-regex="))
    assert "Dns" not in analyzers and "SslTls" not in analyzers
    assert "SeoAndOpenGraph" in analyzers and "Security" in analyzers
    assert "pinned per host/port" in result.reason
    assert "not by the per-request network guard" in result.reason


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
