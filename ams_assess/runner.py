from datetime import UTC, datetime
from typing import Any

from ams_assess import coverage, outcomes, store
from ams_assess.ai import analyse
from ams_assess.consolidate import consolidate, consolidate_security_headers
from ams_assess.lifecycle import scoped
from ams_assess.models import (
    CATEGORIES,
    DISCOVERY_CATEGORY,
    Context,
    Evidence,
    ModuleResult,
    write_json,
)
from ams_assess.modules import FUTURE, REGISTRY, domain_email, presence
from ams_assess.report import render


def run(ctx: Context, no_ai: bool = False, formats: set[str] | None = None) -> dict[str, Any]:
    results = ctx.results = []
    for name, module in REGISTRY.items():
        if not ctx.categories & module.categories:
            continue
        print(f"[{name}] collecting", flush=True)
        try:
            result = module.run(ctx)
            if result.name != name or len({e.id for e in result.evidence}) != len(result.evidence):
                raise ValueError("Invalid module identity or duplicate evidence")
            if any(e.source != name or not e.id.startswith(f"ev-{name}-") for e in result.evidence):
                raise ValueError("Evidence must belong to its source module")
            outcomes.record(ctx, result)
            result.findings = consolidate(result.findings, result.evidence)
        except Exception as exc:
            # Exception messages may embed credentials/URLs. Keep raw secrets out of reports/logs.
            result = ModuleResult(
                name=name,
                status="error",
                reason=f"Module failed ({type(exc).__name__}); remaining checks continued",
            )
            existing_raw = ctx.run_dir / f"raw/{name}.json"
            result.raw_paths = [f"raw/{name}.json"] if existing_raw.exists() else []
            result.raw_paths.append(ctx.raw(f"{name}-error", {"reason": result.reason}))
        if not result.outcomes:
            outcomes.record(ctx, result)
        if not result.raw_paths:
            result.raw_paths = [ctx.raw(name, {"status": result.status, "reason": result.reason})]
        if name == "siteone" and result.status != "ok":
            # Give the narrative a citation for failed coverage, including the zero-page
            # quantity. A status in the prompt alone is not cited measurement evidence.
            result.add_evidence(
                ctx.target,
                "SiteOne crawl coverage unavailable",
                {
                    "status": result.status,
                    "reason": result.reason,
                    "successful_html_pages": len(ctx.pages),
                },
            )
        result.findings = [f for f in result.findings if f.category in ctx.categories]
        results.append(result)
        print(
            f"[{name}] {result.status}" + (f": {result.reason}" if result.reason else ""),
            flush=True,
        )
    evidence = [e for result in results for e in result.evidence]
    findings = consolidate([f for result in results for f in result.findings], evidence)
    findings = scoped(
        consolidate_security_headers(findings, evidence), ctx.site_url, ctx.customer_id
    )
    write_json(ctx.run_dir / "evidence.json", [e.model_dump() for e in evidence])
    print("[ai] analysing" if not no_ai else "[ai] disabled", flush=True)
    ai, findings = analyse(ctx, findings, evidence, no_ai)
    findings = consolidate(findings, evidence)
    assessed = sorted(
        {
            category
            for r in results
            if r.evidence and r.status == "ok"
            for category in REGISTRY[r.name].categories
            if category in ctx.categories
        }
    )
    # Findings from unassessed categories must not reach recommendations or the roadmap.
    findings = [f for f in findings if f.category in assessed or f.category == DISCOVERY_CATEGORY]
    limitations = [
        f"{c}: no successful evidence collection; category was not assessed"
        for c in sorted(ctx.categories - set(assessed))
    ]
    if "domain_email" in ctx.categories:
        limitations.append(domain_email.DELIVERABILITY_NOTE)
    if ctx.local:
        limitations.append(
            "Local fixture demonstration: observations do not describe a live business"
        )
    heuristics = ctx.categories & {"local_seo", "entity", "cro_ux", "copywriting", "geo"}
    if ai["status"] != "ok" and heuristics:
        limitations.append(
            "AI interpretation is unavailable for "
            + ", ".join(CATEGORIES[c] for c in sorted(heuristics))
            + "; collect or review page evidence manually"
        )
    future = [
        {"name": n, "description": d, "requirement": req, "status": "future"}
        for n, d, req, cats in FUTURE
        if cats & ctx.categories
    ]
    website_coverage = coverage.collect(ctx)
    data = {
        "schema_version": 1,
        "customer_id": ctx.customer_id,
        "customer_name": ctx.customer_name,
        "profile": {"fields": ctx.profile} if ctx.profile else None,
        "target": ctx.target,
        "resolved_url": ctx.site_url,
        "created_at": datetime.now(UTC).isoformat(),
        "selected_categories": sorted(ctx.categories),
        "assessed_categories": assessed,
        "max_pages": ctx.max_pages,
        "website_coverage": website_coverage,
        "crawl_assets_excluded": any(r.name == "siteone" and r.collection_started for r in results),
        "outcomes": [
            o.model_dump() for r in results for o in r.outcomes if o.category in ctx.categories
        ],
        "pages": ctx.pages,
        "modules": [r.model_dump() for r in results],
        "ai": ai,
        "findings": [f.model_dump() for f in findings],
        "limitations": limitations,
        "future": future,
        "report_files": [],
        "status": "complete" if evidence else "incomplete",
    }
    if "domain_email" in ctx.categories:
        data["domain_email_health"] = domain_email.health_summary(results, ctx.categories)
    if "presence" in ctx.categories:
        data["digital_presence"] = presence.summary(results, ctx)
    if any(r.status != "ok" or r.reason for r in results) or ai["status"] == "error":
        data["status"] = "partial" if evidence else "incomplete"
    from ams_assess import register

    try:
        previous = store.load(ctx.store_root, ctx.customer_id) if ctx.store_root else None
    except (OSError, ValueError):
        # Keep the existing contract: a bad/busy store never discards completed run output.
        # The merge below reports the store failure and the CLI's existing exit status.
        previous = None
    if previous:
        register.refresh(previous)
    data["systems_register"] = register.snapshot(data, evidence, previous)
    write_json(ctx.run_dir / "findings.json", data["findings"])
    write_json(ctx.run_dir / "results.json", data)
    try:
        data["report_files"] = render(ctx.run_dir, formats)
    except Exception as exc:
        data["status"] = "incomplete"
        data["limitations"].append(f"Report generation failed ({type(exc).__name__})")
        data["report_files"] = [
            f"report.{ext}"
            for ext in (formats or {"md", "html", "pdf"})
            if (ctx.run_dir / f"report.{ext}").is_file()
        ]
    write_json(ctx.run_dir / "results.json", data)
    # External collectors also write raw files/screenshots; keep the whole run owner-only.
    for path in ctx.run_dir.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    if ctx.store_root is not None and not merge_into_store(ctx, data, evidence, results):
        # Only on the returned dict: results.json keeps its per-run contract.
        data["store_status"] = "failed"
    return data


def merge_into_store(
    ctx: Context, data: dict[str, Any], evidence: list[Evidence], results: list[ModuleResult]
) -> bool:
    """Enrich the customer's evolving assessment; the per-run directory is already complete."""
    ok = {r.name for r in results if r.status == "ok" and r.evidence}
    stages = [
        stage
        for stage, present in (("public", bool(ok - {"gsc"})), ("connected", "gsc" in ok))
        if present
    ]
    try:
        assert ctx.store_root is not None
        store.merge_run(
            ctx.store_root,
            ctx.customer_id,
            ctx.customer_name,
            data,
            evidence,
            ctx.run_dir.name,
            stages,
        )
        return True
    except Exception as exc:
        # The run directory stays the source of truth; the caller sees store_status=failed.
        print(f"[store] merge failed ({type(exc).__name__}); run directory is intact", flush=True)
        return False
