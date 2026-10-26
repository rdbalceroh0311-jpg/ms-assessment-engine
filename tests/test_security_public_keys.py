"""Public third-party keys in customer page source: fingerprinted, never stored or sent."""

import base64
import functools
import hashlib
import html
import http.server
import json
import logging
import random
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

from ams_assess import cli, data_boundary, report, store
from ams_assess.data_boundary import sanitise, text, unescape_html

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


def html_levels(levels):
    """The key with its "S" as a character reference, HTML-encoded `levels` times in all."""
    source = "AIza&#83;yFAKE-fixture_key0000000000000Wxyz"
    for _ in range(levels - 1):
        source = source.replace("&", "&amp;")
    return source


# Review round 2: each recovered the key after one decode, or raised.
@pytest.mark.parametrize(
    "source",
    [
        "AIza\\u{00000053}yFAKE-fixture_key0000000000000Wxyz",
        "AIza\\u{0000000000000000000053}yFAKE-fixture_key0000000000000Wxyz",
        "AIza&#00000000000083;yFAKE-fixture_key0000000000000Wxyz",
        "AIza&#x0000000000053;yFAKE-fixture_key0000000000000Wxyz",
        "AIza&#" + "0" * 4400 + "83;yFAKE-fixture_key0000000000000Wxyz",
        html_levels(6),
        html_levels(20),
    ],
)
def test_review_escaped_key_inputs_are_fingerprinted(source):
    cleaned = text(source)
    assert cleaned == FINGERPRINT
    assert not leaks(report.mdtext(source))


def test_encoding_nested_beyond_the_decode_bound_is_withheld():
    for source in (html_levels(70), f"maps key {html_levels(200)} here"):
        cleaned = text(source)
        assert "fixture" not in cleaned and "Wxyz" not in cleaned
        assert "[redacted]" in cleaned


@pytest.mark.parametrize(
    "encode", [base64.b64encode, base64.urlsafe_b64encode], ids=["base64", "base64url"]
)
def test_key_inside_a_base64_payload_is_redacted(encode):
    payload = encode(("prefix" + FAKE_KEY).encode()).decode()
    for source in (
        f"data:text/plain;base64,{payload}",
        f'<img alt="map" src="data:application/octet-stream;base64,{payload}">',
        f"config={payload}&zoom=14",
    ):
        cleaned = text(source)
        assert payload not in cleaned and payload.rstrip("=") not in cleaned
        assert "[redacted]" in cleaned


def spaced_base64(every=8, separator=" "):
    payload = base64.b64encode(("prefix" + FAKE_KEY).encode()).decode()
    return separator.join(payload[i : i + every] for i in range(0, len(payload), every))


# Review round 3: atob() ignores ASCII whitespace inside a base64 payload.
@pytest.mark.parametrize(
    "source",
    [
        f"data:text/plain;base64,{spaced_base64()}",
        f'<img alt="map" src="data:text/plain;base64,{spaced_base64()}">',
        f"data:application/json;charset=utf-8;base64,{spaced_base64(20, chr(10) + chr(9))}",
        f"data:text/plain;base64,{spaced_base64(4, chr(13) + chr(10))}",
        f"<pre>{spaced_base64(8)}</pre>",
        f"config: {spaced_base64(12, '  ')}",
    ],
)
def test_whitespace_split_base64_is_inspected_whole(source):
    cleaned = text(source)
    chunks = [c for c in spaced_base64(4).split() if c != "cHJl"]  # "pre" of "prefix"
    assert not [c for c in chunks if c in cleaned], cleaned
    assert "[redacted]" in cleaned
    # Ordinary prose split by whitespace is never mistaken for base64.
    prose = "Our licensed plumbers attend Sydney suburbs within two hours daily"
    assert text(prose) == prose


@pytest.mark.parametrize("continuation", ["\n", "\r", "\r\n", "\u2028", "\u2029"], ids=repr)
def test_js_line_continuations_decode_to_nothing(continuation):
    split = f"AIza\\{continuation}SyFAKE-fixture_key0000000000000Wxyz"
    assert text(split) == FINGERPRINT
    assert text(f'maps = "{split}";') == f'maps = "{FINGERPRINT}";'


def per_call(function, value):
    """Best per-call time over repeated calls lasting at least 0.2 s (stable at any size)."""
    calls, best, spent = 0, float("inf"), 0.0
    while spent < 0.2 or calls < 2:
        started = time.perf_counter()
        function(value)
        elapsed = time.perf_counter() - started
        best, spent, calls = min(best, elapsed), spent + elapsed, calls + 1
    return best


def b64_chunks(count):
    generator = random.Random(4)
    return " ".join(base64.b64encode(generator.randbytes(6)).decode() for _ in range(count))


# Sizes large enough that quadratic work dominates: 4x the input must stay well under 8x.
SCALING = {
    "escaped-keys": (lambda n: (FAKE_KEY.replace("S", "&#83;") + " ") * n, 5_000),
    "fingerprints-then-entity": (lambda n: (FINGERPRINT + "&amp; ") * n, 5_000),
    "data-scheme-repeated": (lambda n: "data:" * n, 20_000),
    "data-uris-with-payloads": (lambda n: ("data:;base64," + b64_chunks(4) + '" ') * n, 5_000),
    "long-base64-group": (b64_chunks, 20_000),
    "whitespace-runs": (lambda n: ("a" + " " * 1000 + "\u00e9") * n, 100),
}


@pytest.mark.parametrize("make, small", SCALING.values(), ids=SCALING)
def test_redaction_cost_grows_linearly_with_input_size(make, small):
    sample = text(make(small))
    if "AIza" in make(1):
        assert sample.count(FINGERPRINT) == small and not leaks(sample)
    ratio = per_call(text, make(4 * small)) / per_call(text, make(small))
    assert ratio < 8, ratio


def test_span_bookkeeping_is_linear_for_interleaved_detectors():
    """Eight detectors whose spans interleave across the whole value (the access pattern
    that made sorted-list insertion quadratic)."""

    def claim_all(count):
        covered = []
        for group in range(8):
            spans = [(i * 80 + group * 10, i * 80 + group * 10 + 5) for i in range(count // 8)]
            _, covered = data_boundary._claim(covered, spans)
        assert len(covered) == count // 8 * 8
        return covered

    ratio = per_call(claim_all, 160_000) / per_call(claim_all, 40_000)
    assert ratio < 8, ratio


OPERATOR_SECRET = "OPERATOR-demo-1234567890"  # FAKE configured credential


def recovered(cleaned, secret):
    """Whether any base64 decode of the output (whole, per token, after padding, at any
    alignment, ignoring whitespace and markup as a lenient decoder does) yields the secret."""
    if secret in cleaned:
        return True
    remaining = cleaned.replace("[redacted]", " ")
    tokens = re.split(r"[^A-Za-z0-9+/_=-]+", remaining)
    candidates = ["".join(tokens), *tokens, *re.split(r"[^A-Za-z0-9+/_-]+", remaining)]
    for candidate in filter(None, (c.replace("=", "") for c in candidates)):
        for offset in range(4):
            part = candidate[offset:]
            part = part[: len(part) - (len(part) % 4 == 1)]  # a lone 6-bit tail decodes nothing
            if secret.encode() in base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)):
                return True
    return False


def test_review_configured_secret_beside_a_matching_base64_run_is_redacted():
    run = base64.b64encode(("sk-" + "a" * 24).encode()).decode()
    encoded = base64.b64encode(OPERATOR_SECRET.encode()).decode()
    spaced = " ".join(encoded[i : i + 8] for i in range(0, len(encoded), 8))
    assert recovered(spaced, OPERATOR_SECRET)
    for source in (f"{run} {spaced}", f"{spaced} {run}"):
        cleaned = text(source, (OPERATOR_SECRET,))
        assert cleaned == "[redacted]", cleaned


def test_no_decode_recovers_a_configured_secret_split_beside_matching_runs():
    """Property: a matching base64 run and a whitespace-split configured secret, in either
    order, with random widths, separators, padding and surrounding prose."""
    generator = random.Random(2604)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    matching = [
        "sk-" + "a" * 24,
        "ghp_" + "F" * 30,
        FAKE_KEY,
        "prefix" + FAKE_KEY,
        "token=FAKEfixture1234",
    ]

    def separator():
        return "".join(generator.choices(" \t\n\r\f", k=generator.randint(1, 3)))

    for _ in range(300):
        secret = "".join(generator.choice(alphabet) for _ in range(generator.randint(12, 40)))
        encode = generator.choice([base64.b64encode, base64.urlsafe_b64encode])
        run = encode(generator.choice(matching).encode()).decode()
        encoded = encode(secret.encode()).decode()
        width = generator.randint(2, 12)
        spaced = "".join(
            encoded[i : i + width] + separator() for i in range(0, len(encoded), width)
        ).rstrip()
        parts = [run, spaced] if generator.random() < 0.5 else [spaced, run]
        prose = generator.choice(["", "Header value: ", "<pre>"])
        source = prose + separator().join(parts) + generator.choice(["", " end", "</pre>"])
        assert recovered(source, secret), source
        cleaned = text(source, (secret,))
        assert not recovered(cleaned, secret), (source, cleaned)


ESCAPED_SECRETS = [
    ("sk&#45;ant-FAKEfixture000000000000000", "FAKEfixture000000000000000"),
    ("\\u0073k-FAKEfixture0000000000000000", "FAKEfixture0000000000000000"),
    ("gh&#112;_FAKEfixture000000000000000000", "FAKEfixture000000000000000000"),
    ("ghp&#x5f;FAKEfixture000000000000000000", "FAKEfixture000000000000000000"),
    ("ya29&#46;FAKE-fixture-token", "FAKE-fixture-token"),
    ("GOCSPX&#45;FAKEfixture000", "FAKEfixture000"),
    ("CANARY&#45;FAKE-fixture-canary", "FAKE-fixture-canary"),
    ("Bearer&#32;FAKEfixtureToken0000", "FAKEfixtureToken0000"),
    ("token&#61;FAKEfixture1234", "FAKEfixture1234"),
    ("api\\x5fkey=FAKEfixture1234", "FAKEfixture1234"),
    ("Basic&#32;" + base64.b64encode(b"user:FAKEpass").decode(), "dXNlcjpGQUtFcGFzcw"),
]


@pytest.mark.parametrize("source, body", ESCAPED_SECRETS)
def test_every_secret_detector_runs_on_the_decoded_view(source, body):
    for template in ("{}", "page\\n{}\\n", '{{"auth": "{}"}}'):
        cleaned = text(template.format(source))
        assert all(body not in view for view in decoded_views(cleaned)), cleaned


def test_configured_secret_is_removed_before_fingerprinting():
    assert text(FAKE_KEY + "&#32;", (FAKE_KEY,)) == "[redacted]&#32;"
    # Also when the operator's own key is spelt with escapes: redacted, never fingerprinted.
    assert text(html_levels(2), (FAKE_KEY,)) == "[redacted]"


FRAGMENTS = [
    "&", "#", "x", "X", ";", "0", "00000", "9", "83", "5f", "\\", "\\u", "\\x", "{", "}",
    "u{", "%", "%2", "%25", "%5F", "&amp;", "&copy;", "&lowbar;", "&#", "&#x", "&#0", "AIza",
    "Sy", "FAKE", "_", "-", "=", "key", "token", "Bearer ", "sk-", "ghp_", "ya29.", " ", "\n",
    "'", '"', "<", ">", "é", "\U0001f600", "data:;base64,", "QUl6YV", "A" * 20,
]  # fmt: skip


def test_sanitising_never_raises_on_any_input(caplog):
    caplog.set_level(logging.WARNING, logger="ams_assess.data_boundary")
    review = ["100%_satisfaction_guaranteed&copy;", "Price%25_guaranteed_%2520today"]
    for source in review:
        assert text(source) == source
    huge = "&#" + "0" * 4400 + "83;", "&#x" + "0" * 5000 + "41", "\\u{" + "0" * 5000 + "41}"
    generator = random.Random(2026)
    samples = [
        "".join(generator.choice(FRAGMENTS) for _ in range(generator.randint(1, 60)))
        for _ in range(3000)
    ]
    for source in [*review, *huge, *samples]:
        assert isinstance(text(source), str)
        assert isinstance(text(source, ("FAKE-secret",), ai=True), str)
        assert isinstance(unescape_html(source), str)
        assert isinstance(report.mdtext(source), str)
    # Total by construction: the fail-closed guard for an unexpected error never ran.
    assert "could not be inspected" not in caplog.text


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
