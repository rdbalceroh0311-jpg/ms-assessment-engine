import socket
import ssl
from datetime import UTC, datetime
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
}


def run(ctx: Context) -> ModuleResult:
    result, raw, limitations = ModuleResult(name=name), {}, []
    try:
        response = httpx.get(ctx.target, timeout=15, follow_redirects=True)
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
                    severity="low" if ctx.local else "medium",
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
            )
    except httpx.HTTPError as exc:
        limitations.append(f"HTTP headers unavailable ({type(exc).__name__})")
    parts = urlsplit(ctx.target)
    if parts.scheme == "https":
        try:
            with socket.create_connection((ctx.host, parts.port or 443), timeout=8) as sock:
                with ssl.create_default_context().wrap_socket(
                    sock, server_hostname=ctx.host
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
                    eid = result.add_evidence(ctx.target, "Verified TLS certificate", raw["tls"])
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
            eid = result.add_evidence(ctx.target, "TLS verification could not complete", raw["tls"])
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
