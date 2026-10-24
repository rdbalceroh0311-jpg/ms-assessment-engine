"""Add one provider module and its declaration here; consumers discover the contract."""

from ams_assess.connectors.ga4 import CONNECTOR as GA4
from ams_assess.connectors.gsc import CONNECTOR as GSC
from ams_assess.connectors.m365 import CONNECTOR as M365

REGISTRY = {connector.key: connector for connector in (GA4, GSC, M365)}
