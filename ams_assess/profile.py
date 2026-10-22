"""Customer business identity profile and manual presence-verification records.

Profile values are what the customer supplied (provenance: customer statement). Verification
records are what a person observed on a platform (provenance: manual verification); neither
is ever presented as technical evidence, ownership or platform verification.
"""

import copy
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ams_assess.models import normalize_target
from ams_assess.platforms import BUYING_MODELS, BY_KEY, normalise_industry

MAX_FILE_BYTES = 200_000
MAX_VERIFICATIONS = 100
STATES = {
    "found_likely": "Found — likely match",
    "found_needs_verification": "Found — needs verification",
    "not_found": "Not found in searches",
    "inconsistent": "Inconsistent",
    "not_applicable": "Not applicable",
    "not_assessed": "Not assessed",
}
PRIORITY_KEY = "platform:priority-verification"
# A person can record any observed state except "not assessed", which is the absence of one.
MANUAL_STATES = {k: v for k, v in STATES.items() if k != "not_assessed"}
ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
STATE_CODES = "NSW|VIC|QLD|SA|WA|TAS|NT|ACT"
ADDRESS = re.compile(
    rf"^(?P<street>.*?)[,\s]+(?P<suburb>[A-Za-z][A-Za-z' .-]*?)\s+(?P<state>{STATE_CODES})"
    r"\s+(?P<postcode>\d{4})$",
    re.I,
)


def phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return "0" + digits[2:] if digits.startswith("61") and len(digits) == 11 else digits


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=100)
    street: str = Field(default="", max_length=200)
    suburb: str = Field(default="", max_length=100)
    state: str = Field(default="", pattern=rf"^(?:{STATE_CODES})?$")
    postcode: str = Field(default="", pattern=r"^(?:\d{4})?$")

    @classmethod
    def parse(cls, text: str) -> "Location":
        """ "12 Example Street, Sydney NSW 2000" -> fields; otherwise the text is the street."""
        match = ADDRESS.match(" ".join(text.split()))
        if not match:
            return cls(street=text.strip())
        return cls(**{**match.groupdict(), "state": match["state"].upper()})


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    websites: list[str] = Field(default_factory=list, max_length=10)
    abn: str = ""
    industry: str = ""
    buying_model: str = ""
    service_area: list[str] = Field(default_factory=list, max_length=50)
    locations: list[Location] = Field(default_factory=list, max_length=20)
    phones: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("websites")
    @classmethod
    def _websites(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_target(v) for v in value))

    @field_validator("abn")
    @classmethod
    def _abn(cls, value: str) -> str:
        """Format and check digit only; the ABN is never looked up."""
        digits = re.sub(r"\s", "", value)
        if not digits:
            return ""
        if not re.fullmatch(r"\d{11}", digits):
            raise ValueError("ABN must be 11 digits")
        numbers = [int(d) for d in digits]
        numbers[0] -= 1
        if sum(n * w for n, w in zip(numbers, ABN_WEIGHTS, strict=True)) % 89:
            raise ValueError("ABN check digits are invalid")
        return f"{digits[:2]} {digits[2:5]} {digits[5:8]} {digits[8:]}"

    @field_validator("industry")
    @classmethod
    def _industry(cls, value: str) -> str:
        return normalise_industry(value) if value else ""

    @field_validator("buying_model")
    @classmethod
    def _model(cls, value: str) -> str:
        key = value.strip().lower().replace("_", "-").replace(" ", "-")
        if key and key not in BUYING_MODELS:
            raise ValueError("buying_model must be one of: " + ", ".join(BUYING_MODELS))
        return key

    @field_validator("phones")
    @classmethod
    def _phones(cls, value: list[str]) -> list[str]:
        for phone in value:
            if not 8 <= len(phone_key(phone)) <= 12:
                raise ValueError(f"Phone {phone!r} is not a valid phone number")
        return value

    @field_validator("aliases", "service_area")
    @classmethod
    def _texts(cls, value: list[str]) -> list[str]:
        return [v.strip()[:200] for v in value if v.strip()]


class ObservedNap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=200)
    address: str = Field(default="", max_length=300)
    phone: str = Field(default="", max_length=40)


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    state: str
    profile_url: str = Field(default="", max_length=2000)
    observed_nap: ObservedNap = Field(default_factory=ObservedNap)
    verified_by: str = Field(min_length=1, max_length=200)
    date: dt.date
    note: str = Field(default="", max_length=2000)

    @field_validator("platform")
    @classmethod
    def _platform(cls, value: str) -> str:
        if value not in BY_KEY:
            raise ValueError("platform must be a catalogue key: " + ", ".join(BY_KEY))
        return value

    @field_validator("state")
    @classmethod
    def _state(cls, value: str) -> str:
        """A state label or its key; normalised to the label."""
        key = re.sub(r"[^a-z]+", "_", value.lower().replace("—", " ")).strip("_")
        aliases = {"found_likely_match": "found_likely", "not_found_in_searches": "not_found"}
        key = aliases.get(key, key)
        if key not in MANUAL_STATES:
            raise ValueError("state must be one of: " + "; ".join(MANUAL_STATES.values()))
        return MANUAL_STATES[key]

    @field_validator("profile_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if value and not re.match(r"https?://[^\s/]+", value):
            raise ValueError("profile_url must be an http(s) URL")
        return value.strip()


def read_json(path: Path, what: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{what} must be a regular JSON file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{what} file is too large")
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f"{what} is not valid JSON") from None


def validation_message(exc: ValidationError, what: str) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"Invalid {what} at {where}: {first['msg']}"


def merge_profile(current: dict[str, Any] | None, changes: dict[str, Any]) -> Profile:
    """Apply only the fields given; a field given again replaces the stored value."""
    fields = {k: v for k, v in (current or {}).items() if k in Profile.model_fields}
    try:
        return Profile.model_validate({**fields, **changes})
    except ValidationError as exc:
        raise ValueError(validation_message(exc, "profile")) from None


def load_verifications(path: Path) -> list[Verification]:
    raw = read_json(path, "Verification file")
    items = raw.get("verifications") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("Verification file must hold a nonempty list of verifications")
    if len(items) > MAX_VERIFICATIONS:
        raise ValueError(f"Verification file holds more than {MAX_VERIFICATIONS} entries")
    try:
        records = [Verification.model_validate(item) for item in items]
    except ValidationError as exc:
        raise ValueError(validation_message(exc, "verification")) from None
    platforms = [r.platform for r in records]
    if len(platforms) != len(set(platforms)):
        raise ValueError("Each platform may appear only once per verification file")
    return records


def priority_detail(discovery: dict[str, Any]) -> str:
    """Priority-ordered high/medium platforms not yet a likely match ('' when none remain).

    Manually verified platforms are settled, so they never appear here.
    """
    pending = [
        r
        for r in discovery["platforms"]
        if r["relevance"] in {"high", "medium"}
        and r["state"] != STATES["found_likely"]
        and not r.get("manual")
    ]
    if not pending:
        return ""
    who = discovery["identity"]
    return (
        f"In priority order for industry {who['industry_label']}, buying model "
        f"{who['buying_model_label']}: "
        + "; ".join(f"{r['platform']} ({r['relevance']}): {r['state']}" for r in pending)
        + f". Independent search: {discovery['status']}"
        + (f" ({discovery['reason']})" if discovery["reason"] else "")
        + "."
    )


def apply_verifications(
    presence: dict[str, Any], verifications: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Overlay current manual verifications on the automated discovery table (copy).

    For a verified platform the automated state, detail and candidates are kept beside the
    manual record (as automated_*), while its automated manual tasks and NAP rows leave the
    current tables. Stored evidence is untouched.
    """
    discovery = presence.get("discovery")
    if not discovery or not verifications:
        return presence
    presence = copy.deepcopy(presence)
    discovery = presence["discovery"]
    rows = discovery["platforms"]
    by_key: dict[str, dict[str, Any]] = {row["key"]: row for row in rows}
    for key, entry in verifications.items():
        row: dict[str, Any] | None = by_key.get(key)
        if row is None:
            platform = BY_KEY[key]
            row = {
                "key": key,
                "platform": platform.label,
                "group": platform.group,
                "relevance": "-",
                "state": STATES["not_assessed"],
                "candidates": [],
                "duplicates": False,
                "evidence_ids": [],
                "detail": "",
            }
            rows.append(row)
        row["automated_state"] = row["state"]
        row["automated_detail"] = row["detail"]
        row["automated_candidates"] = row["candidates"]
        row["state"] = entry["state"]
        row["detail"] = "Automated result superseded by a manual verification"
        url = entry["profile_url"]
        row["candidates"] = (
            [{"url": url, "matched": ["manual verification"], "score": None}] if url else []
        )
        row["duplicates"] = False
        row["manual"] = {
            k: entry[k]
            for k in ("verified_by", "date", "note", "profile_url", "observed_nap", "evidence_id")
        }
        row["evidence_ids"] = [*row["evidence_ids"], entry["evidence_id"]]
    discovery["manual_tasks"] = [
        t for t in discovery["manual_tasks"] if t["key"] not in verifications
    ]
    discovery["nap"] = [n for n in discovery["nap"] if n["key"] not in verifications]
    return presence


def current_findings(
    findings: list[dict[str, Any]], presence: dict[str, Any], verifications: dict[str, Any]
) -> list[dict[str, Any]]:
    """Findings for current recommendations once manual verifications apply.

    Per-platform discovery findings (platform:<kind>:<key>) of a verified platform are retired
    and the priority finding is recomputed from the overlaid table (retired when nothing is
    pending). The store keeps every finding and all evidence unchanged.
    """
    current = []
    for finding in findings:
        key = finding.get("check_key", "")
        parts = key.split(":")
        if parts[0] == "platform" and len(parts) == 3 and parts[2] in verifications:
            continue
        if key == PRIORITY_KEY and presence.get("discovery"):
            detail = priority_detail(presence["discovery"])
            if not detail:
                continue
            finding = {**finding, "detail": detail}
        current.append(finding)
    return current
