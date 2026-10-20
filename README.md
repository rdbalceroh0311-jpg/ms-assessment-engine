# AMS Assessment Engine

Website assessment CLI for AI Managed Services. One SiteOne crawl supplies the page inventory;
bounded browser checks and optional public/connected services collect evidence. Deterministic findings
are consolidated, optionally interpreted by AI, and rendered into one branded Markdown, HTML and PDF
report. Assessments do not submit forms or modify websites.

## Setup and use

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), curl and tar. Setup supports macOS arm64/x64
and Linux arm64/x64. Tool downloads are pinned to SiteOne 2.5.1 and axe-core 4.10.3.

```sh
./scripts/setup.sh                       # dependencies, Chromium, SiteOne, axe; safe to repeat
uv run ams-assess modules                # requirements and future capabilities
uv run ams-assess gsc-auth --client-secrets /path/to/desktop-client.json
uv run ams-assess run --customer acme --customer-name 'Acme Ltd' --target https://example.com
uv run ams-assess run --customer acme --target example.com \
  --categories domain_email,security,performance --no-ai
uv run ams-assess report data/customers/acme/<timestamp>
```

Options: `--max-pages 100` (a conservative **URL** cap including assets), `--formats md,html,pdf`,
`--no-ai`, `--connect gsc`, `--dataforseo-max-usd X`, `--dkim-selectors a,b`, `--mail-tls-check`. Default categories: technical, seo, geo,
performance, accessibility, domain_email, security, local_seo, entity, cro_ux, copywriting.
Only required modules run; a domain/security/performance-only run has no crawl. Customer IDs must be
lowercase letters/digits/hyphens, 1-63 characters. Exit 1 means incomplete results or report failure;
a useful partial assessment exits 0 and lists unavailable data.

### Local demonstration

```sh
uv run python -m http.server 18765 --bind 127.0.0.1 --directory fixtures/site
# In another terminal; empty ANTHROPIC_API_KEY selects the authenticated Claude CLI:
ANTHROPIC_API_KEY= uv run ams-assess run --customer fixture-ai \
  --customer-name 'Harbour Demo Plumbing' --target http://127.0.0.1:18765 --max-pages 30
```

The five-page synthetic fixture includes a broken link, missing H1/meta description/alt text, low
contrast, a weak CTA and a structured-data telephone mismatch. It contains no real customer data.
An AI-generated sample from an authenticated fixture run is committed under
[`examples/fixture-report/`](examples/fixture-report/) (report, results, findings and evidence).

## Integrations and credentials

Environment overrides repository `.env`, which overrides `~/.config/ams-assess/credentials.env`.
Use `.env.example` for names. Credentials and runtime outputs are ignored by Git; do not stage them.
Raw page text, screenshots and AI prompts can contain customer information. Keep `data/` private.
AI-enabled runs send collected evidence to the provider; use `--no-ai` when inappropriate.

| Module/provider | Requirements and behaviour |
| --- | --- |
| SiteOne | Local binary; one crawl, 2 workers, 5 requests/second, robots honoured, no shared crawl cache or uploads. JSON includes broken links, redirects, SEO and headers. |
| Browser + axe | Chromium + `tools/axe.min.js`; up to 6 successful crawled pages, desktop/mobile. Screenshots, console errors, headings/text, forms, CTAs, contacts, JSON-LD/microdata, trust signals. No form submissions. |
| Domain/email | Public DNS A/AAAA/MX/NS/SOA/TXT/CAA, checkdmarc SPF/DMARC/DNSSEC, RDAP registrar/expiry (expired = ERROR, <30 days = WARNING), DKIM (`--dkim-selectors`/`AMS_DKIM_SELECTORS`, otherwise 8 common selectors; unknown selector = NOT ASSESSED), A/MX/NS/SPF agreement across 1.1.1.1, 8.8.8.8 and 9.9.9.9, and DNSBL lookups of up to 4 MX and 2 website IPv4 addresses (at most 30 queries) on SpamCop, Barracuda (free resolver registration required, otherwise refused = NOT ASSESSED) and PSBL. Spamhaus ZEN only with `SPAMHAUS_DQS_KEY`. SMTP only with `--mail-tls-check`: at most 2 MX hosts, banner/EHLO/STARTTLS/certificate/QUIT, 10-second timeout, never MAIL/RCPT/VRFY/EXPN/DATA. Produces the Domain, DNS & Email Health summary (PASS / WARNING / ERROR / NOT ASSESSED), which reuses Security/Observatory evidence for TLS and headers. Does not measure inbox placement. |
| Security | HTTP headers and verified TLS certificate issuer/expiry/protocol/hostname. Not a penetration test. |
| MDN Observatory | Public v2 scan; aggregate grade only. Failure and localhost are explicit skips. |
| PageSpeed/CrUX | Mobile/desktop PageSpeed v5 with available URL/origin CrUX fields. Optional `PAGESPEED_API_KEY`. Separate CrUX only with `CRUX_API_KEY` or `GOOGLE_API_KEY`. Localhost and exhausted keyless quota are skipped. |
| DataForSEO | Disabled unless `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` and explicit `--dataforseo-max-usd` are present. One Australia/English domain rank overview request by default (`DATAFORSEO_LOCATION_CODE`, overridden by `--dataforseo-location-code`), one result, no retries/pagination. Estimated $0.0101 is recorded before the call; a lower cap is refused. Provider price changes are outside this client cap; verify [pricing](https://dataforseo.com/pricing/dataforseo-labs/dataforseo-labs-api) before enabling. No paid endpoint is used in tests. |
| Google Search Console | Explicit `--connect gsc`, service-account `GOOGLE_APPLICATION_CREDENTIALS` or OAuth `AMS_GSC_TOKEN_FILE`, and an accessible matching property. Optional `AMS_GSC_PROPERTY`. Read-only: top 100 queries/pages over 28 complete days with 3-day lag, plus sitemaps. OAuth requires an authorized-user JSON file created by `gsc-auth`. |
| AI | `ANTHROPIC_API_KEY` selects Anthropic SDK (billable; no retries); otherwise authenticated `claude -p --output-format json` with strict schema, tools/hooks/MCP disabled and 300-second timeout; otherwise deterministic fallback. `--no-ai` disables both. Prompt/raw responses stay in the run directory. |

## Outputs and interpretation

```text
data/customers/<customer>/<UTC timestamp>/
  raw/<module>.json             # plus crawler stderr and AI prompt/response when enabled
  screenshots/                 # bounded desktop/mobile sample
  evidence.json                # stable ev-<module>-NNN IDs within the run
  findings.json
  results.json                 # scope, statuses, limitations, AI and findings
  report.md                    # master Jinja2 template
  report.html                  # python-markdown + print CSS
  report.pdf                   # same HTML printed by Chromium
```

Findings with a shared category/check key merge affected URLs and evidence IDs. AI cannot overwrite
deterministic observations: it enriches likely cause, potential impact, priority and suggested service.
New AI conclusions are labelled `needs_verification` or `subjective`; direct observations use
`confirmed`. AI findings without citations, with unknown citations or unselected categories are dropped
and logged. Reporting refuses missing evidence references. The roadmap is proposed work, not approved
scope or a delivery commitment. Missing data never constitutes a pass.

Limits: sampled pages/viewports, no authenticated journeys, no submission/payment testing, no measured
conversion lift, full manual accessibility assessment or assurance of security. GEO means content and
crawlability readiness; **AI search visibility is not measured**. No A/B or behavioural data. Local
fixture headers say nothing about production hosting. Public quotas/outages may leave performance or
Observatory unavailable. GSC, DataForSEO and the SDK path require credentials and are not proven live
by the fixture. `modules` and report limitations list future capabilities (Elmo, Houtini, OpenSEO, GBP,
GA4, WP-CLI, Workspace/M365, social SEO, PR, A/B and heatmaps); none is implemented or measured.

## Development

Add `ams_assess/modules/<name>.py` with `name`, `categories`, `requires`, `run(ctx) -> ModuleResult`,
then add it to the plain `REGISTRY` in `modules/__init__.py`. Use `result.add_evidence()` and cite its
returned IDs in findings. Persist raw artefacts through `ctx.raw()`. Never crawl independently of
SiteOne, read other customer directories, log credentials, or introduce default paid calls.

```sh
uv run pytest -q
uv run ruff check .
uv run ty check ams_assess tests
```

Tests cover consolidation, AI evidence rejection, customer separation, paid-call guards, GSC property
matching, quota failure, module isolation and a real no-AI fixture run with PDF/citation checks.
The fixture test skips only when SiteOne, Chromium or axe is absent; run setup to exercise it.
