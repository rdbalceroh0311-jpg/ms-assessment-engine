import argparse
import math
import os
from pathlib import Path

from dotenv import dotenv_values

from ams_assess.models import CATEGORIES, Context, create_run_dir, normalize_target
from ams_assess.modules import FUTURE, REGISTRY
from ams_assess.report import render
from ams_assess.runner import run

ROOT = Path(__file__).resolve().parent.parent
ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "PAGESPEED_API_KEY",
    "CRUX_API_KEY",
    "GOOGLE_API_KEY",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AMS_GSC_TOKEN_FILE",
    "AMS_GSC_PROPERTY",
]


def config() -> dict[str, str]:
    loaded = {}
    for file in [Path.home() / ".config/ams-assess/credentials.env", ROOT / ".env"]:
        if file.is_file():
            loaded.update(
                {
                    k: v
                    for k, v in dotenv_values(file, interpolate=False).items()
                    if k in ENV_KEYS and v is not None
                }
            )
    loaded.update({k: os.environ[k] for k in ENV_KEYS if k in os.environ})
    return loaded


def csv(value: str) -> set[str]:
    return {s.strip() for s in value.split(",") if s.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ams-assess", description="AMS evidence-backed assessment"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("modules", help="List available, connected and future capabilities")
    assess = commands.add_parser("run", help="Assess a website without modifying it")
    assess.add_argument("--customer", required=True)
    assess.add_argument("--customer-name")
    assess.add_argument("--target", required=True)
    assess.add_argument("--categories", default=",".join(CATEGORIES))
    assess.add_argument("--connect", action="append", choices=["gsc"], default=[])
    assess.add_argument("--max-pages", type=int, default=100)
    assess.add_argument("--dataforseo-max-usd", type=float)
    assess.add_argument("--no-ai", action="store_true")
    assess.add_argument("--formats", default="md,html,pdf")
    report = commands.add_parser("report", help="Regenerate reports from one existing run")
    report.add_argument("run_dir", type=Path)
    report.add_argument("--formats", default="md,html,pdf")
    args = parser.parse_args(argv)
    if args.command == "modules":
        for module in REGISTRY.values():
            print(
                f"{module.name}: available | {','.join(sorted(module.categories))} | "
                f"requires: {', '.join(module.requires)}"
            )
        for name, description, requirement, _ in FUTURE:
            print(f"{name}: future | {description} | requires: {requirement}")
        return 0
    formats = csv(args.formats)
    if not formats or not formats <= {"md", "html", "pdf"}:
        parser.error("--formats must be a nonempty subset of md,html,pdf")
    if args.command == "report":
        try:
            outputs = render(args.run_dir.resolve(), formats)
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot render run: {type(exc).__name__}")
        print(", ".join(outputs))
        return 0
    categories = csv(args.categories)
    if not categories or not categories <= CATEGORIES.keys():
        parser.error("Unknown or empty categories; choose from " + ",".join(CATEGORIES))
    if not 1 <= args.max_pages <= 10000:
        parser.error("--max-pages must be between 1 and 10000")
    if args.dataforseo_max_usd is not None and (
        not math.isfinite(args.dataforseo_max_usd) or args.dataforseo_max_usd <= 0
    ):
        parser.error("--dataforseo-max-usd must be a finite positive amount")
    try:
        target = normalize_target(args.target)
        directory = create_run_dir(ROOT / "data/customers", args.customer)
    except ValueError as exc:
        parser.error(str(exc))
    ctx = Context(
        customer_id=args.customer,
        customer_name=args.customer_name or args.customer,
        target=target,
        categories=categories,
        connections=set(args.connect),
        config=config(),
        run_dir=directory,
        tools_dir=ROOT / "tools",
        max_pages=args.max_pages,
        dataforseo_max_usd=args.dataforseo_max_usd,
    )
    data = run(ctx, no_ai=args.no_ai, formats=formats)
    print(f"Run directory: {directory}\nStatus: {data['status']}; {len(data['findings'])} findings")
    return 1 if data["status"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
