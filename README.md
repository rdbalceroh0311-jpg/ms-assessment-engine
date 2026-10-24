# AMS Assessment Engine

Business assessment CLI for AI Managed Services. One SiteOne crawl supplies the page inventory;
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

Options: `--browser-pages 6` (1-50), `--wp-inventory /path/to/authorised-export.json` (JSON or CSV URLs), `--max-pages 100` (a page-crawl URL budget excluding static assets; redirects and failed/non-HTML responses can consume slots), `--formats md,html,pdf`,
`--no-ai`, `--connect gsc`, `--dataforseo-max-usd X`, `--keywords 'k1,k2'`, `--dkim-selectors a,b`, `--mail-tls-check`. Default categories: technical, seo, geo,
performance, accessibility, cro_ux, copywriting, security, domain_email, local_seo, entity, presence.
The report follows ten sections: context; scope and limitations; critical risks; website checks;
digital presence; domain/email/security; connected systems; operational discovery; opportunities and
roadmap; customer verification/access/decisions. Sections without assessed content are omitted;
unavailable checks stay in scope/limitations. An evidence index follows. Every generated report starts
with the draft human-review banner. Findings use Confirmed / Reported by customer / Inferred / Not
assessed basis labels and exactly one of the four AMS service categories. The planning approach is
to improve processes and integrate existing systems first; there are no ranking, traffic, lead or
revenue guarantees, and AI-search readiness does not measure AI visibility.
Repeated accessibility rules on the same website share one customer-report block and recommendation
(www/apex aliases share a website), with each affected page/viewport, finding ID and evidence reference
retained. The lifecycle table shows non-current
findings; JSON retains every individual finding. Repeated coverage and evidence are grouped for display.
Only required modules run; a domain/security/performance-only run has no crawl. Customer IDs must be
lowercase letters/digits/hyphens, 1-63 characters. Exit 1 means incomplete results or report failure;
a useful partial assessment exits 0 and lists unavailable data.

### Run the next customer assessment

Replace the synthetic name, target and file paths with the authorised customer's details. Start with
public evidence and customer notes; optional paid searches need an explicit USD cap. Do not request
passwords. The four service categories are AI & Automation, Business Systems, Digital Growth and
Managed IT & Technology.

```sh
uv run ams-assess customer --customer acme --name 'Acme Services' --industry trades \
  --buying-model service-area --service-area Sydney --website https://example.com
uv run ams-assess run --customer acme --target https://example.com --no-ai
uv run ams-assess discover --customer acme --notes /path/to/customer-notes.json
uv run ams-assess presence-verify --customer acme --file /path/to/verified-profiles.json
uv run ams-assess report --customer acme
# Review/edit data/customers/acme/report.md; remove the draft banner only after AMS approval.
uv run ams-assess report --customer acme --from-md data/customers/acme/report.md --based-on 1
```

Use the actual generated revision number for `--based-on`. Markdown is the editable master; HTML/PDF
are regenerated from it and earlier revisions remain readable. `presence-verify` is for observations
actually checked by a person, not assumed ownership. See the notes and verification formats below.
Connected readiness distinguishes Connected successfully, Permission required, Not connected,
Unsupported (no connector) and Not assessed. GSC unverified users and HTTP 403 require permission.
The [documented GSC API](https://developers.google.com/webmaster-tools/v1/searchanalytics/query)
does not expose a separate generative-AI performance report/filter (checked 2026-10-23); review a
customer-provided [Search Console UI export](https://support.google.com/webmasters/answer/16984139)
manually. No new business-system connectors are implemented.

### Progressive assessment: one customer, one evolving assessment

Each customer has a store at `data/customers/<customer>/assessment.json` (`schema_version` 3, mode
0600 in a 0700 directory, gitignored, written atomically). Stages can run in any order and any number
of times; none is a prerequisite for another:

1. **Public technical** (`run`): as above. The per-run timestamp directory is unchanged, and the run
   is also merged into the store.
2. **Connected** (`run --connect gsc`): authorised evidence enriches the same assessment.
3. **Business discovery** (`discover`): structured customer notes.

```sh
uv run ams-assess run --customer acme --target https://example.com        # stage 1
uv run ams-assess discover --customer acme --notes notes.json              # stage 3, exits 0
uv run ams-assess report --customer acme                                   # report at any stage
```

`report --customer` writes a new immutable revision to `data/customers/<customer>/reports/<NNN-timestamp>/`
(files read-only, always including the markdown master), records it in the store's append-only
`report_revisions` (with the assessment run it reflects) and copies it to `report.md/html/pdf` in the
customer directory as the latest. To publish a human edit, edit the markdown and run
`report --customer acme --from-md <file>`: it becomes a new revision and html/pdf are rendered
from it. Its ancestry (`based_on`, `assessment_revision`) comes from the `reports/<revision>/`
directory the file is in, else from `--based-on <revision>`, else it is recorded as `unknown`. `report <run-dir>` still renders one run. Imports and reports take an
exclusive per-customer lock (`.assessment.lock`, waits up to 30 s, then fails with a clear error).
A store with several websites lists every target and labels each module row and email-health summary
with its website and run. Discovery notes:

```json
{
  "sections": {
    "sales_quoting": [
      "Quotes are built by hand",
      {"statement": "Jobs are re-keyed into Xero", "frequency": "daily",
       "systems": ["Xero"], "owner": "Office manager", "impact": "~5 h/week"}
    ]
  },
  "findings": [
    {"title": "Quote automation", "kind": "opportunity", "severity": "medium",
     "detail": "Quotes are built manually", "recommendation": "Automate quote generation",
     "ams_service": "AI & Automation", "cites": ["sales_quoting:0", "sales_quoting:1"]}
  ]
}
```

Sections: `business_overview`, `enquiries_communication`, `sales_quoting`, `bookings_scheduling`,
`systems_in_use`, `manual_duplicated_tasks`, `existing_automations`, `pain_points_bottlenecks`,
`data_integrations`, `opportunities_constraints`; statements are strings or objects with optional
`frequency`, `systems`, `owner`, `impact`, `staff_time`, `errors_rework`, `cost` (as stated),
`workaround`, `ref` (all customer statements until independently verified; shown only when given).
Interview guide: [`examples/discovery/discovery-template.md`](examples/discovery/discovery-template.md)
(questions mapped to section keys); a synthetic notes file is in
[`examples/discovery/notes.example.json`](examples/discovery/notes.example.json). A finding must cite statements
(`section` or `section:N`, 0-based) and its `ams_service` is one of AI & Automation, Business
Systems, Digital Growth, Managed IT & Technology. Invalid notes exit 2 and write nothing. Discovery is
not a crawler module and is never sent to AI.

**Re-ingesting discovery notes.** A statement is identified by its text plus `owner` and `systems`
(context); `frequency`, `impact`, `staff_time`, `errors_rework`, `cost` and `workaround` are detail. Order in the notes never matters and submitting a
subset is safe. An identical statement is reused (no new evidence). A changed version supersedes
the stored one only when unambiguous (one new version, one stored candidate): it gets a new
evidence ID that records `supersedes`, while the old evidence stays intact for findings that cite it.
If several same-identity versions exist the change fails closed: it is added as a distinct entry and
nothing is rewritten. To update a specific statement explicitly, give it a stable `"ref"` (unique
per section). A ref always takes precedence over content: a ref that matches a stored statement
updates exactly that statement (all fields as submitted); a ref that matches none creates a new
entry bound to that ref, even if its content equals another entry. Matching and citations are
computed from the pre-ingestion state, so the order of the notes never changes them.
Limitation: changing or omitting `owner`/`systems` makes a new contextual entry; the old entry stays
and history is not rewritten. Narrative guard: AI text citing customer statements or assumptions
is dropped unless every verification term (verify/confirm/validate/measure/prove/corroborate/
substantiate/authenticate/certify/attest forms) sits in a clause that fully matches an allowlisted
wording: "X is pending/awaiting verification", "X needs verification", "X is (yet) to be verified",
"X has not been / is not / isn't (independently) verified", "X is unverified", "we need to /
should / must verify X", "there is no independent confirmation of X". Text is normalised first
(Unicode apostrophes, case, abbreviation dots). Negations are counted over the WHOLE narrative and
must be exactly the ones those wordings carry, so any other negation or refutation anywhere (even
an unrelated "we don't have the figures, pending verification") makes the narrative be dropped:
this is deliberately conservative. Synonyms are covered: audited, (cross-/double-/re-)checked, reconciled, inspected and tested count as verification terms, refutation words (debunked, nonsense, exaggerated, misleading, inaccurate, baseless…) count as negations, and zero-width/format characters are stripped before matching. Co-occurrence rule (whole narrative, so punctuation cannot split it): an event noun (audit, inspection, review, check, verification, test…) with a conclusion verb (found, shows, revealed, established, confirmed…) and a positive-accuracy word (accurate, correct, true, valid, reliable, genuine…) anywhere in the narrative ("the audit found the loss accurate") is rejected; "the audit found discrepancies" is not. Hedged wording ("legitimate", "plausible") is accepted. Documented limits: any phrasing outside these word lists can pass; the lists are not exhaustive. Bare nouns such as "authentication", "certification" and "the audit found…" are not verification terms, and the guard is a lexical check, not a semantic proof; it narrows what AI prose can assert about customer statements but human review of the report remains required.

**Evidence IDs are namespaced per run in the store** (`ev-gsc-001` becomes `ev-gsc-r2-001`; discovery
statements are `ev-discovery-rN-NNN`); every citation (findings, AI narrative, summaries) is rewritten
with the same mapping. Technical findings use stable customer, site-host, check and page/component identities. A later
explicit pass resolves a failure only when the same check actually covers its affected component(s),
with cited technical evidence. Skipped, failed-to-run, unavailable, out-of-scope and absent results
never resolve a finding. Partial samples leave the other findings **previously observed — not
rechecked**, with their last-observed dates. Resolution records its evidence, date and run; recurrence
reopens the identity and preserves its events and observations. Current severity follows the latest
observation. Resolved and superseded findings are excluded from recommendations and the roadmap;
conflicts remain **needs review**. Discovery supersession and conflict rules below are unchanged.
Axe node groups produce one verdict per rule/page/viewport: any violation fails the check;
incomplete nodes prevent a pass. Historical PASS/FAIL pairs citing the same axe audit are interpreted
as FAIL, with false review holds corrected in the current view and saved on the next store write.
Raw runs and evidence remain unchanged; contradictions between different audits still need review.
Current citations refer to the current observation; older citations remain in finding events.
Historical AI prose is removed only when its cited basis belongs solely to resolved or superseded
findings, preserving sentences with a current or mixed basis.

Page identities normalise the www/apex alias while preserving other subdomains and page/test scope;
evidence keeps the URL actually observed. Legacy `lighthouse-mobile` / `lighthouse-desktop` keys
are predecessors only of the equivalent homepage per-page checks, retaining identity and history
through resolution and recurrence. Legacy per-header findings become predecessors of the matching
`security-headers` aggregate, with their original records retained in history.

Schema 1/2 stores migrate under the existing customer lock. Before conversion, the exact original
`assessment.json` bytes are fsynced to `assessment.json.backup-<UTC timestamp>` beside the store,
mode 0600. Migration is then saved atomically. Legacy findings have no inferred pass: they start as
not rechecked, and ambiguous legacy components require review or an equivalent explicit retest.
Evidence, runs and immutable report revisions are retained. Migration deduplicates normalized
identities; loading or saving a store with duplicate active finding IDs is rejected.

**Finding versions.** A finding whose cited statements have all been superseded is retired to
`finding_history` (kept, never recommended); re-importing a discovery finding with a changed claim
creates a new version that supersedes the old one instead of merging text. A finding citing a
superseded statement whose new version it does not cite, or two current versions of the same
statement (together with every finding citing either version), are listed under "needs review"
in the report and left out of the recommendations and roadmap.
Unrecognised schema versions and mismatched customer IDs are rejected without modification.

**Provenance.** Evidence is `technical_evidence` (default), `customer_statement` or `assumption`. A
finding takes the worst provenance of the evidence it cites and can never be `confirmed` unless all
of it is technical; the report labels customer statements and AI cannot upgrade provenance. A
discovery section appears in the report only when discovery notes exist.

### Local demonstration

```sh
uv run python -m http.server 18765 --bind 127.0.0.1 --directory fixtures/site
# In another terminal (no AI or paid API calls):
uv run ams-assess customer --customer fixture-v1 --name 'Harbour Demo Plumbing' \
  --industry trades --buying-model service-area --service-area Sydney
uv run ams-assess run --customer fixture-v1 --target http://127.0.0.1:18765 --max-pages 30 --no-ai
uv run ams-assess discover --customer fixture-v1 --notes examples/discovery/notes.example.json
uv run ams-assess report --customer fixture-v1
```

The synthetic fixture includes a broken link, missing H1/meta description/alt text, low
contrast, a weak CTA and a structured-data telephone mismatch. Its home page also carries fictional,
consent-gated, non-loading (`type="text/plain"`) GA4, GTM and Clarity tags (reported Unknown), a
`google-site-verification` meta tag, a robots.txt `Sitemap:` line with an eight-URL sitemap including an unlinked fixture page,
JSON-LD `sameAs` and footer links to fictional Google Business Profile, Facebook and Yellow Pages
listings. It contains no real customer data and makes no third-party requests.
A synthetic public + discovery + customer-profile sample, generated with AI disabled, is committed
under [`examples/fixture-report/`](examples/fixture-report/) (Markdown, HTML, PDF, results, findings
and evidence). No live customer or paid/connected API data is included.

### Website coverage and inventories

Every run records discovery sources, successful SiteOne HTML crawls and the bounded Playwright/axe
sample separately. Reports say **X of Y known URLs crawled, Z browser-tested**; known URLs are never
claimed to be the whole site. Untested browser URLs, unreached sitemap URLs, exclusions, blocked,
failed and redirected responses, collection limits and stop reasons are retained per run and in the
customer report. Axe outcomes retain desktop/mobile scope; unavailable or inapplicable rules do not
resolve earlier violations. Site-check aggregates also retain the observation methods and sample.

Coverage reuses the existing robots/sitemap result and follows same-origin sitemap declarations and
index children without recrawling their page URLs: at most 10 sitemap documents, a known-URL budget
of 5000 (or the configured crawl budget when higher),
a further shared 10-second inventory deadline and 2 MB per response. WordPress detection triggers
public read-only REST GETs: `/wp-json/wp/v2/types`, then advertised published pages, posts and public
custom types (`context=view`, `status=publish`, `per_page=100`). Inventory requests identify as
`AMSAssessment/1.0`, use no authentication, reject cross-origin redirects, wait 200 ms between
collection requests, and stop after 10 collection pages or 10 seconds. A disabled or blocked REST
API is reported as **inventory unavailable — authorised export or read-only access needed**.

`--wp-inventory <file>` is an explicit authorised export input, never fetched or uploaded. JSON may
be a list of URLs, a list of `{"url": "..."}` objects, or `{"urls": [...]}`; CSV has a `url` (or `link`)
column. Maximum 2 MB / 5000 rows; URLs outside the assessed origin and credential-bearing URLs are
excluded. Export URLs have their own discovery source and do not count as crawled or browser-tested.
No inventory collection claims complete coverage.
If coverage accounting fails, the run retains its completed checks and report, records coverage
as unavailable, and marks the run partial.

Supported business figures are matched to discovery statements or structured measurements by value,
unit, period and qualifier. Customer statements render from the original source and retain context,
for example: **Customer reports approximately 20 enquiries per week; not independently verified.**
Citations remain attached; matching a bare number or score does not authorise an enquiry count.
Invented quantities, upgrades to measured claims, lost-revenue/ROI claims and promised gains remain
blocked, including future/modal and causal framing in finding titles. Only figure-bearing sentences
are replaced; other validated sentences remain intact. This processing does not send discovery
notes to an AI provider.

### Business identity and platform presence discovery

```sh
uv run ams-assess customer --customer acme --name 'Acme Painting Pty Ltd' --industry trades \
  --buying-model service-area --service-area Parramatta --phone '02 5550 0101' \
  --location '12 Example St, Parramatta NSW 2150' --website example.com --abn '51 824 753 556'
uv run ams-assess customer --customer acme --file profile.json        # same fields as JSON
uv run ams-assess run --customer acme --target example.com --dataforseo-max-usd 0.10 \
  --keywords 'painter parramatta,house painter'                        # optional, paid
uv run ams-assess presence-verify --customer acme --file verified.json
```

`customer` stores the business profile (name, aliases, websites, ABN, industry, buying model, service
area, locations, phones) in the customer store as customer-statement evidence; repeating it with no
change writes nothing. The ABN is checked for format and check digits only, never looked up.
Industries: `restaurant`, `beauty`, `trades`, `b2b_contractor`, `general` (aliases such as `painting`
or `traffic-management` are accepted). Buying models: `storefront`, `service-area`, `online-only`,
`b2b-contract`. Later runs use the profile for presence discovery.

The platform catalogue (`ams_assess/platforms.py`) covers Google Business Profile/Maps, Bing Places,
Apple Business/Apple Maps, social platforms, AU directories, chambers/associations and industry
platforms (restaurants, hair/beauty, trades, traffic management/B2B suppliers). Relevance comes from
industry and buying model; not every platform is required. Each platform gets one state: Found —
likely match, Found — needs verification, Not found in searches, Inconsistent, Not applicable or Not
assessed. Profiles linked from the website are always reused. Independent search runs only with
`--dataforseo-max-usd`: DataForSEO Google Maps and Google organic (`site:`/name) live SERP calls, at
most 12 per run, AU location by default (`--dataforseo-location-code`), ordered so the most relevant
platforms are searched first. Each call's estimate is checked against the cap remaining after earlier
calls before it is sent, and stays booked when the outcome is unknown (timeout):

| Call | Published live price (per SERP, depth 10) | Estimate booked |
| --- | --- | --- |
| Google organic, no operator (name, keywords) | $0.002 | $0.003 |
| Google organic, one operator (`site:` profile search, quoted keyword) | $0.010 (x5 per operator) | $0.015 |
| Google Maps, no operator | $0.002 | $0.003 |
| Any query with two or more operators, or still percent-encoded after 3 decodes | - | refused |

Operators are counted on the URL-decoded query, and every `:` counts as one, whatever the operator
name. `--keywords` must be plain search terms: a `word:` token, `%` or quotes exits 2. Generated
discovery queries strip `:`, `%` and quotes from the business name and suburb, and carry only the
template's single `site:` operator. Map links from the website (Google, Bing, Apple) count as a
likely match only when they carry a business or place identifier (`cid=`, `/maps/place/`, `ypid`,
`auid=`, `place-id=`); a plain map search link needs verification. Any call charged above its
estimate (the domain overview included) stops every later paid call.

Prices from DataForSEO's published SERP pricing (Google Organic and Google Maps pages, checked
2026-10-23); `SERP_PRICING` in `ams_assess/modules/dataforseo.py` is the single source. A reported
charge above the estimate stops further paid calls. Platform pages are never fetched, and only
business-profile routes count (search, category, help and login pages never do). With no cap, the state is Not assessed and the report lists manual verification tasks with
ready-to-use search queries and URLs. NAP is compared only from data actually obtained. Possible
duplicates, inconsistent NAP and profiles not linked from the website become needs-verification
findings. Keyword positions are point-in-time samples.

`presence-verify` takes `{"verifications": [{"platform": "hipages", "state": "found_likely",
"profile_url": "...", "observed_nap": {"name": "", "address": "", "phone": ""}, "verified_by":
"...", "date": "2026-10-20", "note": ""}]}` (one entry per platform; state keys or labels). Each
record becomes `manual_verification` evidence. In the customer report it replaces the automated state
for that platform, which stays shown beside it. A changed record supersedes the earlier one without
deleting its evidence, and importing the same file again changes nothing.

## Customer systems register and local interface

The systems register lives in the existing `assessment.json`; no separate database is created.
Every assessment refreshes public presence, platform, infrastructure and successful GSC test
evidence. Older assessments are backfilled when the register or customer report is opened.
Existing `presence-verify` records and recognised, explicitly named `systems_in_use` discovery
systems are reused. A legacy verification with several websites stays
customer-wide because it has no website attribution. Industry/buying-model relevance uses the
existing platform catalogue. `register set` selects an exact existing `--site`/`--location` scope
first, then reuses the sole matching entry for omitted/empty selectors. A nonempty `--site` or
`--location` that matches nothing creates that scope; it is never silently discarded. When the
site is omitted, a new location uses the platform's sole existing website (or the customer's sole
website for a new platform). An explicit `--site ''` with a new location creates a customer-wide
location. Ambiguous matches require explicit site/location selectors.
Platform keys are listed by `register set --help`.

```sh
uv run ams-assess register show --customer acme
uv run ams-assess register set --customer acme --platform google_business_profile \
  --site https://example.com --location Sydney --presence publicly_observed \
  --ownership customer_confirmed --access requested \
  --evidence-url 'https://maps.google.com/?cid=123456789' --identifier 'CID:123456789' \
  --method manual --tested 'Reviewed public profile and customer control confirmation' \
  --who 'AMS reviewer' --note 'Customer retains recovery methods' \
  --next-action 'Customer to grant delegated access' --responsible customer
uv run ams-assess report --customer acme
```

Presence, ownership, AMS access, engine integration, operational verification and next action are
separate fields. Human-entered status/action fields take precedence over subsequent automated
observations. Conflicts are marked **Needs review**; re-confirm a field to acknowledge its conflicts.
Evidence and full old/new history retain who, when and source. `--tested` records what was actually
checked, with optional ISO `--date`; an access update alone does not claim an operational test.
Manual operational checks remain current until manually changed; later automated checks remain
in the evidence/history. `--site ''` selects an existing customer-wide scope when present.
To clean up an accidental duplicate after verifying the retained row has the intended human data,
use `register remove --customer acme --platform localsearch --site '' --location ''`.
Removal requires an exact scope (no auto-resolution), records the complete removed row in history,
and retains all prior history and evidence. Only purely manual rows can be removed; engine evidence
and nondefault integration states are protected. It does not merge fields into another row. Regenerate
reports afterwards; existing report revisions remain immutable.
Only an engine-recorded successful GSC connection with property evidence sets `connected`.
Access granted never does. Missing tags are unknown, unsupported does not mean absent, and
Workspace and Microsoft 365 can coexist. WP-CLI is server tooling, not an account.
Use public property/account identifiers and evidence references only. Secret-looking inputs,
credential-bearing URLs and long opaque identifiers are rejected; never paste passwords or tokens.
Customers retain ownership and recovery methods; request named, revocable, least-privilege access.
The generated report includes the register, separate public/authorised evidence, and customer actions.
Saved report versions remain snapshots; regenerate a report after manual updates.

Start the local interface from the repository worktree:

```sh
uv run ams-assess ui --port 8765
```

Open the `http://127.0.0.1:8765/?token=...` URL printed once at startup. The interface binds only
to `127.0.0.1`; all requests require that per-start token. POST forms additionally require a separate
CSRF token and matching Origin/Host. Keep the startup URL private. **Stop: press `Ctrl-C` in that
terminal.** Restart with the same start command; a fresh token is generated and saved edits remain.
No credentials/config files are loaded by this command. The UI lists customers, runs, coverage,
register evidence, Current / Resolved / Not rechecked / Needs review findings and saved reports.
Manual verification and access updates use the same validated, locked store functions as the CLI.
It cannot execute commands or run assessments. File serving is limited to recorded customer output
directories and known report/results/evidence/findings files or PNG screenshots; traversal, symlinks,
raw files and credential files are excluded. Saved HTML is sandboxed with scripts disabled.

## Integrations and credentials

Environment overrides repository `.env`, which overrides `~/.config/ams-assess/credentials.env`.
Use `.env.example` for names. Credentials and runtime outputs are ignored by Git; do not stage them.
Raw page text, screenshots and AI prompts can contain customer information. Keep `data/` private.
AI-enabled runs send collected evidence to the provider; use `--no-ai` when inappropriate.

| Module/provider | Requirements and behaviour |
| --- | --- |
| SiteOne | Local binary; one crawl, 2 workers, 5 requests/second, robots honoured, no shared crawl cache or uploads. JSON includes broken links, redirects, SEO and headers. |
| Browser + axe | Chromium + `tools/axe.min.js`; up to `--browser-pages` successful crawled pages (default 6, maximum 50), desktop/mobile. Screenshots, console errors, headings/text, forms, CTAs, contacts, JSON-LD/microdata, trust signals. No form submissions. |
| Domain/email | Public DNS A/AAAA/MX/NS/SOA/TXT/CAA, checkdmarc SPF/DMARC/DNSSEC, RDAP registrar/expiry (expired = ERROR, <30 days = WARNING), DKIM (`--dkim-selectors`/`AMS_DKIM_SELECTORS`, otherwise 8 common selectors; unknown selector = NOT ASSESSED), A/MX/NS/SPF agreement across 1.1.1.1, 8.8.8.8 and 9.9.9.9, and DNSBL lookups of up to 4 MX and 2 website IPv4 addresses (at most 30 queries) on SpamCop, Barracuda (free resolver registration required, otherwise refused = NOT ASSESSED) and PSBL. Spamhaus ZEN only with `SPAMHAUS_DQS_KEY`. SMTP only with `--mail-tls-check`: at most 2 MX hosts, banner/EHLO/STARTTLS/certificate/QUIT, 10-second timeout, never MAIL/RCPT/VRFY/EXPN/DATA. Produces the Domain, DNS & Email Health summary (PASS / WARNING / ERROR / NOT ASSESSED), which reuses Security/Observatory evidence for TLS and headers. Does not measure inbox placement. |
| Security | HTTP headers and verified TLS certificate issuer/expiry/protocol/hostname. Not a penetration test. |
| MDN Observatory | Public v2 scan; aggregate grade only. Failure and localhost are explicit skips. |
| PageSpeed/CrUX | Mobile/desktop PageSpeed v5 with available URL/origin CrUX fields. Optional `PAGESPEED_API_KEY`. Separate CrUX only with `CRUX_API_KEY` or `GOOGLE_API_KEY`. Localhost and exhausted keyless quota are skipped. |
| DataForSEO | Disabled unless `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` and explicit `--dataforseo-max-usd` are present. One Australia/English domain rank overview request by default (`DATAFORSEO_LOCATION_CODE`, overridden by `--dataforseo-location-code`), one result, no retries/pagination. Estimated $0.01515 (including pricing headroom) is recorded before the call; a `--dataforseo-max-usd` cap below $0.01515 refuses the domain overview call. Presence discovery and `--keywords` SERP calls book against the same cap. Provider price changes are outside this client cap; verify [pricing](https://dataforseo.com/pricing/dataforseo-labs/dataforseo-labs-api) before enabling. No paid endpoint is used in tests. |
| Google Search Console | Explicit `--connect gsc`, service-account `GOOGLE_APPLICATION_CREDENTIALS` or OAuth `AMS_GSC_TOKEN_FILE`, and an accessible matching property. Optional `AMS_GSC_PROPERTY`. Read-only: top 100 queries/pages over 28 complete days with 3-day lag, plus sitemaps. OAuth requires an authorized-user JSON file created by `gsc-auth`. |
| Digital presence (`presence`) | Reuses browser DOM (meta tags, script URLs, inline tag snippets, `<link>` URLs, anchors, JSON-LD, request URLs), SiteOne headers/external URLs/robots.txt/canonical summary, domain_email MX/TXT and the GSC connection state. Its only requests, and only after a SiteOne crawl, are one `robots.txt` GET and one GET of the first same-origin `Sitemap:` URL (else `/sitemap.xml`), each with at most one redirect that keeps scheme, host and port, all within one 10-second deadline (passed as each request's timeout) and a 2 MB cap; no recursion. The complete body is parsed with ElementTree. Sitemap PASS (sitemaps.org `urlset`/`sitemapindex` with at least one `<loc>`) / WARNING (404, or fully retrieved but invalid; needs verification when robots.txt could not be read) / NOT ASSESSED (fetch failed, incomplete or declared on another origin). Detects Search Console/Bing verification, GA4, legacy UA, GTM, Clarity, Hotjar, Google Ads, Meta, LinkedIn, Bing UET, TikTok, WordPress/Shopify/Wix/Squarespace/Webflow/Joomla/Drupal and Google Workspace/Microsoft 365, with IDs as evidence. IDs count only in tag context (gtag calls, tag URLs, the GTM loader), never as bare strings. Listings (Google Business Profile, Bing, Apple Maps, Facebook, Instagram, LinkedIn and AU directories) only when the website links to them. Produces platform, listing, search/local signal (NAP, location pages, robots.txt, sitemap, canonicals), connected-readiness and customer access checklist tables. Statuses: Detected / Not detected (no public signal on sampled pages, never "no account") / Unknown (GTM present but GA4 not seen, or a consent manager / consent-gated tags present and the tag not seen loading) / NOT ASSESSED. NAP is a confirmed mismatch only on one page with one business entity, untruncated text and exactly one different phone or postcode shown; several entities or values, truncation, absence or values from different pages give a manual verification item. AI cannot create presence findings or findings cited only by presence evidence; GSC data appears only under "Connected business-system findings". Listings always need ownership confirmation; platform pages are never fetched, and search happens only through the DataForSEO SERP under the cap (see above). `AMS_ACCESS_EMAIL` names the AMS account in the checklist (placeholder if unset). |
| AI | `ANTHROPIC_API_KEY` selects Anthropic SDK (billable; no retries); otherwise authenticated `claude -p --output-format json` with strict schema, tools/hooks/MCP disabled and 300-second timeout; otherwise deterministic fallback. `--no-ai` disables both. Prompt/raw responses stay in the run directory. |

## Outputs and interpretation

```text
data/customers/<customer>/<UTC timestamp>/
  raw/<module>.json             # plus crawler stderr and AI prompt/response when enabled
  screenshots/                 # bounded desktop/mobile sample
  evidence.json                # stable ev-<module>-NNN IDs within the run
  findings.json
  results.json                 # scope, statuses, limitations, AI, findings, digital_presence
report.md                    # editable master generated from Jinja2
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
by the fixture. Platform and listing detection only sees the sampled pages: tags behind consent or
loaded from unsampled templates, and profiles not linked from the site, are reported as not detected
or unknown, never as absent. The access checklist never requests passwords; customers keep ownership. `modules` lists future capabilities (Elmo, Houtini, OpenSEO, GBP,
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
matching, quota failure, module isolation, platform/listing detection, readiness and access
checklist rules, and a real no-AI fixture run with PDF/citation checks.
The fixture test skips only when SiteOne, Chromium or axe is absent; run setup to exercise it.

Opt-in public HTTPS smoke check (SiteOne only, no paid APIs or AI):

```sh
AMS_LIVE_SMOKE=1 uv run pytest -q tests/test_https_crawl.py::test_live_siteone_https_example_com
```

This checks `https://example.com/` with certificate verification enabled. It is skipped in the
default suite. A successful smoke check does not establish that every customer host is reachable.
SiteOne transport statuses such as `-4:SEND-ERROR` and its fatal error appear in the module reason
and evidence; its 2.5.1 binary does not expose the underlying request exception. Inspect the
run's `raw/siteone.json` and `raw/target-resolution.json` when diagnosing a failed crawl.
SiteOne, redirect preflight and normal site-check probes identify as `AMSAssessment/1.0`.
SiteOne's `--user-agent=AMSAssessment/1.0!` preserves that exact identity; browser-shaped
crawler identities can trigger server/edge rejection even when TLS verification succeeds.
Redirects retain the requested target for domain/email checks and store identity; website
collectors use the separately recorded `resolved_url`. A preflight timeout or HTTP rejection
is a limitation and still allows SiteOne to try the last safe URL. Unsafe redirects stop it.
