"""Reviewer-supplied synthetic c4d0b12 exports, copied unchanged into fixtures/."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hosting_challenges import REASON, URL, response
from test_security_siteone_pinning import fake_crawler
from test_site_checks import context, crawl, run_checks, sitemap

from ams_assess import store
from ams_assess.models import Evidence
from ams_assess.modules import siteone

FIXTURES = Path(__file__).parent / "fixtures/hosting_challenges"
LEGACY = ["legacy_probe_only", "legacy_with_crawl", "legacy_with_crawl_and_browser"]


def legacy(name="legacy_with_crawl_and_browser"):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def load_legacy(tmp_path, data):
    saved = store.new_store("test", "Test")
    saved.update(data)
    store.save(tmp_path, "test", saved)
    path = store.store_file(tmp_path, "test")
    original = path.read_bytes()
    loaded = store.load(tmp_path, "test", prune_backups=False)
    assert loaded is not None
    return loaded, path, original


def assert_withdrawn(tmp_path, data):
    loaded, path, original = load_legacy(tmp_path, data)
    assert loaded["findings"] == []
    assert len(loaded["finding_history"]) == 1
    retired = loaded["finding_history"][0]
    assert retired["id"] == data["findings"][0]["id"]
    assert retired["status"] == "superseded" and REASON in retired["reason"]
    assert retired["evidence_ids"] == data["findings"][0]["evidence_ids"]
    assert loaded["evidence"] == data["evidence"]
    assert next(path.parent.glob("assessment.json.backup-*")).read_bytes() == original
    again = store.load(tmp_path, "test", prune_backups=False)
    assert again is not None and again["finding_history"] == loaded["finding_history"]


@pytest.mark.parametrize("fixture", LEGACY)
def test_real_legacy_exports_with_healthy_coverage_are_withdrawn(tmp_path, fixture):
    assert_withdrawn(tmp_path, legacy(fixture))


@pytest.mark.parametrize(
    "body,headers",
    [
        ("<html><style>.hidden { display:none; }</style><h1>Real page</h1></html>", {}),
        ("<html><p>What noindex and none mean</p></html>", {}),
        ("<html>Real page</html>", {"X-Layout": "none"}),
        ('<meta name="description" content="noindex and none explained">', {}),
        ('<script>var example = \'<meta name="robots" content="noindex">\';</script>', {}),
    ],
    ids=["display-none", "prose", "unrelated-header", "description-meta", "script-text"],
)
def test_secondary_probe_incidental_words_do_not_support_noindex(tmp_path, body, headers):
    data = legacy("legacy_probe_only")
    probe = next(
        e for e in data["evidence"] if e["description"] == "Bounded site-check HTTP probes"
    )
    probe["data"]["search"] = response(status=200, body=body, headers=headers)
    assert_withdrawn(tmp_path, data)


@pytest.mark.parametrize(
    "support",
    [
        "crawl-header",
        "browser-robots",
        "browser-googlebot",
        "secondary-header",
        "secondary-robots",
        "secondary-googlebot",
        "aggregate",
        "jsd-primary",
        "seo-noindex",
    ],
)
def test_legacy_finding_with_genuine_directive_support_is_kept(tmp_path, support):
    data = legacy()
    by_id = {e["id"]: e for e in data["evidence"]}
    probes = by_id["ev-site_checks-001"]["data"]
    row = by_id["ev-site_checks-003"]["data"]
    if support == "crawl-header":
        by_id["ev-siteone-001"]["data"]["headers"] = {"X-Robots-Tag": ["googlebot: none"]}
    elif support.startswith("browser-"):
        agent = support.removeprefix("browser-")
        by_id["ev-browser-001"]["data"]["page"]["meta"] = [{"name": agent, "content": "none"}]
    elif support == "secondary-header":
        probes["search"] = response(
            status=200, body="Real page", headers={"X-Robots-Tag": "noindex"}
        )
    elif support.startswith("secondary-"):
        agent = support.removeprefix("secondary-")
        probes["search"] = response(
            status=200, headers={}, body=f'<meta name="{agent}" content="NOINDEX">'
        )
    elif support == "aggregate":
        aggregate = Evidence(
            id="ev-siteone-005",
            source="siteone",
            url=URL,
            description="SiteOne headers-values table",
            data=[{"header": "X-Robots-Tag", "value": "noindex"}],
        )
        data["evidence"].append(aggregate.model_dump())
        row["detail"]["aggregate_header_evidence"] = [aggregate.id]
    elif support == "jsd-primary":
        probes["normal"] = response(
            status=200,
            headers={"x-robots-tag": ["noindex"]},
            body=(
                "<html><script>var a=document.createElement('script');"
                "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script></html>"
            ),
        )
    else:
        by_id["ev-siteone-003"]["data"]["robotsIndex"] = "0"
    loaded, path, _ = load_legacy(tmp_path, data)
    assert loaded["findings"] == data["findings"]
    assert not loaded["finding_history"]
    assert not list(path.parent.glob("assessment.json.backup-*"))


def robots_result(tmp_path, monkeypatch, status, challenged_html=False, challenged_robots=False):
    ctx = context(tmp_path)
    rows = [{"url": URL, "status": 200, "type": 1}]
    if challenged_html:
        rows.append(response(url=URL + "blocked", type=1))
    if challenged_robots:
        rows.append(response(url=URL + "robots.txt", status=status, type=0))
    fake_crawler(monkeypatch, ctx, rows)
    run = siteone.subproc.run

    def with_summary(*args, **kwargs):
        output = json.loads(run(*args, **kwargs).stdout)
        output["summary"] = {
            "items": [{"aplCode": "robots-txt-result", "text": f"robots.txt status code {status}"}]
        }
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(output))

    monkeypatch.setattr(siteone.subproc, "run", with_summary)
    return siteone.run(ctx)


@pytest.mark.parametrize("status", [403, 503])
@pytest.mark.parametrize("challenged_html", [False, True], ids=["healthy-crawl", "challenged-page"])
def test_nonchallenge_robots_errors_keep_original_fail_and_finding(
    tmp_path, monkeypatch, status, challenged_html
):
    result = robots_result(tmp_path, monkeypatch, status, challenged_html=challenged_html)
    checks = [o for o in result.outcomes if o.check_id == "robots-txt-missing"]
    assert len(checks) == 1 and checks[0].status == "fail"
    assert any(
        f.check_key == "robots-txt-missing" and f.title == "robots.txt not available"
        for f in result.findings
    )


@pytest.mark.parametrize("status", [403, 503])
def test_proven_challenged_robots_errors_remain_unassessed(tmp_path, monkeypatch, status):
    result = robots_result(tmp_path, monkeypatch, status, challenged_robots=True)
    assert not any(f.check_key == "robots-txt-missing" for f in result.findings)
    assert not any(
        o.check_id == "robots-txt-missing" and o.status in {"pass", "fail"} for o in result.outcomes
    )
    assert any(
        o.component == URL + "robots.txt" and o.status == "unavailable" and o.reason == REASON
        for o in result.outcomes
    )


def test_noindex_pass_explains_excluded_challenged_crawl_page(tmp_path, monkeypatch):
    module = crawl({URL: 200}, {URL: {"robotsIndex": "1"}})
    module.add_evidence(URL + "blocked", "SiteOne HTTP response", response(url=URL + "blocked"))
    ctx = context(tmp_path, categories={"seo"}, results=[module, sitemap()])
    _, rows = run_checks(ctx, monkeypatch)
    assert rows["noindex"]["status"] == "PASS"
    assert "Excluded 1 crawl pages" in rows["noindex"]["checked"]
    assert REASON in rows["noindex"]["checked"]
