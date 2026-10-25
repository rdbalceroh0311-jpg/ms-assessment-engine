"""Track B UI regressions exercise HTTP parsing, browser forms and outbound referrers."""

import os
import re
import socket
import stat
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from test_ui import customer as customer
from test_ui import form, running

from ams_assess import store, ui
from ams_assess.browser_safety import LAUNCH_OPTIONS


@pytest.mark.parametrize("route", ["/", "/file", "/update", "/connect", "/missing"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"])
def test_every_route_and_method_requires_auth(customer, route, method):
    with running(customer) as (server, client):
        client.headers.pop("Authorization")
        response = client.request(method, route)
        assert response.status_code == 403
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert server.token not in response.text


def test_bootstrap_exchanges_url_for_httponly_cookie_and_token_free_links(customer, capsys, caplog):
    with running(customer) as (server, client):
        client.headers.pop("Authorization")
        initial = client.get(server.bootstrap_url() + "&customer=synthetic")
        assert initial.status_code == 303
        assert initial.headers["Location"] == "/?customer=synthetic"
        assert "HttpOnly" in initial.headers["Set-Cookie"]
        assert "SameSite=Strict" in initial.headers["Set-Cookie"]
        assert server.token not in initial.text
        page = client.get(initial.headers["Location"])
        assert page.status_code == 200
        assert server.token not in page.text and "token=" not in page.text
        assert client.get("/missing").status_code == 404
        for host in ("evil.example", "127.0.0.1.evil.test", "127.0.0.1:1", "[::1]:123"):
            assert client.get("/", headers={"Host": host}).status_code == 403
        assert (
            client.get("/", headers={"Host": f"localhost:{server.server_port}"}).status_code == 200
        )
        assert server.token not in capsys.readouterr().out + capsys.readouterr().err + caplog.text


def test_startup_opens_browser_without_logging_bootstrap_token(customer, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(url) or True)

    def stop(server):
        raise KeyboardInterrupt

    monkeypatch.setattr(ui.LocalServer, "serve_forever", stop)
    ui.serve(customer, port=0)
    assert len(opened) == 1 and "?token=" in opened[0]
    token = opened[0].split("?token=", 1)[1]
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.test"},
        {"Origin": "null"},
        {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "valid", "Referer": "http://evil.test/form"},
    ],
)
def test_writes_reject_missing_foreign_and_opaque_origin_even_with_csrf(customer, headers):
    before = (customer / "synthetic/assessment.json").read_bytes()
    with running(customer) as (server, client):
        headers = {k: server.origin if v == "valid" else v for k, v in headers.items()}
        response = client.post("/update", data=form(server), headers=headers)
        assert response.status_code == 403
    assert (customer / "synthetic/assessment.json").read_bytes() == before


def test_malformed_http_never_echoes_or_logs_bootstrap_token(customer, capsys, caplog):
    with running(customer) as (server, client):
        for raw in [
            f"GET /?token={server.token}&padding={'a' * 66000} HTTP/1.1\r\n\r\n",
            f"GET /?token={server.token} BAD-VERSION\r\n\r\n",
            f"GET /?token={server.token} HTTP/1.1\r\nHost: evil.test\r\n\r\n",
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
            f"Host: evil.test\r\nAuthorization: Bearer {server.token}\r\n\r\n",
        ]:
            with socket.create_connection(server.server_address, timeout=5) as connection:
                connection.sendall(raw.encode())
                data = b""
                while chunk := connection.recv(65536):
                    data += chunk
                assert server.token.encode() not in data
        client.headers.pop("Authorization")
        assert client.get("/", headers={"Cookie": "invalid[key]=FAKE"}).status_code == 403
        captured = capsys.readouterr()
        assert server.token not in captured.out + captured.err + caplog.text


def test_saved_report_outbound_link_sends_no_token_or_referrer(customer, http_server):
    attacker = http_server()
    # Different host from the authenticated cookie's host, on the same local test machine.
    outbound = attacker.origin.replace("127.0.0.1", "localhost") + "/land"
    directory = customer / "synthetic/reports/001-fixture"
    directory.mkdir(parents=True)
    (directory / "report.html").write_text(f"<a href='{outbound}'>Reference</a>")
    current = store.load(customer, "synthetic")
    assert current is not None
    current["report_revisions"] = [{"directory": "reports/001-fixture", "files": ["report.html"]}]
    with store.locked(customer, "synthetic"):
        store.save(customer, "synthetic", current)
    with running(customer) as (server, client), sync_playwright() as p:
        browser = p.chromium.launch(**LAUNCH_OPTIONS)
        try:
            page = browser.new_page()
            page.goto(server.bootstrap_url())
            page.goto(
                server.origin
                + server.url("/file", customer="synthetic", path="reports/001-fixture/report.html")
            )
            page.get_by_text("Reference").click()
            page.wait_for_url(outbound)
            assert attacker.received
            assert all("Referer" not in row["headers"] for row in attacker.received)
            assert server.token not in repr(attacker.received)
        finally:
            browser.close()


def test_headless_startup_hands_token_over_in_owner_only_file(customer, monkeypatch, capsys):
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: False)
    seen = {}

    def stop(server):
        match = re.search(r"saved in (\S+)", capsys.readouterr().out)
        assert match
        path = Path(match[1])
        seen.update(mode=stat.S_IMODE(path.stat().st_mode), url=path.read_text().strip(), path=path)
        seen["token"] = server.token
        raise KeyboardInterrupt

    monkeypatch.setattr(ui.LocalServer, "serve_forever", stop)
    ui.serve(customer, port=0)
    assert seen["mode"] == 0o600
    assert seen["url"].endswith("?token=" + seen["token"])
    assert not seen["path"].exists()  # removed at shutdown
    captured = capsys.readouterr()
    assert seen["token"] not in captured.out + captured.err


def test_hardlinked_output_is_refused(customer):
    directory = customer / "synthetic/reports/001-fixture"
    directory.mkdir(parents=True)
    secret = customer / "synthetic/raw-canary.txt"
    secret.write_text("FAKE-HARDLINK-CANARY")
    os.link(secret, directory / "report.md")
    current = store.load(customer, "synthetic")
    assert current is not None
    current["report_revisions"] = [{"directory": "reports/001-fixture", "files": ["report.md"]}]
    with store.locked(customer, "synthetic"):
        store.save(customer, "synthetic", current)
    with running(customer) as (server, client):
        response = client.get(
            server.url("/file", customer="synthetic", path="reports/001-fixture/report.md")
        )
        assert response.status_code == 403
        assert "FAKE-HARDLINK-CANARY" not in response.text


def test_session_cookie_is_bound_to_this_port_and_start(customer):
    def bootstrap(server, client):
        response = client.get(server.bootstrap_url())
        assert response.status_code == 303
        name, value = response.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)
        return name, value

    with running(customer) as (first, one), running(customer) as (second, two):
        for client in (one, two):
            client.headers.pop("Authorization")
        name, value = bootstrap(first, one)
        assert name == f"ams_session_{first.server_port}"
        assert value != first.token and first.token not in value  # History URL ≠ session.
        assert one.get("/", headers={"Cookie": f"{name}={value}"}).status_code == 200
        # Browsers send host cookies to every port: another instance must reject them all.
        for cookie in (
            f"{name}={value}",
            f"ams_session_{second.server_port}={value}",
            f"ams_session={value}",
            f"ams_session_{second.server_port}={first.token}",
        ):
            assert two.get("/", headers={"Cookie": cookie}).status_code == 403
        assert one.get("/", headers={"Cookie": f"{name}={first.token}"}).status_code == 403
        own = bootstrap(second, two)
        both = f"{name}={value}; {own[0]}={own[1]}"
        assert two.get("/", headers={"Cookie": both}).status_code == 200
        assert one.get("/", headers={"Cookie": both}).status_code == 200
        port = first.server_port
    # A restart on the same port has a fresh secret: the earlier cookie no longer works.
    with ui.LocalServer(customer, port=port) as restarted:
        assert restarted.cookie == name and restarted.session != value
