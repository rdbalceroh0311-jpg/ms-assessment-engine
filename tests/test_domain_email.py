import io
import json
import ssl
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

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
        domain_email, "_SMTP", lambda *a, **k: pytest.fail("Null MX has no host to probe")
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


def mx_address(monkeypatch):
    monkeypatch.setattr(
        domain_email, "_lookup", lambda resolver, host, kind: ("ok", ["192.0.2.25"])
    )


def test_mail_tls_probe_is_bounded_and_blocked_port_is_not_assessed(monkeypatch):
    calls = []

    class FakeSMTP:
        banner = b"mx ESMTP"

        def __init__(self, host, address, deadline, cancelled, holder, **kwargs):
            assert deadline - time.monotonic() <= 10
            calls.append(("connect", host, address))
            if host == "blocked.example.com":
                raise TimeoutError

        def ehlo(self):
            calls.append(("ehlo",))

        def has_extn(self, name):
            return False

    mx_address(monkeypatch)
    monkeypatch.setattr(domain_email, "_SMTP", FakeSMTP)
    result = ModuleResult(name="domain_email")
    hosts = ["mx1.example.com", "mx2.example.com", "mx3.example.com"]
    checks = domain_email.mail_tls_checks(result, hosts)
    assert status_of(checks, "STARTTLS") == "ERROR"
    assert {c[0] for c in calls} == {"connect", "ehlo"}
    assert len([c for c in calls if c[0] == "connect"]) == 2
    checks = domain_email.mail_tls_checks(result, ["blocked.example.com"])
    assert all(c["status"] == NOT_ASSESSED and "port 25" in c["detail"] for c in checks)


class FakeSocket:
    """SMTP peer that serves `replies` line by line, then stalls until it is closed."""

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []
        self.closed, self.shut = threading.Event(), False

    def makefile(self, mode):
        return self

    def readline(self, limit=-1):
        if self.replies:
            return self.replies.pop(0)
        self.closed.wait(5)
        return b""

    def sendall(self, data):
        if self.closed.is_set():
            raise OSError("socket closed")
        self.sent.append(data)

    def settimeout(self, timeout):
        assert 0 < timeout <= 10

    def shutdown(self, how):
        self.shut = True
        self.closed.set()

    def close(self):
        self.closed.set()

    def commands(self):
        return [line.split()[0].upper() for line in b"".join(self.sent).decode().splitlines()]


def test_starttls_verifies_certificate_against_mx_hostname(monkeypatch):
    plain = FakeSocket(
        [
            b"220 mx.example.com ESMTP\r\n",
            b"250-mx.example.com\r\n",
            b"250 STARTTLS\r\n",
            b"220 Go\r\n",
        ]
    )
    wrapped = {}
    tls = MagicMock(spec=ssl.SSLSocket)
    tls.getpeercert.return_value = {"notAfter": "Jan  1 00:00:00 2099 GMT"}
    tls.makefile.return_value = io.BytesIO(b"221 Bye\r\n")

    def create_connection(address, timeout):
        assert 0 < timeout <= 10
        wrapped["connect"] = address
        return plain

    def wrap_socket(sock, server_hostname=None, do_handshake_on_connect=True):
        # The real default context raises without server_hostname when check_hostname is on.
        wrapped["server_hostname"] = server_hostname
        assert do_handshake_on_connect is False
        return tls

    mx_address(monkeypatch)
    monkeypatch.setattr(domain_email.socket, "create_connection", create_connection)
    context = SimpleNamespace(wrap_socket=wrap_socket)
    monkeypatch.setattr(domain_email.ssl, "create_default_context", lambda: context)
    info = domain_email.smtp_tls("mx.example.com")
    assert wrapped == {"connect": ("192.0.2.25", 25), "server_hostname": "mx.example.com"}
    assert info["banner"] == "mx.example.com ESMTP" and info["starttls"] is True
    assert info["certificate"]["verified"] is True and "error" not in info
    assert plain.commands() == ["EHLO", "STARTTLS"]
    tls.do_handshake.assert_called_once_with()


def test_slow_mx_is_cancelled_without_further_commands(monkeypatch):
    # Greeting arrives, then the server stalls on the EHLO reply.
    stalled = FakeSocket([b"220 mx.example.com ESMTP\r\n"])
    mx_address(monkeypatch)
    monkeypatch.setattr(domain_email.socket, "create_connection", lambda address, timeout: stalled)
    started = time.monotonic()
    info = domain_email.smtp_tls("slow.example.com", timeout=0.3)
    assert "limit" in info["error"] and "banner" not in info
    assert time.monotonic() - started < 1.5
    assert stalled.shut and stalled.closed.is_set()
    time.sleep(0.3)
    assert stalled.commands() == ["EHLO"]  # no STARTTLS or QUIT after cancellation
    assert not any(t.name == "smtp-probe-slow.example.com" for t in threading.enumerate())


class StalledTLS(FakeSocket):
    """TLS layer whose handshake blocks until the socket is shut down or closed."""

    def __init__(self):
        super().__init__([])
        self.timeouts, self.handshake_ended = [], threading.Event()

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def do_handshake(self):
        self.closed.wait(5)
        self.handshake_ended.set()
        raise ssl.SSLError("handshake aborted")


def test_stalled_tls_handshake_is_cancelled_by_closing_tls_socket(monkeypatch):
    plain = FakeSocket(
        [
            b"220 mx.example.com ESMTP\r\n",
            b"250-mx.example.com\r\n",
            b"250 STARTTLS\r\n",
            b"220 Go\r\n",
        ]
    )
    tls = StalledTLS()

    def wrap_socket(sock, server_hostname=None, do_handshake_on_connect=True):
        assert do_handshake_on_connect is False and server_hostname == "tls-stall.example.com"
        return tls

    mx_address(monkeypatch)
    monkeypatch.setattr(domain_email.socket, "create_connection", lambda address, timeout: plain)
    context = SimpleNamespace(wrap_socket=wrap_socket)
    monkeypatch.setattr(domain_email.ssl, "create_default_context", lambda: context)
    started = time.monotonic()
    info = domain_email.smtp_tls("tls-stall.example.com", timeout=0.5)
    elapsed = time.monotonic() - started
    assert "limit" in info["error"] and "certificate" not in info
    assert elapsed <= 0.5 + 0.3
    # The caller shut down the TLS-layer socket, which is what aborted the handshake.
    assert tls.shut and tls.closed.is_set() and tls.handshake_ended.wait(1)
    assert tls.timeouts and all(0 < t <= 0.5 for t in tls.timeouts)
    time.sleep(0.3)
    assert plain.commands() == ["EHLO", "STARTTLS"] and tls.sent == []  # no QUIT
    assert not any(t.name == "smtp-probe-tls-stall.example.com" for t in threading.enumerate())


def test_mx_resolution_counts_only_hosts_that_resolved(tmp_path, monkeypatch):
    table = {
        ("example.com", "MX"): ["10 mx1.example.com.", "20 mx2.example.com."],
        ("mx1.example.com", "A"): ["192.0.2.25"],
        ("mx2.example.com", "A"): dns.resolver.LifetimeTimeout(),
        ("mx2.example.com", "AAAA"): dns.resolver.LifetimeTimeout(),
    }
    monkeypatch.setattr(dns.resolver, "Resolver", lambda configure=True: FakeResolver(table))
    monkeypatch.setattr(
        dns.resolver, "zone_for_name", lambda *a, **k: dns.name.from_text("example.com")
    )
    monkeypatch.setattr(
        domain_email.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError())
    )
    monkeypatch.setattr(
        httpx, "get", lambda url, **k: (_ for _ in ()).throw(httpx.ConnectError("offline"))
    )
    result = domain_email.run(context(tmp_path))
    mail = {
        c["name"]: c for c in next(r for r in result.checks if r["key"] == "mail_server")["checks"]
    }
    assert mail["MX resolution"]["status"] == "PASS"
    assert "mx1.example.com" in mail["MX resolution"]["detail"]
    assert "mx2" not in mail["MX resolution"]["detail"]
    assert mail["MX lookup"]["status"] == NOT_ASSESSED
    assert "mx2.example.com" in mail["MX lookup"]["detail"]
    assert not any(f.check_key.startswith("health:mail_server") for f in result.findings)


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
    assert "| DNS configuration & health | PASS |" in report
    assert "| Domain registration & expiry | WARNING |" in report
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


@pytest.mark.parametrize("outcome", ["error", "skipped"])
def test_summary_renders_when_module_ran_even_on_error(tmp_path, monkeypatch, outcome):
    def module(ctx):
        if outcome == "error":
            raise RuntimeError("synthetic failure")
        return ModuleResult(name="domain_email", status="skipped", reason="Local target")

    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {"domain_email": SimpleNamespace(categories={"domain_email"}, run=module)},
    )
    ctx = context(tmp_path)
    data = runner.run(ctx, no_ai=True, formats={"md"})
    assert data["assessed_categories"] == []
    report = (ctx.run_dir / "report.md").read_text()
    if outcome == "error":
        assert "Email Health summary" in report
        assert "| DNS configuration & health | NOT ASSESSED |" in report
        assert "domain\\_email module error: Module failed (RuntimeError)" in report
    else:
        assert "Email Health summary" not in report
        assert "domain\\_email: no successful evidence collection" in report
