"""Business quantities are matched with units, periods and qualifiers, not bare numbers."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ams_assess.models import Evidence

UNIT_PATTERN = (
    r"leads?|sales|conversions?|enquir(?:y|ies)|calls?|bookings?|jobs?|customers?|"
    r"visitors?|traffic|clicks"
)
QUANTITY = re.compile(
    r"(?P<qualifier>approximately\s+|approx\.?\s+|about\s+|around\s+|~\s*|up to\s+|at least\s+)?"
    r"(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)\s+"
    r"(?P<modifier>(?:new|more|additional)\s+)?"
    rf"(?P<unit>{UNIT_PATTERN})\b"
    r"(?P<period>\s+(?:per|a|each)\s+(?:day|week|month|year)|\s*/\s*(?:day|week|month|year))?",
    re.I,
)
# Evaluate the whole field, including sentences referring back to the quantity. A matching
# historic number never supports a forecast, intervention effect or conditional gain.
PROMISSORY = re.compile(
    r"\b(?:will|could|would|can|should|may|might|shall)\s+\w+"
    r"|\b\w+['’](?:ll|d)\s+\w+|\b(?:won['’]t|can['’]t|cannot)\s+\w+"
    r"|\b(?:by|after|through|from)\s+(?:\w+ly\s+)*\w+ing\b"
    r"|\b(?:leads?|leading|results?|resulting|translates?|translating)\s+(?:to|in|into)\b"
    r"|\b(?:expected|recoverable)\s+(?:after|with|once|when|from|through)\b"
    r"|\bexpect\b.*\b(?:once|after|when|with)\b"
    r"|\b(?:because|therefore|so that|as a result|in order to|if)\b",
    re.I,
)
GAIN = re.compile(
    r"\b(?:increas(?:e|es|ed|ing)|boost(?:s|ed|ing)?|gain(?:s|ed|ing)?|"
    r"generat(?:e|es|ed|ing)|deliver(?:s|ed|ing)?|add(?:s|ed|ing)?|win(?:s|ning)?|"
    r"driv(?:e|es|ing)|bring(?:s|ing)?)\b|^\s*get\b",
    re.I,
)
FORECAST = re.compile(
    r"\b(?:doubl(?:e[sd]?|ing)|recover(?:s|ed|ing|able)?|grow(?:s|ing|n)?|grew|expect(?:s|ed|ing)?)\b",
    re.I,
)
FIGURE_REFERENCE = re.compile(
    r"\b(?:that|this|these|those|it|them|the\s+(?:number|figure|count|total|volume))\b", re.I
)
FUTURE = re.compile(
    r"\b(?:will|would|could|can|should|may|might|shall|expect(?:s|ed|ing)?|likely)\b"
    r"|\b\w+['’](?:ll|d)\b",
    re.I,
)
OUTCOME_OR_DIRECTION = re.compile(
    rf"\b(?:{UNIT_PATTERN}|revenue|more|higher|ris(?:e[sd]?|ing)|"
    r"(?:lift|climb|jump)(?:s|ed|ing)?|tripl(?:e[sd]?|ing)|"
    r"improv(?:e[sd]?|ing)|increas(?:e[sd]?|ing))\b",
    re.I,
)


def followup_promise(sentence: str) -> bool:
    """A modal/causal instruction alone is not a promised effect on the recorded figure."""
    reference = re.sub(r"\b(?:check|ensure|confirm|verify)\s+that\b", "", sentence, flags=re.I)
    return bool(
        GAIN.search(sentence)
        or FORECAST.search(sentence)
        or (PROMISSORY.search(sentence) and FIGURE_REFERENCE.search(reference))
        or (FUTURE.search(sentence) and OUTCOME_OR_DIRECTION.search(sentence))
    )


def sentences(text: str) -> list[str]:
    """Keep trailing citations with their sentence and decimal quantities intact."""
    return [
        part.strip()
        for part in re.findall(
            r".+?(?:[.!?](?=\s|$)(?:\s*[(\[]ev-[^\]\n)]+[)\]]\.?)*|\n|$)",
            re.sub(r"\bapprox\.\s+", "approx ", text, flags=re.I),
            re.S,
        )
        if part.strip()
    ]


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
        return [
            (figure(m), sentence)
            for sentence in sentences(statement)
            for m in QUANTITY.finditer(sentence)
        ]
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
    has_figure = bool(QUANTITY.search(text))
    for sentence in sentences(text):
        matches = list(QUANTITY.finditer(sentence))
        if (matches and (PROMISSORY.search(sentence) or GAIN.search(sentence))) or (
            has_figure and not matches and followup_promise(sentence)
        ):
            return False
        for match in matches:
            if not any(figure(match) == value for e in cited for value, _ in records(e)):
                return False
    return True


def without_followup_promises(text: str) -> str:
    """Drop separate promises; leave figure-bearing claims for evidence validation to reject."""
    if not QUANTITY.search(text):
        return text
    parts = sentences(text)
    retained = [
        sentence
        for sentence in parts
        if QUANTITY.search(sentence) or not followup_promise(sentence)
    ]
    return " ".join(retained) if len(retained) != len(parts) else text


def render(text: str, cited: list[Evidence]) -> str:
    """Replace only figure-bearing sentences, retaining other validated prose."""
    if not QUANTITY.search(text):
        return text
    output = []
    for sentence in sentences(without_followup_promises(text)):
        rows = []
        for match in QUANTITY.finditer(sentence):
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
        output.append(" ".join(dict.fromkeys(rows)) if rows else sentence)
    return " ".join(output)


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
