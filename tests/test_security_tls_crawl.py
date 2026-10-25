"""TLS verification and SiteOne's explicit preflight/per-request boundary."""

import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ams_assess import coverage, network
from ams_assess.models import Context, create_run_dir
from ams_assess.modules import siteone


def test_pinned_tls_keeps_chain_and_original_hostname_verification(
    tmp_path, http_server, monkeypatch
):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate = tmp_path / "synthetic-cert.pem"
    private_key = tmp_path / "synthetic-key.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(certificate, private_key)
    receiver = http_server(tls=server_tls)
    hostname_url = receiver.origin.replace("127.0.0.1", "localhost")
    monkeypatch.setattr(network, "_fixture_origins", frozenset({hostname_url, receiver.origin}))
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        network.public_get(hostname_url)
    assert receiver.received == []
    trust = ssl.create_default_context(cafile=str(certificate))
    assert trust.check_hostname and trust.verify_mode == ssl.CERT_REQUIRED
    transport = network.PublicTransport()
    transport.inner.close()
    transport.inner = httpx.HTTPTransport(
        verify=trust, limits=httpx.Limits(max_keepalive_connections=0)
    )
    with network.public_client(transport=transport) as client:
        assert client.get(hostname_url).text == "OK"
        # Same checked IP, different hostname: the certificate must be verified again.
        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            client.get(receiver.origin)
    assert len(receiver.received) == 1
    assert receiver.received[0]["headers"]["Host"] == hostname_url.removeprefix("https://")


def test_siteone_filters_seeds_pins_domain_scope_and_records_residual_limit(tmp_path, monkeypatch):
    target = "https://example.com/"
    ctx = Context(
        "synthetic",
        "Synthetic",
        target,
        {"seo"},
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        tmp_path / "tools",
    )
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda _: {"resolved": target})
    seeds = [target + "safe", "http://127.0.0.1/secret", "https://other.example/scope-escape"]
    monkeypatch.setattr(
        coverage,
        "seed_inventory",
        lambda _: {
            "seeds": seeds,
            "seed_limit": 10,
            "seeds_over_limit": [],
        },
    )
    commands = []

    def crawler(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "results": [{"url": target, "status": "200", "type": 1}],
                }
            ),
        )

    monkeypatch.setattr(siteone.subproc, "run", crawler)
    result = siteone.run(ctx)
    assert result.status == "ok"
    assert "pinned per host/port" in result.reason
    assert "--allowed-domain-for-crawling=example.com" in commands[0]
    assert "--allowed-domain-for-crawling=www.example.com" in commands[0]
    assert not any("other.example" in arg for arg in commands[0])
    listing = Path(
        next(arg.split("=", 1)[1] for arg in commands[0] if arg.startswith("--url-list="))
    )
    assert listing.read_text().splitlines() == [target, target + "safe"]


def test_no_tls_or_ssh_verification_bypasses_in_product():
    root = Path(__file__).resolve().parents[1]
    import re

    forbidden = re.compile(
        r"verify\s*=\s*False|CERT_NONE|check_hostname\s*=\s*False|"
        r"ignore_https_errors[\"']?\s*[:=]\s*True|"
        r"StrictHostKeyChecking\s*=\s*no|ssl\._create_unverified_context|PYTHONHTTPSVERIFY"
    )
    for path in (root / "ams_assess").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".j2", ".sh"}:
            assert not forbidden.search(path.read_text()), path.name
    assert not forbidden.search((root / "scripts/setup.sh").read_text())
    assert "StrictHostKeyChecking=yes" in (root / "ams_assess/connectors/wp_cli.py").read_text()


def test_real_siteone_keeps_links_and_redirects_on_target_domain(
    tmp_path, http_server, monkeypatch
):
    attacker = http_server()
    external = attacker.origin.replace("127.0.0.1", "localhost")

    def respond(request):
        if request["path"] == "/jump":
            return 302, {"Location": external + "/redirect"}, b""
        if request["path"] != "/":
            return 404, {}, b""
        return (
            200,
            {"Content-Type": "text/html"},
            (
                f"<html><title>Fixture</title><a href='{external}/link'>Outside</a>"
                "<a href='/jump'>Redirect</a></html>"
            ).encode(),
        )

    fixture = http_server(respond)
    monkeypatch.setattr(network, "_fixture_origins", frozenset({fixture.origin}))
    root = Path(__file__).resolve().parents[1]
    ctx = Context(
        "synthetic",
        "Synthetic",
        fixture.origin + "/",
        {"seo"},
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        root / "tools",
        max_pages=3,
    )
    result = siteone.run(ctx)
    assert result.status == "ok"
    assert any(row["path"] == "/jump" for row in fixture.received)
    assert attacker.received == []
