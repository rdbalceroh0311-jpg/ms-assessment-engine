from types import ModuleType

from ams_assess.connectors import REGISTRY as CONNECTORS
from ams_assess.connectors.adapter import ConnectorModule
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
REGISTRY: dict[str, ModuleType | ConnectorModule] = {
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

REGISTRY.update(
    {key: ConnectorModule(spec) for key, spec in CONNECTORS.items() if key not in REGISTRY}
)

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
        "WordPress Site Health",
        "Site Health status checks (the WP-CLI connector is available)",
        "Approved hosting access",
        {"technical", "security"},
    ),
    (
        "ScubaGoggles/ScubaGear posture baseline",
        "CISA SCuBA baseline posture (Workspace and M365 connectors are available)",
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
