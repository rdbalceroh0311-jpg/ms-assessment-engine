"""Inventories use synthetic HTTP responses or a loopback fixture, never live websites."""

import functools
import http.server
import json
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ams_assess import coverage
from ams_assess.models import Context, ModuleResult, create_run_dir
from ams_assess.modules import browser, presence, siteone

REPO = Path(__file__).resolve().parents[1]


def context(tmp_path, **kwargs):
    return replace(
        Context(
            "fixture",
            "Synthetic",
            "http://127.0.0.1/",
            {"seo"},
            set(),
            {},
            create_run_dir(tmp_path, "fixture"),
            REPO / "tools",
        ),
        **kwargs,
    )


def test_sitemap_reconciliation_and_public_wp_inventory_on_local_fixture(tmp_path, monkeypatch):
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            requests.append(
                (self.path, self.headers.get("User-Agent"), self.headers.get("Authorization"))
            )
            base = f"http://127.0.0.1:{server.server_port}"
            path = self.path.split("?", 1)[0]
            if path == "/sitemap.xml":
                body = (
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<sitemap><loc>{base}/child.xml</loc></sitemap></sitemapindex>"
                ).encode()
            elif path == "/child.xml":
                body = (
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{base}/unlinked.html</loc></url></urlset>"
                ).encode()
            elif path == "/wp-json/wp/v2/types":
                body = json.dumps(
                    {
                        "page": {"rest_base": "pages"},
                        "post": {"rest_base": "posts"},
                        "product": {"rest_base": "products", "rest_namespace": "wp/v2"},
                        "private": {"rest_base": "private", "viewable": False},
                    }
                ).encode()
            elif path.startswith("/wp-json/wp/v2/"):
                body = json.dumps(
                    [
                        {
                            "link": base + "/" + path.rsplit("/", 1)[-1] + "/one",
                            "status": "publish",
                        },
                        {"link": "http://elsewhere.invalid/private", "status": "publish"},
                        {"link": base + "/draft", "status": "draft"},
                    ]
                ).encode()
            else:
                return super().do_GET()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(REPO / "fixtures/site"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx = context(tmp_path, target=f"http://127.0.0.1:{server.server_port}/")
        crawl = ModuleResult(name="siteone", collection_started=True)
        crawl.add_evidence(ctx.site_url, "SiteOne HTTP response", {"type": 1, "status": 200})
        rendered = ModuleResult(name="browser")
        rendered.add_evidence(
            ctx.site_url,
            "Rendered page evidence (desktop)",
            {"page": {"script_srcs": ["/wp-content/a.js"]}, "axe": {}},
        )
        sitemap = ModuleResult(name="site_checks")
        sitemap.add_evidence(
            ctx.site_url, "Reused robots/sitemap collector", presence.sitemap_check(ctx)
        )
        ctx.results, ctx.pages = [crawl, rendered, sitemap], [ctx.site_url]
        result = coverage.collect(ctx)
        assert result["known_urls"] == 5
        assert result["summary"].startswith("1 of 5 known URLs crawled, 1 browser-tested")
        assert ctx.site_url + "unlinked.html" in result["sitemap_not_reached"]
        assert ctx.site_url + "unlinked.html" in result["untested"]
        assert len(result["wordpress"]["urls"]) == 3
        assert all("draft" not in row["url"] for row in result["discovered"])
        rest = [r for r in requests if r[0].startswith("/wp-json")]
        assert len(rest) == 4
        assert all(ua == "AMSAssessment/1.0" and auth is None for _, ua, auth in rest)
        assert all("per_page=100" in path and "status=publish" in path for path, _, _ in rest[1:])
        assert len([path for path, _, _ in requests if path == "/sitemap.xml"]) == 1
        assert len([path for path, _, _ in requests if path == "/child.xml"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_wp_inventory_bounds_pages_and_only_follows_safe_public_collections(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(coverage, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(coverage, "MAX_WP_PAGES", 2)
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "GET" and request.url.host == "127.0.0.1"
        assert request.headers["User-Agent"] == "AMSAssessment/1.0"
        if request.url.path.endswith("types"):
            return httpx.Response(
                200,
                json={
                    "page": {"rest_base": "pages"},
                    "bad": {"rest_base": "https://evil.invalid/"},
                },
            )
        return httpx.Response(
            200, json=[{"link": ctx.target + f"page/{i}", "status": "publish"} for i in range(100)]
        )

    result = coverage.wp_inventory(ctx, httpx.MockTransport(respond))
    assert len(seen) == 3 and len(result["urls"]) == 100
    assert result["status"] == "partial" and "budget" in result["reason"]
    assert "Unsafe REST collection excluded" in result["reason"]


@pytest.mark.parametrize("mode", ["blocked", "disabled", "redirect", "invalid", "timeout"])
def test_wp_unavailable_never_becomes_empty_complete_inventory(tmp_path, mode):
    seen = []

    def respond(request):
        seen.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic query?token=secret")
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://external.invalid/"})
        return httpx.Response({"blocked": 403, "disabled": 404, "invalid": 200}[mode], text="bad")

    result = coverage.wp_inventory(context(tmp_path), httpx.MockTransport(respond))
    assert result["status"] == "unavailable"
    assert coverage.UNAVAILABLE in result["reason"] and len(seen) == 1
    assert "secret" not in json.dumps(result)


def test_wp_deadline_and_sitemap_cycles_are_bounded(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(coverage, "INVENTORY_SECONDS", 0)
    result = coverage.wp_inventory(
        ctx, httpx.MockTransport(lambda r: pytest.fail("deadline ignored"))
    )
    assert result["status"] == "unavailable"
    monkeypatch.setattr(coverage, "INVENTORY_SECONDS", 10)
    sitemap = ModuleResult(name="site_checks")
    sitemap.add_evidence(
        ctx.target,
        "Reused robots/sitemap collector",
        {"sitemap_url": ctx.target + "root.xml", "sitemap_children": [ctx.target + "child.xml"]},
    )
    ctx.results = [sitemap]
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            text='<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<sitemap><loc>{ctx.target}root.xml</loc></sitemap></sitemapindex>",
        )

    result = coverage.collect(ctx, httpx.MockTransport(respond))
    assert len(seen) == 1 and result["known_urls"] == 0


@pytest.mark.parametrize("suffix", ["json", "csv"])
def test_authorised_export_is_a_separate_source_and_never_crawls(tmp_path, suffix):
    path = tmp_path / f"inventory.{suffix}"
    urls = [
        "http://127.0.0.1/unlinked",
        "https://external.invalid/",
        "http://user:secret@127.0.0.1/",
    ]
    path.write_text(json.dumps({"urls": urls}) if suffix == "json" else "url\n" + "\n".join(urls))
    ctx = context(tmp_path, wp_inventory=path)
    result = coverage.collect(ctx, httpx.MockTransport(lambda r: pytest.fail("Unexpected fetch")))
    assert result["discovered"] == [{"url": urls[0], "sources": ["authorised WordPress export"]}]
    assert result["untested"] == [urls[0]] and not result["crawled"]
    assert len(result["excluded"]) == 2 and "secret" not in json.dumps(result)


def test_failed_blocked_redirected_and_browser_sample_are_accounted_separately(tmp_path):
    ctx = context(tmp_path, browser_pages=1)
    crawl = ModuleResult(name="siteone", collection_started=True, reason="Crawl cap reached")
    for path, status in [("ok", 200), ("missing", 404), ("forbidden", 403), ("redirect", 301)]:
        crawl.add_evidence(
            ctx.target + path, "SiteOne HTTP response", {"type": 1, "status": status}
        )
    ctx.results, ctx.pages = [crawl], [ctx.target + "ok"]
    result = coverage.collect(ctx)
    assert len(result["failed"]) == len(result["blocked"]) == len(result["redirected"]) == 1
    assert result["known_urls"] == 4 and len(result["crawled"]) == 1
    assert not result["browser_tested"] and result["limits"]["browser_pages"] == 1
    assert "Crawl cap reached" in result["stop_reasons"]
    assert browser.select_pages([ctx.target + "ok", ctx.target], ctx.target, 1) == [ctx.target]


def test_siteone_and_axe_emit_pass_fail_and_viewport_coverage_on_fixture(tmp_path):
    if not (REPO / "tools/siteone/siteone-crawler/siteone-crawler").is_file():
        pytest.skip("SiteOne missing; run scripts/setup.sh")
    if not (REPO / "tools/axe.min.js").is_file():
        pytest.skip("axe missing; run scripts/setup.sh")
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(REPO / "fixtures/site")
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx = context(
            tmp_path,
            target=f"http://127.0.0.1:{server.server_port}/",
            browser_pages=1,
            max_pages=15,
        )
        result = siteone.run(ctx)
        assert result.status == "ok"
        assert {o.status for o in result.outcomes} >= {"pass", "fail"}
        ctx.pages = [ctx.site_url + "quote.html"]
        result = browser.run(ctx)
        assert result.status == "ok"
        assert {o.status for o in result.outcomes} >= {"pass", "fail", "out-of-scope"}
        assert {o.component.rsplit("::", 1)[-1] for o in result.outcomes} == {"desktop", "mobile"}
        assert all(o.evidence_ids for o in result.outcomes)
        assert len({e.url for e in result.evidence}) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sitemap_collection_runs_without_presence_or_site_checks_and_stays_bounded(tmp_path):
    ctx = context(tmp_path, categories={"accessibility"})
    ctx.results = [ModuleResult(name="siteone", collection_started=True)]
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="Sitemap: /sitemap.xml")
        return httpx.Response(
            200,
            text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>/unlinked.html</loc></url></urlset>",
        )

    result = coverage.collect(ctx, httpx.MockTransport(respond))
    assert len(seen) == 2
    assert result["sitemap_not_reached"] == [ctx.target + "unlinked.html"]


def test_cli_accepts_browser_bound_and_authorised_export(tmp_path, monkeypatch):
    from ams_assess import cli

    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "config", dict)
    path = tmp_path / "export.json"
    path.write_text('["https://example.test/unlinked"]')
    captured = []

    def run(ctx, **kwargs):
        captured.append(ctx)
        return {"status": "complete", "findings": []}

    monkeypatch.setattr(cli, "run", run)
    assert (
        cli.main(
            [
                "run",
                "--customer",
                "synthetic",
                "--target",
                "example.test",
                "--browser-pages",
                "2",
                "--wp-inventory",
                str(path),
                "--no-ai",
            ]
        )
        == 0
    )
    assert captured[0].browser_pages == 2 and captured[0].wp_inventory == path
    with pytest.raises(SystemExit):
        cli.main(
            ["run", "--customer", "synthetic", "--target", "example.test", "--browser-pages", "51"]
        )


def test_known_url_accounting_never_drops_successful_crawls_at_a_larger_budget(tmp_path):
    ctx = context(tmp_path, max_pages=6000)
    ctx.pages = [ctx.target + str(i) for i in range(5001)]
    result = coverage.collect(ctx)
    assert result["known_urls"] == len(result["crawled"]) == 5001
    assert result["limits"]["known_urls"] == 6000
