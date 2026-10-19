import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import dns.name
import dns.resolver
import httpx
import pytest

from ams_assess import runner
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir
from ams_assess.modules import domain_email
from ams_assess.modules.domain_email import NOT_ASSESSED


class FakeResolver:
    """Answers from a {(qname, kind): [values]} table; anything else is NXDOMAIN."""

    def __init__(self, table, nameserver_tables=None):
        self.table, self.nameserver_tables = table, nameserver_tables or {}
        self.nameservers, self.timeout, self.lifetime = [], 2, 2

    def resolve(self, qname, kind):
        table = self.nameserver_tables.get(tuple(self.nameservers), self.table)
        values = table.get((str(qname).rstrip("."), kind))
        if isinstance(values, Exception):
            raise values
        if values is None:
            raise dns.resolver.NXDOMAIN()
        return [
            SimpleNamespace(strings=[v.encode()], to_text=lambda v=v: f'"{v}"')
            if kind == "TXT"
            else SimpleNamespace(to_text=lambda v=v: v)
            for v in values
        ]


def fake(table) -> dns.resolver.Resolver:
    return cast(dns.resolver.Resolver, FakeResolver(table))


def context(tmp_path, **config):
    return Context(
        customer_id="test",
        customer_name="Test",
        target="https://example.com/",
        categories={"domain_email"},
        connections=set(),
        config=config,
        run_dir=create_run_dir(tmp_path, "test"),
        tools_dir=tmp_path,
    )


def status_of(checks, name):
    return next(c["status"] for c in checks if c["name"] == name)


@pytest.mark.parametrize(
    ("records", "configured", "expected"),
    [
        ({("google._domainkey.example.com", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBg"]}, [], "PASS"),
        ({}, [], NOT_ASSESSED),
        ({}, ["s2024"], "WARNING"),
        ({("s1._domainkey.example.com", "TXT"): ["v=DKIM1; p="]}, [], "WARNING"),
    ],
)
def test_dkim_found_unknown_selector_and_revoked(records, configured, expected):
    result = ModuleResult(name="domain_email")
    outcome = domain_email.dkim_check(result, fake(records), "example.com", configured)
    assert outcome["status"] == expected and outcome["evidence_ids"] == ["ev-domain_email-001"]
    if not configured:
        assert set(result.evidence[0].data) == set(domain_email.COMMON_DKIM_SELECTORS)
    if expected == NOT_ASSESSED:
        assert "selector unknown" in outcome["detail"]


def test_dnsbl_listed_refused_and_spamhaus_licensing():
    table = {
        ("10.2.0.192.bl.spamcop.net", "A"): ["127.0.0.2"],
        # Barracuda answers its never-listed test point: the resolver is refused, not listed.
        ("1.0.0.127.b.barracudacentral.org", "A"): ["127.255.255.254"],
        ("10.2.0.192.psbl.surriel.com", "A"): dns.resolver.LifetimeTimeout(),
    }
    result = ModuleResult(name="domain_email")
    checks = domain_email.dnsbl_checks(result, fake(table), {"192.0.2.10": "MX mx.example.com"}, "")
    assert status_of(checks, "bl.spamcop.net") == "ERROR"
    assert "127.0.0.2" in checks[0]["detail"]
    assert status_of(checks, "b.barracudacentral.org") == NOT_ASSESSED
    assert status_of(checks, "psbl.surriel.com") == NOT_ASSESSED
    assert status_of(checks, "Spamhaus ZEN") == NOT_ASSESSED
    assert domain_email.row("dnsbl", checks)["status"] == "ERROR"
    keyed = ModuleResult(name="domain_email")
    checks = domain_email.dnsbl_checks(keyed, fake({}), {"192.0.2.10": "website"}, "sek")
    assert status_of(checks, "Spamhaus ZEN (DQS)") == "PASS"
    assert "sek" not in json.dumps(keyed.model_dump())


def test_dns_consistency_mismatch_and_resolver_failure(monkeypatch):
    base = {
        ("example.com", "A"): ["192.0.2.1"],
        ("example.com", "MX"): ["10 mx.example.com."],
        ("example.com", "NS"): ["a.ns.example.net.", "b.ns.example.net."],
        ("example.com", "TXT"): ["v=spf1 -all", "unrelated"],
    }
    tables = {
        ("1.1.1.1",): base,
        ("8.8.8.8",): {**base, ("example.com", "A"): ["192.0.2.99"]},
        ("9.9.9.9",): {k: dns.resolver.NoNameservers() for k in base},
    }
    monkeypatch.setattr(
        domain_email,
        "_resolver",
        lambda ip=None, timeout=2: FakeResolver({}, tables) if not ip else _with(ip, tables),
    )
    checks = domain_email.consistency_checks(
        ModuleResult(name="domain_email"), "example.com", "example.com"
    )
    assert [c["status"] for c in checks] == ["WARNING", "PASS", "PASS", "PASS"]
    assert "Quad9 9.9.9.9" in checks[1]["detail"]


def _with(ip, tables):
    resolver = FakeResolver({}, tables)
    resolver.nameservers = [ip]
    return resolver


def test_null_mx_domain_is_not_an_email_failure(tmp_path, monkeypatch):
    table = {
        ("example.com", "A"): ["192.0.2.1"],
        ("example.com", "MX"): ["0 ."],
        ("example.com", "NS"): ["a.ns.example.net.", "b.ns.example.net."],
        ("example.com", "SOA"): ["a.ns.example.net. dns.example.net. 1 2 3 4 5"],
        ("example.com", "TXT"): ["v=spf1 -all"],
    }
    checkdmarc = {
        "dnssec": False,
        "spf": {"record": "v=spf1 -all", "valid": True, "parsed": {"all": "fail"}},
        "dmarc": {"record": None, "valid": False, "error": "A DMARC record does not exist."},
    }
    events = [{"eventAction": "expiration", "eventDate": "2099-01-01T00:00:00Z"}]
    monkeypatch.setattr(dns.resolver, "Resolver", lambda configure=True: FakeResolver(table))
    monkeypatch.setattr(
        dns.resolver, "zone_for_name", lambda *a, **k: dns.name.from_text("example.com")
    )
    monkeypatch.setattr(
        domain_email.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(checkdmarc)),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **k: httpx.Response(
            200, request=httpx.Request("GET", url), json={"events": events}
        ),
    )
    monkeypatch.setattr(
        domain_email.smtplib, "SMTP", lambda **k: pytest.fail("Null MX has no host to probe")
    )
    result = domain_email.run(context(tmp_path, AMS_MAIL_TLS_CHECK="1"))
    rows = {r["key"]: r for r in result.checks}
    assert rows["mail_server"]["status"] == "PASS" and rows["mail_server"]["issue"] == "None found."
    assert "Null MX" in rows["mail_server"]["checks"][0]["detail"]
    auth = {c["name"]: c["status"] for c in rows["email_auth"]["checks"]}
    assert auth == {"SPF": "PASS", "DKIM": NOT_ASSESSED, "DMARC": NOT_ASSESSED}
    assert not any(r["status"] == "ERROR" for r in rows.values())
    assert not result.findings
    assert rows["registration"]["status"] == "PASS"
    assert all(r["evidence_ids"] for r in rows.values() if r["status"] != NOT_ASSESSED)


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("2026-01-01T00:00:00Z", "ERROR"),
        ("2026-06-20T00:00:00Z", "WARNING"),
        ("2027-06-01T00:00:00Z", "PASS"),
        (None, NOT_ASSESSED),
    ],
)
def test_rdap_expiry_thresholds(date, expected):
    events = [{"eventAction": "expiration", "eventDate": date}] if date else []
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert domain_email.registration_check(events, "ev-1", now)["status"] == expected


def test_mail_tls_probe_is_bounded_and_blocked_port_is_not_assessed(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] <= 10

        def connect(self, host, port):
            calls.append(("connect", host, port))
            if host == "blocked.example.com":
                raise TimeoutError
            return 220, b"mx ESMTP"

        def ehlo(self):
            calls.append(("ehlo",))

        def has_extn(self, name):
            return False

        def quit(self):
            calls.append(("quit",))

    monkeypatch.setattr(domain_email.smtplib, "SMTP", FakeSMTP)
    result = ModuleResult(name="domain_email")
    hosts = ["mx1.example.com", "mx2.example.com", "mx3.example.com"]
    checks = domain_email.mail_tls_checks(result, hosts)
    assert status_of(checks, "STARTTLS") == "ERROR"
    assert {c[0] for c in calls} == {"connect", "ehlo", "quit"}
    assert len([c for c in calls if c[0] == "connect"]) == 2
    checks = domain_email.mail_tls_checks(result, ["blocked.example.com"])
    assert all(c["status"] == NOT_ASSESSED and "port 25" in c["detail"] for c in checks)


def test_health_summary_renders_statuses_only_when_selected(tmp_path, monkeypatch):
    def fake_domain(ctx):
        result = ModuleResult(name="domain_email")
        eid = result.add_evidence(ctx.target, "DNS NS lookup", {})
        result.checks = [
            domain_email.row("dns", [domain_email.check("Nameservers", "PASS", "2.", [eid])]),
            domain_email.row(
                "registration",
                [domain_email.check("Expiry", "WARNING", "Expires soon.", [eid])],
            ),
        ]
        return result

    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {"domain_email": SimpleNamespace(categories={"domain_email"}, run=fake_domain)},
    )
    ctx = context(tmp_path)
    data = runner.run(ctx, no_ai=True, formats={"md"})
    statuses = {r["key"]: r["status"] for r in data["domain_email_health"]["rows"]}
    assert statuses["dns"] == "PASS" and statuses["registration"] == "WARNING"
    assert statuses["web_tls"] == NOT_ASSESSED and statuses["dnsbl"] == NOT_ASSESSED
    assert "domain_email_health" in json.loads((ctx.run_dir / "results.json").read_text())
    report = (ctx.run_dir / "report.md").read_text()
    assert "| DNS configuration &amp; health | PASS |" in report
    assert "| Domain registration &amp; expiry | WARNING |" in report
    assert "| HTTP security headers | NOT ASSESSED |" in report
    assert "do not measure inbox placement" in report
    other = context(tmp_path)
    other.categories = {"security"}
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "security": SimpleNamespace(
                categories={"security"},
                run=lambda c: ModuleResult(
                    name="security",
                    evidence=[
                        Evidence(
                            id="ev-security-001",
                            source="security",
                            url=c.target,
                            description="headers",
                            data={},
                        )
                    ],
                ),
            )
        },
    )
    data = runner.run(other, no_ai=True, formats={"md"})
    assert "domain_email_health" not in data
    assert "Email Health summary" not in (other.run_dir / "report.md").read_text()
