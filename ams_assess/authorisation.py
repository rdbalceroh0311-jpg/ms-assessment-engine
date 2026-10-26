"""Explicit per-customer authorisation before any customer network or credential use.

A customer's profile records who authorised the assessment, when, and its scope. Every
assessment run and connected read of a customer checks that record first, and the CLI
also requires an explicit confirmation flag. No environment or customer setting exempts a
customer: tests may monkeypatch the private exact sets below for their synthetic customers,
as they do for network._fixture_origins, and a run against an exact fixture origin is a
fixture run.
"""

import datetime as dt
import ipaddress
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ams_assess import network
from ams_assess.models import normalize_target

CONNECTORS_SCOPE = "connectors"
CONFIRM_FLAG = "--confirm-authorisation"
_fixture_customers: frozenset[str] = frozenset()


class Unauthorised(ValueError):
    pass


class Authorisation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorised_by: str = Field(min_length=1, max_length=200)
    date: str
    # Website hosts (each covers its subdomains) and/or "connectors" for connected reads.
    scope: list[str] = Field(min_length=1, max_length=20)

    @field_validator("authorised_by")
    @classmethod
    def _who(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("authorised_by must name who authorised the assessment")
        return value

    @field_validator("date")
    @classmethod
    def _date(cls, value: str) -> str:
        try:
            day = dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD") from None
        if day > dt.date.today():
            raise ValueError("date must not be in the future")
        return day.isoformat()

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: list[str]) -> list[str]:
        scope = []
        for entry in value:
            entry = entry.strip()
            if entry.lower() == CONNECTORS_SCOPE:
                scope.append(CONNECTORS_SCOPE)
                continue
            host = (urlsplit(normalize_target(entry)).hostname or "").removesuffix(".")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("scope must name website hosts, not IP addresses")
            if "." not in host:
                raise ValueError("scope must name website hosts such as example.com")
            scope.append(host)
        return list(dict.fromkeys(scope))


def in_scope(record: Authorisation, target: str | None) -> bool:
    if target is None:
        return CONNECTORS_SCOPE in record.scope
    host = (urlsplit(target).hostname or "").lower().removesuffix(".")
    return any(host == s or host.endswith("." + s) for s in record.scope if s != CONNECTORS_SCOPE)


def fixture(customer: str, target: str | None) -> bool:
    if customer in _fixture_customers:
        return True
    if target is None:
        return False
    parts = urlsplit(target)
    return f"{parts.scheme}://{parts.netloc}" in network._fixture_origins


def require(
    current: dict[str, Any] | None, customer: str, target: str | None, *, confirmed: bool
) -> None:
    """Fail closed unless the stored profile authorises this target (None: connected reads)."""
    if fixture(customer, target):
        return
    raw = (((current or {}).get("profile") or {}).get("fields") or {}).get("authorisation")
    if not raw:
        raise Unauthorised(
            "No recorded authorisation for this customer; record it with "
            "'ams-assess authorise' before any assessment or connected read"
        )
    try:
        record = Authorisation.model_validate(raw)
    except ValueError:
        raise Unauthorised("The recorded authorisation is invalid; record it again") from None
    if not in_scope(record, target):
        raise Unauthorised(
            "The recorded authorisation does not cover "
            + ("connected reads" if target is None else "this target")
            + "; update its scope with 'ams-assess authorise'"
        )
    if not confirmed:
        raise Unauthorised(
            f"Confirm with {CONFIRM_FLAG} that this run is within the authorisation recorded "
            f"by {record.authorised_by} on {record.date}"
        )
