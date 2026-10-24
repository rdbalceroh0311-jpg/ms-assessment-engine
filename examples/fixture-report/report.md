> Draft — AI-assisted analysis requires AMS human review before it is sent to the customer

# AI Managed Services
## Business assessment report

**Customer:** Harbour Demo Plumbing<br>
**Assessment date (UTC):** 2026-10-23<br>
**Target:** http://127.0.0.1:18765/<br>
**Scope:** Functionality & accessibility; Copywriting & messaging; CRO & UX/UI; Domain, DNS & email health; Entity & brand authority; GEO / AI search readiness; Local SEO; Website performance; Digital presence & integration readiness; Security observations; SEO findings; Website technical health; Business discovery (customer statements)<br>
**Stages completed:** public technical assessment; business discovery
> Evidence-led observations and a proposed improvement plan. Confirm scope, priorities and estimates with AMS before implementation. This assessment makes no changes to your website.

## 1. Executive summary and business context

This report connects the evidence to practical decisions about your business. A finding is an observation or a customer statement; a recommendation is a proposed next step for review.

**Business context — Reported by customer:** Harbour Demo Plumbing. Industry: Trades & home services (incl. painting). Buying model: Service-area (staff travel to customers). Service area: Sydney. Evidence: ev-profile-r1-001.


The assessment recorded 20 findings supported by collected evidence or customer statements, presented as 20 issues below. Repeated page/viewport observations of the same rule share one recommendation; their individual IDs and evidence remain listed. Unavailable checks remain listed as limitations and must not be interpreted as a clean bill of health.

> No ranking, traffic, lead or revenue guarantees. AI-search readiness is not measured AI visibility. This technical assessment is a limited, point-in-time sample, not a full security, accessibility or operational audit. Potential impacts and likely causes require verification.

Basis labels: **Confirmed (technical evidence)** means directly observed; **Manually verified** means a person's recorded observation, not a technical measurement or proof of account ownership; **Reported by customer** is not independently verified; **Inferred (heuristic or AI, needs verification)** is provisional; **Not assessed** means no usable evidence was obtained. Optional features and unavailable integrations are not failures.

## 2. Scope, sources, dates and coverage limitations

The table explains what ran and where evidence was unavailable. Results apply only to the website, date and sample shown; they do not establish account ownership or complete coverage.

**Standard requirements:** Where a finding refers to these requirements, its dependencies are Customer approval of scope and an accountable system owner before changes. Information still required: Confirm the current setup, existing integrations, access and business priority. Finding-specific exceptions are stated with each issue.

| Website / run | Date (UTC) | Successful HTML pages / crawl URL budget | Run outcome |
| --- | --- | --- | --- |
| http://127.0.0.1:18765/ (r2) | 2026-10-23 | 7 / 30 (static assets excluded) | partial |



| Source / website / date | Status | Coverage or limitation |
| --- | --- | --- |
| siteone (http://127.0.0.1:18765/)<br>2026-10-23 | Assessed | Completed within the stated scope |
| browser (http://127.0.0.1:18765/)<br>2026-10-23 | Assessed | Completed within the stated scope |
| domain\_email (http://127.0.0.1:18765/)<br>2026-10-23 | Not assessed | Local/private target has no public domain audit |
| security (http://127.0.0.1:18765/)<br>2026-10-23 | Assessed | TLS not checked: supplied target uses HTTP; Local fixture security headers are not evidence about production hosting |
| performance (http://127.0.0.1:18765/)<br>2026-10-23 | Not assessed | PageSpeed/CrUX cannot access localhost |
| observatory (http://127.0.0.1:18765/)<br>2026-10-23 | Not assessed | Observatory cannot scan local targets |
| dataforseo (http://127.0.0.1:18765/)<br>2026-10-23 | Not assessed | Missing DataForSEO credentials; disabled |
| gsc (http://127.0.0.1:18765/)<br>2026-10-23 | Not assessed | GSC requires --connect gsc and credentials; missing connection or credentials |
| presence (http://127.0.0.1:18765/)<br>2026-10-23 | Assessed | Completed within the stated scope |
| site\_checks (http://127.0.0.1:18765/)<br>2026-10-23 | Assessed | 2 of 15 checks NOT ASSESSED; see status rows for missing evidence and manual verification |
| profile (r1)<br>2026-10-23 | Recorded | Customer-supplied information or manual observation; not independent technical verification. See evidence index. |
| discovery (r3)<br>2026-10-23 | Recorded | Customer-supplied information or manual observation; not independent technical verification. See evidence index. |

- Crawl coverage (http://127.0.0.1:18765/): 7 successful HTML pages within a crawl budget of 30 URLs (static assets excluded; redirects and failed/non-HTML responses can consume slots).
- Browser coverage: 12 page/viewport observations across the included runs, with at most six key pages per run at desktop and mobile sizes. Form structure was inspected. Automated accessibility checks are not a full accessibility audit.
- Observations are a point-in-time sample. Form submission, booking, payment and account journeys were not exercised.
- AI search visibility was **NOT measured**. GEO findings concern content structure, answer readiness and crawlability only. No A/B testing or behavioural data was collected.
- AI analysis: skipped. AI disabled (--no-ai)
- domain\_email: no successful evidence collection; category was not assessed
- DNS, authentication and blacklist checks do not measure inbox placement; they cannot guarantee email deliverability.
- Local fixture demonstration: observations do not describe a live business
- AI interpretation is unavailable for Copywriting & messaging, CRO & UX/UI, Entity & brand authority, GEO / AI search readiness, Local SEO; collect or review page evidence manually

## 3. Critical findings and business risks

Review these higher-importance observations first. Their business impact is potential, not a measured loss; the full finding explains the action and dependencies.

| Finding | Basis / source | Potential risk | Urgency |
| --- | --- | --- | --- |
| Elements must meet minimum color contrast ratio thresholds (browser-001) | Confirmed (technical evidence); browser (ev-browser-r2-003); browser (ev-browser-r2-009) | Low contrast may make service information or controls hard to read. | Review within 0-30 days |
| Images must have alternative text (browser-002) | Confirmed (technical evidence); browser (ev-browser-r2-003); browser (ev-browser-r2-009) | Customers using screen readers may miss information conveyed by images. | Review within 0-30 days |
| Missed calls and slow enquiry replies (discovery-missed-calls-and-slow-enquiry-replies) | Reported by customer; discovery (ev-discovery-r3-005); discovery (ev-discovery-r3-006); discovery (ev-discovery-r3-017) | Customer believes some new work goes to competitors (not measured) | Review within 0-30 days |
| Broken URLs (siteone-002) | Confirmed (technical evidence); siteone (ev-siteone-r2-010) | Broken links may prevent customers reaching the information they need. | Review within 0-30 days |

## 4. Website, SEO, performance, accessibility and AI-search readiness

These checks look at whether customers and search engines can reach and understand the sampled pages. SEO means search engine optimisation; AI-search readiness concerns content and crawler access, not measured appearances in AI answers.

### Website technical health

#### siteone-002: Broken URLs

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** high | **Priority:** P1 | **Urgency:** Review within 0-30 days | **Type:** problem

**Observed / reported:** HTTP 404 at http://127.0.0.1:18765/missing-offer.html.

**Potential business impact (interpretation, not measured):** Broken links may prevent customers reaching the information they need.

**Recommended next action:** Repair internal links and restore or redirect unavailable pages.

**AMS service category:** Managed IT & Technology
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/missing-offer.html

**Source:** siteone (ev-siteone-r2-010).
#### siteone-001: Review internal redirects

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** opportunity

**Observed / reported:** HTTP 301 at http://127.0.0.1:18765/old-service. HTTP 301 at http://127.0.0.1:18765/older-service.

**Potential business impact (interpretation, not measured):** May affect reliability or protection of business systems; impact requires verification.

**Recommended next action:** Link directly to the final canonical URL.

**AMS service category:** Managed IT & Technology
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/old-service; http://127.0.0.1:18765/older-service

**Source:** siteone (ev-siteone-r2-009); siteone (ev-siteone-r2-011).

### SEO findings

#### siteone-004: Missing meta description

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** medium | **Priority:** P2 | **Urgency:** Plan within 30-90 days | **Type:** problem

**Observed / reported:** No meta description recorded at http://127.0.0.1:18765/quote.html.

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Add a descriptive, page-specific meta description.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Source:** siteone (ev-siteone-r2-015).
#### siteone-005: Missing H1

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** medium | **Priority:** P2 | **Urgency:** Plan within 30-90 days | **Type:** problem

**Observed / reported:** No H1 recorded at http://127.0.0.1:18765/quote.html.

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Add a descriptive, page-specific H1.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Source:** siteone (ev-siteone-r2-015).
#### site\_checks-002: Duplicates, headings / internal links

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** problem

**Observed / reported:** Duplicate title groups: 1; duplicate description groups: 1; heading issues: 1; broken internal links: 2; unavailable links: 0; internal redirects: 2. Inbound links: NOT ASSESSED (incomplete sampled link graph).

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Use distinct titles/descriptions, fix heading jumps and broken links, link directly to final targets. Retry unavailable links before classifying them. Verify possible orphan pages with a complete link graph.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** site\_checks (ev-site_checks-r2-007).
#### siteone-010: Pages without a canonical link

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** opportunity

**Observed / reported:** 4 page(s) without a rel=canonical link

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Add a self-referencing rel=canonical to each indexable page.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/

**Source:** siteone (ev-siteone-r2-024).

### GEO / AI search readiness

#### site\_checks-001: AI crawler rules by purpose

**Basis:** Inferred (heuristic or AI, needs verification). **Verification status:** needs verification.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** problem

**Observed / reported:** Search/user crawlers blocked on sampled paths: ChatGPT-User

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Review search/user restrictions with the customer; retain intentional training choices. User-initiated agents may not obey robots rules; verify the journey manually.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** site\_checks (ev-site_checks-r2-003).


### Functionality & accessibility

#### browser-001: Elements must meet minimum color contrast ratio thresholds

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** high | **Priority:** P1 | **Urgency:** Review within 0-30 days | **Type:** problem

**Observed / reported:** desktop: 1 affected elements at http://127.0.0.1:18765/quote.html (axe color-contrast). mobile: 1 affected elements at http://127.0.0.1:18765/quote.html (axe color-contrast).

**Potential business impact (interpretation, not measured):** Low contrast may make service information or controls hard to read.

**Recommended next action:** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/color-contrast?application=axeAPI and verify with keyboard and assistive technology.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Source:** browser (ev-browser-r2-003); browser (ev-browser-r2-009).
#### browser-002: Images must have alternative text

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** high | **Priority:** P1 | **Urgency:** Review within 0-30 days | **Type:** problem

**Observed / reported:** desktop: 1 affected elements at http://127.0.0.1:18765/quote.html (axe image-alt). mobile: 1 affected elements at http://127.0.0.1:18765/quote.html (axe image-alt).

**Potential business impact (interpretation, not measured):** Customers using screen readers may miss information conveyed by images.

**Recommended next action:** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/image-alt?application=axeAPI and verify with keyboard and assistive technology.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Source:** browser (ev-browser-r2-003); browser (ev-browser-r2-009).



### Website checks: status and evidence

Each row describes the sampled evidence. NOT ASSESSED means evidence is unavailable or incomplete. Optional features are informational. Form submissions are always **NOT TESTED**. AI-search readiness is not measured visibility.

Checks with findings are described in their finding blocks; the remaining checks are listed here.

- **Robots rules / site-wide Disallow — PASS** (ev-site_checks-r2-002; http://127.0.0.1:18765/). Checked: Parsed 4 groups; root and representative public path allowed for \*, Googlebot and Bingbot. Why it matters: Broad rules can prevent search engines crawling public pages. Recommended correction: Confirm intended visibility, remove unintended broad Disallow rules, then retest.
- **Page noindex / X-Robots-Tag — PASS** (ev-site_checks-r2-004; http://127.0.0.1:18765/). Checked: No noindex in 7 crawl indexing records. Meta directives inspected where available; individual HTTP-header attribution may be unavailable outside the homepage. Why it matters: Unintended noindex on home or key pages prevents those pages appearing in search. Recommended correction: Confirm intended indexing per URL; inspect response headers on unsampled pages. Remove noindex only on pages intended for search.
- **Sitemap URL validity — PASS** (ev-site_checks-r2-005; http://127.0.0.1:18765/). Checked: Compared 7 sitemap URLs with existing crawl results: 0 with observed issues, 0 uncrawled/unavailable (NOT ASSESSED). Why it matters: Sitemaps should list final, successful, indexable URLs; uncrawled URLs are unknown. Recommended correction: Replace redirects/non-200/noindex URLs; manually validate uncrawled entries. Use absolute HTTP(S) sitemap URLs in production.
- **Canonical accessibility / consistency — PASS** (ev-site_checks-r2-006; http://127.0.0.1:18765/). Checked: 3 declarations; 0 need review; 0 targets NOT ASSESSED. http://127.0.0.1:18765/ -\> http://127.0.0.1:18765/: HTTP 200 ; http://127.0.0.1:18765/service/a/ -\> http://127.0.0.1:18765/service/a/: HTTP 200 ; http://127.0.0.1:18765/service/b/ -\> http://127.0.0.1:18765/service/b/: HTTP 200 Why it matters: Canonical hints should consistently identify accessible, indexable preferred pages. Recommended correction: Review conflicting targets; use final indexable URLs. Manually verify uncrawled targets.
- **Organization / LocalBusiness / BreadcrumbList — INFO** (ev-site_checks-r2-008; http://127.0.0.1:18765/). Checked: Validated 1 supported JSON-LD nodes. No invalid supported properties found. 4 applicable-property suggestions; absence is not a failure. Why it matters: Consistent structured data clarifies entities; it does not guarantee rich results. Recommended correction: Fix invalid JSON-LD and required properties; review applicable recommended properties with Google's Rich Results Test. Organization has no required properties.
- **Core Web Vitals (field p75) — NOT ASSESSED** (ev-site_checks-r2-010; http://127.0.0.1:18765/). Checked: No usable CrUX/PSI p75 field data. Lighthouse lab scores are not CWV. Why it matters: Field LCP, INP and CLS describe real-user experience; origin and URL scopes differ. Recommended correction: Investigate non-good field metrics and verify all three at p75. Good: LCP \<=2500 ms, INP \<=200 ms, CLS \<=0.1. Poor: LCP \>4000 ms, INP \>500 ms, CLS \>0.25. Missing field data requires manual verification; preserve separate lab findings.
- **Booking / CRM / chat tools — INFO** (ev-site_checks-r2-011; http://127.0.0.1:18765/). Checked: Detected markup signals: Calendly. Script/link/iframe URLs and recognised inline API calls; prose excluded. Iframes outside the normal-UA homepage probe may be unavailable. Why it matters: Public integration signals help identify systems to discuss; they do not prove account existence, ownership or working integration. Recommended correction: Confirm customer-owned systems and test authorised journeys before proposing changes.
- **Conversion pathways / forms NOT TESTED — INFO** (ev-site_checks-r2-012; http://127.0.0.1:18765/). Checked: 6 rendered pages inspected for forms, booking links, tel/mailto, CTA wording, trust signals and case-study/portfolio/team/about links. Above-the-fold placement NOT ASSESSED where positions are unavailable. Form submissions: NOT TESTED. Trust claims and journey outcomes are unverified. Why it matters: Visible enquiry options help plan journey validation; their presence does not prove forms, booking, lead delivery or conversion performance work. Recommended correction: Review missing paths and CTA prominence manually; arrange an authorised safe journey test with the customer. No submissions were made.
- **Analytics / conversion-event code signals — NOT ASSESSED** (ev-site_checks-r2-013; http://127.0.0.1:18765/). Checked: Unknown: consent manager/gated scripts prevent confirming execution. Analytics/advertising tags: ga4, gtm, clarity; visible event calls: 2. Events are not proof of measured conversions. Why it matters: An event call can indicate measurement intent; consent, delivery and key-event configuration need separate verification. Recommended correction: Use customer-authorised tag diagnostics and analytics access to verify post-consent events and conversion configuration.
- **Suspected CDN / firewall crawler blocking — PASS** (ev-site_checks-r2-014; http://127.0.0.1:18765/). Checked: Normal UA HTTP 200; AI-search UA HTTP 200. No differential HTTP block observed in this pair. Why it matters: A differing response may indicate a CDN/firewall policy, rate limit or transient failure; spoofing a UA does not reproduce a verified crawler IP. Recommended correction: Review CDN/server logs and allow intentional search access after customer approval. Verify with the real crawler; do not bypass challenges.
- **llms.txt (informational) — INFO** (ev-site_checks-r2-015; http://127.0.0.1:18765/). Checked: llms.txt present; HTTP 200. One bounded fetch; HTML fallbacks, redirects, empty or incomplete responses are not confirmed files. Why it matters: llms.txt is an optional informational convention, not an indexing requirement or evidence of ranking or AI-search visibility. Recommended correction: No correction required for absence. Review accuracy if choosing to publish this file.
- **Answer readiness (heuristic) — INFO** (ev-site_checks-r2-016; http://127.0.0.1:18765/). Checked: 6 pages checked for FAQPage/QAPage, question headings, FAQ sections, and service/location/contact statements. Signals are heuristic; clarity, correctness and AI-search visibility are not established. Why it matters: Explicit answers and business context can make content easier to interpret. Recommended correction: Review accurate answers, service scope, locations and contact details with the customer; do not add unsupported schema or claim AI-search rankings.

Crawler-policy detail (training restrictions are customer choices):

Website: http://127.0.0.1:18765/ (ev-site_checks-r2-003).

| Crawler | Purpose | Status | Blocked sampled paths |
| --- | --- | --- | --- |
| OAI-SearchBot | search | PASS | - |
| Claude-SearchBot | search | PASS | - |
| PerplexityBot | search | PASS | - |
| Googlebot | search | PASS | - |
| Bingbot | search | PASS | - |
| GPTBot | training | INFO | /; /about.html; /contact.html; /quote.html; /service/a/; /service/b/; /services.html |
| ClaudeBot | training | PASS | - |
| Google-Extended | training | PASS | - |
| CCBot | training | PASS | - |
| Applebot-Extended | training | PASS | - |
| meta-externalagent | training | PASS | - |
| Bytespider | training | PASS | - |
| ChatGPT-User | user-initiated | WARNING | /quote.html |
| Claude-User | user-initiated | PASS | - |
| Perplexity-User | user-initiated | PASS | - |

The probes use at most two homepage GETs and one llms.txt GET, with no redirects, a 64 KiB raw body cap per response, identity encoding and a 3-second request timeout capped by the remaining 10-second shared deadline. Robots/sitemap data reuses the presence collector. No canonical or sitemap target URLs are fetched by these checks. Sample limits, missing header attribution, iframe coverage, CTA positions and incomplete link graphs remain explicit; no forms are submitted.


## 5. Digital presence: maps, social, directories and industry channels

These observations help you confirm where customers can find the business. A likely profile match still needs customer verification; missing search results do not prove that an account or listing does not exist.

### Local SEO

#### presence-002: Business name, address or phone differs between structured data and the page

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** medium | **Priority:** P2 | **Urgency:** Plan within 30-90 days | **Type:** problem

**Observed / reported:** on http://127.0.0.1:18765/, structured telephone '+61 2 5550 0199' differs from the only value shown (02 5550 0100).

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Use one canonical name, address and phone number in structured data, the footer, contact page and external listings.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** presence (ev-presence-r2-010).
#### site\_checks-003: Near-duplicate location pages

**Basis:** Inferred (heuristic or AI, needs verification). **Verification status:** needs verification.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** problem

**Observed / reported:** Compared 2 location-like URLs with \>=80 sampled words using 5-word shingle Jaccard \>=0.8: 1 similar pairs. Heuristic only; shared navigation and truncated text can influence similarity.

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Manually compare unique local value and remove shared boilerplate before deciding whether to improve or consolidate pages.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** site\_checks (ev-site_checks-r2-009).

### Entity & brand authority

#### presence-004: Linked profiles missing from structured-data sameAs

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** opportunity

**Observed / reported:** The website links to Google Business Profile / Google Maps, Yellow Pages AU but the business JSON-LD does not list them in sameAs.

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Add the customer-owned profile URLs to the business sameAs property.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** presence (ev-presence-r2-006); presence (ev-presence-r2-009); presence (ev-presence-r2-010).



### Search & local presence signals

Website: http://127.0.0.1:18765/ (run r2). Public assessment: statuses below come from the sampled pages (6), the crawl, public DNS and one bounded robots.txt/sitemap fetch.

> Not detected means no public signal on the pages sampled; it does not mean an account does not exist. Tags may load through Google Tag Manager or only after cookie consent.

> The listings table records only profiles the website links to (links or JSON-LD sameAs). Ownership, verification and management are not confirmed. Platform pages are never fetched; independent search results appear in the platform discovery table.

> Google Business Profile existence is looked up only through the DataForSEO Google Maps SERP when a USD cap is set; otherwise its status needs manual verification.

> Readiness rows describe what authorised access would add; only existing integrations (Search Console) can be connected by this engine.


| Signal | Status | Detail | Evidence |
| --- | --- | --- | --- |
| NAP consistency (structured data vs visible text and phone links) | Inconsistent | on http://127.0.0.1:18765/, structured telephone '+61 2 5550 0199' differs from the only value shown (02 5550 0100) | ev-presence-r2-010 |
| Structured business entity (JSON-LD) | Detected | LocalBusiness; sameAs links: 2 | ev-presence-r2-010 |
| Location / service-area pages | Not detected | No location or service-area URL among 7 crawled pages | ev-presence-r2-011 |
| robots.txt | Detected | Loaded robots.txt for domain '127.0.0.1': status code 200, size 296 B and took 2 ms. | ev-siteone-r2-023 |
| Canonical links | Incomplete | 4 page(s) without a rel=canonical link | ev-siteone-r2-024 |
| XML sitemap | PASS | Sitemap at http://127.0.0.1:18765/sitemap.xml (robots.txt Sitemap line) lists 7 URLs | ev-presence-r2-012 |



### Business listings linked from the website

- **Google Business Profile / Google Maps — Detected (linked from website)**. Source: crawl external link, website link. Evidence URL: https://g.page/harbour-demo-plumbing-fixture. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-006.
- **Bing Places / Bing Maps — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.
- **Apple Business / Apple Maps — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.
- **Facebook — Detected (linked from website)**. Source: JSON-LD sameAs, crawl external link, website link. Evidence URL: https://www.facebook.com/harbour.demo.plumbing.fixture. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-007.
- **Instagram — Detected (linked from website)**. Source: JSON-LD sameAs. Evidence URL: https://www.instagram.com/harbour.demo.plumbing.fixture. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-008.
- **LinkedIn — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.
- **Yellow Pages AU — Detected (linked from website)**. Source: crawl external link, website link. Evidence URL: https://www.yellowpages.com.au/nsw/sydney/harbour-demo-plumbing-fixture. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-009.
- **True Local — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.
- **hipages — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.
- **ServiceSeeking — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.
- **Word of Mouth — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.
- **Houzz — Not detected**. Source: -. Evidence URL: -. Ownership: Ownership confirmation needed. Evidence: ev-presence-r2-001.

### Platform presence discovery

Independent search: **Not assessed** (no --dataforseo-max-usd cap was passed). Business searched: Harbour Demo Plumbing (source: customer profile (customer statement)); industry: Trades & home services (incl. painting); buying model: Service-area (staff travel to customers). Paid search calls: 0 of at most 12.

**Google Business Profile eligibility:** Service-area business: eligible only if staff genuinely travel to customers; hide the street address and set the real service areas. Never invent addresses, use virtual offices or create per-suburb profiles; one profile per genuine location.

> Not found in searches describes only the searches listed; it does not mean a profile does not exist.

> Found states describe a likely or possible match from linked or search-result data. Ownership, management and platform verification are never confirmed by this engine.

> Platform pages are not fetched or scraped. Search results come from the DataForSEO Google SERP and Google Maps APIs, only when a USD cap was passed for the run.

> Keyword positions are point-in-time samples for one location; they are not a ranking guarantee and AI-search readiness is not AI-search visibility.

| Platform / relevance / state | Candidate profiles, match evidence and detail |
| --- | --- |
| Google Business Profile / Google Maps (high)<br>Found — likely match | https://g.page/harbour-demo-plumbing-fixture [linked from the website]; 1 candidate profile(s); linked from the website. Searched through the DataForSEO Google Maps SERP only. Evidence: ev-presence-r2-015 |
| hipages (high)<br>Not assessed | Not searched: no --dataforseo-max-usd cap was passed Evidence: ev-presence-r2-014 |
| Bing Places / Bing Maps (medium)<br>Not assessed | Not searched: no --dataforseo-max-usd cap was passed. No provider search; manual verification only. Evidence: ev-presence-r2-014 |
| Facebook (medium)<br>Found — likely match | https://www.facebook.com/harbour.demo.plumbing.fixture [linked from the website]; 1 candidate profile(s); linked from the website Evidence: ev-presence-r2-016 |
| Instagram (medium)<br>Found — likely match | https://www.instagram.com/harbour.demo.plumbing.fixture [linked from the website]; 1 candidate profile(s); linked from the website Evidence: ev-presence-r2-017 |
| Yellow Pages AU (medium)<br>Found — likely match | https://www.yellowpages.com.au/nsw/sydney/harbour-demo-plumbing-fixture [linked from the website]; 1 candidate profile(s); linked from the website Evidence: ev-presence-r2-018 |
| True Local, Localsearch, Houzz (medium)<br>Not assessed | Not searched: no --dataforseo-max-usd cap was passed Evidence: ev-presence-r2-014 |
| Apple Business / Apple Maps (low)<br>Not assessed | Not searched: no --dataforseo-max-usd cap was passed. Apple Business replaced Apple Business Connect. No provider search; manual only. Evidence: ev-presence-r2-014 |
| LinkedIn, YouTube, TikTok, Yelp, Hotfrog, Cylex, Whereis, Word of Mouth, Airtasker, ServiceSeeking (low)<br>Not assessed | Not searched: no --dataforseo-max-usd cap was passed Evidence: ev-presence-r2-014 |
| Pinterest (low)<br>Not assessed | Not searched: no --dataforseo-max-usd cap was passed. Relevant only where visual inspiration drives enquiries. Evidence: ev-presence-r2-014 |
| Chambers of commerce / professional & industry associations (low)<br>Not assessed | Not searched: no --dataforseo-max-usd cap was passed. Generic manual item: membership directories differ by industry and region (e.g. local chambers, trade or traffic-management industry bodies). Evidence: ev-presence-r2-014 |
| Tripadvisor, OpenTable, Uber Eats, DoorDash, Menulog, Fresha, Booksy, Timely, NSW buy.nsw Supplier Hub, AusTender, ICN Gateway (not\_applicable)<br>Not applicable | Not relevant for the recorded industry and buying model. Evidence: ev-presence-r2-014 |

### Website and email platforms

- **Google Workspace — NOT ASSESSED**. Connection readiness: Unsupported. Public DNS evidence unavailable (domain\_email not assessed). Next action: Verify manually
- **Microsoft 365 — NOT ASSESSED**. Connection readiness: Unsupported. Public DNS evidence unavailable (domain\_email not assessed). Next action: Verify manually

Other website platforms not detected on the sampled pages: WordPress, Shopify, Wix, Squarespace, Webflow, Joomla, Drupal (evidence ev-presence-r2-001).


### Analytics & tracking readiness

Public tags suggest which tools may be in use; they do not establish access to the accounts or prove that tracking is configured correctly.



Consent management observed (consent-gated scripts): tags not seen loading are reported as Unknown rather than Detected or Not detected.

- **Google Search Console — Detected**. Connection readiness: Not connected. Seen on http://127.0.0.1:18765/ (ev-presence-r2-002). Next action: Request access (customer access checklist)
- **Bing Webmaster Tools — Not detected**. Connection readiness: Unsupported. No public signal on 6 sampled pages (ev-presence-r2-001). Next action: Optional: import the site from Search Console
- **Google Analytics 4 — Unknown** (G-FIXTURE001). Connection readiness: Unsupported. Tag present on http://127.0.0.1:18765/ but consent-gated and not seen loading on the sampled pages (ev-presence-r2-003). Next action: Verify manually
- **Universal Analytics (legacy) — Unknown**. Connection readiness: Unsupported. Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent (ev-presence-r2-001). Next action: Verify manually
- **Google Tag Manager — Unknown** (GTM-FIXTURE1). Connection readiness: Unsupported. Tag present on http://127.0.0.1:18765/ but consent-gated and not seen loading on the sampled pages (ev-presence-r2-004). Next action: Verify manually
- **Microsoft Clarity — Unknown** (fixture01). Connection readiness: Unsupported. Tag present on http://127.0.0.1:18765/ but consent-gated and not seen loading on the sampled pages (ev-presence-r2-005). Next action: Verify manually
- **Hotjar — Unknown**. Connection readiness: Unsupported. Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent (ev-presence-r2-001). Next action: Verify manually
- **Google Ads — Unknown**. Connection readiness: Unsupported. Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent (ev-presence-r2-001). Next action: Verify manually
- **Meta Pixel — Unknown**. Connection readiness: Unsupported. Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent (ev-presence-r2-001). Next action: Verify manually
- **LinkedIn Insight Tag — Unknown**. Connection readiness: Unsupported. Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent (ev-presence-r2-001). Next action: Verify manually
- **Microsoft Advertising UET — Unknown**. Connection readiness: Unsupported. Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent (ev-presence-r2-001). Next action: Verify manually
- **TikTok Pixel — Unknown**. Connection readiness: Unsupported. Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent (ev-presence-r2-001). Next action: Verify manually


#### presence-001: Priority platforms still need presence verification

**Basis:** Reported by customer. **Verification status:** needs verification.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** opportunity

**Observed / reported:** In priority order for industry Trades & home services (incl. painting), buying model Service-area (staff travel to customers): hipages (high): Not assessed; Bing Places / Bing Maps (medium): Not assessed; True Local (medium): Not assessed; Localsearch (medium): Not assessed; Houzz (medium): Not assessed. Independent search: Not assessed (no --dataforseo-max-usd cap was passed).

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Work through the manual verification tasks in priority order and record the results with ams-assess presence-verify. Create or correct only profiles the business genuinely operates.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** presence (ev-presence-r2-014); presence (ev-presence-r2-013).
#### presence-003: Analytics or tracking tags could not be confirmed

**Basis:** Inferred (heuristic or AI, needs verification). **Verification status:** needs verification.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** opportunity

**Observed / reported:** Google Analytics 4: Tag present on http://127.0.0.1:18765/ but consent-gated and not seen loading on the sampled pages; Universal Analytics (legacy): Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent; Google Tag Manager: Tag present on http://127.0.0.1:18765/ but consent-gated and not seen loading on the sampled pages; Microsoft Clarity: Tag present on http://127.0.0.1:18765/ but consent-gated and not seen loading on the sampled pages; Hotjar: Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent; Google Ads: Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent; Meta Pixel: Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent; LinkedIn Insight Tag: Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent; Microsoft Advertising UET: Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent; TikTok Pixel: Not seen, but a consent manager or consent-gated tags are present; the tag may load only after consent.

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Verify with the customer in Google Tag Manager, Tag Assistant or the consent manager which tags run after consent.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** presence (ev-presence-r2-001); presence (ev-presence-r2-003); presence (ev-presence-r2-004); presence (ev-presence-r2-005).
## 6. Domain, DNS, email and relevant security observations

These checks concern the technical services behind the website and email. DNS is the domain's public address book; email authentication helps receiving systems recognise authorised senders. These observations do not guarantee security or email delivery.

### Security observations

#### security-005: Recommended security headers missing or need review

**Basis:** Inferred (heuristic or AI, needs verification). **Verification status:** needs verification.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** problem

**Observed / reported:** Framing protection (importance: low): Neither X-Frame-Options nor CSP frame-ancestors was observed. Evidence: ev-security-r2-001. content-security-policy (importance: low): Content-Security-Policy header is not set. It restricts resources the page can load and prevents XSS attacks. The sampled response did not include content-security-policy. Evidence: ev-security-r2-001, ev-siteone-r2-019. x-content-type-options (importance: low): X-Content-Type-Options header is not set. It stops MIME type sniffing and mitigates content type attacks. The sampled response did not include x-content-type-options. Evidence: ev-security-r2-001, ev-siteone-r2-020. referrer-policy (importance: low): Referrer-Policy header is not set. It controls referrer header sharing and enhances privacy and security. The sampled response did not include referrer-policy. Evidence: ev-security-r2-001, ev-siteone-r2-021. permissions-policy (importance: low): Permissions-Policy header is not set. It allows enabling/disabling browser APIs and features for security. The sampled response did not include permissions-policy. Evidence: ev-security-r2-001, ev-siteone-r2-022.

**Potential business impact (interpretation, not measured):** May affect reliability or protection of business systems; impact requires verification.

**Recommended next action:** Have the hosting team review the listed policies together, prioritising the higher importance items. Test content loading, embedding and HTTPS compatibility before deployment, then recheck the affected HTML responses and Observatory assessment.

**AMS service category:** Managed IT & Technology
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/

**Source:** security (ev-security-r2-001); siteone (ev-siteone-r2-019); siteone (ev-siteone-r2-020); siteone (ev-siteone-r2-021); siteone (ev-siteone-r2-022).
#### security-006: Server software version disclosed

**Basis:** Confirmed (technical evidence). **Verification status:** confirmed.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** problem

**Observed / reported:** Server response header discloses a version: SimpleHTTP/0.6 Python/3.12.13.

**Potential business impact (interpretation, not measured):** May affect reliability or protection of business systems; impact requires verification.

**Recommended next action:** Remove unnecessary version detail in the hosting configuration; a version string alone does not establish a vulnerability.

**AMS service category:** Managed IT & Technology
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Affected URLs:** http://127.0.0.1:18765/

**Source:** security (ev-security-r2-001).

## 8. Operational-process and automation findings

Business discovery: customer-provided information

> The statements below were provided by the customer during discovery. They are not technical evidence and have not been independently verified; findings that rest on them are labelled as such.

### Business model, services, customers & priorities

- Residential plumbing and heating firm with 6 engineers and 2 office staff, mostly boiler servicing, repairs and bathroom refits Owner: Owner. Evidence: ev-discovery-r3-001.
- Priority for the next year is more planned servicing contracts and fewer one-off emergency call-outs Owner: Owner. Evidence: ev-discovery-r3-002.

### Enquiries & communication

- Calls during jobs or after 5pm go to voicemail and some are never returned Frequency: several times a week. Systems: Landline. Owner: Office manager. Impact: Customer believes some new jobs go to competitors. Staff time: about 1 hour a day listening to and returning voicemails. Current workaround: Engineers sometimes take calls on their own mobiles. Evidence: ev-discovery-r3-005.
- Website form enquiries arrive in the shared inbox and can wait a day for a reply Frequency: daily. Systems: Gmail. Errors/rework: Occasionally two people reply to the same enquiry. Evidence: ev-discovery-r3-006.

### Sales & quoting

- Quotes for refits are typed up in a word processor from the engineer's site notes Frequency: 8-10 quotes a week. Owner: Office manager. Staff time: about 45 minutes per quote. Errors/rework: Prices sometimes copied from an out-of-date price list. Current workaround: Owner re-checks large quotes before they are sent. Evidence: ev-discovery-r3-007.
- Nobody follows up quotes that get no response Impact: Customer does not know how many quotes are lost this way. Evidence: ev-discovery-r3-008.

### Bookings & scheduling

- Engineers' diaries are juggled by phone when an emergency job comes in Frequency: most days. Systems: Google Calendar. Owner: Office manager. Staff time: 1-2 hours a day. Errors/rework: Double bookings happen about twice a month. Current workaround: A paper whiteboard in the office mirrors the calendar. Evidence: ev-discovery-r3-009.

### Systems in use

- Jobs are tracked in a shared spreadsheet, invoices in Xero, bookings in a shared Google Calendar Systems: Spreadsheet, Xero, Google Calendar. Evidence: ev-discovery-r3-003.
- Calls go to a single office landline with a basic voicemail; email is a shared Gmail inbox Systems: Landline, Gmail. Evidence: ev-discovery-r3-004.

### Manual & duplicated tasks

- Completed jobs are re-keyed from the spreadsheet into Xero to raise invoices Frequency: weekly, on Fridays. Systems: Spreadsheet, Xero. Owner: Office manager. Staff time: about 4 hours a week. Errors/rework: Missed or duplicated invoices found at month end. Evidence: ev-discovery-r3-010.
- Annual service reminders are sent by hand from a list Frequency: monthly. Staff time: half a day a month. Evidence: ev-discovery-r3-011.

### Existing automations

- Xero sends automatic payment reminders; nothing else is automated and no AI tools are used Systems: Xero. Evidence: ev-discovery-r3-015.

### Pain points & bottlenecks

- Month-end invoicing is the biggest bottleneck and delays cash coming in Frequency: monthly. Owner: Owner. Impact: Payment typically arrives 3-4 weeks after the job. Evidence: ev-discovery-r3-013.
- The office laptop holding the job spreadsheet has no backup Current workaround: Owner occasionally copies it to a USB stick. Evidence: ev-discovery-r3-014.

### Data duplication, integrations & disconnected systems

- Customer details exist separately in the spreadsheet, Xero and the phone contacts, and often disagree Systems: Spreadsheet, Xero. Errors/rework: Invoices sent to old addresses a few times a quarter. Cost (as stated): Owner estimates one or two unpaid invoices a quarter, around 300 GBP each. Evidence: ev-discovery-r3-012.

### Opportunities, constraints & required human approvals

- Owner would most like enquiries and bookings to run without the office chasing them Owner: Owner. Evidence: ev-discovery-r3-016.
- Quotes and any message about price must be approved by the owner before sending Owner: Owner. Evidence: ev-discovery-r3-017.
- Budget is limited this year and the team wants to keep Xero Systems: Xero. Cost (as stated): Owner mentioned up to 150 GBP a month for new software. Evidence: ev-discovery-r3-018.

#### discovery-missed-calls-and-slow-enquiry-replies: Missed calls and slow enquiry replies

**Basis:** Reported by customer. **Verification status:** needs verification.

**Importance:** high | **Priority:** P1 | **Urgency:** Review within 0-30 days | **Type:** problem

**Observed / reported:** The customer states that out-of-hours and on-job calls go to voicemail and some are never returned, and form enquiries can wait a day.

**Potential business impact (interpretation, not measured):** Customer believes some new work goes to competitors (not measured)

**Recommended next action:** Map the enquiry journey, then trial call capture with a missed-call text-back and a shared enquiry queue; keep pricing replies for human approval. Review the current process first; prefer keeping and integrating existing systems before considering replacement.

**Likely cause (interpretation):** No cover for calls while the office is busy or closed

**AMS service category:** AI & Automation
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** discovery (ev-discovery-r3-005); discovery (ev-discovery-r3-006); discovery (ev-discovery-r3-017).
#### discovery-job-scheduling-and-invoicing-data-live-in-disconne: Job, scheduling and invoicing data live in disconnected tools

**Basis:** Reported by customer. **Verification status:** needs verification.

**Importance:** medium | **Priority:** P2 | **Urgency:** Plan within 30-90 days | **Type:** opportunity

**Observed / reported:** Jobs are tracked in a spreadsheet and re-keyed into Xero, customer details disagree between systems, and diaries are juggled by phone.

**Potential business impact (interpretation, not measured):** Stated rework, double bookings and delayed invoicing

**Recommended next action:** Assess a job-management system that integrates with Xero, within the stated budget, before considering any automation on top. Review the current process first; prefer keeping and integrating existing systems before considering replacement.

**AMS service category:** Business Systems
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** discovery (ev-discovery-r3-003); discovery (ev-discovery-r3-009); discovery (ev-discovery-r3-010); discovery (ev-discovery-r3-012); discovery (ev-discovery-r3-018).
#### discovery-job-spreadsheet-has-no-backup: Job spreadsheet has no backup

**Basis:** Reported by customer. **Verification status:** needs verification.

**Importance:** medium | **Priority:** P2 | **Urgency:** Plan within 30-90 days | **Type:** problem

**Observed / reported:** The customer states the laptop holding the job spreadsheet is not backed up.

**Potential business impact (interpretation, not measured):** May affect reliability or protection of business systems; impact requires verification.

**Recommended next action:** Confirm the current setup on site, then put managed backup in place for business data.

**AMS service category:** Managed IT & Technology
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** discovery (ev-discovery-r3-014).
#### discovery-no-follow-up-on-quotes-or-service-reminders: No follow-up on quotes or service reminders

**Basis:** Reported by customer. **Verification status:** needs verification.

**Importance:** low | **Priority:** P3 | **Urgency:** Consider after 90 days | **Type:** opportunity

**Observed / reported:** Unanswered quotes are not followed up and annual service reminders are sent by hand, while the stated priority is more planned servicing.

**Potential business impact (interpretation, not measured):** May affect how customers find, understand or use the business online; outcomes are not measured.

**Recommended next action:** Set up owner-approved quote follow-up and automated service reminders to support recurring servicing work.

**AMS service category:** Digital Growth
**Dependencies:** Standard requirements (section 2).
**Information still required:** Standard requirements (section 2).

**Source:** discovery (ev-discovery-r3-002); discovery (ev-discovery-r3-008); discovery (ev-discovery-r3-011).
## 9. Prioritised improvement opportunities and recommended roadmap

Agree the process, owner and desired outcome before selecting products. Timing is a planning suggestion, dependent on verification, access, budget and approval.

### 0-30 days

- **P1 - Elements must meet minimum color contrast ratio thresholds (browser-001):** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/color-contrast?application=axeAPI and verify with keyboard and assistive technology. Evidence: ev-browser-r2-003, ev-browser-r2-009.
- **P1 - Images must have alternative text (browser-002):** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/image-alt?application=axeAPI and verify with keyboard and assistive technology. Evidence: ev-browser-r2-003, ev-browser-r2-009.
- **P1 - Missed calls and slow enquiry replies (discovery-missed-calls-and-slow-enquiry-replies):** Map the enquiry journey, then trial call capture with a missed-call text-back and a shared enquiry queue; keep pricing replies for human approval. Prefer keeping and integrating existing systems before considering replacement. Evidence: ev-discovery-r3-005, ev-discovery-r3-006, ev-discovery-r3-017.
- **P1 - Broken URLs (siteone-002):** Repair internal links and restore or redirect unavailable pages. Evidence: ev-siteone-r2-010.

### 30-90 days

- **P2 - Job, scheduling and invoicing data live in disconnected tools (discovery-job-scheduling-and-invoicing-data-live-in-disconne):** Assess a job-management system that integrates with Xero, within the stated budget, before considering any automation on top. Prefer keeping and integrating existing systems before considering replacement. Evidence: ev-discovery-r3-003, ev-discovery-r3-009, ev-discovery-r3-010, ev-discovery-r3-012, ev-discovery-r3-018.
- **P2 - Job spreadsheet has no backup (discovery-job-spreadsheet-has-no-backup):** Confirm the current setup on site, then put managed backup in place for business data. Evidence: ev-discovery-r3-014.
- **P2 - Business name, address or phone differs between structured data and the page (presence-002):** Use one canonical name, address and phone number in structured data, the footer, contact page and external listings. Evidence: ev-presence-r2-010.
- **P2 - Missing meta description (siteone-004):** Add a descriptive, page-specific meta description. Evidence: ev-siteone-r2-015.
- **P2 - Missing H1 (siteone-005):** Add a descriptive, page-specific H1. Evidence: ev-siteone-r2-015.

### 90+ days

- **P3 - No follow-up on quotes or service reminders (discovery-no-follow-up-on-quotes-or-service-reminders):** Set up owner-approved quote follow-up and automated service reminders to support recurring servicing work. Evidence: ev-discovery-r3-002, ev-discovery-r3-008, ev-discovery-r3-011.
- **P3 - Linked profiles missing from structured-data sameAs (presence-004):** Add the customer-owned profile URLs to the business sameAs property. Evidence: ev-presence-r2-006, ev-presence-r2-009, ev-presence-r2-010.
- **P3 - AI crawler rules by purpose (site_checks-001):** Review search/user restrictions with the customer; retain intentional training choices. User-initiated agents may not obey robots rules; verify the journey manually. Evidence: ev-site_checks-r2-003.
- **P3 - Near-duplicate location pages (site_checks-003):** Manually compare unique local value and remove shared boilerplate before deciding whether to improve or consolidate pages. Evidence: ev-site_checks-r2-009.
- **P3 - Priority platforms still need presence verification (presence-001):** Work through the manual verification tasks in priority order and record the results with ams-assess presence-verify. Create or correct only profiles the business genuinely operates. Evidence: ev-presence-r2-014, ev-presence-r2-013.
- **P3 - Analytics or tracking tags could not be confirmed (presence-003):** Verify with the customer in Google Tag Manager, Tag Assistant or the consent manager which tags run after consent. Evidence: ev-presence-r2-001, ev-presence-r2-003, ev-presence-r2-004, ev-presence-r2-005.
- **P3 - Recommended security headers missing or need review (security-005):** Have the hosting team review the listed policies together, prioritising the higher importance items. Test content loading, embedding and HTTPS compatibility before deployment, then recheck the affected HTML responses and Observatory assessment. Evidence: ev-security-r2-001, ev-siteone-r2-019, ev-siteone-r2-020, ev-siteone-r2-021, ev-siteone-r2-022.
- **P3 - Server software version disclosed (security-006):** Remove unnecessary version detail in the hosting configuration; a version string alone does not establish a vulnerability. Evidence: ev-security-r2-001.
- **P3 - Duplicates, headings / internal links (site_checks-002):** Use distinct titles/descriptions, fix heading jumps and broken links, link directly to final targets. Retry unavailable links before classifying them. Verify possible orphan pages with a complete link graph. Evidence: ev-site_checks-r2-007.
- **P3 - Pages without a canonical link (siteone-010):** Add a self-referencing rel=canonical to each indexable page. Evidence: ev-siteone-r2-024.
- **P3 - Review internal redirects (siteone-001):** Link directly to the final canonical URL. Evidence: ev-siteone-r2-009, ev-siteone-r2-011.

## 10. Items requiring customer verification, access or decisions

Resolve these uncertainties before approving changes. Confirm business priorities and the existing systems to retain; access should be named, limited and revocable.

**Form submissions: NOT TESTED.** Arrange an owner-approved test of enquiry, booking and payment journeys; this assessment submitted no forms.

### Connection readiness

A successful connection means evidence was returned for this sample. Unsupported means this engine has no connector; any review would be manual. Public detection is separate from authorised access.

| Service / website | Readiness status | Coverage, limitation or next step | Evidence |
| --- | --- | --- | --- |
| PageSpeed Insights<br>http://127.0.0.1:18765/ | Not assessed | PageSpeed/CrUX cannot access localhost | - |
| CrUX<br>http://127.0.0.1:18765/ | Not assessed | PageSpeed/CrUX cannot access localhost | - |
| DataForSEO<br>http://127.0.0.1:18765/ | Not connected | Missing DataForSEO credentials; disabled | - |
| Google Search Console<br>http://127.0.0.1:18765/ | Not connected | GSC requires --connect gsc and credentials; missing connection or credentials. The generative-AI performance report is available in the Search Console interface. As of 2026-10-23, the documented API has no separate generative-AI report endpoint or filter; use a customer-provided UI export for manual review. Aggregate Web results cannot establish AI visibility. Sources: https://developers.google.com/webmaster-tools/v1/searchanalytics/query and https://support.google.com/webmasters/answer/16984139 | - |
| Google Analytics 4<br>http://127.0.0.1:18765/ | Unsupported | Traffic sources, key events/conversions and engagement. No connector implemented; manual review only. | ev-presence-r2-003 |
| Google Business Profile<br>http://127.0.0.1:18765/ | Unsupported | Profile completeness, reviews, calls, direction requests and Maps/Search views. No connector implemented; manual review only. | ev-presence-r2-006 |
| Bing Webmaster Tools<br>http://127.0.0.1:18765/ | Unsupported | Bing search performance, crawl errors and index status. No connector implemented; manual review only. | ev-presence-r2-001 |
| WordPress / WP-CLI (+ Site Health)<br>http://127.0.0.1:18765/ | Unsupported | Core, plugin and theme versions, pending updates and Site Health status. No connector implemented; manual review only. | ev-presence-r2-001 |
| Microsoft Clarity<br>http://127.0.0.1:18765/ | Unsupported | Heatmaps, session recordings and rage/dead-click signals. No connector implemented; manual review only. | ev-presence-r2-005 |

### Customer access requirements: http://127.0.0.1:18765/


- The customer keeps ownership of every account and property; AMS is added as a user.
- AMS never asks for passwords, MFA codes or shared logins.
- Least privilege: read-only roles unless an agreed task needs more.
- Access is revocable by the customer at any time and is removed when the work ends.
- The AMS access account is shown as a placeholder; set AMS\_ACCESS\_EMAIL before sending this checklist.

1. **Google Search Console** (access: Restricted user): Add \[AMS access account - set AMS\_ACCESS\_EMAIL\] as a Restricted user on the existing Search Console property (Full user only if AMS will submit sitemaps or fixes). Evidence: ev-presence-r2-002.
2. **Google Analytics 4** (access: Viewer): GA4 (G-FIXTURE001) could not be confirmed loading (consent-gated or via Google Tag Manager). Confirm with the customer; if used, grant \[AMS access account - set AMS\_ACCESS\_EMAIL\] the Viewer role on the GA4 property. Evidence: ev-presence-r2-003.
3. **Google Business Profile** (access: Manager): Confirm the business owns its Google Business Profile (linked from the website: https://g.page/harbour-demo-plumbing-fixture), then add \[AMS access account - set AMS\_ACCESS\_EMAIL\] as a Manager (not Owner). Evidence: ev-presence-r2-006.
4. **Bing Webmaster Tools** (access: Read-only user): If the site is in Bing Webmaster Tools, add \[AMS access account - set AMS\_ACCESS\_EMAIL\] as a Read-only user; if not, the customer can import it from Search Console. Evidence: ev-presence-r2-001.
5. **Microsoft Clarity** (access: Team member): Confirm the Clarity project (fixture01) is in use, then add \[AMS access account - set AMS\_ACCESS\_EMAIL\] as a Team member. Evidence: ev-presence-r2-005.

Manual verification tasks (record results with `ams-assess presence-verify`):

1. **hipages** (high): Search hipages for the business and record what you find. Search: [site:hipages.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Ahipages.com.au+%22Harbour+Demo+Plumbing%22).
2. **Bing Places / Bing Maps** (medium): Search Bing Places / Bing Maps for the business and record what you find. Search: [Harbour Demo Plumbing Sydney](https://www.bing.com/maps?q=Harbour+Demo+Plumbing+Sydney).
3. **True Local** (medium): Search True Local for the business and record what you find. Search: [site:truelocal.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Atruelocal.com.au+%22Harbour+Demo+Plumbing%22).
4. **Localsearch** (medium): Search Localsearch for the business and record what you find. Search: [site:localsearch.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Alocalsearch.com.au+%22Harbour+Demo+Plumbing%22).
5. **Houzz** (medium): Search Houzz for the business and record what you find. Search: [site:houzz.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Ahouzz.com.au+%22Harbour+Demo+Plumbing%22).
6. **Apple Business / Apple Maps** (low): Search Apple Business / Apple Maps for the business and record what you find. Search: [Harbour Demo Plumbing Sydney](https://maps.apple.com/?q=Harbour+Demo+Plumbing+Sydney).
7. **LinkedIn** (low): Search LinkedIn for the business and record what you find. Search: [site:linkedin.com/company "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Alinkedin.com%2Fcompany+%22Harbour+Demo+Plumbing%22).
8. **YouTube** (low): Search YouTube for the business and record what you find. Search: [site:youtube.com "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Ayoutube.com+%22Harbour+Demo+Plumbing%22).
9. **TikTok** (low): Search TikTok for the business and record what you find. Search: [site:tiktok.com "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Atiktok.com+%22Harbour+Demo+Plumbing%22).
10. **Pinterest** (low): Search Pinterest for the business and record what you find. Search: [site:pinterest.com "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Apinterest.com+%22Harbour+Demo+Plumbing%22).
11. **Yelp** (low): Search Yelp for the business and record what you find. Search: [site:yelp.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Ayelp.com.au+%22Harbour+Demo+Plumbing%22).
12. **Hotfrog** (low): Search Hotfrog for the business and record what you find. Search: [site:hotfrog.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Ahotfrog.com.au+%22Harbour+Demo+Plumbing%22).
13. **Cylex** (low): Search Cylex for the business and record what you find. Search: [site:cylex.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Acylex.com.au+%22Harbour+Demo+Plumbing%22).
14. **Whereis** (low): Search Whereis for the business and record what you find. Search: [site:whereis.com "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Awhereis.com+%22Harbour+Demo+Plumbing%22).
15. **Word of Mouth** (low): Search Word of Mouth for the business and record what you find. Search: [site:wordofmouth.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Awordofmouth.com.au+%22Harbour+Demo+Plumbing%22).
16. **Chambers of commerce / professional & industry associations** (low): Search Chambers of commerce / professional & industry associations for the business and record what you find. Search: ["Harbour Demo Plumbing" member (chamber OR association) Sydney](https://www.google.com.au/search?q=%22Harbour+Demo+Plumbing%22+member+%28chamber+OR+association%29+Sydney).
17. **Airtasker** (low): Search Airtasker for the business and record what you find. Search: [site:airtasker.com "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Aairtasker.com+%22Harbour+Demo+Plumbing%22).
18. **ServiceSeeking** (low): Search ServiceSeeking for the business and record what you find. Search: [site:serviceseeking.com.au "Harbour Demo Plumbing"](https://www.google.com.au/search?q=site%3Aserviceseeking.com.au+%22Harbour+Demo+Plumbing%22).

### Manual verification items



- discovery-missed-calls-and-slow-enquiry-replies: Missed calls and slow enquiry replies. Evidence: ev-discovery-r3-005, ev-discovery-r3-006, ev-discovery-r3-017.
- discovery-job-scheduling-and-invoicing-data-live-in-disconne: Job, scheduling and invoicing data live in disconnected tools. Evidence: ev-discovery-r3-003, ev-discovery-r3-009, ev-discovery-r3-010, ev-discovery-r3-012, ev-discovery-r3-018.
- discovery-job-spreadsheet-has-no-backup: Job spreadsheet has no backup. Evidence: ev-discovery-r3-014.
- discovery-no-follow-up-on-quotes-or-service-reminders: No follow-up on quotes or service reminders. Evidence: ev-discovery-r3-002, ev-discovery-r3-008, ev-discovery-r3-011.
- site_checks-001: AI crawler rules by purpose. Evidence: ev-site_checks-r2-003.
- site_checks-003: Near-duplicate location pages. Evidence: ev-site_checks-r2-009.
- presence-001: Priority platforms still need presence verification. Evidence: ev-presence-r2-014, ev-presence-r2-013.
- presence-003: Analytics or tracking tags could not be confirmed. Evidence: ev-presence-r2-001, ev-presence-r2-003, ev-presence-r2-004, ev-presence-r2-005.
- security-005: Recommended security headers missing or need review. Evidence: ev-security-r2-001, ev-siteone-r2-019, ev-siteone-r2-020, ev-siteone-r2-021, ev-siteone-r2-022.

- **Not assessed:** domain\_email (http://127.0.0.1:18765/). Local/private target has no public domain audit. Confirm access or arrange a manual check if this is important to the customer.
- **Not assessed:** performance (http://127.0.0.1:18765/). PageSpeed/CrUX cannot access localhost. Confirm access or arrange a manual check if this is important to the customer.
- **Not assessed:** observatory (http://127.0.0.1:18765/). Observatory cannot scan local targets. Confirm access or arrange a manual check if this is important to the customer.
- **Not assessed:** dataforseo (http://127.0.0.1:18765/). Missing DataForSEO credentials; disabled. Confirm access or arrange a manual check if this is important to the customer.
- **Not assessed:** gsc (http://127.0.0.1:18765/). GSC requires --connect gsc and credentials; missing connection or credentials. Confirm access or arrange a manual check if this is important to the customer.

## Appendix: evidence index

The machine-readable evidence.json retains the observed snippets and links to raw source artefacts. Evidence IDs below are the stable references used throughout this report.

**profile — Customer business profile (customer statement)** (customer statement): customer-profile: ev-profile-r1-001.

**siteone — Target redirect resolution before crawling**: http://127.0.0.1:18765/: ev-siteone-r2-001.

**siteone — SiteOne HTTP response**: http://127.0.0.1:18765/: ev-siteone-r2-002; http://127.0.0.1:18765/service/a/: ev-siteone-r2-003; http://127.0.0.1:18765/about.html: ev-siteone-r2-004; http://127.0.0.1:18765/services.html: ev-siteone-r2-005; http://127.0.0.1:18765/contact.html: ev-siteone-r2-006; http://127.0.0.1:18765/service/b/: ev-siteone-r2-007; http://127.0.0.1:18765/quote.html: ev-siteone-r2-008; http://127.0.0.1:18765/old-service: ev-siteone-r2-009; http://127.0.0.1:18765/missing-offer.html: ev-siteone-r2-010; http://127.0.0.1:18765/older-service: ev-siteone-r2-011.

**siteone — SiteOne SEO metadata**: http://127.0.0.1:18765/: ev-siteone-r2-012; http://127.0.0.1:18765/about.html: ev-siteone-r2-013; http://127.0.0.1:18765/contact.html: ev-siteone-r2-014; http://127.0.0.1:18765/quote.html: ev-siteone-r2-015; http://127.0.0.1:18765/service/a/: ev-siteone-r2-016; http://127.0.0.1:18765/service/b/: ev-siteone-r2-017; http://127.0.0.1:18765/services.html: ev-siteone-r2-018.

**siteone — SiteOne header analysis (crawl aggregate)**: http://127.0.0.1:18765/: ev-siteone-r2-019, ev-siteone-r2-020, ev-siteone-r2-021, ev-siteone-r2-022.

**siteone — SiteOne robots.txt load**: http://127.0.0.1:18765/: ev-siteone-r2-023.

**siteone — SiteOne canonical link summary**: http://127.0.0.1:18765/: ev-siteone-r2-024.

**siteone — SiteOne seo-headings table**: http://127.0.0.1:18765/: ev-siteone-r2-025.

**siteone — SiteOne redirects table**: http://127.0.0.1:18765/: ev-siteone-r2-026.

**siteone — SiteOne 404 table**: http://127.0.0.1:18765/: ev-siteone-r2-027.

**siteone — SiteOne headers table**: http://127.0.0.1:18765/: ev-siteone-r2-028.

**siteone — SiteOne headers-values table**: http://127.0.0.1:18765/: ev-siteone-r2-029.

**siteone — SiteOne external-urls table**: http://127.0.0.1:18765/: ev-siteone-r2-030.

**browser — Rendered page evidence (desktop)**: http://127.0.0.1:18765/: ev-browser-r2-001; http://127.0.0.1:18765/about.html: ev-browser-r2-002; http://127.0.0.1:18765/quote.html: ev-browser-r2-003; http://127.0.0.1:18765/service/a/: ev-browser-r2-004; http://127.0.0.1:18765/service/b/: ev-browser-r2-005; http://127.0.0.1:18765/contact.html: ev-browser-r2-006.

**browser — Rendered page evidence (mobile)**: http://127.0.0.1:18765/: ev-browser-r2-007; http://127.0.0.1:18765/about.html: ev-browser-r2-008; http://127.0.0.1:18765/quote.html: ev-browser-r2-009; http://127.0.0.1:18765/service/a/: ev-browser-r2-010; http://127.0.0.1:18765/service/b/: ev-browser-r2-011; http://127.0.0.1:18765/contact.html: ev-browser-r2-012.

**security — HTTP response security headers**: http://127.0.0.1:18765/: ev-security-r2-001.

**presence — Digital presence detection coverage (no additional requests)**: http://127.0.0.1:18765/: ev-presence-r2-001.

**presence — Platform signal: Google Search Console**: http://127.0.0.1:18765/: ev-presence-r2-002.

**presence — Platform signal: Google Analytics 4**: http://127.0.0.1:18765/: ev-presence-r2-003.

**presence — Platform signal: Google Tag Manager**: http://127.0.0.1:18765/: ev-presence-r2-004.

**presence — Platform signal: Microsoft Clarity**: http://127.0.0.1:18765/: ev-presence-r2-005.

**presence — Listing link: Google Business Profile / Google Maps**: https://g.page/harbour-demo-plumbing-fixture: ev-presence-r2-006.

**presence — Listing link: Facebook**: https://www.facebook.com/harbour.demo.plumbing.fixture: ev-presence-r2-007.

**presence — Listing link: Instagram**: https://www.instagram.com/harbour.demo.plumbing.fixture: ev-presence-r2-008.

**presence — Listing link: Yellow Pages AU**: https://www.yellowpages.com.au/nsw/sydney/harbour-demo-plumbing-fixture: ev-presence-r2-009.

**presence — NAP consistency comparison**: http://127.0.0.1:18765/: ev-presence-r2-010.

**presence — Location / service-area page scan of crawled URLs**: http://127.0.0.1:18765/: ev-presence-r2-011.

**presence — Bounded robots.txt and XML sitemap fetch**: http://127.0.0.1:18765/sitemap.xml: ev-presence-r2-012.

**presence — Customer business profile (customer statement)** (customer statement): customer-profile: ev-presence-r2-013.

**presence — Independent presence discovery plan and coverage**: http://127.0.0.1:18765/: ev-presence-r2-014.

**presence — Presence candidates: Google Business Profile / Google Maps**: https://g.page/harbour-demo-plumbing-fixture: ev-presence-r2-015.

**presence — Presence candidates: Facebook**: https://www.facebook.com/harbour.demo.plumbing.fixture: ev-presence-r2-016.

**presence — Presence candidates: Instagram**: https://www.instagram.com/harbour.demo.plumbing.fixture: ev-presence-r2-017.

**presence — Presence candidates: Yellow Pages AU**: https://www.yellowpages.com.au/nsw/sydney/harbour-demo-plumbing-fixture: ev-presence-r2-018.

**site\_checks — Bounded site-check HTTP probes**: http://127.0.0.1:18765/: ev-site_checks-r2-001.

**site\_checks — Site check: Robots rules / site-wide Disallow**: http://127.0.0.1:18765/: ev-site_checks-r2-002.

**site\_checks — Site check: AI crawler rules by purpose**: http://127.0.0.1:18765/: ev-site_checks-r2-003.

**site\_checks — Site check: Page noindex / X-Robots-Tag**: http://127.0.0.1:18765/: ev-site_checks-r2-004.

**site\_checks — Site check: Sitemap URL validity**: http://127.0.0.1:18765/: ev-site_checks-r2-005.

**site\_checks — Site check: Canonical accessibility / consistency**: http://127.0.0.1:18765/: ev-site_checks-r2-006.

**site\_checks — Site check: Duplicates, headings / internal links**: http://127.0.0.1:18765/: ev-site_checks-r2-007.

**site\_checks — Site check: Organization / LocalBusiness / BreadcrumbList**: http://127.0.0.1:18765/: ev-site_checks-r2-008.

**site\_checks — Site check: Near-duplicate location pages**: http://127.0.0.1:18765/: ev-site_checks-r2-009.

**site\_checks — Site check: Core Web Vitals (field p75)**: http://127.0.0.1:18765/: ev-site_checks-r2-010.

**site\_checks — Site check: Booking / CRM / chat tools**: http://127.0.0.1:18765/: ev-site_checks-r2-011.

**site\_checks — Site check: Conversion pathways / forms NOT TESTED**: http://127.0.0.1:18765/: ev-site_checks-r2-012.

**site\_checks — Site check: Analytics / conversion-event code signals**: http://127.0.0.1:18765/: ev-site_checks-r2-013.

**site\_checks — Site check: Suspected CDN / firewall crawler blocking**: http://127.0.0.1:18765/: ev-site_checks-r2-014.

**site\_checks — Site check: llms.txt (informational)**: http://127.0.0.1:18765/: ev-site_checks-r2-015.

**site\_checks — Site check: Answer readiness (heuristic)**: http://127.0.0.1:18765/: ev-site_checks-r2-016.

**discovery — Customer statement (Business model, services, customers & priorities): Residential plumbing and heating firm with 6 engineers and 2 office staff, mostly boiler servicing, repairs and bathroom refits** (customer statement): customer-discovery-notes: ev-discovery-r3-001.

**discovery — Customer statement (Business model, services, customers & priorities): Priority for the next year is more planned servicing contracts and fewer one-off emergency call-outs** (customer statement): customer-discovery-notes: ev-discovery-r3-002.

**discovery — Customer statement (Systems in use): Jobs are tracked in a shared spreadsheet, invoices in Xero, bookings in a shared Google Calendar** (customer statement): customer-discovery-notes: ev-discovery-r3-003.

**discovery — Customer statement (Systems in use): Calls go to a single office landline with a basic voicemail; email is a shared Gmail inbox** (customer statement): customer-discovery-notes: ev-discovery-r3-004.

**discovery — Customer statement (Enquiries & communication): Calls during jobs or after 5pm go to voicemail and some are never returned** (customer statement): customer-discovery-notes: ev-discovery-r3-005.

**discovery — Customer statement (Enquiries & communication): Website form enquiries arrive in the shared inbox and can wait a day for a reply** (customer statement): customer-discovery-notes: ev-discovery-r3-006.

**discovery — Customer statement (Sales & quoting): Quotes for refits are typed up in a word processor from the engineer's site notes** (customer statement): customer-discovery-notes: ev-discovery-r3-007.

**discovery — Customer statement (Sales & quoting): Nobody follows up quotes that get no response** (customer statement): customer-discovery-notes: ev-discovery-r3-008.

**discovery — Customer statement (Bookings & scheduling): Engineers' diaries are juggled by phone when an emergency job comes in** (customer statement): customer-discovery-notes: ev-discovery-r3-009.

**discovery — Customer statement (Manual & duplicated tasks): Completed jobs are re-keyed from the spreadsheet into Xero to raise invoices** (customer statement): customer-discovery-notes: ev-discovery-r3-010.

**discovery — Customer statement (Manual & duplicated tasks): Annual service reminders are sent by hand from a list** (customer statement): customer-discovery-notes: ev-discovery-r3-011.

**discovery — Customer statement (Data duplication, integrations & disconnected systems): Customer details exist separately in the spreadsheet, Xero and the phone contacts, and often disagree** (customer statement): customer-discovery-notes: ev-discovery-r3-012.

**discovery — Customer statement (Pain points & bottlenecks): Month-end invoicing is the biggest bottleneck and delays cash coming in** (customer statement): customer-discovery-notes: ev-discovery-r3-013.

**discovery — Customer statement (Pain points & bottlenecks): The office laptop holding the job spreadsheet has no backup** (customer statement): customer-discovery-notes: ev-discovery-r3-014.

**discovery — Customer statement (Existing automations): Xero sends automatic payment reminders; nothing else is automated and no AI tools are used** (customer statement): customer-discovery-notes: ev-discovery-r3-015.

**discovery — Customer statement (Opportunities, constraints & required human approvals): Owner would most like enquiries and bookings to run without the office chasing them** (customer statement): customer-discovery-notes: ev-discovery-r3-016.

**discovery — Customer statement (Opportunities, constraints & required human approvals): Quotes and any message about price must be approved by the owner before sending** (customer statement): customer-discovery-notes: ev-discovery-r3-017.

**discovery — Customer statement (Opportunities, constraints & required human approvals): Budget is limited this year and the team wants to keep Xero** (customer statement): customer-discovery-notes: ev-discovery-r3-018.
