"""Provider-independent connector contract. No persistence or provider SDKs here."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ams_assess.models import Evidence, ModuleResult, Provenance

Status = Literal[
    "not_configured",
    "credential_missing",
    "credential_expired",
    "permission_required",
    "licence_required",
    "rate_limited",
    "not_found",
    "provider_unavailable",
    "api_not_enabled",
    "approval_required",
    "failed",
    "connected",
    "connected_partial",
]
AuthKind = Literal["google_oauth", "ms_oauth/app", "api_key", "ssh"]


class ConnectorError(Exception):
    """Only fixed, public messages belong here; never provider exception text."""

    def __init__(self, status: Status, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ErrorClassification:
    status: Status
    detail: str = ""

    @property
    def retryable(self) -> bool:
        return self.status in {"rate_limited", "provider_unavailable"}


def readonly(value: Any) -> Any:
    """Detached, recursively immutable JSON view; preserve evidence provenance."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: readonly(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(readonly(item) for item in value)
    return value


@dataclass(frozen=True)
class EvidenceView:
    id: str
    source: str
    url: str
    description: str
    data: Any
    provenance: Provenance

    @classmethod
    def of(cls, evidence: Evidence) -> EvidenceView:
        return cls(**{**evidence.model_dump(), "data": readonly(evidence.data)})


@dataclass(frozen=True)
class ReadOperation:
    method: str
    url_pattern: str

    def allows(self, method: str, url: str) -> bool:
        return method == self.method and re.fullmatch(self.url_pattern, url) is not None


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    automatic: bool = False

    @classmethod
    def make(cls, days: int = 28, start: str = "", end: str = "", zone: str = "UTC") -> Period:
        today = datetime.now(ZoneInfo(zone)).date()
        if not 1 <= days <= 366 or bool(start) != bool(end):
            raise ValueError("Use 1-366 days or both --start and --end")
        last = date.fromisoformat(end) if end else today - timedelta(days=1)
        first = date.fromisoformat(start) if start else last - timedelta(days=days - 1)
        if not 0 <= (last - first).days < 366 or last >= today:
            raise ValueError("Period must contain 1-366 complete days")
        return cls(first, last, not start)

    def json(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass
class Endpoint:
    key: str
    endpoint: str
    status: Status = "connected"
    data: dict[str, Any] = field(default_factory=dict)
    rows: int = 0
    truncated: bool = False
    detail: str = ""
    collected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    evidence_id: str = ""

    @property
    def complete(self) -> bool:
        return self.status == "connected" and not self.truncated


@dataclass
class AuthMaterial:
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    secrets: tuple[str, ...] = field(default_factory=tuple, repr=False)
    private_data: Any = field(default=None, repr=False)


@dataclass
class ConnectorContext:
    # HTTP is intentionally an interface: all provider requests use ReadClient.request().
    http: Any
    resource: str
    period: Period
    customer: str = ""
    websites: list[str] = field(default_factory=list)
    profile: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    profile_evidence_id: str = ""
    profile_evidence: EvidenceView | None = None
    public_evidence: tuple[EvidenceView, ...] = ()
    public_findings: list[dict[str, Any]] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    max_pages: int = 5
    page_size: int = 100
    top_n: int = 100

    @property
    def public_evidence_refs(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.public_evidence)

    def capture(self, key: str, endpoint: str, read: Callable[[Endpoint], None]) -> Endpoint:
        result = Endpoint(key=key, endpoint=endpoint)
        self.endpoints.append(result)
        try:
            read(result)
        except ConnectorError as exc:
            result.status, result.detail = exc.status, exc.detail
            result.truncated |= result.rows > 0
        except Exception:
            result.status, result.detail = "failed", "Response could not be processed"
            result.truncated |= result.rows > 0
        return result


@dataclass(frozen=True)
class Connector:
    key: str
    label: str
    provider: str
    auth_kind: AuthKind
    scopes: tuple[str, ...]
    resource_type: str
    read_allowlist: tuple[ReadOperation, ...]
    normalize_resource: Callable[[str], str]
    list_resources: Callable[[ConnectorContext], Endpoint]
    test: Callable[[ConnectorContext], Endpoint]
    collect: Callable[[ConnectorContext], None]
    analysers: tuple[Callable[[ConnectorContext, ModuleResult], None], ...]
    # Providers may supply module-local auth adapters; Google is the default implementation.
    authenticate: Callable[..., AuthMaterial]
    authorize: Callable[..., None] | None = None
    categories: frozenset[str] = frozenset({"cro_ux"})
    # An optional module-owned transport can map allowlisted read routes to SSH/SDK operations.
    transport_factory: Callable[[AuthMaterial], Any] | None = None

    # Return None to use the default classifier. Only transient statuses are retried.
    classify_error: Callable[[int, Mapping[str, Any]], ErrorClassification | None] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", self.key):
            raise ValueError("Connector key must be a short lowercase module identifier")
        if not self.read_allowlist or any(
            op.method not in {"GET", "POST"} for op in self.read_allowlist
        ):
            raise ValueError("Declare explicit GET/read-only POST operations")

    @property
    def auth_command(self) -> str:
        return (
            f"ams-assess connect {self.key} auth --credential-ref <name> "
            "--client-secrets <desktop-client.json>"
        )
