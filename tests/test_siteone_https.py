"""Real SiteOne + hostname certificates, SNI routing and local-only redirect fixtures."""

import contextlib
import json
import socket
import ssl
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NotRequired, TypedDict

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_security_process_cancel import cancelled
from test_security_siteone_pinning import context

from ams_assess import network, subproc
from ams_assess.modules import siteone
from ams_assess.siteone_transport import CrawlProxy, crawl_proxy

ROOT = Path(__file__).resolve().parents[1]
TLS_TOOLS = ROOT / "tools/siteone-test-ca"
HOSTS = ("fixture.example.org", "www.fixture.example.org")


class FixtureState(TypedDict):
    redirect: str
    links: str
    hold: threading.Event
    on_jump: NotRequired[Callable[[], None]]


@pytest.fixture
def https_site(tmp_path, http_server, monkeypatch):
    if not (TLS_TOOLS / "siteone/siteone-crawler/siteone-crawler").is_file():
        pytest.fail("Build the verified-TLS fixture crawler: scripts/setup-siteone-test-ca.sh")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fixture CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), True
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in HOSTS]), False)
        .sign(key, hashes.SHA256())
    )
    certificate, private_key = tmp_path / "cert.pem", tmp_path / "key.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    # A proper CA and leaf: Rustls rejects a CA certificate used as the server leaf.
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTS[0])]))
        .issuer_name(name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, False, False), True
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in HOSTS]), False)
        .sign(key, hashes.SHA256())
    )
    leaf_path = tmp_path / "leaf.pem"
    leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(leaf_path, private_key)
    sni = []

    def route(connection, hostname, ctx):
        sni.append(hostname)
        if hostname not in HOSTS:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        return None

    tls.set_servername_callback(route)
    state: FixtureState = {"redirect": "", "links": "", "hold": threading.Event()}

    def respond(request):
        if request["path"] == "/slow":
            state["hold"].wait(30)
        if request["path"] == "/robots.txt":
            return 200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"
        if request["path"] == "/jump":
            if callback := state.get("on_jump"):
                callback()
            return 302, {"Location": state["redirect"]}, b""
        if {k.lower(): v for k, v in request["headers"].items()}["host"].startswith(HOSTS[0] + ":"):
            return 301, {"Location": origins[1] + request["path"]}, b""
        return (
            200,
            {"Content-Type": "text/html", "Set-Cookie": "session=synthetic; Secure; HttpOnly"},
            ("<html><title>Fixture</title><h1>Fixture</h1>" + state["links"] + "</html>").encode(),
        )

    server = http_server(respond, tls=tls)
    origins = [server.origin.replace("127.0.0.1", host) for host in HOSTS]
    lookup = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: lookup("127.0.0.1" if host in HOSTS else host, *a, **k),
    )
    monkeypatch.setattr(network, "_fixture_origins", frozenset(origins))
    trust = ssl.create_default_context(cafile=str(certificate))
    init = network.PublicTransport.__init__

    def trusted(self):
        init(self)
        self.inner.close()
        self.inner = httpx.HTTPTransport(
            verify=trust, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )

    monkeypatch.setattr(network.PublicTransport, "__init__", trusted)
    environment = subproc.environment
    monkeypatch.setattr(
        subproc,
        "environment",
        lambda kind, source=None: {
            **environment(kind, source),
            **({"AMS_SITEONE_TEST_CA": str(certificate)} if kind == "siteone" else {}),
        },
    )
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    server.origins, server.sni, server.state = origins, sni, state
    server.trust, server.tls = trust, tls
    return server


def test_real_siteone_pinned_https_preserves_sni_host_and_redirect(tmp_path, https_site):
    site = https_site
    # The existing pinned Python transport authenticates the hostname at this same IP.
    assert network.public_get(site.origins[1]).status_code == 200
    ctx = context(tmp_path, site.origins[0] + "/", tools=TLS_TOOLS)
    result = siteone.run(ctx)
    assert result.status == "ok", (
        result.reason,
        (ctx.run_dir / "raw/siteone.stderr.txt").read_text(),
    )
    assert ctx.site_url == site.origins[1] + "/"
    assert ctx.pages == [site.origins[1] + "/"]
    assert set(site.sni) == set(HOSTS)
    assert all(
        {k.lower(): v for k, v in r["headers"].items()}["host"]
        in {o.removeprefix("https://") for o in site.origins}
        for r in site.received
    )
    raw = json.loads((ctx.run_dir / "raw/siteone.json").read_text())
    assert any(row["url"] == ctx.site_url and row["status"] == "200" for row in raw["results"])


def test_real_siteone_original_resolve_failure(tmp_path, https_site, monkeypatch):
    """The installed binary and CA-enabled build both lose SNI with old --resolve."""
    site = https_site
    run = subproc.run

    def original(command, **kwargs):
        command = [arg for arg in command if not arg.startswith("--proxy=")]
        for host in HOSTS:
            command.append(f"--resolve={host}:{site.server.server_port}:127.0.0.1")
        return run(command, **kwargs)

    monkeypatch.setattr(subproc, "run", original)
    for index, tools in enumerate((ROOT / "tools", TLS_TOOLS)):
        site.sni.clear()
        ctx = context(tmp_path / str(index), site.origins[1] + "/", tools=tools)
        result = siteone.run(ctx)
        assert result.status == "error"
        assert "-1:CONN-FAIL (exit 3)" in result.reason
        assert None in site.sni


def test_real_siteone_follows_https_bare_to_www_during_crawl(tmp_path, https_site):
    site = https_site
    site.state["links"] = "<a href='/jump'>Redirect</a>"
    site.state["redirect"] = site.origins[0] + "/finish"
    ctx = context(tmp_path, site.origins[0] + "/", tools=TLS_TOOLS)
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason
    assert site.origins[1] + "/finish" in ctx.pages
    assert any(
        r["path"] == "/finish"
        and {k.lower(): v for k, v in r["headers"].items()}["host"].startswith(HOSTS[0] + ":")
        for r in site.received
    )
    assert any(e.url == site.origins[0] + "/finish" for e in result.evidence)


@pytest.mark.parametrize(
    "location",
    [
        "https://127.0.0.1/secret",
        "https://10.0.0.1/secret",
        "https://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_real_siteone_blocks_unsafe_https_redirects(tmp_path, https_site, location):
    site = https_site
    site.state["links"] = "<a href='/jump'>Redirect</a>"
    site.state["redirect"] = location
    ctx = context(tmp_path, site.origins[1] + "/", tools=TLS_TOOLS)
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason
    assert any(r["path"] == "/jump" for r in site.received)
    assert all(
        {k.lower(): v for k, v in r["headers"].items()}["host"].startswith(HOSTS[1] + ":")
        for r in site.received
    )
    assert location not in ctx.pages


@pytest.mark.parametrize("bad_address", ["10.0.0.1", "169.254.169.254", "::1", "93.184.216.34"])
def test_proxy_revalidates_dns_before_every_connect(https_site, monkeypatch, bad_address):
    site = https_site
    port = site.server.server_port
    with crawl_proxy({HOSTS[1]: "127.0.0.1"}, (port,)) as proxy:
        with httpx.Client(
            proxy=f"http://127.0.0.1:{proxy.server_port}", verify=site.trust
        ) as client:
            assert client.get(site.origins[1]).status_code == 200
            received = len(site.received)
            lookup = socket.getaddrinfo
            monkeypatch.setattr(
                socket,
                "getaddrinfo",
                lambda host, *a, **k: lookup(bad_address if host == HOSTS[1] else host, *a, **k),
            )
            with pytest.raises(httpx.ProxyError, match="403"):
                client.get(site.origins[1] + "/private")
            assert len(site.received) == received


@pytest.mark.parametrize("failure", ["untrusted", "hostname"])
def test_real_siteone_rejects_bad_tls_before_http(tmp_path, https_site, monkeypatch, failure):
    site = https_site
    target = site.origins[1] + "/"
    if failure == "untrusted":
        env = subproc.environment
        monkeypatch.setattr(
            subproc,
            "environment",
            lambda kind, source=None: {
                key: value
                for key, value in env(kind, source).items()
                if key != "AMS_SITEONE_TEST_CA"
            },
        )
    else:
        target = site.origin + "/"  # CA trusted, but certificate has no IP SAN.
        monkeypatch.setattr(network, "_fixture_origins", network._fixture_origins | {site.origin})
        site.tls.set_servername_callback(lambda *args: None)
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    ctx = context(tmp_path, target, tools=TLS_TOOLS)
    result = siteone.run(ctx)
    assert result.status == "error"
    assert "-1:CONN-FAIL" in result.reason
    assert site.received == []


def test_real_siteone_https_credentials_are_not_forwarded(tmp_path, https_site):
    site = https_site
    secret_url = site.origins[1].replace("https://", "https://user:password@") + "/credentials"
    site.state["links"] = f"<a href='{secret_url}'>Link</a><a href='/jump'>Redirect</a>"
    site.state["redirect"] = secret_url
    ctx = context(tmp_path, site.origins[1] + "/", tools=TLS_TOOLS)
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason
    assert all(
        not {k.lower() for k in r["headers"]} & {"authorization", "proxy-authorization", "cookie"}
        for r in site.received
    )


def test_sigterm_stops_real_https_crawler_and_proxy(tmp_path, https_site, monkeypatch):
    site = https_site
    site.state["links"] = "<a href='/slow'>Slow</a>"
    proxies = []

    @contextlib.contextmanager
    def capture(pins, ports):
        with crawl_proxy(pins, ports) as proxy:
            proxies.append(proxy)
            yield proxy

    monkeypatch.setattr(siteone, "crawl_proxy", capture)
    ctx = context(tmp_path, site.origins[1] + "/", tools=TLS_TOOLS)
    try:
        cancelled(
            lambda: siteone.run(ctx), lambda: any(r["path"] == "/slow" for r in site.received)
        )
    finally:
        site.state["hold"].set()
    assert len(proxies) == 1 and proxies[0].stopped.is_set()
    assert all(connection.fileno() == -1 for connection in proxies[0].sockets)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxies[0].server_port), timeout=1)


def test_proxy_http_drops_credentials_and_refuses_unpinned_targets(http_server, monkeypatch):
    site = http_server(
        lambda _: (
            200,
            {"X-Content-Type-Options": "nosniff", "Set-Cookie": "session=synthetic; HttpOnly"},
            b"OK",
        )
    )
    attacker = http_server()
    monkeypatch.setattr(network, "_fixture_origins", frozenset({site.origin, attacker.origin}))
    with crawl_proxy({"127.0.0.1": "127.0.0.1"}, (site.server.server_port,)) as proxy:
        with httpx.Client(proxy=f"http://127.0.0.1:{proxy.server_port}") as client:
            assert (
                client.get(
                    site.origin,
                    headers={
                        "Authorization": "Bearer secret",
                        "Cookie": "secret=cookie",
                        "Proxy-Authorization": "Basic secret",
                        "X-Secret": "secret",
                    },
                ).status_code
                == 200
            )
            response = client.get(site.origin + "/next")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Set-Cookie"] == "session=synthetic; HttpOnly"
            with pytest.raises(httpx.RemoteProtocolError):
                client.get(attacker.origin)
            assert client.post(site.origin, content="secret").status_code == 501
    assert attacker.received == []
    assert len(site.received) == 2
    for request in site.received:
        assert not {k.lower() for k in request["headers"]} & {
            "authorization",
            "cookie",
            "proxy-authorization",
            "x-secret",
        }


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/",
        "http://10.0.0.1/",
        "https://user:password@example.com/",
        "https://example.com:0/",
        "ftp://example.com/",
        "https://outside.example/",
    ],
)
def test_proxy_refuses_unsafe_destinations_before_connect(url):
    proxy = CrawlProxy({"example.com": "93.184.216.34"}, (80, 443))
    try:
        with pytest.raises(network.UnsafeURL):
            proxy.connect(url)
        assert proxy.sockets == set()
    finally:
        proxy.server_close()


def test_real_siteone_never_falls_back_when_proxy_stops(tmp_path, http_server, monkeypatch):
    site = http_server()
    monkeypatch.setattr(network, "_fixture_origins", frozenset({site.origin}))
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])

    @contextlib.contextmanager
    def stopped(pins, ports):
        proxy = CrawlProxy(pins, ports)
        proxy.server_close()
        yield proxy

    monkeypatch.setattr(siteone, "crawl_proxy", stopped)
    ctx = context(tmp_path, site.origin + "/", tools=ROOT / "tools")
    result = siteone.run(ctx)
    assert result.status == "error"
    assert site.received == []


@pytest.mark.parametrize(
    "url,port",
    [
        ("http://example.com/", 80),
        ("https://example.com/", 443),
        ("http://example.com:443/", 443),
        ("https://example.com.:80/", 80),
    ],
)
def test_proxy_checks_exact_host_and_port(url, port):
    proxy = CrawlProxy({"example.com": "93.184.216.34"}, (80, 443))
    try:
        assert proxy.destination(url) == ("93.184.216.34", port)
    finally:
        proxy.server_close()


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("siteone", 1)])
def test_proxy_stops_when_crawler_cannot_finish(tmp_path, monkeypatch, failure):
    from test_security_siteone_pinning import fake_crawler

    ctx = context(tmp_path, "https://example.com/")
    fake_crawler(monkeypatch, ctx, [])
    proxies = []

    @contextlib.contextmanager
    def capture(pins, ports):
        with crawl_proxy(pins, ports) as proxy:
            proxies.append(proxy)
            yield proxy

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(siteone, "crawl_proxy", capture)
    monkeypatch.setattr(subproc, "run", fail)
    assert siteone.run(ctx).status == "error"
    assert len(proxies) == 1 and proxies[0].stopped.is_set()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxies[0].server_port), timeout=1)


@pytest.mark.parametrize("bad_address", ["10.0.0.1", "169.254.169.254"])
def test_real_siteone_blocks_rebinding_on_https_redirect(
    tmp_path, https_site, monkeypatch, bad_address
):
    site = https_site
    site.state["links"] = "<a href='/jump'>Redirect</a>"
    site.state["redirect"] = site.origins[1] + "/private"
    lookup = socket.getaddrinfo

    def rebind():
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, *a, **k: lookup(bad_address if host == HOSTS[1] else host, *a, **k),
        )

    site.state["on_jump"] = rebind
    ctx = context(tmp_path, site.origins[1] + "/", tools=TLS_TOOLS)
    result = siteone.run(ctx)
    assert result.status == "ok", result.reason  # Initial HTML succeeded, later URL is unavailable.
    assert any(r["path"] == "/jump" for r in site.received)
    assert not any(r["path"] == "/private" for r in site.received)
    response = next(
        e
        for e in result.evidence
        if e.description == "SiteOne HTTP response" and e.url == site.origins[1] + "/private"
    )
    assert siteone.status_code(response.data["status"]) < 0
