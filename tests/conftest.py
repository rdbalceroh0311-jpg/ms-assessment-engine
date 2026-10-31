"""Every test starts without operator credentials; network is fixture/loopback only."""

import ipaddress
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

# Do this before collection imports application modules (some calculate default paths).
_ROOT = Path(__file__).resolve().parents[1]
(_ROOT / "tmp").mkdir(exist_ok=True)
_HOME = tempfile.TemporaryDirectory(prefix="test-home-", dir=_ROOT / "tmp")
_SAFE = {key: os.environ[key] for key in ("PATH", "LANG", "SYSTEMROOT") if key in os.environ}
os.environ.clear()
os.environ.update(_SAFE)
os.environ.update(
    HOME=_HOME.name,
    XDG_CONFIG_HOME=str(Path(_HOME.name) / "config"),
    AMS_CONFIG_DIR=str(Path(_HOME.name) / "config/ams-assess"),
    AMS_ENV_FILE=str(Path(_HOME.name) / "absent.env"),
    AMS_AI_PROVIDER="none",
    AMS_LIVE_SMOKE="0",
    PLAYWRIGHT_BROWSERS_PATH=str(_ROOT / "tools/playwright"),
)

from ams_assess import network  # noqa: E402  (after the environment is isolated)

# Synthetic public answer for external names. Any connection to it is still refused below,
# so public-guard tests can exercise hostname validation without live DNS.
SYNTHETIC_PUBLIC_IP = "93.184.216.34"


_CUSTOMERS = _ROOT / "data/customers"


def _customer_tree(root=_CUSTOMERS):
    """Names and stat metadata only: never read operator customer data."""
    if not root.exists():
        return None
    return {
        (str(Path(directory) / name), os.lstat(Path(directory) / name).st_mtime_ns)
        for directory, dirs, files in os.walk(root)
        for name in dirs + files
    }


def _loopback(host):
    if host in {"localhost", b"localhost", None, "", b""}:
        return
    try:
        address = ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
    except ValueError:
        raise AssertionError("Tests must mock external DNS/network access") from None
    if not address.is_loopback:
        raise AssertionError("Tests must mock external network access")


@pytest.fixture(autouse=True)
def isolated_customer_root(tmp_path, monkeypatch):
    """Customer stores go to tmp; fail any test that writes under the repo's data/customers."""
    from ams_assess import cli

    before = _customer_tree()
    # Tests that relocate cli.ROOT keep their own <ROOT>/data/customers.
    monkeypatch.setattr(
        cli,
        "data_root",
        lambda: (
            (tmp_path / "data/customers")
            if cli.ROOT.resolve() == _ROOT
            else cli.ROOT / "data/customers"
        ),
    )
    yield
    assert _customer_tree() == before, f"Test wrote under {_CUSTOMERS}"


@pytest.fixture(autouse=True)
def isolated_operator_and_network(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("AMS_CONFIG_DIR", str(home / "config/ams-assess"))
    monkeypatch.setenv("AMS_ENV_FILE", str(home / "absent.env"))
    original_lookup = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def lookup(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name == "localhost":
            # Fixture servers bind 127.0.0.1 only; the OS may list ::1 first.
            return original_lookup("127.0.0.1", *args, **kwargs)
        try:
            _loopback(name)
        except AssertionError:
            try:
                ipaddress.ip_address(name)
            except ValueError:
                # Never ask a real resolver; the answer below is unreachable in tests.
                return original_lookup(SYNTHETIC_PUBLIC_IP, *args, **kwargs)
            # Numeric non-loopback literals need no resolver; connect() still refuses them.
        return original_lookup(host, *args, **kwargs)

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            _loopback(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            _loopback(address[0])
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            _loopback(args[-1][0])
        return original_sendto(sock, data, *args)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)


@pytest.fixture(autouse=True)
def synthetic_customers(monkeypatch):
    """Exact synthetic customer slugs used by tests; no setting can mark a real customer."""
    from ams_assess import authorisation

    monkeypatch.setattr(
        authorisation,
        "_fixture_customers",
        frozenset({"synthetic", "test", "acme", "beta", "other", "example", "alpha", "bravo"}),
    )


@pytest.fixture
def allow_local_fixture(monkeypatch):
    """Explicit opt-in: only exact origins of HTTP servers created by this test."""
    bind = HTTPServer.server_bind

    def register(server):
        bind(server)
        assert server.server_address[0] == "127.0.0.1"
        origins = {f"http://{host}:{server.server_port}" for host in ("127.0.0.1", "localhost")}
        monkeypatch.setattr(network, "_fixture_origins", network._fixture_origins | origins)

    monkeypatch.setattr(HTTPServer, "server_bind", register)


@pytest.fixture
def http_server():
    """Actual receiving server; no public-guard exception is granted by this fixture."""
    servers = []

    def create(respond=lambda request: (200, {}, b"OK"), *, tls=None):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                record = {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                }
                received.append(record)
                status, headers, body = respond(record)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_POST = do_HEAD = do_PUT = do_DELETE = do_GET

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return SimpleNamespace(
            server=server,
            received=received,
            origin=f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}",
        )

    yield create
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(5)


def mock_siteone_traffic(monkeypatch):
    """Pair a mocked successful crawler response with simulated relay traffic.

    Explicit unit-test opt-in only; real crawls and bypass regressions use the real counter.
    """
    from ams_assess.siteone_transport import CrawlProxy

    monkeypatch.setattr(CrawlProxy, "carried_requests", property(lambda self: 1))
