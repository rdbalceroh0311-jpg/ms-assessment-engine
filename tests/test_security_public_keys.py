"""Public third-party keys in customer page source: fingerprinted, never stored or sent."""

import functools
import hashlib
import http.server
import json
import threading
import uuid
from pathlib import Path

import anthropic
import pytest
from playwright.sync_api import sync_playwright
from test_security_ai import RecordingAI

from ams_assess import cli
from ams_assess.data_boundary import sanitise, text

pytestmark = pytest.mark.usefixtures("allow_local_fixture")

# FAKE key in fixtures/public-key/index.html; it has the 39-character browser key shape.
FAKE_KEY = "AIzaSyFAKE-fixture_key0000000000000Wxyz"
FINGERPRINT = (
    f"[redacted:google-key:...Wxyz:sha256-{hashlib.sha256(FAKE_KEY.encode()).hexdigest()[:8]}]"
)


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
    files = [p for p in (cli.data_root() / customer).rglob("*") if p.is_file()]
    stored = {p: p.read_bytes() for p in files if p.suffix != ".png"}
    assert any(p.name == "report.pdf" for p in stored)
    leaked = [str(p) for p, content in stored.items() if FAKE_KEY.encode() in content]
    assert leaked == []
    # Traceable: the stored evidence still identifies the key by fingerprint.
    evidence = next(p for p in stored if p.name == "evidence.json")
    assert FINGERPRINT in evidence.read_text()
    assert provider.received, "AI provider was not exercised"
    assert FAKE_KEY not in json.dumps(provider.received)
    assert "AIza" not in json.dumps(provider.received)
