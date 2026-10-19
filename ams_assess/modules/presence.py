"""Digital platform, listing-link and connected-assessment readiness from collected evidence.

Page signals come from the browser module (SiteOne page list), header signals from SiteOne,
email-platform and DNS verification signals from domain_email, and the connection state from the
gsc module. The only requests made here are the bounded same-origin robots.txt and sitemap fetch,
and only after SiteOne reached the site. Statuses are deterministic; AI may only summarise them.
"""

import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ams_assess.models import Context, ModuleResult

name = "presence"
categories = {"presence"}
requires = [
    "browser page evidence",
    "domain_email DNS evidence (email platform, DNS verification)",
    "one bounded same-origin robots.txt + sitemap fetch after a SiteOne crawl",
]

DETECTED, NOT_DETECTED, UNKNOWN, NOT_ASSESSED = (
    "Detected",
    "Not detected",
    "Unknown",
    "NOT ASSESSED",
)
LINKED = "Detected (linked from website)"
UNKNOWN_MANUAL = "Unknown - manual verification"
OWNERSHIP = "Ownership confirmation needed"
NOT_CONNECTED = "Not connected"
PLACEHOLDER_EMAIL = "[AMS access account - set AMS_ACCESS_EMAIL]"
PASS, WARNING = "PASS", "WARNING"
SITEMAP_TIMEOUT, SITEMAP_MAX_BYTES = 10, 2_000_000

# key: (label, group, patterns). A first capture group, when it matched, is recorded as an ID.
# Matched against script URLs, inline script/noscript text, <link> URLs, request URLs,
# "meta:<name>=<content>" and "header:<name>=<value>" lines; never against visible text or anchors.
# IDs only count inside tag-specific context (gtag calls, tag URLs, the GTM loader), so an
# "AW-12345678" product code or similar string elsewhere in a script is not a detection.
SIGNATURES: dict[str, tuple[str, str, list[str]]] = {
    "gsc": ("Google Search Console", "search", [r"meta:google-site-verification=(\S+)"]),
    "bing_wmt": ("Bing Webmaster Tools", "search", [r"meta:msvalidate\.01=(\S+)"]),
    "ga4": (
        "Google Analytics 4",
        "analytics",
        [
            r"googletagmanager\.com/gtag/js\?id=(G-[A-Z0-9]{4,})",
            r"gtag\(\s*['\"]config['\"]\s*,\s*['\"](G-[A-Z0-9]{4,})",
            r"google-analytics\.com/g/collect\?\S*?tid=(G-[A-Z0-9]{4,})",
        ],
    ),
    "ua": (
        "Universal Analytics (legacy)",
        "analytics",
        [
            r"gtag/js\?id=(UA-\d{4,10}-\d{1,4})\b",
            r"\b(?:gtag|ga)\(\s*['\"](?:config|create)['\"]\s*,\s*['\"](UA-\d{4,10}-\d{1,4})['\"]",
            r"_setAccount['\"]\s*,\s*['\"](UA-\d{4,10}-\d{1,4})['\"]",
            r"google-analytics\.com/(?:analytics|ga)\.js",
        ],
    ),
    "gtm": (
        "Google Tag Manager",
        "analytics",
        [
            r"googletagmanager\.com/(?:gtm\.js|ns\.html)\?id=(GTM-[A-Z0-9]{4,})",
            # Standard loader: (window,document,'script','dataLayer','GTM-XXXX').
            r"['\"]script['\"]\s*,\s*['\"]\w+['\"]\s*,\s*['\"](GTM-[A-Z0-9]{4,})['\"]",
        ],
    ),
    "clarity": (
        "Microsoft Clarity",
        "analytics",
        [
            r"clarity\.ms/tag/([a-z0-9]{6,})",
            r"['\"]clarity['\"]\s*,\s*['\"]script['\"]\s*,\s*['\"]([a-z0-9]{6,})['\"]",
            r"clarity\.ms/tag/",
        ],
    ),
    "hotjar": ("Hotjar", "analytics", [r"static\.hotjar\.com", r"hjid\s*:\s*(\d{4,})"]),
    "google_ads": (
        "Google Ads",
        "advertising",
        [
            r"gtag/js\?id=(AW-\d{6,})",
            r"gtag\(\s*['\"](?:config|event)['\"]\s*,\s*['\"](AW-\d{6,})",
            r"send_to['\"]?\s*:\s*['\"](AW-\d{6,})",
            r"googleadservices\.com/pagead/conversion",
        ],
    ),
    "meta_pixel": (
        "Meta Pixel",
        "advertising",
        [
            r"connect\.facebook\.net/\w+/fbevents\.js",
            r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,})",
            r"facebook\.com/tr\?id=(\d{6,})",
        ],
    ),
    "linkedin": (
        "LinkedIn Insight Tag",
        "advertising",
        [
            r"snap\.licdn\.com/li\.lms-analytics",
            r"_linkedin_partner_id\s*=\s*['\"]?(\d{4,})",
            r"px\.ads\.linkedin\.com",
        ],
    ),
    "bing_uet": (
        "Microsoft Advertising UET",
        "advertising",
        [r"bat\.bing\.com/(?:bat\.js|action/0\?ti=(\d+))"],
    ),
    "tiktok": (
        "TikTok Pixel",
        "advertising",
        [r"analytics\.tiktok\.com", r"ttq\.load\(\s*['\"]([A-Z0-9]{8,})"],
    ),
    "wordpress": (
        "WordPress",
        "cms",
        [
            r"meta:generator=WordPress ?([\d.]+)?",
            r"/wp-content/",
            r"/wp-includes/",
            r"/wp-json",
            r"api\.w\.org",
        ],
    ),
    "shopify": ("Shopify", "cms", [r"cdn\.shopify\.com", r"Shopify\.theme", r"header:x-shopify"]),
    "wix": (
        "Wix",
        "cms",
        [
            r"static\.wixstatic\.com",
            r"static\.parastorage\.com",
            r"meta:generator=Wix",
            r"header:x-wix-",
        ],
    ),
    "squarespace": (
        "Squarespace",
        "cms",
        [r"static1\.squarespace\.com", r"assets\.squarespace\.com", r"meta:generator=Squarespace"],
    ),
    "webflow": (
        "Webflow",
        "cms",
        [r"meta:generator=Webflow", r"assets\.website-files\.com", r"webflow\.js"],
    ),
    "joomla": ("Joomla", "cms", [r"meta:generator=Joomla", r"/media/jui/", r"/media/system/js/"]),
    "drupal": (
        "Drupal",
        "cms",
        [
            r"meta:generator=Drupal",
            r"/sites/default/files/",
            r"drupal-settings-json",
            r"header:x-drupal-",
            r"header:x-generator=Drupal",
        ],
    ),
}
EMAIL_PLATFORMS = {
    "google_workspace": (
        "Google Workspace",
        [r"\.google\.com\.?$", r"\.googlemail\.com\.?$"],
        [r"include:_spf\.google\.com"],
    ),
    "m365": (
        "Microsoft 365",
        [r"\.mail\.protection\.outlook\.com\.?$"],
        [r"include:spf\.protection\.outlook\.com", r"\bMS=ms\d+"],
    ),
}
# Tags commonly injected by GTM or held back by a consent manager until the visitor opts in.
TAG_GROUPS = {"analytics", "advertising"}
CONSENT_MANAGERS = {
    "Cookiebot": r"consent\.cookiebot\.com|data-cookieconsent",
    "OneTrust": r"cdn\.cookielaw\.org|otSDKStub|optanon",
    "CookieYes": r"cdn-cookieyes\.com",
    "Complianz": r"cmplz",
    "Usercentrics": r"usercentrics\.eu",
    "iubenda": r"cdn\.iubenda\.com",
    "Osano": r"cmp\.osano\.com",
    "Termly": r"app\.termly\.io",
}
CONNECTABLE = {"gsc", "bing_wmt", "ga4", "clarity", "wordpress", "google_workspace", "m365"}
ACTIONS = {
    "gsc": ("Request access (customer access checklist)", "Confirm whether a property exists"),
    "bing_wmt": ("Request read-only access", "Optional: import the site from Search Console"),
    "ga4": ("Request Viewer access", "Confirm the analytics setup with the customer"),
    "ua": ("Remove the legacy Universal Analytics tag", "None"),
    "gtm": ("Review container tags and consent with the customer", "None"),
    "clarity": ("Request Team member access", "Optional: consider behavioural analytics"),
    "hotjar": ("Confirm consent and data retention settings", "None"),
    "wordpress": ("Review updates and plugins if in scope", "None"),
    "google_workspace": ("Security baseline review only if in scope", "None"),
    "m365": ("Security baseline review only if in scope", "None"),
}
DEFAULT_ACTIONS = {
    "advertising": ("Confirm consent and conversion setup with the customer", "None"),
    "cms": ("Record the platform for future work", "None"),
}

# (key, host full-match after stripping "www.", path search). First match wins.
LISTINGS: list[tuple[str, str, str]] = [
    (
        "google_business_profile",
        r"g\.page|business\.google\.com|maps\.app\.goo\.gl|maps\.google\.[a-z.]+",
        "",
    ),
    ("google_business_profile", r"google\.[a-z.]+|goo\.gl", r"^/maps"),
    ("bing_places", r"bing\.com", r"^/maps"),
    ("bing_places", r"bingplaces\.com", ""),
    ("apple_maps", r"maps\.apple\.com", ""),
    ("facebook", r"(?:m\.)?facebook\.com|fb\.com", r"^/(?!sharer|share|dialog|plugins|tr\b)[^/]+"),
    ("instagram", r"instagram\.com", r"^/(?!p/|share)[^/]+"),
    ("linkedin", r"(?:[a-z]{2}\.)?linkedin\.com", r"^/(?:company|in|school)/"),
    ("yellow_pages", r"yellowpages\.com\.au", r"^/."),
    ("true_local", r"truelocal\.com\.au", r"^/."),
    ("hipages", r"hipages\.com\.au", r"^/."),
    ("serviceseeking", r"serviceseeking\.com\.au", r"^/."),
    ("oneflare", r"oneflare\.com\.au", r"^/."),
    ("word_of_mouth", r"wordofmouth\.com\.au", r"^/."),
    ("houzz", r"houzz\.com(?:\.au)?", r"^/."),
]
LISTING_LABELS = {
    "google_business_profile": "Google Business Profile",
    "bing_places": "Bing Places / Bing Maps",
    "apple_maps": "Apple Maps",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "linkedin": "LinkedIn",
    "yellow_pages": "Yellow Pages AU",
    "true_local": "True Local",
    "hipages": "hipages",
    "serviceseeking": "ServiceSeeking",
    "oneflare": "Oneflare",
    "word_of_mouth": "Word of Mouth",
    "houzz": "Houzz",
}
LOCATION_PAGE = re.compile(
    r"/(?:locations?|service-areas?|areas?(?:-we)?-serve[ds]?|suburbs?|regions?|near-me)(?:[/.-]|$)",
    re.I,
)
PHONE = re.compile(r"(?:\+?61|\b0)[\d ()-]{7,14}\d")
POSTCODE = re.compile(r"\b(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\s+(\d{4})\b")
READINESS = [
    ("gsc", "Google Search Console", "Search queries, clicks, impressions, indexing and sitemaps"),
    ("ga4", "Google Analytics 4", "Traffic sources, key events/conversions and engagement"),
    (
        "google_business_profile",
        "Google Business Profile",
        "Profile completeness, reviews, calls, direction requests and Maps/Search views",
    ),
    ("bing_wmt", "Bing Webmaster Tools", "Bing search performance, crawl errors and index status"),
    (
        "wordpress",
        "WordPress / WP-CLI (+ Site Health)",
        "Core, plugin and theme versions, pending updates and Site Health status",
    ),
    (
        "google_workspace",
        "Google Workspace (ScubaGoggles)",
        "Workspace security settings against CISA SCuBA baselines",
    ),
    (
        "m365",
        "Microsoft 365 (ScubaGear)",
        "Microsoft 365 security settings against CISA SCuBA baselines",
    ),
    ("clarity", "Microsoft Clarity", "Heatmaps, session recordings and rage/dead-click signals"),
]
RULES = [
    "The customer keeps ownership of every account and property; AMS is added as a user.",
    "AMS never asks for passwords, MFA codes or shared logins.",
    "Least privilege: read-only roles unless an agreed task needs more.",
    "Access is revocable by the customer at any time and is removed when the work ends.",
]
NOTES = [
    "Not detected means no public signal on the pages sampled; it does not mean an account "
    "does not exist. Tags may load through Google Tag Manager or only after cookie consent.",
    "Listings are recorded only when the website links to them (links or JSON-LD sameAs). "
    "Ownership, verification and management are not confirmed. Google Maps, Facebook, Apple, "
    "Bing and directory sites were not queried.",
    "Google Business Profile existence is not looked up; without a website link its status "
    "needs manual verification.",
    "Readiness rows describe what authorised access would add; only existing integrations "
    "(Search Console) can be connected by this engine.",
]


def detect(sources: list[tuple[str, str, list[str]]]) -> dict[str, dict[str, list[str]]]:
    """Match SIGNATURES against (evidence_id, url, lines) sources."""
    found: dict[str, dict[str, set[str]]] = {}
    for eid, url, lines in sources:
        for line in lines:
            for key, (_, _, patterns) in SIGNATURES.items():
                for pattern in patterns:
                    for match in re.finditer(pattern, line):
                        hit = found.setdefault(
                            key,
                            {
                                "ids": set(),
                                "signals": set(),
                                "evidence_ids": set(),
                                "urls": set(),
                                "loaded": set(),
                            },
                        )
                        if match.groups() and match.group(1):
                            hit["ids"].add(match.group(1))
                        hit["signals"].add(match.group(0)[:120])
                        hit["evidence_ids"].add(eid)
                        hit["urls"].add(url)
                        # Only a request the page actually made confirms that a tag loaded.
                        if line.startswith("request:"):
                            hit["loaded"].add(match.group(0)[:120])
    return {key: {k: sorted(v) for k, v in hit.items()} for key, hit in found.items()}


def resolve(href: Any, base: str) -> str | None:
    """Absolute http(s) URL for a link or sameAs value, or None when it is unusable."""
    if not isinstance(href, str) or not href.strip():
        return None
    try:
        url = urljoin(base, href.strip())
        parts = urlsplit(url)
        _ = parts.port  # Raises ValueError for a malformed port.
    except ValueError:
        return None
    return url if parts.scheme in {"http", "https"} and parts.hostname else None


def classify_listing(url: str) -> str | None:
    try:
        parts = urlsplit(str(url).strip())
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    host = (hostname or "").removeprefix("www.")
    for key, hosts, path in LISTINGS:
        if re.fullmatch(hosts, host) and re.search(path, parts.path or "/"):
            return key
    return None


def jsonld_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from jsonld_nodes(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (list, dict)):
                yield from jsonld_nodes(item)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([] if value is None else [value])


def phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return "0" + digits[2:] if digits.startswith("61") and len(digits) == 11 else digits


def page_lines(record: dict[str, Any]) -> list[str]:
    page = record.get("page", {})
    lines = [
        f"meta:{str(m.get('name') or '').lower()}={m.get('content') or ''}"
        for m in page.get("meta", [])
    ]
    for key in ["script_srcs", "tag_snippets", "resource_links"]:
        lines += [str(v) for v in page.get(key, [])]
    return lines + [f"request:{v}" for v in record.get("requests", [])]


def consent_context(pages: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Consent managers and consent-gated scripts seen on the sampled pages."""
    managers, gated = set(), set()
    for _, record in pages:
        page = record.get("page", {})
        text = "\n".join(page_lines(record) + [str(v) for v in page.get("gated_scripts", [])])
        managers |= {n for n, p in CONSENT_MANAGERS.items() if re.search(p, text, re.I)}
        gated |= {str(v)[:120] for v in page.get("gated_scripts", [])}
    return {"managers": sorted(managers), "gated_scripts": sorted(gated)}


def nap_check(pages: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Compare structured NAP with observed values.

    Only a concretely different observed value is a conflict. A value missing from the
    (possibly truncated) visible text is "not_found", which is never a confirmed mismatch.
    """
    entities, site_phones, postcodes, text = [], {}, set(), ""
    for _, record in pages:
        page = record.get("page", {})
        for node in jsonld_nodes(page.get("json_ld", [])):
            try:
                types = [str(t) for t in as_list(node.get("@type"))]
                if "PostalAddress" in types or not (node.get("telephone") or node.get("address")):
                    continue
                address = next((a for a in as_list(node.get("address")) if isinstance(a, dict)), {})
                entity = {
                    "type": types,
                    "name": str(node.get("name") or ""),
                    "telephone": [str(t) for t in as_list(node.get("telephone"))],
                    "street": str(address.get("streetAddress") or ""),
                    "postcode": str(address.get("postalCode") or ""),
                }
            except (AttributeError, TypeError):
                continue
            if entity not in entities:
                entities.append(entity)
        for contact in page.get("contacts", []):
            href = str(contact.get("href", ""))
            if href.startswith("tel:"):
                site_phones.setdefault(phone_key(href), contact.get("text") or href[4:])
        observed = "\n".join(
            [str(page.get("visible_text_summary", ""))]
            + [str(v) for v in page.get("nap_candidates", [])]
        )
        for match in PHONE.finditer(observed):
            site_phones.setdefault(phone_key(match.group(0)), match.group(0).strip())
        postcodes |= set(POSTCODE.findall(observed))
        text += "\n" + observed
    folded = " ".join(text.casefold().split())
    checks = []

    def add(field: str, value: str, result: str, observed: list[str] | None = None) -> None:
        checks.append(
            {"field": field, "structured": value, "result": result, "observed": observed or []}
        )

    for entity in entities:
        for phone in entity["telephone"]:
            if phone_key(phone) in site_phones:
                add("telephone", phone, "match")
            elif site_phones:
                add("telephone", phone, "conflict", sorted(site_phones.values()))
            else:
                add("telephone", phone, "not_found")
        postcode = entity["postcode"]
        if postcode:
            if postcode in postcodes:
                add("postcode", postcode, "match")
            elif postcodes:
                add("postcode", postcode, "conflict", sorted(postcodes))
            else:
                add("postcode", postcode, "not_found")
        for field in ["name", "street"]:
            value = entity[field]
            if value:
                found = " ".join(value.casefold().split()) in folded
                add(field, value, "match" if found else "not_found")
    results = {c["result"] for c in checks}
    if not entities:
        status = "Unknown (no structured business data)"
    elif "conflict" in results:
        status = "Inconsistent"
    elif "not_found" in results or not checks:
        status = UNKNOWN
    else:
        status = "Consistent"
    return {"status": status, "entities": entities, "checks": checks}


def page_evidence(browser: ModuleResult | None) -> list[tuple[str, dict[str, Any]]]:
    if browser is None:
        return []
    return [
        (e.id, e.data)
        for e in browser.evidence
        if e.description.startswith("Rendered page evidence") and isinstance(e.data, dict)
    ]


def dns_evidence(domain: ModuleResult | None) -> dict[str, tuple[str, dict[str, Any]]] | None:
    if domain is None or domain.status == "skipped":
        return None
    found = {}
    for e in domain.evidence:
        for kind in ["MX", "TXT"]:
            if e.description.startswith(f"DNS {kind} lookup") and isinstance(e.data, dict):
                found[kind] = (e.id, e.data)
    return found or None


def gsc_state(ctx: Context, gsc: ModuleResult | None) -> tuple[str, list[str], str]:
    if gsc and gsc.status == "ok" and gsc.evidence:
        data = gsc.evidence[0].data if isinstance(gsc.evidence[0].data, dict) else {}
        prop = str(data.get("property", ""))
        return f"Connected & verified ({prop})", [gsc.evidence[0].id], prop
    if "gsc" in ctx.connections:
        reason = gsc.reason if gsc else "Search Console module did not run"
        return f"{NOT_CONNECTED} ({reason})", [], ""
    return NOT_CONNECTED, [], ""


def platform_row(key: str, label: str, group: str, status: str, **fields: Any) -> dict[str, Any]:
    detected, missing = ACTIONS.get(key) or DEFAULT_ACTIONS.get(group, ("", "None"))
    action = {DETECTED: detected, NOT_DETECTED: missing}.get(status, "Verify manually")
    row = {
        "key": key,
        "platform": label,
        "group": group,
        "status": status,
        "connected": NOT_CONNECTED if key in CONNECTABLE else "n/a",
        "ids": [],
        "evidence_ids": [],
        "detail": "",
        "action": action,
    }
    row.update(fields)
    return row


def checklist(readiness: list[dict[str, Any]], email: str) -> list[dict[str, Any]]:
    items = []
    for row in readiness:
        key, status = row["key"], row["status"]
        ids = ", ".join(row.get("ids", []))
        if key == "gsc":
            if row["access"].startswith("Connected"):
                action, role = (
                    "Use the existing read-only Search Console connection; no further access.",
                    "Existing connection",
                )
            elif status == DETECTED:
                action, role = (
                    f"Add {email} as a Restricted user on the existing Search Console property "
                    "(Full user only if AMS will submit sitemaps or fixes).",
                    "Restricted user",
                )
            else:
                action, role = (
                    "Confirm whether a Search Console property exists. If it does, add "
                    f"{email} as a Restricted user. If not, the customer creates a "
                    "customer-owned Domain property verified by DNS TXT record, then adds "
                    f"{email}.",
                    "Restricted user",
                )
        elif key == "ga4":
            if status == DETECTED:
                action = f"Grant {email} the Viewer role on the GA4 property ({ids})."
            elif status == UNKNOWN:
                seen = f" ({ids})" if ids else ""
                action = (
                    f"GA4{seen} could not be confirmed loading (consent-gated or via Google Tag "
                    f"Manager). Confirm with the customer; if used, grant {email} the Viewer role "
                    "on the GA4 property."
                )
            else:
                action = (
                    f"Confirm whether the website uses GA4. If it does, grant {email} the "
                    "Viewer role; if not, flag analytics setup for verification."
                )
            role = "Viewer"
        elif key == "google_business_profile":
            linked = f" (linked from the website: {ids})" if ids else ""
            action, role = (
                f"Confirm the business owns its Google Business Profile{linked}, then add "
                f"{email} as a Manager (not Owner).",
                "Manager",
            )
        elif key == "bing_wmt":
            action, role = (
                f"If the site is in Bing Webmaster Tools, add {email} as a Read-only user; "
                "if not, the customer can import it from Search Console.",
                "Read-only user",
            )
        elif status != DETECTED and not (key == "clarity" and status == UNKNOWN and ids):
            continue
        elif key == "wordpress":
            action, role = (
                "Only if WordPress review is in scope and justified: the customer creates a "
                f"dedicated, named, revocable account for {email} (Site Health needs "
                "Administrator; remove it after the review). WP-CLI only through "
                "hosting-level access by separate arrangement.",
                "Dedicated revocable account",
            )
        elif key == "clarity":
            action, role = (
                f"Confirm the Clarity project ({ids}) is in use, then add {email} as a Team "
                "member.",
                "Team member",
            )
        elif key == "google_workspace":
            action, role = (
                "Only if security review is in scope: run ScubaGoggles with a customer "
                f"administrator, or grant {email} time-limited read-only admin access.",
                "Read-only, time-limited",
            )
        else:
            action, role = (
                "Only if security review is in scope: assign "
                f"{email} the Global Reader role for a time-limited ScubaGear review.",
                "Global Reader, time-limited",
            )
        items.append(
            {
                "service": row["service"],
                "action": action,
                "access": role,
                "condition": "Only if security review is in scope"
                if key in {"google_workspace", "m365"}
                else "",
                "evidence_ids": row["evidence_ids"],
            }
        )
    return items


def fetch(url: str) -> dict[str, Any]:
    """One bounded GET: same-origin redirects only (two at most), 10 s deadline, 2 MB cap."""
    deadline = time.monotonic() + SITEMAP_TIMEOUT
    try:
        with httpx.Client(timeout=SITEMAP_TIMEOUT, follow_redirects=False) as client:
            for _ in range(3):
                with client.stream("GET", url) as response:
                    location = response.headers.get("location", "")
                    if response.is_redirect and location:
                        target = urljoin(url, location)
                        parts = urlsplit(target)
                        if (parts.hostname or "", parts.port) != (
                            urlsplit(url).hostname or "",
                            urlsplit(url).port,
                        ) or parts.scheme not in {"http", "https"}:
                            return {"url": url, "status": response.status_code, "redirect": target}
                        url = target
                        continue
                    body, truncated = b"", False
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > SITEMAP_MAX_BYTES or time.monotonic() > deadline:
                            truncated = True
                            break
                    return {
                        "url": url,
                        "status": response.status_code,
                        "content_type": response.headers.get("content-type", ""),
                        "body": body[:SITEMAP_MAX_BYTES].decode("utf-8", "replace"),
                        "truncated": truncated,
                    }
            return {"url": url, "error": "TooManyRedirects"}
    except (httpx.HTTPError, ValueError) as exc:
        return {"url": url, "error": type(exc).__name__}


def parse_sitemap(body: str) -> tuple[str, int]:
    """Root type and child count, by counting tags (no XML parser, no recursion)."""
    if re.search(r"<(?:\w+:)?sitemapindex\b", body):
        return "sitemapindex", len(re.findall(r"<(?:\w+:)?sitemap\b", body))
    if re.search(r"<(?:\w+:)?urlset\b", body):
        return "urlset", len(re.findall(r"<(?:\w+:)?loc\b", body))
    return "other", 0


def sitemap_check(ctx: Context) -> dict[str, Any]:
    """robots.txt Sitemap lines, then the first same-origin sitemap (or /sitemap.xml), once each.

    SiteOne loads robots.txt but does not keep its content, so robots.txt is fetched here.
    """
    parts = urlsplit(ctx.target)
    origin = f"{parts.scheme}://{parts.netloc}"
    robots = fetch(origin + "/robots.txt")
    declared = []
    if robots.get("status") == 200:
        for match in re.finditer(r"(?im)^\s*sitemap\s*:\s*(\S+)", robots.get("body", "")):
            url = resolve(match.group(1), origin + "/robots.txt")
            if url:
                declared.append(url)
    data: dict[str, Any] = {
        "robots_url": origin + "/robots.txt",
        "robots_status": robots.get("status") or robots.get("error"),
        "declared": declared,
    }
    same_origin = [u for u in declared if urlsplit(u).netloc == parts.netloc]
    if declared and not same_origin:
        return data | {
            "status": NOT_ASSESSED,
            "detail": "robots.txt declares a sitemap on another host; it was not fetched",
        }
    url = same_origin[0] if same_origin else origin + "/sitemap.xml"
    response = fetch(url)
    data |= {
        "sitemap_url": url,
        "source": "robots.txt Sitemap line" if same_origin else "default /sitemap.xml",
        "http_status": response.get("status"),
        "content_type": response.get("content_type", ""),
        "truncated": response.get("truncated", False),
    }
    if "error" in response or response.get("redirect"):
        reason = response.get("error") or f"redirect to {response['redirect']} not followed"
        return data | {"status": NOT_ASSESSED, "detail": f"Sitemap fetch failed: {reason}"}
    status_code = response["status"]
    if status_code in {404, 410}:
        return data | {
            "status": WARNING,
            "detail": f"No sitemap at {url} (HTTP {status_code})"
            + ("" if declared else "; robots.txt declares none"),
        }
    if status_code != 200:
        return data | {
            "status": NOT_ASSESSED,
            "detail": f"Sitemap fetch returned HTTP {status_code}",
        }
    kind, count = parse_sitemap(response.get("body", ""))
    data |= {"kind": kind, "url_count" if kind == "urlset" else "child_sitemaps": count}
    if kind == "other":
        return data | {"status": WARNING, "detail": f"{url} is not an XML sitemap or index"}
    noun = "URLs" if kind == "urlset" else "child sitemaps"
    return data | {
        "status": PASS,
        "detail": f"{'Sitemap index' if kind == 'sitemapindex' else 'Sitemap'} at {url} "
        f"({data['source']}) lists {count} {noun}"
        + (" (truncated at 2 MB)" if data["truncated"] else ""),
    }


def build(ctx: Context, results: list[ModuleResult], result: ModuleResult) -> dict[str, Any]:
    modules = {r.name: r for r in results}
    pages = page_evidence(modules.get("browser"))
    dns = dns_evidence(modules.get("domain_email"))
    siteone = modules.get("siteone")
    gsc_text, gsc_eids, prop = gsc_state(ctx, modules.get("gsc"))
    assessed = bool(pages or dns)
    page_urls = sorted({str(record.get("url", "")) for _, record in pages})
    coverage = ""
    if assessed:
        coverage = result.add_evidence(
            ctx.target,
            "Digital presence detection coverage (no additional requests)",
            {
                "pages_sampled": page_urls,
                "browser_evidence_ids": [eid for eid, _ in pages],
                "dns_evidence_ids": [eid for eid, _ in (dns or {}).values()],
            },
        )

    sources = [(eid, str(record.get("url", "")), page_lines(record)) for eid, record in pages]
    if pages and siteone:
        for e in siteone.evidence:
            if e.description == "SiteOne headers-values table" and isinstance(e.data, list):
                lines = [
                    f"header:{str(r.get('header', '')).lower()}={r.get('value', '')}"
                    for r in e.data
                ]
                sources.append((e.id, e.url, lines))
    hits = detect(sources)
    consent = consent_context(pages)
    gated = bool(consent["managers"] or consent["gated_scripts"])
    if dns and "TXT" in dns:
        eid, data = dns["TXT"]
        tokens = [
            m.group(1)
            for r in data.get("records", [])
            for m in [re.search(r"google-site-verification=([\w-]+)", str(r))]
            if m
        ]
        if tokens:
            hit = hits.setdefault(
                "gsc", {"ids": [], "signals": [], "evidence_ids": [], "urls": [], "loaded": []}
            )
            hit["ids"] = sorted(set(hit["ids"]) | set(tokens))
            hit["signals"] = sorted(set(hit["signals"]) | {"DNS TXT google-site-verification"})
            hit["evidence_ids"] = sorted(set(hit["evidence_ids"]) | {eid})
            hit["urls"] = sorted(set(hit["urls"]) | {f"dns:{ctx.host}"})

    platforms, refs = [], {}
    for key, (label, group, _) in SIGNATURES.items():
        hit = hits.get(key)
        if hit:
            refs[key] = result.add_evidence(
                hit["urls"][0],
                f"Platform signal: {label}",
                {"platform": key, "source_evidence_ids": hit["evidence_ids"], **hit},
            )
            detail = "Seen on " + ", ".join(hit["urls"][:3])
            if key == "wordpress" and hit["ids"]:
                detail += f"; version {hit['ids'][0]} publicly exposed (info)"
            status = DETECTED
            if group in TAG_GROUPS and gated and not hit["loaded"]:
                # Markup alone does not show the tag runs when consent can hold it back.
                status = UNKNOWN
                detail = (
                    f"Tag present on {', '.join(hit['urls'][:3])} but consent-gated and not seen "
                    "loading on the sampled pages"
                )
            platforms.append(
                platform_row(
                    key,
                    label,
                    group,
                    status,
                    # Verification tokens and the WordPress version are not account IDs.
                    ids=[] if key in {"gsc", "bing_wmt", "wordpress"} else hit["ids"],
                    signals=hit["signals"][:5],
                    evidence_ids=[refs[key]],
                    detail=detail,
                )
            )
        elif not pages:
            platforms.append(
                platform_row(
                    key, label, group, NOT_ASSESSED, detail="No rendered page evidence collected"
                )
            )
        elif group in TAG_GROUPS and gated:
            platforms.append(
                platform_row(
                    key,
                    label,
                    group,
                    UNKNOWN,
                    evidence_ids=[coverage],
                    detail="Not seen, but a consent manager or consent-gated tags are present; "
                    "the tag may load only after consent",
                )
            )
        elif key == "ga4" and "gtm" in hits:
            platforms.append(
                platform_row(
                    key,
                    label,
                    group,
                    UNKNOWN,
                    evidence_ids=[coverage],
                    detail="Google Tag Manager is present but GA4 was not seen directly; it may "
                    "load through GTM or after consent",
                )
            )
        else:
            detail = f"No public signal on {len(page_urls)} sampled pages"
            if group in TAG_GROUPS:
                detail += "; tags may load through GTM or only after consent"
            if key == "gsc" and dns is None:
                detail += "; DNS TXT not assessed"
            platforms.append(
                platform_row(
                    key, label, group, NOT_DETECTED, evidence_ids=[coverage], detail=detail
                )
            )
    for row in platforms:
        if row["key"] == "gsc":
            # Connection state only; GSC data stays in the authorised findings below.
            row["connected"] = gsc_text
    for key, (label, mx_patterns, txt_patterns) in EMAIL_PLATFORMS.items():
        if dns is None or not {"MX", "TXT"} <= dns.keys():
            platforms.append(
                platform_row(
                    key,
                    label,
                    "email",
                    NOT_ASSESSED,
                    detail="Public DNS evidence unavailable (domain_email not assessed)",
                )
            )
            continue
        (mx_eid, mx), (txt_eid, txt) = dns["MX"], dns["TXT"]
        signals = [
            str(r)
            for r in mx.get("records", [])
            if any(re.search(p, str(r).split()[-1]) for p in mx_patterns)
        ] + [
            str(r)
            for r in txt.get("records", [])
            if any(re.search(p, str(r)) for p in txt_patterns)
        ]
        if signals:
            refs[key] = result.add_evidence(
                f"dns:{ctx.host}",
                f"Email platform signal: {label}",
                {"platform": key, "signals": signals, "source_evidence_ids": [mx_eid, txt_eid]},
            )
            status, eids, detail = (
                DETECTED,
                [refs[key]],
                "MX/SPF/verification TXT: " + "; ".join(signals[:3]),
            )
        elif "error" in {mx.get("status"), txt.get("status")}:
            status, eids, detail = UNKNOWN, [mx_eid, txt_eid], "MX or TXT lookup failed"
        else:
            status, eids, detail = NOT_DETECTED, [mx_eid, txt_eid], "No MX/SPF/TXT signal"
        platforms.append(
            platform_row(
                key, label, "email", status, signals=signals, evidence_ids=eids, detail=detail
            )
        )
    by_key = {row["key"]: row for row in platforms}

    links: dict[str, list[dict[str, str]]] = {}

    def add_link(value: Any, page: str, source: str, eid: str) -> None:
        # Resolved and classified per item: one malformed URL never stops the rest.
        href = resolve(value, page or ctx.target)
        key = classify_listing(href) if href else None
        if (
            href
            and key
            and all((item["href"], item["source"]) != (href, source) for item in links.get(key, []))
        ):
            links.setdefault(key, []).append(
                {"href": href, "page": page, "source": source, "source_evidence_id": eid}
            )

    same_as: set[str] = set()
    for eid, record in pages:
        page = record.get("page", {})
        page_url = str(record.get("url", "")) or ctx.target
        for href in page.get("links", []):
            add_link(href, page_url, "website link", eid)
        for node in jsonld_nodes(page.get("json_ld", [])):
            for href in as_list(node.get("sameAs")):
                resolved = resolve(href, page_url)
                if resolved:
                    same_as.add(resolved)
                add_link(href, page_url, "JSON-LD sameAs", eid)
    if pages and siteone:
        for e in siteone.evidence:
            if e.description == "SiteOne external-urls table" and isinstance(e.data, list):
                for item in e.data:
                    if isinstance(item, dict):
                        add_link(item.get("url"), "", "crawl external link", e.id)
    listings = []
    for key, label in LISTING_LABELS.items():
        found = links.get(key, [])
        if found:
            eid = result.add_evidence(found[0]["href"], f"Listing link: {label}", {"links": found})
            refs[key] = eid
            status, eids = LINKED, [eid]
        elif not pages:
            status, eids = NOT_ASSESSED, []
        else:
            status = UNKNOWN_MANUAL if key == "google_business_profile" else NOT_DETECTED
            eids = [coverage]
        listings.append(
            {
                "key": key,
                "listing": label,
                "status": status,
                "sources": sorted({f["source"] for f in found}),
                "urls": list(dict.fromkeys(f["href"] for f in found)),
                "evidence_ids": eids,
                "ownership": OWNERSHIP,
            }
        )

    signals: list[dict[str, Any]] = []
    if pages:
        nap = nap_check(pages)
        nap_eid = result.add_evidence(ctx.target, "NAP consistency comparison", nap)
        conflicts = [c for c in nap["checks"] if c["result"] == "conflict"]
        unseen = [c for c in nap["checks"] if c["result"] == "not_found"]
        detail = "; ".join(
            f"structured {c['field']} '{c['structured']}' differs from the site "
            f"({', '.join(c['observed'])})"
            for c in conflicts
        ) or "; ".join(
            f"structured {c['field']} '{c['structured']}' not seen in the sampled page text "
            "(text may be truncated)"
            for c in unseen
        )
        if not detail and nap["checks"]:
            detail = "Structured name, phone and address match the sampled pages"
        signals.append(
            {
                "signal": "NAP consistency (structured data vs visible text and phone links)",
                "status": nap["status"],
                "detail": detail or "No structured business name, address or phone to compare",
                "evidence_ids": [nap_eid],
            }
        )
        types = sorted({t for e in nap["entities"] for t in e["type"]})
        signals.append(
            {
                "signal": "Structured business entity (JSON-LD)",
                "status": DETECTED if types else NOT_DETECTED,
                "detail": ", ".join(types) + (f"; sameAs links: {len(same_as)}" if types else "")
                or "No JSON-LD entity with a name, address or phone",
                "evidence_ids": [nap_eid],
            }
        )
    if ctx.pages and (pages or siteone):
        matches = [u for u in ctx.pages if LOCATION_PAGE.search(urlsplit(u).path)]
        location_eid = result.add_evidence(
            ctx.target,
            "Location / service-area page scan of crawled URLs",
            {"pages_scanned": len(ctx.pages), "matches": matches},
        )
        signals.append(
            {
                "signal": "Location / service-area pages",
                "status": DETECTED if matches else NOT_DETECTED,
                "detail": ", ".join(matches[:5])
                or f"No location or service-area URL among {len(ctx.pages)} crawled pages",
                "evidence_ids": [location_eid],
            }
        )
    if siteone:
        for e in siteone.evidence:
            if e.description == "SiteOne robots.txt load":
                code = re.search(r"status code (\d+)", str(e.data.get("text", "")))
                signals.append(
                    {
                        "signal": "robots.txt",
                        "status": DETECTED if code and code.group(1) == "200" else NOT_DETECTED,
                        "detail": str(e.data.get("text", "")),
                        "evidence_ids": [e.id],
                    }
                )
            elif e.description == "SiteOne canonical link summary":
                signals.append(
                    {
                        "signal": "Canonical links",
                        "status": "Complete" if e.data.get("status") == "OK" else "Incomplete",
                        "detail": str(e.data.get("text", "")),
                        "evidence_ids": [e.id],
                    }
                )
    if siteone and siteone.status == "ok":
        sitemap = sitemap_check(ctx)
        sitemap_eid = result.add_evidence(
            sitemap.get("sitemap_url") or ctx.target,
            "Bounded robots.txt and XML sitemap fetch",
            sitemap,
        )
        signals.append(
            {
                "signal": "XML sitemap",
                "status": sitemap["status"],
                "detail": sitemap["detail"],
                "evidence_ids": [sitemap_eid],
            }
        )
    gsc_module = modules.get("gsc")
    readiness = []
    for key, service, insight in READINESS:
        if key == "google_business_profile":
            source = next(r for r in listings if r["key"] == key)
            status, ids = (
                (DETECTED if source["status"] == LINKED else source["status"]),
                source["urls"],
            )
        else:
            source = by_key[key]
            # Name meta verification signals without repeating their tokens.
            signals_seen = [
                re.sub(r"^(meta:[^=]+)=.*", r"\1", s) for s in source.get("signals", [])
            ]
            status, ids = source["status"], source["ids"] or signals_seen[:2]
        if key in {"google_workspace", "m365"} and status != DETECTED:
            continue
        public = source["status"]
        if status == DETECTED and ids:
            public += ": " + ", ".join(ids)
        elif status == UNKNOWN and source.get("detail"):
            public += (f" ({', '.join(ids)})" if ids else "") + f": {source['detail']}"
        readiness.append(
            {
                "key": key,
                "service": service,
                "status": status,
                "ids": ids,
                "public_evidence": public,
                "evidence_ids": source["evidence_ids"],
                "confirmation": "Account existence and customer ownership confirmation needed",
                "access": gsc_text if key == "gsc" else NOT_CONNECTED,
                "insights": insight,
            }
        )
    email = ctx.config.get("AMS_ACCESS_EMAIL") or PLACEHOLDER_EMAIL
    items = checklist(readiness, email)
    steps = {item["service"]: item for item in items}
    for row in readiness:
        item = steps.get(row["service"])
        if item is None:
            row["next_step"] = "None: no public signal for this platform"
        elif item["access"] == "Existing connection":
            row["next_step"] = "Use the existing connection"
        else:
            row["next_step"] = f"Confirm ownership; grant AMS {item['access']}" + (
                f" ({item['condition'].lower()})" if item["condition"] else ""
            )

    authorised = []
    if gsc_eids and gsc_module:
        data = gsc_module.evidence[0].data
        authorised.append(
            {
                "source": "Google Search Console",
                "detail": f"Property {prop}, {data.get('start_date')} to {data.get('end_date')}: "
                f"{len(data.get('query', {}).get('rows', []))} query rows and "
                f"{len(data.get('page', {}).get('rows', []))} page rows (top 100 each).",
                "evidence_ids": gsc_eids,
            }
        )
        sitemaps = data.get("sitemaps", {}).get("sitemap", [])
        authorised.append(
            {
                "source": "Google Search Console sitemaps",
                "detail": f"{len(sitemaps)} sitemap(s) submitted"
                + (
                    ": " + ", ".join(str(m.get("path", "")) for m in sitemaps[:3])
                    if sitemaps
                    else ""
                )
                + ".",
                "evidence_ids": gsc_eids,
            }
        )

    if pages:
        add_findings(result, by_key, listings, signals, same_as, coverage, refs)
    sitemap_row = next((r for r in signals if r["signal"] == "XML sitemap"), None)
    if sitemap_row and sitemap_row["status"] == WARNING:
        result.finding(
            "sitemap-missing",
            "XML sitemap missing or invalid",
            "seo",
            sitemap_row["evidence_ids"],
            sitemap_row["detail"] + ".",
            "Publish an XML sitemap of indexable pages and declare it in robots.txt.",
            severity="low",
            urls=[ctx.target],
        )
    return {
        "status": "assessed" if assessed else NOT_ASSESSED,
        "pages_sampled": page_urls,
        "consent": consent,
        "platforms": platforms,
        "listings": listings,
        "signals": signals,
        "readiness": readiness,
        "checklist": items,
        "rules": RULES,
        "authorised": authorised,
        "access_email_configured": bool(ctx.config.get("AMS_ACCESS_EMAIL")),
        "notes": NOTES,
    }


def add_findings(
    result: ModuleResult,
    platforms: dict[str, dict[str, Any]],
    listings: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    same_as: set[str],
    coverage: str,
    refs: dict[str, str],
) -> None:
    nap = next((s for s in signals if s["signal"].startswith("NAP")), None)
    if nap and nap["status"] == "Inconsistent":
        # One deterministic finding per mismatch; AI is told to enrich it, not repeat it.
        result.finding(
            "nap-inconsistent",
            "Business name, address or phone differs between structured data and the page",
            "local_seo",
            nap["evidence_ids"],
            nap["detail"] + ".",
            "Use one canonical name, address and phone number in structured data, the footer, "
            "contact page and external listings.",
            ams_service="Local SEO",
        )
    elif nap and nap["status"] == UNKNOWN:
        result.finding(
            "nap-unverified",
            "Structured business details not confirmed on the page",
            "local_seo",
            nap["evidence_ids"],
            nap["detail"] + ".",
            "Check that the structured name, address and phone appear on the website exactly.",
            severity="low",
            confidence="needs_verification",
            ams_service="Local SEO",
        )
    tags = ["ga4", "ua", "gtm", "clarity", "hotjar"]
    if all(platforms[k]["status"] == NOT_DETECTED for k in tags):
        result.finding(
            "tracking:no-analytics",
            "No analytics tag detected on the sampled pages",
            "presence",
            [coverage],
            "No GA4, Google Tag Manager, Clarity or Hotjar signal was seen on the sampled pages. "
            "Analytics may still load after consent or from an unsampled template.",
            "Confirm with the customer whether analytics exists; if not, plan a customer-owned, "
            "consent-aware GA4 property.",
            kind="opportunity",
            confidence="needs_verification",
            ams_service="Analytics & tracking",
        )
    unknown = [r for r in platforms.values() if r["group"] in TAG_GROUPS and r["status"] == UNKNOWN]
    if unknown:
        result.finding(
            "tracking:unconfirmed",
            "Analytics or tracking tags could not be confirmed",
            "presence",
            sorted({e for r in unknown for e in r["evidence_ids"]}),
            "; ".join(f"{r['platform']}: {r['detail']}" for r in unknown) + ".",
            "Verify with the customer in Google Tag Manager, Tag Assistant or the consent "
            "manager which tags run after consent.",
            severity="low",
            kind="opportunity",
            confidence="needs_verification",
            ams_service="Analytics & tracking",
        )
    if platforms["ua"]["status"] == DETECTED:
        result.finding(
            "tracking:legacy-ua",
            "Legacy Universal Analytics tag still present",
            "presence",
            [refs["ua"]],
            "A Universal Analytics (UA-) tag or library was found. Universal Analytics no longer "
            "processes data.",
            "Remove the legacy tag and confirm GA4 measures the same key events.",
            severity="low",
            ams_service="Analytics & tracking",
        )
    gbp = next(r for r in listings if r["key"] == "google_business_profile")
    if gbp["status"] == UNKNOWN_MANUAL:
        result.finding(
            "listing:gbp-not-linked",
            "Google Business Profile not linked from the website",
            "presence",
            [coverage],
            "No Google Business Profile or Google Maps link was found on the sampled pages. "
            "Whether a profile exists was not checked.",
            "Confirm the profile exists and is customer-owned, then link it from the contact "
            "page and structured data.",
            severity="low",
            kind="opportunity",
            confidence="needs_verification",
            ams_service="Local SEO",
        )
    entity = next((s for s in signals if s["signal"].startswith("Structured business")), None)
    missing = [
        r["listing"] for r in listings if r["status"] == LINKED and not set(r["urls"]) & same_as
    ]
    if entity and entity["status"] == DETECTED and missing:
        result.finding(
            "listing:sameas-incomplete",
            "Linked profiles missing from structured-data sameAs",
            "entity",
            [refs[r["key"]] for r in listings if r["listing"] in missing] + entity["evidence_ids"],
            "The website links to " + ", ".join(missing) + " but the business JSON-LD does not "
            "list them in sameAs.",
            "Add the customer-owned profile URLs to the business sameAs property.",
            severity="low",
            kind="opportunity",
            ams_service="Local SEO",
        )


def run(ctx: Context) -> ModuleResult:
    result = ModuleResult(name=name)
    try:
        tables = build(ctx, ctx.results, result)
    except Exception as exc:
        # Fail soft: the report shows NOT ASSESSED presence tables instead of losing the run.
        return ModuleResult(
            name=name,
            status="error",
            reason=f"Presence analysis failed ({type(exc).__name__}); other results unaffected",
        )
    if tables["status"] == NOT_ASSESSED:
        return ModuleResult(
            name=name,
            status="skipped",
            reason="Requires browser page evidence or public DNS evidence (domain_email)",
        )
    # Stored on the module result so results.json and re-rendering reuse the same tables.
    result.checks = [{"key": key, "value": value} for key, value in tables.items()]
    result.raw_paths = [ctx.raw(name, tables)]
    return result


def summary(results: list[ModuleResult], ctx: Context) -> dict[str, Any]:
    module = next((r for r in results if r.name == name), None)
    if module and module.status == "ok" and module.checks:
        data = {c["key"]: c["value"] for c in module.checks}
        data["reason"] = module.reason
        return data
    # Only connection state is reused here: page/DNS-derived rows must not cite unsaved evidence.
    data = build(ctx, [r for r in results if r.name == "gsc"], ModuleResult(name=name))
    data["reason"] = (
        "presence module did not run"
        if module is None
        else f"presence module {module.status}" + (f": {module.reason}" if module.reason else "")
    )
    return data
