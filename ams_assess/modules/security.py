import re
import socket
import ssl
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

import httpx

from ams_assess.models import Context, ModuleResult

name = "security"
categories = {"security"}
requires = ["HTTP", "TLS for HTTPS targets"]
HEADERS = {
    "content-security-policy": "Constrain resource sources with a tested Content Security Policy.",
    "strict-transport-security": "Enable HSTS after confirming all required hosts support HTTPS.",
    "x-content-type-options": "Set X-Content-Type-Options to nosniff.",
    "referrer-policy": "Choose a privacy-conscious Referrer-Policy.",
    "permissions-policy": "Limit browser capabilities to those the website actually uses.",
}


def run(ctx: Context) -> ModuleResult:
    result, raw, limitations = ModuleResult(name=name), {}, []
    try:
        response = httpx.get(ctx.site_url, timeout=15, follow_redirects=True)
        # Cookies and auth headers never belong in report evidence.
        headers = {
            k: v for k, v in response.headers.items() if k not in {"set-cookie", "www-authenticate"}
        }
        raw["http"] = {"url": str(response.url), "status": response.status_code, "headers": headers}
        eid = result.add_evidence(str(response.url), "HTTP response security headers", raw["http"])
        if response.status_code >= 400:
            limitations.append(
                f"Header observations are from an HTTP {response.status_code} response"
            )
        for header, recommendation in HEADERS.items():
            if header == "strict-transport-security" and response.url.scheme != "https":
                continue
            if header not in headers:
                result.finding(
                    f"header-missing:{header}",
                    f"Missing {header} header",
                    "security",
                    [eid],
                    f"The sampled response did not include {header}.",
                    recommendation,
                    severity="low"
                    if ctx.local or header in {"referrer-policy", "permissions-policy"}
                    else "medium",
                    urls=[str(response.url)],
                )
        if (
            "x-frame-options" not in headers
            and "frame-ancestors" not in headers.get("content-security-policy", "").lower()
        ):
            result.finding(
                "framing-policy",
                "No explicit framing protection observed",
                "security",
                [eid],
                "Neither X-Frame-Options nor CSP frame-ancestors was observed.",
                "Set an appropriate frame-ancestors policy after checking embedding needs.",
                severity="low" if ctx.local else "medium",
                urls=[str(response.url)],
            )
        # Only specific cookie flag observations are actionable; never retain cookie values.
        cookie_issues = []
        for value in response.headers.get_list("set-cookie"):
            cookie = SimpleCookie()
            try:
                cookie.load(value)
            except CookieError:
                continue
            for key, morsel in cookie.items():
                missing = [
                    flag
                    for flag in ("Secure", "HttpOnly")
                    if not morsel[flag.lower()]
                    and (flag != "Secure" or response.url.scheme == "https")
                ]
                if missing:
                    cookie_issues.append({"name": key, "missing_flags": missing})
        if cookie_issues:
            cookie_eid = result.add_evidence(
                str(response.url), "Cookie flag observations (values omitted)", cookie_issues
            )
            result.finding(
                "header-review:set-cookie",
                "Review specific cookie protection flags",
                "security",
                [cookie_eid],
                "; ".join(
                    f"Cookie {c['name']} lacks {', '.join(c['missing_flags'])}"
                    for c in cookie_issues
                ),
                "Confirm each cookie's purpose. Set Secure on HTTPS cookies and HttpOnly where "
                "client-side JavaScript does not need access; test before deployment.",
                severity="low",
                confidence="needs_verification",
                urls=[str(response.url)],
            )
        server = headers.get("server", "")
        if re.search(r"\b[A-Za-z][\w.-]*/\d+\.\d+[\w.-]*", server):
            result.finding(
                "header-review:server",
                "Server software version disclosed",
                "security",
                [eid],
                f"Server response header discloses a version: {server}.",
                "Remove unnecessary version detail in the hosting configuration; "
                "a version string alone does not establish a vulnerability.",
                severity="low",
                urls=[str(response.url)],
            )
    except httpx.HTTPError as exc:
        limitations.append(f"HTTP headers unavailable ({type(exc).__name__})")
    parts = urlsplit(ctx.site_url)
    if parts.scheme == "https":
        try:
            with socket.create_connection((ctx.site_host, parts.port or 443), timeout=8) as sock:
                with ssl.create_default_context().wrap_socket(
                    sock, server_hostname=ctx.site_host
                ) as tls:
                    cert = tls.getpeercert()
                    if not cert or not isinstance(cert.get("notAfter"), str):
                        raise ValueError("Missing certificate expiry")
                    expiry = datetime.fromtimestamp(
                        ssl.cert_time_to_seconds(str(cert["notAfter"])), UTC
                    )
                    raw["tls"] = {
                        "issuer": cert["issuer"],
                        "expires": expiry.isoformat(),
                        "days_to_expiry": (expiry - datetime.now(UTC)).days,
                        "protocol": tls.version(),
                        "hostname_match": True,
                    }
                    eid = result.add_evidence(ctx.site_url, "Verified TLS certificate", raw["tls"])
                    if raw["tls"]["days_to_expiry"] < 30:
                        result.finding(
                            "tls-expiry",
                            "TLS certificate expires within 30 days",
                            "security",
                            [eid],
                            f"Expires {expiry.date()}.",
                            "Confirm automated renewal and expiry alerts.",
                            severity="high",
                        )
        except (OSError, ValueError) as exc:
            raw["tls"] = {"error": type(exc).__name__, "hostname_match": None}
            eid = result.add_evidence(
                ctx.site_url, "TLS verification could not complete", raw["tls"]
            )
            limitations.append(f"TLS verification unavailable ({type(exc).__name__})")
            result.finding(
                "tls-unverified",
                "TLS verification needs investigation",
                "security",
                [eid],
                "Certificate verification or connection failed from the assessment host.",
                "Recheck certificate chain, hostname and network access independently.",
                confidence="needs_verification",
                severity="high",
            )
    else:
        limitations.append("TLS not checked: supplied target uses HTTP")
    if ctx.local:
        limitations.append(
            "Local fixture security headers are not evidence about production hosting"
        )
    result.reason = "; ".join(limitations)
    result.raw_paths = [ctx.raw(name, raw)]
    if "http" not in raw and "tls" not in raw:
        result.status = "error"
    return result
