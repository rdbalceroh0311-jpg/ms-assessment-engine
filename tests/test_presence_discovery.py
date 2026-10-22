import json
from decimal import Decimal
from types import SimpleNamespace

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
from ams_assess.profile import load_verifications
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


def serp_body(items, cost="0.004"):
    return {
        "status_code": 20000,
        "cost": float(cost),
        "tasks": [{"status_code": 20000, "result": [{"check_url": "", "items": items}]}],
    }


def fake_serp(responses, calls, cost="0.004"):
    def post(url, **kwargs):
        task = kwargs["json"][0]
        calls.append((url.rsplit("/serp/google/", 1)[-1], task))
        body = serp_body(responses.get(task["keyword"], []), cost)
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
    assert likely["matched"] == [
        "name: acme painting",
        "suburb/postcode: Parramatta",
        "name in profile URL",
    ]
    by_phone = score(who, "Painters", "https://x.test/p/1", ["+61 2 5550 0101"], "example.com")
    assert by_phone["state"] == FOUND_LIKELY and by_phone["score"] == 5
    name_only = score(who, "Acme Painting reviews", "https://x.test/p/2", [])
    assert name_only["state"] == FOUND_VERIFY and name_only["score"] == 3
    # A suburb alone never makes a candidate: the name or the phone must match.
    unrelated = score(who, "Bob's Plumbing Parramatta", "https://x.test/bob", [])
    assert unrelated["state"] == "" and unrelated["score"] == 1


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


def test_serp_cost_cap_is_checked_before_every_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", fake_serp({}, calls))
    deadline = 1e12
    # Two calls fit under the cap at the estimate; the third would exceed it and is not made.
    ctx = context(tmp_path, cap=0.009, **CREDENTIALS)
    outcomes = [dataforseo.serp(ctx, "organic", f"q{i}", deadline) for i in range(4)]
    assert [o["status"] for o in outcomes] == ["ok", "ok", "refused", "refused"]
    assert "exceeds the remaining cap" in outcomes[2]["reason"] and len(calls) == 2
    assert ctx.dataforseo_spent_usd == Decimal("0.008")
    assert calls[0][1] == {
        "keyword": "q0",
        "location_code": 2036,
        "language_code": "en",
        "depth": 10,
    }
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


def test_discovery_states_nap_duplicates_unlinked_and_keywords(tmp_path, monkeypatch):
    responses = {
        "Acme Painting Parramatta": [
            {
                "type": "maps_search",
                "title": "Acme Painting",
                "address": "1 Church St, Parramatta NSW 2150",
                "phone": "+61 2 5550 0999",
                "domain": "example.com",
                "cid": "123456789",
            }
        ],
        '"Acme Painting" Parramatta': [
            {
                "type": "organic",
                "url": "https://www.instagram.com/acmepainting/",
                "title": "Acme Painting (@acmepainting) Parramatta",
                "description": "Painters in Parramatta",
            },
            {"type": "organic", "url": "https://example.com/", "title": "Acme Painting"},
        ],
        "painter parramatta": [
            {"type": "organic", "rank_group": 1, "url": "https://other.test/"},
            {"type": "organic", "rank_group": 3, "url": "https://www.example.com/services"},
        ],
        'site:hipages.com.au "Acme Painting"': [
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
        'site:facebook.com "Acme Painting"': [
            {
                "type": "organic",
                "url": "https://www.facebook.com/acmepainting/",
                "title": "Acme Painting | Parramatta",
            }
        ],
        'site:truelocal.com.au "Acme Painting"': [
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
    assert discovery["keywords"][0]["state"] == "Position 3"
    assert discovery["keywords"][0]["url"] == "https://www.example.com/services"
    findings = {f.check_key: f for f in result.findings}
    assert "0255500999" in findings["platform:external-nap-inconsistent"].detail.replace(" ", "")
    assert "acme-painting-services" in findings["platform:possible-duplicates"].detail
    unlinked = findings["platform:profiles-not-linked"].detail
    assert "instagram.com/acmepainting" in unlinked and "facebook" not in unlinked
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
