from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

CATEGORIES = {
    "technical": "Website technical health",
    "seo": "SEO findings",
    "geo": "GEO / AI search readiness",
    "local_seo": "Local SEO",
    "entity": "Entity & brand authority",
    "performance": "Website performance",
    "accessibility": "Functionality & accessibility",
    "cro_ux": "CRO & UX/UI",
    "copywriting": "Copywriting & messaging",
    "domain_email": "Domain, DNS & email health",
    "security": "Security observations",
}
SEVERITIES = {v: i for i, v in enumerate(("critical", "high", "medium", "low", "info"))}


class Evidence(BaseModel):
    id: str
    source: str
    url: str
    description: str
    data: Any


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


def create_run_dir(root: Path, customer: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", customer):
        raise ValueError("Customer must be a lowercase slug: letters, numbers and hyphens (1-63)")
    root = root.resolve()
    customer_dir = root / customer
    if customer_dir.is_symlink():
        raise ValueError("Customer directory must not be a symlink")
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
    pages: list[str] = field(default_factory=list)

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
