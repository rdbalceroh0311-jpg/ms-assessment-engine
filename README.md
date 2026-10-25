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
`--no-ai`, `--connect gsc`, `--connect ga4`, `--dataforseo-max-usd X`, `--keywords 'k1,k2'`, `--dkim-selectors a,b`, `--mail-tls-check`. Default categories: technical, seo, geo,
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
retained. Per-page mobile/desktop lab-performance findings group the same way, listing each page
and its score. The lifecycle table shows non-current
findings; JSON retains every individual finding. Repeated coverage and evidence are grouped for display;
website coverage shows the latest run per website with earlier runs summarised in one line.
Same-site HTML pages published in the sitemap or public WordPress inventory are seeded into the crawl
(robots.txt applied; media, attachments and feeds excluded; at most half of `--max-pages`), and up to a
third of the `--browser-pages` sample is reserved for crawled seeds the key-page order would skip.
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
manually. GA4 and explicitly bound GSC reads use the connector framework below. Returned GSC rows are analysed
deterministically into section-7 findings (confirmed from GSC; recommendations need verification):
http/www variants of the website with impressions, no submitted sitemap while the website publishes
one, and default WordPress placeholder pages with impressions. AI cannot write findings on GSC evidence.

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
Only an engine-recorded successful bound-resource read with permission evidence sets `connected`.
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
Opening the UI does not load credentials. Explicit connector Test/Collect POST actions resolve the selected credential reference privately. The UI lists customers, runs, coverage,
register evidence, Current / Resolved / Not rechecked / Needs review findings and saved reports.
Manual verification and access updates use the same validated, locked store functions as the CLI.
Connector POST controls run bounded read-only tests/collections through the same service as the CLI; public website assessments remain in the CLI. It cannot execute arbitrary commands. File serving is limited to recorded customer output
directories and known report/results/evidence/findings files or PNG screenshots; traversal, symlinks,
raw files and credential files are excluded. Saved HTML is sandboxed with scripts disabled.

## Integrations and credentials

Environment overrides repository `.env`, which overrides `~/.config/ams-assess/credentials.env`.
Use `.env.example` for names. Credentials and runtime outputs are ignored by Git; do not stage them.
Raw page text, screenshots and AI prompts can contain customer information. Keep `data/` private.
AI-enabled runs send the explicit, redacted field projection described in
[DATA-FLOWS.md](docs/DATA-FLOWS.md); use `--no-ai` when inappropriate.

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
| AI | `AMS_AI_PROVIDER=anthropic` explicitly selects the SDK (API key, billable; no retries); `claude-cli` explicitly selects the CLI login with tools/hooks/MCP disabled; unset/`none` makes no AI call. Failures never switch accounts. `--no-ai` or a customer profile with `no_ai: true` disables both. Debug artefacts default to sanitised hashes; see [data flows and retention](docs/DATA-FLOWS.md). |

## Microsoft 365 read-only connector (`m365`)

This connector reads one explicitly selected Microsoft Entra tenant through Microsoft Graph
v1.0. It uses the shared credential references, binding locks, GET allowlist, evidence lifecycle,
systems register, local UI and report section 7. No tenant has been authorised for a live read;
verification uses synthetic Graph responses and mocked Microsoft authentication only. The connector
declares the framework's native `device_code` auth flow, auth hint and collection/request limits.

### Entra app registration (David / the customer administrator)

1. In **Microsoft Entra admin center → Entra ID → App registrations → New registration**,
   create a dedicated app named `AMS Assessment Read Only`. Select **Accounts in any
   organizational directory (Any Microsoft Entra ID tenant — Multitenant)**, excluding personal
   Microsoft accounts. Record its **Application (client) ID**. Separately record the **Directory
   (tenant) ID of the customer being assessed**, which may differ from the app's home tenant.
   See [app registration](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
   and [multitenant registration](https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant).
2. Under **Authentication → Advanced settings**, set **Allow public client flows = Yes**
   and save. This connector uses MSAL's local CLI device-code flow; no redirect URI, implicit
   grants, client secret or certificate is needed. The CLI displays the short-lived user code
   and `https://microsoft.com/devicelogin`; enter that code there and sign in to the authorised
   customer tenant. The private device code and tokens are never displayed. Sign-in polling is
   capped at 180 seconds. See [device-code authentication](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)
   and [the public-client-flow setting](https://learn.microsoft.com/en-us/entra/identity-platform/msal-client-applications).
3. Under **API permissions**, remove the default `User.Read` entry. Choose **Add a permission →
   Microsoft Graph → Delegated permissions**, and add exactly `Organization.Read.All`,
   `Domain.Read.All`, `User.Read.All`, `RoleManagement.Read.Directory`, `AuditLog.Read.All` and
   `Policy.Read.All`. Add no application permissions, mail/files permissions or write scopes.
   The endpoint table below verifies these permissions. MSAL adds only the protocol scopes
   `openid`, `profile` and `offline_access` for sign-in and refresh; the connector rejects broader
   Graph grants and missing permissions rather than silently expanding consent.
4. Obtain **tenant-wide admin consent in the customer's tenant** from its authorised consent
   administrator (for these delegated scopes, Cloud Application Administrator is supported).
   For an app registered in that tenant, use **App registrations → app → API permissions →
   Grant admin consent**. To provision the app in another tenant, run the `auth` command below
   with that customer's tenant ID and have its consent administrator complete the device-code
   sign-in, review the six permissions and select **Consent on behalf of your organization**.
   Confirm the grant under **Enterprise apps → app → Permissions**; repeat `auth` as the reader
   account in step 5 before any `test` or `collect`. Consent in the app's home tenant does not
   grant customer-tenant access. See [Microsoft's admin consent instructions](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/grant-admin-consent).
5. Use a customer-tenant **member** account for collection, with **Directory Readers + Security
   Reader**, or an existing **Global Reader** assignment. These reader roles cover the endpoint
   requirements below. Activate any required PIM assignment before authenticating. App consent
   and the signed-in user's role are separate requirements; granting app consent alone is
   insufficient. Do not use a Global Administrator account for routine collection.
6. Confirm existing **Entra ID P1/P2** entitlement for authentication registration reporting and
   Conditional Access. The connector does not purchase licences or change configuration. Without
   required entitlement, affected endpoint evidence is `licence_required`; successful endpoints
   remain available with overall `connected_partial`. Ordinary permission denial stays
   `permission_required`. Neither status is a security finding. See
   [registration-report licensing](https://learn.microsoft.com/en-us/entra/identity/authentication/howto-authentication-methods-activity)
   and [Conditional Access licensing](https://learn.microsoft.com/en-us/entra/identity/conditional-access/overview#license-requirements).

### Local setup, authorisation and explicit tenant binding

Run from this checkout. MSAL is a declared dependency installed from `uv.lock` by `uv sync`;
it loads only when Microsoft authentication is requested.

```sh
uv sync
```

After the customer explicitly authorises assessment, substitute its slug, approved website,
tenant UUID and application client UUID below. Choose a distinct lowercase credential reference
for each tenant. Native framework auth flags create the private reference and MSAL cache; no
app JSON file is required.

```sh
uv run ams-assess customer --customer CUSTOMER_SLUG --website https://APPROVED_CUSTOMER_DOMAIN
uv run ams-assess connect m365 auth --credential-ref CUSTOMER-m365 --tenant-id CUSTOMER_TENANT_UUID --client-id APPLICATION_CLIENT_UUID
uv run ams-assess connect m365 list-resources --credential-ref CUSTOMER-m365
uv run ams-assess connect m365 bind --customer CUSTOMER_SLUG --credential-ref CUSTOMER-m365 --resource tenant:CUSTOMER_TENANT_UUID
uv run ams-assess connect m365 test --customer CUSTOMER_SLUG
uv run ams-assess connect m365 collect --customer CUSTOMER_SLUG --days 28
uv run ams-assess connectors --customer CUSTOMER_SLUG
uv run ams-assess run --customer CUSTOMER_SLUG --target https://APPROVED_CUSTOMER_DOMAIN --connect m365 --no-ai
```

Use lowercase reference names, for example `example-m365` (the uppercase text above is a
replacement marker). The website option replaces the saved website list; repeat `--website`
for each approved website that should remain. `tenant:<UUID>` is the public resource identifier
syntax; bare UUIDs are rejected by the shared register's opaque-secret guard. Listing returns
only the credential's configured tenant, never all visible tenants, and does not bind it.
The test refuses a mismatch among the credential tenant, bound tenant and Graph organization.
It also requires an exact match between an organization verified domain and a saved profile
website hostname (case/IDNA/trailing-dot normalization, removing a leading `www.`). It never uses
an arbitrary previous run target as proof of customer identity. A missing profile or unmatched
domain stops collection; a deliberate cross-customer `--force --reason ...` binding does not
override this tenant/domain check.

The reference is stored in `~/.config/ams-assess/connectors.json`. The client ID, tenant ID,
selected account and MSAL refresh cache stay in `~/.config/ams-assess/m365-<reference>-token.json`
(0600, directory 0700), protected by shared credential locks. Customer records contain the
reference name and resource ID only. Silent MSAL authentication refreshes expired access tokens;
revoked tokens require repeating `auth` with the same reference, tenant ID and client ID. Changing tenant
or application requires a new reference, preventing old customer bindings from being retargeted.
For cached access tokens, the connector uses [MSAL's token-cache search](https://learn.microsoft.com/en-us/python/api/msal/msal.token_cache.tokencache?view=msal-py-latest#msal-token-cache-tokencache-search)
to verify the exact scopes of the returned token for the selected account, tenant and app.
Missing, ambiguous or extra cached scopes are refused; network token responses still require
the exact delegated scope set.

### Verified Graph v1.0 reads and delegated permissions

Checked against Microsoft Learn on **2026-10-24**. All paths below are relative to
`https://graph.microsoft.com/v1.0`; every operation is **GET**. The HTTP allowlist rejects other
methods, hosts, versions and endpoints. OAuth discovery/token exchanges are handled by MSAL
against the configured tenant's Microsoft identity authority; they do not write Graph resources.

| Endpoint / official reference | Delegated permission used | Collected evidence / role requirement |
| --- | --- | --- |
| [`/organization`](https://learn.microsoft.com/en-us/graph/api/organization-list?view=graph-rest-1.0) | `Organization.Read.All` | Tenant ID and verified domains; Directory Readers or Global Reader. This scope is also reused for subscriptions. |
| [`/domains`](https://learn.microsoft.com/en-us/graph/api/domain-list?view=graph-rest-1.0) | `Domain.Read.All` | Domain, verification flag and Managed/Federated authentication type; Directory Readers or Security Reader. |
| [`/users`](https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0) | `User.Read.All` | Enabled/disabled, guest/member and licensed/unlicensed counts. `User.ReadBasic.All` cannot supply the requested account/licence properties; guest callers are unsupported. |
| [`/subscribedSkus`](https://learn.microsoft.com/en-us/graph/api/subscribedsku-list?view=graph-rest-1.0) | `Organization.Read.All` | Assigned/prepaid seat totals across SKUs; Directory Readers. `LicenseAssignment.Read.All` is the narrowest permission in isolation, but Organization.Read.All is already needed and explicitly supported, so no additional grant is requested. Only `$select` is sent. |
| [`/roleManagement/directory/roleAssignments`](https://learn.microsoft.com/en-us/graph/api/rbacapplication-list-roleassignments?view=graph-rest-1.0) | `RoleManagement.Read.Directory` | Active assignments, distinct roles/holders and privileged built-in role counts; Directory Readers. Uses the documented [`roleDefinition` expansion](https://learn.microsoft.com/en-us/graph/api/resources/unifiedroleassignment?view=graph-rest-1.0) with template ID and built-in flag. |
| [`/reports/authenticationMethods/userRegistrationDetails`](https://learn.microsoft.com/en-us/graph/api/authenticationmethodsroot-list-userregistrationdetails?view=graph-rest-1.0) | `AuditLog.Read.All` | MFA registered/capable counts, administrator count and redacted administrators without registration; Security Reader (Reports Reader also works for this endpoint). Disabled users are excluded. Only `$filter` is documented, so no `$select`/`$top` is assumed: each received page is immediately reduced. |
| [`/policies/identitySecurityDefaultsEnforcementPolicy`](https://learn.microsoft.com/en-us/graph/api/identitysecuritydefaultsenforcementpolicy-get?view=graph-rest-1.0) | `Policy.Read.All` | Security defaults `isEnabled`; the API page specifies this delegated permission without an additional role list. |
| [`/identity/conditionalAccess/policies`](https://learn.microsoft.com/en-us/graph/api/conditionalaccessroot-list-policies?view=graph-rest-1.0) | `Policy.Read.All` | Enabled/disabled/report-only counts and enabled policies with an MFA requirement; Security Reader. |

If Graph rejects the nested `roleDefinition($select=templateId,isBuiltIn)` projection with
HTTP 400, the connector retries that page once with `$expand=roleDefinition`, retaining the
assignment `$select` and pagination limits. The broader role definition is reduced to the same
counts and redacted assignments; extra fields are discarded.

### Evidence meaning, bounds and privacy

- These are current configuration/report snapshots, not historical sign-in telemetry. The shared
  `--days` (default 28) or `--start`/`--end` period is provenance; it does not retrospectively filter
  these APIs. Each evidence record includes connector, selected resource, collection timestamp,
  period, endpoint, status and row/truncation coverage.
- [Graph pagination](https://learn.microsoft.com/en-us/graph/paging) follows `@odata.nextLink`
  only on the original endpoint, retaining the decoded query values through HTTP parameter
  encoding. Host/path changes, altered projections and repeated links are refused. Collection
  has five pages and 500 processed rows per endpoint, 100 redacted holder samples, the shared
  45-second Graph deadline, 80-request ceiling, 10-second request timeout and 2 MB response cap.
  Each 429/5xx read receives up to three attempts with bounded backoff. Pagination cursors are
  never saved. Caps and failures preserve earlier aggregates, with explicit partial coverage.
- [Registration and capability](https://learn.microsoft.com/en-us/graph/api/resources/userregistrationdetails?view=graph-rest-1.0)
  are distinct from enforcement. Registration reporting can lag 36 hours or longer. The only
  automatic security finding is administrators reported without MFA registration, labelled as
  confirmed **report evidence**, with an explicit enforcement caveat. Empty, unknown, failed or
  truncated registration reports cannot resolve that finding; a complete equivalent passing
  recheck can. This tenant finding does not duplicate public website/email findings.
- Conditional Access counts describe policy configuration. An `OR` policy offering another grant
  control alongside MFA is counted as an MFA option, not an MFA requirement; report-only policies
  are not enabled enforcement. [Grant-control semantics](https://learn.microsoft.com/en-us/graph/api/resources/conditionalaccessgrantcontrols?view=graph-rest-1.0)
  do not establish whether conditions/exclusions cover a particular user. Authentication-strength
  policies are counted separately without inferring their MFA guarantee. Legacy per-user MFA,
  group membership, actual sign-in enforcement and PIM eligibility are outside these reads.
- Privileged built-in roles use the dated badges in the [Microsoft role reference](https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/permissions-reference),
  since [v1.0 role definitions](https://learn.microsoft.com/en-us/graph/api/resources/unifiedroledefinition?view=graph-rest-1.0)
  do not expose `isPrivileged`. Custom/unknown assignments are separately flagged for review;
  an absent badge is not proof of safety. Principals can be users, groups or service principals.
  Role IDs are retained; principal IDs become hashes salted by customer and tenant.
- Raw users, names, email addresses, registration inventories and policy user targeting are never
  persisted. Evidence keeps counts/flags and capped redacted role/admin samples. The existing AI
  boundary receives only status/row/truncation aggregates, excluding even those redacted samples.
  Credentials and provider error text do not enter evidence, UI, reports or logs.

Synthetic checks (no tenant sign-in or Graph access):

```sh
uv run pytest -q tests/test_m365.py
uv run pytest -q
uv run ruff check .
uv run ty check ams_assess tests
```

The real-MSAL mocked-HTTP cases use the SDK installed by `uv sync` and fail if it is
missing. All other Microsoft contract cases use a synthetic SDK boundary.

## Bound connectors: GA4 setup and use

GA4 uses only `https://www.googleapis.com/auth/analytics.readonly`. Ask the customer to grant
Viewer access to the specific property; customers retain ownership and recovery methods. In your
Google Cloud OAuth project, enable **Google Analytics Admin API** and **Google Analytics Data API**,
configure the consent screen (and test users if the app is in testing), and create a **Desktop app**
OAuth client. Save its JSON outside Git, preferably `~/.config/ams-assess/google-desktop-client.json`
with mode 0600. These project/access setup steps are manual; the connector never changes settings.
No paid service is used. No live access is exercised by the tests.

From this worktree, David can authorise GA4 and explicitly bind 2EC Traffic's property as follows
(use the customer's existing slug if it differs from `2ec-traffic`):

```sh
uv run ams-assess connect ga4 auth --credential-ref ga4-2ec \
  --client-secrets ~/.config/ams-assess/google-desktop-client.json
uv run ams-assess connect ga4 list-resources --credential-ref ga4-2ec
uv run ams-assess connect ga4 bind --customer 2ec-traffic \
  --credential-ref ga4-2ec --resource properties/278830925
uv run ams-assess connect ga4 test --customer 2ec-traffic
uv run ams-assess connect ga4 collect --customer 2ec-traffic --days 28
uv run ams-assess connectors --customer 2ec-traffic
uv run ams-assess report --customer 2ec-traffic
```

The browser consent flow has a three-minute timeout. It requests only the selected connector's
scopes, disables scope combination, and writes a separate token; it never widens a GSC token.
References live in `~/.config/ams-assess/connectors.json`, mapping a short name to provider,
connector, auth kind, and a private file path or environment-variable name. OAuth tokens live in
`~/.config/ams-assess/<connector>-<reference>-token.json`. The directory is 0700; files are 0600
and atomically replaced. Refresh is automatic. A revoked/expired credential gives a re-authorisation
command. Only the reference name is stored with the customer's binding in `assessment.json`.
Never put tokens, client secrets, passwords or credential JSON into UI forms, notes or evidence.
For a module with an API-key/app/SSH auth adapter, `connect <key> auth --credential-ref <name>
--credential-file <private-json>` or `--env-var <VARIABLE_NAME>` registers a source; environment
variables override `credentials.env`. It does not test access or mark the integration connected.

`list-resources` uses Admin `accountSummaries.list` and **does not bind or assess** anything.
`bind` selects exactly one resource. A resource bound to another customer is refused across
credential references; an exceptional shared-property binding requires `--force --reason '...'`,
recorded in binding history. Rebinding resets connection/collection state, retains old evidence,
and rejects stale in-flight results. Customer updates use the existing store locks.

`collect` first tests the bound resource. The default is the last 28 complete days in the property's
time zone. `--days 1..366` or `--start YYYY-MM-DD --end YYYY-MM-DD` selects a bounded period;
today/future dates are refused. Example combined run:

```sh
uv run ams-assess run --customer acme --target https://example.com --connect ga4 --no-ai
uv run ams-assess connect ga4 collect --customer acme --start 2026-09-01 --end 2026-09-28
```

The Admin sample includes property metadata, web streams, key events, custom-dimension count,
retention and Ads-link count. Data reports cover daily sessions/users/engagement, acquisition,
top landing paths, events and configured key-event counts. Average engagement seconds per active
user is `userEngagementDuration / activeUsers`, not average session duration. Landing query strings
and URL credentials/fragments are discarded; emails are reduced to domain-level references.
No user inventories are collected. AI receives coverage aggregates, not report rows or inventories.

Standalone connector tests/collections update lifecycle only for that connector's findings;
public and other connected findings retain their state and recommendations. The connected stage
is recorded only for `connected` or `connected_partial` results, including a successful test.

Each endpoint records resource, collection time, period, status, rows and truncation. Lists/reports
have at most five 100-row pages; landing pages are the top 100 by sessions. HTTP responses are capped
at 2 MB, requests at 80 including retries, individual I/O at 10 seconds and the read phase at 45 seconds
(40 in the UI). Transient rate limits/5xx/timeouts receive at most three attempts with bounded
backoff. Provider errors are classified before retrying: disabled APIs, missing permissions,
licence gates and approval requirements (including Google zero quota) are not retried. OAuth refresh
has a separate bounded transport timeout. Quota responses stop further Data API calls when exhausted.
Successful pages/endpoints survive subsequent failures. Sampling/thresholding/caps are explicit
partial coverage and cannot support zero-event findings. Only allowlisted GETs and the exact
read-only Data `runReport` POST are permitted; redirects and all write endpoints are refused.

CLI, UI and report use the same statuses: `not_configured`, `credential_missing`,
`credential_expired`, `permission_required`, `licence_required`, `rate_limited`, `not_found`,
`provider_unavailable`, `api_not_enabled`,
`approval_required`, `failed`, `connected`, `connected_partial`. The register's integration field is
engine-controlled; human `ams_access=granted` remains separate. The UI shows each endpoint's coverage
and offers reference, bind, test and bounded collect POST controls under its existing token/CSRF rules.
Only configured/attempted connectors appear in report section 7; public-only reports stay public.

Findings distinguish confirmed zero events from unavailable data, and configured-but-unrecorded key
events from recorded events. Enquiry-name matching is an inferred heuristic needing verification:
clicks and `form_start` never establish completed enquiries. Stream URI comparisons and public tag
cross-references retain their evidence; public missing-tag findings are referenced, not duplicated.
Stable resource/check identities use the existing lifecycle: only an equivalent complete passing
recheck resolves a finding; unavailable/partial checks do not. Evidence and history are retained.

GSC also has an explicit framework binding (`connect gsc ...`, `sc-domain:example.com` or a URL
property) with its own `webmasters.readonly` token. Existing `gsc-auth` and unbound legacy
`run --connect gsc` remain supported; the legacy flow keeps its prior matching-property behaviour.

Provider contracts: [Admin account listing](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/accountSummaries/list),
[Data runReport](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport),
[Data dimensions/metrics](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema),
[Admin retention](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties/getDataRetentionSettings).

### Google Workspace: authorise, bind, test and collect

`workspace` reads the Admin SDK Directory and Reports APIs. It uses exactly these six scopes
(each prefixed with `https://www.googleapis.com/auth/`):

| Scope suffix | Allowlisted GET endpoint and official reference |
| --- | --- |
| `admin.directory.customer.readonly` | [`/admin/directory/v1/customers/{customerKey}`](https://developers.google.com/workspace/admin/directory/reference/rest/v1/customers/get) |
| `admin.directory.domain.readonly` | [`/admin/directory/v1/customer/{customer}/domains`](https://developers.google.com/workspace/admin/directory/reference/rest/v1/domains/list) |
| `admin.directory.user.readonly` | [`/admin/directory/v1/users?customer={id}`](https://developers.google.com/workspace/admin/directory/reference/rest/v1/users/list) |
| `admin.directory.group.readonly` | [`/admin/directory/v1/groups?customer={id}`](https://developers.google.com/workspace/admin/directory/reference/rest/v1/groups/list) |
| `admin.directory.rolemanagement.readonly` | [`/admin/directory/v1/customer/{customer}/roleassignments`](https://developers.google.com/workspace/admin/directory/reference/rest/v1/roleAssignments/list) |
| `admin.reports.usage.readonly` | [`/admin/reports/v1/usage/dates/{date}?customerId={id}`](https://developers.google.com/workspace/admin/reports/reference/rest/v1/customerUsageReports/get) |

All API reads use `https://admin.googleapis.com`. The connector refuses all writes, other
endpoints and redirects. It requests no Gmail, Drive, Calendar, security-management write or
Groups Settings scopes. Official contracts above and the field references below were checked
on 2026-10-24. No live customer read was performed during implementation; tests use synthetic HTTP.

**Access David needs from 2EC.** Ask 2EC's existing **Google Workspace Super Admin** to approve
the assessment and complete the browser OAuth consent for these six read-only scopes on the
authorised assessment machine. The sign-in must belong to the intended 2EC Workspace customer.
This avoids assigning David a new Super Admin role or sharing an admin password. Google documents
that only super admins can view another admin's security settings, and its admin-console role
assignment review is also restricted to super admins. Full admin security coverage should therefore
use a customer-controlled super-admin consent session; a general user, Groups Reader, or Users
Read role alone is not sufficient assurance of full coverage.
See [administrator privileges](https://knowledge.workspace.google.com/admin/users/administrator-privilege-definitions)
and [role assignment visibility](https://knowledge.workspace.google.com/admin/users/view-role-assignments-and-privileges).

A delegated account can return partial evidence when granted **Users > Read** across all
organisational units, **Groups > Read API privilege**, relevant **Domain Settings/Domain Management**
access, and **Reports**. Do not grant broad write privileges merely to remove a limitation:
endpoint denials remain `permission_required` with the required privilege, and missing security
flags remain unknown. Role assignments require role-reading access; ask the super admin if denied.
Google's [role/privilege mapping](https://developers.google.com/workspace/admin/directory/v1/guides/manage-roles)
and the privilege definitions above distinguish scopes from the signed-in administrator's rights.
The connector cannot prove that a delegated administrator sees every organisational unit.

In the OAuth project's Google Cloud console, manually enable **Admin SDK API**, configure the
[OAuth consent screen](https://developers.google.com/workspace/guides/configure-oauth-consent), and
create a **Desktop app** OAuth client. If the app is external and in testing, include the consenting
admin as a test user. If 2EC's API Controls restrict third-party apps, their super admin must allow
this OAuth client to request the specified six scopes using
[Specific Google data access](https://knowledge.workspace.google.com/admin/apps/control-which-apps-access-google-workspace-data).
The connector does not change these controls,
create users, purchase licences or configure domain-wide delegation. Keep the Desktop client JSON
and the generated token in `~/.config/ams-assess` (0700 directory, 0600 files).

Run these commands **only after that access and the tenant have been explicitly authorised**:

```sh
uv run ams-assess connect workspace auth --credential-ref workspace-2ec \
  --client-secrets ~/.config/ams-assess/google-desktop-client.json
uv run ams-assess connect workspace list-resources --credential-ref workspace-2ec
# Replace this synthetic value with the exact id returned above after 2EC confirms it.
workspace_resource='C01example/primary.example'
uv run ams-assess connect workspace bind --customer 2ec-traffic \
  --credential-ref workspace-2ec --resource "$workspace_resource"
uv run ams-assess connect workspace test --customer 2ec-traffic
uv run ams-assess connect workspace collect --customer 2ec-traffic --days 28
uv run ams-assess connectors --customer 2ec-traffic
uv run ams-assess report --customer 2ec-traffic
```

Use the existing customer slug if it differs. No 2EC customer ID/domain is guessed or prebound.
`list-resources` resolves [`my_customer`](https://developers.google.com/workspace/admin/directory/v1/guides/manage-customers)
to the account's immutable customer ID and primary domain, verifies that domain in its domain list,
and returns one `<customer-id>/<primary-domain>` selector. It does not collect users or bind anything.
The `my_customer` GET is allowed only during this explicit unbound discovery operation; test/collect
allow only concrete customer IDs. Discovery alone does not authorise customer collection.
Only concrete IDs can be bound; storing `my_customer` would let a credential change silently change
the tenant. Primary domains are normalised for case, IDNA and a trailing dot. Test/collect verify
both the customer response and verified primary-domain membership; an ID/domain mismatch or failed
domain read refuses further collection. Before returning `connected`, they also require a verified
Workspace domain (primary or secondary) to match a registrable domain from the **saved AMS customer
profile websites**. Matching normalises IDNA, case, trailing dots, `www` and subdomains using the
bundled public suffix list, without network lookups. Public suffixes such as `co.uk` and private
suffixes such as `github.io` do not make unrelated customers match. No saved profile domain or no
verified match returns a fixed `permission_required` message before inventory collection. Prior run
targets and fallback websites never substitute for the saved profile. The domain evidence records
the matched domain and profile evidence reference. Secondary domains are not alternative binding
selectors. Existing framework isolation rejects sharing that resource
between customer stores unless `--force --reason 'approved non-secret reason'` records the exception.

Collection records verified/unverified/unknown domain flags; user counts by active, suspended,
archived or unknown status (archived takes precedence over suspended); super/delegated admin counts;
separate enrolled/not-enrolled/unknown and enforced/not-enforced/unknown counts; and a customer-scoped
hashed list of **active** admins explicitly not enrolled. The overall 2SV inventory includes all
visible user statuses; a separate `active_enforcement` aggregate drives the enforcement finding.
Active means both suspended and archived are explicitly false; unknown status never counts as active.
Super/delegated counts are the individual Directory
flags; `any` is their union. Names, email addresses, phone numbers, addresses, group memberships and
role descriptions are neither requested nor retained. Raw user/group/assignment IDs are requested
transiently for duplicate detection and hashing but never persisted. Role IDs are non-personal identifiers;
role assignment counts are grouped by role ID, assignee type and customer/OU scope. Direct assignments
to groups are counted, but membership is not expanded. See the
[User fields](https://developers.google.com/workspace/admin/directory/reference/rest/v1/users),
[Domain fields](https://developers.google.com/workspace/admin/directory/reference/rest/v1/domains), and
[RoleAssignment fields](https://developers.google.com/workspace/admin/directory/reference/rest/v1/roleAssignments).

Groups are counted. External sharing is **unavailable**, stored as `null`, never zero or secure:
[Directory Group](https://developers.google.com/workspace/admin/directory/reference/rest/v1/groups)
does not expose sharing policy. [Groups Settings GET](https://developers.google.com/workspace/admin/groups-settings/v1/reference/groups/get)
requires the additional write-capable `apps.groups.settings` scope, which this connector never requests.
Review that coverage separately with 2EC; it is not an access request to widen this connector.

Directory evidence is a snapshot at collection time. Reports supplies **one snapshot on the selected
period's end date**, in its documented UTC-08:00 reporting date, not a sum of users over the period.
Default and explicit periods use complete days in that fixed reporting timezone; today is refused.
It requests only the six `accounts:num_users_2sv_*` metrics for enrolled/not enrolled,
enforced/not enforced, and protected/not protected. These are
[distinct Google-reported metrics](https://developers.google.com/workspace/admin/reports/v1/appendix/usage/customer/accounts).
Protection is never calculated from the Directory flags, and historic reports never replace current
enforcement. Missing metrics, warnings, empty report payloads and lag are explicitly partial coverage,
not zero. If the default end date is too recent, choose an older explicit period, for example
`--start 2026-09-01 --end 2026-09-28`; there is no silent fallback to a different date.

Users, groups, assignments and usage pagination are limited to five pages and 500 returned rows per
endpoint; Directory page requests ask for 100 rows. Non-paginated domains retain at most 500 rows.
Role-assignment `maxResults` is additionally capped at 200 even if the context requests larger pages;
see the [roleAssignments.list parameters](https://developers.google.com/workspace/admin/directory/reference/rest/v1/roleAssignments/list).
Workspace declares a 90-second collection budget and 66-request cap: two binding reads plus four
five-page lists, with up to three attempts per read. Caller budgets can only reduce these limits;
the UI caps collection at 40 seconds. The shared 2 MB response and 10-second I/O caps also apply.
Transient 429/5xx/timeouts use shared bounded backoff; exhausted rate limits remain
`rate_limited`. Disabled APIs and permission/approval failures are classified before retries.
Successful pages survive a later failure. OAuth uses a separate Workspace token, automatic refresh
and expiry/revocation status.
Endpoint evidence records customer/domain binding, collection time, selected period, actual report
date, status and coverage. AI receives only framework coverage aggregates, never admin identifiers.

Confirmed 2SV findings cover active admins explicitly not enrolled and active users explicitly not
enforced, excluding suspended and archived users. Suspended accounts still carrying admin flags
remain a separate finding. Report wording explicitly limits results to the users visible to the
authorised admin: a delegated admin restricted to some organisational units sees a subset.
Partial positive observations are lower bounds;
empty, inaccessible, incomplete or missing-flag results cannot establish a passing security recheck.
Only a complete nonempty passing Directory recheck of the same returned user population resolves
those findings through the existing lifecycle. One customer-scoped population digest prevents a
narrower OU view from clearing earlier findings; no extra user inventory is saved. Changed membership
leaves the earlier observation not rechecked until equivalent coverage or human review is available.
Findings appear in report section 7 with connected evidence. No public DNS/email finding
is recreated or upgraded, and no overall "secure" conclusion follows from these checks.
Standalone Workspace tests and collections preserve public findings, their lifecycle history and
recommendations, including on denied or partial reads. Workspace declares the `client_secrets`
auth flow and a private Desktop-client path in its auth hint. It does not use `ctx.connected_evidence`:
the customer-ID/domain binding is not a website scope, and these checks need only Admin SDK evidence.

Synthetic verification (no paid services, real credentials or customer configuration changes):

```sh
uv run pytest -q tests/test_workspace.py
uv run pytest -q
uv run ruff check .
uv run ty check ams_assess tests
```

### How to add a connector

Add one module under `ams_assess/connectors/`, export a `core.Connector` declaration, and add it to
`connectors.REGISTRY`. Add mocked-provider tests. No shared CLI/UI/report/store changes are needed.

- Declare `key`, `label`, `provider`, `auth_kind`, least-privilege `scopes`, `resource_type`,
  `categories` and exact `ReadOperation(method, url_pattern)` allowlist entries.
- Supply `normalize_resource`, `list_resources(ctx) -> Endpoint`, `test(ctx) -> Endpoint`,
  `collect(ctx) -> None`, and `analysers` that append findings plus explicit pass/fail outcomes.
  `test` must verify the requested resource identity with an authenticated read.
- Supply `authenticate(spec, ref, directory=...) -> AuthMaterial`; optionally `authorize` for OAuth.
  Reuse `google_authenticate`/`google_authorize` for Google, or `auth.load_reference` for private JSON,
  env names and `credentials.env`. Never return secrets in exceptions or evidence. An optional
  `transport_factory(AuthMaterial)` can map allowlisted routes to a provider SDK/SSH read transport.
- All provider operations go through `ctx.http.request`. Use `ctx.capture` and update endpoint
  payload/row coverage after each page so partial successes survive. Enforce row/page bounds, minimise
  fields, and cite generated endpoint evidence IDs. Emit explicit complete passing outcomes only
  for checks actually rechecked, using resource/check/component identities; missing data is no pass.
- The shared service handles binding isolation, secret redaction, aggregate-only AI projection,
  persistence, register status and UI/report views. Test auth, allowlist, errors, caps, privacy,
  lifecycle and cross-customer isolation with synthetic fixtures.

Optional `Connector` settings keep provider-specific setup and budgets in its module:

- `auth_hint: str = ""` overrides the displayed auth command (never include secrets).
- `auth_flow: Literal["client_secrets", "device_code", "reference"] | None = None` defaults to
  `client_secrets` for `google_oauth`, otherwise `reference`. Existing Google callbacks keep
  `authorize(spec, ref, client_secrets_path)`. Device-code callbacks use
  `authorize(spec, ref, *, tenant_id, client_id)`; the CLI accepts `--tenant-id` and `--client-id`
  without a client secret. Reference flows use `--env-var` or `--credential-file`; default hints
  suggest an environment variable for API keys and private configuration for SSH/app credentials.
- `collection_seconds: float = 45` and `max_requests: int = 80` bound collection/test/list reads,
  including retries. Caller limits can only reduce these budgets (the UI additionally caps at 40s).

Both `connect <key> collect` and `run --connect <key>` pass the same `ConnectorContext` to
`collect(ctx)` and each `analyser(ctx, result)`. It includes:

- `ctx.profile: Mapping[str, Any]`: the current stored approved fields (`name`, `aliases`,
  `websites`, `abn`, `phones`, `locations`, `service_area`, `industry`, `buying_model`).
- `ctx.profile_evidence_id: str` and `ctx.profile_evidence: EvidenceView | None`: the current
  profile's citation and evidence, retaining `customer_statement` provenance. Without an approved
  profile these are an empty mapping, empty ID and `None`; a target URL does not create one.
- `ctx.public_evidence: tuple[EvidenceView, ...]` and `ctx.public_evidence_refs: tuple[str, ...]`:
  latest public samples per source/site. Current-run samples replace the matching stored group;
  other latest stored groups remain. Profile/discovery/other connector records are excluded.
  `EvidenceView` exposes `id`, `source`, `url`, `description`, `data`, `provenance`.
- `ctx.connected_evidence: tuple[EvidenceView, ...]`: latest collection per other connected
  source for this customer and explicit bound site, including legacy and framework GSC evidence.
  URL-prefix properties must match scheme/host/port/path (an empty path means `/`); a GSC domain
  property can match a root URL with the same host. Broader parent domains/prefixes, other sites,
  and this connector's own evidence are excluded. An unbound or opaque resource ID yields `()`;
  profile websites never grant site scope. Current-run evidence replaces matching stored samples.
  All endpoints from the latest collection remain, including failed/partial results; check their
  period/status/coverage before comparing. Views retain provenance, redact secrets/emails, and
  never include credential configuration. No other customer's store is read for this context.

These are detached read-only views: nested mappings are immutable and lists become tuples.
Connectors do not read the store. Persist selected comparison values and source reference IDs in
the connector's own endpoint evidence, and cite that endpoint evidence in findings. Preserve the
source provenance on derived findings, such as `customer_statement` for profile comparisons.

Optionally declare `Connector(classify_error=classify_error, ...)` with this signature:

```python
def classify_error(status_code: int, body: Mapping[str, Any]) -> ErrorClassification | None:
    if body.get("error", {}).get("code") == "LicenseRequired":
        return ErrorClassification("licence_required", "Microsoft Entra ID P1 licence required")
    return None  # shared default classification
```

Import `ErrorClassification` from `ams_assess.connectors.core`. The hook receives parsed JSON
after secret/email redaction, capped at 16 KiB, 8 levels and 64 items per container; invalid/non-object
JSON becomes `{}`. Machine-readable `error.status`, `error.code`, `details[].reason` and quota
metadata are prioritised. Return an error status and a specific missing permission/role/licence
in `detail`. Only `rate_limited` and `provider_unavailable` are retryable. The shared default handles
Google `SERVICE_DISABLED`/`accessNotConfigured`, insufficient scopes/`PERMISSION_DENIED`, and
`RESOURCE_EXHAUSTED` with `quota_limit_value` zero. Hook details are redacted again and capped at
2,000 characters before reaching evidence, CLI, UI or report status. Raw error bodies are not stored.

## Bing Webmaster Tools (`bing_wmt`)

This connector reads **Bing Webmaster Tools**, independently of Bing Places. It requires an
existing API key and an explicitly selected, verified site. It does not add/verify sites, submit
URLs or feeds, change crawl settings, or manage users. No paid service is used.

Microsoft documents [API-key and OAuth access](https://learn.microsoft.com/en-us/bingwebmaster/getting-access).
This implementation uses the API-key option only: there is no OAuth token to refresh. The key
belongs to a user and can access that user's verified sites. Ask the owner for
[ReadOnly site access](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.siteroles.userrole?view=bing-webmaster-dotnet)
where available; the key itself is not restricted to read operations. The connector enforces a
GET-only allowlist containing exactly the nine methods below, refusing all other routes and verbs.
The declaration's permission description is a site role, not an OAuth scope.

Store `BING_WMT_API_KEY` in `~/.config/ams-assess/credentials.env` using a private editor, never a
CLI argument, repository file, report or chat. Keep the directory mode 0700 and the file 0600.
Alternatively, `--credential-file ~/.config/ams-assess/bing-key.json` can reference a private JSON
object with an `api_key` field. The shared reference loader also supports environment overrides.
The connector declares `auth_flow='reference'` and a `BING_WMT_API_KEY` auth hint. Query-key
requests run under the shared private logging guard; registered key values are redacted before
any evidence or status is emitted, and provider error bodies/URLs are never echoed.
Registration stores only the source reference; it neither verifies access nor binds every visible
site. An invalid/revoked key produces `credential_expired` with instructions to update the existing
private source. Rotation/re-authorisation is manual; this connector never obtains new access.

From this worktree (replace the example site with the **exact** verified URL in the listing):

```sh
uv sync
uv run ams-assess connect bing_wmt auth --credential-ref bing-2ec --env-var BING_WMT_API_KEY
uv run ams-assess connect bing_wmt list-resources --credential-ref bing-2ec
uv run ams-assess connect bing_wmt bind --customer 2ec-traffic \
  --credential-ref bing-2ec --resource 'https://2ectraffic.com/'
uv run ams-assess connect bing_wmt test --customer 2ec-traffic
uv run ams-assess connect bing_wmt collect --customer 2ec-traffic --days 28
uv run ams-assess connect bing_wmt collect --customer 2ec-traffic \
  --start 2026-09-01 --end 2026-09-28
uv run ams-assess connectors --customer 2ec-traffic
uv run ams-assess report --customer 2ec-traffic
```

`bind` records a selection; `test` and every `collect` authenticate that exact URL using
`GetUserSites` with `IsVerified=true` before any site-data read. Scheme, hostname, path and trailing
slash are not guessed. Unverified sites are omitted from the listing. Other sites' verification
codes and inventory are never persisted in the bound customer's evidence. A site already bound to
another customer is refused across credential references unless `--force --reason '...'` records
an explicit shared-site exception. Collection uses the existing CLI, UI, register and report
section 7 lifecycle; a partial/unavailable recheck cannot resolve an earlier finding.

Official contracts checked **2026-10-24**. All routes are below
`https://ssl.bing.com/webmaster/api.svc/json/`, with the API key supplied privately as `apikey`.
Each method is probed separately on each collection; documentation does not establish live
availability for a particular account.

| Method and official reference | Parameters beyond `apikey` | Evidence and limits |
| --- | --- | --- |
| [GetUserSites](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getusersites?view=bing-webmaster-dotnet) | None | Exact selected URL and verified flag; verification codes discarded. |
| [GetRankAndTrafficStats](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getrankandtrafficstats?view=bing-webmaster-dotnet) | `siteUrl` | Daily impressions/clicks, filtered to the selected period. Microsoft describes traffic across search verticals, not web-only traffic. |
| [GetQueryStats](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getquerystats?view=bing-webmaster-dotnet) | `siteUrl` | Provider top-query records, clicks/impressions and average positions; query text replaced by a stable hash. Weekly updates. |
| [GetPageStats](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getpagestats?view=bing-webmaster-dotnet) | `siteUrl` | Provider top-page records and positions; URL query strings, credentials, fragments and email identifiers removed. Weekly updates. |
| [GetCrawlStats](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getcrawlstats?view=bing-webmaster-dotnet) | `siteUrl` | Daily crawl/status/index/block/error counts; API describes six months of history. |
| [GetCrawlIssues](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getcrawlissues?view=bing-webmaster-dotnet) | `siteUrl` | Current counts by documented issue flag. Fixed issues can remain for days; no URL inventory retained. |
| [GetFeeds](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getfeeds?view=bing-webmaster-dotnet) | `siteUrl` | Current top-level sitemap/feed URLs, status, dates and URL counts; no recursive child-feed fetching. |
| [GetSiteRoles](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getsiteroles?view=bing-webmaster-dotnet) | `siteUrl`, `includeAllSubdomains=false` | Current counts by role/expired flag; emails, delegation and verification codes discarded. |
| [GetBlockedUrls](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getblockedurls?view=bing-webmaster-dotnet) | `siteUrl` | Count of current blocked page/directory entries; no URL inventory retained. |

The `GetBlockedUrls` page's JSON example mistakenly shows the write method `AddBlockedUrl`;
this connector follows the declared **WebGet** signature and refuses that write method.
[Crawl issue flags](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.urlwithcrawlissues.crawlissues?view=bing-webmaster-dotnet)
and [role enum values](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.siteroles.userrole?view=bing-webmaster-dotnet)
are interpreted explicitly; unknown shapes/values fail the affected endpoint.

None of these method signatures offers paging or period parameters. One bounded response is read
per method, then dates are filtered locally (including Microsoft's `/Date(milliseconds-offset)/`
format). The default period is the last 28 complete UTC days; `--days 1..366` or `--start/--end`
uses the framework's complete-day validation. Stored dates retain the provider's offset/date basis.
Top-query/page records are weekly provider samples, not daily totals. Returned dates and top-row
scope are recorded; missing dates never become zero activity. Feeds, issues, roles, verification
and blocked URLs are explicitly current snapshots, regardless of the requested traffic period.

Each response retains at most `max_pages * page_size` rows (500 by default), with truncation
recorded. The connector limits requests to 27 including retries (nine methods, three attempts).
The framework limits responses to 2 MB, I/O to 10 seconds and the read phase to 45 seconds
(40 in the UI). Oversized responses fail explicitly;
there is no undocumented pagination fallback. Bing's small error bodies have an additional
64 KB cap. 429/5xx/timeouts and documented throttling/internal-error faults use the shared
three-attempt bounded backoff. Successful endpoints and processed rows survive other failures.

Microsoft's [JSON fault format](https://learn.microsoft.com/en-us/bingwebmaster/getting-started)
uses HTTP 400 with [ApiErrorCode](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.apierrorcode?view=bing-webmaster-dotnet).
The module-owned transport maps those codes to framework statuses without retaining the message:
invalid key → `credential_expired`; blocked/not-allowed/not-authorised → `permission_required`
with the required ReadOnly site access; missing resource/access → `not_found`; throttling/internal
errors → bounded retries; exhausted throttling → `rate_limited`. Deprecated faults and HTTP
405/410/501 explicitly record `availability=unsupported`, `provider_unavailable`.
HTTP 404 on a method route is recorded as
endpoint unavailable, not proof that the bound site does not exist. Other malformed or rejected
reads are `failed`. Empty successful lists stay `connected`; mixed endpoint results become
`connected_partial` with the affected endpoint names. Shared API-disabled/approval-required
machine-code handling remains available when the provider actually returns those codes.

Confirmed findings cover returned crawl errors, robots-blocked counts, crawl issues, blocking
entries, absent submitted feeds and explicit error/failure feed statuses. Pending/unknown feed
statuses cannot resolve an error finding. Findings describe Bing's observation and never infer
that intentional blocks are wrong or that an empty issue list proves security. Existing public
robots/sitemap findings are referenced rather than duplicated. Bing/GSC comparisons use the
framework's immutable `ctx.connected_evidence` view of the latest collection for this customer's
bound site. They require both Bing and GSC page evidence for a matching period and exact URL
property (or same-host root domain property), filter page rows to the selected Bing site, and
cite both sources. Missing evidence creates no comparison finding. They are labelled
`needs_verification`: different engines, verticals, reporting cadence and top-row coverage do not
establish lost traffic or a
ranking fault. Inaccessible, capped or mismatched evidence cannot support a comparison. AI
receives only endpoint status/row-count/truncation aggregates, not queries, paths or role lists.

Synthetic verification (no live network, real credentials or customer fixtures):

```sh
uv run pytest -q tests/test_bing_wmt.py
uv run pytest -q
uv run ruff check .
uv run ty check ams_assess tests
```

For the authorised 2EC validation, live reading is allowed only if `BING_WMT_API_KEY` already
exists in `~/.config/ams-assess/credentials.env` and a verified `2ectraffic.com` site is returned
by `GetUserSites`. Otherwise stop at tested code. Do not copy or change the main checkout's
`data/customers/**`. No live read was made during implementation: the key name was absent on
2026-10-24.

## WordPress WP-CLI over SSH (`wp_cli`)

This connector reads one explicitly configured installation through the existing connector
framework. It never discovers other installs, requests admin passwords, retrieves `wp-config.php`,
or runs arbitrary commands. No live SSH was used during implementation or in tests.

David must obtain these details from the hosting administrator:

- SSH **hostname, port and username**, with a dedicated key authorised for this installation.
  Supply the usable private key as a local 0600 file; passwords and SSH agents are disabled.
- The **canonical absolute installation path**, for example `/srv/www/example/wordpress`.
  Use the same canonical hostname/path across references; aliases/symlink paths cannot be resolved
  safely by this command allowlist. Spaces, traversal, trailing slashes and shell metacharacters
  are rejected. The customer is explicitly bound to this installation, not all sites on the host.
- The server's **pinned host public key**, independently verified with the host administrator,
  in OpenSSH `known_hosts` format. For a non-default port use `[hostname]:port`. Hashed entries
  are supported by OpenSSH. Do not trust an unverified `ssh-keyscan` result or auto-accept a key.

The local machine needs `/usr/bin/ssh`. The remote POSIX host needs **WP-CLI ≥ 2.6.0**, PHP and GNU `timeout`
in `/usr/local/bin:/usr/bin:/bin`; no tools are installed by this connector. The SSH identity needs
file read/traverse access and WordPress database SELECT access through the installation's existing
configuration. Ask the administrator to enforce least privilege, including filesystem/database
write restrictions where feasible. The connector does not grant permissions or alter configuration.
The minimum follows the [official context configuration history](https://make.wordpress.org/cli/handbook/references/config/#context-configuration):
`--context=cli` was introduced in 2.6.0 and is unavailable in 2.5.x.

Keep the credential JSON, key and pinned-host file directly under `~/.config/ams-assess/`
(0700 directory, 0600 files; no symlinks or hard links). Example setup, using placeholders only:

```sh
install -d -m 700 ~/.config/ams-assess
# Save the authorised private key as wp-2ec-key and verified host entry as wp-2ec-known-hosts.
chmod 600 ~/.config/ams-assess/wp-2ec-key ~/.config/ams-assess/wp-2ec-known-hosts
```

Create `~/.config/ams-assess/wp-2ec.json` privately with exactly these fields; substitute the
administrator-supplied values and your absolute local config-directory paths:

```json
{
  "host": "ssh.example.com",
  "port": 22,
  "user": "assessor",
  "key_path": "/Users/david/.config/ams-assess/wp-2ec-key",
  "known_hosts": "/Users/david/.config/ams-assess/wp-2ec-known-hosts",
  "install_path": "/srv/www/example/wordpress"
}
```

Once hosting access and the bootstrap precautions below are ready, run from this worktree:

```sh
chmod 600 ~/.config/ams-assess/wp-2ec.json
uv run ams-assess connect wp_cli auth --credential-ref wp-2ec \
  --credential-file ~/.config/ams-assess/wp-2ec.json
uv run ams-assess connect wp_cli list-resources --credential-ref wp-2ec
# Copy the returned wp-install:<64 hex digits> resource_id into the next command.
uv run ams-assess connect wp_cli bind --customer 2ec-traffic \
  --credential-ref wp-2ec --resource 'wp-install:<64 hex digits>'
uv run ams-assess connect wp_cli test --customer 2ec-traffic
uv run ams-assess connect wp_cli collect --customer 2ec-traffic --days 28
uv run ams-assess connectors --customer 2ec-traffic
uv run ams-assess report --customer 2ec-traffic
# Collect alongside public checks to reconcile the current run's coverage:
uv run ams-assess run --customer 2ec-traffic --target https://CUSTOMER-WEBSITE \
  --connect wp_cli --no-ai
```

`auth` only registers the private reference. `list-resources` authenticates and reads
`core is-installed` at the **single configured path**; it never scans the server. Its returned ID
hashes the canonical host, port and install path, independently of username/key/reference. Only that
ID and credential reference enter customer bindings. Cross-customer reuse requires the framework's
`--force --reason 'approved non-secret reason'`; changing the credential's host/path without rebinding
fails before SSH. Host-key mismatch fails closed; key renewal/revocation requires administrator action.

All reads use strict host-key checking, BatchMode, explicit identity, no agent/X11/port forwarding,
no password fallback, no SSH user config/proxies/control sockets, a five-second connect timeout and
at most ten seconds per process. Both stdout and stderr are drained under a **combined 2 MB cap**;
timeout/overflow kills and reaps the local process. A remote GNU timeout also terminates the WP-CLI
process (TERM, then KILL after one second). The framework bounds collection to 90 seconds (40 in the
UI), 40 attempts, and at most three attempts per transient connection/timeout failure. Permission,
host-key, malformed-output and command errors are not retried. Completed endpoints/pages survive
later failures and appear as `connected_partial`, with failed endpoint names in CLI/UI/report.
Exit 255 alone does not establish an SSH failure: remote PHP can return it too. Only recognised SSH
transport diagnostics are retried; PHP fatals and ambiguous exit-255 failures are not retried.

The fixed WP-CLI command allowlist was checked against the official references below. Every command
also gets `--path=<validated path> --skip-plugins --skip-themes --skip-packages --context=cli --no-color`.
Only the page integer (1–5) varies within the fixed post-list operations. No user-supplied extra flags,
filters, PHP, shell programs, filenames, post types or statuses are accepted.

| Read and official contract | Output and limits |
| --- | --- |
| [`wp cli info --format=json`](https://developer.wordpress.org/cli/commands/cli/info/) | Retain CLI PHP and WP-CLI versions only; web PHP may differ. Discard binary, ini, home and config paths. |
| [`wp core version --extra`](https://developer.wordpress.org/cli/commands/core/version/) | Parse the documented text version. This command has no JSON format option. |
| [`wp core is-installed`](https://developer.wordpress.org/cli/commands/core/is-installed/) | Exit-code check; success intentionally has no output. It has no JSON format option. |
| [`wp core verify-checksums`](https://developer.wordpress.org/cli/commands/core/verify-checksums/) | Parse fixed success/warning messages into counts. Official docs mark `--format` as not yet in a stable release, so use the section G text command; never install a nightly release or use `--insecure`. |
| [`wp plugin list`](https://developer.wordpress.org/cli/commands/plugin/list/) and [`wp theme list`](https://developer.wordpress.org/cli/commands/theme/list/) | `--format=json --fields=name,status,version,update,update_version,auto_update --skip-update-check`; retain at most 1,000 rows each, flag caps. Null/false/missing optional fields become empty strings; bounded version header text and `update=unavailable` are retained. Unparseable rows are counted and excluded individually, preserving valid rows with partial coverage. |
| [`wp plugin verify-checksums --all --format=json`](https://developer.wordpress.org/cli/commands/plugin/verify-checksums/) | Parse JSON discrepancies and the documented batch summary. A fully passing run may emit only the success message; skipped/unverifiable premium/custom plugins never count as a passing integrity check. |
| [`wp post list`](https://developer.wordpress.org/cli/commands/post/list/) | `--post_type=post` or `page`, `--post_status=publish --format=json --fields=ID,post_type,post_name,post_modified,url --posts_per_page=100 --paged=1..5 --orderby=ID --order=ASC`. At most 500 records per type; cap or page failure makes counts a lower bound. Persist counts/URLs, not titles, content, author, slugs or modified timestamps. |
| [`wp option get <name> --format=json`](https://developer.wordpress.org/cli/commands/option/get/) | Only `blog_public`, `siteurl`, `home`, `permalink_structure`, `blogdescription`, `default_comment_status`, `users_can_register`, `default_role`, `WPLANG`, `timezone_string`. Prose/permalink/timezone options become presence flags; no arbitrary option names or secret fields. |

Every command operates on a **current snapshot**, not historical activity within `--days`; the framework
records that requested reporting period plus actual collection time and a snapshot flag. Only built-in
public `post`/`page` with `publish` status are enumerated. Custom types would require reviewed additions
to the fixed allowlist; loading plugins just to discover them is not permitted. Drafts/private posts,
users, emails, database dumps, `wp eval`, `wp config *`, `wp db *`, `wp shell`, `wp option list` and
update/install/activate/delete/set operations are forbidden. All remote values are validated and
`shlex`-quoted into a constant SSH command scaffold; the local subprocess never uses `shell=True`.

Bootstrap precautions: [WP-CLI's global parameters and configuration](https://make.wordpress.org/cli/handbook/references/config/)
document that `--skip-plugins` **does not skip must-use plugins**. WordPress reads that need bootstrap
(is-installed, plugin/theme lists, plugin checksums, posts and options) necessarily load WordPress's
own configuration, must-use plugins and applicable drop-ins. The connector never reads, transmits,
prints or persists `wp-config.php` or its secret values; normal WordPress bootstrap reads it internally.
Core version/checksums and CLI info execute before WordPress loads.

The remote environment is cleared, starts in `/`, suppresses global/system YAML via `/dev/null`,
disables the WP-CLI cache, sets `WP_CLI_DISABLE_AUTO_CHECK_UPDATE=1` and `DISABLE_WP_CRON=1`, and skips
plugin/theme update checks. Cached update flags can be stale. **An environment variable is not the
PHP `DISABLE_WP_CRON` constant**: this client cannot prove arbitrary site bootstrap code is side-effect
free. Before a production read, the administrator must verify cron is disabled for this CLI context,
that root `/wp-cli.yml` / `/wp-cli.local.yml` cannot add hooks, and that mu-plugins/drop-ins/config/PHP
and SSH startup hooks do not write, send mail, trigger updates or run background tasks. Use a reviewed read-only
snapshot if this cannot be guaranteed. No remote configuration changes or injected PHP are used to
enforce this, and no live access has been attempted.

Findings distinguish **update available — not a confirmed vulnerability**, inactive plugins,
published-checksum integrity discrepancies (not proof of malware), search discouragement and registration
with its default role. Unknown/empty diagnostics never imply "secure". No fresh update check is performed,
so a cached negative update flag cannot resolve an earlier update finding. Inaccessible or partial
checks cannot resolve findings; an equivalent complete passing recheck can. Existing same-site public
noindex findings are referenced by ID/evidence rather than duplicated.
Valid rows within a partial plugin list can still establish update/inactive findings; an incomplete
list cannot establish absence or resolve an earlier inactive-plugin finding.

During `run --connect wp_cli`, the additive coverage hook consumes only that run's bound inventory
evidence, revalidates same-origin URLs, retains successful partial pages and marks source **`wp_cli`**.
It adds **known URLs only**, alongside REST/manual-export inputs, never crawled/browser-tested pages.
Prior customer evidence and other installations cannot be substituted. A standalone `collect` stores
inventory evidence and comparisons without claiming to have performed a public crawl. Query secrets,
URL credentials, personal email paths and absolute server paths are excluded; numeric `?p=`/`?page_id=`
public permalinks are retained. AI receives the framework's aggregate status/row coverage only.
URL hosts must be syntactically valid DNS names (including IDNA); address literals are excluded.
Valid percent-encoded international permalinks are accepted after validating the decoded text.
C0/DEL/C1 controls and Unicode format/bidi characters, including encoded forms, are rejected before
evidence is produced. The shared privacy layer may store accepted international paths as Unicode.

## Bound connector: Google Business Profile (`gbp`)

GBP uses `https://www.googleapis.com/auth/business.manage`. Google provides no narrower read scope
for these endpoints. The connector permits only the six documented GET routes below; all POST,
PUT, PATCH, DELETE and unlisted routes are refused before HTTP. OAuth consent/token refresh use
the shared Google authentication helper. The connector never edits a profile, replies to a review,
enables an API, requests approval, or calls a paid service.

External prerequisites are manual and must be completed before a live read:

- **Project approval:** apply for Basic API Access using the project's number and an email listed
  as a GBP owner/manager. Google's current eligibility requirements include managing a verified
  profile active for at least 60 days and a business website on that profile. A zero QPM allocation
  means approval has not been granted; apply for access rather than requesting a quota increase.
  See [prerequisites](https://developers.google.com/my-business/content/prereqs) and
  [quota limits](https://developers.google.com/my-business/content/limits).
- **API enablement:** these reads use **My Business Account Management API**, **My Business Business
  Information API**, **Google My Business API** (legacy v4 reviews), and **Business Profile Performance
  API**. Google's [general setup](https://developers.google.com/my-business/content/basic-setup)
  additionally lists Lodging, Place Actions, Notifications and Verifications among its setup APIs;
  this connector does not call them. The legacy Google My Business API is visible after access
  approval. If using a Workspace account, Google Business Profile must also be enabled for that user.
- **Profile access:** the OAuth user needs **Manager** access to the explicitly selected profile;
  existing Owner access also works, but ownership transfer is unnecessary. Reviews require a
  verified location. [Role capabilities](https://support.google.com/business/answer/3403100)
  include reading insights and reviews. A Google Cloud IAM project role alone is not GBP access.
- **OAuth:** configure the consent screen/test user and a Desktop app client in the approved project.
  Keep the client JSON and generated token in `~/.config/ams-assess` (directory 0700, files 0600).
  Authorisation is separate from approval and from location binding.

No live GBP read has been performed for this implementation; credentials, project approval/API
enablement and verified resource access have not been established. All GBP tests use synthetic
HTTP responses. These commands are for David to run only after those prerequisites and explicit
resource authorisation are satisfied:

```sh
cd /Users/david/dev/builds/p1-ai-automation/ams-assessment-engine-worktrees/conn-gbp
uv run ams-assess connect gbp auth --credential-ref gbp-2ec \
  --client-secrets ~/.config/ams-assess/google-desktop-client.json
uv run ams-assess connect gbp list-resources --credential-ref gbp-2ec
# Inspect the returned id, label, address and metadata before binding.
# Replace the placeholder with the actual API location ID returned by Google.
GBP_LOCATION='locations/REPLACE_WITH_API_LOCATION_ID'
uv run ams-assess connect gbp bind --customer 2ec-traffic \
  --credential-ref gbp-2ec --resource "$GBP_LOCATION"
uv run ams-assess connect gbp test --customer 2ec-traffic
uv run ams-assess connect gbp collect --customer 2ec-traffic \
  --start 2026-09-01 --end 2026-09-30
uv run ams-assess connectors --customer 2ec-traffic
uv run ams-assess report --customer 2ec-traffic
```

For the authorised Kirrawee location, verify the API-returned `metadata.placeId` against
`ChIJ2SXszLy5EmsR6egdy8Tm97U` and, when supplied in `mapsUri`, `metadata.maps_cid` against
`13112202573008201961`, along with the location title/address. Those identifiers come from the task
specification; they have **not** been verified live. Neither is the API location ID. Never substitute
a CID/place ID, guess an API ID, or bind based only on a similar business name. If metadata cannot
establish the match, stop for verification. `test` checks the exact bound API identity and records its
metadata again. Listing discovers choices and does not assess or bind them.

One canonical `locations/<API ID>` is bound per customer. Another location requires an explicit rebind;
earlier evidence is retained. Canonical IDs prevent the same location bypassing customer isolation
through a different account parent. Cross-customer reuse requires `--force --reason '...'`. During
collection, bounded account/location-name discovery resolves the parent needed by the reviews API;
only the selected location receives profile, review and performance analysis. Missing or incomplete
discovery is recorded and does not become a claim of no account or no reviews.

The connector declares `auth_flow='client_secrets'` and a GBP authorisation hint pointing to the
private Desktop client JSON. It uses the framework's immutable current profile/evidence context
for both standalone collection and `run --connect gbp`. Website comparisons use the latest scoped
`ctx.public_evidence` NAP observations. `ctx.connected_evidence` is empty for the opaque
`locations/<id>` binding: a profile website does not authorise comparisons with another connector's
site data. GBP test/collect preserves public findings, their lifecycle history, evidence and AI
recommendations; only GBP findings are rechecked by the connector operation.

The error hook reports `approval_required` for a specific approval reason or a 403/429 with an
explicit zero quota, with the Basic API Access application as the next
action. Ordinary throttling remains `rate_limited` after bounded retries; an ambiguous 403 remains
`permission_required` with the Manager role and scope requirements. Disabled APIs, expired/revoked
credentials and missing resources retain distinct statuses. An inaccessible endpoint never becomes
an absence finding. No live approval check or access application is performed automatically.

The usual period is 28 complete UTC days (`--days 1..366` or explicit `--start`/`--end`). Daily metrics
cover impressions by Search/Maps and desktop/mobile, call-button clicks, website clicks, direction
requests and Reserve with Google bookings where returned. Call clicks are not completed calls or
leads. Missing metrics/dates are unavailable, not zeros. An explicitly returned zero is retained.
Monthly keywords use only complete calendar months inside that daily period. A period with no full
month produces an explicit coverage limitation and no keyword request; select a full month as above.
Keyword thresholds remain upper bounds, separate from exact counts. Profile fields and reviews are
current snapshots, not historical data filtered by the performance period; evidence says so.

Account lists use at most five 20-row pages, location lists five 100-row pages per account, reviews
five 50-row pages and monthly keywords five 100-row pages. Resource listing additionally caps its
output at 500 locations. GBP explicitly sets `max_requests=80` and `collection_seconds=45`, sharing
that budget across discovery, reviews and performance, including retries. The UI caps it at 40 seconds;
caller limits can only reduce it. Requests have 10-second I/O timeouts and 2 MB response caps.
Repeated page tokens stop pagination. Completed pages/endpoints survive subsequent failures;
truncation and unavailable
endpoints are shown in CLI, UI and report section 7. Review stars/reply counts describe only the
retrieved sample, while Google's total count/average rating are separately identified.

Only review aggregates are retained: no reviewer name, photo, review ID, review text or reply text.
Keyword emails are hashed, URL credentials and arbitrary queries are removed, and Maps CID is kept
only as a numeric identity value. The shared AI projection exposes coverage aggregates only.
Profile comparisons confirm differences against the supplied profile; they do not verify that the
customer-supplied value is correct. Area labels may describe overlapping geography and require human
interpretation. Public website observations retain references to their existing findings instead of
duplicating them. Missing evidence cannot resolve an earlier finding.

Official endpoint and scope contracts (checked 2026-10-24):

| Read | Official reference |
| --- | --- |
| `GET mybusinessaccountmanagement.googleapis.com/v1/accounts` | [accounts.list](https://developers.google.com/my-business/reference/accountmanagement/rest/v1/accounts/list) |
| `GET mybusinessbusinessinformation.googleapis.com/v1/accounts/{id}/locations` | [locations.list](https://developers.google.com/my-business/reference/businessinformation/rest/v1/accounts.locations/list) |
| `GET mybusinessbusinessinformation.googleapis.com/v1/locations/{id}` | [locations.get](https://developers.google.com/my-business/reference/businessinformation/rest/v1/locations/get) |
| `GET mybusiness.googleapis.com/v4/accounts/{id}/locations/{id}/reviews` | [reviews.list](https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews/list) |
| `GET businessprofileperformance.googleapis.com/v1/locations/{id}:fetchMultiDailyMetricsTimeSeries` | [daily metrics](https://developers.google.com/my-business/reference/performance/rest/v1/locations/fetchMultiDailyMetricsTimeSeries) |
| `GET businessprofileperformance.googleapis.com/v1/locations/{id}/searchkeywords/impressions/monthly` | [monthly keywords](https://developers.google.com/my-business/reference/performance/rest/v1/locations.searchkeywords.impressions.monthly/list) |

Business Information uses `readMask=name,title,categories,regularHours,specialHours,websiteUri,
phoneNumbers,storefrontAddress,serviceArea,profile.description,metadata`; review-parent discovery
requests `name` only. See [location fields and metadata](https://developers.google.com/my-business/reference/businessinformation/rest/v1/accounts.locations)
and [daily metric definitions](https://developers.google.com/my-business/reference/performance/rest/v1/DailyMetric).
Google's [time-series contract](https://developers.google.com/my-business/reference/performance/rest/v1/TimeSeries)
defines an omitted numeric value on an explicitly returned date as zero; an absent date is not zero.
Each endpoint reference specifies `business.manage`; no legacy `plus.business.manage` scope is requested.

Synthetic verification, from this worktree:

```sh
uv run pytest -q tests/test_gbp.py
uv run pytest -q
uv run ruff check .
uv run ty check ams_assess tests
```

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
Site Health, Workspace/M365, social SEO, PR, A/B and heatmaps); these future capabilities are not implemented or measured. GA4 and WP-CLI are available through explicit binding. The legacy combined "WP-CLI / Site Health" opportunity label does not indicate the bound connector's status; use the connector table.

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

The test harness clears operator credentials and blocks external network access, even if
`AMS_LIVE_SMOKE` was present in the invoking shell. The retained legacy public-HTTPS smoke
test is therefore skipped. Exercise actual sites separately only with explicit authorisation;
fixture results do not establish that every customer host is reachable.
SiteOne transport statuses such as `-4:SEND-ERROR` and its fatal error appear in the module reason
and evidence; its 2.5.1 binary does not expose the underlying request exception. Inspect the
run's `raw/siteone.json` and `raw/target-resolution.json` when diagnosing a failed crawl.
SiteOne, redirect preflight and normal site-check probes identify as `AMSAssessment/1.0`.
SiteOne's `--user-agent=AMSAssessment/1.0!` preserves that exact identity; browser-shaped
crawler identities can trigger server/edge rejection even when TLS verification succeeds.
Redirects retain the requested target for domain/email checks and store identity; website
collectors use the separately recorded `resolved_url`. A preflight timeout or HTTP rejection
is a limitation and still allows SiteOne to try the last safe URL. Unsafe redirects stop it.

## Credential and AI boundary

See [DATA-FLOWS.md](docs/DATA-FLOWS.md) for outgoing fields/providers, explicit
`AMS_AI_PROVIDER` selection (default `none`), per-customer `no_ai`, process isolation,
private artefact retention, regression coverage and residual limitations.

The offline security tests require Chromium binaries under `tools/playwright` (no browser
profiles). On a machine where downloads are permitted, prepare them with
`PLAYWRIGHT_BROWSERS_PATH="$PWD/tools/playwright" uv run playwright install chromium`.
Tests never download tools or contact providers; optional SiteOne/axe integration tests
remain skipped when those tools are absent.
