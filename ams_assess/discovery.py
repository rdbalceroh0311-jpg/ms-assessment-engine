"""Structured business-discovery notes (Stage 3): customer statements, never technical evidence."""

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ams_assess.models import AMS_SERVICES

DISCOVERY_SECTIONS = {
    "enquiries_communication": "Enquiries & communication",
    "sales_quoting": "Sales & quoting",
    "bookings_scheduling": "Bookings & scheduling",
    "systems_in_use": "Systems in use",
    "manual_duplicated_tasks": "Manual & duplicated tasks",
    "existing_automations": "Existing automations",
    "pain_points_bottlenecks": "Pain points & bottlenecks",
}
MAX_NOTES_BYTES = 1_000_000
MAX_STATEMENTS = 200


class Statement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=2000)
    frequency: str = Field(default="", max_length=200)
    systems: list[str] = Field(default_factory=list, max_length=30)
    owner: str = Field(default="", max_length=200)
    impact: str = Field(default="", max_length=500)

    @field_validator("systems", mode="before")
    @classmethod
    def _systems(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class DerivedFinding(BaseModel):
    """A finding the author derived from the notes; it must cite statements by reference."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    kind: Literal["problem", "opportunity"] = "opportunity"
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"
    detail: str = Field(min_length=1, max_length=2000)
    recommendation: str = Field(min_length=1, max_length=2000)
    ams_service: str
    # "section" cites every statement in it; "section:N" cites the Nth (0-based) one.
    cites: list[str] = Field(min_length=1, max_length=50)
    likely_cause: str = Field(default="", max_length=1000)
    business_impact: str = Field(default="", max_length=1000)
    key: str = Field(default="", pattern=r"^[a-z0-9][a-z0-9-]{0,49}$|^$")

    @field_validator("ams_service")
    @classmethod
    def _service(cls, value: str) -> str:
        if value not in AMS_SERVICES:
            raise ValueError("ams_service must be one of: " + "; ".join(AMS_SERVICES))
        return value


class Notes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: dict[str, list[Statement | str]] = Field(default_factory=dict)
    findings: list[DerivedFinding] = Field(default_factory=list, max_length=100)

    def statements(self) -> dict[str, list[Statement]]:
        return {
            key: [s if isinstance(s, Statement) else Statement(statement=s) for s in items]
            for key, items in self.sections.items()
        }


def load_notes(path: Path) -> Notes:
    """Parse and fully validate a notes file; raises ValueError with a safe message."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Notes must be a regular JSON file")
    if path.stat().st_size > MAX_NOTES_BYTES:
        raise ValueError("Notes file is too large")
    try:
        notes = Notes.model_validate(json.loads(path.read_text()))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Notes are not valid JSON") from None
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ValueError(f"Invalid notes at {where}: {first['msg']}") from None
    unknown = sorted(set(notes.sections) - DISCOVERY_SECTIONS.keys())
    if unknown:
        raise ValueError(
            f"Unknown sections {', '.join(unknown)}; choose from {', '.join(DISCOVERY_SECTIONS)}"
        )
    statements = notes.statements()
    if not sum(len(v) for v in statements.values()):
        raise ValueError("Notes contain no statements")
    if sum(len(v) for v in statements.values()) > MAX_STATEMENTS:
        raise ValueError(f"Notes contain more than {MAX_STATEMENTS} statements")
    for finding in notes.findings:
        for cite in finding.cites:
            section, _, index = cite.partition(":")
            items = statements.get(section)
            if not items or (index and not (index.isdigit() and int(index) < len(items))):
                raise ValueError(f"Finding '{finding.title}' cites unknown statement '{cite}'")
    return notes


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:50] or "finding"
