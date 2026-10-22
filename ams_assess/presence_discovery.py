"""Independent digital-presence discovery across the Australian platform catalogue.

Sources, in order of strength: profiles the website links to (links and JSON-LD sameAs, already
collected by the presence module), then Google organic and Google Maps results obtained through
the DataForSEO SERP API. Paid calls happen only under the run's --dataforseo-max-usd cap, with
the estimate checked against the remaining cap before every call and a hard query count. Platform
pages themselves are never fetched. States never say a profile does not exist, is owned, or is
verified; "Not found in searches" only describes the searches that ran.
"""

import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlsplit

from ams_assess.models import Context, ModuleResult
from ams_assess.modules import dataforseo
from ams_assess.platforms import (
    BUYING_MODELS,
    BY_KEY,
    INDUSTRIES,
    LEVELS,
    NA,
    Platform,
    classify,
    identifies,
    prioritised,
)
from ams_assess.profile import PRIORITY_KEY, STATES, phone_key, priority_detail

FOUND_LIKELY = STATES["found_likely"]
FOUND_VERIFY = STATES["found_needs_verification"]
NOT_FOUND = STATES["not_found"]
INCONSISTENT = STATES["inconsistent"]
NOT_APPLICABLE = STATES["not_applicable"]
NOT_ASSESSED = STATES["not_assessed"]
DEADLINE_SECONDS = 120
MAX_KEYWORDS = 5
# Explainable score: name in the title/snippet 3 (or only in the profile URL slug 2; never both),
# partial name 1, phone 3, website domain 2, suburb/postcode not part of the name 2. A candidate
# needs the name or the phone; "likely" needs 5+ including independent corroboration.
LIKELY_SCORE, CANDIDATE_SCORE = 5, 2
LEGAL = {"pty", "ltd", "limited", "the", "co", "inc", "pl"}
PHONE = re.compile(r"(?:\+?61|\b0)[\d ()-]{7,14}\d")
POSTCODE = re.compile(r"\b(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\s+(\d{4})\b", re.I)
GOOGLE_SEARCH = "https://www.google.com.au/search?q={q}"
NOTES = [
    "Not found in searches describes only the searches listed; it does not mean a profile "
    "does not exist.",
    "Found states describe a likely or possible match from linked or search-result data. "
    "Ownership, management and platform verification are never confirmed by this engine.",
    "Platform pages are not fetched or scraped. Search results come from the DataForSEO "
    "Google SERP and Google Maps APIs, only when a USD cap was passed for the run.",
    "Keyword positions are point-in-time samples for one location; they are not a ranking "
    "guarantee and AI-search readiness is not AI-search visibility.",
]
GBP_ELIGIBILITY = {
    "storefront": "Storefront: eligible for a Google Business Profile at the staffed address "
    "customers visit during stated hours.",
    "service-area": "Service-area business: eligible only if staff genuinely travel to "
    "customers; hide the street address and set the real service areas.",
    "online-only": "Online-only: generally not eligible for a Google Business Profile; "
    "focus on the website, search and social presence instead.",
    "b2b-contract": "B2B / contract: eligible only with a staffed location customers can visit, "
    "or as a service-area business if staff genuinely serve customers on site.",
}
GBP_RULE = (
    "Never invent addresses, use virtual offices or create per-suburb profiles; one profile "
    "per genuine location."
)


def norm(text: str) -> str:
    text = str(text).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def name_phrase(name: str) -> str:
    return " ".join(t for t in norm(name).split() if t not in LEGAL)


def terms(text: str) -> str:
    """Plain search terms: nothing a search engine (or its biller) could read as an operator.

    Colons, percent signs, quotes and pipes are removed, OR/AND/AROUND lose their operator
    case and leading minus signs are dropped, so a generated query carries only the operator
    its fixed template adds.
    """
    text = re.sub(r"[:%\"“”‘’'|]", " ", str(text))
    text = re.sub(r"\b(OR|AND|AROUND)\b", lambda m: m.group(1).lower(), text)
    text = re.sub(r"(?:^|(?<=\s))-+", " ", text)
    return " ".join(text.split())


def keyword_problem(keyword: str) -> str:
    """Why a user keyword is not a plain search term ('' when it is)."""
    if re.search(r"[a-z]+:", keyword, re.I) or ":" in keyword:
        return "search operators (word:) are not allowed"
    if "%" in keyword:
        return "percent-encoding (%) is not allowed"
    if re.search(r"[\"“”]", keyword):
        return "quotes are not allowed"
    return ""


def search_name(name: str) -> str:
    """The trading name for queries: a trailing "Pty Ltd" / "Limited" only narrows results."""
    return terms(re.sub(r"[\s,]+(?:pty\.?\s*)?(?:ltd|limited)\.?$", "", name.strip(), flags=re.I))


def host_of(url: str) -> str:
    try:
        host = urlsplit(url if "://" in url else f"https://{url}").hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.").removeprefix("m.")


def url_key(url: str) -> str:
    """Comparison key for a profile URL: host and path, plus the query for Google Maps IDs."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
    query = f"?{parts.query}" if "google" in host and "cid=" in parts.query else ""
    return f"{host}{parts.path.rstrip('/')}".lower() + query


def identity(ctx: Context, site: dict[str, Any]) -> dict[str, Any]:
    """Who to search for: customer profile values first, then the website's structured data."""
    profile = ctx.profile or {}
    names = [n for n in [profile.get("name", ""), *site["names"]] if n]
    if not names and ctx.customer_name != ctx.customer_id:
        names = [ctx.customer_name]
    source = (
        "customer profile (customer statement)"
        if profile.get("name")
        else "website structured data"
        if site["names"]
        else "customer name given for the run"
        if names
        else ""
    )
    locations = profile.get("locations", [])
    localities = list(
        dict.fromkeys(
            [loc["suburb"] for loc in locations if loc.get("suburb")]
            + list(profile.get("service_area", []))
            + site["localities"]
        )
    )
    phones = dict(site["phones"])
    for phone in profile.get("phones", []):
        phones.setdefault(phone_key(phone), phone)
    domains = {host_of(ctx.target)} | {host_of(w) for w in profile.get("websites", [])}
    industry = profile.get("industry") or None
    model = profile.get("buying_model") or None
    return {
        "name": names[0] if names else "",
        "names": list(dict.fromkeys(names + list(profile.get("aliases", [])))),
        "source": source,
        "industry": industry,
        "industry_label": INDUSTRIES.get(industry or "", "not recorded"),
        "buying_model": model,
        "buying_model_label": BUYING_MODELS.get(model or "", "not recorded"),
        "localities": localities,
        "postcodes": sorted(
            {loc["postcode"] for loc in locations if loc.get("postcode")} | set(site["postcodes"])
        ),
        "phones": phones,
        "domains": sorted(d for d in domains if d),
    }


def score(who: dict[str, Any], text: str, url: str, phones: list[str], domain: str = "") -> dict:
    """Simple explainable match score of one search result against the business identity.

    The title/snippet and the profile URL slug are one piece of name evidence, never two. A
    suburb or postcode counts only when it is not part of the business name. "Likely" needs the
    name (or phone) plus independent corroboration: phone, website domain or suburb/postcode.
    """
    folded = f" {norm(text)} "
    points, matched, corroborated = 0, [], []
    phrases = [p for p in (name_phrase(n) for n in who["names"]) if p]
    name_tokens = {t for p in phrases for t in p.split()}
    full = next((p for p in phrases if f" {p} " in folded), None)
    slug = re.sub(r"[^a-z0-9]", "", urlsplit(url).path.lower())
    slugged = any(len(s) >= 6 and s in slug for s in (p.replace(" ", "") for p in phrases))
    named = partial = False
    if full:
        points, named = 3, True
        matched.append(f"name: {full}" + (" (also in profile URL)" if slugged else ""))
    elif slugged:
        points, named = 2, True
        matched.append("name in profile URL")
    else:
        for phrase in phrases:
            tokens = [t for t in phrase.split() if len(t) > 2]
            hits = [t for t in tokens if f" {t} " in folded]
            if len(tokens) >= 2 and len(hits) * 2 >= len(tokens):
                points, partial = points + 1, True
                matched.append(f"partial name: {' '.join(hits)}")
                break
    seen = {phone_key(p) for p in phones if p} | {phone_key(m) for m in PHONE.findall(text)}
    seen = {p for p in seen if len(p) >= 8}
    if same := sorted(seen & who["phones"].keys()):
        points += 3
        corroborated.append("phone")
        matched.append(f"phone: {who['phones'][same[0]]}")
    if domain and host_of(domain) in who["domains"]:
        points += 2
        corroborated.append("website")
        matched.append(f"website: {host_of(domain)}")
    rest = folded
    for phrase in phrases:
        rest = rest.replace(f" {phrase} ", "  ")
    place = next(
        (
            p
            for p in [*who["localities"], *who["postcodes"]]
            if norm(p) and not set(norm(p).split()) <= name_tokens and f" {norm(p)} " in rest
        ),
        None,
    )
    if place:
        points += 2
        corroborated.append("suburb/postcode")
        matched.append(f"suburb/postcode: {place}")
    likely = points >= LIKELY_SCORE and ((named and corroborated) or len(corroborated) >= 2)
    candidate = points >= CANDIDATE_SCORE and (named or partial or "phone" in corroborated)
    return {
        "score": points,
        "matched": matched,
        "state": FOUND_LIKELY if likely else FOUND_VERIFY if candidate else "",
        "observed_phones": sorted(seen),
        "observed_postcodes": sorted(set(POSTCODE.findall(text))),
    }


def ranked(kind: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Result items of the queried type in reported rank order, within the requested depth.

    Mixed SERP features (ads, People also ask, maps packs) are dropped BEFORE the depth limit,
    so they never push an organic result out of the sample; reported ranks are kept as is.
    """
    wanted = {"maps": {"maps_search"}, "organic": {"organic"}}[kind]
    typed = [i for i in items if i.get("type", next(iter(wanted))) in wanted]

    def rank(pair: tuple[int, dict[str, Any]]) -> int:
        value = pair[1].get("rank_group")
        return value if isinstance(value, int) and value > 0 else pair[0] + 1

    ordered = sorted(enumerate(typed), key=rank)
    return [item for index, item in ordered if rank((index, item)) <= dataforseo.SERP_DEPTH]


def trim(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        ["type", "rank_group", "title", "address", "phone", "domain", "url", "cid", "category"]
        if kind == "maps"
        else ["type", "rank_group", "rank_absolute", "title", "url", "domain", "description"]
    )
    return {k: str(item[k])[:300] for k in keys if item.get(k) not in (None, "")}


def plan_queries(
    ctx: Context, who: dict[str, Any], rows: list[tuple[Platform, str | None]]
) -> list[dict[str, Any]]:
    """Queries in relevance order, so the most relevant platforms are searched first.

    Order: high-relevance platforms (with the Maps and name searches), then the requested
    keywords, then medium and low platforms. Queries avoid quotes so a profile search carries
    exactly one billed operator (site:); every call is still estimated and capped individually.
    """
    name, place = search_name(who["name"]), terms((who["localities"] or [""])[0])
    if not name:
        return []
    order = {level: float(i) for i, level in enumerate(LEVELS)}
    level = dict((p.key, lvl) for p, lvl in rows)
    plan: list[dict[str, Any]] = []
    gbp = level.get("google_business_profile")
    if gbp is not None and gbp in LEVELS:
        plan.append(
            {
                "kind": "maps",
                "purpose": "Google Maps business search",
                "keyword": f"{name} {place}".strip(),
                "platforms": ["google_business_profile"],
                "rank": order[gbp],
            }
        )
    plan.append(
        {
            "kind": "organic",
            "purpose": "Business name search",
            "keyword": f"{name} {place}".strip(),
            "platforms": [],
            "rank": 0.0,
        }
    )
    for platform, lvl in rows:
        if lvl in LEVELS and platform.search_domain:
            plan.append(
                {
                    "kind": "organic",
                    "purpose": f"{platform.label} profile search",
                    "keyword": f"site:{platform.search_domain} {name}",
                    "platforms": [platform.key],
                    "rank": order[lvl],
                }
            )
    for keyword in ctx.keywords[:MAX_KEYWORDS]:
        plan.append(
            {
                "kind": "organic",
                "purpose": "Keyword position",
                "keyword": keyword,
                "platforms": [],
                "rank": 0.5,
            }
        )
        if problem := keyword_problem(keyword):
            # The CLI rejects these; refused here too so no caller can send one.
            plan[-1].update(status="refused", reason=f"not a plain keyword: {problem}")
    plan.sort(key=lambda q: q["rank"])
    for query in plan:
        estimate = dataforseo.serp_estimate(query["kind"], query["keyword"])
        query["estimated_usd"] = str(estimate) if estimate is not None else ""
    return plan


def manual_query(platform: Platform, who: dict[str, Any]) -> tuple[str, str]:
    name = search_name(who["name"]) or "<business name>"
    place = terms((who["localities"] or [""])[0])
    if platform.key == "associations":
        query = f'"{name}" member (chamber OR association) {place}'.strip()
    elif platform.search_domain:
        query = f'site:{platform.search_domain} "{name}"'
    else:
        query = f"{name} {place}".strip()
    template = platform.manual_url or GOOGLE_SEARCH
    return query, template.format(q=quote_plus(query))


def pricing_table() -> list[dict[str, str]]:
    return [
        {"endpoint": kind, "operators": str(ops), "published_usd": str(p), "estimate_usd": str(e)}
        for (kind, ops), (p, e) in dataforseo.SERP_PRICING.items()
    ]


def own(ids: list[str]) -> list[str]:
    """Findings may cite only this module's evidence; linked-page evidence stays in the data."""
    return [e for e in ids if e.startswith("ev-presence-")]


def discover(
    ctx: Context,
    result: ModuleResult,
    site: dict[str, Any],
    links: dict[str, list[dict[str, str]]],
    paid: bool,
) -> dict[str, Any]:
    """Discovery tables (stored with the presence tables) plus deterministic findings."""
    who = identity(ctx, site)
    profile_eid = ""
    if ctx.profile:
        profile_eid = result.add_evidence(
            "customer-profile", "Customer business profile (customer statement)", ctx.profile
        )
        result.evidence[-1].provenance = "customer_statement"
    rows = prioritised(who["industry"], who["buying_model"])
    refusal = "" if paid else "independent search was not requested for this run"
    refusal = refusal or dataforseo.serp_refusal(ctx)
    if not who["name"]:
        refusal = refusal or "business name unknown (record it with ams-assess customer)"
    plan = plan_queries(ctx, who, rows) if not refusal else []
    deadline = time.monotonic() + DEADLINE_SECONDS
    candidates: dict[str, dict[str, dict[str, Any]]] = {}
    searched: dict[str, str] = {}
    positions: dict[str, dict[str, Any]] = {}
    sampled = datetime.now(UTC).date().isoformat()

    def add(key: str, url: str, found: dict[str, Any], source: str, eid: str) -> None:
        # The one promotion path for every source (website links, organic and Maps results): a
        # URL without a business or place identifier (e.g. a map search) is never likely.
        if not identifies(url):
            found = {
                **found,
                "state": FOUND_VERIFY,
                "matched": [*found["matched"], "no business or place identifier"],
            }
        entry = candidates.setdefault(key, {}).setdefault(
            url_key(url),
            {
                "url": url,
                "state": found["state"],
                "score": found.get("score"),
                "matched": [],
                "sources": [],
                "observed_phones": [],
                "observed_postcodes": [],
                "evidence_ids": [],
                "title": found.get("title", ""),
                "snippet": found.get("snippet", ""),
            },
        )
        if found["state"] == FOUND_LIKELY:
            entry["state"] = FOUND_LIKELY
        if found.get("score") is not None:
            entry["score"] = max(entry["score"] or 0, found["score"])
        for field, value in [
            ("matched", found["matched"]),
            ("sources", [source]),
            ("observed_phones", found.get("observed_phones", [])),
            ("observed_postcodes", found.get("observed_postcodes", [])),
            ("evidence_ids", [eid]),
        ]:
            entry[field] = list(dict.fromkeys(entry[field] + value))

    for key, found_links in links.items():
        for link in found_links:
            add(
                key,
                link["href"],
                {"state": FOUND_LIKELY, "matched": ["linked from the website"]},
                link["source"],
                link["source_evidence_id"],
            )

    ledger = ""
    for query in plan:
        if query.get("status") == "refused":
            continue
        outcome = dataforseo.serp(ctx, query["kind"], query["keyword"], deadline)
        query.update(
            status=outcome["status"],
            reason=outcome.get("reason", ""),
            cost_usd=outcome.get("cost_usd", ""),
        )
        # Rewritten after every call, so the spend survives any later failure in the module.
        ledger = ctx.raw(
            "presence-dataforseo-ledger",
            {
                "cap_usd": str(ctx.dataforseo_max_usd),
                "spent_usd_run_total": str(ctx.dataforseo_spent_usd),
                "serp_calls": ctx.dataforseo_serp_calls,
                "estimates_usd": pricing_table(),
                "queries": plan,
            },
        )
        if ledger not in result.raw_paths:
            result.raw_paths.append(ledger)
        if outcome["status"] != "ok":
            continue
        items = ranked(query["kind"], outcome["items"])
        eid = result.add_evidence(
            outcome.get("check_url") or f"dataforseo:google-{query['kind']}",
            f"DataForSEO Google {query['kind']} SERP: {query['keyword'][:120]}",
            {
                "query": outcome["query"],
                "purpose": query["purpose"],
                "cost_usd": outcome.get("cost_usd", ""),
                "sampled_at": sampled,
                "items": [trim(query["kind"], i) for i in items],
            },
        )
        query["evidence_id"] = eid
        for key in query["platforms"]:
            searched[key] = eid
        if query["purpose"] == "Keyword position":
            hit = next(
                (
                    i
                    for i in items
                    if any(
                        host_of(str(i.get("url", ""))) == d
                        or host_of(str(i.get("url", ""))).endswith("." + d)
                        for d in who["domains"]
                    )
                ),
                None,
            )
            positions[query["keyword"]] = {
                "position": (
                    hit["rank_group"]
                    if isinstance(hit.get("rank_group"), int)
                    else items.index(hit) + 1
                )
                if hit
                else None,
                "rank_absolute": hit.get("rank_absolute") if hit else None,
                "url": str(hit.get("url", "")) if hit else "",
                "evidence_id": eid,
            }
            continue
        for item in items:
            if query["kind"] == "maps":
                cid = str(item.get("cid") or "")
                if not cid.isdigit():
                    continue
                url, key = f"https://maps.google.com/?cid={cid}", "google_business_profile"
                text = f"{item.get('title', '')} {item.get('address', '')}"
                found = score(
                    who, text, url, [str(item.get("phone") or "")], str(item.get("domain") or "")
                )
                found["snippet"] = str(item.get("address") or "")[:300]
            else:
                url = str(item.get("url") or "")
                key = classify(url)
                if key is None:
                    continue
                text = f"{item.get('title', '')} {item.get('description', '')}"
                found = score(who, text, url, [])
                found["snippet"] = str(item.get("description") or "")[:300]
            found["title"] = str(item.get("title") or "")[:200]
            if found["state"]:
                add(key, url, found, f"search: {query['keyword']}", eid)

    # NAP comparison from obtained data only: search-result fields against site/profile values.
    nap: list[dict[str, Any]] = []
    reference_eids = [e for e in [*site["evidence_ids"], profile_eid] if e]
    for key, found in candidates.items():
        for candidate in found.values():
            checks = [
                ("phone", candidate["observed_phones"], set(who["phones"]), who["phones"]),
                ("postcode", candidate["observed_postcodes"], set(who["postcodes"]), {}),
            ]
            for field, observed, reference, display in checks:
                for value in observed:
                    outcome = (
                        "no reference"
                        if not reference
                        else "match"
                        if value in reference
                        else "differs"
                    )
                    nap.append(
                        {
                            "key": key,
                            "platform": BY_KEY[key].label,
                            "url": candidate["url"],
                            "field": field,
                            "observed": display.get(value, value),
                            "reference": [display.get(r, r) for r in sorted(reference)],
                            "result": outcome,
                            "evidence_ids": own(candidate["evidence_ids"]) + reference_eids,
                        }
                    )
                    if outcome == "differs":
                        candidate["inconsistent"] = True

    status = (
        NOT_ASSESSED
        if refusal
        else "Assessed"
        if all(q["status"] == "ok" for q in plan)
        else "Partially assessed"
    )
    reasons = sorted({q["reason"] for q in plan if q.get("reason")})
    reason = refusal or "; ".join(reasons)
    plan_eid = result.add_evidence(
        ctx.target,
        "Independent presence discovery plan and coverage",
        {
            "identity": {k: v for k, v in who.items() if k != "phones"},
            "status": status,
            "reason": reason,
            "queries": plan,
            "budget": {
                "cap_usd": str(ctx.dataforseo_max_usd) if ctx.dataforseo_max_usd else None,
                "spent_usd_run_total": str(ctx.dataforseo_spent_usd),
                "serp_calls": ctx.dataforseo_serp_calls,
                "max_serp_calls": dataforseo.MAX_SERP_CALLS,
                "estimates_usd": pricing_table(),
                **dataforseo.market(ctx),
            },
            "relevance": {p.key: lvl for p, lvl in rows},
            "profile_evidence_id": profile_eid,
        },
    )

    platform_rows, tasks = [], []
    for platform, level in rows:
        found = [c for c in candidates.get(platform.key, {}).values()]
        if level is None and not found:
            continue  # Industry-specific platform while the industry is unknown.
        if any(c.get("inconsistent") for c in found):
            state = INCONSISTENT
        elif any(c["state"] == FOUND_LIKELY for c in found):
            state = FOUND_LIKELY
        elif found:
            state = FOUND_VERIFY
        elif level == NA:
            state = NOT_APPLICABLE
        elif platform.key in searched:
            state = NOT_FOUND
        else:
            state = NOT_ASSESSED
        if found:
            eids = [
                result.add_evidence(
                    found[0]["url"],
                    f"Presence candidates: {platform.label}",
                    {"platform": platform.key, "state": state, "candidates": found},
                )
            ]
        else:
            eids = [searched.get(platform.key) or plan_eid]
        linked = bool(links.get(platform.key))
        if state == NOT_APPLICABLE:
            detail = f"Not relevant for {who['buying_model_label'].lower()} / "
            detail += f"{who['industry_label'].lower()}"
        elif found:
            detail = f"{len(found)} candidate profile(s)"
            detail += "; possible duplicates" if len(found) > 1 else ""
            detail += "; linked from the website" if linked else "; not linked from the website"
        elif state == NOT_FOUND:
            detail = "No matching profile in the search results sampled"
        else:
            query = next((q for q in plan if platform.key in q["platforms"]), None)
            why = (query or {}).get("reason") or refusal
            detail = f"Not searched: {why}" if why else "No provider search; manual check only"
        platform_rows.append(
            {
                "key": platform.key,
                "platform": platform.label,
                "group": platform.group,
                "relevance": level or "unknown",
                "state": state,
                "candidates": found,
                "duplicates": len(found) > 1,
                "linked": linked,
                "detail": detail + (f". {platform.notes}" if platform.notes else ""),
                "evidence_ids": eids,
            }
        )
        if level not in LEVELS:
            continue
        if (
            state in {NOT_ASSESSED, FOUND_VERIFY, INCONSISTENT}
            or len(found) > 1
            or (state == NOT_FOUND and level == "high")
        ):
            query, url = manual_query(platform, who)
            action = {
                NOT_ASSESSED: f"Search {platform.label} for the business and record what you find",
                FOUND_VERIFY: "Open the candidate profile(s) and confirm whether they belong to "
                "the business",
                INCONSISTENT: "Open the candidate profile(s) and compare name, address and phone "
                "with the business's details",
                NOT_FOUND: "Confirm manually: absence from the search results sampled is not "
                "proof that no profile exists",
            }.get(
                state,
                "Confirm which candidate profile is the business's and whether the "
                "others are duplicates",
            )
            if len(found) > 1 and state != INCONSISTENT:
                action += "; several candidates were found (possible duplicates)"
            tasks.append(
                {
                    "platform": platform.label,
                    "key": platform.key,
                    "relevance": level,
                    "action": action,
                    "query": query,
                    "url": url,
                    "candidates": [c["url"] for c in found],
                }
            )

    keyword_rows = []
    for keyword in ctx.keywords[:MAX_KEYWORDS]:
        hit = positions.get(keyword)
        query = next((q for q in plan if q["keyword"] == keyword), None)
        if hit:
            state = (
                f"Organic position {hit['position']}"
                + (f" (absolute rank {hit['rank_absolute']})" if hit["rank_absolute"] else "")
                if hit["position"]
                else f"Not in the top {dataforseo.SERP_DEPTH} results sampled"
            )
        else:
            state = f"Not assessed: {(query or {}).get('reason') or refusal}"
        keyword_rows.append(
            {
                "keyword": keyword,
                "state": state,
                "url": (hit or {}).get("url", ""),
                "sampled_at": sampled if hit else "",
                "location_code": dataforseo.market(ctx)["location_code"],
                "evidence_ids": [(hit or {}).get("evidence_id") or plan_eid],
            }
        )

    model = who["buying_model"]
    eligibility = GBP_ELIGIBILITY.get(
        model or "", "Eligibility depends on the buying model; record it in the customer profile."
    )
    tables = {
        "status": status,
        "reason": reason,
        "identity": {
            k: who[k]
            for k in ("name", "source", "industry_label", "buying_model_label", "localities")
        },
        "gbp_eligibility": f"{eligibility} {GBP_RULE}",
        "platforms": platform_rows,
        "nap": nap,
        "keywords": keyword_rows,
        "queries": [
            {
                k: q.get(k, "")
                for k in (
                    "purpose",
                    "keyword",
                    "status",
                    "reason",
                    "estimated_usd",
                    "cost_usd",
                    "evidence_id",
                )
            }
            for q in plan
        ],
        "budget": {
            "serp_calls": ctx.dataforseo_serp_calls,
            "max_serp_calls": dataforseo.MAX_SERP_CALLS,
            "spent_usd_run_total": str(ctx.dataforseo_spent_usd),
            "cap_usd": str(ctx.dataforseo_max_usd) if ctx.dataforseo_max_usd else "",
        },
        "manual_tasks": tasks,
        "notes": NOTES,
    }
    add_findings(result, tables, who, plan_eid, profile_eid)
    return tables


def add_findings(
    result: ModuleResult,
    tables: dict[str, Any],
    who: dict[str, Any],
    plan_eid: str,
    profile_eid: str,
) -> None:
    """Deterministic findings; per-platform ones are keyed platform:<kind>:<platform key> so a
    later manual verification of that platform can retire them from current recommendations."""
    rows = tables["platforms"]
    detail = priority_detail(tables)
    if detail:
        result.finding(
            PRIORITY_KEY,
            "Priority platforms still need presence verification",
            "presence",
            [plan_eid] + ([profile_eid] if profile_eid else []),
            detail,
            "Work through the manual verification tasks in priority order and record the "
            "results with ams-assess presence-verify. Create or correct only profiles the "
            "business genuinely operates.",
            severity="low",
            kind="opportunity",
            confidence="needs_verification",
            ams_service="Local SEO",
        )
    for row in rows:
        key, label = row["key"], row["platform"]
        differs = [n for n in tables["nap"] if n["key"] == key and n["result"] == "differs"]
        if differs:
            result.finding(
                f"platform:external-nap-inconsistent:{key}",
                f"Business details differ on a {label} profile found in search",
                "presence",
                sorted({e for n in differs for e in n["evidence_ids"]}),
                "; ".join(
                    f"Candidate {n['url']}: {n['field']} {n['observed']} "
                    f"(business: {', '.join(n['reference'])})"
                    for n in differs
                )
                + ". The candidate may be a different business with a similar name.",
                "Confirm the candidate belongs to the business, then correct its name, address "
                "and phone to match the website.",
                confidence="needs_verification",
                ams_service="Local SEO",
            )
        if row["duplicates"]:
            result.finding(
                f"platform:possible-duplicates:{key}",
                f"Several candidate profiles on {label}",
                "presence",
                row["evidence_ids"],
                ", ".join(c["url"] for c in row["candidates"]) + ".",
                "Confirm which profile the business operates; merge or close genuine duplicates "
                "through the platform's own process.",
                severity="low",
                confidence="needs_verification",
                ams_service="Local SEO",
            )
        unlinked = [
            c["url"]
            for c in row["candidates"]
            if c["state"] == FOUND_LIKELY
            and not any(not s.startswith("search: ") for s in c["sources"])
        ]
        if unlinked:
            result.finding(
                f"platform:profiles-not-linked:{key}",
                f"Likely {label} profile found in search but not linked from the website",
                "presence",
                row["evidence_ids"],
                ", ".join(unlinked) + ".",
                "If the business operates this profile, link it from the website and add it to "
                "the business JSON-LD sameAs.",
                severity="low",
                kind="opportunity",
                confidence="needs_verification",
                ams_service="Local SEO",
            )
