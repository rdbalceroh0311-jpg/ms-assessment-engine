"""Add one provider module and its declaration here; consumers discover the contract."""

from ams_assess.connectors.ga4 import CONNECTOR as GA4
from ams_assess.connectors.gsc import CONNECTOR as GSC
from ams_assess.connectors.m365 import CONNECTOR as M365
from ams_assess.connectors.workspace import CONNECTOR as WORKSPACE
from ams_assess.connectors.wp_cli import CONNECTOR as WP_CLI

REGISTRY = {connector.key: connector for connector in (GA4, GSC, M365, WORKSPACE, WP_CLI)}
