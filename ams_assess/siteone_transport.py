"""Run-scoped SiteOne proxy: checked numeric peers without rewriting TLS hostnames.

SiteOne 2.5.1's --resolve rewrites the HTTPS URL to an IP, losing SNI and hostname
verification. Its HTTP proxy option preserves both. CONNECT is an opaque TLS tunnel:
SiteOne verifies the real server certificate, with no local CA or TLS interception.
Only this run's prevalidated host/port pins are accepted; this is not a general proxy.
"""

import contextlib
import http.client
import ipaddress
import select
import socket
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urljoin, urlsplit

from ams_assess.network import UnsafeURL, validate_public_url
from ams_assess.targets import REQUEST_SECONDS


class CrawlProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, pins: dict[str, str], ports: tuple[int, ...]):
        self.pins, self.ports = dict(pins), ports
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.sockets: set[socket.socket] = set()
        # Crawler uses two workers; cap local clients.
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", 0), ProxyRequest)

    @property
    def option(self) -> str:
        return f"--proxy=127.0.0.1:{self.server_port}"

    def destination(self, url: str) -> tuple[str, int]:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").removesuffix(".")
            port = (
                parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
            )
            if self.stopped.is_set() or host not in self.pins or port not in self.ports:
                raise UnsafeURL()
            checked = validate_public_url(url)
            address = self.pins[host]
            # Refuse rebinding (including mixed public/private answers), and do not
            # silently choose a new public address if the original pin disappears.
            if address not in checked.addresses:
                raise UnsafeURL()
            return address, port
        except ValueError:
            raise UnsafeURL() from None

    def track(self, connection: socket.socket) -> None:
        with self.lock:
            if self.stopped.is_set():
                connection.close()
                raise OSError("Crawl proxy stopped")
            self.sockets.add(connection)

    def untrack(self, connection: socket.socket) -> None:
        with self.lock:
            self.sockets.discard(connection)
        connection.close()

    def connect(self, url: str) -> socket.socket:
        address, port = self.destination(url)
        family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(REQUEST_SECONDS)
        self.track(connection)
        try:
            # Numeric address only: there is no second DNS lookup or direct fallback.
            connection.connect((address, port))
        except BaseException:
            self.untrack(connection)
            raise
        return connection

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            self.track(request)
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            self.untrack(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.untrack(request)
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # No request URLs or proxy credentials in logs.

    def stop(self) -> None:
        with self.lock:
            self.stopped.set()
            for connection in self.sockets:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
                connection.close()
        self.shutdown()
        self.server_close()


class ProxyRequest(BaseHTTPRequestHandler):
    # Do not buffer beyond the CONNECT headers into the TLS ClientHello.
    rbufsize = 0

    def setup(self):
        self.request.settimeout(REQUEST_SECONDS)
        super().setup()

    def log_message(self, format, *args):
        pass

    def bodyless(self) -> bool:
        if self.headers.get("Content-Length", "0") != "0" or "Transfer-Encoding" in self.headers:
            self.send_error(400)
            return False
        return True

    def do_CONNECT(self):
        if not self.bodyless():
            return
        proxy = cast(CrawlProxy, self.server)
        try:
            url = "https://" + self.path
            parts = urlsplit(url)
            if parts.path or parts.query or parts.fragment or parts.port is None:
                raise UnsafeURL()
            upstream = proxy.connect(url)
        except (UnsafeURL, OSError, ValueError):
            self.send_error(403)
            return
        try:
            self.send_response(200, "Connection established")
            self.end_headers()
            # CONNECT headers are never sent upstream. SiteOne has no
            # HTTP auth configured and its subprocess inherits no credentials.
            deadline = time.monotonic() + REQUEST_SECONDS + 2
            peers = (self.connection, upstream)
            while not proxy.stopped.is_set() and time.monotonic() < deadline:
                ready, _, _ = select.select(peers, [], [], 0.1)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    other = upstream if source is self.connection else self.connection
                    other.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            proxy.untrack(upstream)
            self.close_connection = True

    def do_GET(self):
        self.forward()

    def do_HEAD(self):
        self.forward()

    def forward(self):
        if not self.bodyless():
            return
        proxy = cast(CrawlProxy, self.server)
        upstream = None
        try:
            parts = urlsplit(self.path)
            if parts.scheme != "http" or parts.fragment:
                raise UnsafeURL()
            upstream = proxy.connect(self.path)
            # Only public crawler headers, never cookies/auth/proxy credentials or
            # arbitrary caller headers. HTTPConnection receives an already pinned socket.
            headers = {
                key: self.headers[key] for key in ("User-Agent", "Accept") if key in self.headers
            }
            headers.update(
                {"Host": parts.netloc, "Connection": "close", "Accept-Encoding": "identity"}
            )
            client = http.client.HTTPConnection(parts.hostname or "", timeout=REQUEST_SECONDS)
            client.auto_open = 0  # Never let http.client resolve/connect on its own.
            client.sock = upstream
            path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
            client.request(self.command, path, headers=headers)
            response = client.getresponse()
            if 300 <= response.status < 400 and response.getheader("Location"):
                proxy.destination(urljoin(self.path, response.getheader("Location") or ""))
            self.send_response_only(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {
                    "connection",
                    "transfer-encoding",
                    "proxy-authenticate",
                }:
                    self.send_header(key, value)
            self.end_headers()
            deadline = time.monotonic() + REQUEST_SECONDS
            while not proxy.stopped.is_set() and time.monotonic() < deadline:
                data = response.read(65536)
                if not data:
                    break
                self.wfile.write(data)
        except UnsafeURL:
            with contextlib.suppress(OSError):
                self.send_error(403)
        except (OSError, ValueError, http.client.HTTPException):
            # Preserve a transport failure, rather than inventing an HTTP response
            # attributed to the public site when the upstream never sent one.
            pass
        finally:
            if upstream is not None:
                proxy.untrack(upstream)
            self.close_connection = True


@contextlib.contextmanager
def crawl_proxy(pins: dict[str, str], ports: tuple[int, ...]) -> Generator[CrawlProxy]:
    proxy = CrawlProxy(pins, ports)
    thread = threading.Thread(
        target=proxy.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield proxy
    finally:
        proxy.stop()
        thread.join(2)
