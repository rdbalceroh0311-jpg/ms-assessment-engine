"""Australian digital-presence platform catalogue: recognition patterns and relevance rules.

Pure data plus three helpers (classify, relevance, prioritised). Nothing here makes a request;
platform pages are never fetched. Relevance decides which platforms matter for a business: not
every platform is mandatory, and "not applicable" is a valid outcome.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

GROUPS = {
    "search_maps": "Search & maps",
    "social": "Social",
    "au_directory": "Australian directories",
    "industry": "Industry platforms",
}
INDUSTRIES = {
    "restaurant": "Restaurants, cafes & food",
    "beauty": "Hair, beauty & wellness",
    "trades": "Trades & home services (incl. painting)",
    "b2b_contractor": "Traffic management & B2B contractors",
    "general": "General service SME",
}
INDUSTRY_ALIASES = {
    "restaurants": "restaurant",
    "cafe": "restaurant",
    "food": "restaurant",
    "hospitality": "restaurant",
    "hair": "beauty",
    "salon": "beauty",
    "wellness": "beauty",
    "trade": "trades",
    "painting": "trades",
    "painter": "trades",
    "plumbing": "trades",
    "electrical": "trades",
    "traffic_management": "b2b_contractor",
    "traffic": "b2b_contractor",
    "b2b": "b2b_contractor",
    "contractor": "b2b_contractor",
    "services": "general",
    "professional_services": "general",
    "other": "general",
}
BUYING_MODELS = {
    "storefront": "Storefront (customers visit a staffed address)",
    "service-area": "Service-area (staff travel to customers)",
    "online-only": "Online-only",
    "b2b-contract": "B2B / contract",
}
LEVELS = ("high", "medium", "low")
NOT_APPLICABLE_LEVEL = "not_applicable"
NA = NOT_APPLICABLE_LEVEL
# Local listings need a place customers visit or an area staff genuinely serve.
LOCAL = {
    "storefront": "medium",
    "service-area": "medium",
    "b2b-contract": "low",
    "online-only": "low",
}


# Generic platform pages that are never a business profile, whatever the platform pattern says.
GENERIC_PAGE = re.compile(
    r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:search|s|find|category|categories|help|support|login"
    r"|log-in|signin|sign-in|signup|sign-up|register|join|blog|articles|news|about|careers"
    r"|jobs|terms|privacy|legal|explore|discover|directory|topics|hashtag|tags?|pricing"
    r"|partners|for-business|contact|faq|sitemap)(?:[/?._-]|$)",
    re.I,
)


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    group: str
    # (host full-match after stripping "www.", business-profile route searched in path+query);
    # empty = recognised manually only.
    patterns: tuple[tuple[str, str], ...] = ()
    # Domain for a Google "site:" query through the DataForSEO SERP; empty = no provider search.
    search_domain: str = ""
    # Level by buying model; "*" is the default. Industry overrides win over the buying model.
    by_model: dict[str, str] = field(default_factory=lambda: {"*": "medium"})
    by_industry: dict[str, str] = field(default_factory=dict)
    # None: relevant to every industry; otherwise not applicable outside these industries.
    industries: frozenset[str] | None = None
    manual_url: str = ""  # Search URL template with {q}; used for manual-verification tasks.
    # Searched in host+path+query: a recognised URL that also names a business or place. Empty
    # means every recognised route already does (profile routes only).
    identifier: str = ""
    notes: str = ""


def _p(key: str, label: str, group: str, *patterns: tuple[str, str], **kwargs) -> Platform:
    return Platform(key, label, group, tuple(patterns), **kwargs)


CATALOGUE: list[Platform] = [
    _p(
        "google_business_profile",
        "Google Business Profile / Google Maps",
        "search_maps",
        (r"g\.page", r"^/(?:r/)?[\w.-]+"),
        (r"maps\.app\.goo\.gl", r"^/\w+"),
        (r"goo\.gl", r"^/maps/\w+"),
        # A place page or a cid link; never a Maps search, directions or a web search.
        (r"(?:maps\.)?google\.[a-z.]+", r"^/maps/place/[^/?]+|^/(?:maps/?)?\?(?:.*&)?cid=\d+"),
        by_model={"*": "high", "b2b-contract": "medium", "online-only": NA},
        identifier=r"^g\.page/[\w.-]+|/maps/place/[^/?]+|[?&]cid=\d+",
        manual_url="https://www.google.com/maps/search/?api=1&query={q}",
        notes="Searched through the DataForSEO Google Maps SERP only.",
    ),
    _p(
        "bing_places",
        "Bing Places / Bing Maps",
        "search_maps",
        # Never searched by the engine, so only a link the website itself chose is recognised.
        (r"bing\.com", r"^/maps\b"),
        by_model={"*": "medium", "b2b-contract": "low", "online-only": NA},
        identifier=r"[?&](?:ss=ypid\.|ypid=|id=ypid\.)[\w.-]+",
        manual_url="https://www.bing.com/maps?q={q}",
        notes="No provider search; manual verification only.",
    ),
    _p(
        "apple_maps",
        "Apple Business / Apple Maps",
        "search_maps",
        (r"maps\.apple\.com", r"^/(?:place\b|\?)"),
        by_model={"*": "low", "storefront": "medium", "online-only": NA},
        identifier=r"^maps\.apple\.com/place\b|[?&](?:place-id|auid)=[\w.-]+",
        manual_url="https://maps.apple.com/?q={q}",
        notes="Apple Business replaced Apple Business Connect. No provider search; manual only.",
    ),
    _p(
        "facebook",
        "Facebook",
        "social",
        (
            r"(?:m\.)?facebook\.com|fb\.com",
            r"^/(?:pages/[^/?]+/\d+|profile\.php\?(?:.*&)?id=\d+|(?!(?:sharer|share|dialog"
            r"|plugins|tr|groups|events|watch|marketplace|gaming|login|help|search|hashtag"
            r"|policies|privacy|pages|people|photo|photos|reel|stories|business|ads|settings"
            r"|home\.php|profile\.php)\b)[\w.-]+/?(?:\?.*)?$)",
        ),
        search_domain="facebook.com",
        by_industry={"restaurant": "high", "beauty": "high"},
    ),
    _p(
        "instagram",
        "Instagram",
        "social",
        (
            r"instagram\.com",
            r"^/(?!(?:p|reel|reels|explore|accounts|stories|tv|direct|about|legal|developer"
            r"|web|share)\b)[\w.]+/?(?:\?.*)?$",
        ),
        search_domain="instagram.com",
        by_model={"*": "low"},
        by_industry={"restaurant": "high", "beauty": "high", "trades": "medium"},
    ),
    _p(
        "linkedin",
        "LinkedIn",
        "social",
        (
            r"(?:[a-z]{2}\.)?linkedin\.com",
            r"^/(?:company|in|school)/[^/?]+/?(?:about/?)?(?:\?.*)?$",
        ),
        search_domain="linkedin.com/company",
        by_model={"*": "low", "b2b-contract": "high"},
        by_industry={"b2b_contractor": "high", "general": "medium"},
    ),
    _p(
        "youtube",
        "YouTube",
        "social",
        (
            r"(?:m\.)?youtube\.com",
            r"^/(?:@[\w.-]+|c/[^/?]+|channel/[\w-]+|user/[^/?]+)(?:/(?:featured|videos|about))?"
            r"/?(?:\?.*)?$",
        ),
        search_domain="youtube.com",
        by_model={"*": "low"},
    ),
    _p(
        "tiktok",
        "TikTok",
        "social",
        (r"tiktok\.com", r"^/@[\w.-]+/?(?:\?.*)?$"),
        search_domain="tiktok.com",
        by_model={"*": "low"},
        by_industry={"restaurant": "medium", "beauty": "medium"},
    ),
    _p(
        "pinterest",
        "Pinterest",
        "social",
        (
            r"(?:[a-z]{2}\.)?pinterest\.(?:com|com\.au)",
            r"^/(?!(?:pin|search|ideas|categories|today|business|login|_)\b)[\w-]+/?(?:\?.*)?$",
        ),
        search_domain="pinterest.com",
        by_model={"*": "low"},
        industries=frozenset({"beauty", "trades"}),
        notes="Relevant only where visual inspiration drives enquiries.",
    ),
    _p(
        "yellow_pages",
        "Yellow Pages AU",
        "au_directory",
        (r"yellowpages\.com\.au", r"^/(?:nsw|vic|qld|sa|wa|tas|nt|act)/[^/?]+/[^/?]+"),
        search_domain="yellowpages.com.au",
        by_model=LOCAL,
    ),
    _p(
        "true_local",
        "True Local",
        "au_directory",
        (r"truelocal\.com\.au", r"^/business/[^/?]+"),
        search_domain="truelocal.com.au",
        by_model=LOCAL,
    ),
    _p(
        "localsearch",
        "Localsearch",
        "au_directory",
        (r"localsearch\.com\.au", r"^/profile/[^/?]+"),
        search_domain="localsearch.com.au",
        by_model=LOCAL,
    ),
    _p(
        "yelp",
        "Yelp",
        "au_directory",
        (r"(?:m\.)?yelp\.com(?:\.au)?", r"^/biz/[^/?]+"),
        search_domain="yelp.com.au",
        by_model={**LOCAL, "service-area": "low"},
    ),
    _p(
        "hotfrog",
        "Hotfrog",
        "au_directory",
        (r"hotfrog\.com\.au", r"^/company/[^/?]+"),
        search_domain="hotfrog.com.au",
        by_model={**LOCAL, "storefront": "low", "service-area": "low"},
    ),
    _p(
        "cylex",
        "Cylex",
        "au_directory",
        (r"cylex\.com\.au|cylex-australia\.com", r"^/company/[^/?]+"),
        search_domain="cylex.com.au",
        by_model={**LOCAL, "storefront": "low", "service-area": "low"},
    ),
    _p(
        "whereis",
        "Whereis",
        "au_directory",
        (r"whereis\.com", r"^/(?:nsw|vic|qld|sa|wa|tas|nt|act)/[^/?]+/[^/?]+"),
        search_domain="whereis.com",
        by_model={**LOCAL, "storefront": "low", "service-area": "low"},
    ),
    _p(
        "word_of_mouth",
        "Word of Mouth",
        "au_directory",
        (r"wordofmouth\.com\.au", r"^/reviews/[^/?]+"),
        search_domain="wordofmouth.com.au",
        by_model={"*": "low"},
        industries=frozenset({"trades", "general", "beauty"}),
    ),
    _p(
        "associations",
        "Chambers of commerce / professional & industry associations",
        "au_directory",
        by_model={"*": "low", "b2b-contract": "high"},
        by_industry={"b2b_contractor": "high"},
        manual_url="https://www.google.com.au/search?q={q}",
        notes="Generic manual item: membership directories differ by industry and region "
        "(e.g. local chambers, trade or traffic-management industry bodies).",
    ),
    # Restaurants. TheFork is not assumed to operate in Australia.
    _p(
        "tripadvisor",
        "Tripadvisor",
        "industry",
        (r"tripadvisor\.com(?:\.au)?", r"^/(?:Restaurant_Review|Attraction_Review|Hotel_Review)"),
        search_domain="tripadvisor.com.au",
        by_model={"*": "high"},
        industries=frozenset({"restaurant"}),
    ),
    _p(
        "opentable",
        "OpenTable",
        "industry",
        (r"opentable\.com(?:\.au)?", r"^/(?:r/[^/?]+|restaurant/profile/\d+)"),
        search_domain="opentable.com.au",
        industries=frozenset({"restaurant"}),
    ),
    _p(
        "uber_eats",
        "Uber Eats",
        "industry",
        (r"ubereats\.com", r"^/(?:[a-z]{2}/)?store/[^/?]+"),
        search_domain="ubereats.com",
        industries=frozenset({"restaurant"}),
    ),
    _p(
        "doordash",
        "DoorDash",
        "industry",
        (r"doordash\.com", r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?store/[^/?]+"),
        search_domain="doordash.com",
        industries=frozenset({"restaurant"}),
    ),
    _p(
        "menulog",
        "Menulog",
        "industry",
        (r"menulog\.com\.au", r"^/restaurants-[^/?]+"),
        search_domain="menulog.com.au",
        industries=frozenset({"restaurant"}),
    ),
    # Hair, beauty and wellness booking marketplaces.
    _p(
        "fresha",
        "Fresha",
        "industry",
        (r"fresha\.com", r"^/(?:[a-z]{2}/)?(?:a|lvp)/[^/?]+"),
        search_domain="fresha.com",
        by_model={"*": "high"},
        industries=frozenset({"beauty"}),
    ),
    _p(
        "booksy",
        "Booksy",
        "industry",
        (r"booksy\.com", r"^/[a-z]{2}-[a-z]{2}/\d+_[^/?]+"),
        search_domain="booksy.com",
        industries=frozenset({"beauty"}),
    ),
    _p(
        "timely",
        "Timely",
        "industry",
        (r"bookings\.gettimely\.com", r"^/[\w-]+(?:/book)?/?(?:\?.*)?$"),
        search_domain="gettimely.com",
        industries=frozenset({"beauty"}),
    ),
    # Trades and painting. Oneflare closed and is deliberately not catalogued.
    _p(
        "hipages",
        "hipages",
        "industry",
        (r"hipages\.com\.au", r"^/connect/[^/?]+"),
        search_domain="hipages.com.au",
        by_model={"*": "high"},
        industries=frozenset({"trades"}),
    ),
    _p(
        "houzz",
        "Houzz",
        "industry",
        (r"houzz\.com(?:\.au)?", r"^/(?:professionals/[^/?]+/[^/?]+|pro/[^/?]+)"),
        search_domain="houzz.com.au",
        industries=frozenset({"trades"}),
    ),
    _p(
        "airtasker",
        "Airtasker",
        "industry",
        (r"airtasker\.com", r"^/(?:au/)?users/[^/?]+"),
        search_domain="airtasker.com",
        by_model={"*": "low"},
        industries=frozenset({"trades"}),
    ),
    _p(
        "serviceseeking",
        "ServiceSeeking",
        "industry",
        (r"serviceseeking\.com\.au", r"^/profile/[^/?]+"),
        search_domain="serviceseeking.com.au",
        by_model={"*": "low"},
        industries=frozenset({"trades"}),
    ),
    # Traffic management and B2B contractors: government and industry supplier registers.
    _p(
        "buy_nsw",
        "NSW buy.nsw Supplier Hub",
        "industry",
        (r"buy\.nsw\.gov\.au", r"^/supplier/(?:profile/)?[^/?]+"),
        search_domain="buy.nsw.gov.au",
        by_model={"*": "high"},
        industries=frozenset({"b2b_contractor"}),
        notes="Supplier registration is confirmed in the Supplier Hub by the customer.",
    ),
    _p(
        "austender",
        "AusTender",
        "industry",
        (r"tenders\.gov\.au", r"^/Cn/Show/[\w-]+"),
        search_domain="tenders.gov.au",
        industries=frozenset({"b2b_contractor"}),
    ),
    _p(
        "icn_gateway",
        "ICN Gateway",
        "industry",
        (r"gateway\.icn\.org\.au", r"^/company/[^/?]+"),
        search_domain="gateway.icn.org.au",
        industries=frozenset({"b2b_contractor"}),
    ),
]
BY_KEY = {p.key: p for p in CATALOGUE}


def normalise_industry(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    key = INDUSTRY_ALIASES.get(key, key)
    if key not in INDUSTRIES:
        raise ValueError("industry must be one of: " + ", ".join(INDUSTRIES))
    return key


def classify(url: str) -> str | None:
    """Catalogue key for a recognised business-profile URL, or None (search/help/... pages)."""
    try:
        parts = urlsplit(str(url).strip())
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    host = (hostname or "").removeprefix("www.")
    route = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if GENERIC_PAGE.match(parts.path or "/"):
        return None
    for platform in CATALOGUE:
        for hosts, path in platform.patterns:
            if re.fullmatch(hosts, host) and re.search(path, route):
                return platform.key
    return None


def identifies(url: str) -> bool:
    """Whether a recognised URL carries a business/place identifier (not a search link)."""
    key = classify(url)
    if key is None:
        return False
    pattern = BY_KEY[key].identifier
    if not pattern:
        return True
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").removeprefix("www.")
    route = f"{host}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
    return bool(re.search(pattern, route))


def relevance(platform: Platform, industry: str | None, model: str | None) -> str | None:
    """high / medium / low / not_applicable; None when the industry is unknown and decides it."""
    if platform.industries is not None:
        if industry is None:
            return None
        if industry not in platform.industries:
            return NA
    level = platform.by_model.get(model or "*", platform.by_model.get("*", "medium"))
    if level == NA:
        return NA
    return platform.by_industry.get(industry or "", level)


def prioritised(industry: str | None, model: str | None) -> list[tuple[Platform, str | None]]:
    """Every catalogue platform with its relevance, most relevant first (stable within a level)."""
    order = {level: i for i, level in enumerate((*LEVELS, None, NA))}
    rows = [(p, relevance(p, industry, model)) for p in CATALOGUE]
    return sorted(rows, key=lambda row: order[row[1]])
