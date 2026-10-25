"""Public third-party keys in customer page source: fingerprinted, never stored or sent."""

import functools
import hashlib
import html
import http.server
import json
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import anthropic
import markdown
import pypdf
import pytest
from playwright.sync_api import sync_playwright
from test_customer_isolation import ui
from test_security_ai import RecordingAI

from ams_assess import cli, report, store
from ams_assess.data_boundary import sanitise, text

pytestmark = pytest.mark.usefixtures("allow_local_fixture")

# FAKE key in fixtures/public-key/index.html; it has the 39-character browser key shape.
FAKE_KEY = "AIzaSyFAKE-fixture_key0000000000000Wxyz"
FINGERPRINT = (
    f"[redacted:google-key:...Wxyz:sha256-{hashlib.sha256(FAKE_KEY.encode()).hexdigest()[:8]}]"
)
# Any surviving part of the key body (the fingerprint keeps only its last four characters).
KEY_TAIL = "key0000000000000Wxyz"
_JS_ESCAPE = re.compile(r"\\u\{([0-9a-fA-F]+)\}|\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")


def decoded_views(content: str) -> list[str]:
    """Independently decode HTML references, JS/JSON and percent escapes, nested."""
    views = [content]
    for _ in range(4):
        value = html.unescape(views[-1]).replace("\\\\", "\\")
        value = _JS_ESCAPE.sub(lambda m: chr(int(next(g for g in m.groups() if g), 16)), value)
        views.append(unquote(value))
    return views


def leaks(content: str) -> bool:
    return any(FAKE_KEY in view or KEY_TAIL in view for view in decoded_views(content))


@pytest.mark.parametrize(
    "source",
    [
        f"https://maps.googleapis.com/maps/api/js?key={FAKE_KEY}&callback=initMap",
        f'window.fixtureMaps = {{apiKey: "{FAKE_KEY}"}};',
        f"<div data-src='https://www.google.com/maps/embed/v1/place?key={FAKE_KEY}'>",
        f"escaped page source\\n{FAKE_KEY}",
        f"key: {FAKE_KEY}",
    ],
)
def test_page_source_key_is_replaced_by_stable_fingerprint(source):
    cleaned = text(source)
    assert FAKE_KEY not in cleaned
    assert FINGERPRINT in cleaned
    # Every artefact is sanitised again on write; the fingerprint must survive unchanged.
    assert text(cleaned) == cleaned
    assert sanitise({"nested": [cleaned]}) == {"nested": [cleaned]}


ESCAPED = [
    "AIza&#83;yFAKE-fixture_key0000000000000Wxyz",
    "AIza&#x53;yFAKE-fixture&#X5f;key0000000000000Wxyz",
    "AIza&#0000083yFAKE-fixture&lowbar;key0000000000000Wxyz",
    "&#65;&#73;&#122;&#97;&#83;&#121;FAKE-fixture_key0000000000000Wxyz",
    "AIza&amp;#83;yFAKE-fixture&amp;amp;lowbar;key0000000000000Wxyz",
    "AIza\\u0053yFAKE-fixture_key0000000000000Wxyz",
    "\\x41Iza\\x53yFAKE-fixture\\u{5f}key0000000000000Wxyz",
    "AIza\\\\u0053yFAKE-fixture_key0000000000000Wxyz",
    "AIza\\u0026#83;yFAKE-fixture_key0000000000000Wxyz",
    "AIza\\u0053y&#70;AKE-fixture%5Fkey0000000000000Wxyz",
]


@pytest.mark.parametrize("encoded", ESCAPED)
@pytest.mark.parametrize(
    "template",
    [
        "{}",
        "page\\n{}\\n",
        "https://maps.googleapis.com/maps/api/js?key={}&amp;callback=initMap",
        '{{"apiKey": "{}", "zoom": 14}}',
        "key={}",
    ],
)
def test_escaped_page_source_key_is_fingerprinted_in_place(encoded, template):
    assert leaks(encoded)
    cleaned = text(template.format(encoded))
    assert not leaks(cleaned), cleaned
    assert FINGERPRINT in cleaned
    # Only the key's own source span is replaced; the surrounding source is kept.
    assert cleaned == template.format(FINGERPRINT)
    assert text(cleaned) == cleaned


def test_escape_decoding_keeps_ordinary_source_and_stays_linear():
    source = "&nbsp;Fish &amp; chips \\n %20 &copy; \\u00e9 AIza-short &#83;"
    assert text(source) == source
    blob = "A" * 400_000 + "&#83;" + "B" * 400_000  # e.g. an inline data: URI
    started = time.monotonic()
    assert text(blob) == blob
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("encoded", ESCAPED)
def test_report_rendering_cannot_recreate_an_escaped_key(encoded):
    """Report text is unescaped for display; the result is sanitised again."""
    rendered = report.mdtext(f"Evidence: {encoded} seen on /contact")
    page = markdown.markdown(rendered)
    assert not leaks(rendered) and not leaks(page), page
    assert FINGERPRINT.replace("[", "\\[").replace("]", "\\]") in rendered


def test_fingerprint_never_extends_trailing_assignment_value_or_configured_secret():
    assert text(f"key={FAKE_KEY}.trailing") == f"key={FINGERPRINT}[redacted]"
    # An operator's own configured key stays fully redacted, without a fingerprint.
    assert text(f"key={FAKE_KEY}", (FAKE_KEY,)) == "key=[redacted]"
    # The AI view drops URL queries entirely; nothing of the key remains.
    assert "Wxyz" not in text(f"https://maps.googleapis.com/maps/api/js?key={FAKE_KEY}", ai=True)


def test_fixture_run_stores_reports_and_sends_only_the_fingerprint(monkeypatch):
    binary = cli.ROOT / "tools/siteone/siteone-crawler/siteone-crawler"
    with sync_playwright() as p:
        chromium = Path(p.chromium.executable_path)
    if not (binary.is_file() and chromium.is_file() and (cli.ROOT / "tools/axe.min.js").is_file()):
        pytest.skip("Run scripts/setup.sh for SiteOne, Chromium and axe-core")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    handler = functools.partial(Quiet, directory=str(cli.ROOT / "fixtures/public-key"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = RecordingAI()
    monkeypatch.setattr(anthropic, "Anthropic", provider.client)
    monkeypatch.setattr(
        cli,
        "config",
        lambda: {"AMS_AI_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "FAKE-anthropic-key-0000"},
    )
    customer = "test-fixture-" + uuid.uuid4().hex[:12]
    try:
        code = cli.main(
            [
                "run",
                "--customer",
                customer,
                "--target",
                f"http://127.0.0.1:{server.server_port}/",
                "--categories",
                "technical,seo,presence,security,cro_ux",
                "--max-pages",
                "5",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert code == 0
    root = cli.data_root()
    # The customer-level report too, which the local UI lists and serves.
    report.render_customer(root, customer, {"md", "html", "pdf"})
    files = [p for p in (root / customer).rglob("*") if p.is_file()]
    stored = {
        p: "\n".join(page.extract_text() for page in pypdf.PdfReader(p).pages)
        if p.suffix == ".pdf"
        else p.read_text(errors="replace")
        for p in files
        if p.suffix != ".png"
    }
    names = {p.name for p in stored}
    assert {"report.md", "report.html", "report.pdf", "evidence.json", "assessment.json"} <= names
    assert {"results.json", "browser.json"} <= names
    assert [str(p) for p, content in stored.items() if leaks(content)] == []
    # Traceable: the stored evidence still identifies every spelling by the one fingerprint.
    evidence = next(p for p in stored if p.name == "evidence.json")
    assert FINGERPRINT in evidence.read_text()
    assert evidence.read_text().count(FINGERPRINT) >= 5
    assert provider.received, "AI provider was not exercised"
    payload = json.dumps(provider.received)
    assert not leaks(payload) and "AIza" not in payload
    # The local UI's customer page and every report file it serves.
    with ui(root) as (server, client):
        page = client.get(server.url(customer=customer))
        assert page.status_code == 200 and not leaks(page.text)
        current = store.load(root, customer)
        assert current is not None and current["report_revisions"]
        for revision in current["report_revisions"]:
            for name in ("report.md", "report.html"):
                path = revision["directory"] + "/" + name
                served = client.get(server.url("/file", customer=customer, path=path))
                assert served.status_code == 200 and not leaks(served.text), path
