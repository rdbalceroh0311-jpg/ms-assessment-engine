import json
import subprocess
import sys

import dns.exception
import dns.resolver
import httpx

from ams_assess.models import Context, ModuleResult

name = "domain_email"
categories = {"domain_email"}
requires = ["public DNS", "RDAP"]


def run(ctx: Context) -> ModuleResult:
    if ctx.local:
        return ModuleResult(
            name=name, status="skipped", reason="Local/private target has no public domain audit"
        )
    result = ModuleResult(name=name)
    raw, limitations = {}, []
    resolver = dns.resolver.Resolver()
    resolver.timeout, resolver.lifetime = 2, 4
    for kind in ["A", "AAAA", "MX", "NS", "TXT", "CAA"]:
        try:
            records = [r.to_text() for r in resolver.resolve(ctx.host, kind)]
            raw[kind] = {"status": "ok", "records": records}
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN) as exc:
            raw[kind] = {"status": "absent", "reason": type(exc).__name__, "records": []}
        except dns.exception.DNSException as exc:
            raw[kind] = {"status": "error", "reason": type(exc).__name__}
            limitations.append(f"{kind} lookup failed ({type(exc).__name__})")
        result.add_evidence(ctx.target, f"DNS {kind} lookup for {ctx.host}", raw[kind])
    # Bound checkdmarc's entire operation as well as each DNS lookup. Never probe SMTP.
    code = (
        "import checkdmarc,json,sys; print(json.dumps(checkdmarc.check_domains("
        "[sys.argv[1]],skip_tls=True,timeout=2,retries=0),default=str))"
    )
    try:
        process = subprocess.run(
            [sys.executable, "-c", code, ctx.host],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        checks = json.loads(process.stdout)
        raw["checkdmarc"] = checks
        eid = result.add_evidence(ctx.target, "checkdmarc SPF, DMARC, MX and DNSSEC checks", checks)
        for key in ["spf", "dmarc"]:
            check = checks.get(key, {})
            if check.get("valid") is False:
                detail = check.get("error", "Record did not validate")
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
    try:
        response = httpx.get(
            f"https://rdap.org/domain/{ctx.host}", timeout=15, follow_redirects=True
        )
        response.raise_for_status()
        rdap = response.json()
        raw["rdap"] = rdap
        summary = {
            "domain": rdap.get("ldhName"),
            "events": rdap.get("events", []),
            "registrar": [e for e in rdap.get("entities", []) if "registrar" in e.get("roles", [])],
        }
        result.add_evidence(ctx.target, "RDAP registrar and registration/expiry events", summary)
    except (httpx.HTTPError, ValueError) as exc:
        limitations.append(
            f"RDAP unavailable ({type(exc).__name__}); may require registrable domain"
        )
    result.raw_paths = [ctx.raw(name, raw)]
    result.reason = "; ".join(limitations)
    if all(raw[k]["status"] == "error" for k in ["A", "AAAA", "MX", "NS", "TXT", "CAA"]):
        result.status = "error"
    return result
