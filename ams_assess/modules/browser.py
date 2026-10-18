import re

from playwright.sync_api import sync_playwright

from ams_assess.models import Context, ModuleResult

name = "browser"
categories = {"accessibility", "geo", "local_seo", "entity", "cro_ux", "copywriting"}
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
     /review|testimonial|accredit|licen[cs]|insured|privacy|guarantee/i.test(t)).slice(0,30)
 };
}"""


def select_pages(pages: list[str], target: str) -> list[str]:
    # Only reuse successfully crawled pages. No independent link discovery.
    ordered = sorted(
        pages,
        key=lambda u: (
            u != target,
            not bool(re.search(r"contact|about|service|quote|book", u, re.I)),
            len(u),
            u,
        ),
    )
    return ordered[:6]


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
        browser = p.chromium.launch()
        try:
            for viewport, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
                session = browser.new_context(
                    viewport={"width": width, "height": height},
                    is_mobile=viewport == "mobile",
                    service_workers="block",
                )
                # Observational only: no form submissions or background writes from page scripts.
                session.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if route.request.method in {"GET", "HEAD"}
                        else route.abort()
                    ),
                )
                for index, url in enumerate(select_pages(ctx.pages, ctx.target)):
                    page = session.new_page()
                    console = []
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
                        page.goto(url, wait_until="networkidle", timeout=25000)
                        record = {
                            "url": page.url,
                            "viewport": viewport,
                            "page": page.evaluate(EXTRACT),
                            "console_errors": console,
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
                        }
                        records.append(record)
                        eid = result.add_evidence(
                            url, f"Rendered page evidence ({viewport})", record
                        )
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
                            )
                    except Exception as exc:
                        errors.append(f"{viewport} {url}: {type(exc).__name__}")
                    finally:
                        page.close()
                session.close()
        finally:
            browser.close()
    result.raw_paths = [ctx.raw(name, {"pages": records, "errors": errors})]
    if errors:
        result.reason = "Some page checks failed: " + "; ".join(errors)
    if not records:
        result.status = "error"
        result.reason = result.reason or "No rendered pages collected"
    return result
