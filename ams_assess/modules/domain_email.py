import contextlib
import ipaddress
import json
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import dns.exception
import dns.resolver
import httpx

from ams_assess.models import Context, ModuleResult
from ams_assess.targets import registrable

name = "domain_email"
categories = {"domain_email"}
requires = ["public DNS", "RDAP", "public DNSBLs", "SMTP port 25 only with --mail-tls-check"]

NOT_ASSESSED = "NOT ASSESSED"
DELIVERABILITY_NOTE = (
    "DNS, authentication and blacklist checks do not measure inbox placement; "
    "they cannot guarantee email deliverability."
)
COMMON_DKIM_SELECTORS = ["google", "selector1", "selector2", "default", "k1", "s1", "s2", "mail"]
# Queryable without a paid licence; each answered its RFC 5782 127.0.0.2 test point (2026-10).
# Barracuda requires free registration of the querying resolver's IP or it may refuse queries.
DNSBLS = ["bl.spamcop.net", "b.barracudacentral.org", "psbl.surriel.com"]
MAX_DNSBL_QUERIES = 30
PUBLIC_RESOLVERS = {
    "Cloudflare 1.1.1.1": "1.1.1.1",
    "Google 8.8.8.8": "8.8.8.8",
    "Quad9 9.9.9.9": "9.9.9.9",
}
AREAS = {
    "dns": (
        "DNS configuration & health",
        "Nameservers (at least two), SOA, A/AAAA, CAA (informational) and DNSSEC",
        "Broken or single-provider DNS takes the website and email offline together.",
    ),
    "dns_consistency": (
        "DNS consistency",
        "A, MX, NS and SPF answers from Cloudflare, Google and Quad9 public resolvers",
        "Disagreeing resolvers indicate unfinished propagation or a split configuration.",
    ),
    "email_auth": (
        "Email authentication",
        "SPF, DKIM (known or common selectors) and DMARC records",
        "Receivers use SPF, DKIM and DMARC to reject spoofed mail and to trust genuine mail.",
    ),
    "mail_server": (
        "Mail server configuration",
        "MX or null MX, MX hostname resolution, STARTTLS and MX certificate (opt-in)",
        "Missing or broken MX records stop inbound email; no STARTTLS exposes mail in transit.",
    ),
    "dnsbl": (
        "Email blacklist / reputation",
        "MX and website IPv4 addresses against free public DNS blocklists",
        "Listed sending addresses are commonly rejected or filtered as spam.",
    ),
    "web_tls": (
        "Website server & SSL/TLS",
        "HTTP response and verified TLS certificate (reused from the Security section)",
        "Certificate errors stop visitors and damage trust.",
    ),
    "http_headers": (
        "HTTP security headers",
        "Security header presence and MDN Observatory grade (reused from the Security section)",
        "Security headers reduce exposure to common browser-based attacks.",
    ),
    "registration": (
        "Domain registration & expiry",
        "RDAP registration expiry date",
        "An expired domain takes the website and email offline and can be lost.",
    ),
    "verification": (
        "Google / Facebook verification records",
        "google-site-verification and facebook-domain-verification DNS TXT records (informational)",
        "DNS verification proves ownership to platforms; other methods are equally valid.",
    ),
}


def check(
    name: str,
    status: str,
    detail: str,
    evidence: list[str] | None = None,
    fix: str = "",
    finding: str = "",
    severity: str = "medium",
    confidence: str = "confirmed",
) -> dict[str, Any]:
    """One sub-check; a non-empty finding title raises a finding when it fails."""
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "evidence_ids": [e for e in evidence or [] if e],
        "fix": fix,
        "_finding": (finding, severity, confidence),
    }


def row(key: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    area, checked, why = AREAS[key]
    checks = [{k: v for k, v in c.items() if k != "_finding"} for c in checks]
    for c in checks:
        c["detail"] = c["detail"].rstrip(".")
        # Every assessed status must be traceable to evidence; otherwise it is not assessed.
        if c["status"] != NOT_ASSESSED and not c["evidence_ids"]:
            c["status"], c["detail"] = NOT_ASSESSED, c["detail"] + " (no evidence recorded)"
    statuses = {c["status"] for c in checks}
    status = next((s for s in ("ERROR", "WARNING", "PASS") if s in statuses), NOT_ASSESSED)
    problems = [c for c in checks if c["status"] in {"ERROR", "WARNING"}]
    skipped = [c for c in checks if c["status"] == NOT_ASSESSED]
    issues = [f"{c['name']}: {c['detail']}" for c in problems]
    if skipped:
        issues.append("Not assessed: " + "; ".join(f"{c['name']} ({c['detail']})" for c in skipped))
    fixes = dict.fromkeys(c["fix"] for c in (problems or skipped) if c["fix"])
    return {
        "key": key,
        "area": area,
        "status": status,
        "checked": checked,
        "issue": "; ".join(issues) + "." if issues else "None found.",
        "why": why,
        "recommendation": " ".join(fixes) or "No correction required.",
        "evidence_ids": sorted(
            {e for c in checks if c["status"] != NOT_ASSESSED for e in c["evidence_ids"]}
        ),
        "checks": checks,
    }


def not_assessed(key: str, reason: str) -> dict[str, Any]:
    return row(key, [check(AREAS[key][0], NOT_ASSESSED, reason)])


def _resolver(nameserver: str | None = None, timeout: float = 2) -> dns.resolver.Resolver:
    resolver = dns.resolver.Resolver(configure=nameserver is None)
    if nameserver:
        resolver.nameservers = [nameserver]
    resolver.timeout = resolver.lifetime = timeout
    return resolver


def _lookup(resolver: dns.resolver.Resolver, qname: str, kind: str) -> tuple[str, list[str]]:
    """Return ("ok"|"absent"|"error", records-or-[error name]); TXT strings are joined."""
    try:
        answer = resolver.resolve(qname, kind)
        records = [
            b"".join(r.strings).decode(errors="replace") if kind == "TXT" else r.to_text()
            for r in answer
        ]
        return "ok", sorted(records)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return "absent", []
    except dns.exception.DNSException as exc:
        return "error", [type(exc).__name__]


def dkim_check(
    result: ModuleResult,
    resolver: dns.resolver.Resolver,
    domain: str,
    configured: list[str],
    no_mail: bool = False,
) -> dict[str, Any]:
    selectors = configured or COMMON_DKIM_SELECTORS
    found, revoked, lookups = [], [], {}
    for selector in selectors[:10]:
        status, records = _lookup(resolver, f"{selector}._domainkey.{domain}", "TXT")
        lookups[selector] = {"status": status}
        for record in records if status == "ok" else []:
            tags = {
                k.strip().lower(): v.strip()
                for k, _, v in (p.partition("=") for p in record.split(";"))
                if k.strip()
            }
            if "p" not in tags and tags.get("v") != "DKIM1":
                continue
            lookups[selector].update(
                version=tags.get("v", ""),
                key_type=tags.get("k", "rsa"),
                public_key_chars=len(tags.get("p", "")),
            )
            (found if tags.get("p") else revoked).append(selector)
    eid = result.add_evidence(
        f"dns:{domain}",
        "DKIM selector lookups (" + ("configured" if configured else "common") + " selectors)",
        lookups,
    )
    note = f" Revoked (empty p=): {', '.join(revoked)}." if revoked else ""
    if found:
        return check(
            "DKIM", "PASS", f"Key published at selector(s) {', '.join(found)}.{note}", [eid]
        )
    fix = "Confirm the active DKIM selector with the email provider and publish its public key."
    if revoked and no_mail:
        return check(
            "DKIM",
            "PASS",
            f"Revoked key (empty p=) at {', '.join(revoked)}: no-mail posture.",
            [eid],
        )
    if revoked:
        return check(
            "DKIM",
            "WARNING",
            f"Selector(s) {', '.join(revoked)} publish a revoked key (empty p=).",
            [eid],
            fix,
            "DKIM key revoked at known selector",
            severity="low",
            confidence="needs_verification",
        )
    if configured and any(v["status"] == "absent" for v in lookups.values()):
        return check(
            "DKIM",
            "WARNING",
            f"No DKIM key at configured selector(s) {', '.join(configured)}.",
            [eid],
            fix,
            "No DKIM key at the configured selector",
            confidence="needs_verification",
        )
    if all(v["status"] == "error" for v in lookups.values()):
        return check("DKIM", NOT_ASSESSED, "DKIM lookups failed", [eid], fix)
    return check(
        "DKIM",
        NOT_ASSESSED,
        "selector unknown; DKIM cannot be confirmed without the customer's selector",
        [eid],
        "Rerun with --dkim-selectors from the email provider.",
    )


def _dnsbl_query(resolver: dns.resolver.Resolver, ip: str, zone: str) -> tuple[str, str]:
    qname = ".".join(reversed(ip.split("."))) + "." + zone
    status, records = _lookup(resolver, qname, "A")
    if status == "absent":
        return "clean", ""
    if status == "error":
        return "error", records[0]
    code = ipaddress.ip_address(records[0])
    # 127.0.0.2-254 means listed; 127.255.255.x, 127.0.0.255 and anything else mean refused.
    if code in ipaddress.ip_network("127.0.0.0/24") and 2 <= int(code) % 256 <= 254:
        return "listed", str(code)
    return "refused", str(code)


def dnsbl_checks(
    result: ModuleResult, resolver: dns.resolver.Resolver, ips: dict[str, str], dqs_key: str
) -> list[dict[str, Any]]:
    lists = {zone: zone for zone in DNSBLS}
    if dqs_key:
        lists["Spamhaus ZEN (DQS)"] = f"{dqs_key}.zen.dq.spamhaus.net"
    budget, outcomes = MAX_DNSBL_QUERIES, {}
    for label, zone in lists.items():
        # RFC 5782: 127.0.0.1 is never listed, so an answer means the list refuses our resolver.
        state, code = _dnsbl_query(resolver, "127.0.0.1", zone)
        budget -= 1
        outcomes[label] = {
            "usable": state == "clean",
            "test_point": f"{state} {code}".strip(),
            "results": {},
        }
    for ip in ips:
        for label, zone in lists.items():
            if outcomes[label]["usable"] and budget > 0:
                outcomes[label]["results"][ip] = " ".join(_dnsbl_query(resolver, ip, zone)).strip()
                budget -= 1
    eid = result.add_evidence(
        "dnsbl",
        "DNSBL lookups for MX and website IPv4 addresses",
        {"addresses": ips, "lists": outcomes, "query_cap": MAX_DNSBL_QUERIES},
    )
    checks = []
    for label, outcome in outcomes.items():
        answers = outcome["results"]
        listed = {ip: a.split()[1] for ip, a in answers.items() if a.startswith("listed")}
        clean = [ip for ip, a in answers.items() if a == "clean"]
        if not outcome["usable"]:
            checks.append(
                check(
                    label,
                    NOT_ASSESSED,
                    f"list refused or did not answer its test point ({outcome['test_point']})",
                    [eid],
                )
            )
        elif listed:
            detail = ", ".join(f"{ip} ({ips[ip]}) returned {code}" for ip, code in listed.items())
            checks.append(
                check(
                    label,
                    "ERROR",
                    f"Listed: {detail}.",
                    [eid],
                    "Identify the cause of the listing, fix it, then request delisting.",
                    f"IP address listed on {label}",
                    severity="high",
                )
            )
        elif clean:
            other = len(answers) - len(clean)
            note = f" {other} lookup(s) refused or timed out." if other else ""
            checks.append(
                check(label, "PASS", f"{len(clean)} address(es) not listed.{note}", [eid])
            )
        else:
            checks.append(check(label, NOT_ASSESSED, "lookups refused or timed out", [eid]))
    if not dqs_key:
        checks.append(
            check(
                "Spamhaus ZEN",
                NOT_ASSESSED,
                "not queried: the free public mirror excludes commercial use and public resolvers",
                fix="Set SPAMHAUS_DQS_KEY (Spamhaus Data Query Service) to include Spamhaus.",
            )
        )
    return checks


class _SMTP(smtplib.SMTP):
    """SMTP client bounded by a shared deadline and cancellation flag.

    Connects to a pre-resolved address while keeping the MX name for STARTTLS certificate
    verification. Every reply read and command write first checks the deadline/flag and sets
    the socket timeout to the remaining time, so nothing is sent after cancellation.
    """

    banner = b""

    def __init__(
        self,
        host: str,
        address: str,
        deadline: float,
        cancelled: threading.Event,
        holder: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self.mx_host, self.address, self.deadline = host, address, deadline
        self.cancelled = cancelled
        self.lock: threading.Lock = holder["lock"]
        holder["client"] = self
        super().__init__(host, 25, **kwargs)

    def _remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if self.cancelled.is_set() or left <= 0:
            raise TimeoutError("SMTP probe cancelled or out of time")
        if self.sock is not None:
            self.sock.settimeout(left)
        return left

    def _get_socket(self, host, port, timeout):
        return socket.create_connection((self.address, port), self._remaining())

    def connect(self, host="localhost", port=0, source_address=None):
        code, self.banner = super().connect(host, port, source_address)
        return code, self.banner

    def getreply(self):
        self._remaining()
        return super().getreply()

    def putcmd(self, cmd, args=""):
        self._remaining()
        super().putcmd(cmd, args)

    def starttls(self, *, context=None):
        """smtplib.starttls, but the TLS socket becomes cancellable before the handshake."""
        resp, reply = self.docmd("STARTTLS")
        if resp != 220:
            raise smtplib.SMTPResponseException(resp, reply)
        plain = self.sock
        if plain is None:
            raise smtplib.SMTPServerDisconnected("not connected")
        context = context or ssl.create_default_context()
        tls = context.wrap_socket(
            plain, server_hostname=self.mx_host, do_handshake_on_connect=False
        )
        with self.lock:
            if self.cancelled.is_set():
                tls.close()
                raise TimeoutError("SMTP probe cancelled")
            self.sock = tls  # the caller's cancel path now shuts down the TLS socket
        self._remaining()
        tls.do_handshake()
        self.file = None
        self.helo_resp = self.ehlo_resp = None
        self.esmtp_features, self.does_esmtp = {}, False
        return resp, reply


def _smtp_probe(
    host: str, address: str, deadline: float, cancelled: threading.Event, holder: dict[str, Any]
) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        smtp = _SMTP(host, address, deadline, cancelled, holder, local_hostname="localhost")
        info["banner"] = smtp.banner.decode(errors="replace")[:200]
        smtp.ehlo()
        info["starttls"] = smtp.has_extn("starttls")
        if info["starttls"]:
            try:
                smtp.starttls(context=ssl.create_default_context())
                cert = smtp.sock.getpeercert() if isinstance(smtp.sock, ssl.SSLSocket) else None
                if not cert or not isinstance(cert.get("notAfter"), str):
                    raise ValueError("Missing certificate expiry")
                expiry = datetime.fromtimestamp(
                    ssl.cert_time_to_seconds(str(cert["notAfter"])), UTC
                )
                info["certificate"] = {
                    "verified": True,
                    "hostname_match": True,
                    "expires": expiry.isoformat(),
                    "days_to_expiry": (expiry - datetime.now(UTC)).days,
                }
            except ssl.SSLCertVerificationError as exc:
                info["certificate"] = {"verified": False, "reason": str(exc.verify_message)}
    except (OSError, ValueError, smtplib.SMTPException) as exc:
        info["error"] = type(exc).__name__
    finally:
        client = holder.get("client")
        if client is not None:
            try:
                client.quit()  # putcmd refuses to send QUIT once cancelled or out of time
            except (OSError, smtplib.SMTPException):
                client.close()
    return info


def smtp_tls(host: str, timeout: float = 10) -> dict[str, Any]:
    """Banner, EHLO, STARTTLS, certificate, QUIT. Never MAIL, RCPT, VRFY, EXPN or DATA.

    The whole probe, DNS and cleanup included, is bounded by one wall-clock deadline.
    """
    deadline = time.monotonic() + timeout
    address = None
    for kind in ["A", "AAAA"]:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        status, records = _lookup(_resolver(timeout=min(left, 4)), host, kind)
        if status == "ok":
            address = records[0]
            break
    if address is None:
        return {"host": host, "error": "MX address lookup failed"}
    cancelled, lock, done = threading.Event(), threading.Lock(), {}
    holder: dict[str, Any] = {"lock": lock}

    def work() -> None:
        info = _smtp_probe(host, address, deadline, cancelled, holder)
        with lock:
            if not cancelled.is_set():
                done["info"] = info

    probe = threading.Thread(target=work, name=f"smtp-probe-{host}", daemon=True)
    probe.start()
    probe.join(max(0.0, deadline - time.monotonic()))
    with lock:
        if "info" in done:
            return {"host": host, **done["info"]}
        cancelled.set()  # under the lock: the worker can no longer publish a late result
        # The active socket (plain or TLS) is read under the same lock starttls publishes under.
        sock = getattr(holder.get("client"), "sock", None)
    # Force any blocking read/handshake to fail now rather than at its socket timeout.
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()
    probe.join(1)
    return {"host": host, "error": f"probe exceeded {timeout:g}-second limit"}


def mail_tls_checks(result: ModuleResult, hosts: list[str]) -> list[dict[str, Any]]:
    probes = [smtp_tls(host) for host in hosts[:2]]
    eid = result.add_evidence(
        "smtp", "Bounded SMTP EHLO/STARTTLS probe of up to two MX hosts", probes
    )
    connected = [p for p in probes if "banner" in p]
    if not connected:
        errors = ", ".join(sorted({p.get("error", "") for p in probes} - {""}))
        reason = f"port 25 blocked from assessment host or MX unreachable ({errors})"
        return [
            check("STARTTLS", NOT_ASSESSED, reason, [eid]),
            check("MX certificate", NOT_ASSESSED, reason, [eid]),
        ]
    plain = [p["host"] for p in connected if p.get("starttls") is False]
    offered = [p for p in connected if p.get("starttls")]
    if plain:
        starttls = check(
            "STARTTLS",
            "ERROR",
            f"Not offered by {', '.join(plain)}.",
            [eid],
            "Enable STARTTLS with a valid certificate on every MX host.",
            "MX host does not offer STARTTLS",
        )
    elif offered:
        starttls = check("STARTTLS", "PASS", f"Offered by {len(offered)} MX host(s).", [eid])
    else:
        starttls = check("STARTTLS", NOT_ASSESSED, "EHLO did not complete", [eid])
    certs = {p["host"]: p["certificate"] for p in connected if "certificate" in p}
    failed = {h: c["reason"] for h, c in certs.items() if not c["verified"]}
    expiring = {
        h: c["days_to_expiry"]
        for h, c in certs.items()
        if c["verified"] and c["days_to_expiry"] < 30
    }
    fix = "Install a publicly trusted certificate matching the MX hostname and automate renewal."
    if failed:
        detail = "; ".join(f"{h}: {r}" for h, r in failed.items())
        cert = check(
            "MX certificate",
            "WARNING",
            f"Verification failed ({detail}).",
            [eid],
            fix,
            "MX certificate failed verification",
            severity="low",
            confidence="needs_verification",
        )
    elif expiring:
        detail = ", ".join(f"{h} in {d} days" for h, d in expiring.items())
        cert = check(
            "MX certificate",
            "WARNING",
            f"Expires soon ({detail}).",
            [eid],
            fix,
            "MX certificate expires within 30 days",
        )
    elif certs:
        cert = check("MX certificate", "PASS", "Verified and matches the MX hostname.", [eid])
    else:
        cert = check("MX certificate", NOT_ASSESSED, "no certificate was negotiated", [eid])
    return [starttls, cert]


def consistency_checks(result: ModuleResult, host: str, zone: str) -> list[dict[str, Any]]:
    queries = {"A": host, "MX": host, "NS": zone, "SPF": host}
    answers: dict[str, dict[str, Any]] = {kind: {} for kind in queries}
    for label, ip in PUBLIC_RESOLVERS.items():
        resolver = _resolver(ip)
        for kind, qname in queries.items():
            status, records = _lookup(resolver, qname, "TXT" if kind == "SPF" else kind)
            if status == "error":
                answers[kind][label] = {"error": records[0]}
                continue
            if kind == "SPF":
                records = [r for r in records if r.lower().startswith("v=spf1")]
            answers[kind][label] = sorted({r.lower().rstrip(".") for r in records})
    eid = result.add_evidence(
        f"dns:{host}", "Answers compared across three public resolvers", answers
    )
    checks = []
    for kind, by_resolver in answers.items():
        ok = {k: tuple(v) for k, v in by_resolver.items() if isinstance(v, list)}
        failed = sorted(set(by_resolver) - set(ok))
        note = f" Not assessed for {', '.join(failed)} (lookup failed)." if failed else ""
        if len(ok) < 2:
            checks.append(check(kind, NOT_ASSESSED, "fewer than two resolvers answered", [eid]))
        elif len(set(ok.values())) == 1:
            checks.append(check(kind, "PASS", f"{len(ok)} resolvers agree.{note}", [eid]))
        else:
            checks.append(
                check(
                    kind,
                    "WARNING",
                    f"Resolvers returned different answers.{note}",
                    [eid],
                    "Verify recent DNS changes have propagated; CDN or geo-DNS A records can "
                    "legitimately differ.",
                    f"Public resolvers disagree on {kind} records",
                    severity="low",
                    confidence="needs_verification",
                )
            )
    return checks


def registration_check(events: list[dict], eid: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    expiry = next(
        (e.get("eventDate") for e in events if e.get("eventAction") == "expiration"), None
    )
    if not expiry:
        return check(
            "Expiry",
            NOT_ASSESSED,
            "RDAP publishes no expiration date (common for some ccTLDs)",
            [eid],
            "Confirm the expiry date and auto-renewal with the registrar.",
        )
    try:
        date = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except ValueError:
        return check("Expiry", NOT_ASSESSED, f"unparseable RDAP expiry {expiry!r}", [eid])
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    days = (date - now).days
    fix = "Renew the domain now and enable auto-renewal with current billing details."
    if date <= now:
        return check(
            "Expiry",
            "ERROR",
            f"Expired on {date.date()}.",
            [eid],
            fix,
            "Domain registration has expired",
            severity="critical",
        )
    if days < 30:
        return check(
            "Expiry",
            "WARNING",
            f"Expires on {date.date()} ({days} days).",
            [eid],
            fix,
            "Domain registration expires within 30 days",
            severity="high",
        )
    return check("Expiry", "PASS", f"Expires on {date.date()} ({days} days).", [eid])


def auth_checks(checks: dict, eid: str, mail: bool | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """SPF and DMARC from checkdmarc; findings for these stay with the existing flow."""
    no_mail = mail is False
    out = []
    for key in ["spf", "dmarc"]:
        label, data = key.upper(), checks.get(key, {})
        error = str(data.get("error", "Record did not validate"))
        if data.get("valid"):
            policy = (
                data.get("parsed", {}).get("all")
                if key == "spf"
                else data.get("tags", {}).get("p", {}).get("value")
            )
            if key == "spf" and policy == "pass":
                out.append(
                    check(
                        label,
                        "WARNING",
                        "+all authorises any sender.",
                        [eid],
                        "Replace +all with ~all or -all.",
                        "SPF authorises any sender (+all)",
                        severity="high",
                    )
                )
            elif key == "dmarc" and policy == "none":
                out.append(
                    check(
                        label,
                        "WARNING",
                        "p=none monitors but does not enforce.",
                        [eid],
                        "Review reports, then move to quarantine or reject.",
                    )
                )
            else:
                posture = " (no-mail posture)" if no_mail else ""
                out.append(check(label, "PASS", f"{data.get('record')}{posture}", [eid]))
        elif any(w in error.lower() for w in ["timeout", "timed out", "servfail"]):
            out.append(check(label, NOT_ASSESSED, f"lookup inconclusive ({error})", [eid]))
        elif no_mail and not data.get("record"):
            record = "v=spf1 -all" if key == "spf" else "v=DMARC1; p=reject"
            out.append(
                check(
                    label,
                    NOT_ASSESSED,
                    "domain accepts no mail and has no record",
                    [eid],
                    f"Publish {record} to stop the domain being spoofed.",
                )
            )
        else:
            out.append(
                check(
                    label,
                    "ERROR",
                    error,
                    [eid],
                    f"Confirm sending services and correct {label} safely.",
                )
            )
    return out[0], out[1]


def run(ctx: Context) -> ModuleResult:
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="Local/private target has no public domain audit"
        )
    domain_host = ctx.host
    domain = registrable(domain_host)
    if domain and domain_host == "www." + domain:
        domain_host = domain
    result = ModuleResult(name=name)
    raw, limitations, eids = {}, [], {}
    resolver = dns.resolver.Resolver()
    resolver.timeout, resolver.lifetime = 2, 4
    for kind in ["A", "AAAA", "MX", "NS", "TXT", "CAA"]:
        query_host = ctx.site_host if kind in {"A", "AAAA"} else domain_host
        try:
            records = [r.to_text() for r in resolver.resolve(query_host, kind)]
            raw[kind] = {"status": "ok", "records": records}
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN) as exc:
            raw[kind] = {"status": "absent", "reason": type(exc).__name__, "records": []}
        except dns.exception.DNSException as exc:
            raw[kind] = {"status": "error", "reason": type(exc).__name__}
            limitations.append(f"{kind} lookup failed ({type(exc).__name__})")
        eids[kind] = result.add_evidence(
            f"dns:{query_host}", f"DNS {kind} lookup for {query_host}", raw[kind]
        )
    try:
        zone = dns.resolver.zone_for_name(domain_host, resolver=resolver).to_text().rstrip(".")
    except dns.exception.DNSException:
        zone = domain_host
    zone_dns = {"zone": zone, "SOA": _lookup(resolver, zone, "SOA")}
    zone_dns["NS"] = (
        _lookup(resolver, zone, "NS")
        if zone != domain_host
        else (raw["NS"]["status"], raw["NS"].get("records", []))
    )
    raw["zone"] = zone_dns
    zone_eid = result.add_evidence(
        f"dns:{zone}", f"DNS SOA and NS lookup for zone {zone}", zone_dns
    )
    exchanges = sorted(
        (int(pref), host.rstrip(".") or ".")
        for pref, host in (r.split() for r in raw["MX"].get("records", []))
    )
    null_mx = any(host == "." for _, host in exchanges)
    mx_hosts = [host for _, host in exchanges if host != "."]
    mail = None if raw["MX"]["status"] == "error" else bool(mx_hosts)
    # Bound checkdmarc's entire operation as well as each DNS lookup. SMTP is probed separately,
    # only with explicit opt-in.
    code = (
        "import checkdmarc,json,sys; print(json.dumps(checkdmarc.check_domains("
        "[sys.argv[1]],skip_tls=True,timeout=2,retries=0),default=str))"
    )
    checks, dmarc_eid = None, ""
    try:
        process = subprocess.run(
            [sys.executable, "-c", code, domain_host],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        checks = json.loads(process.stdout)
        raw["checkdmarc"] = checks
        eid = dmarc_eid = result.add_evidence(
            ctx.target, "checkdmarc SPF, DMARC, MX and DNSSEC checks", checks
        )
        for key in ["spf", "dmarc"]:
            check_data = checks.get(key, {})
            # A missing record is not a failure for a domain that accepts no mail.
            if check_data.get("valid") is False and not (
                mail is False and not check_data.get("record")
            ):
                detail = check_data.get("error", "Record did not validate")
                uncertain = any(
                    w in str(detail).lower() for w in ["timeout", "timed out", "servfail"]
                )
                result.finding(
                    f"invalid-{key}",
                    f"Review {key.upper()} configuration",
                    "domain_email",
                    [eid],
                    str(detail),
                    f"Confirm sending services and correct {key.upper()} safely.",
                    confidence="needs_verification" if uncertain else "confirmed",
                )
        policy = checks.get("dmarc", {}).get("tags", {}).get("p", {}).get("value")
        if policy == "none":
            result.finding(
                "dmarc-monitoring",
                "DMARC is in monitoring mode",
                "domain_email",
                [eid],
                "The published DMARC policy is p=none.",
                "Review alignment reports before moving to quarantine or reject.",
                kind="opportunity",
                severity="low",
            )
    except (subprocess.SubprocessError, ValueError):
        limitations.append("checkdmarc failed or exceeded its 60-second budget")
    rdap, rdap_eid = None, ""
    try:
        response = httpx.get(f"https://rdap.org/domain/{zone}", timeout=15, follow_redirects=True)
        response.raise_for_status()
        rdap = response.json()
        raw["rdap"] = rdap
        summary = {
            "domain": rdap.get("ldhName"),
            "events": rdap.get("events", []),
            "registrar": [e for e in rdap.get("entities", []) if "registrar" in e.get("roles", [])],
            "privacy_redaction_observed": rdap_privacy_redacted(rdap),
        }
        rdap_eid = result.add_evidence(
            ctx.target, "RDAP registrar and registration/expiry events", summary
        )
    except (httpx.HTTPError, ValueError) as exc:
        limitations.append(
            f"RDAP unavailable ({type(exc).__name__}); may require registrable domain"
        )

    ns_status, ns_records = zone_dns["NS"]
    soa_status, soa_records = zone_dns["SOA"]
    if ns_status == "error":
        ns = check("Nameservers", NOT_ASSESSED, "lookup failed", [zone_eid])
    elif len(ns_records) >= 2:
        ns = check("Nameservers", "PASS", f"{len(ns_records)} nameservers.", [zone_eid])
    else:
        ns = check(
            "Nameservers",
            "ERROR" if not ns_records else "WARNING",
            f"{len(ns_records)} nameserver(s) for {zone}.",
            [zone_eid],
            "Publish at least two nameservers, ideally on separate networks.",
            "Fewer than two nameservers",
        )
    soa = check(
        "SOA",
        {"ok": "PASS", "absent": "ERROR"}.get(soa_status, NOT_ASSESSED),
        f"SOA for {zone}: {', '.join(soa_records) or 'none'}",
        [zone_eid],
        "Restore the zone's SOA record with the DNS host.",
        "No SOA record for the zone",
    )
    if raw["A"]["status"] == "ok" or raw["AAAA"]["status"] == "ok":
        addresses = check(
            "A/AAAA",
            "PASS",
            ", ".join(raw["A"].get("records", []) + raw["AAAA"].get("records", [])),
            [eids["A"], eids["AAAA"]],
        )
    elif "error" in {raw["A"]["status"], raw["AAAA"]["status"]}:
        addresses = check("A/AAAA", NOT_ASSESSED, "lookup failed", [eids["A"]])
    else:
        addresses = check(
            "A/AAAA",
            "ERROR",
            f"{ctx.site_host} has no A or AAAA record.",
            [eids["A"], eids["AAAA"]],
            "Point the website host at its server.",
            "Website host does not resolve",
        )
    if raw["CAA"]["status"] == "error":
        caa = check("CAA", NOT_ASSESSED, "lookup failed", [eids["CAA"]])
    else:
        caa = check(
            "CAA",
            "PASS",
            ", ".join(raw["CAA"]["records"]) or "No CAA record (optional; informational).",
            [eids["CAA"]],
        )
    if checks is None:
        dnssec = check("DNSSEC", NOT_ASSESSED, "checkdmarc unavailable")
    else:
        dnssec = check(
            "DNSSEC",
            "PASS",
            "Signed."
            if checks.get("dnssec")
            else "Not signed (optional hardening; informational).",
            [dmarc_eid],
        )
    rows = {"dns": [ns, soa, addresses, caa, dnssec]}

    rows["dns_consistency"] = consistency_checks(result, domain_host, zone)

    if checks is None:
        spf = check("SPF", NOT_ASSESSED, "checkdmarc unavailable")
        dmarc = check("DMARC", NOT_ASSESSED, "checkdmarc unavailable")
    else:
        spf, dmarc = auth_checks(checks, dmarc_eid, mail)
    selectors = [
        s.strip() for s in ctx.config.get("AMS_DKIM_SELECTORS", "").split(",") if s.strip()
    ]
    rows["email_auth"] = [
        spf,
        dkim_check(result, resolver, domain_host, selectors, mail is False),
        dmarc,
    ]

    mx_eid = eids["MX"]
    if mail is None:
        rows["mail_server"] = [check("MX", NOT_ASSESSED, "MX lookup failed", [mx_eid])]
    elif null_mx:
        rows["mail_server"] = [
            check("MX", "PASS", "Null MX (0 .): domain accepts no mail.", [mx_eid])
        ]
    elif not mail:
        rows["mail_server"] = [
            check(
                "MX",
                "WARNING",
                "No MX record; mail falls back to the A record or is undeliverable.",
                [mx_eid],
                "Publish MX records, or a null MX (0 .) if the domain sends no mail.",
                "No MX record published",
                severity="low",
            )
        ]
    else:
        rows["mail_server"] = [check("MX", "PASS", ", ".join(mx_hosts), [mx_eid])]
    mx_ips: dict[str, str] = {}
    if mx_hosts:
        resolved = {h: {k: _lookup(resolver, h, k) for k in ["A", "AAAA"]} for h in mx_hosts[:5]}
        eid = result.add_evidence(f"dns:{domain_host}", "MX host address resolution", resolved)
        ok = [h for h, r in resolved.items() if any(s == "ok" for s, _ in r.values())]
        dead = [h for h, r in resolved.items() if all(s == "absent" for s, _ in r.values())]
        failed = [h for h in resolved if h not in ok and h not in dead]
        for host, lookups in resolved.items():
            for ip in lookups["A"][1][:2] if lookups["A"][0] == "ok" else []:
                if len(mx_ips) < 4:
                    mx_ips[ip] = f"MX {host}"
        if ok:
            rows["mail_server"].append(
                check("MX resolution", "PASS", f"Resolves: {', '.join(ok)}.", [eid])
            )
        if dead:
            # Mail still flows through another MX when only some hosts are broken.
            rows["mail_server"].append(
                check(
                    "MX unresolvable",
                    "WARNING" if ok else "ERROR",
                    f"No address for {', '.join(dead)}.",
                    [eid],
                    "Correct the MX hostnames or their A/AAAA records.",
                    "MX hostname does not resolve",
                    severity="medium" if ok else "high",
                )
            )
        if failed:
            rows["mail_server"].append(
                check("MX lookup", NOT_ASSESSED, f"lookup failed for {', '.join(failed)}", [eid])
            )
        if ctx.config.get("AMS_MAIL_TLS_CHECK") == "1":
            rows["mail_server"] += mail_tls_checks(result, mx_hosts)
        else:
            rows["mail_server"].append(
                check(
                    "STARTTLS / MX certificate",
                    NOT_ASSESSED,
                    "requires opt-in; contacts the customer's mail servers",
                    fix="Rerun with --mail-tls-check once the customer approves.",
                )
            )

    web_ips = {ip: "website" for ip in raw["A"].get("records", [])[:2]}
    ips = {**mx_ips, **web_ips}
    if ips:
        rows["dnsbl"] = dnsbl_checks(
            result, _resolver(), ips, ctx.config.get("SPAMHAUS_DQS_KEY", "")
        )
    else:
        rows["dnsbl"] = [check("DNSBL", NOT_ASSESSED, "no MX or website IPv4 address")]

    if rdap is None:
        rows["registration"] = [check("Expiry", NOT_ASSESSED, "RDAP unavailable")]
    else:
        rows["registration"] = [registration_check(rdap.get("events", []), rdap_eid)]

    txt = raw["TXT"]
    rows["verification"] = []
    for label, prefix in [
        ("Google", "google-site-verification="),
        ("Facebook", "facebook-domain-verification="),
    ]:
        if txt["status"] == "error":
            rows["verification"].append(check(label, NOT_ASSESSED, "TXT lookup failed"))
        elif any(prefix in r for r in txt.get("records", [])):
            rows["verification"].append(
                check(label, "PASS", f"{prefix} TXT present.", [eids["TXT"]])
            )
        else:
            rows["verification"].append(
                check(
                    label,
                    NOT_ASSESSED,
                    f"no {prefix} TXT on {domain_host}; "
                    "ownership may be verified by another method",
                    [eids["TXT"]],
                )
            )

    for key, area_checks in rows.items():
        for c in area_checks:
            title, severity, confidence = c["_finding"]
            if title and c["status"] in {"ERROR", "WARNING"} and c["evidence_ids"]:
                result.finding(
                    f"health:{key}:{c['name'].lower()}",
                    title,
                    "domain_email",
                    c["evidence_ids"],
                    c["detail"],
                    c["fix"],
                    severity=severity,
                    confidence=confidence,
                )
    result.checks = [row(key, area_checks) for key, area_checks in rows.items()]
    raw["health"] = result.checks
    raw["CNAME"] = cname_chain(resolver, ctx.site_host)
    eids["CNAME"] = result.add_evidence(
        f"dns:{ctx.site_host}", "Bounded website CNAME chain", raw["CNAME"]
    )
    raw["infrastructure"] = infrastructure(ctx, raw, eids, zone_eid, rdap, rdap_eid)
    result.add_evidence(
        ctx.target, "Infrastructure attribution", {"infrastructure": raw["infrastructure"]}
    )
    result.raw_paths = [ctx.raw(name, raw)]
    result.reason = "; ".join(limitations)
    if all(raw[k]["status"] == "error" for k in ["A", "AAAA", "MX", "NS", "TXT", "CAA"]):
        result.status = "error"
    return result


def _security_row(key: str, modules: dict[str, ModuleResult], selected: set[str]) -> dict:
    """Summarise existing Security/Observatory evidence; never re-check."""
    security = modules.get("security")
    if "security" not in selected:
        return not_assessed(key, "Security category not selected; add security to assess")
    if security is None or not security.evidence:
        reason = security.reason if security else "module did not run"
        return not_assessed(key, f"Security evidence unavailable ({reason or 'no evidence'})")
    http = next((e for e in security.evidence if "headers" in e.data), None)
    tls = next((e for e in security.evidence if "hostname_match" in e.data), None)
    keys = {f.check_key for f in security.findings}
    see = "See the Security section."
    checks = []
    if key == "web_tls":
        if http is None:
            checks.append(check("HTTP response", NOT_ASSESSED, "HTTP request failed"))
        else:
            code = http.data["status"]
            checks.append(
                check(
                    "HTTP response",
                    "PASS" if code < 400 else "WARNING",
                    f"HTTP {code} from {http.data['url']}.",
                    [http.id],
                    see,
                )
            )
        if tls is None:
            checks.append(
                check("TLS certificate", NOT_ASSESSED, security.reason or "TLS not checked")
            )
        elif "error" in tls.data:
            checks.append(
                check(
                    "TLS certificate",
                    "WARNING",
                    "Verification or connection failed (needs verification).",
                    [tls.id],
                    see,
                )
            )
        else:
            days = tls.data["days_to_expiry"]
            checks.append(
                check(
                    "TLS certificate",
                    "PASS" if days >= 30 else "WARNING",
                    f"Verified {tls.data['protocol']}; expires {tls.data['expires'][:10]} "
                    f"({days} days).",
                    [tls.id],
                    see,
                )
            )
        return row(key, checks)
    if http is None:
        checks.append(check("Headers", NOT_ASSESSED, "HTTP request failed"))
    else:
        missing = sorted(k.split(":", 1)[1] for k in keys if k.startswith("header-missing:"))
        missing += ["framing protection"] if "framing-policy" in keys else []
        checks.append(
            check(
                "Headers",
                "WARNING" if missing else "PASS",
                f"Missing: {', '.join(missing)}." if missing else "Checked headers present.",
                [http.id],
                see,
            )
        )
    observatory = modules.get("observatory")
    grade = (
        next((e for e in observatory.evidence if "grade" in e.data), None)
        if (observatory)
        else None
    )
    if grade is None:
        reason = observatory.reason if observatory else "module did not run"
        checks.append(check("Observatory", NOT_ASSESSED, reason or "no grade"))
    else:
        letter = str(grade.data["grade"])
        checks.append(
            check(
                "Observatory",
                "PASS" if letter[:1] in {"A", "B"} else "WARNING",
                f"Grade {letter}.",
                [grade.id],
                see,
            )
        )
    return row(key, checks)


def health_summary(results: list[ModuleResult], selected: set[str]) -> dict[str, Any]:
    modules = {r.name: r for r in results}
    domain = modules.get(name)
    own = {r["key"]: r for r in domain.checks} if domain else {}
    if domain is None:
        reason = "domain_email module did not run"
    else:
        reason = f"domain_email module {domain.status}" + (
            f": {domain.reason}" if domain.reason else ""
        )
    rows = []
    for key in AREAS:
        if key in {"web_tls", "http_headers"}:
            rows.append(_security_row(key, modules, selected))
        else:
            rows.append(own.get(key) or not_assessed(key, reason))
    return {"rows": rows, "limitations": [DELIVERABILITY_NOTE]}


# Hostname patterns are clues, not proof of relationships or account access.
DNS_PROVIDERS = {
    "siteground.net": "SiteGround",
    "ns.cloudflare.com": "Cloudflare",
    "domaincontrol.com": "GoDaddy",
    "googledomains.com": "Google Cloud DNS",
    "dns.google": "Google Cloud DNS",
    "azure-dns.com": "Azure DNS",
    "azure-dns.net": "Azure DNS",
    "azure-dns.org": "Azure DNS",
    "azure-dns.info": "Azure DNS",
    "awsdns-": "Amazon Route 53",
    "registrar-servers.com": "Namecheap",
}
WEB_PROVIDERS = {
    "cloudflare.net": ("Cloudflare", True),
    "cloudfront.net": ("Amazon CloudFront", True),
    "fastly.net": ("Fastly", True),
    "akamaiedge.net": ("Akamai", True),
    "akamai.net": ("Akamai", True),
    "edgekey.net": ("Akamai", True),
    "azureedge.net": ("Azure CDN", True),
    "vercel-dns.com": ("Vercel", True),
    "netlify.app": ("Netlify", True),
    "wpengine.com": ("WP Engine", False),
    "sgvps.net": ("SiteGround", False),
    "web-hosting.com": ("Namecheap", False),
}
MAIL_PROVIDERS = {
    "google.com": ("Google Workspace", "mailbox routing"),
    "googlemail.com": ("Google Workspace", "mailbox routing"),
    "protection.outlook.com": ("Microsoft 365", "mailbox routing"),
    "mimecast.com": ("Mimecast", "gateway"),
    "pphosted.com": ("Proofpoint", "gateway"),
    "ppe-hosted.com": ("Proofpoint", "gateway"),
}


def hostname_matches(host: str, suffix: str) -> bool:
    host = host.lower().rstrip(".")
    if suffix == "awsdns-":
        import re

        return bool(re.fullmatch(r"ns-\d+\.awsdns-\d+\.(com|net|org|co\.uk)", host))
    return host == suffix or host.endswith("." + suffix)


def cname_chain(resolver: dns.resolver.Resolver, host: str) -> dict[str, Any]:
    chain, seen = [], {host.lower().rstrip(".")}
    for _ in range(8):
        status, records = _lookup(resolver, host, "CNAME")
        if status != "ok":
            return {"chain": chain, "status": status, "terminal_host": host}
        target = records[0].lower().rstrip(".")
        chain.append({"host": host, "target": target})
        if target in seen:
            return {"chain": chain, "status": "loop", "terminal_host": target}
        seen.add(target)
        host = target
    return {"chain": chain, "status": "limit", "terminal_host": host}


def rdap_privacy_redacted(rdap: dict[str, Any]) -> bool:
    """Look for actual entity/contact redaction markers, not generic privacy notices."""

    def marker(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().casefold() in {
                "redacted",
                "redacted for privacy",
                "data redacted",
                "data protected",
                "not disclosed",
            }
        if isinstance(value, dict):
            return any(marker(v) for v in value.values())
        if isinstance(value, list):
            return any(marker(v) for v in value)
        return False

    return bool(rdap.get("redacted")) or marker(rdap.get("entities", []))


def infrastructure(
    ctx: Context,
    raw: dict[str, Any],
    eids: dict[str, str],
    zone_eid: str,
    rdap: dict[str, Any] | None,
    rdap_eid: str,
) -> dict[str, Any]:
    """Public attribution with explicit claim labels; no new HTTP or paid lookups."""

    def claim(label, value, detail, ids):
        return {
            "label": label,
            "value": value,
            "detail": detail,
            "evidence_ids": [eid for eid in ids if eid],
        }

    registrars = []
    for entity in (rdap or {}).get("entities", []):
        if "registrar" not in entity.get("roles", []):
            continue
        vcard = entity.get("vcardArray", [])
        properties = vcard[1] if len(vcard) == 2 else []
        names = [p[3] for p in properties if len(p) >= 4 and p[0] in {"fn", "org"}]
        registrars.append(
            {
                "names": names,
                "handle": entity.get("handle"),
                "public_ids": entity.get("publicIds", []),
            }
        )
    ns_status, nameservers = raw["zone"]["NS"]
    nameservers = nameservers if ns_status == "ok" else []
    dns_providers = sorted(
        {
            provider
            for host in nameservers
            for pattern, provider in DNS_PROVIDERS.items()
            if hostname_matches(host, pattern)
        }
    )
    http, tls, reused = [], [], []
    for module in ctx.results:
        for evidence in module.evidence:
            data = evidence.data
            if not isinstance(data, dict) or urlsplit(evidence.url).hostname != ctx.site_host:
                continue
            if "headers" in data:
                headers = {
                    str(k).lower(): v
                    for k, v in data["headers"].items()
                    if str(k).lower()
                    in {"server", "via", "cf-ray", "x-served-by", "x-cache", "x-vercel-id"}
                    or str(k).lower().startswith("x-sg-")
                }
                http.append({"url": evidence.url, "status": data.get("status"), "headers": headers})
                reused.append(evidence.id)
            if "hostname_match" in data:
                tls.append(
                    {
                        k: data[k]
                        for k in ["issuer", "protocol", "hostname_match", "error"]
                        if k in data
                    }
                )
                reused.append(evidence.id)
    hosts = [ctx.site_host, *(link["target"] for link in raw["CNAME"]["chain"])]
    providers: dict[str, dict[str, Any]] = {}

    def provider(label, cdn, signal):
        entry = providers.setdefault(label, {"provider": label, "cdn": cdn, "signals": []})
        entry["cdn"] = entry["cdn"] or cdn
        entry["signals"].append(signal)

    for host in hosts:
        for pattern, (label, cdn) in WEB_PROVIDERS.items():
            if hostname_matches(host, pattern):
                provider(label, cdn, f"Hostname: {host}")
    for response in http:
        headers = response["headers"]
        server, via = str(headers.get("server", "")).lower(), str(headers.get("via", "")).lower()
        if "cf-ray" in headers or server == "cloudflare":
            provider("Cloudflare", True, "HTTP server/cf-ray header")
        if "cloudfront" in via or "cloudfront" in str(headers.get("x-cache", "")).lower():
            provider("Amazon CloudFront", True, "HTTP via/x-cache header")
        if "x-vercel-id" in headers:
            provider("Vercel", True, "HTTP x-vercel-id header")
        if any(k.startswith("x-sg-") for k in headers):
            provider(
                "SiteGround",
                headers.get("x-sg-cdn") in {"1", "true", "enabled"},
                "HTTP x-sg-* header",
            )
    for entry in providers.values():
        entry["role"] = (
            "platform/CDN (may also host the site)"
            if entry["provider"] in {"Vercel", "Netlify"}
            else "CDN; origin host unknown"
            if entry["cdn"]
            else "possible hosting provider"
        )
    cdn = any(p["cdn"] for p in providers.values())
    platforms = [
        p["provider"] for p in providers.values() if p["provider"] in {"Vercel", "Netlify"}
    ]
    endpoints = {
        "host": ctx.site_host,
        "A": raw["A"],
        "AAAA": raw["AAAA"],
        "cname": raw["CNAME"],
        "http": http,
        "tls": tls,
    }
    mx = raw["MX"].get("records", [])
    routes: dict[str, dict[str, Any]] = {}
    for record in mx:
        host = record.split()[-1].rstrip(".").lower()
        for pattern, (label, role) in MAIL_PROVIDERS.items():
            if hostname_matches(host, pattern):
                route = routes.setdefault(
                    label,
                    {
                        "provider": label,
                        "role": role,
                        "mx": [],
                        "detail": "gateway; underlying mailbox provider unknown"
                        if role == "gateway"
                        else "MX indicates inbound routing only",
                    },
                )
                if host not in route["mx"]:
                    route["mx"].append(host)
    web_ids = [eids["A"], eids["AAAA"], eids["CNAME"], *reused]
    return {
        "schema_version": 1,
        "target": ctx.target,
        "registrar": claim(
            "OBSERVED" if registrars else NOT_ASSESSED,
            registrars,
            (
                "RDAP registrar of record; reseller/white-label supplier may differ. "
                if registrars
                else "RDAP registrar unavailable; registrar unknown. "
            )
            + (
                "Privacy redaction observed in RDAP entity/contact records. "
                if rdap is not None and rdap_privacy_redacted(rdap)
                else "Privacy redaction may hide registrant details. "
            )
            + "RDAP does not prove account ownership.",
            [rdap_eid],
        ),
        "nameservers": claim(
            "OBSERVED", nameservers, f"Authoritative zone NS lookup: {ns_status}", [zone_eid]
        ),
        "dns_provider": claim(
            "INFERRED",
            dns_providers,
            "NS hostname patterns; provider unknown when no pattern matches.",
            [zone_eid],
        ),
        "website_endpoints": claim(
            "OBSERVED",
            endpoints,
            "DNS endpoints and reused HTTP/TLS evidence. Missing HTTP/TLS "
            "means not collected; certificate issuer is not the host.",
            web_ids,
        ),
        "website_provider": claim(
            "INFERRED",
            list(providers.values()),
            "Provider clues from CNAME/HTTP, not proof of an account or owner.",
            web_ids,
        ),
        "origin_host": claim(
            "INFERRED",
            None,
            ", ".join(platforms) + ": platform/CDN (may also host the site); "
            "origin hosting arrangement requires customer confirmation."
            if platforms
            else "CDN in front indicated; origin host unknown."
            if cdn
            else "Origin hosting provider requires customer confirmation.",
            web_ids,
        ),
        "email_routing": claim(
            "OBSERVED",
            raw["MX"],
            "Published MX records; null MX declares no inbound mail.",
            [eids["MX"]],
        ),
        "email_provider": claim(
            "INFERRED",
            list(routes.values()),
            "MX routing is not a full software inventory. Google Workspace and "
            "Microsoft 365 can coexist; absence here does not exclude either. "
            "Gateway routes leave the underlying mailbox provider unknown.",
            [eids["MX"]],
        ),
        "account_ownership": claim(
            "INFERRED",
            "customer confirmation required",
            "Account ownership and administrative access require recorded "
            "customer confirmation; public records cannot establish them.",
            [],
        ),
    }
