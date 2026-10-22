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

from pydantic import BaseModel, Field

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
    ams_service: str = "Website improvement"
    # Derived worst-of from cited evidence by consolidate(); never trusted from AI output.
    provenance: Provenance = "technical_evidence"


class ModuleResult(BaseModel):
    name: str
    status: Literal["ok", "skipped", "error"] = "ok"
    reason: str = ""
    raw_paths: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    # Optional structured per-area check rows (domain_email health summary).
    checks: list[dict[str, Any]] = Field(default_factory=list)

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
    (result / "raw").mkdir()
    (result / "screenshots").mkdir()
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
    # Set (with the reason) once any call is charged above its estimate: stops every later call.
    dataforseo_stopped: str = ""
    # Results of modules that already ran, so later modules reuse evidence instead of refetching.
    results: list[ModuleResult] = field(default_factory=list)

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
