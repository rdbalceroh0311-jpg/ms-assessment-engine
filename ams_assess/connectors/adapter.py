"""Present registered connectors through the existing assessment module registry."""

from ams_assess.connectors.core import Connector
from ams_assess.models import Context, ModuleResult


class ConnectorModule:
    def __init__(self, spec: Connector):
        self.spec = spec
        self.name = spec.key
        self.categories = spec.categories
        self.requires = [f"--connect {spec.key}", "explicit resource binding", *spec.scopes]

    def run(self, ctx: Context) -> ModuleResult:
        from ams_assess import store
        from ams_assess.connectors.service import prepare

        if self.name not in ctx.connections:
            return ModuleResult(name=self.name, status="skipped", reason="Connector not selected")
        if ctx.store_root is None:
            return ModuleResult(name=self.name, status="skipped", reason="Customer store required")
        current = store.load(ctx.store_root, ctx.customer_id) or store.new_store(
            ctx.customer_id, ctx.customer_name
        )
        public = [e for r in ctx.results for e in r.evidence]
        findings = [f.model_dump() for r in ctx.results for f in r.findings]
        current.setdefault("profile", {"fields": {"websites": [ctx.target]}})
        result, state = prepare(
            current, self.name, collect=True, public_evidence=public, public_findings=findings
        )
        ctx.connector_results[self.name] = state
        return result
