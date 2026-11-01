import re
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ams_assess.browser_safety import LAUNCH_OPTIONS, isolate_context
from ams_assess.challenges import REASON, blocked_urls, challenged
from ams_assess.data_boundary import private_artifacts
from ams_assess.lifecycle import observe
from ams_assess.models import Context, ModuleResult, site_authority
from ams_assess.modules.siteone import diagnostic
from ams_assess.network import validate_public_url
from ams_assess.subproc import environment
from ams_assess.subproc import playwright as sync_playwright

name = "browser"
categories = {"accessibility", "geo", "local_seo", "entity", "cro_ux", "copywriting", "presence"}
requires = ["Chromium", "tools/axe.min.js", "SiteOne page list"]

EXTRACT = r"""() => {
 const text = e => (e?.innerText || '').trim();
 const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const select = (s, f, n=60) => [...document.querySelectorAll(s)].filter(visible).slice(0,n).map(f);
 const link = e => ({text: text(e).slice(0,180), href: e.href || ''});
 const body = document.body.innerText;
 return {
   title: document.title,
   meta: [...document.querySelectorAll('meta[name],meta[property]')].map(e =>
     ({name: e.name || e.getAttribute('property'), content: e.content})),
   headings: select('h1,h2,h3', e => ({level: e.tagName, text: text(e)})),
   visible_text_summary: body.slice(0,10000), visible_text_truncated: body.length > 10000,
   ctas: select('a,button,input[type=submit]', e => ({...link(e), text: text(e) || e.value || ''})),
   forms: select('form', f => ({action: f.action, method: f.method,
     fields: [...f.elements].map(e => ({name: e.name, type: e.type,
       label: [...(e.labels || [])].map(text).join(' '), required: e.required}))})),
   contacts: select('a[href^="tel:"],a[href^="mailto:"]', link),
   json_ld: [...document.querySelectorAll('script[type="application/ld+json"]')].map(e => {
     try {return JSON.parse(e.textContent)}
     catch {return {parse_error:true, text:e.textContent.slice(0,500)}}
   }),
   microdata: [...document.querySelectorAll('[itemscope],[itemprop]')].slice(0,60).map(e =>
     ({type:e.getAttribute('itemtype'), property:e.getAttribute('itemprop'),
       text:text(e).slice(0,400)})),
   nap_candidates: body.split('\n').filter(t =>
     /\d{3,}|street|road|avenue|pty|ltd/i.test(t)).slice(0,30),
   nav_links: select('nav a', link),
   trust_signals: body.split('\n').filter(t =>
     /review|testimonial|accredit|licen[cs]|insured|privacy|guarantee/i.test(t)).slice(0,30),
   canonical: document.querySelector('link[rel="canonical"]')?.href || '',
   // Platform/listing signals for the presence module; read from the same DOM, no navigation.
   script_srcs: [...document.scripts].map(e => e.src).filter(Boolean).slice(0,100),
   tag_snippets: [...document.querySelectorAll(
     'script:not([src]):not([type="application/ld+json"]),noscript')]
     .map(e => e.textContent.trim().slice(0,3000)).filter(Boolean).slice(0,40),
   resource_links: [...document.querySelectorAll('link[href]')].map(e => e.href).slice(0,60),
   // Scripts a consent manager holds back (type=text/plain or CMP blocking attributes).
   gated_scripts: [...document.scripts].filter(e => /^text\/plain$/i.test(e.type)
     || e.hasAttribute('data-cookieconsent') || e.hasAttribute('data-cookiecategory')
     || e.hasAttribute('data-consent') || /optanon-category|cmplz/i.test(e.className))
     .map(e => (e.src || e.textContent.trim()).slice(0,300)).slice(0,40),
   links: [...new Set([...document.querySelectorAll('a[href]')].map(e => e.href)
     .filter(h => /^https?:/.test(h)))].slice(0,300)
 };
}"""


KEY_PAGE = re.compile(r"contact|about|service|quote|book", re.I)


def select_pages(
    pages: list[str], target: str, limit: int = 6, seeds: list[str] | None = None
) -> list[str]:
    # Only reuse successfully crawled pages. No independent link discovery.
    ordered = list(
        dict.fromkeys(sorted(pages, key=lambda u: (u != target, not KEY_PAGE.search(u), len(u), u)))
    )
    # Up to a third of the sample is reserved for crawled sitemap/WordPress-inventory pages
    # that the key-page order would always skip (they may be linked from nowhere).
    known = set(seeds or [])
    reserved = [u for u in ordered if u in known and u != target and not KEY_PAGE.search(u)]
    reserved = reserved[: limit // 3]
    regular = [u for u in ordered if u not in reserved][: limit - len(reserved)]
    return [u for u in ordered if u in regular or u in reserved]


def axe_outcomes(audit, component, evidence_id):
    # Axe groups describe nodes, not whole-page verdicts. A violation takes precedence;
    # incomplete nodes prevent a pass even when other nodes passed the same rule.
    rules = {}
    for group, status in [
        ("violations", "fail"),
        ("incomplete", "unavailable"),
        ("passes", "pass"),
        ("inapplicable", "out-of-scope"),
    ]:
        for rule in audit.get(group, []):
            rules.setdefault(rule["id"], status)
    return [
        observe(name, "axe:" + rule, "accessibility", component, status, [evidence_id])
        for rule, status in rules.items()
    ]


@private_artifacts()
def run(ctx: Context) -> ModuleResult:
    result = ModuleResult(name=name)
    axe = ctx.tools_dir / "axe.min.js"
    if not axe.is_file() or not ctx.pages:
        return ModuleResult(
            name=name,
            status="skipped",
            reason="Requires axe-core and a successful SiteOne page list",
        )
    records, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(**LAUNCH_OPTIONS, env=environment("browser"))
        try:
            for viewport, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
                session = isolate_context(
                    browser,
                    viewport={"width": width, "height": height},
                    is_mobile=viewport == "mobile",
                    # Strict CSPs would otherwise block the injected axe-core script.
                    bypass_csp=True,
                )
                for index, url in enumerate(
                    select_pages(
                        ctx.pages, ctx.site_url, ctx.browser_pages, ctx.inventory.get("seeds")
                    )
                ):
                    page = session.new_page()
                    console, requested = [], []
                    page.on(
                        "request",
                        lambda request, requested=requested: (
                            requested.append(request.url[:300]) if len(requested) < 300 else None
                        ),
                    )
                    page.on(
                        "pageerror", lambda error, console=console: console.append(str(error)[:500])
                    )
                    page.on(
                        "console",
                        lambda msg, console=console: (
                            console.append(msg.text[:500]) if msg.type == "error" else None
                        ),
                    )
                    try:
                        validate_public_url(url)
                        try:
                            response = page.goto(url, wait_until="networkidle", timeout=25000)
                        except PlaywrightTimeoutError:
                            # Persistent polling can prevent network idle; check the loaded page.
                            response = page.goto(url, wait_until="load", timeout=25000)
                        final_url = (
                            getattr(response, "headers", {}).get("x-ams-resolved-url", page.url)
                            if response
                            else page.url
                        )
                        redirected = page.url != url or final_url != url
                        response_headers = getattr(response, "headers", {})
                        challenge = bool(response) and (
                            response_headers.get("x-ams-hosting-challenge") == "1"
                            or challenged({"status": response.status, "headers": response_headers})
                        )
                        if (
                            challenge
                            or response is None
                            or not 200 <= response.status < 300
                            or redirected
                        ):
                            # Error/redirect destinations are not an equivalent test of this URL.
                            detail = (
                                REASON
                                if challenge
                                else "Page failed or redirected; no equivalent test"
                            )
                            eid = result.add_evidence(
                                url,
                                "Browser page load unavailable",
                                {
                                    "status": response.status if response else None,
                                    "final_url": diagnostic(final_url, ctx),
                                    "redirected": redirected,
                                    "hosting_challenge": challenge,
                                    "reason": detail,
                                    "viewport": viewport,
                                },
                            )
                            errors.append(f"{viewport} {diagnostic(url, ctx)}: {detail}")
                            result.outcomes.append(
                                observe(
                                    name,
                                    "page-load",
                                    "technical",
                                    url + "::" + viewport,
                                    "unavailable",
                                    [eid],
                                    reason=detail,
                                )
                            )
                            continue
                        record = {
                            "url": page.url,
                            "viewport": viewport,
                            "page": page.evaluate(EXTRACT),
                            "console_errors": console,
                            "requests": list(dict.fromkeys(requested)),
                        }
                        shot = f"screenshots/{index + 1:02d}-{viewport}.png"
                        page.screenshot(path=str(ctx.run_dir / shot), full_page=True)
                        record["screenshot"] = shot
                        page.add_script_tag(path=str(axe))
                        audit = page.evaluate(
                            "async () => await axe.run(document, "
                            "{runOnly:{type:'tag',values:['wcag2a','wcag2aa',"
                            "'wcag21a','wcag21aa']}})"
                        )
                        record["axe"] = {
                            "version": audit["testEngine"]["version"],
                            "violations": audit["violations"],
                            "incomplete": audit["incomplete"],
                            "passes": audit.get("passes", []),
                            "inapplicable": audit.get("inapplicable", []),
                        }
                        records.append(record)
                        eid = result.add_evidence(
                            url, f"Rendered page evidence ({viewport})", record
                        )
                        component = url + "::" + viewport
                        result.outcomes.extend(axe_outcomes(audit, component, eid))
                        for violation in audit["violations"]:
                            severity = (
                                "high"
                                if violation["impact"] in {"serious", "critical"}
                                else "medium"
                            )
                            result.finding(
                                "axe:" + violation["id"],
                                violation["help"],
                                "accessibility",
                                [eid],
                                f"{viewport}: {len(violation['nodes'])} affected elements "
                                f"at {url} (axe {violation['id']}).",
                                f"Apply the guidance at {violation['helpUrl']} and "
                                "verify with keyboard and assistive technology.",
                                severity=severity,
                                urls=[url],
                                component=component,
                            )
                        canonical = record["page"].get("canonical", "")
                        actual = urlsplit(record["url"])
                        target = urlsplit(canonical)
                        mismatch = bool(
                            canonical
                            and (target.scheme, site_authority(canonical))
                            != (actual.scheme, site_authority(record["url"]))
                        )
                        for check, category, failed in [
                            ("canonical-mismatch", "seo", mismatch),
                            ("console-errors", "technical", bool(console)),
                        ]:
                            result.outcomes.append(
                                observe(
                                    name,
                                    check,
                                    category,
                                    component,
                                    "fail" if failed else "pass",
                                    [eid],
                                )
                            )
                        if mismatch:
                            result.finding(
                                "canonical-mismatch",
                                "Canonical URL points to a different host or protocol",
                                "seo",
                                [eid],
                                f"{url} declares canonical {canonical}.",
                                "Point canonical links at the preferred public HTTPS host.",
                                confidence="needs_verification",
                                urls=[url],
                                component=component,
                            )
                        if console:
                            result.finding(
                                "console-errors",
                                "Browser console errors",
                                "technical",
                                [eid],
                                f"{len(console)} errors on {viewport} at {url}.",
                                "Investigate browser errors and retest key journeys.",
                                confidence="needs_verification",
                                urls=[url],
                                component=component,
                            )
                    except Exception as exc:
                        detail = type(exc).__name__
                        errors.append(f"{viewport} {diagnostic(url, ctx)}: {detail}")
                        eid = result.add_evidence(
                            url,
                            "Browser page load unavailable",
                            {"viewport": viewport, "error": detail},
                        )
                        result.outcomes.append(
                            observe(
                                name,
                                "page-load",
                                "technical",
                                url + "::" + viewport,
                                "failed-to-run",
                                [eid],
                                reason=detail,
                            )
                        )
                    finally:
                        page.close()
                session.close()
        finally:
            browser.close()
    result.raw_paths = [ctx.raw(name, {"pages": records, "errors": errors})]
    if errors:
        result.reason = (
            "Some page checks NOT ASSESSED: "
            if blocked_urls(result.evidence)
            else "Some page checks failed: "
        ) + "; ".join(errors)
    if blocked := blocked_urls(result.evidence):
        result.reason = f"{len(blocked)} pages {REASON}; " + result.reason
    if not records:
        result.status = "error"
        result.reason = result.reason or "No rendered pages collected"
    return result
