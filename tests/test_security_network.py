"""Track B public egress attacks. Assertions inspect a real receiving HTTP server."""

import socket
import time
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from ams_assess import network, targets
from ams_assess.models import Context, create_run_dir
from ams_assess.modules import (
    domain_email,
    observatory,
    performance,
    presence,
    security,
    site_checks,
    siteone,
)

BLOCKED = [
    "http://127.0.0.1/",
    "http://127.1/",
    "http://2130706433/",
    "http://0177.0.0.1/",
    "http://0x7f000001/",
    "http://0x7f.0.0.1/",
    "http://10.2.3.4/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/",
    "http://100.64.0.1/",
    "http://0.0.0.0/",
    "http://224.0.0.1/",
    "http://255.255.255.255/",
    "http://[::1]/",
    "http://[::]/",
    "http://[fe80::1]/",
    "http://[fd00:ec2::254]/",
    "http://[fc00::1]/",
    "http://[ff02::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:8.8.8.8]/",
    "http://[::127.0.0.1]/",
    "http://[64:ff9b::a00:1]/",
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://[64:ff9b:1::a00:1]/",
    "http://localhost/",
    "http://localhost./",
    "http://foo.localhost/",
    "http://x.local/",
    "http://x.internal/",
    "http://metadata/",
    "file:///etc/passwd",
    "ftp://example.com/",
    "data:text/plain,test",
    "gopher://example.com/",
    "http://user:FAKE@example.com/",
    "http://@example.com/",
    "http://example.com\\@127.0.0.1/",
    "http://%31%32%37.0.0.1/",
    "http://[fe80::1%25en0]/",
    "http://example.com/\r\nHost:127.0.0.1",
    "http://0/",
    "http://017700000001/",
    "http://0x7f.1/",
    "http://[::ffff:7f00:1]/",
    "http://[0:0:0:0:0:ffff:a9fe:a9fe]/",
    "http://[2002:7f00:1::]/",
    "http://\u24dbocalhost/",
    "http://LOCALHOST/",
    "HTTP://127.0.0.1/",
    "http://example.com:0/",
    "javascript:alert(1)",
    "ws://example.com/",
    "http:///127.0.0.1/",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_unsafe_targets_never_reach_attacker(url, http_server, monkeypatch):
    attacker = http_server()
    original = httpx.HTTPTransport.handle_request

    def wire(transport, request):
        # If any forbidden URL gets as far as HTTP transport, deliver it to the trap.
        redirected = httpx.Request(request.method, attacker.origin, headers=request.headers)
        return original(transport, redirected)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", wire)
    with pytest.raises(network.UnsafeURL):
        network.public_get(url)
    assert attacker.received == []


def test_every_dns_answer_is_checked_and_rebinding_never_connects(http_server, monkeypatch):
    attacker = http_server()
    port = attacker.server.server_port
    responses = [["93.184.216.34", "127.0.0.1"], ["93.184.216.34"], ["127.0.0.1"]]

    def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in responses.pop(0)]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with pytest.raises(network.UnsafeURL):
        network.validate_public_url(f"http://rebind.example:{port}/")
    with pytest.raises(network.UnsafeURL):
        network.public_get(f"http://rebind.example:{port}/")
    assert responses == []  # hook approved public IP, transport rejected changed answer
    assert attacker.received == []


def test_checked_ip_is_the_connected_ip_with_original_host_and_tls_name(monkeypatch):
    calls = []

    def dns(host, port, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    transport = network.PublicTransport()

    def wire(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "public.example"
        assert request.extensions["sni_hostname"] == "public.example"
        return httpx.Response(200)

    monkeypatch.setattr(transport.inner, "handle_request", wire)
    with httpx.Client(transport=transport) as client:
        assert client.get("https://public.example/path").status_code == 200
    assert calls == ["public.example"]


def test_ipv6_answer_is_pinned_bracketed_and_keeps_tls_name(monkeypatch):
    def dns(host, port, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", port, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    transport = network.PublicTransport()

    def wire(request):
        assert request.url.netloc == b"[2606:2800:220:1::1]"
        assert request.headers["Host"] == "v6.example"
        assert request.extensions["sni_hostname"] == "v6.example"
        return httpx.Response(200)

    monkeypatch.setattr(transport.inner, "handle_request", wire)
    with httpx.Client(transport=transport) as client:
        assert client.get("https://v6.example/").status_code == 200


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_hops_cannot_reach_private_peer(status, http_server, monkeypatch):
    attacker = http_server()
    redirect = http_server(lambda r: (status, {"Location": attacker.origin + "/steal"}, b""))
    first = http_server(lambda r: (302, {"Location": redirect.origin}, b""))
    monkeypatch.setattr(network, "_fixture_origins", frozenset({first.origin, redirect.origin}))
    with pytest.raises(network.UnsafeURL):
        network.public_get(first.origin, follow_redirects=True)
    assert len(first.received) == len(redirect.received) == 1
    assert attacker.received == []


def test_fixture_exception_is_exact_test_only_and_never_environment_enabled(
    http_server, monkeypatch
):
    fixture = http_server()
    attacker = http_server()
    monkeypatch.setenv("AMS_ALLOW_LOCALHOST", "1")
    with pytest.raises(network.UnsafeURL):
        network.public_get(fixture.origin)
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    assert network.public_get(fixture.origin).text == "OK"
    with pytest.raises(network.UnsafeURL):
        network.public_get(attacker.origin)
    assert len(fixture.received) == 1 and attacker.received == []


def test_all_public_entrypoints_refuse_private_target(tmp_path, http_server, monkeypatch):
    attacker = http_server()
    url = attacker.origin + "/"
    ctx = Context(
        "synthetic",
        "Fixture",
        url,
        {"security", "technical", "geo"},
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        tmp_path / "tools",
    )
    with network.public_client() as client:
        assert presence.fetch(client, url, presence.origin_of(url), time.monotonic() + 5)["error"]
        assert site_checks.probe(client, url, "Fixture", time.monotonic() + 5)["error"]
    assert targets.resolve_target(url)["unsafe"]
    assert security.run(ctx).status == "error"
    # Force remote scan entrypoints past their ordinary convenience local-skip check.
    remote = SimpleNamespace(local=False, site_url=url, site_host="127.0.0.1")
    assert observatory.run(cast(Context, remote)).status == "skipped"
    assert performance.query_crux("url", url, "FAKE-KEY")["outcome"] != "ok"
    assert performance.measure_lab(url, "mobile", "FAKE-KEY")["outcome"] != "ok"
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone.subproc, "run", lambda *a, **k: pytest.fail("Unsafe crawl"))
    assert siteone.run(ctx).status == "error"
    assert attacker.received == []


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_mail_probe_never_connects_to_nonpublic_mx(address, http_server, monkeypatch):
    attacker = http_server()
    connect = socket.create_connection
    monkeypatch.setattr(domain_email, "_lookup", lambda *a: ("ok", [address]))

    def trap(*args, **kwargs):
        # Turn any attempted SMTP connection into a logged request to the local trap.
        with connect(attacker.server.server_address, timeout=3) as sock:
            sock.sendall(b"GET /smtp-canary HTTP/1.0\r\nHost: localhost\r\n\r\n")
            sock.recv(4096)
        raise OSError("Synthetic SMTP trap")

    monkeypatch.setattr(socket, "create_connection", trap)
    outcome = domain_email.smtp_tls("mx.example.test")
    assert outcome.get("error")
    assert attacker.received == []
