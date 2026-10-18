# AI Managed Services
## Website assessment report

**Customer:** Harbour Demo Plumbing<br>
**Assessment date (UTC):** 2026-10-18<br>
**Target:** http://127.0.0.1:18765/<br>
**Scope:** Functionality &amp; accessibility; Copywriting &amp; messaging; CRO &amp; UX/UI; Domain, DNS &amp; email health; Entity &amp; brand authority; GEO / AI search readiness; Local SEO; Website performance; Security observations; SEO findings; Website technical health

> Evidence-led observations and a proposed improvement plan. Confirm scope, priorities and estimates with AMS before implementation. This assessment makes no changes to your website.

## Executive summary

The assessment of the Harbour Demo Plumbing local fixture (five pages crawled, desktop and mobile rendering) found a small, generally well-structured site with a concentrated set of issues on the quote page. That page has no H1 and no meta description, shows a colour-contrast failure and an image without alternative text, and links through a vague "Click here" CTA to a URL that returns 404 (ev-browser-003, ev-browser-008, ev-siteone-012, ev-siteone-007, ev-siteone-020). The structured data phone number (+61 2 5550 0199) differs from the visible and tel: phone number (02 5550 0100), and LocalBusiness JSON-LD appears only on the home page (ev-browser-001, ev-browser-002). Security headers are missing or weak on the sampled response (ev-security-001, ev-siteone-014). This is a local fixture, so these observations do not establish production risk. Measurements such as performance, rankings and AI search visibility were not available and are treated as limitations.

## Website technical health

### siteone-001: Broken or unavailable URLs

**Severity:** high | **Confidence:** confirmed | **Type:** problem | **Priority:** P1

HTTP 404 at http://127.0.0.1:18765/missing-offer.html.

**Recommended action:** Repair internal links and restore or redirect unavailable pages.

**Likely cause (interpretation):** Hypothesis: an offer page was removed or never published while the link remained.

**Potential business impact:** The only CTA on the quote page leads to an error page, which interrupts the enquiry path.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/missing-offer.html

**Evidence:** ev-siteone-007

## SEO findings

### siteone-002: Missing meta description

**Severity:** medium | **Confidence:** confirmed | **Type:** problem | **Priority:** P2

No meta description recorded at http://127.0.0.1:18765/quote.html.

**Recommended action:** Add a descriptive, page-specific meta description.

**Likely cause (interpretation):** Hypothesis: the page was built from a minimal template.

**Potential business impact:** Search engines may generate their own snippet, giving less control over how the quote page is presented.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Evidence:** ev-siteone-012

### siteone-003: Missing H1

**Severity:** medium | **Confidence:** confirmed | **Type:** problem | **Priority:** P2

No H1 recorded at http://127.0.0.1:18765/quote.html.

**Recommended action:** Add a descriptive, page-specific H1.

**Likely cause (interpretation):** Hypothesis: the heading level was chosen for styling rather than structure.

**Potential business impact:** The page topic is less clear to users, assistive technology and search systems.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Evidence:** ev-siteone-012

## GEO / AI search readiness

### geo-answer-readiness: Service pages offer limited answer-ready content

**Severity:** low | **Confidence:** subjective | **Type:** opportunity | **Priority:** P3

The services page gives one-sentence descriptions for leak repairs and drain clearing, with no pricing guidance, service area detail, timeframes or FAQs. Heading structure is otherwise clean. No claim is made about AI search visibility, which was not measured.

**Recommended action:** Add concise factual sections covering what each service includes, typical process, areas served and common questions, supported by matching structured data.

**Likely cause (interpretation):** Hypothesis: the fixture is intentionally minimal.

**Potential business impact:** Richer, clearly structured answers can help both visitors and search or assistant systems understand the offer.

**Suggested AMS service:** Content and copywriting

**Affected URLs:** http://127.0.0.1:18765/services.html

**Evidence:** ev-browser-005, ev-siteone-019

## Local SEO

### entity-phone-mismatch: Structured data phone differs from visible phone number

**Severity:** medium | **Confidence:** needs_verification | **Type:** problem | **Priority:** P1

Home page LocalBusiness JSON-LD lists telephone +61 2 5550 0199, while the visible footer text and tel: link use 02 5550 0100 (+61255500100). Name and address match between the footer and JSON-LD.

**Recommended action:** Confirm the correct number, then align the JSON-LD, visible text and tel: links, and check any external listings against it.

**Likely cause (interpretation):** Hypothesis: the structured data was written separately from the page template and not updated.

**Potential business impact:** Inconsistent business details can weaken entity consistency and may send callers to the wrong number.

**Suggested AMS service:** Local SEO

**Affected URLs:** http://127.0.0.1:18765/

**Evidence:** ev-browser-001, ev-browser-006

### local-schema-coverage: LocalBusiness structured data observed only on the home page

**Severity:** low | **Confidence:** needs_verification | **Type:** opportunity | **Priority:** P2

JSON-LD was found on the home page only. The about, quote, contact and services pages had none. The home page markup also lacks fields such as url, openingHours, areaServed, email and sameAs.

**Recommended action:** Extend structured data with a complete LocalBusiness or Plumber entity, plus Service markup on the services page, and validate it.

**Likely cause (interpretation):** Hypothesis: markup was added manually to one template.

**Potential business impact:** Entity and service information is less machine-readable on pages where users make decisions.

**Suggested AMS service:** Local SEO

**Affected URLs:** http://127.0.0.1:18765/; http://127.0.0.1:18765/services.html

**Evidence:** ev-browser-001, ev-browser-002, ev-browser-003, ev-browser-004, ev-browser-005

## Entity & brand authority

### contact-privacy-heading: Privacy policy is an H2 anchor on the contact page

**Severity:** low | **Confidence:** needs_verification | **Type:** problem | **Priority:** P3

The privacy policy is a short section (H2) within the contact page, reached via \#privacy, rather than a dedicated page, and states that it does not collect submissions even though the form posts to /request-quote.

**Recommended action:** Provide a dedicated, accurate privacy policy that matches the real form handling before launch.

**Likely cause (interpretation):** Hypothesis: fixture simplification.

**Potential business impact:** A policy that does not match the form's behaviour can undermine trust and compliance.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/contact.html

**Evidence:** ev-browser-004, ev-siteone-019

## Functionality & accessibility

### browser-001: Elements must meet minimum color contrast ratio thresholds

**Severity:** high | **Confidence:** confirmed | **Type:** problem | **Priority:** P1

desktop: 1 affected elements at http://127.0.0.1:18765/quote.html (axe color-contrast). mobile: 1 affected elements at http://127.0.0.1:18765/quote.html (axe color-contrast).

**Recommended action:** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/color-contrast?application=axeAPI and verify with keyboard and assistive technology.

**Likely cause (interpretation):** Hypothesis: low-contrast text colour or background styling applied to the quote details paragraph.

**Potential business impact:** Visitors with low vision may be unable to read quote details, which could reduce enquiry completion on the key conversion page.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Evidence:** ev-browser-003, ev-browser-008

### browser-002: Images must have alternative text

**Severity:** high | **Confidence:** confirmed | **Type:** problem | **Priority:** P1

desktop: 1 affected elements at http://127.0.0.1:18765/quote.html (axe image-alt). mobile: 1 affected elements at http://127.0.0.1:18765/quote.html (axe image-alt).

**Recommended action:** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/image-alt?application=axeAPI and verify with keyboard and assistive technology.

**Likely cause (interpretation):** Hypothesis: the work.svg image was added without an alt attribute.

**Potential business impact:** Screen reader users miss the image content, and the page is less accessible and less clear to automated readers.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Evidence:** ev-browser-003, ev-browser-008

## CRO & UX/UI

### cro-contact-form-submit: Contact form submit control has no accessible label and the quote page has no form

**Severity:** medium | **Confidence:** needs_verification | **Type:** problem | **Priority:** P2

The contact form's submit field reports no name or label, and the 'Request a quote' CTA has an empty href. The page states the demo form does not send messages. The Quote page has no form at all, and its CTA leads to a 404.

**Recommended action:** Give the submit button a clear value or label, confirm the form action works in production, and provide one working quote request path from the Quote page.

**Likely cause (interpretation):** Hypothesis: the fixture form is intentionally static and unfinished.

**Potential business impact:** Fragmented quote paths could add friction for visitors trying to enquire; actual behaviour was not measured.

**Suggested AMS service:** Conversion optimisation

**Affected URLs:** http://127.0.0.1:18765/contact.html; http://127.0.0.1:18765/quote.html

**Evidence:** ev-browser-004, ev-browser-003

### trust-signals-thin: Trust signals are limited and not specific

**Severity:** low | **Confidence:** subjective | **Type:** opportunity | **Priority:** P3

The home page tells visitors to ask about licence details and insurance rather than stating them. The only testimonial is labelled as a demo, with no name or source. The quote page shows only a privacy link as a trust signal. Part of this reflects the fixture being fictional.

**Recommended action:** For a real deployment, state licence and insurance details, add attributed reviews and service guarantees, and place them near quote CTAs.

**Likely cause (interpretation):** Hypothesis: fixture content is deliberately synthetic.

**Potential business impact:** Visitors may need more reassurance before requesting a quote; effect was not measured.

**Suggested AMS service:** Conversion optimisation

**Affected URLs:** http://127.0.0.1:18765/; http://127.0.0.1:18765/about.html

**Evidence:** ev-browser-001, ev-browser-002, ev-browser-003

## Copywriting & messaging

### copy-quote-page-generic: Quote page copy is generic and the CTA is non-descriptive

**Severity:** medium | **Confidence:** subjective | **Type:** problem | **Priority:** P2

The page uses 'We do stuff', 'Quality solutions for all your needs.', 'Get in touch for more.' and a 'Click here' link. These do not state the service, area, what a quote includes or what happens next.

**Recommended action:** Rewrite with a specific H1 such as the quote service and Sydney area, describe the quote process, and use an action-led CTA label.

**Likely cause (interpretation):** Hypothesis: placeholder copy was left in place.

**Potential business impact:** Vague wording gives visitors and answer engines little to work with on the page meant to drive enquiries.

**Suggested AMS service:** Content and copywriting

**Affected URLs:** http://127.0.0.1:18765/quote.html

**Evidence:** ev-browser-003

## Security observations

### security-004: No explicit framing protection observed

**Severity:** medium | **Confidence:** confirmed | **Type:** problem | **Priority:** P2

Neither X-Frame-Options nor CSP frame-ancestors was observed.

**Recommended action:** Set an appropriate frame-ancestors policy after checking embedding needs.

**Likely cause (interpretation):** Hypothesis: the basic development web server sends no hardening headers.

**Potential business impact:** The site could potentially be framed by other origins, which raises clickjacking exposure if the production setup is the same.

**Suggested AMS service:** Website improvement

**Evidence:** ev-security-001

### siteone-004: Review content-security-policy header

**Severity:** low | **Confidence:** needs_verification | **Type:** problem | **Priority:** P3

Content-Security-Policy header is not set. It restricts resources the page can load and prevents XSS attacks. The sampled response did not include content-security-policy.

**Recommended action:** Review and test the header policy for the affected responses.

**Likely cause (interpretation):** Hypothesis: default server configuration with no hardening.

**Potential business impact:** A missing CSP reduces defence in depth against script injection if production matches.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/

**Evidence:** ev-security-001, ev-siteone-014

### siteone-005: Review server header

**Severity:** low | **Confidence:** needs_verification | **Type:** problem | **Priority:** P3

Server header is set to 'SimpleHTTP/0.6 Python/3.12.13'. It is better not to reveal the technologies used and especially their versions.

**Recommended action:** Review and test the header policy for the affected responses.

**Likely cause (interpretation):** Hypothesis: a local development server is in use.

**Potential business impact:** Version disclosure can help attackers fingerprint the stack if present in production.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/

**Evidence:** ev-siteone-015

### siteone-006: Review x-content-type-options header

**Severity:** low | **Confidence:** needs_verification | **Type:** problem | **Priority:** P3

X-Content-Type-Options header is not set. It stops MIME type sniffing and mitigates content type attacks. The sampled response did not include x-content-type-options.

**Recommended action:** Review and test the header policy for the affected responses.

**Likely cause (interpretation):** Hypothesis: default server configuration.

**Potential business impact:** Browsers may MIME-sniff content, a minor hardening gap.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/

**Evidence:** ev-security-001, ev-siteone-016

### siteone-007: Review referrer-policy header

**Severity:** low | **Confidence:** needs_verification | **Type:** problem | **Priority:** P3

Referrer-Policy header is not set. It controls referrer header sharing and enhances privacy and security. The sampled response did not include referrer-policy.

**Recommended action:** Review and test the header policy for the affected responses.

**Likely cause (interpretation):** Hypothesis: default server configuration.

**Potential business impact:** Referrer information sharing is left to browser defaults, a minor privacy gap.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/

**Evidence:** ev-security-001, ev-siteone-017

### siteone-008: Review permissions-policy header

**Severity:** low | **Confidence:** needs_verification | **Type:** problem | **Priority:** P3

Permissions-Policy header is not set. It allows enabling/disabling browser APIs and features for security.

**Recommended action:** Review and test the header policy for the affected responses.

**Likely cause (interpretation):** Hypothesis: default server configuration.

**Potential business impact:** Browser features are not explicitly restricted, a minor hardening gap.

**Suggested AMS service:** Website improvement

**Affected URLs:** http://127.0.0.1:18765/

**Evidence:** ev-siteone-018

## Prioritised recommendations

1. **P1 - Broken or unavailable URLs (siteone-001):** Repair internal links and restore or redirect unavailable pages. Evidence: ev-siteone-007.
2. **P1 - Elements must meet minimum color contrast ratio thresholds (browser-001):** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/color-contrast?application=axeAPI and verify with keyboard and assistive technology. Evidence: ev-browser-003, ev-browser-008.
3. **P1 - Images must have alternative text (browser-002):** Apply the guidance at https://dequeuniversity.com/rules/axe/4.10/image-alt?application=axeAPI and verify with keyboard and assistive technology. Evidence: ev-browser-003, ev-browser-008.
4. **P1 - Structured data phone differs from visible phone number (entity-phone-mismatch):** Confirm the correct number, then align the JSON-LD, visible text and tel: links, and check any external listings against it. Evidence: ev-browser-001, ev-browser-006.
5. **P2 - Missing H1 (siteone-003):** Add a descriptive, page-specific H1. Evidence: ev-siteone-012.
6. **P2 - Missing meta description (siteone-002):** Add a descriptive, page-specific meta description. Evidence: ev-siteone-012.
7. **P2 - Contact form submit control has no accessible label and the quote page has no form (cro-contact-form-submit):** Give the submit button a clear value or label, confirm the form action works in production, and provide one working quote request path from the Quote page. Evidence: ev-browser-004, ev-browser-003.
8. **P2 - Quote page copy is generic and the CTA is non-descriptive (copy-quote-page-generic):** Rewrite with a specific H1 such as the quote service and Sydney area, describe the quote process, and use an action-led CTA label. Evidence: ev-browser-003.
9. **P2 - LocalBusiness structured data observed only on the home page (local-schema-coverage):** Extend structured data with a complete LocalBusiness or Plumber entity, plus Service markup on the services page, and validate it. Evidence: ev-browser-001, ev-browser-002, ev-browser-003, ev-browser-004, ev-browser-005.
10. **P2 - No explicit framing protection observed (security-004):** Set an appropriate frame-ancestors policy after checking embedding needs. Evidence: ev-security-001.
11. **P3 - Trust signals are limited and not specific (trust-signals-thin):** For a real deployment, state licence and insurance details, add attributed reviews and service guarantees, and place them near quote CTAs. Evidence: ev-browser-001, ev-browser-002, ev-browser-003.
12. **P3 - Service pages offer limited answer-ready content (geo-answer-readiness):** Add concise factual sections covering what each service includes, typical process, areas served and common questions, supported by matching structured data. Evidence: ev-browser-005, ev-siteone-019.
13. **P3 - Privacy policy is an H2 anchor on the contact page (contact-privacy-heading):** Provide a dedicated, accurate privacy policy that matches the real form handling before launch. Evidence: ev-browser-004, ev-siteone-019.
14. **P3 - Review content-security-policy header (siteone-004):** Review and test the header policy for the affected responses. Evidence: ev-security-001, ev-siteone-014.
15. **P3 - Review server header (siteone-005):** Review and test the header policy for the affected responses. Evidence: ev-siteone-015.
16. **P3 - Review x-content-type-options header (siteone-006):** Review and test the header policy for the affected responses. Evidence: ev-security-001, ev-siteone-016.
17. **P3 - Review referrer-policy header (siteone-007):** Review and test the header policy for the affected responses. Evidence: ev-security-001, ev-siteone-017.
18. **P3 - Review permissions-policy header (siteone-008):** Review and test the header policy for the affected responses. Evidence: ev-siteone-018.

## Proposed implementation roadmap

### 0-30 days

- Broken or unavailable URLs (siteone-001). Evidence: ev-siteone-007.
- Elements must meet minimum color contrast ratio thresholds (browser-001). Evidence: ev-browser-003, ev-browser-008.
- Images must have alternative text (browser-002). Evidence: ev-browser-003, ev-browser-008.
- Structured data phone differs from visible phone number (entity-phone-mismatch). Evidence: ev-browser-001, ev-browser-006.
- Missing H1 (siteone-003). Evidence: ev-siteone-012.
- Missing meta description (siteone-002). Evidence: ev-siteone-012.
- Quote page copy is generic and the CTA is non-descriptive (copy-quote-page-generic). Evidence: ev-browser-003.
- Contact form submit control has no accessible label and the quote page has no form (cro-contact-form-submit). Evidence: ev-browser-004, ev-browser-003.

### 30-90 days

- LocalBusiness structured data observed only on the home page (local-schema-coverage). Evidence: ev-browser-001, ev-browser-002, ev-browser-003, ev-browser-004, ev-browser-005.
- No explicit framing protection observed (security-004). Evidence: ev-security-001.
- Review content-security-policy header (siteone-004). Evidence: ev-security-001, ev-siteone-014.
- Review x-content-type-options header (siteone-006). Evidence: ev-security-001, ev-siteone-016.
- Review referrer-policy header (siteone-007). Evidence: ev-security-001, ev-siteone-017.
- Review permissions-policy header (siteone-008). Evidence: ev-siteone-018.
- Review server header (siteone-005). Evidence: ev-siteone-015.
- Trust signals are limited and not specific (trust-signals-thin). Evidence: ev-browser-001, ev-browser-002, ev-browser-003.

### 90+ days

- Service pages offer limited answer-ready content (geo-answer-readiness). Evidence: ev-browser-005, ev-siteone-019.
- Privacy policy is an H2 anchor on the contact page (contact-privacy-heading). Evidence: ev-browser-004, ev-siteone-019.

## Data sources, missing data & limitations

| Source | Status | Coverage or limitation |
| --- | --- | --- |
| siteone | ok | Completed within the stated scope |
| browser | ok | Completed within the stated scope |
| domain\_email | skipped | Local/private target has no public domain audit |
| security | ok | TLS not checked: supplied target uses HTTP; Local fixture security headers are not evidence about production hosting |
| performance | skipped | PageSpeed/CrUX cannot access localhost |
| observatory | skipped | Observatory cannot scan local targets |
| dataforseo | skipped | Missing DataForSEO credentials; disabled |
| gsc | skipped | GSC requires --connect gsc and credentials; missing connection or credentials |

- Crawl coverage: 5 successful HTML pages within a cap of 30 URLs (including assets).
- Browser coverage: 10 page/viewport observations, with at most six key pages at desktop and mobile sizes. Form structure was inspected. Automated accessibility checks are not a full accessibility audit.
- Observations are a point-in-time sample. Form submission, booking, payment and account journeys were not exercised.
- AI search visibility was **NOT measured**. GEO findings concern content structure, answer readiness and crawlability only. No A/B testing or behavioural data was collected.
- Confidence labels: **confirmed** means directly observed by a deterministic check; **needs_verification** means a hypothesis or incomplete check; **subjective** means editorial or UX judgement. Likely causes and business impacts are interpretations, not measured outcomes.
- AI analysis: ok via claude. 
- domain\_email: no successful evidence collection; category was not assessed
- performance: no successful evidence collection; category was not assessed
- Local fixture demonstration: observations do not describe a live business

### Manual verification items

- entity-phone-mismatch: Structured data phone differs from visible phone number. Evidence: ev-browser-001, ev-browser-006.
- copy-quote-page-generic: Quote page copy is generic and the CTA is non-descriptive. Evidence: ev-browser-003.
- cro-contact-form-submit: Contact form submit control has no accessible label and the quote page has no form. Evidence: ev-browser-004, ev-browser-003.
- local-schema-coverage: LocalBusiness structured data observed only on the home page. Evidence: ev-browser-001, ev-browser-002, ev-browser-003, ev-browser-004, ev-browser-005.
- trust-signals-thin: Trust signals are limited and not specific. Evidence: ev-browser-001, ev-browser-002, ev-browser-003.
- contact-privacy-heading: Privacy policy is an H2 anchor on the contact page. Evidence: ev-browser-004, ev-siteone-019.
- geo-answer-readiness: Service pages offer limited answer-ready content. Evidence: ev-browser-005, ev-siteone-019.
- siteone-004: Review content-security-policy header. Evidence: ev-security-001, ev-siteone-014.
- siteone-005: Review server header. Evidence: ev-siteone-015.
- siteone-006: Review x-content-type-options header. Evidence: ev-security-001, ev-siteone-016.
- siteone-007: Review referrer-policy header. Evidence: ev-security-001, ev-siteone-017.
- siteone-008: Review permissions-policy header. Evidence: ev-siteone-018.

### Future capabilities outside this assessment

- **Elmo AI visibility:** Measured AI search visibility. Requires Provider integration and measurement budget. Not implemented or measured.
- **Houtini SEO Audit:** Additional SEO audit. Requires Provider integration. Not implemented or measured.
- **OpenSEO:** Additional SEO tooling. Requires Provider integration. Not implemented or measured.
- **Google Business Profile API:** Verified listing data. Requires OAuth and listing access. Not implemented or measured.
- **GA4:** Analytics. Requires Read-only property access. Not implemented or measured.
- **WP-CLI / Site Health:** WordPress configuration. Requires Approved hosting access. Not implemented or measured.
- **Google Workspace (ScubaGoggles):** Workspace posture. Requires Approved tenant access. Not implemented or measured.
- **M365 (ScubaGear):** Microsoft tenant posture. Requires Approved tenant access. Not implemented or measured.
- **Social/platform SEO:** Platform discovery. Requires Account/API access. Not implemented or measured.
- **Digital PR:** Authority and earned media. Requires Research scope and source access. Not implemented or measured.
- **A/B testing:** Controlled conversion experiments. Requires Consent, traffic and experiment design. Not implemented or measured.
- **Heatmaps/behaviour:** Behavioural analysis. Requires Consent and analytics access. Not implemented or measured.

## Appendix: evidence index

The machine-readable evidence.json retains the observed snippets and links to raw source artefacts. Evidence IDs below are the stable references used throughout this report.

| Evidence ID | Source | Observation | URL |
| --- | --- | --- | --- |
| ev-siteone-001 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/ |
| ev-siteone-002 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/contact.html |
| ev-siteone-003 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/style.css |
| ev-siteone-004 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/about.html |
| ev-siteone-005 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/services.html |
| ev-siteone-006 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/quote.html |
| ev-siteone-007 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/missing-offer.html |
| ev-siteone-008 | siteone | SiteOne HTTP response | http://127.0.0.1:18765/work.svg |
| ev-siteone-009 | siteone | SiteOne SEO metadata | http://127.0.0.1:18765/ |
| ev-siteone-010 | siteone | SiteOne SEO metadata | http://127.0.0.1:18765/about.html |
| ev-siteone-011 | siteone | SiteOne SEO metadata | http://127.0.0.1:18765/contact.html |
| ev-siteone-012 | siteone | SiteOne SEO metadata | http://127.0.0.1:18765/quote.html |
| ev-siteone-013 | siteone | SiteOne SEO metadata | http://127.0.0.1:18765/services.html |
| ev-siteone-014 | siteone | SiteOne header analysis (crawl aggregate) | http://127.0.0.1:18765/ |
| ev-siteone-015 | siteone | SiteOne header analysis (crawl aggregate) | http://127.0.0.1:18765/ |
| ev-siteone-016 | siteone | SiteOne header analysis (crawl aggregate) | http://127.0.0.1:18765/ |
| ev-siteone-017 | siteone | SiteOne header analysis (crawl aggregate) | http://127.0.0.1:18765/ |
| ev-siteone-018 | siteone | SiteOne header analysis (crawl aggregate) | http://127.0.0.1:18765/ |
| ev-siteone-019 | siteone | SiteOne seo-headings table | http://127.0.0.1:18765/ |
| ev-siteone-020 | siteone | SiteOne 404 table | http://127.0.0.1:18765/ |
| ev-siteone-021 | siteone | SiteOne headers table | http://127.0.0.1:18765/ |
| ev-siteone-022 | siteone | SiteOne headers-values table | http://127.0.0.1:18765/ |
| ev-browser-001 | browser | Rendered page evidence (desktop) | http://127.0.0.1:18765/ |
| ev-browser-002 | browser | Rendered page evidence (desktop) | http://127.0.0.1:18765/about.html |
| ev-browser-003 | browser | Rendered page evidence (desktop) | http://127.0.0.1:18765/quote.html |
| ev-browser-004 | browser | Rendered page evidence (desktop) | http://127.0.0.1:18765/contact.html |
| ev-browser-005 | browser | Rendered page evidence (desktop) | http://127.0.0.1:18765/services.html |
| ev-browser-006 | browser | Rendered page evidence (mobile) | http://127.0.0.1:18765/ |
| ev-browser-007 | browser | Rendered page evidence (mobile) | http://127.0.0.1:18765/about.html |
| ev-browser-008 | browser | Rendered page evidence (mobile) | http://127.0.0.1:18765/quote.html |
| ev-browser-009 | browser | Rendered page evidence (mobile) | http://127.0.0.1:18765/contact.html |
| ev-browser-010 | browser | Rendered page evidence (mobile) | http://127.0.0.1:18765/services.html |
| ev-security-001 | security | HTTP response security headers | http://127.0.0.1:18765/ |
