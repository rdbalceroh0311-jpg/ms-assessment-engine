import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from ams_assess import cli, runner, store
from ams_assess.models import Context, create_run_dir
from ams_assess.modules import performance, site_checks
from ams_assess.modules.site_rules import field_vitals

NO_DATA = {
    "error": {"code": 404, "status": "NOT_FOUND", "message": "chrome ux report data not found"}
}
FIELD = {
    "record": {
        "key": {"origin": "https://example.com"},
        "metrics": {"interaction_to_next_paint": {"percentiles": {"p75": 175}}},
        "collectionPeriod": {"firstDate": {"year": 2026, "month": 10, "day": 1}},
    }
}


def context(tmp_path, **config):
    return Context(
        customer_id="example",
        customer_name="Example",
        target="https://example.com/",
        categories={"performance"},
        connections=set(),
        config=config,
        run_dir=create_run_dir(tmp_path, "example"),
        tools_dir=tmp_path,
        store_root=tmp_path,
    )


def psi_body(url, strategy, score=0.95, lcp=2300, version="13.0"):
    return {
        "lighthouseResult": {
            "requestedUrl": url,
            "finalUrl": url,
            "fetchTime": "2026-10-24T00:00:00.000Z",
            "lighthouseVersion": version,
            "categories": {"performance": {"score": score}},
            "configSettings": {
                "formFactor": strategy,
                "throttlingMethod": "simulate",
                "throttling": {"rttMs": 150, "cpuSlowdownMultiplier": 4},
            },
            "audits": {
                "largest-contentful-paint": {"numericValue": lcp},
                "total-blocking-time": {"numericValue": 300},
                "cumulative-layout-shift": {"numericValue": 0.05},
            },
        }
    }


def mock_http(monkeypatch, field_status=200, field_body=None, score=0.95, lcp=2300, version="13.0"):
    calls = []

    def post(url, **kwargs):
        calls.append(("crux", kwargs))
        return httpx.Response(
            field_status,
            json=field_body if field_body is not None else FIELD,
            request=httpx.Request("POST", url),
        )

    def get(url, **kwargs):
        calls.append(("psi", kwargs))
        p = kwargs["params"]
        return httpx.Response(
            200,
            json=psi_body(p["url"], p["strategy"], score, lcp, version),
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    return calls


def summary(result):
    return result.evidence[-1].data["performance"]


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (200, FIELD, "data"),
        (404, NO_DATA, "no_eligible_data"),
        (403, {"error": {"message": "secret-key"}}, "permission"),
        (429, {}, "quota"),
        (500, {}, "error"),
        (404, {}, "error"),
        (
            404,
            {"error": {"code": 404, "status": "NOT_FOUND", "message": "wrong endpoint"}},
            "error",
        ),
        (200, {"record": {"metrics": {}}}, "error"),
        (200, [], "error"),
        (200, {"record": {"metrics": {"interaction_to_next_paint": None}}}, "error"),
    ],
)
def test_crux_url_and_origin_outcomes(tmp_path, monkeypatch, status, body, outcome):
    ctx = context(tmp_path, CRUX_API_KEY="secret-key")
    calls = mock_http(monkeypatch, status, body)
    result = performance.run(ctx)
    fields = summary(result)["field"]
    assert [f["outcome"] for f in fields] == [outcome, outcome]
    requests = [kw["json"] for kind, kw in calls if kind == "crux"]
    assert requests == [{"url": ctx.target}, {"origin": "https://example.com"}]
    assert not any("formFactor" in r for r in requests)
    encoded = result.model_dump_json() + (ctx.run_dir / "raw/performance.json").read_text()
    assert "secret-key" not in encoded
    assert "insufficient traffic" not in encoded and "not enough traffic" not in encoded
    if outcome == "no_eligible_data":
        assert "No eligible CrUX field data for this url" in fields[0]["detail"]
        assert "does not establish the cause" in fields[0]["detail"]
    if outcome == "data":
        assert (
            fields[0]["record"]["metrics"]["interaction_to_next_paint"]["percentiles"]["p75"] == 175
        )
        assert not summary(result)["rum_recommendation"]
    elif outcome == "no_eligible_data":
        assert "Recommendation only; nothing installed" in summary(result)["rum_recommendation"]
    else:
        assert not summary(result)["rum_recommendation"]
        assert all("field data status unknown" in f["detail"] for f in fields)


@pytest.mark.parametrize("failure", ["timeout", "invalid_json"])
def test_crux_transport_and_invalid_json_are_errors(tmp_path, monkeypatch, failure):
    mock_http(monkeypatch)

    def post(url, **kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-key")
        return httpx.Response(200, text="not json secret-key")

    monkeypatch.setattr(httpx, "post", post)
    result = performance.run(context(tmp_path, CRUX_API_KEY="secret-key"))
    assert all(f["outcome"] == "error" for f in summary(result)["field"])
    assert "secret-key" not in result.model_dump_json()


def test_representative_pages_are_bounded_repeatable_and_record_conditions(tmp_path, monkeypatch):
    ctx = context(
        tmp_path,
        CRUX_API_KEY="secret",
        AMS_PERFORMANCE_KEY_PAGES="2",
        AMS_PERFORMANCE_PAGES='["https://example.com/services"]',
    )
    ctx.pages = [
        "https://other.example/path",
        "https://example.com/z",
        "https://example.com/about",
        "https://example.com/about",
        "https://user:pass@example.com/private",
        "https://example.com/?token=secret",
        "https://example.com/z#fragment",
    ]
    calls = mock_http(monkeypatch)
    result = performance.run(ctx)
    p = summary(result)
    assert p["pages"] == [ctx.target, "https://example.com/services", "https://example.com/about"]
    assert len([c for c in calls if c[0] == "psi"]) == 6
    assert len([c for c in calls if c[0] == "crux"]) == 4
    assert p["psi_request_cap"] == 6 and p["crux_request_cap"] == 4
    for lab in p["lab"]:
        c = lab["conditions"]
        assert datetime.fromisoformat(c["requested_at_utc"]).utcoffset() == timedelta(0)
        assert c["url_tested"] == c["final_url"] == lab["url"]
        assert c["strategy"] == c["form_factor"] == lab["strategy"]
        assert c["lighthouse_version"] == "13.0" and c["throttling_method"] == "simulate"
        assert c["throttling"]["rttMs"] == 150
        assert lab["kind"] == "LAB" and "interaction_to_next_paint" not in lab["metrics"]
        assert lab["metrics"]["total-blocking-time"]["numericValue"] == 300
    ctx.pages.reverse()
    assert performance.representative_pages(ctx) == p["pages"]


def test_homepage_included_for_path_target_and_default_cap(tmp_path):
    ctx = context(tmp_path)
    ctx.target = "https://example.com/services"
    ctx.pages = [f"https://example.com/page{i}" for i in range(50)]
    assert performance.representative_pages(ctx) == [
        "https://example.com/",
        ctx.target,
        "https://example.com/page0",
        "https://example.com/page1",
    ]
    ctx.config["AMS_PERFORMANCE_KEY_PAGES"] = "0"
    assert performance.representative_pages(ctx) == ["https://example.com/"]


@pytest.mark.parametrize("limit", ["-1", "11", "invalid"])
def test_page_limit_rejected_before_http(tmp_path, limit):
    with pytest.raises(ValueError):
        performance.representative_pages(context(tmp_path, AMS_PERFORMANCE_KEY_PAGES=limit))


@pytest.mark.parametrize("page", ["https://elsewhere.example/a", "https://example.com/?key=x"])
def test_explicit_page_validation(tmp_path, page):
    with pytest.raises(ValueError):
        performance.representative_pages(
            context(tmp_path, AMS_PERFORMANCE_PAGES=json.dumps([page]))
        )


def test_keyless_field_is_not_no_data_and_local_skips_http(tmp_path, monkeypatch):
    calls = mock_http(monkeypatch)
    ctx = context(tmp_path)
    result = performance.run(ctx)
    assert {f["outcome"] for f in summary(result)["field"]} == {"not_configured"}
    assert all(kind == "psi" for kind, _ in calls)
    ctx.target = "http://localhost/"
    calls.clear()
    assert performance.run(ctx).status == "skipped" and not calls


@pytest.mark.parametrize(
    "body",
    [[], {"lighthouseResult": None}, {"lighthouseResult": {"runtimeError": {"message": "secret"}}}],
)
def test_psi_malformed_or_runtime_error_is_not_a_measurement(tmp_path, monkeypatch, body):
    mock_http(monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(200, json=body))
    result = performance.run(context(tmp_path))
    assert result.status == "skipped" and not result.findings
    assert all(lab["outcome"] == "error" for lab in summary(result)["lab"])


def test_credentials_echoed_in_success_payload_are_redacted(tmp_path, monkeypatch):
    mock_http(monkeypatch)
    payload = psi_body("https://example.com/", "mobile")
    payload["lighthouseResult"]["lighthouseVersion"] = "secret-key"
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(200, json=payload))
    ctx = context(tmp_path, PAGESPEED_API_KEY="secret-key")
    result = performance.run(ctx)
    assert "secret-key" not in result.model_dump_json()
    assert "secret-key" not in (ctx.run_dir / "raw/performance.json").read_text()


def registry(monkeypatch):
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {"performance": SimpleNamespace(categories={"performance"}, run=performance.run)},
    )


def test_before_after_reads_customer_store_and_renders_matching_metrics(tmp_path, monkeypatch):
    registry(monkeypatch)
    mock_http(monkeypatch, score=0.6, lcp=4000, version="12.0")
    first = context(tmp_path, CRUX_API_KEY="secret")
    runner.run(first, no_ai=True, formats={"md"})
    saved_path = store.store_file(tmp_path, "example")
    before_bytes = saved_path.read_bytes()
    # Reading/collecting alone must not mutate the prior customer store.
    mock_http(monkeypatch, score=0.9, lcp=2000)
    second = context(tmp_path, CRUX_API_KEY="secret")
    second.pages = ["https://example.com/new"]
    result = performance.run(second)
    assert saved_path.read_bytes() == before_bytes
    comparison = summary(result)["comparison"]
    assert comparison["prior_run_id"] == "r1" and len(comparison["rows"]) == 2
    for row in comparison["rows"]:
        assert row["url"] == first.target
        assert row["metrics"]["largest-contentful-paint"]["delta"] == -2000
        assert row["metrics"]["score"]["delta"] == 0.3
        assert "lighthouse_version" in row["conditions_changed"]
        assert row["before_evidence_id"].startswith("ev-performance-r1-")
    runner.run(second, no_ai=True, formats={"md"})
    report = (second.run_dir / "report.md").read_text()
    assert "Before / after lab comparison" in report and "-2000" in report
    assert "LAB results" in report and "not INP" in report and "175" in report
    assert "Recorded lab test conditions" in report
    # Persisted structured evidence remains usable for a subsequent comparison.
    third = context(tmp_path)
    assert performance.prior_measurements(third)[0] == "r2"


def test_comparison_excludes_other_sites_customers_and_unmatched_pages(tmp_path, monkeypatch):
    registry(monkeypatch)
    mock_http(monkeypatch)
    first = context(tmp_path)
    runner.run(first, no_ai=True, formats={"md"})
    other_site = context(tmp_path)
    other_site.target = "https://other.example/"
    assert performance.prior_measurements(other_site)[1] == []
    other_customer = context(tmp_path)
    other_customer.customer_id = "different"
    assert performance.prior_measurements(other_customer)[1] == []
    same = context(tmp_path)
    unmatched = performance.measure_lab("https://example.com/new", "mobile", "")
    unmatched["evidence_id"] = "ev-performance-999"
    assert performance.compare_labs(same, [unmatched])["rows"] == []
    assert "No matching page" in performance.compare_labs(same, [unmatched])["detail"]


def test_comparison_store_failure_is_explicit_and_lab_collection_continues(tmp_path, monkeypatch):
    mock_http(monkeypatch)
    monkeypatch.setattr(store, "load", lambda *a: (_ for _ in ()).throw(ValueError("secret")))
    result = performance.run(context(tmp_path))
    assert result.status == "ok"
    assert "could not be read" in summary(result)["comparison"]["detail"]


def test_report_exposes_no_data_and_lab_failure_without_findings(tmp_path, monkeypatch):
    registry(monkeypatch)
    mock_http(monkeypatch, 404, NO_DATA)
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(429))
    ctx = context(tmp_path, CRUX_API_KEY="secret")
    data = runner.run(ctx, no_ai=True, formats={"md"})
    assert not data["findings"]
    report = (ctx.run_dir / "report.md").read_text()
    assert "no eligible data" in report and "HTTP 429" in report
    assert "Recommendation only; nothing installed" in report


def test_cli_performance_flags_reach_context(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(cli, "config", lambda: {})
    monkeypatch.setattr(cli, "data_root", lambda: tmp_path)
    monkeypatch.setattr(
        cli, "run", lambda ctx, **kw: captured.append(ctx) or {"status": "complete", "findings": []}
    )
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ams-assess",
            "run",
            "--customer",
            "example",
            "--target",
            "https://example.com",
            "--categories",
            "performance",
            "--performance-key-pages",
            "1",
            "--performance-page",
            "https://example.com/contact",
            "--no-ai",
            "--formats",
            "md",
        ],
    )
    cli.main()
    assert performance.representative_pages(captured[0]) == [
        "https://example.com/",
        "https://example.com/contact",
    ]


def test_url_no_data_does_not_hide_origin_data(tmp_path, monkeypatch):
    mock_http(monkeypatch)

    def post(url, **kwargs):
        return (
            httpx.Response(404, json=NO_DATA)
            if "url" in kwargs["json"]
            else httpx.Response(200, json=FIELD)
        )

    monkeypatch.setattr(httpx, "post", post)
    p = summary(performance.run(context(tmp_path, CRUX_API_KEY="secret")))
    assert [f["outcome"] for f in p["field"]] == ["no_eligible_data", "data"]
    assert p["field"][1]["scope"] == "origin" and p["rum_recommendation"]


def test_redirected_key_pages_keep_paths_and_bound_all_requests(tmp_path, monkeypatch):
    ctx = context(
        tmp_path,
        CRUX_API_KEY="secret",
        AMS_PERFORMANCE_KEY_PAGES="10",
        AMS_PERFORMANCE_PAGES='["https://example.com/contact"]',
    )
    ctx.resolved_url = "https://www.example.com/"
    ctx.pages = [f"https://www.example.com/page{i}" for i in range(30)]
    calls = mock_http(monkeypatch)
    p = summary(performance.run(ctx))
    assert p["pages"][:2] == [ctx.site_url, "https://www.example.com/contact"]
    assert len(p["pages"]) == 11
    assert len([c for c in calls if c[0] == "psi"]) == 22
    assert len([c for c in calls if c[0] == "crux"]) == 12
    assert all(kw["timeout"] in {30, 90} for _, kw in calls)


def test_legacy_lab_history_is_compared_with_missing_conditions_explicit(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    saved = store.new_store(ctx.customer_id, ctx.customer_name)
    saved["runs"] = [
        {"run_id": "r1", "kind": "assessment", "status": "complete", "target": ctx.target},
        {"run_id": "r2", "kind": "assessment", "status": "partial", "target": ctx.target},
    ]
    saved["evidence"] = [
        {
            "id": "ev-performance-r1-001",
            "source": "performance",
            "url": ctx.target,
            "description": "PageSpeed mobile: lab and available field data",
            "data": {
                "strategy": "mobile",
                "score": 0.5,
                "metrics": {"total-blocking-time": {"numericValue": 900}},
            },
        }
    ]
    store.save(tmp_path, ctx.customer_id, saved)
    mock_http(monkeypatch)
    p = summary(performance.run(ctx))
    assert p["comparison"]["prior_run_id"] == "r1"
    (row,) = p["comparison"]["rows"]
    assert row["strategy"] == "mobile"
    assert row["metrics"]["total-blocking-time"]["delta"] == -600
    assert "throttling" in row["conditions_unrecorded"]
    assert "interaction_to_next_paint" not in row["metrics"]


def test_key_page_sample_prefers_customer_journeys(tmp_path):
    ctx = context(tmp_path, AMS_PERFORMANCE_KEY_PAGES="1")
    ctx.pages = ["https://example.com/aaa-archive", "https://example.com/services"]
    assert performance.representative_pages(ctx)[1] == "https://example.com/services"


@pytest.mark.parametrize("lab_error", [False, True])
def test_keyless_psi_field_data_survives_for_cwv_and_report(tmp_path, monkeypatch, lab_error):
    registry(monkeypatch)
    payload = psi_body("https://example.com/", "mobile")
    field = {
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2400},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 190},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 10},
        }
    }
    payload["loadingExperience"] = {"id": "https://example.com/", **field}
    payload["originLoadingExperience"] = {"id": "https://example.com", **field}
    if lab_error:
        payload["lighthouseResult"]["runtimeError"] = {"code": "FAILED_DOCUMENT_REQUEST"}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(200, json=payload))
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: pytest.fail("Keyless run queried CrUX"))
    ctx = context(tmp_path)
    runner.run(ctx, no_ai=True, formats={"md"})
    result = ctx.results[0]
    assert result.status == "ok"
    for lab in summary(result)["lab"]:
        assert lab["field_url"] == payload["loadingExperience"]
        assert lab["field_origin"] == payload["originLoadingExperience"]
        assert lab["field_data_label"] == "PSI-reported field data"
        metrics = field_vitals(lab)
        assert len(metrics) == 6 and all(m["grade"] == "good" for m in metrics)
        assert {m["scope"] for m in metrics} == {"URL", "origin"}
        assert {m["p75"] for m in metrics if m["metric"] == "INP"} == {190}
        assert {m["p75"] for m in metrics if m["metric"] == "CLS"} == {0.1}
    checks = site_checks.Checks(ctx, {})
    checks.performance()
    assert checks.result.checks[0]["status"] == "PASS"
    report = (ctx.run_dir / "report.md").read_text()
    assert "PSI-reported field data" in report and "190" in report
    assert "Field monitoring recommendation" not in report


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "not configured"),
        (403, "permission"),
        (429, "quota"),
        (500, "HTTP 500"),
    ],
)
def test_rum_not_recommended_when_field_availability_is_unknown(
    tmp_path, monkeypatch, status, reason
):
    registry(monkeypatch)
    mock_http(monkeypatch, status or 200, {})
    ctx = context(tmp_path, **({"CRUX_API_KEY": "secret"} if status else {}))
    runner.run(ctx, no_ai=True, formats={"md"})
    p = summary(ctx.results[0])
    assert not p["rum_recommendation"]
    assert all(
        "field data status unknown" in f["detail"] and reason in f["detail"] for f in p["field"]
    )
    report = (ctx.run_dir / "report.md").read_text()
    assert "field data status unknown" in report and reason in report
    assert "Field monitoring recommendation" not in report


def test_equal_lab_scores_remain_attributable_after_consolidation(tmp_path, monkeypatch):
    registry(monkeypatch)
    mock_http(monkeypatch, score=0.6)
    ctx = context(tmp_path)
    ctx.pages = ["https://example.com/contact"]
    data = runner.run(ctx, no_ai=True, formats={"md"})
    findings = data["findings"]
    assert len(findings) == 4 and len({f["check_key"] for f in findings}) == 4
    for page in [ctx.target, ctx.pages[0]]:
        for strategy in ["mobile", "desktop"]:
            (finding,) = [f for f in findings if f["check_key"] == f"lighthouse-{strategy}:{page}"]
            assert finding["urls"] == [page]
            assert page in finding["detail"] and strategy in finding["detail"]
            assert "60/100" in finding["detail"]


@pytest.mark.parametrize("cap", [0, 1])
def test_excess_explicit_performance_pages_are_reported(tmp_path, monkeypatch, cap):
    registry(monkeypatch)
    calls = mock_http(monkeypatch)
    explicit = ["https://example.com/contact", "https://example.com/services"]
    ctx = context(
        tmp_path,
        AMS_PERFORMANCE_KEY_PAGES=str(cap),
        AMS_PERFORMANCE_PAGES=json.dumps([explicit[0], explicit[0], explicit[1]]),
    )
    ctx.resolved_url = "https://www.example.com/"
    runner.run(ctx, no_ai=True, formats={"md"})
    p = summary(ctx.results[0])
    assert p["dropped_explicit_pages"] == explicit[cap:]
    assert len(p["pages"]) == cap + 1
    assert len([c for c in calls if c[0] == "psi"]) == (cap + 1) * 2
    raw = json.loads((ctx.run_dir / "raw/performance.json").read_text())["performance"]
    assert raw["dropped_explicit_pages"] == explicit[cap:]
    report = (ctx.run_dir / "report.md").read_text()
    line = next(line for line in report.splitlines() if "excluded by the key-page cap" in line)
    assert all(page in line for page in explicit[cap:])
    if cap:
        assert explicit[0] not in line
