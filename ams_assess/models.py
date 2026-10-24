from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, model_validator

# Report order: website and technical findings, then local SEO and digital presence.
CATEGORIES = {
    "technical": "Website technical health",
    "seo": "SEO findings",
    "geo": "GEO / AI search readiness",
    "performance": "Website performance",
    "accessibility": "Functionality & accessibility",
    "cro_ux": "CRO & UX/UI",
    "copywriting": "Copywriting & messaging",
    "security": "Security observations",
    "domain_email": "Domain, DNS & email health",
    "local_seo": "Local SEO",
    "entity": "Entity & brand authority",
    "presence": "Digital presence & integration readiness",
}
SEVERITIES = {v: i for i, v in enumerate(("critical", "high", "medium", "low", "info"))}
# Business discovery findings are not a crawler category: they come from customer statements.
DISCOVERY_CATEGORY = "discovery"
AMS_SERVICES = (
    "AI & Automation",
    "Business Systems",
    "Digital Growth",
    "Managed IT & Technology",
)
AMSService = Literal[
    "AI & Automation", "Business Systems", "Digital Growth", "Managed IT & Technology"
]
SERVICE_BY_CATEGORY = {
    **dict.fromkeys(CATEGORIES, "Digital Growth"),
    "security": "Managed IT & Technology",
    "domain_email": "Managed IT & Technology",
    "technical": "Managed IT & Technology",
    "discovery": "Business Systems",
}
# Potential consequences, not measured losses or promised outcomes. Keys match the
# deterministic collectors; uncatalogued checks retain the category-level wording.
CHECK_IMPACTS = {
    "unavailable-urls": "Customers may be unable to reach services or complete an enquiry.",
    "broken-links": "Broken links may prevent customers reaching the information they need.",
    "tls-expiry": "An expired certificate may show browser warnings and interrupt enquiries.",
    "tls-unverified": "Certificate problems may prevent secure access to the website.",
    "health:registration:expiry": "Lapsed registration may interrupt the website and email.",
    "health:email_auth:spf": "Unauthorised senders may impersonate the business by email.",
    "health:mail_server:mx unresolvable": "Customers' email may not reach the business.",
    "axe:color-contrast": "Low contrast may make service information or controls hard to read.",
    "axe:image-alt": "Customers using screen readers may miss information conveyed by images.",
    "axe:label": "Unlabelled fields may prevent customers using assistive tools from enquiring.",
    "axe:button-name": "Unnamed buttons may make actions unclear to screen-reader users.",
    "axe:link-name": "Unnamed links may make destinations unclear to screen-reader users.",
    "site:robots": "Blocked search crawlers may leave public pages out of search results.",
    "site:noindex": "Customers may not find affected pages through search results.",
    "site:structured-data": "Invalid business markup may obscure business details in search.",
    **dict.fromkeys(
        (
            "health:dnsbl:bl.spamcop.net",
            "health:dnsbl:b.barracudacentral.org",
            "health:dnsbl:psbl.surriel.com",
            "health:dnsbl:spamhaus zen (dqs)",
        ),
        "Mail from listed sending addresses may be rejected or filtered as spam.",
    ),
}
ConnectionStatus = Literal[
    "Connected successfully", "Permission required", "Not connected", "Unsupported", "Not assessed"
]


class Connection(BaseModel):
    key: str
    service: str
    status: ConnectionStatus
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)


Provenance = Literal[
    "technical_evidence", "manual_verification", "customer_statement", "assumption"
]
# Lower is stronger; a finding is only as strong as its weakest cited evidence. A manual
# verification is a person's observation of a public profile: never technical evidence.
PROVENANCE_RANK: dict[str, int] = {
    "technical_evidence": 0,
    "manual_verification": 1,
    "customer_statement": 2,
    "assumption": 3,
}
_STRONGEST = "technical_evidence"
CUSTOMER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def worst_provenance(values: Iterable[str]) -> Provenance:
    return cast(Provenance, max(values, key=PROVENANCE_RANK.__getitem__, default=_STRONGEST))


class Evidence(BaseModel):
    id: str
    source: str
    url: str
    description: str
    data: Any
    provenance: Provenance = "technical_evidence"


class CheckOutcome(BaseModel):
    module: str
    check_id: str
    category: str
    site: str
    component: str
    status: Literal["pass", "fail", "skipped", "failed-to-run", "unavailable", "out-of-scope"]
    coverage: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class Finding(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str
    category: str
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"
    kind: Literal["problem", "opportunity"] = "problem"
    confidence: Literal["confirmed", "needs_verification", "subjective"] = "confirmed"
    evidence_ids: list[str] = Field(min_length=1)
    detail: str
    recommendation: str
    check_key: str = ""
    urls: list[str] = Field(default_factory=list)
    likely_cause: str = ""
    business_impact: str = ""
    priority: Literal["P1", "P2", "P3"] = "P2"
    ams_service: AMSService = "Digital Growth"
    urgency: str = ""
    dependencies: list[str] = Field(
        default_factory=lambda: [
            "Customer approval of scope and an accountable system owner before changes"
        ]
    )
    information_required: list[str] = Field(
        default_factory=lambda: [
            "Confirm the current setup, existing integrations, access and business priority"
        ]
    )
    site: str = ""
    component: str = ""
    coverage: list[str] = Field(default_factory=list)
    lifecycle_status: Literal["current", "not_rechecked", "resolved", "needs_review"] = "current"
    first_observed: str = ""
    last_observed: str = ""
    last_run_id: str = ""
    resolution: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    # Derived worst-of from cited evidence by consolidate(); never trusted from AI output.
    provenance: Provenance = "technical_evidence"

    @model_validator(mode="before")
    @classmethod
    def material_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        # Older runs allowed arbitrary AI-written service names. Normalise every legacy
        # value on read; validate_analysis separately rejects invalid *new* AI output.
        if value.get("ams_service") not in AMS_SERVICES:
            value["ams_service"] = SERVICE_BY_CATEGORY.get(
                value.get("category"), "Business Systems"
            )
        if not value.get("urgency"):
            value["urgency"] = {
                "P1": "Review within 0-30 days",
                "P2": "Plan within 30-90 days",
                "P3": "Consider after 90 days",
            }.get(value.get("priority", "P2"), "Confirm urgency with the customer")
        if not value.get("business_impact"):
            specific = (
                CHECK_IMPACTS.get(value.get("check_key", ""))
                if value.get("severity") in {"critical", "high"}
                else None
            )
            value["business_impact"] = specific or (
                {
                    "Managed IT & Technology": (
                        "May affect reliability or protection of business systems; "
                        "impact requires verification."
                    ),
                    "Business Systems": (
                        "May affect staff hand-offs or data consistency; "
                        "impact requires verification."
                    ),
                    "AI & Automation": (
                        "May reduce repetitive work if the process is suitable; "
                        "benefits are not measured."
                    ),
                    "Digital Growth": (
                        "May affect how customers find, understand or use the business online; "
                        "outcomes are not measured."
                    ),
                }[value.get("ams_service", "Digital Growth")]
                if value.get("ams_service", "Digital Growth") in AMS_SERVICES
                else "Impact requires verification."
            )
        return value

    @property
    def basis(self) -> str:
        if self.provenance == "customer_statement":
            return "Reported by customer"
        if self.provenance == "manual_verification":
            return "Manually verified"
        if self.provenance == "technical_evidence" and self.confidence == "confirmed":
            return "Confirmed (technical evidence)"
        return "Inferred (heuristic or AI, needs verification)"


class ModuleResult(BaseModel):
    name: str
    status: Literal["ok", "skipped", "error"] = "ok"
    reason: str = ""
    raw_paths: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    outcomes: list[CheckOutcome] = Field(default_factory=list)
    collection_started: bool = False
    # Optional structured per-area check rows (domain_email health summary).
    checks: list[dict[str, Any]] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)

    def add_evidence(self, url: str, description: str, data: Any) -> str:
        eid = f"ev-{self.name}-{len(self.evidence) + 1:03d}"
        self.evidence.append(
            Evidence(id=eid, source=self.name, url=url, description=description, data=data)
        )
        return eid

    def finding(
        self,
        key: str,
        title: str,
        category: str,
        evidence_ids: list[str],
        detail: str,
        recommendation: str,
        **kwargs: Any,
    ) -> None:
        severity = kwargs.get("severity", "medium")
        kwargs.setdefault(
            "priority",
            "P1" if severity in {"critical", "high"} else ("P2" if severity == "medium" else "P3"),
        )
        self.findings.append(
            Finding(
                id=f"{self.name}-{len(self.findings) + 1:03d}",
                check_key=key,
                title=title,
                category=category,
                evidence_ids=evidence_ids,
                detail=detail,
                recommendation=recommendation,
                **kwargs,
            )
        )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
    path.chmod(0o600)


def normalize_target(value: str) -> str:
    parts = urlsplit(value if "://" in value else f"https://{value}")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Target must be an HTTP(S) URL or domain")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Target must not include credentials, query parameters or a fragment")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if not re.fullmatch(r"[a-z0-9.:-]+", host):
        raise ValueError("Invalid target hostname")
    netloc = f"[{host}]" if ":" in host else host
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))


def is_local(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".local", ".test")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return "." not in host


def customer_path(root: Path, customer: str) -> Path:
    """The only way to address a customer's directory: validated slug, never a symlink."""
    if not CUSTOMER_RE.fullmatch(customer):
        raise ValueError("Customer must be a lowercase slug: letters, numbers and hyphens (1-63)")
    result = root.resolve() / customer
    if result.is_symlink():
        raise ValueError("Customer directory must not be a symlink")
    return result


def create_run_dir(root: Path, customer: str) -> Path:
    customer_dir = customer_path(root, customer)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    result = customer_dir / stamp
    result.mkdir(parents=True, mode=0o700)
    (result / "raw").mkdir(mode=0o700)
    (result / "screenshots").mkdir(mode=0o700)
    return result


@dataclass
class Context:
    customer_id: str
    customer_name: str
    target: str
    categories: set[str]
    connections: set[str]
    config: dict[str, str]
    run_dir: Path
    tools_dir: Path
    max_pages: int = 100
    browser_pages: int = 6
    wp_inventory: Path | None = None
    dataforseo_max_usd: float | None = None
    # Root of data/customers; when set, run() also merges the run into the customer store.
    store_root: Path | None = None
    pages: list[str] = field(default_factory=list)
    # Customer business profile from the store (customer-supplied values), if one exists.
    profile: dict[str, Any] | None = None
    # Optional --keywords for point-in-time organic position samples (paid, same cap).
    keywords: list[str] = field(default_factory=list)
    # Shared DataForSEO ledger for this run: every paid call books against the one cap.
    dataforseo_spent_usd: Decimal = Decimal(0)
    dataforseo_serp_calls: int = 0
    # Shared stop for definitive rejection or any charge above its estimate.
    dataforseo_stopped: str = ""
    dataforseo_failure: dict[str, Any] = field(default_factory=dict)
    # Results of modules that already ran, so later modules reuse evidence instead of refetching.
    results: list[ModuleResult] = field(default_factory=list)
    # Pre-crawl robots/sitemap/WordPress inventory and crawl seeds (coverage.seed_inventory).
    inventory: dict[str, Any] = field(default_factory=dict)
    # Website redirects must never change the requested domain/store identity.
    resolved_url: str = ""
    connector_results: dict[str, Any] = field(default_factory=dict)

    @property
    def site_url(self) -> str:
        return self.resolved_url or self.target

    @site_url.setter
    def site_url(self, value: str) -> None:
        self.resolved_url = value

    @property
    def site_host(self) -> str:
        return urlsplit(self.site_url).hostname or ""

    @property
    def host(self) -> str:
        return urlsplit(self.target).hostname or ""

    @property
    def local(self) -> bool:
        return is_local(self.host)

    def raw(self, module: str, data: Any) -> str:
        rel = f"raw/{module}.json"
        write_json(self.run_dir / rel, data)
        return rel
