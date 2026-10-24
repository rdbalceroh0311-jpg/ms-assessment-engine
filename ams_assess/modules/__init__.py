from ams_assess.modules import (
    browser,
    dataforseo,
    domain_email,
    gsc,
    observatory,
    performance,
    presence,
    security,
    site_checks,
    siteone,
)

# Order is intentional: downstream browser evidence reuses the one crawler's page list, and
# domain_email, presence and site_checks reuse evidence the earlier modules collected.
REGISTRY = {
    module.name: module
    for module in [
        siteone,
        browser,
        security,
        domain_email,
        performance,
        observatory,
        dataforseo,
        gsc,
        presence,
        site_checks,
    ]
}

FUTURE = [
    (
        "Elmo AI visibility",
        "Measured AI search visibility",
        "Provider integration and measurement budget",
        {"geo"},
    ),
    ("Houtini SEO Audit", "Additional SEO audit", "Provider integration", {"seo"}),
    ("OpenSEO", "Additional SEO tooling", "Provider integration", {"seo"}),
    (
        "Google Business Profile API",
        "Verified listing data",
        "OAuth and listing access",
        {"local_seo", "entity"},
    ),
    ("GA4", "Analytics", "Read-only property access", {"cro_ux", "performance"}),
    (
        "WP-CLI / Site Health",
        "WordPress configuration",
        "Approved hosting access",
        {"technical", "security"},
    ),
    (
        "Google Workspace (ScubaGoggles)",
        "Workspace posture",
        "Approved tenant access",
        {"domain_email", "security"},
    ),
    (
        "M365 (ScubaGear)",
        "Microsoft tenant posture",
        "Approved tenant access",
        {"domain_email", "security"},
    ),
    ("Social/platform SEO", "Platform discovery", "Account/API access", {"seo", "entity"}),
    ("Digital PR", "Authority and earned media", "Research scope and source access", {"entity"}),
    (
        "A/B testing",
        "Controlled conversion experiments",
        "Consent, traffic and experiment design",
        {"cro_ux", "copywriting"},
    ),
    ("Heatmaps/behaviour", "Behavioural analysis", "Consent and analytics access", {"cro_ux"}),
]
