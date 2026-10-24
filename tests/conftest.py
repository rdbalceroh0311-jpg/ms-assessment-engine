"""Hermetic tests: isolated operator state, synthetic DNS, local sockets only."""

import ipaddress
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from ams_assess import network

TEST_ROOT = Path(__file__).resolve().parents[1] / ".security-test"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(TEST_ROOT / "browsers")
os.environ.pop("AMS_LIVE_SMOKE", None)


@pytest.fixture(autouse=True)
def isolated_operator(tmp_path, monkeypatch):
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for key in tuple(os.environ):
        if key.startswith(("AMS_", "ANTHROPIC_", "OPENAI_", "GOOGLE_", "CRUX_", "DATAFORSEO_")):
            monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    getaddrinfo = socket.getaddrinfo
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    sendto = socket.socket.sendto

    def local(address):
        if isinstance(address, tuple):
            try:
                assert ipaddress.ip_address(address[0]).is_loopback, "Live network forbidden"
            except ValueError:
                assert address[0] == "localhost", "Live hostname connection forbidden"

    def guarded_connect(sock, address):
        local(address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        local(address)
        return connect_ex(sock, address)

    def guarded_sendto(sock, data, *args):
        local(args[-1])
        return sendto(sock, data, *args)

    def synthetic_dns(host, port, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            numeric = host
        except ValueError:
            numeric = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return getaddrinfo(numeric, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", synthetic_dns)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)


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

            do_POST = do_HEAD = do_GET

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
