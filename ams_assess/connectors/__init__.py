"""Add one provider module and its declaration here; consumers discover the contract."""

from ams_assess.connectors.bing_wmt import CONNECTOR as BING_WMT
from ams_assess.connectors.ga4 import CONNECTOR as GA4
from ams_assess.connectors.gsc import CONNECTOR as GSC

REGISTRY = {connector.key: connector for connector in (GA4, GSC, BING_WMT)}
