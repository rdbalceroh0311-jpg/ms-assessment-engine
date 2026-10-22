"""Deterministic rules for site_checks; no I/O or additional dependencies.

Google reference contracts (a focused subset, not rich-result eligibility certification):
https://developers.google.com/search/docs/appearance/structured-data/organization
https://developers.google.com/search/docs/appearance/structured-data/local-business
https://developers.google.com/search/docs/appearance/structured-data/breadcrumb
CWV: https://web.dev/articles/defining-core-web-vitals-thresholds
"""

import json
import math
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from ams_assess.modules.presence import as_list, jsonld_nodes, resolve

# Tokens are purpose-specific: denying model training does not imply denying search.
CRAWLERS = {
    "search": ["OAI-SearchBot", "Claude-SearchBot", "PerplexityBot", "Googlebot", "Bingbot"],
    "training": [
        "GPTBot",
        "ClaudeBot",
        "Google-Extended",
        "CCBot",
        "Applebot-Extended",
        "meta-externalagent",
        "Bytespider",
    ],
    "user-initiated": ["ChatGPT-User", "Claude-User", "Perplexity-User"],
}


def product_token(value: str) -> str:
    """RFC 9309 product token; version suffixes are not part of the crawler name."""
    match = re.match(r"[A-Za-z_-]+", value.strip())
    return match[0].lower() if match else ""


def robots_groups(body: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    started_rules = False
    for line in body.removeprefix("\ufeff").splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (s.strip() for s in line.split(":", 1))
        field = field.lower()
        if field == "user-agent" and value:
            if started_rules:
                groups.append({"agents": agents, "rules": rules})
                agents, rules, started_rules = [], [], False
            agents.append("*" if value == "*" else product_token(value))
        elif agents and field in {"allow", "disallow"}:
            started_rules = True
            if value.startswith("/") or "*" in value:
                rules.append((field, value))
    if agents:
        groups.append({"agents": agents, "rules": rules})
    return groups


def applicable_rules(groups: list[dict[str, Any]], bot: str) -> list[tuple[str, str]]:
    token = product_token(bot)
    specific = [
        g
        for g in groups
        if token and any(a != "*" and product_token(a) == token for a in g["agents"])
    ]
    selected = specific or [g for g in groups if "*" in g["agents"]]
    return [rule for group in selected for rule in group["rules"]]


def robot_path(value: str) -> str:
    # Normalize percent-encoded unreserved octets; preserve reserved separators.
    value = re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda m: (
            chr(int(m[1], 16))
            if chr(int(m[1], 16))
            in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
            else m[0].upper()
        ),
        value,
    )
    return quote(value, safe="/%?=&*:$!+,;@-._~")


def robots_allowed(rules: list[tuple[str, str]], path: str) -> bool:
    matched = []
    for directive, pattern in rules:
        pattern = robot_path(pattern)
        end = pattern.endswith("$")
        pattern = pattern[:-1] if end else pattern
        regex = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + ("$" if end else "")
        if re.search(regex, robot_path(path)):
            matched.append((len(pattern.replace("*", "")), directive == "allow"))
    return max(matched, default=(0, True))[1]


ROBOTS_DIRECTIVES = {
    "all",
    "none",
    "index",
    "noindex",
    "follow",
    "nofollow",
    "nosnippet",
    "noarchive",
    "nocache",
    "notranslate",
    "noimageindex",
    "indexifembedded",
    "unavailable_after",
    "noodp",
    "noydir",
    "noyaca",
}


def noindex(value: Any) -> bool:
    """Exact directives for general search, never a parameter value or another crawler.

    A crawler prefix scopes the following comma-separated directives until a new prefix.
    Separate header values supplied as a list start with an independent unscoped context.
    """
    if isinstance(value, (list, tuple)):
        return any(noindex(part) for part in value)
    agent = ""
    for part in str(value).split(","):
        directive = part.strip().lower()
        if ":" in directive:
            prefix, rest = (s.strip() for s in directive.split(":", 1))
            if prefix not in ROBOTS_DIRECTIVES and not prefix.startswith("max-"):
                if re.fullmatch(r"[a-z][a-z0-9_-]*", prefix):
                    agent, directive = prefix, rest
        if agent in {"", "googlebot", "bingbot"} and directive in {"noindex", "none"}:
            return True
    return False


CWV = {
    "LCP": (2500, 4000, "LARGEST_CONTENTFUL_PAINT_MS", "largest_contentful_paint", "ms"),
    "INP": (200, 500, "INTERACTION_TO_NEXT_PAINT", "interaction_to_next_paint", "ms"),
    "CLS": (0.1, 0.25, "CUMULATIVE_LAYOUT_SHIFT_SCORE", "cumulative_layout_shift", "score"),
}


def cwv_grade(metric: str, value: Any) -> str:
    if isinstance(value, bool):
        return "NOT ASSESSED"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "NOT ASSESSED"
    if not math.isfinite(number) or number < 0:
        return "NOT ASSESSED"
    good, poor, *_ = CWV[metric]
    return "good" if number <= good else ("poor" if number > poor else "needs improvement")


def field_vitals(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Only p75 field data. PSI CLS percentiles are scaled by 100; CrUX values are not."""
    records = []
    for scope, key in [("URL", "field_url"), ("origin", "field_origin")]:
        field = data.get(key, {})
        if field.get("metrics"):
            records.append(
                (
                    "origin" if field.get("origin_fallback") else scope,
                    field,
                    True,
                    data.get("strategy", "unspecified"),
                )
            )
    record = data.get("record", {})
    if record.get("metrics"):
        key = record.get("key", {})
        records.append(
            (
                "URL" if key.get("url") else "origin",
                record,
                False,
                key.get("formFactor", "all form factors"),
            )
        )
    out = []
    for scope, record, psi, device in records:
        for label, (_, _, psi_key, crux_key, unit) in CWV.items():
            metric = record["metrics"].get(psi_key if psi else crux_key, {})
            value = metric.get("percentile") if psi else metric.get("percentiles", {}).get("p75")
            if label == "CLS" and psi and not isinstance(value, bool):
                try:
                    value = float(value) / 100
                except (TypeError, ValueError):
                    value = None
            out.append(
                {
                    "metric": label,
                    "p75": value,
                    "unit": unit,
                    "scope": scope,
                    "device": device,
                    "grade": cwv_grade(label, value),
                }
            )
    return out


# Schema.org LocalBusiness descendants, including types whose name has no 'Business' suffix.
LOCAL_TYPES = set(
    """LocalBusiness AnimalShelter ArchiveOrganization AutomotiveBusiness AutoBodyShop AutoDealer
AutoPartsStore AutoRental AutoRepair AutoWash GasStation MotorcycleDealer MotorcycleRepair
ChildCare Dentist DryCleaningOrLaundry EmergencyService FireStation Hospital PoliceStation
EmploymentAgency EntertainmentBusiness AdultEntertainment AmusementPark ArtGallery Casino
ComedyClub MovieTheater NightClub FinancialService AccountingService AutomatedTeller
BankOrCreditUnion InsuranceAgency FoodEstablishment Bakery BarOrPub Brewery
CafeOrCoffeeShop Distillery FastFoodRestaurant IceCreamShop Restaurant Winery
GovernmentOffice PostOffice HealthAndBeautyBusiness BeautySalon DaySpa HairSalon
HealthClub NailSalon TattooParlor HomeAndConstructionBusiness Electrician
GeneralContractor HVACBusiness HousePainter Locksmith MovingCompany Plumber
RoofingContractor InternetCafe LegalService Attorney Library LodgingBusiness
BedAndBreakfast Campground Hostel Hotel Motel Resort VacationRental MedicalBusiness
CommunityHealth Dermatology DietNutrition Geriatric Gynecologic MedicalClinic Midwifery
Nursing Obstetric Oncologic Optician Optometric Otolaryngologic Pediatric Pharmacy
Physician Physiotherapy PlasticSurgery Podiatric PrimaryCare Psychiatric PublicHealth
ProfessionalService RadioStation RealEstateAgent RecyclingCenter SelfStorage
ShoppingCenter SportsActivityLocation BowlingAlley ExerciseGym GolfCourse
PublicSwimmingPool SkiResort SportsClub StadiumOrArena TennisComplex Store AutoPartsStore
BikeStore BookStore ClothingStore ComputerStore ConvenienceStore DepartmentStore
ElectronicsStore Florist FurnitureStore GardenStore GroceryStore HardwareStore HobbyShop
HomeGoodsStore JewelryStore LiquorStore MensClothingStore MobilePhoneStore
MovieRentalStore MusicStore OfficeEquipmentStore OutletStore PawnShop PetStore ShoeStore
SportingGoodsStore TireShop ToyStore WholesaleStore TelevisionStation
TouristInformationCenter TravelAgency VeterinaryCare""".split()
)


def schema_types(node: dict[str, Any]) -> set[str]:
    return {str(t).rsplit("/", 1)[-1] for t in as_list(node.get("@type"))}


def entity_definitions(values: Any) -> list[dict[str, Any]]:
    """Collect roots/@graph members and nested breadcrumbs, merging matching @ids."""
    roots: list[dict[str, Any]] = []

    def visit(value: Any, declared: bool = False) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, declared)
        elif isinstance(value, dict):
            if declared or "BreadcrumbList" in schema_types(value):
                roots.append(value)
            for key, child in value.items():
                visit(child, declared=key == "@graph")

    visit(values, declared=True)
    by_id: dict[str, dict[str, Any]] = {}
    definitions = []
    for node in roots:
        identifier = node.get("@id")
        if isinstance(identifier, str) and identifier:
            if identifier not in by_id:
                by_id[identifier] = dict(node)
                definitions.append(by_id[identifier])
            else:
                merged = by_id[identifier]
                node_types = set(as_list(merged.get("@type"))) | set(as_list(node.get("@type")))
                merged.update(node)
                if node_types:
                    merged["@type"] = sorted(node_types)
        else:
            definitions.append(node)
    return [
        node
        for node in definitions
        if not (node.get("@id") and set(node) <= {"@id", "@type", "@context"})
    ]


def structured_data(values: list[Any]) -> dict[str, Any]:
    """Google Organization has no required properties; LocalBusiness needs name/address.

    BreadcrumbList needs >=2 ListItems with position and name; the final item may omit item.
    Optional properties are advice, not failures when the feature/schema itself is absent.
    """
    errors, recommended, checked, types = [], [], [], set()
    parsed = []
    for value in values:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                errors.append("Invalid JSON-LD syntax")
                continue
        if not isinstance(value, (dict, list)):
            errors.append("JSON-LD root must be an object or array")
            continue
        parsed.append(value)
    for node in jsonld_nodes(parsed):
        types |= schema_types(node)
    for node in entity_definitions(parsed):
        if node.get("parse_error"):
            errors.append("Invalid JSON-LD syntax")
            continue
        ts = schema_types(node)
        types |= ts
        kind = (
            "LocalBusiness"
            if ts & LOCAL_TYPES
            else (
                "Organization"
                if "Organization" in ts
                else ("BreadcrumbList" if "BreadcrumbList" in ts else "")
            )
        )
        if not kind:
            continue
        checked.append(kind)
        if kind == "BreadcrumbList":
            items = node.get("itemListElement")
            if not isinstance(items, list) or len(items) < 2:
                errors.append("BreadcrumbList needs at least two ListItems")
                continue
            positions = []
            for index, item in enumerate(items):
                if not isinstance(item, dict) or "ListItem" not in schema_types(item):
                    errors.append("Breadcrumb entry must be a ListItem")
                    continue
                position = item.get("position")
                if isinstance(position, str) and re.fullmatch(r"[0-9]+", position.strip()):
                    recommended.append(
                        "Breadcrumb position: use an integer rather than numeric text"
                    )
                    position = int(position)
                if type(position) is not int or position < 1:
                    errors.append("Breadcrumb position must be a positive integer")
                positions.append(position)
                target = item.get("item")
                target_name = target.get("name") if isinstance(target, dict) else None
                if (
                    not isinstance(item.get("name") or target_name, str)
                    or not (item.get("name") or target_name or "").strip()
                ):
                    errors.append("Breadcrumb entry needs name")
                if index < len(items) - 1 or target is not None:
                    url = target.get("@id") if isinstance(target, dict) else target
                    if not resolve(url, ""):
                        if (
                            isinstance(url, str)
                            and not urlsplit(url).scheme
                            and resolve(url, "https://example.com/")
                        ):
                            recommended.append("Breadcrumb: use an absolute URL for relative item")
                        else:
                            errors.append("Breadcrumb item needs a valid HTTP(S) URL")
            if positions != list(range(1, len(items) + 1)):
                errors.append("Breadcrumb positions must be ordered consecutively from 1")
            continue
        if kind == "LocalBusiness":
            if not isinstance(node.get("name"), str) or not node["name"].strip():
                errors.append("LocalBusiness requires name")
            address = node.get("address")
            if not isinstance(address, dict) or "PostalAddress" not in schema_types(address):
                errors.append("LocalBusiness requires a PostalAddress object")
            else:
                for prop in (
                    "streetAddress",
                    "addressLocality",
                    "addressRegion",
                    "postalCode",
                    "addressCountry",
                ):
                    if not address.get(prop):
                        recommended.append(f"LocalBusiness address: add {prop}")
        for prop in (
            ("url", "telephone", "geo", "openingHoursSpecification", "image")
            if kind == "LocalBusiness"
            else ("name", "url", "logo", "sameAs", "address")
        ):
            if not node.get(prop):
                recommended.append(f"{kind}: consider {prop} where applicable")
        for prop in ("name", "telephone"):
            if prop in node and (not isinstance(node[prop], str) or not node[prop].strip()):
                errors.append(f"{kind}: {prop} must be nonempty text")
        for prop in ("url", "sameAs", "logo", "image"):
            for value in as_list(node.get(prop)):
                url = (
                    value.get("url") or value.get("contentUrl") or value.get("@id")
                    if (isinstance(value, dict))
                    else value
                )
                if not resolve(url, ""):
                    if (
                        isinstance(url, str)
                        and not urlsplit(url).scheme
                        and resolve(url, "https://example.com/")
                    ):
                        recommended.append(f"{kind}: use an absolute URL for relative {prop}")
                    else:
                        errors.append(f"{kind}: {prop} needs a valid HTTP(S) URL")
        geo = node.get("geo")
        if geo is not None:
            try:
                if not isinstance(geo, dict) or not (
                    math.isfinite(float(geo["latitude"]))
                    and math.isfinite(float(geo["longitude"]))
                    and -90 <= float(geo["latitude"]) <= 90
                    and -180 <= float(geo["longitude"]) <= 180
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                errors.append(f"{kind}: geo needs valid latitude/longitude")
    return {
        "errors": sorted(set(errors)),
        "recommended": sorted(set(recommended)),
        "checked": checked,
        "types": sorted(types),
    }


TOOLS = {
    "Calendly": ("calendly.com",),
    "HubSpot": (
        "hubspot.com",
        "hsforms.com",
        "hsforms.net",
        "hs-scripts.com",
        "hs-analytics.net",
        "hubspot.net",
        "hubspotusercontent.com",
        "hscollectedforms.net",
    ),
    "GoHighLevel / LeadConnector": (
        "gohighlevel.com",
        "highlevel.com",
        "leadconnectorhq.com",
        "msgsndr.com",
        "msgsndr.net",
    ),
    "Acuity": ("acuityscheduling.com", "squarespacescheduling.com", "as.me"),
    "Square Appointments": ("squareup.com/appointments", "book.squareup.com", "square.site/book"),
    "SimplyBook.me": ("simplybook.me", "simplybook.it"),
    "Setmore": ("setmore.com",),
    "Fresha": ("fresha.com",),
    "Timely": ("gettimely.com", "book.gettimely.com"),
    "ServiceM8": ("servicem8.com",),
    "Tradify": ("tradifyhq.com",),
    "Jobber": ("getjobber.com", "clienthub.getjobber.com"),
    "Zoho": ("zoho.com", "zohopublic.com", "zohopublic.eu", "zohopublic.com.au", "zoho.eu"),
    "Salesforce / Pardot": ("salesforce.com", "force.com", "pardot.com"),
    "ActiveCampaign": ("activecampaign.com", "activehosted.com", "trackcmp.net"),
    "Mailchimp": ("mailchimp.com", "list-manage.com", "chimpstatic.com"),
    "Intercom": ("intercom.io", "intercomcdn.com"),
    "Tidio": ("tidio.co", "tidio.com"),
    "LiveChat": ("livechatinc.com",),
    "Drift": ("drift.com", "driftt.com"),
}


# Anchors need evidence of a booking/customer endpoint. A vendor's marketing site is not
# evidence that the assessed business uses that vendor. Script/iframe contexts are separate.
ACCOUNT_LINKS = {
    "HubSpot": r"meetings(?:-[a-z0-9-]+)?\.hubspot\.com(?:/|$)|"
    r"(?:share|forms)\.hsforms\.(?:com|net)/.+",
    "GoHighLevel / LeadConnector": r"[^/]+\.leadconnectorhq\.com(?:/|$)|"
    r"(?:[^/]+\.)?msgsndr\.(?:com|net)/widget/(?:booking|form|survey)/.+",
    "Square Appointments": r"book\.squareup\.com(?:/|$)|(?:[^/]+\.)?square\.site/book/.+|"
    r"(?:www\.)?squareup\.com/appointments/book/.+",
    "SimplyBook.me": r"[^/]+\.simplybook\.(?:me|it)(?:/|$)",
    "Setmore": r"[^/]+\.setmore\.com(?:/|$)",
    "Fresha": r"(?:www\.)?fresha\.com/a/.+",
    "Timely": r"book\.gettimely\.com(?:/|$)",
    "ServiceM8": r"(?:book|form)\.servicem8\.com/.+",
    "Tradify": r"enquiries\.tradifyhq\.com/.+",
    "Jobber": r"clienthub\.getjobber\.com/.+",
    "Zoho": r"[^/]+\.zohopublic\.(?:com(?:\.au)?|eu)(?:/|$)",
    "Salesforce / Pardot": r"[^/]+\.my\.salesforce\.com(?:/|$)|go\.pardot\.com/l/.+",
    "ActiveCampaign": r"[^/]+\.activehosted\.com(?:/|$)",
    "Mailchimp": r"[^/]+\.list-manage\.com/subscribe(?:/|\?).+",
}


def account_link(tool: str, url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.split(".")[0] in {"www", "blog", "docs", "help", "support"} and tool not in {
        "Calendly",
        "Square Appointments",
        "Fresha",
        "Acuity",
    }:
        return False
    if tool == "Acuity":
        if re.fullmatch(r"[a-z0-9-]+\.as\.me", host):
            return host.split(".")[0] not in {"www", "blog", "docs", "help", "support"}
        if parts.path != "/schedule.php":
            return False
        return host == "app.acuityscheduling.com" or (
            host in {"acuityscheduling.com", "www.acuityscheduling.com"}
            and bool(parse_qs(parts.query).get("owner"))
        )
    if tool == "Calendly":
        if host not in {"calendly.com", "www.calendly.com"}:
            return False
        segments = [s for s in parts.path.lower().split("/") if s]
        return bool(
            segments
            and segments[0]
            not in {
                "blog",
                "features",
                "pricing",
                "about",
                "solutions",
                "integrations",
                "resources",
                "enterprise",
                "teams",
                "login",
                "signup",
                "contact",
                "sales",
                "security",
                "partners",
                "help",
                "legal",
                "pages",
            }
        )
    pattern = ACCOUNT_LINKS.get(tool)
    location = host + parts.path + ("?" + parts.query if parts.query else "")
    return bool(pattern and re.fullmatch("(?:" + pattern + ").*", location))


def tool_signals(page: dict[str, Any], base: str) -> list[dict[str, str]]:
    hits = []
    for context in ("script_srcs", "links", "iframes", "resource_links"):
        for value in page.get(context, []):
            if context == "resource_links":
                # The current browser collector drops rel: bare URLs cannot distinguish a
                # stylesheet from a preconnect/DNS/prefetch hint, so do not infer a tool.
                if not isinstance(value, dict):
                    continue
                rel = str(value.get("rel", "")).lower().split()
                if set(rel) & {"preconnect", "dns-prefetch", "prefetch"} or "stylesheet" not in rel:
                    continue
            value = value.get("href") or value.get("src", "") if isinstance(value, dict) else value
            url = resolve(value, base)
            if not url:
                continue
            parts = urlsplit(url)
            for label, signatures in TOOLS.items():
                for sig in signatures:
                    host, _, path = sig.partition("/")
                    actual = (parts.hostname or "").lower()
                    if (actual == host or actual.endswith("." + host)) and (
                        not path
                        or parts.path == "/" + path
                        or parts.path.startswith("/" + path + "/")
                    ):
                        if context != "links" or account_link(label, url):
                            hits.append({"tool": label, "context": context, "url": url})
                        break
    # Recognise executable API calls, not strings containing provider names.
    for snippet in page.get("tag_snippets", []):
        code = re.sub(
            r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`",
            " ",
            str(snippet),
        )
        for tool, pattern in {
            "Calendly": r"\bCalendly\.(?:initInlineWidget|initPopupWidget|initBadgeWidget)\s*\(",
            "HubSpot": r"\bhbspt\.forms\.create\s*\(",
        }.items():
            if re.search(pattern, code):
                hits.append({"tool": tool, "context": "inline_script", "url": base})
    return hits


EVENT = re.compile(
    r"\b(gtag|fbq)\s*\(\s*(['\"])(event|track|trackCustom)\2\s*,\s*(['\"])([\w.-]+)\4"
)
# Skip comments and string literals unless they are arguments of an actual matched call.
JS_TOKEN = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`"
    r"|\b(?:gtag|fbq)\s*\(\s*(['\"])(?:event|track|trackCustom)\1\s*,\s*(['\"])[\w.-]+\2"
)


def conversion_events(page: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for snippet in page.get("tag_snippets", []):
        for token in JS_TOKEN.finditer(str(snippet)):
            match = EVENT.fullmatch(token[0])
            if match and (
                (match[1] == "gtag" and match[3] == "event")
                or (match[1] == "fbq" and match[3] in {"track", "trackCustom"})
            ):
                out.append({"library": match[1], "event": match[5]})
    return out


LOCATION = re.compile(
    r"(?:^|/)(?:locations?|suburbs?|areas?|service-areas?)(?:/|[-_])|"
    r"(?:^|/)(?:services?)/[^/]+",
    re.I,
)


def near_duplicates(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for page in pages[:100]:
        if not LOCATION.search(urlsplit(page["url"]).path):
            continue
        words = re.findall(r"\w+", page.get("visible_text_summary", "").lower())
        if len(words) < 80:  # Tiny/shared snippets are not adequate evidence of near-duplicates.
            continue
        shingles = {tuple(words[i : i + 5]) for i in range(len(words) - 4)}
        candidates.append((page, shingles))
    pairs = []
    for i, (a, left) in enumerate(candidates):
        for b, right in candidates[i + 1 :]:
            score = len(left & right) / len(left | right)
            if score >= 0.8:
                pairs.append(
                    {
                        "urls": [a["url"], b["url"]],
                        "jaccard": round(score, 3),
                        "truncated": bool(
                            a.get("visible_text_truncated") or b.get("visible_text_truncated")
                        ),
                    }
                )
    return pairs


class HomeHTML(HTMLParser):
    """Use the already permitted normal-UA homepage probe; never crawl a second page.

    This supplements iframe, full heading and HTTP-header coverage missing from the browser
    collector. It is static markup, so visibility/above-the-fold position is unknown.
    """

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.page: dict[str, Any] = {
            k: []
            for k in (
                "meta",
                "headings",
                "json_ld",
                "script_srcs",
                "tag_snippets",
                "links",
                "iframes",
                "resource_links",
                "gated_scripts",
            )
        }
        self.script: dict[str, str] | None = None
        self.chunks: list[str] = []
        self.heading = ""
        self.heading_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "meta" and a.get("name"):
            self.page["meta"].append({"name": a["name"], "content": a.get("content", "")})
        if tag in {"script", "iframe", "a", "link"}:
            field = {
                "script": "script_srcs",
                "iframe": "iframes",
                "a": "links",
                "link": "resource_links",
            }[tag]
            value = a.get("src" if tag in {"script", "iframe"} else "href", "")
            if value:
                self.page[field].append(
                    {"href": urljoin(self.base, value), "rel": a.get("rel", "")}
                    if tag == "link"
                    else urljoin(self.base, value)
                )
        if tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.page["canonical"] = urljoin(self.base, a.get("href", ""))
        if tag == "script":
            self.script, self.chunks = a, []
        if re.fullmatch(r"h[1-6]", tag):
            self.heading, self.heading_text = tag.upper(), []

    def handle_data(self, data: str) -> None:
        if self.script is not None:
            self.chunks.append(data)
        if self.heading:
            self.heading_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script is not None:
            a, text = self.script, "".join(self.chunks)
            if a.get("type", "").lower() == "application/ld+json":
                self.page["json_ld"].append(text)
            elif not a.get("src"):
                self.page["tag_snippets"].append(text)
            if a.get("type", "").lower() == "text/plain" or any(
                k in a for k in ("data-consent", "data-cookieconsent", "data-cookiecategory")
            ):
                self.page["gated_scripts"].append(a.get("src") or text)
            self.script = None
        if tag.upper() == self.heading:
            self.page["headings"].append(
                {"level": self.heading, "text": "".join(self.heading_text).strip()}
            )
            self.heading = ""
