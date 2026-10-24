"""Bound Google Business Profile reads. Official endpoint contracts are linked in README."""

import hashlib
import math
import re
from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from ams_assess.connectors.auth import google_authenticate, google_authorize
from ams_assess.connectors.core import (
    Connector,
    ConnectorContext,
    Endpoint,
    ErrorClassification,
    ReadOperation,
)
from ams_assess.connectors.errors import default_error_classifier
from ams_assess.connectors.privacy import EMAIL, redact, safe_url
from ams_assess.lifecycle import observe, site_key
from ams_assess.models import ModuleResult
from ams_assess.profile import phone_key

ACCOUNTS = "https://mybusinessaccountmanagement.googleapis.com/v1/"
INFO = "https://mybusinessbusinessinformation.googleapis.com/v1/"
REVIEWS = "https://mybusiness.googleapis.com/v4/"
PERFORMANCE = "https://businessprofileperformance.googleapis.com/v1/"
SCOPE = "https://www.googleapis.com/auth/business.manage"
ID = r"[1-9][0-9]{0,29}"
READ_MASK = (
    "name,title,categories,regularHours,specialHours,websiteUri,phoneNumbers,"
    "storefrontAddress,serviceArea,profile.description,metadata"
)
# https://developers.google.com/my-business/reference/performance/rest/v1/DailyMetric
METRICS = (
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    "CALL_CLICKS",
    "WEBSITE_CLICKS",
    "BUSINESS_DIRECTION_REQUESTS",
    "BUSINESS_BOOKINGS",
)
STARS = ("ONE", "TWO", "THREE", "FOUR", "FIVE")


def classify_error(status_code: int, body: Mapping[str, Any]) -> ErrorClassification | None:
    def zero_quota(value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(
                (key == "quota_limit_value" and str(item) == "0") or zero_quota(item)
                for key, item in value.items()
            )
        return isinstance(value, (list, tuple)) and any(zero_quota(item) for item in value)

    classified = default_error_classifier(status_code, body)
    if classified.status == "approval_required" or (
        status_code in {403, 429}
        and classified.status in {"permission_required", "rate_limited"}
        and zero_quota(body)
    ):
        return ErrorClassification(
            "approval_required",
            "GBP API access is not approved for this project (approval reason or zero quota). "
            "Submit Application for Basic API Access using the OAuth project's number: "
            "https://developers.google.com/my-business/content/prereqs . "
            "Do not request a quota increase for a zero-quota project.",
        )
    if classified.status == "permission_required":
        return ErrorClassification(
            "permission_required",
            "GBP requires business.manage OAuth scope and Manager (or existing Owner) access "
            "to the selected profile. Reviews require a verified location. For Workspace users, "
            "confirm the Google Business Profile service is enabled for the user.",
        )
    if classified.status == "api_not_enabled":
        return ErrorClassification(
            "api_not_enabled",
            "Enable the failed GBP API in the approved OAuth project. This connector uses "
            "My Business Account Management API, My Business Business Information API, "
            "Google My Business API (reviews), and Business Profile Performance API.",
        )
    return None


def normalize_resource(value: str) -> str:
    # A CID/place ID is NOT the API location ID. No implicit conversion or account aliases.
    if not re.fullmatch(r"locations/" + ID, value):
        raise ValueError("GBP resource must be locations/<API location ID> from list-resources")
    return value


def text(value: str) -> str:
    for _ in range(3):
        value = unquote(value)
    value = EMAIL.sub(
        lambda m: "[email:" + hashlib.sha256(m[0].lower().encode()).hexdigest()[:12] + "]",
        value,
    )
    return redact(value)


def metadata(raw: dict) -> dict:
    """Keep public identity metadata, never arbitrary provider fields/query parameters."""
    value = {k: raw[k] for k in ("placeId", "hasVoiceOfMerchant") if k in raw}
    maps = raw.get("mapsUri", "")
    parts = urlsplit(maps)
    value["mapsUri"] = safe_url(maps)
    # Preserve a numeric CID when supplied by Google; never derive the API location ID from it.
    cid = parse_qs(parts.query).get("cid", [""])[0]
    if re.fullmatch(r"[0-9]{1,20}", cid):
        value["maps_cid"] = cid
        value["mapsUri"] += "?cid=" + cid
    return redact(value)


def location_data(raw: dict) -> dict:
    value = {k: raw[k] for k in READ_MASK.split(",") if k in raw}
    value["name"] = normalize_resource(raw["name"])
    value["title"] = text(raw.get("title", ""))
    value["websiteUri"] = safe_url(raw.get("websiteUri", ""))
    value["profile"] = {"description": text(raw.get("profile", {}).get("description", ""))}
    value["metadata"] = metadata(raw.get("metadata", {}))
    # PostalAddress can also contain recipients/organization names; omit those personal fields.
    value["storefrontAddress"] = {
        k: v
        for k, v in raw.get("storefrontAddress", {}).items()
        if k in {"regionCode", "administrativeArea", "locality", "postalCode", "addressLines"}
    }
    return redact(value)


def pages(
    ctx: ConnectorContext,
    result: Endpoint,
    url: str,
    field: str,
    size: int,
    params: dict | None = None,
) -> Iterator[tuple[dict, list[dict]]]:
    token, seen = "", set()
    size = min(size, ctx.page_size)
    for _ in range(ctx.max_pages):
        query = {**(params or {}), "pageSize": size}
        if token:
            query["pageToken"] = token
        body = ctx.http.request("GET", url, params=query)
        rows = body.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Invalid list response")
        result.truncated |= len(rows) > size
        yield body, rows[:size]
        token = body.get("nextPageToken", "")
        if not isinstance(token, str):
            raise ValueError("Invalid pagination token")
        if not token or result.truncated:
            return
        if token in seen:
            result.truncated = True
            return
        seen.add(token)
    result.truncated = bool(token)


def accounts(ctx: ConnectorContext) -> Endpoint:
    url = ACCOUNTS + "accounts"

    def read(result: Endpoint) -> None:
        result.data["rows"] = []
        for _, batch in pages(ctx, result, url, "accounts", 20):
            for raw in batch:
                name = raw["name"]
                if not re.fullmatch(r"accounts/" + ID, name):
                    raise ValueError("Invalid account identity")
                if name not in result.data["rows"]:
                    result.data["rows"].append(name)
                    result.rows += 1

    return ctx.capture("accounts", url, read)


def locations(ctx: ConnectorContext, account: str, *, discovery: bool) -> Endpoint:
    url = INFO + account + "/locations"

    def read(result: Endpoint) -> None:
        result.data["rows"] = []
        mask = READ_MASK if discovery else "name"
        for _, batch in pages(ctx, result, url, "locations", 100, {"readMask": mask}):
            for raw in batch:
                name = normalize_resource(raw["name"])
                if discovery:
                    result.data["rows"].append(
                        {
                            "id": name,
                            "label": text(raw.get("title", "")),
                            "metadata": metadata(raw.get("metadata", {})),
                            "address": location_data(raw)["storefrontAddress"],
                        }
                    )
                elif name == ctx.resource:
                    result.data["rows"].append({"id": name, "account": account})
                result.rows += 1
                if not discovery and name == ctx.resource:
                    # This is an exact membership lookup, not a claim to list the entire account.
                    return

    return ctx.capture("locations:" + account, url, read)


def list_resources(ctx: ConnectorContext) -> Endpoint:
    result = Endpoint("resources", INFO + "accounts/*/locations", data={"rows": []})
    listed = accounts(ctx)
    seen = set()
    for account in listed.data.get("rows", []):
        found = locations(ctx, account, discovery=True)
        for row in found.data.get("rows", []):
            if row["id"] not in seen:
                seen.add(row["id"])
                result.data["rows"].append(row)
                result.rows += 1
                if result.rows >= ctx.max_pages * ctx.page_size:
                    result.truncated = True
                    break
        if result.truncated:
            break
    problems = [e for e in ctx.endpoints if not e.complete]
    if problems:
        result.truncated = True
        result.status = "connected_partial" if result.rows else problems[0].status
        result.detail = "; ".join(e.key + ": " + e.status + " " + e.detail for e in problems)
    return result


def test(ctx: ConnectorContext) -> Endpoint:
    url = INFO + normalize_resource(ctx.resource)

    def read(result: Endpoint) -> None:
        body = ctx.http.request("GET", url, params={"readMask": READ_MASK})
        if body.get("name") != ctx.resource:
            raise ValueError("Unexpected location identity")
        result.data = location_data(body)
        result.data["permission_evidence"] = "Authenticated location GET with business.manage"
        result.data["coverage_period"] = "Current location configuration at collection time"
        result.rows = 1
        category = result.data.get("categories", {}).get("primaryCategory", {})
        result.detail = "Current profile snapshot; primary category: " + text(
            category.get("displayName") or category.get("name") or "not returned"
        )

    return ctx.capture("location", url, read)


def review_parent(ctx: ConnectorContext) -> str:
    listed = accounts(ctx)
    for account in listed.data.get("rows", []):
        found = locations(ctx, account, discovery=False)
        if found.data.get("rows"):
            return account + "/" + ctx.resource
    return ""


def reviews(ctx: ConnectorContext, parent: str) -> Endpoint:
    url = REVIEWS + parent + "/reviews"

    def read(result: Endpoint) -> None:
        result.data = {
            "star_counts": dict.fromkeys(STARS, 0),
            "unanswered": 0,
            "replied": 0,
            "coverage_period": "Current review inventory (not limited to the performance period)",
        }
        seen = set()
        for body, batch in pages(ctx, result, url, "reviews", 50):
            if "totalReviewCount" in body:
                result.data["total_review_count"] = integer(body["totalReviewCount"])
            if "averageRating" in body:
                rating = float(body["averageRating"])
                if not math.isfinite(rating) or not 0 <= rating <= 5:
                    raise ValueError("Invalid rating")
                result.data["average_rating"] = rating
            for raw in batch:
                # Identifiers exist only in memory for deduplication; no reviewer or review text.
                identity = raw["reviewId"]
                if identity in seen:
                    result.truncated = True
                    continue
                seen.add(identity)
                star = raw["starRating"]
                if star not in STARS:
                    raise ValueError("Invalid star rating")
                result.data["star_counts"][star] += 1
                replied = isinstance(raw.get("reviewReply"), dict)
                result.data["replied" if replied else "unanswered"] += 1
                result.rows += 1
            result.detail = (
                "Current review inventory; Google total: "
                f"{result.data.get('total_review_count', 'not returned')}; "
                f"Google average rating: {result.data.get('average_rating', 'not returned')}; "
                f"retrieved: {result.rows}; no reply: {result.data['unanswered']}"
            )
        if result.data.get("total_review_count", result.rows) != result.rows:
            result.truncated = True

    return ctx.capture("reviews", url, read)


def integer(value: Any) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("Invalid count")
    return int(value)


def daily(ctx: ConnectorContext) -> Endpoint:
    url = PERFORMANCE + ctx.resource + ":fetchMultiDailyMetricsTimeSeries"

    def read(result: Endpoint) -> None:
        query: dict[str, Any] = {"dailyMetrics": list(METRICS)}
        for name, day in (("start_date", ctx.period.start), ("end_date", ctx.period.end)):
            for field in ("year", "month", "day"):
                query[f"dailyRange.{name}.{field}"] = getattr(day, field)
        body = ctx.http.request("GET", url, params=query)
        result.data = {"series": {}, "missing_metrics": [], "metric_coverage": {}}
        expected = (ctx.period.end - ctx.period.start).days + 1
        for group in body.get("multiDailyMetricTimeSeries", []):
            for series in group.get("dailyMetricTimeSeries", []):
                metric = series["dailyMetric"]
                if metric not in METRICS or series.get("dailySubEntityType"):
                    raise ValueError("Unexpected metric series")
                if metric in result.data["series"]:
                    raise ValueError("Duplicate metric series")
                rows = result.data["series"][metric] = []
                seen = set()
                for raw in series.get("timeSeries", {}).get("datedValues", []):
                    stamp = raw["date"]
                    day = date(stamp["year"], stamp["month"], stamp["day"])
                    if day in seen or not ctx.period.start <= day <= ctx.period.end:
                        raise ValueError("Unexpected metric date")
                    seen.add(day)
                    # Protobuf scalar omission is zero only for an explicitly returned date.
                    rows.append({"date": str(day), "value": integer(raw.get("value", 0))})
                    result.rows += 1
                result.data["metric_coverage"][metric] = {
                    "days_returned": len(rows),
                    "days_requested": expected,
                    "sum_of_returned_days": sum(row["value"] for row in rows),
                }
                result.truncated |= len(rows) != expected
        result.data["missing_metrics"] = sorted(set(METRICS) - result.data["series"].keys())
        result.truncated |= bool(result.data["missing_metrics"])
        if result.truncated:
            result.detail = "Missing metrics/dates are unavailable, not zero activity"

    return ctx.capture("daily_metrics", url, read)


def keywords(ctx: ConnectorContext) -> Endpoint:
    url = PERFORMANCE + ctx.resource + "/searchkeywords/impressions/monthly"

    def read(result: Endpoint) -> None:
        # Only complete calendar months fully contained in the selected daily period.
        first = ctx.period.start.replace(day=1)
        if ctx.period.start.day != 1:
            first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = (ctx.period.end + timedelta(days=1)).replace(day=1) - timedelta(days=1)
        if first > last:
            result.truncated = True
            result.detail = "No complete calendar month in period; select a full month for keywords"
            result.data = {"rows": [], "coverage_period": None}
            return
        result.data = {"rows": [], "coverage_period": {"start": str(first), "end": str(last)}}
        query = {}
        for name, day in (("start_month", first), ("end_month", last)):
            for field in ("year", "month"):
                query[f"monthlyRange.{name}.{field}"] = getattr(day, field)
        for _, batch in pages(ctx, result, url, "searchKeywordsCounts", 100, query):
            for raw in batch:
                value = raw["insightsValue"]
                fields = set(value) & {"value", "threshold"}
                if len(fields) != 1:
                    raise ValueError("Invalid keyword count")
                field = fields.pop()
                result.data["rows"].append(
                    {
                        "keyword": text(raw["searchKeyword"]),
                        field: integer(value[field]),
                    }
                )
                result.rows += 1
                if field == "threshold":
                    result.data["thresholded"] = True
        if result.data.get("thresholded"):
            result.detail = "Thresholds are upper bounds, not exact impression counts"

    return ctx.capture("monthly_keywords", url, read)


def collect(ctx: ConnectorContext) -> None:
    before = len(ctx.endpoints)
    parent = review_parent(ctx)
    if parent:
        reviews(ctx, parent)
    else:
        discovery = ctx.endpoints[before:]
        failures = [e for e in discovery if e.status != "connected"]
        capped = any(e.truncated for e in discovery)
        ctx.endpoints.append(
            Endpoint(
                "reviews",
                REVIEWS + "accounts/*/" + ctx.resource + "/reviews",
                status=failures[0].status if failures else "connected" if capped else "not_found",
                truncated=capped,
                detail="Review parent unavailable in account/location discovery; "
                "inspect discovery coverage and verify the selected resource and Manager access",
            )
        )
    daily(ctx)
    keywords(ctx)


def folded(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def website(value: str) -> str:
    parts = urlsplit(safe_url(value))
    return urlunsplit(
        ("", (parts.hostname or "").removeprefix("www."), parts.path.rstrip("/"), "", "")
    )


def analyse_profile(
    ctx: ConnectorContext,
    result: ModuleResult,
    profile: Mapping[str, Any],
    profile_reference: str = "",
) -> None:
    """Compare observed configuration to supplied identity; never validate the supplied claims."""
    endpoints = {e.key: e for e in ctx.endpoints}
    location = endpoints.get("location")
    if not location or not location.complete:
        return
    data = location.data
    comparisons = data.setdefault("profile_comparisons", [])

    def check(
        key: str,
        failed: bool,
        title: str,
        detail: str,
        endpoint=location,
        *,
        complete=True,
        inferred=False,
    ) -> None:
        component = ctx.resource + ":" + key
        if complete or failed:
            outcome = observe(
                "gbp",
                "gbp:" + key,
                "local_seo",
                component,
                "fail" if failed else "pass",
                [endpoint.evidence_id],
            )
            outcome.site = "gbp://" + ctx.resource
            result.outcomes.append(outcome)
        if failed:
            result.finding(
                "gbp:" + key,
                title,
                "local_seo",
                [endpoint.evidence_id],
                detail,
                "Review the selected profile and evidence with the customer before any changes.",
                site="gbp://" + ctx.resource,
                component=component,
                coverage=[component],
                confidence="needs_verification" if inferred else "confirmed",
                severity="low",
            )

    check(
        "hours-missing",
        not data.get("regularHours", {}).get("periods"),
        "GBP has no regular opening hours",
        "The selected location returned no regular hours; "
        "verify which hours apply to this business.",
    )
    check(
        "category-missing",
        not data.get("categories", {}).get("primaryCategory"),
        "GBP has no primary category",
        "No primary category was returned for this location.",
    )

    phones = data.get("phoneNumbers", {})
    actual_phones = {
        phone_key(p)
        for p in [phones.get("primaryPhone", ""), *phones.get("additionalPhones", [])]
        if p
    }
    expected_phones = {phone_key(p) for p in profile.get("phones", [])}
    actual_address = data.get("storefrontAddress", {})
    actual_parts = {
        "street": " ".join(actual_address.get("addressLines", [])),
        "suburb": actual_address.get("locality", ""),
        "state": actual_address.get("administrativeArea", ""),
        "postcode": actual_address.get("postalCode", ""),
    }
    expected_addresses = [a for a in profile.get("locations", []) if a.get("street")]
    areas = data.get("serviceArea", {}).get("places", {}).get("placeInfos", [])
    actual_areas = {folded(a["placeName"]) for a in areas if a.get("placeName")}
    expected_areas = {folded(a) for a in profile.get("service_area", [])}
    expected_values = {
        "name": [profile.get("name", ""), *profile.get("aliases", [])],
        "phone": sorted(expected_phones),
        "website": list(profile.get("websites", [])),
        "address": [{k: a[k] for k in actual_parts if a.get(k)} for a in expected_addresses],
        "service-area": list(profile.get("service_area", [])),
    }
    fields = [
        (
            "name",
            bool(profile.get("name")),
            folded(data.get("title", ""))
            not in {folded(n) for n in [profile.get("name", ""), *profile.get("aliases", [])]},
        ),
        ("phone", bool(expected_phones), not actual_phones or not actual_phones <= expected_phones),
        (
            "website",
            bool(profile.get("websites")),
            website(data.get("websiteUri", ""))
            not in {website(w) for w in profile.get("websites", [])},
        ),
        (
            "address",
            bool(expected_addresses)
            and data.get("serviceArea", {}).get("businessType") != "CUSTOMER_LOCATION_ONLY",
            not any(
                all(folded(actual_parts[k]) == folded(a[k]) for k in actual_parts if a.get(k))
                for a in expected_addresses
            ),
        ),
        (
            "service-area",
            bool(expected_areas),
            expected_areas != actual_areas,
        ),
    ]
    for field, assessed, mismatch in fields:
        comparisons.append(
            {
                "field": field,
                "assessed": assessed,
                "differs": mismatch if assessed else None,
                "profile_reference": profile_reference,
                "source_provenance": "customer_statement",
                "supplied_values": redact(expected_values[field]) if assessed else None,
            }
        )
        if assessed:
            check(
                "profile-" + field,
                mismatch,
                "GBP " + field + " differs from supplied profile",
                "The authenticated GBP value differs from the customer's supplied "
                + field
                + ". This confirms a difference, not which value is correct."
                + (
                    " Area labels may describe overlapping geography; verify equivalence."
                    if field == "service-area"
                    else ""
                ),
            )
    review = endpoints.get("reviews")
    if review and "unanswered" in review.data:
        count = review.data["unanswered"]
        check(
            "unanswered-reviews",
            count > 0,
            "GBP reviews have no business reply",
            f"{count} of {review.rows} retrieved reviews have no reply. "
            "Counts describe the retrieved sample; no reviewer identities or text are retained.",
            endpoint=review,
            complete=review.complete,
        )

    # The framework supplies current public samples separately from connected evidence.
    # locations/<id> is not a website binding: ctx.connected_evidence is intentionally empty.
    # Use the approved profile and its public website NAP samples, without inferring a
    # cross-connector site binding from the profile's website or Google's websiteUri.
    hosts = {site_key(w) for w in ctx.websites}
    for evidence in ctx.public_evidence:
        if evidence.source != "presence" or site_key(evidence.url) not in hosts:
            continue
        if not isinstance(evidence.data, Mapping) or not evidence.data.get("entities"):
            continue
        related = [f["id"] for f in ctx.public_findings if evidence.id in f.get("evidence_ids", [])]
        by_page: dict[str, list[Mapping]] = {}
        for entity in evidence.data["entities"]:
            page = safe_url(entity.get("page", ""))
            if site_key(page) in hosts:
                by_page.setdefault(page, []).append(entity)
        names = {
            folded(n)
            for n in [data.get("title", ""), profile.get("name", ""), *profile.get("aliases", [])]
            if n
        }
        for page, entities in by_page.items():
            # Multiple entities on one page cannot establish which is the bound location.
            if len(entities) != 1:
                data.setdefault("website_comparisons", []).append(
                    {
                        "page": page,
                        "evidence_ids": [evidence.id],
                        "related_finding_ids": related,
                        "detail": "Multiple website entities; GBP comparison needs verification",
                    }
                )
                continue
            entity = entities[0]
            observed_phones = {phone_key(p) for p in entity.get("telephone", [])}
            matched = bool(entity.get("name")) and folded(entity["name"]) in names
            pairs = [
                ("phone", observed_phones, actual_phones),
                (
                    "name",
                    {folded(entity["name"])} if entity.get("name") else set(),
                    {folded(data["title"])} if data.get("title") else set(),
                ),
                (
                    "street",
                    {folded(entity["street"])} if entity.get("street") else set(),
                    {folded(actual_parts["street"])} if actual_parts["street"] else set(),
                ),
                (
                    "postcode",
                    {folded(entity["postcode"])} if entity.get("postcode") else set(),
                    {folded(actual_parts["postcode"])} if actual_parts["postcode"] else set(),
                ),
            ]
            for field, observed, actual in pairs:
                if not observed or not actual:
                    continue
                differs = not bool(observed & actual)
                detail = "GBP " + field + " compared with sampled website structured data. "
                detail += "Values differ. " if differs else "Values match. "
                if not matched:
                    detail += "Entity correspondence needs verification."
                data.setdefault("website_comparisons", []).append(
                    {
                        "field": field,
                        "differs": differs,
                        "evidence_ids": [evidence.id],
                        "related_finding_ids": related,
                        "page": page,
                        "detail": detail,
                    }
                )
                if not related and (matched or differs):
                    check(
                        "public-" + field + ":" + page,
                        differs,
                        "GBP " + field + " differs from sampled website structured data",
                        detail,
                        inferred=not matched,
                    )

    public_comparisons = data.get("website_comparisons", [])
    if public_comparisons:
        refs = sorted({ref for c in public_comparisons for ref in c["evidence_ids"]})
        related_ids = sorted({ref for c in public_comparisons for ref in c["related_finding_ids"]})
        location.detail += (
            f"; {len(public_comparisons)} website comparisons recorded. "
            "Public evidence: "
            + ", ".join(refs[:10])
            + ". Related public findings: "
            + (", ".join(related_ids[:10]) or "none")
            + ". See this endpoint's evidence for all comparison details and references."
        )


def analyse(ctx: ConnectorContext, result: ModuleResult) -> None:
    analyse_profile(ctx, result, ctx.profile, ctx.profile_evidence_id)


CONNECTOR = Connector(
    key="gbp",
    label="Google Business Profile",
    provider="google",
    auth_kind="google_oauth",
    auth_flow="client_secrets",
    auth_hint=(
        "ams-assess connect gbp auth --credential-ref <name> "
        "--client-secrets ~/.config/ams-assess/google-desktop-client.json"
    ),
    # Account/location discovery shares this budget with reviews and performance reads.
    # Retain the bounded interactive collection budget; the UI further caps it at 40s.
    collection_seconds=45,
    max_requests=80,
    scopes=(SCOPE,),
    resource_type="gbp_location",
    read_allowlist=(
        ReadOperation("GET", re.escape(ACCOUNTS) + "accounts"),
        ReadOperation("GET", re.escape(INFO) + r"accounts/" + ID + "/locations"),
        ReadOperation("GET", re.escape(INFO) + r"locations/" + ID),
        ReadOperation(
            "GET", re.escape(REVIEWS) + r"accounts/" + ID + r"/locations/" + ID + "/reviews"
        ),
        ReadOperation(
            "GET", re.escape(PERFORMANCE) + r"locations/" + ID + ":fetchMultiDailyMetricsTimeSeries"
        ),
        ReadOperation(
            "GET",
            re.escape(PERFORMANCE) + r"locations/" + ID + "/searchkeywords/impressions/monthly",
        ),
    ),
    normalize_resource=normalize_resource,
    list_resources=list_resources,
    test=test,
    collect=collect,
    analysers=(analyse,),
    classify_error=classify_error,
    authenticate=google_authenticate,
    authorize=google_authorize,
    categories=frozenset({"local_seo"}),
)
