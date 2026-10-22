import json
import re
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from ams_assess import cli, platforms, runner, store
from ams_assess.consolidate import consolidate
from ams_assess.models import Context, ModuleResult, create_run_dir
from ams_assess.modules import dataforseo, presence
from ams_assess.presence_discovery import (
    FOUND_LIKELY,
    FOUND_VERIFY,
    INCONSISTENT,
    NOT_APPLICABLE,
    NOT_ASSESSED,
    NOT_FOUND,
    identity,
    score,
)
from ams_assess.profile import Verification, load_verifications
from ams_assess.report import render_customer

CREDENTIALS = {"DATAFORSEO_LOGIN": "synthetic", "DATAFORSEO_PASSWORD": "synthetic"}
PROFILE = {
    "name": "Acme Painting Pty Ltd",
    "aliases": [],
    "websites": ["https://example.com/"],
    "abn": "",
    "industry": "trades",
    "buying_model": "service-area",
    "service_area": ["Parramatta"],
    "locations": [],
    "phones": ["02 5550 0101"],
}
PAGE = {
    "links": ["https://www.facebook.com/acmepainting"],
    "json_ld": [
        {
            "@type": "HousePainter",
            "name": "Acme Painting",
            "telephone": "02 5550 0101",
            "address": {"addressLocality": "Parramatta", "postalCode": "2150"},
        }
    ],
    "contacts": [{"href": "tel:0255500101", "text": "02 5550 0101"}],
    "visible_text_summary": "Acme Painting 02 5550 0101 Parramatta NSW 2150",
}


def context(tmp_path, cap=None, profile=None, keywords=(), **config):
    return Context(
        customer_id="acme",
        customer_name="acme",
        target="https://example.com/",
        categories={"presence"},
        connections=set(),
        config=config,
        run_dir=create_run_dir(tmp_path, "acme"),
        tools_dir=tmp_path,
        dataforseo_max_usd=cap,
        profile=profile,
        keywords=list(keywords),
    )


def browser(page):
    result = ModuleResult(name="browser")
    url = "https://example.com/"
    result.add_evidence(url, "Rendered page evidence (desktop)", {"url": url, "page": dict(page)})
    return result


def serp_body(items, cost="0.002"):
    return {
        "status_code": 20000,
        "cost": float(cost),
        "tasks": [{"status_code": 20000, "result": [{"check_url": "", "items": items}]}],
    }


def fake_serp(responses, calls, cost="0.002"):
    """Responses keyed by (endpoint, keyword); the default cost is the published live price."""

    def post(url, **kwargs):
        task = kwargs["json"][0]
        calls.append((url.rsplit("/serp/google/", 1)[-1], task))
        kind = "maps" if "/maps/" in url else "organic"
        body = serp_body(responses.get((kind, task["keyword"]), []), cost)
        return httpx.Response(200, request=httpx.Request("POST", url), json=body)

    return post


def tables(result):
    return {c["key"]: c["value"] for c in result.checks}


def test_catalogue_relevance_by_industry_and_buying_model():
    def level(key, industry, model):
        return platforms.relevance(platforms.BY_KEY[key], industry, model)

    assert level("tripadvisor", "restaurant", "storefront") == "high"
    assert level("hipages", "restaurant", "storefront") == platforms.NA
    assert level("pinterest", "restaurant", "storefront") == platforms.NA
    assert level("hipages", "trades", "service-area") == "high"
    assert level("pinterest", "trades", "service-area") == "low"
    assert level("fresha", "beauty", "storefront") == "high"
    assert level("instagram", "beauty", "storefront") == "high"
    # Online-only businesses are generally not eligible for local map listings.
    assert level("google_business_profile", "general", "online-only") == platforms.NA
    assert level("bing_places", "general", "online-only") == platforms.NA
    assert level("google_business_profile", "trades", "service-area") == "high"
    for key in ["linkedin", "buy_nsw", "associations"]:
        assert level(key, "b2b_contractor", "b2b-contract") == "high"
    assert level("austender", "b2b_contractor", "b2b-contract") == "medium"
    # Unknown industry: industry platforms are undecided rather than not applicable.
    assert level("tripadvisor", None, None) is None
    assert level("facebook", None, None) == "medium"
    keys = set(platforms.BY_KEY)
    assert {"oneflare", "thefork"}.isdisjoint(keys)
    assert {"apple_maps", "yelp", "hotfrog", "cylex", "whereis", "menulog", "icn_gateway"} <= keys
    order = [p.key for p, _ in platforms.prioritised("trades", "service-area")]
    assert order.index("hipages") < order.index("facebook") < order.index("youtube")
    assert platforms.normalise_industry("Traffic-Management") == "b2b_contractor"
    with pytest.raises(ValueError, match="industry must be one of"):
        platforms.normalise_industry("astrology")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.tripadvisor.com.au/Restaurant_Review-g1-d2-Acme.html", "tripadvisor"),
        ("https://www.tripadvisor.com.au/Tourism-g1", None),
        ("https://www.ubereats.com/au/store/acme-cafe/abc", "uber_eats"),
        ("https://www.fresha.com/a/acme-salon-sydney-1", "fresha"),
        ("https://bookings.gettimely.com/acmesalon/book", "timely"),
        ("https://www.hipages.com.au/connect/acmepainting", "hipages"),
        ("https://www.airtasker.com/users/acme-123/", "airtasker"),
        ("https://www.oneflare.com.au/b/acme", None),
        ("https://buy.nsw.gov.au/supplier/acme", "buy_nsw"),
        ("https://gateway.icn.org.au/company/acme", "icn_gateway"),
        ("https://www.youtube.com/@acme", "youtube"),
        ("https://www.youtube.com/watch?v=x", None),
        ("https://www.tiktok.com/@acme", "tiktok"),
        ("https://www.facebook.com/sharer/sharer.php?u=x", None),
        ("https://www.yelp.com.au/biz/acme-sydney", "yelp"),
        ("https://maps.apple.com/?q=acme", "apple_maps"),
        ("ftp://www.facebook.com/acme", None),
        # Business-profile routes only: search, category, help, login and map-search pages are not.
        ("https://www.fresha.com/search?q=acme", None),
        ("https://www.fresha.com/help/acme", None),
        ("https://www.booksy.com/en-au/s/hair-salon", None),
        ("https://www.yellowpages.com.au/find/painters/parramatta-nsw", None),
        ("https://www.yellowpages.com.au/search/listings?clue=acme", None),
        ("https://www.truelocal.com.au/find/painter/parramatta", None),
        ("https://www.hipages.com.au/find/painters/nsw", None),
        ("https://www.houzz.com.au/professionals/painters", None),
        ("https://www.tripadvisor.com.au/Restaurants-g1", None),
        ("https://www.google.com/maps/search/acme", None),
        ("https://www.google.com/maps/dir/a/b", None),
        ("https://maps.google.com/?cid=123456", "google_business_profile"),
        ("https://www.facebook.com/groups/acme", None),
        ("https://www.facebook.com/login", None),
        ("https://www.facebook.com/profile.php?id=100012345", "facebook"),
        ("https://www.instagram.com/explore/tags/acme/", None),
        ("https://www.linkedin.com/company/acme/jobs/", None),
        ("https://www.menulog.com.au/area/2000-sydney", None),
        ("https://www.menulog.com.au/restaurants-acme-cafe/menu", "menulog"),
    ],
)
def test_catalogue_url_classification(url, expected):
    assert platforms.classify(url) == expected
    assert presence.classify_listing(url) == expected


def test_match_scoring_is_simple_and_explainable(tmp_path):
    ctx = context(tmp_path, profile=PROFILE)
    site = presence.site_identity([("ev-browser-001", {"page": PAGE})])
    who = identity(ctx, site)
    assert who["name"] == "Acme Painting Pty Ltd" and who["localities"] == ["Parramatta"]
    likely = score(who, "Acme Painting | Parramatta painters", "https://x.test/acme-painting", [])
    assert likely["state"] == FOUND_LIKELY and likely["score"] == 5
    # Title and URL slug are one piece of name evidence; the suburb corroborates independently.
    assert likely["matched"] == [
        "name: acme painting (also in profile URL)",
        "suburb/postcode: Parramatta",
    ]
    by_phone = score(who, "Painters", "https://x.test/p/1", ["+61 2 5550 0101"], "example.com")
    assert by_phone["state"] == FOUND_LIKELY and by_phone["score"] == 5
    name_only = score(who, "Acme Painting reviews", "https://x.test/acme-painting", [])
    assert name_only["state"] == FOUND_VERIFY and name_only["score"] == 3
    # A suburb alone never makes a candidate: the name or the phone must match.
    unrelated = score(who, "Bob's Plumbing Parramatta", "https://x.test/bob", [])
    assert unrelated["state"] == "" and unrelated["score"] == 2


def test_geographic_name_and_slug_cannot_make_a_likely_match_alone(tmp_path):
    """Reviewer scenario: 'Sydney Painting' title + /sydneypainting/ used to score 5."""
    profile = PROFILE | {"name": "Sydney Painting", "service_area": ["Sydney"], "phones": []}
    who = identity(context(tmp_path, profile=profile), presence.site_identity([]))
    found = score(who, "Sydney Painting | Painters", "https://www.facebook.com/sydneypainting/", [])
    assert found["score"] == 3 and found["state"] == FOUND_VERIFY
    assert found["matched"] == ["name: sydney painting (also in profile URL)"]
    # Even with "Sydney" repeated outside the name, the suburb is part of the name: no points.
    repeated = score(who, "Sydney Painting - painters in Sydney", "https://x.test/sp", [])
    assert repeated["state"] == FOUND_VERIFY and repeated["score"] == 3
    # Independent corroboration (the website domain) makes it likely.
    corroborated = score(who, "Sydney Painting", "https://x.test/1", [], "example.com")
    assert corroborated["state"] == FOUND_LIKELY
    # The slug alone is weaker name evidence than the title, and still needs corroboration.
    slug_only = score(who, "Painters near you", "https://x.test/sydneypainting", [])
    assert slug_only["score"] == 2 and slug_only["state"] == FOUND_VERIFY


def test_no_cap_means_not_assessed_with_manual_tasks_and_no_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no paid call without cap"))
    ctx = context(tmp_path, profile=PROFILE, keywords=["painter parramatta"], **CREDENTIALS)
    ctx.results = [browser(PAGE)]
    discovery = tables(presence.run(ctx))["discovery"]
    assert discovery["status"] == NOT_ASSESSED and "cap" in discovery["reason"]
    rows = {r["key"]: r for r in discovery["platforms"]}
    assert rows["facebook"]["state"] == FOUND_LIKELY and rows["facebook"]["linked"]
    assert rows["hipages"]["state"] == NOT_ASSESSED and rows["hipages"]["relevance"] == "high"
    assert rows["uber_eats"]["state"] == NOT_APPLICABLE
    assert NOT_FOUND not in {r["state"] for r in discovery["platforms"]}
    tasks = {t["key"]: t for t in discovery["manual_tasks"]}
    assert tasks["hipages"]["query"] == 'site:hipages.com.au "Acme Painting"'
    assert "google.com" in tasks["hipages"]["url"] and "facebook" not in tasks
    assert discovery["manual_tasks"][0]["relevance"] == "high"
    assert discovery["keywords"][0]["state"].startswith("Not assessed: no --dataforseo-max-usd")
    assert "Service-area business" in discovery["gbp_eligibility"]
    assert "per-suburb" in discovery["gbp_eligibility"]


def test_serp_estimates_follow_published_pricing_per_endpoint_and_operator():
    # Every estimate covers the published live price with headroom.
    for (_kind, ops), (published, estimate) in dataforseo.SERP_PRICING.items():
        assert estimate >= published * Decimal("1.5")
        assert published == Decimal("0.002") * (5 if ops else 1)
    assert dataforseo.serp_estimate("organic", "Acme Painting Parramatta") == Decimal("0.003")
    assert dataforseo.serp_estimate("organic", "site:hipages.com.au Acme") == Decimal("0.015")
    assert dataforseo.serp_estimate("maps", "Acme Painting Parramatta") == Decimal("0.003")
    assert dataforseo.serp_estimate("organic", '"painter" ryde') == Decimal("0.015")
    # "and" in a business name is not an operator; two operators cannot be estimated safely.
    assert dataforseo.operators("Smith and Sons Painting") == 0
    assert dataforseo.serp_estimate("organic", 'site:x.com "Acme"') is None


def test_serp_cost_cap_is_checked_before_every_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", fake_serp({}, calls))
    deadline = 1e12
    # Reserve $0.003, book the reported $0.002: two calls fit, the third would exceed the cap.
    ctx = context(tmp_path, cap=0.005, **CREDENTIALS)
    outcomes = [dataforseo.serp(ctx, "organic", f"q{i}", deadline) for i in range(4)]
    assert [o["status"] for o in outcomes] == ["ok", "ok", "refused", "refused"]
    assert "exceeds the remaining cap" in outcomes[2]["reason"] and len(calls) == 2
    assert ctx.dataforseo_spent_usd == Decimal("0.004")
    assert calls[0][1] == {
        "keyword": "q0",
        "location_code": 2036,
        "language_code": "en",
        "depth": 10,
    }
    # site: queries are estimated at the operator price, so a cap that fits a plain query
    # still refuses them; two-operator queries are never sent.
    calls.clear()
    ctx = context(tmp_path, cap=0.014, **CREDENTIALS)
    assert dataforseo.serp(ctx, "organic", "site:hipages.com.au Acme", deadline)["status"] == (
        "refused"
    )
    assert (
        "cannot be estimated" in dataforseo.serp(ctx, "organic", 'site:x "a"', deadline)["reason"]
    )
    assert not calls and dataforseo.serp(ctx, "organic", "Acme", deadline)["status"] == "ok"
    # Spend from the domain overview call counts against the same cap.
    calls.clear()
    ctx = context(tmp_path, cap=0.012, **CREDENTIALS)
    ctx.dataforseo_spent_usd = dataforseo.ESTIMATE_USD
    assert dataforseo.serp(ctx, "maps", "x", deadline)["status"] == "refused" and not calls
    # Hard query count, whatever the cap.
    ctx = context(tmp_path, cap=100, **CREDENTIALS)
    for i in range(dataforseo.MAX_SERP_CALLS + 2):
        dataforseo.serp(ctx, "organic", f"q{i}", deadline)
    assert len(calls) == dataforseo.MAX_SERP_CALLS == 12
    assert "query cap" in dataforseo.serp(ctx, "organic", "more", deadline)["reason"]
    # A charge above the estimate stops all further paid calls.
    calls.clear()
    monkeypatch.setattr(httpx, "post", fake_serp({}, calls, cost="0.05"))
    ctx = context(tmp_path, cap=100, **CREDENTIALS)
    dataforseo.serp(ctx, "organic", "a", deadline)
    assert dataforseo.serp(ctx, "organic", "b", deadline)["status"] == "refused"
    assert len(calls) == 1 and ctx.dataforseo_spent_usd == Decimal("0.05")
    # No credentials, no cap, a local target or a passed deadline: nothing is sent.
    calls.clear()
    for ctx, why in [
        (context(tmp_path, cap=1), "credentials"),
        (context(tmp_path, **CREDENTIALS), "cap"),
    ]:
        assert why in dataforseo.serp(ctx, "organic", "a", deadline)["reason"]
    local = context(tmp_path, cap=1, **CREDENTIALS)
    local.target = "http://127.0.0.1:8000/"
    assert "local" in dataforseo.serp(local, "organic", "a", deadline)["reason"]
    assert (
        "deadline"
        in dataforseo.serp(context(tmp_path, cap=1, **CREDENTIALS), "maps", "a", 0)["reason"]
    )
    assert not calls


def test_timed_out_charged_calls_never_take_spend_past_the_cap(tmp_path, monkeypatch):
    """Reviewer scenario: every call times out but is still charged its published price."""
    charged = []

    def timeout(url, **kwargs):
        keyword = kwargs["json"][0]["keyword"]
        # Published price, independently of the engine's matcher: x5 for the site: operator.
        charged.append(Decimal("0.010") if "site:" in keyword else Decimal("0.002"))
        raise httpx.ReadTimeout("synthetic", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", timeout)
    ctx = context(tmp_path, cap=0.10, profile=PROFILE, keywords=["painter ryde"], **CREDENTIALS)
    ctx.dataforseo_spent_usd = dataforseo.ESTIMATE_USD  # The domain overview ran first.
    ctx.results = [browser(PAGE)]
    discovery = tables(presence.run(ctx))["discovery"]
    # Unknown outcomes stay booked at the full estimate, which covers what was charged.
    assert sum(charged) + dataforseo.ESTIMATE_USD <= ctx.dataforseo_spent_usd <= Decimal("0.10")
    queries = discovery["queries"]
    sent = [q for q in queries if q["status"] == "error"]
    assert len(sent) == len(charged) and all("ReadTimeout" in q["reason"] for q in sent)
    assert any("exceeds the remaining cap" in q["reason"] for q in queries)
    # Most relevant first: Maps (GBP high), the name search, the high-relevance hipages search,
    # then the keyword, then medium platforms.
    assert [q["purpose"] for q in queries[:5]] == [
        "Google Maps business search",
        "Business name search",
        "hipages profile search",
        "Keyword position",
        "Facebook profile search",
    ]
    assert queries[2]["keyword"] == "site:hipages.com.au Acme Painting"
    assert queries[2]["estimated_usd"] == "0.015"
    ledger = json.loads((ctx.run_dir / "raw/presence-dataforseo-ledger.json").read_text())
    assert Decimal(ledger["spent_usd_run_total"]) == ctx.dataforseo_spent_usd


def test_discovery_states_nap_duplicates_unlinked_and_keywords(tmp_path, monkeypatch):
    responses = {
        ("maps", "Acme Painting Parramatta"): [
            {
                "type": "maps_search",
                "title": "Acme Painting",
                "address": "1 Church St, Parramatta NSW 2150",
                "phone": "+61 2 5550 0999",
                "domain": "example.com",
                "cid": "123456789",
            }
        ],
        ("organic", "Acme Painting Parramatta"): [
            {
                "type": "organic",
                "url": "https://www.instagram.com/acmepainting/",
                "title": "Acme Painting (@acmepainting) Parramatta",
                "description": "Painters in Parramatta",
            },
            {"type": "organic", "url": "https://example.com/", "title": "Acme Painting"},
        ],
        ("organic", "painter parramatta"): [
            {"type": "organic", "rank_group": 1, "url": "https://other.test/"},
            {"type": "organic", "rank_group": 3, "url": "https://www.example.com/services"},
        ],
        ("organic", "site:hipages.com.au Acme Painting"): [
            {
                "type": "organic",
                "url": "https://hipages.com.au/connect/acmepainting",
                "title": "Acme Painting - Parramatta",
            },
            {
                "type": "organic",
                "url": "https://hipages.com.au/connect/acme-painting-services",
                "title": "Acme Painting Services",
            },
        ],
        ("organic", "site:facebook.com Acme Painting"): [
            {
                "type": "organic",
                "url": "https://www.facebook.com/acmepainting/",
                "title": "Acme Painting | Parramatta",
            }
        ],
        ("organic", "site:truelocal.com.au Acme Painting"): [
            {
                "type": "organic",
                "url": "https://www.truelocal.com.au/business/bobs-plumbing/parramatta",
                "title": "Bob's Plumbing Parramatta",
            }
        ],
    }
    calls = []
    monkeypatch.setattr(httpx, "post", fake_serp(responses, calls, cost="0.002"))
    ctx = context(
        tmp_path, cap=1.0, profile=PROFILE, keywords=["painter parramatta"], **CREDENTIALS
    )
    ctx.results = [browser(PAGE)]
    result = presence.run(ctx)
    assert result.status == "ok"
    discovery = tables(result)["discovery"]
    assert len(calls) == 12 and discovery["budget"]["serp_calls"] == 12
    assert calls[0] == (
        "maps/live/advanced",
        {
            "keyword": "Acme Painting Parramatta",
            "location_code": 2036,
            "language_code": "en",
            "depth": 10,
        },
    )
    assert discovery["status"] == "Partially assessed" and "query cap" in discovery["reason"]
    rows = {r["key"]: r for r in discovery["platforms"]}
    # Maps result matches by name and website but shows another phone: Inconsistent.
    assert rows["google_business_profile"]["state"] == INCONSISTENT
    nap = [n for n in discovery["nap"] if n["key"] == "google_business_profile"]
    assert {(n["field"], n["result"]) for n in nap} == {("phone", "differs"), ("postcode", "match")}
    assert rows["instagram"]["state"] == FOUND_LIKELY and not rows["instagram"]["linked"]
    assert rows["hipages"]["state"] == FOUND_LIKELY and rows["hipages"]["duplicates"]
    assert [c["state"] for c in rows["hipages"]["candidates"]] == [FOUND_LIKELY, FOUND_VERIFY]
    facebook = rows["facebook"]["candidates"]
    assert len(facebook) == 1 and facebook[0]["sources"][0] == "website link"
    assert any(s.startswith("search: ") for s in facebook[0]["sources"])
    # Searched with no matching result: "not found in searches", never "does not exist".
    assert rows["yellow_pages"]["state"] == rows["true_local"]["state"] == NOT_FOUND
    assert rows["tiktok"]["state"] == NOT_ASSESSED and "query cap" in rows["tiktok"]["detail"]
    assert rows["menulog"]["state"] == NOT_APPLICABLE
    assert discovery["keywords"][0]["state"] == "Organic position 3"
    assert discovery["keywords"][0]["url"] == "https://www.example.com/services"
    findings = {f.check_key: f for f in result.findings}
    nap_finding = findings["platform:external-nap-inconsistent:google_business_profile"]
    assert "0255500999" in nap_finding.detail.replace(" ", "")
    assert "acme-painting-services" in findings["platform:possible-duplicates:hipages"].detail
    unlinked = findings["platform:profiles-not-linked:instagram"].detail
    assert "instagram.com/acmepainting" in unlinked
    assert not any(k.startswith("platform:profiles-not-linked:facebook") for k in findings)
    priority = findings["platform:priority-verification"]
    assert priority.detail.index("Google Business Profile") < priority.detail.index("True Local")
    # Relevance rests on the customer-stated industry, so the finding is labelled as such.
    merged = consolidate(result.findings, result.evidence)
    assert next(f for f in merged if f.id == priority.id).provenance == "customer_statement"
    assert all(f.confidence != "confirmed" for f in findings.values() if "platform:" in f.check_key)
    known = {e.id for e in result.evidence}
    assert all(set(f.evidence_ids) <= known for f in result.findings)
    assert all(set(r["evidence_ids"]) <= known for r in discovery["platforms"])
    profile_evidence = next(e for e in result.evidence if e.description.startswith("Customer"))
    assert profile_evidence.provenance == "customer_statement"
    text = json.dumps(discovery).lower()
    assert "does not exist" not in text.replace("does not mean a profile does not exist", "")
    assert "owned" not in text and "is verified" not in text


def test_online_only_gbp_not_applicable_and_eligibility_note(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", fake_serp({}, calls))
    profile = PROFILE | {"industry": "general", "buying_model": "online-only"}
    ctx = context(tmp_path, cap=1.0, profile=profile, **CREDENTIALS)
    ctx.results = [browser({**PAGE, "links": []})]
    discovery = tables(presence.run(ctx))["discovery"]
    rows = {r["key"]: r for r in discovery["platforms"]}
    assert rows["google_business_profile"]["state"] == NOT_APPLICABLE
    assert all(kind != "maps/live/advanced" for kind, _ in calls)
    assert "generally not eligible" in discovery["gbp_eligibility"]


def test_customer_profile_cli_validates_and_is_idempotent(home, capsys):
    base = ["customer", "--customer", "acme"]
    with pytest.raises(SystemExit):
        cli.main(base + ["--abn", "51 824 753 557"])
    with pytest.raises(SystemExit):
        cli.main(base + ["--industry", "astrology"])
    assert store.load(home, "acme") is None
    args = base + [
        "--name",
        "Acme Painting Pty Ltd",
        "--abn",
        "51824753556",
        "--industry",
        "painting",
        "--buying-model",
        "service-area",
        "--location",
        "12 Example Street, Parramatta NSW 2150",
        "--phone",
        "02 5550 0101",
        "--website",
        "example.com",
    ]
    assert cli.main(args) == 0
    assert "Profile saved" in capsys.readouterr().out
    assert cli.main(args) == 0
    assert "Profile unchanged" in capsys.readouterr().out
    saved = store.load(home, "acme")
    assert saved is not None and saved["customer_name"] == "Acme Painting Pty Ltd"
    fields = saved["profile"]["fields"]
    assert fields["abn"] == "51 824 753 556" and fields["industry"] == "trades"
    assert fields["websites"] == ["https://example.com/"]
    assert fields["locations"][0] == {
        "label": "",
        "street": "12 Example Street",
        "suburb": "Parramatta",
        "state": "NSW",
        "postcode": "2150",
    }
    assert [e["provenance"] for e in saved["evidence"]] == ["customer_statement"]
    # An update keeps other fields and the earlier evidence.
    assert cli.main(base + ["--service-area", "Parramatta", "--service-area", "Ryde"]) == 0
    saved = store.load(home, "acme")
    assert saved is not None and saved["profile"]["fields"]["phones"] == ["02 5550 0101"]
    assert len(saved["evidence"]) == 2
    assert saved["evidence"][1]["data"]["supersedes"] == saved["evidence"][0]["id"]


def test_run_uses_stored_profile_and_validates_keywords(home, monkeypatch):
    seen = {}

    def fake_run(ctx, **kwargs):
        seen.update(profile=ctx.profile, keywords=ctx.keywords, name=ctx.customer_name)
        return {"status": "complete", "findings": []}

    monkeypatch.setattr(cli, "config", dict)
    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(["customer", "--customer", "acme", "--name", "Acme Painting"]) == 0
    args = ["run", "--customer", "acme", "--target", "example.com"]
    assert cli.main(args + ["--keywords", "painter ryde, painter ryde,house painter"]) == 0
    assert seen["profile"]["name"] == seen["name"] == "Acme Painting"
    assert seen["keywords"] == ["painter ryde", "house painter"]
    with pytest.raises(SystemExit):
        cli.main(args + ["--keywords", "a,b,c,d,e,f"])


def presence_registry():
    page_module = SimpleNamespace(
        categories={"presence"}, run=lambda ctx: browser(PAGE), name="browser"
    )
    return {"browser": page_module, "presence": presence}


def verification(**changes):
    entry = {
        "platform": "hipages",
        "state": "found_likely",
        "profile_url": "https://hipages.com.au/connect/acmepainting",
        "observed_nap": {"name": "Acme Painting", "phone": "02 5550 0101"},
        "verified_by": "AMS analyst",
        "date": "2026-10-20",
        "note": "Checked the listing details",
    }
    return entry | changes


def test_presence_verify_import_is_idempotent_and_supersedes(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "REGISTRY", presence_registry())
    ctx = Context(
        customer_id="acme",
        customer_name="Acme",
        target="https://example.com/",
        categories={"presence"},
        connections=set(),
        config={},
        run_dir=create_run_dir(home, "acme"),
        tools_dir=tmp_path,
        store_root=home,
        profile=PROFILE,
    )
    runner.run(ctx, no_ai=True, formats={"md"})
    path = tmp_path / "verified.json"
    path.write_text(json.dumps({"verifications": [verification()]}))
    args = ["presence-verify", "--customer", "acme", "--file", str(path)]
    assert cli.main(args) == 0
    assert "1 entries (1 new, 0 unchanged)" in capsys.readouterr().out
    first = store.load(home, "acme")
    assert first is not None
    assert cli.main(args) == 0
    assert "(0 new, 1 unchanged)" in capsys.readouterr().out
    assert store.load(home, "acme") == first
    manual = [e for e in first["evidence"] if e["source"] == "presence_verification"]
    assert len(manual) == 1 and manual[0]["provenance"] == "manual_verification"
    path.write_text(json.dumps([verification(state="Inconsistent", note="Old phone shown")]))
    assert cli.main(args) == 0
    saved = store.load(home, "acme")
    assert saved is not None
    manual = [e for e in saved["evidence"] if e["source"] == "presence_verification"]
    assert len(manual) == 2 and manual[1]["data"]["supersedes"] == manual[0]["id"]
    current = saved["presence_verifications"]["hipages"]
    assert current["history"] == [manual[0]["id"]] and current["state"] == INCONSISTENT
    # The automated state is kept beside the manual one; earlier evidence is not deleted.
    data, evidence, _ = store.report_data(saved)
    row = next(
        r for r in data["digital_presence"]["discovery"]["platforms"] if r["key"] == "hipages"
    )
    assert row["state"] == INCONSISTENT and row["automated_state"] == NOT_ASSESSED
    assert manual[1]["id"] in row["evidence_ids"]
    assert {m["id"] for m in manual} <= {e.id for e in evidence}
    render_customer(home, "acme", {"md"})
    report = (home / "acme/report.md").read_text()
    assert "manual verification 2026-10-20 by AMS analyst; automated: Not assessed" in report
    assert "Manual note: Old phone shown" in report
    automated = [r for r in saved["runs"] if r["kind"] == "assessment"][0]["digital_presence"]
    assert (
        next(r for r in automated["discovery"]["platforms"] if r["key"] == "hipages")["state"]
        == NOT_ASSESSED
    )


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([verification(platform="oneflare")], "catalogue key"),
        ([verification(state="Not assessed")], "state must be one of"),
        ([verification(state="owned")], "state must be one of"),
        ([verification(), verification()], "only once"),
        ([verification(verified_by="")], "verified_by"),
        ([verification(date="yesterday")], "date"),
        ([], "nonempty"),
    ],
)
def test_invalid_verifications_are_rejected_without_writing(home, tmp_path, entries, message):
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(entries))
    with pytest.raises(SystemExit):
        cli.main(["presence-verify", "--customer", "acme", "--file", str(path)])
    assert store.load(home, "acme") is None
    with pytest.raises(ValueError, match=message):
        load_verifications(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    return tmp_path / "data/customers"


def test_generic_platform_pages_in_search_results_are_never_candidates(tmp_path, monkeypatch):
    """Reviewer scenario: a Fresha search page with the name in its title became likely."""
    profile = PROFILE | {"name": "Acme Salon", "industry": "beauty", "buying_model": "storefront"}
    responses = {
        ("organic", "site:fresha.com Acme Salon"): [
            {
                "type": "organic",
                "url": "https://www.fresha.com/search?query=acme-salon",
                "title": "Acme Salon in Parramatta | Fresha",
                "description": "Book Acme Salon, Parramatta. Call 02 5550 0101",
            }
        ]
    }
    monkeypatch.setattr(httpx, "post", fake_serp(responses, []))
    ctx = context(tmp_path, cap=1.0, profile=profile, **CREDENTIALS)
    ctx.results = [browser({**PAGE, "links": []})]
    rows = {r["key"]: r for r in tables(presence.run(ctx))["discovery"]["platforms"]}
    assert rows["fresha"]["candidates"] == [] and rows["fresha"]["state"] == NOT_FOUND


def test_keyword_rank_ignores_serp_features_before_the_depth_limit(tmp_path, monkeypatch):
    """Reviewer scenario: a rank-8 organic result behind SERP features was reported as absent."""
    features = [
        {"type": t, "rank_absolute": i + 1}
        for i, t in enumerate(["paid"] * 4 + ["people_also_ask", "local_pack", "images", "video"])
    ]
    organic = [
        {"type": "organic", "rank_group": n, "rank_absolute": n + 8, "url": f"https://o{n}.test/"}
        for n in range(1, 8)
    ] + [
        {
            "type": "organic",
            "rank_group": 8,
            "rank_absolute": 16,
            "url": "https://www.example.com/painting",
        },
        {"type": "organic", "rank_group": 11, "url": "https://example.com/deep"},
    ]
    responses = {("organic", "painter parramatta"): features + organic}
    monkeypatch.setattr(httpx, "post", fake_serp(responses, []))
    ctx = context(
        tmp_path, cap=1.0, profile=PROFILE, keywords=["painter parramatta"], **CREDENTIALS
    )
    ctx.results = [browser(PAGE)]
    row = tables(presence.run(ctx))["discovery"]["keywords"][0]
    assert row["state"] == "Organic position 8 (absolute rank 16)"
    assert row["url"] == "https://www.example.com/painting"
    # Beyond the requested depth (rank 11) is outside the sample, so it is not reported.
    responses[("organic", "painter parramatta")] = organic[-1:]
    ctx = context(
        tmp_path, cap=1.0, profile=PROFILE, keywords=["painter parramatta"], **CREDENTIALS
    )
    ctx.results = [browser(PAGE)]
    row = tables(presence.run(ctx))["discovery"]["keywords"][0]
    assert row["state"] == "Not in the top 10 results sampled"


def test_paid_discovery_is_kept_when_website_evidence_is_missing(tmp_path, monkeypatch):
    """Reviewer scenario: 12 paid calls, then the module was skipped with zero evidence."""
    calls = []
    monkeypatch.setattr(httpx, "post", fake_serp({}, calls))
    ctx = context(tmp_path, cap=1.0, profile=PROFILE, **CREDENTIALS)
    ctx.results = []  # Browser and DNS evidence unavailable upstream.
    result = presence.run(ctx)
    assert (
        calls and result.status == "ok" and "only independent presence discovery" in result.reason
    )
    paid = [e for e in result.evidence if e.description.startswith("DataForSEO Google")]
    assert len(paid) == len(calls)
    assert tables(result)["discovery"]["budget"]["serp_calls"] == len(calls)
    assert "raw/presence-dataforseo-ledger.json" in result.raw_paths
    # A failure after the paid calls still keeps their evidence and the ledger.
    calls.clear()
    monkeypatch.setattr(presence, "add_findings", lambda *a: 1 / 0)
    ctx = context(tmp_path, cap=1.0, profile=PROFILE, **CREDENTIALS)
    ctx.results = [browser(PAGE)]
    failed = presence.run(ctx)
    assert failed.status == "error" and calls
    assert len(failed.evidence) == len(calls) and not failed.findings
    assert "raw/presence-dataforseo-ledger.json" in failed.raw_paths
    # Without a business name there is nothing to search: no call is made, no spend.
    calls.clear()
    ctx = context(tmp_path, cap=1.0, **CREDENTIALS)
    assert presence.run(ctx).status == "skipped" and not calls
    assert ctx.dataforseo_spent_usd == 0


def test_online_only_business_gets_no_gbp_link_opportunity(tmp_path):
    profile = PROFILE | {"industry": "general", "buying_model": "online-only"}
    ctx = context(tmp_path, profile=profile)
    ctx.results = [browser({**PAGE, "links": []})]
    keys = {f.check_key for f in presence.run(ctx).findings}
    assert "listing:gbp-not-linked" not in keys
    ctx = context(tmp_path, profile=PROFILE)
    ctx.results = [browser({**PAGE, "links": []})]
    assert "listing:gbp-not-linked" in {f.check_key for f in presence.run(ctx).findings}


def test_profile_name_change_updates_the_report_name(home):
    store.set_profile(home, "acme", {"name": "Acme Painting"})
    store.set_profile(home, "acme", {"phones": ["02 5550 0101"]})
    saved = store.load(home, "acme")
    assert saved is not None and saved["customer_name"] == "Acme Painting"
    store.set_profile(home, "acme", {"name": "Acme Painting & Decorating"})
    saved = store.load(home, "acme")
    assert saved is not None and saved["customer_name"] == "Acme Painting & Decorating"
    data, _, _ = store.report_data(saved)
    assert data["customer_name"] == "Acme Painting & Decorating"


def test_manual_verification_retires_obsolete_tasks_and_findings(home, tmp_path, monkeypatch):
    responses = {
        ("organic", "site:hipages.com.au Acme Painting"): [
            {
                "type": "organic",
                "url": "https://hipages.com.au/connect/acmepainting",
                "title": "Acme Painting - Parramatta",
            },
            {
                "type": "organic",
                "url": "https://hipages.com.au/connect/acme-painting-2",
                "title": "Acme Painting",
            },
        ]
    }
    monkeypatch.setattr(httpx, "post", fake_serp(responses, []))
    monkeypatch.setattr(runner, "REGISTRY", presence_registry())
    ctx = Context(
        customer_id="acme",
        customer_name="Acme",
        target="https://example.com/",
        categories={"presence"},
        connections=set(),
        config=dict(CREDENTIALS),
        run_dir=create_run_dir(home, "acme"),
        tools_dir=tmp_path,
        store_root=home,
        profile=PROFILE,
        dataforseo_max_usd=1.0,
    )
    runner.run(ctx, no_ai=True, formats={"md"})
    before = store.load(home, "acme")
    assert before is not None
    keys = {f["check_key"] for f in before["findings"]}
    assert "platform:possible-duplicates:hipages" in keys
    data, _, _ = store.report_data(before)
    priority = next(
        f for f in data["findings"] if f["check_key"] == "platform:priority-verification"
    )
    assert any(t["key"] == "hipages" for t in data["digital_presence"]["discovery"]["manual_tasks"])
    path = tmp_path / "verified.json"
    yellow = verification(
        platform="yellow_pages", state="not_found", profile_url="", note="Searched by name"
    )
    assert "Yellow Pages AU (medium)" in priority["detail"]
    path.write_text(json.dumps([verification(state="found_likely"), yellow]))
    assert cli.main(["presence-verify", "--customer", "acme", "--file", str(path)]) == 0
    saved = store.load(home, "acme")
    assert saved is not None
    data, evidence, _ = store.report_data(saved)
    current = {f["check_key"]: f for f in data["findings"]}
    assert "platform:possible-duplicates:hipages" not in current
    discovery = data["digital_presence"]["discovery"]
    assert not any(t["key"] == "hipages" for t in discovery["manual_tasks"])
    row = next(r for r in discovery["platforms"] if r["key"] == "hipages")
    assert not row["duplicates"] and len(row["automated_candidates"]) == 2
    assert row["automated_state"] == FOUND_LIKELY and row["state"] == FOUND_LIKELY
    # The priority finding is recomputed without the manually settled platforms.
    assert "Yellow Pages" not in current["platform:priority-verification"]["detail"]
    assert "True Local (medium)" in current["platform:priority-verification"]["detail"]
    assert not any(n["key"] == "yellow_pages" for n in discovery["nap"])
    # History stays: the store keeps the finding and all automated evidence.
    assert "platform:possible-duplicates:hipages" in {f["check_key"] for f in saved["findings"]}
    assert {e["id"] for e in before["evidence"]} <= {e.id for e in evidence}
    render_customer(home, "acme", {"md"})
    assert "Several candidate profiles on hipages" not in (home / "acme/report.md").read_text()


def test_profile_and_verification_writes_use_the_customer_lock(home, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.1)
    records = [Verification.model_validate(verification())]
    with store.locked(home, "acme"):
        with pytest.raises(ValueError, match="busy"):
            store.set_profile(home, "acme", {"name": "Acme Painting"})
        with pytest.raises(ValueError, match="busy"):
            store.add_presence_verifications(home, "acme", records)
    assert store.load(home, "acme") is None
    assert store.set_profile(home, "acme", {"name": "Acme Painting"})[2]
    assert store.add_presence_verifications(home, "acme", records)["new"] == 1


# Expected billing written out by hand from the published rule (x5 per operator), NOT derived
# from the engine's matcher: raw query -> estimate booked, or None for "refused, never sent".
BILLING_TABLE = [
    ("painter parramatta", "0.003"),
    ("Smith and Sons Painting", "0.003"),
    ("men's haircut sydney", "0.003"),
    ("site:hipages.com.au Acme Painting", "0.015"),
    ("SITE:hipages.com.au Acme", "0.015"),
    ("link:example.com", "0.015"),
    ("info:example.com", "0.015"),
    ("id:12345", "0.015"),
    ("allinanchor:painter", "0.015"),
    ("definition:painter", "0.015"),
    ("unknownop:painter", "0.015"),
    ("site%3Ahipages.com.au Acme", "0.015"),
    ("site%253Ahipages.com.au Acme", "0.015"),
    ('"acme painting" sydney', "0.015"),
    ("painters -cheap", "0.015"),
    ("site:x.com link:y.com", None),
    ("site%3Ax.com link%3Ay.com", None),
    ("site:x.com%20link:y.com", None),
    ('site:x.com "acme"', None),
    ("a:b:c", None),
    ("100% painting", None),
    ("site%25253Ax.com", "0.015"),  # three encoding layers decode to one site:
    ("site%2525253Ax.com", None),  # more layers than are decoded: refused
]


@pytest.mark.parametrize(("query", "expected"), BILLING_TABLE)
@pytest.mark.parametrize("kind", ["organic", "maps"])
def test_estimates_match_an_independent_billing_table(kind, query, expected):
    estimate = dataforseo.serp_estimate(kind, query)
    assert estimate == (Decimal(expected) if expected else None)


@pytest.mark.parametrize(
    "keyword",
    ["painter link:x.com", "site%3Ax.com painter", "info:x", '"painter" ryde', "100%", "a:b"],
)
def test_cli_rejects_keywords_that_are_not_plain_terms(home, monkeypatch, capsys, keyword):
    monkeypatch.setattr(cli, "config", dict)
    monkeypatch.setattr(cli, "run", lambda *a, **k: pytest.fail("run must not start"))
    args = ["run", "--customer", "acme", "--target", "example.com", "--dataforseo-max-usd", "0.1"]
    with pytest.raises(SystemExit) as exit_info:
        cli.main(args + ["--keywords", f"painter sydney,{keyword}"])
    assert exit_info.value.code == 2
    assert "--keywords must be plain search terms" in capsys.readouterr().err


def test_unsafe_names_and_keywords_never_escape_the_cap(tmp_path, monkeypatch):
    """Reviewer scenario: operator keywords charged $0.1141 against $0.0791 booked ($0.10 cap)."""
    charged, sent = [], []

    def billed(url, **kwargs):
        # Independent provider model: x5 per word: operator or quoted phrase in the decoded query.
        query = kwargs["json"][0]["keyword"]
        plain = unquote(unquote(unquote(query)))
        ops = len(re.findall(r"[a-z]+:", plain, re.I)) + len(re.findall(r'"[^"]*"', plain))
        charged.append(Decimal("0.002") * (5 * ops if ops else 1))
        sent.append(query)
        raise httpx.ReadTimeout("synthetic", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", billed)
    profile = PROFILE | {
        "name": 'Acme: "Painting" 100% OR site%3Aevil.com',
        "service_area": ["a:b"],
    }
    keywords = ["painter link:x.com", "site%3Ax.com painter", "site:x link:y", "plain painter"]
    ctx = context(tmp_path, cap=0.10, profile=profile, keywords=keywords, **CREDENTIALS)
    ctx.dataforseo_spent_usd = dataforseo.ESTIMATE_USD
    ctx.results = [browser({**PAGE, "links": []})]
    discovery = tables(presence.run(ctx))["discovery"]
    assert sent and sum(charged) + dataforseo.ESTIMATE_USD <= ctx.dataforseo_spent_usd
    assert ctx.dataforseo_spent_usd <= Decimal("0.10")
    # Generated queries carry only their template's operator; unsafe keywords are never sent.
    for query in sent:
        assert "%" not in query and '"' not in query
        assert query.count(":") == (1 if query.startswith("site:") else 0)
    assert "plain painter" in sent
    assert not any(k in sent for k in keywords[:3])
    refused = {q["keyword"]: q["reason"] for q in discovery["queries"] if q["status"] == "refused"}
    assert all("not a plain keyword" in refused[k] for k in keywords[:3])


@pytest.mark.parametrize(
    ("url", "identified"),
    [
        ("https://www.bing.com/maps?q=restaurants", False),
        ("https://www.bing.com/maps?ss=ypid.YN3724x123456789&cp=-33.8~151.2", True),
        ("https://maps.apple.com/?q=restaurants", False),
        ("https://maps.apple.com/?auid=1234567890&q=Acme", True),
        ("https://maps.apple.com/place?place-id=I1234ABCD", True),
        ("https://maps.apple.com/place?address=1+Church+St", True),
        ("https://maps.app.goo.gl/AbC123", False),
        ("https://g.page/acme-painting", True),
        ("https://www.google.com.au/maps/place/Acme+Painting", True),
        ("https://maps.google.com/?cid=123456", True),
        ("https://www.facebook.com/acmepainting", True),
        ("https://www.google.com/maps/search/restaurants", False),
    ],
)
def test_only_map_links_with_a_business_identifier_are_promoted(url, identified):
    assert platforms.identifies(url) is identified


def test_generic_map_search_links_from_the_website_need_verification(tmp_path):
    """Reviewer scenario: links to bing.com/maps?q=restaurants became 'Found — likely match'."""
    links = ["https://www.bing.com/maps?q=restaurants", "https://maps.apple.com/?q=restaurants"]
    ctx = context(tmp_path, profile=PROFILE)
    ctx.results = [browser({**PAGE, "links": links})]
    rows = {r["key"]: r for r in tables(presence.run(ctx))["discovery"]["platforms"]}
    for key in ["bing_places", "apple_maps"]:
        assert rows[key]["state"] == FOUND_VERIFY
        assert "no business or place identifier" in rows[key]["candidates"][0]["matched"]
    ctx = context(tmp_path, profile=PROFILE)
    ctx.results = [browser({**PAGE, "links": ["https://maps.apple.com/?auid=1234567890"]})]
    rows = {r["key"]: r for r in tables(presence.run(ctx))["discovery"]["platforms"]}
    assert rows["apple_maps"]["state"] == FOUND_LIKELY


def test_overview_overcharge_stops_later_searches(tmp_path, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        body = {"status_code": 20000, "cost": 0.05, "tasks": [{"status_code": 20000, "result": []}]}
        return httpx.Response(200, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx, "post", post)
    ctx = context(tmp_path, cap=1.0, profile=PROFILE, **CREDENTIALS)
    ctx.categories = {"presence", "seo"}
    assert dataforseo.run(ctx).status == "ok" and len(calls) == 1
    assert ctx.dataforseo_stopped and ctx.dataforseo_serp_calls == 0
    outcome = dataforseo.serp(ctx, "organic", "painter", 1e12)
    assert outcome["status"] == "refused" and "stopped" in outcome["reason"]
    ctx.results = [browser(PAGE)]
    discovery = tables(presence.run(ctx))["discovery"]
    assert len(calls) == 1
    assert all("stopped" in q["reason"] for q in discovery["queries"])


def test_organic_map_search_results_cannot_become_likely(tmp_path, monkeypatch):
    """Reviewer scenario: an organic result linking a Bing Maps search scored 5 and was likely."""
    url = "https://www.bing.com/maps?q=Acme+Painting+Parramatta"
    responses = {
        ("organic", "Acme Painting Parramatta"): [
            {"type": "organic", "url": url, "title": "Acme Painting — Parramatta"}
        ]
    }
    monkeypatch.setattr(httpx, "post", fake_serp(responses, []))
    ctx = context(tmp_path, cap=1.0, profile=PROFILE, **CREDENTIALS)
    ctx.results = [browser({**PAGE, "links": []})]
    row = next(
        r for r in tables(presence.run(ctx))["discovery"]["platforms"] if r["key"] == "bing_places"
    )
    candidate = row["candidates"][0]
    assert not platforms.identifies(url) and candidate["score"] == 5
    assert candidate["state"] == row["state"] == FOUND_VERIFY
    assert "no business or place identifier" in candidate["matched"]
