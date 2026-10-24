"""Business quantities are matched with units, periods and qualifiers, not bare numbers."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ams_assess.models import Evidence

QUANTITY = re.compile(
    r"(?P<qualifier>approximately\s+|approx\.?\s+|about\s+|around\s+|~\s*|up to\s+|at least\s+)?"
    r"(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)\s+"
    r"(?P<modifier>(?:new|more|additional)\s+)?"
    r"(?P<unit>leads?|sales|conversions?|enquir(?:y|ies)|calls?|bookings?|jobs?|customers?|"
    r"visitors?|traffic|clicks)\b"
    r"(?P<period>\s+(?:per|a|each)\s+(?:day|week|month|year)|\s*/\s*(?:day|week|month|year))?",
    re.I,
)
GAIN = re.compile(r"\b(?:increase|boost|gain|generate|deliver)\b", re.I)


@dataclass(frozen=True)
class Figure:
    value: Decimal
    unit: str
    period: str
    qualifier: str
    modifier: str


def figure(match: re.Match[str]) -> Figure:
    unit = match["unit"].lower()
    unit = {"enquiries": "enquiry", "sales": "sale"}.get(unit, unit.rstrip("s"))
    period = re.sub(r"^(?:per|a|each|/)\s*", "", (match["period"] or "").strip().lower())
    qualifier = (match["qualifier"] or "").strip().lower()
    if qualifier in {"~", "approx", "approx.", "about", "around"}:
        qualifier = "approximately"
    return Figure(
        Decimal(match["value"].replace(",", "")),
        unit,
        period,
        qualifier,
        (match["modifier"] or "").strip().lower(),
    )


def records(evidence: Evidence) -> list[tuple[Figure, str]]:
    data = evidence.data
    if not isinstance(data, dict):
        return []
    if evidence.source == "discovery" and evidence.provenance == "customer_statement":
        statement = str(data.get("statement", ""))
        frequency = str(data.get("frequency", ""))
        if frequency and not any(m["period"] for m in QUANTITY.finditer(statement)):
            period = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}
            if frequency.lower() in period:
                statement += " per " + period[frequency.lower()]
        return [(figure(m), statement) for m in QUANTITY.finditer(statement)]
    if evidence.provenance != "technical_evidence":
        return []
    # Only explicitly labelled measurements, never a coincidentally equal score or identifier.
    values = data.get("measurements", [])
    result = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict) or "value" not in value or "unit" not in value:
            continue
        phrase = " ".join(str(value.get(k, "")) for k in ("qualifier", "value", "unit")).strip()
        if value.get("period"):
            phrase += " per " + str(value["period"])
        if value.get("context"):
            phrase += " " + str(value["context"])
        result.extend((figure(m), phrase) for m in QUANTITY.finditer(phrase))
    return result


def supported(text: str, cited: list[Evidence]) -> bool:
    for match in QUANTITY.finditer(text):
        wanted = figure(match)
        if not any(wanted == recorded for e in cited for recorded, _ in records(e)):
            return False
        # Turning a recorded workload into a gain is a different claim.
        if GAIN.search(text[max(0, match.start() - 30) : match.start()]):
            return False
    return True


def render(text: str, cited: list[Evidence]) -> str:
    """Render matched quantities from the original record so no context is silently lost."""
    matches = list(QUANTITY.finditer(text))
    if not matches:
        return text
    rows = []
    for match in matches:
        for evidence in cited:
            found = next(
                (phrase for value, phrase in records(evidence) if value == figure(match)), None
            )
            if found is None:
                continue
            if evidence.provenance == "customer_statement":
                found = (
                    re.sub(
                        r"^(?:the customer reports?|customer reports?|we (?:receive|get))\s+",
                        "",
                        found,
                        flags=re.I,
                    )
                    .strip()
                    .rstrip(".")
                )
                found = re.sub(
                    r"^(?:~\s*|approx\.?\s+|about\s+|around\s+)",
                    "approximately ",
                    found,
                    flags=re.I,
                )
                found = found[:1].lower() + found[1:]
                rows.append(
                    f"Customer reports {found}; not independently verified. ({evidence.id})"
                )
            else:
                rows.append(f"Measured evidence records {found.rstrip('.')}. ({evidence.id})")
            break
    return " ".join(dict.fromkeys(rows))


def customer_statement(value: Any) -> str:
    statement = str(value)
    if not QUANTITY.search(statement):
        return statement
    evidence = Evidence(
        id="ev-discovery-000",
        source="discovery",
        url="",
        description="",
        data={"statement": statement},
        provenance="customer_statement",
    )
    return render(statement, [evidence]).replace(" (ev-discovery-000)", "")
