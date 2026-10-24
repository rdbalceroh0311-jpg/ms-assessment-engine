import json
from types import SimpleNamespace

import dns.name
import dns.resolver
import httpx
import pytest
from test_domain_email import FakeResolver, context, fake

from ams_assess import runner, store
from ams_assess.models import ModuleResult
from ams_assess.modules import REGISTRY, domain_email


def mock_domain(monkeypatch, ns=None, mx=None, headers=None):
    table = {
        ("example.com", "A"): ["192.0.2.1"],
        ("example.com", "AAAA"): ["2001:db8::1"],
        ("example.com", "NS"): ns or ["a.ns.cloudflare.com.", "b.ns.cloudflare.com."],
        ("example.com", "MX"): mx or ["10 aspmx.l.google.com."],
        ("example.com", "CNAME"): ["edge.example.net."],
        ("edge.example.net", "CNAME"): ["example.cloudfront.net."],
    }
    monkeypatch.setattr(dns.resolver, "Resolver", lambda **kw: FakeResolver(table))
    monkeypatch.setattr(
        dns.resolver, "zone_for_name", lambda *a, **k: dns.name.from_text("example.com")
    )
    monkeypatch.setattr(
        domain_email.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="{}")
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "ldhName": "example.com",
                "entities": [
                    {
                        "roles": ["registrar"],
                        "handle": "9999",
                        "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]],
                        "publicIds": [{"type": "IANA Registrar ID", "identifier": "9999"}],
                    },
                    {
                        "roles": ["registrant"],
                        "vcardArray": ["vcard", [["fn", {}, "text", "REDACTED FOR PRIVACY"]]],
                    },
                ],
                "events": [],
            },
        )

    monkeypatch.setattr(httpx, "get", get)
    security = ModuleResult(name="security")
    security.add_evidence(
        "https://example.com/",
        "HTTP response",
        {
            "status": 200,
            "url": "https://example.com/",
            "headers": headers or {"server": "cloudflare", "cf-ray": "test"},
        },
    )
    security.add_evidence(
        "https://example.com/",
        "Verified TLS",
        {
            "hostname_match": True,
            "issuer": [["commonName", "Example CA"]],
            "protocol": "TLSv1.3",
            "expires": "2099-01-01",
            "days_to_expiry": 300,
        },
    )
    security.add_evidence(
        "https://other.example/",
        "Unrelated HTTP",
        {"status": 200, "headers": {"x-vercel-id": "other-site"}},
    )
    return security, calls


def block(result):
    return next(e.data["infrastructure"] for e in result.evidence if "infrastructure" in e.data)


def test_infrastructure_reuses_evidence_labels_providers_and_keeps_origin_unknown(
    tmp_path, monkeypatch
):
    security, calls = mock_domain(monkeypatch)
    ctx = context(tmp_path)
    ctx.results = [security]
    result = domain_email.run(ctx)
    infra = block(result)
    assert calls == ["https://rdap.org/domain/example.com"]
    assert infra["registrar"]["label"] == "OBSERVED"
    assert infra["registrar"]["value"][0]["names"] == ["Example Registrar"]
    assert "reseller/white-label" in infra["registrar"]["detail"]
    assert "Privacy redaction" in infra["registrar"]["detail"]
    assert infra["nameservers"]["value"] == ["a.ns.cloudflare.com.", "b.ns.cloudflare.com."]
    assert infra["dns_provider"]["value"] == ["Cloudflare"]
    assert infra["dns_provider"]["label"] == "INFERRED"
    assert {p["provider"] for p in infra["website_provider"]["value"]} == {
        "Cloudflare",
        "Amazon CloudFront",
    }
    assert "origin host unknown" in infra["origin_host"]["detail"]
    endpoints = infra["website_endpoints"]
    assert endpoints["value"]["A"]["records"] == ["192.0.2.1"]
    assert endpoints["value"]["AAAA"]["records"] == ["2001:db8::1"]
    assert len(endpoints["value"]["cname"]["chain"]) == 2
    assert endpoints["value"]["tls"][0]["protocol"] == "TLSv1.3"
    assert "ev-security-001" in endpoints["evidence_ids"]
    assert "ev-security-002" in endpoints["evidence_ids"]
    assert "ev-security-003" not in endpoints["evidence_ids"]
    assert infra["email_provider"]["value"][0]["provider"] == "Google Workspace"
    assert infra["account_ownership"]["value"] == "customer confirmation required"
    assert all(item["label"] != "CONFIRMED" for item in infra.values() if isinstance(item, dict))
    raw = json.loads((ctx.run_dir / "raw/domain_email.json").read_text())
    assert raw["infrastructure"] == infra


@pytest.mark.parametrize(
    ("mx", "expected", "gateway"),
    [
        (["10 example.mail.protection.outlook.com."], {"Microsoft 365"}, False),
        (
            ["10 aspmx.l.google.com.", "20 example.mail.protection.outlook.com."],
            {"Google Workspace", "Microsoft 365"},
            False,
        ),
        (["10 au-smtp-inbound-1.mimecast.com."], {"Mimecast"}, True),
        (["10 mxa-1.pphosted.com."], {"Proofpoint"}, True),
        (["10 mx.ppe-hosted.com."], {"Proofpoint"}, True),
        (["10 google.com.evil.example."], set(), False),
        (["0 ."], set(), False),
    ],
)
def test_mx_attribution_coexistence_gateways_and_hostname_boundaries(
    tmp_path, monkeypatch, mx, expected, gateway
):
    mock_domain(monkeypatch, mx=mx)
    infra = block(domain_email.run(context(tmp_path)))
    routes = infra["email_provider"]["value"]
    assert {p["provider"] for p in routes} == expected
    assert "not a full software inventory" in infra["email_provider"]["detail"]
    if gateway:
        assert all(p["detail"] == "gateway; underlying mailbox provider unknown" for p in routes)


def test_unknown_infrastructure_does_not_invent_provider_or_owner(tmp_path, monkeypatch):
    mock_domain(monkeypatch, ns=["ns.custom.example."], mx=["10 mail.custom.example."])
    monkeypatch.setattr(domain_email, "cname_chain", lambda *a: {"chain": [], "status": "absent"})
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("secret"))
    )
    infra = block(domain_email.run(context(tmp_path)))
    assert infra["registrar"]["value"] == []
    assert "unavailable" in infra["registrar"]["detail"]
    assert infra["dns_provider"]["value"] == infra["website_provider"]["value"] == []
    assert infra["email_provider"]["value"] == []
    assert infra["website_endpoints"]["value"]["http"] == []
    assert infra["website_endpoints"]["value"]["tls"] == []
    assert infra["account_ownership"]["value"] == "customer confirmation required"


@pytest.mark.parametrize(("kind", "expected", "count"), [("loop", "loop", 1), ("long", "limit", 8)])
def test_cname_chain_stops_on_cycles_and_budget(monkeypatch, kind, expected, count):
    calls = []

    def lookup(resolver, host, qtype):
        calls.append(host)
        return "ok", [host if kind == "loop" else f"hop{len(calls)}.example.com"]

    monkeypatch.setattr(domain_email, "_lookup", lookup)
    result = domain_email.cname_chain(fake({}), "example.com")
    assert result["status"] == expected and len(calls) == count


def test_siteground_headers_are_inferred_and_sensitive_headers_are_not_copied(
    tmp_path, monkeypatch
):
    security, _ = mock_domain(
        monkeypatch,
        headers={"x-sg-cdn": "1", "authorization": "secret", "set-cookie": "session=secret"},
    )
    ctx = context(tmp_path)
    ctx.results = [security]
    infra = block(domain_email.run(ctx))
    assert "SiteGround" in {p["provider"] for p in infra["website_provider"]["value"]}
    assert "secret" not in json.dumps(infra)


def test_infrastructure_report_and_store_preserve_attribution_without_lifecycle_changes(
    tmp_path, monkeypatch
):
    security, _ = mock_domain(monkeypatch)
    assert list(REGISTRY).index("security") < list(REGISTRY).index("domain_email")
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "security": SimpleNamespace(categories={"security"}, run=lambda ctx: security),
            "domain_email": domain_email,
        },
    )
    ctx = context(tmp_path)
    ctx.categories = {"domain_email", "security"}
    ctx.store_root = tmp_path
    runner.run(ctx, no_ai=True, formats={"md"})
    report = (ctx.run_dir / "report.md").read_text()
    assert "Infrastructure attribution" in report and "Example Registrar" in report
    assert "OBSERVED" in report and "INFERRED" in report and "customer-confirmed" in report
    assert "origin host unknown" in report and "customer confirmation required" in report
    saved = store.load(tmp_path, "test")
    assert saved is not None
    infra = next(
        e["data"]["infrastructure"] for e in saved["evidence"] if "infrastructure" in e["data"]
    )
    assert "ev-security-r1-001" in infra["website_endpoints"]["evidence_ids"]
