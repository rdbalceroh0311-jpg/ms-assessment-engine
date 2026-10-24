"""Customer-report regressions reproduced from synthetic responses and a loopback site."""

import http.server
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ams_assess import runner, targets
from ams_assess.models import Context, create_run_dir
from ams_assess.modules import observatory, performance, security, siteone
from ams_assess.report import markdown_html, mdtext, render_markdown, write_outputs

REPO = Path(__file__).resolve().parents[1]


def context(tmp_path, **changes):
    return replace(
        Context(
            customer_id="synthetic-quality",
            customer_name="Synthetic & Company",
            target="https://example.com/",
            categories={"seo", "security"},
            connections=set(),
            config={},
            run_dir=create_run_dir(tmp_path, "synthetic-quality"),
            tools_dir=REPO / "tools",
        ),
        **changes,
    )


def test_page_budget_excludes_assets_and_retains_html_header_evidence(tmp_path):
    if not (REPO / "tools/siteone/siteone-crawler/siteone-crawler").is_file():
        pytest.skip("Run scripts/setup.sh for SiteOne")
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path == "/robots.txt":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"User-agent: *\nAllow: /\n")
                return
            if self.path not in {"/", "/one.html", "/two.html"}:
                self.send_error(404)
                return
            body = (
                '<html><head><title>Fixture</title><link rel="stylesheet" href="/asset.css">'
                '<link rel="preload" href="/asset.woff" as="font">'
                '<script src="/asset.js"></script></head><body>'
                + "".join(f'<img src="/image-{i}.png">' for i in range(12))
                + '<a href="/one.html">One</a><a href="/two.html">Two</a></body></html>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx = context(tmp_path, target=f"http://127.0.0.1:{server.server_port}/", max_pages=3)
        result = siteone.run(ctx)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.status == "ok" and len(ctx.pages) == 3
    assert set(requests) == {"/", "/robots.txt", "/one.html", "/two.html"}
    raw = json.loads((ctx.run_dir / "raw/siteone.json").read_text())
    assert raw["options"]["disableAllAssets"] is True
    assert raw["options"]["workers"] == 2 and raw["options"]["maxReqsPerSec"] == 5
    assert "3 successful HTML pages" in result.reason and "Crawl cap reached" in result.reason
    assert len([e for e in result.evidence if e.description == "SiteOne HTTP response"]) == 3
    header_rows = [e for e in result.evidence if "header" in e.description.lower()]
    assert header_rows and "nosniff" in json.dumps([e.data for e in header_rows])


def test_headers_framing_and_observatory_form_one_cited_action(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    ctx.tools_dir = tmp_path / "tools"
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    headers = ["content-security-policy", "strict-transport-security", "referrer-policy"]
    rows = [{"header": h, "critical": "1", "recommendation": f"{h} is not set."} for h in headers]
    rows += [
        {"header": "set-cookie", "critical": "1", "recommendation": ""},
        {"header": "server", "warning": "1", "recommendation": "Server identifies nginx."},
    ]
    monkeypatch.setattr(
        siteone.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "results": [{"url": ctx.target, "status": "200", "type": 1}],
                    "tables": {"security": {"rows": rows}},
                }
            ),
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers={"server": "nginx"},
        ),
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"grade": "D", "tests_failed": 3},
        ),
    )

    def unavailable(*args, **kwargs):
        raise OSError("mock TLS unavailable")

    monkeypatch.setattr(security.socket, "create_connection", unavailable)
    monkeypatch.setattr(runner, "REGISTRY", {m.name: m for m in [siteone, security, observatory]})
    data = runner.run(ctx, no_ai=True, formats={"md"})
    groups = [f for f in data["findings"] if f["check_key"] == "security-headers"]
    assert len(groups) == 1
    group = groups[0]
    assert all(h in group["detail"] for h in [*headers, "Framing protection"])
    assert "importance: medium" in group["detail"] and "importance: low" in group["detail"]
    assert "Observatory grade D" in group["detail"]
    assert "does not identify individual header failures" in group["detail"]
    assert all(eid in group["detail"] for eid in group["evidence_ids"])
    assert not any(
        f["check_key"]
        in {
            "framing-policy",
            "observatory-review",
            "header-review:server",
            "header-review:set-cookie",
        }
        for f in data["findings"]
    )
    assert any(f["check_key"] == "tls-unverified" for f in data["findings"])
    report = (ctx.run_dir / "report.md").read_text()
    assert report.count("#### " + group["id"] + ":") == 1
    assert "**AMS service category:** Managed IT & Technology" in report
    assert "&amp;" not in report


@pytest.mark.parametrize("concrete", [True, False])
def test_cookie_and_server_findings_require_specific_response_values(
    tmp_path, monkeypatch, concrete
):
    ctx = context(tmp_path, target="http://localhost/")
    headers = {
        "server": "nginx/1.24.0" if concrete else "nginx",
        "set-cookie": "session=private-value" + ("" if concrete else "; HttpOnly; Secure"),
    }
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers=headers,
        ),
    )
    result = security.run(ctx)
    findings = {f.check_key: f for f in result.findings}
    assert ("header-review:server" in findings) is concrete
    assert ("header-review:set-cookie" in findings) is concrete
    if concrete:
        assert "nginx/1.24.0" in findings["header-review:server"].detail
        assert "Cookie session lacks HttpOnly" in findings["header-review:set-cookie"].detail
    assert "private-value" not in json.dumps(result.model_dump())
    assert "private-value" not in (ctx.run_dir / "raw/security.json").read_text()


def test_markdown_master_keeps_ampersands_and_html_stage_escapes_untrusted_text(tmp_path):
    value = (
        "A & B &amp; C <script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:bad)"
    )
    escaped = mdtext(value)
    assert "A & B & C" in escaped and "&amp;" not in escaped
    write_outputs(tmp_path, "# Title & Company\n\n" + escaped + "<br>End", {"md", "html"})
    document = (tmp_path / "report.html").read_text()
    assert "A &amp; B &amp; C" in document and "<br />" in document
    assert "&lt;script&gt;" in document and "&lt;img" in document
    assert "<script>" not in document and "<img " not in document and "href=" not in document
    assert "<script>" not in markdown_html("<script>alert(1)</script>")
    assert "<img " not in markdown_html("<img src=x onerror=alert(1)>")


@pytest.mark.parametrize(
    "status,body,no_data",
    [
        (
            404,
            {
                "error": {
                    "code": 404,
                    "status": "NOT_FOUND",
                    "message": "chrome ux report data not found",
                }
            },
            True,
        ),
        (
            404,
            {"error": {"code": 404, "status": "NOT_FOUND", "message": "wrong endpoint secret-key"}},
            False,
        ),
        (404, {}, False),
        (403, {}, False),
        (500, {}, False),
    ],
)
def test_crux_documented_no_data_is_distinct_from_api_errors(
    tmp_path, monkeypatch, status, body, no_data
):
    ctx = context(tmp_path, config={"CRUX_API_KEY": "secret-key"})
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"lighthouseResult": {"categories": {"performance": {"score": 1}}}},
        ),
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            status,
            request=httpx.Request("POST", url + "?key=secret-key"),
            json=body,
        ),
    )
    result = performance.run(ctx)
    crux = next(c for c in result.connections if c.key == "crux")
    fields = result.evidence[-1].data["performance"]["field"]
    expected = "no_eligible_data" if no_data else "permission" if status == 403 else "error"
    assert [f["outcome"] for f in fields] == [expected, expected]
    assert all(f["detail"] in crux.detail and f["detail"] in result.reason for f in fields)
    assert crux.status == ("Permission required" if status == 403 else "Not assessed")
    assert "secret-key" not in json.dumps(result.model_dump())
    assert "secret-key" not in (ctx.run_dir / "raw/performance.json").read_text()


@pytest.mark.parametrize("status", [403, 404, 503])
def test_rejected_speculative_https_upgrade_crawls_explicit_http(tmp_path, monkeypatch, status):
    ctx = context(tmp_path, target="http://example.com/", tools_dir=tmp_path / "tools")
    binary = ctx.tools_dir / "siteone/siteone-crawler/siteone-crawler"
    binary.parent.mkdir(parents=True)
    binary.touch()
    client = httpx.Client
    calls, commands = [], []

    def request(req):
        calls.append(str(req.url))
        return httpx.Response(status if req.url.scheme == "https" else 200)

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(request), **kw)
    )

    def process(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps({"results": [{"url": ctx.target, "status": "200", "type": 1}]}),
        )

    monkeypatch.setattr(siteone.subprocess, "run", process)
    result = siteone.run(ctx)
    assert result.status == "ok" and ctx.site_url == ctx.target == "http://example.com/"
    assert calls == ["https://example.com/", "http://example.com/"]
    assert "--url=http://example.com/" in commands[0]
    assert (
        result.evidence[0].data["upgrade_limitation"] == f"Preferred HTTPS returned HTTP {status}"
    )
    # Explicit HTTPS is never downgraded by this fallback.
    calls.clear()
    resolution = targets.resolve_target("https://example.com/")
    assert calls == ["https://example.com/"] and resolution["last_safe_url"].startswith("https:")


def test_coverage_distinguishes_legacy_asset_budget():
    data = json.loads((REPO / "examples/fixture-report/results.json").read_text())
    from ams_assess.models import Evidence

    evidence = [
        Evidence.model_validate(e)
        for e in json.loads((REPO / "examples/fixture-report/evidence.json").read_text())
    ]
    for excluded in (True, False):
        data["crawl_assets_excluded"] = excluded
        for r in data.get("coverage", []):
            r["crawl_assets_excluded"] = excluded
        text = render_markdown(data, evidence)
        line = next(row for row in text.splitlines() if row.startswith("- Crawl coverage"))
        assert f"{len(data['pages'])} successful HTML pages" in line
        assert ("static assets excluded" if excluded else "including assets") in line
