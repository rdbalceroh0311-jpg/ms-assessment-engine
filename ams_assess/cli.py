import argparse
import json
import math
import os
import re
from pathlib import Path

from dotenv import dotenv_values
from google_auth_oauthlib.flow import InstalledAppFlow

from ams_assess import store
from ams_assess.discovery import load_notes
from ams_assess.models import CATEGORIES, Context, create_run_dir, normalize_target
from ams_assess.modules import FUTURE, REGISTRY, gsc
from ams_assess.report import render, render_customer
from ams_assess.runner import run

ROOT = Path(__file__).resolve().parent.parent
ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "PAGESPEED_API_KEY",
    "CRUX_API_KEY",
    "GOOGLE_API_KEY",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "DATAFORSEO_LOCATION_CODE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AMS_GSC_CLIENT_SECRETS",
    "AMS_GSC_TOKEN_FILE",
    "AMS_GSC_PROPERTY",
    "AMS_DKIM_SELECTORS",
    "SPAMHAUS_DQS_KEY",
    "AMS_ACCESS_EMAIL",
]


def data_root() -> Path:
    return ROOT / "data/customers"


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


def gsc_auth(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        settings = config()
        client_value = args.client_secrets or settings.get("AMS_GSC_CLIENT_SECRETS")
        client = Path(client_value).expanduser().resolve() if client_value else None
        token = (
            Path(
                args.token_file
                or settings.get("AMS_GSC_TOKEN_FILE")
                or "~/.config/ams-assess/gsc-token.json"
            )
            .expanduser()
            .resolve()
        )
        if token.is_relative_to(ROOT.resolve()):
            parser.error("GSC token file must be outside the git repository root")
        if client and (
            token == client or (token.exists() and client.exists() and token.samefile(client))
        ):
            parser.error("GSC token file must differ from the client-secrets file")
        if not args.verify:
            if client is None:
                parser.error("--client-secrets or AMS_GSC_CLIENT_SECRETS is required")
            client_data = json.loads(client.read_text())
            if (
                not isinstance(client_data, dict)
                or not isinstance(client_data.get("installed"), dict)
                or "web" in client_data
            ):
                parser.error("GSC client secrets must be a Google Desktop OAuth JSON (installed)")
            auth = InstalledAppFlow.from_client_secrets_file(str(client), scopes=[gsc.SCOPE])
            creds = auth.run_local_server(
                port=0,
                open_browser=True,
                access_type="offline",
                prompt="consent",
                # The default URL message includes the client ID; keep it out of output.
                authorization_prompt_message="",
            )
            if not creds.refresh_token:
                parser.error("GSC authorization returned no refresh token; consent is required")
            gsc.save_token(token, creds)
        sites = gsc.verify_token(token)
    except Exception:
        # OAuth and HTTP exceptions can contain client secrets or tokens.
        parser.error(
            "GSC authorization or verification failed; check OAuth files and property access"
        )
    for site in sites:
        print(f"{site['siteUrl']} {site['permissionLevel']}")
    if not sites:
        print("no properties")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ams-assess", description="AMS evidence-backed assessment"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("modules", help="List available, connected and future capabilities")
    auth = commands.add_parser(
        "gsc-auth", help="Authorize or verify read-only Search Console access"
    )
    auth.add_argument(
        "--client-secrets", type=Path, help="Desktop OAuth JSON; AMS_GSC_CLIENT_SECRETS"
    )
    auth.add_argument(
        "--token-file", type=Path, help="AMS_GSC_TOKEN_FILE or ~/.config/ams-assess/gsc-token.json"
    )
    auth.add_argument(
        "--verify", action="store_true", help="Verify the existing token without a browser"
    )
    assess = commands.add_parser("run", help="Assess a website without modifying it")
    assess.add_argument("--customer", required=True)
    assess.add_argument("--customer-name")
    assess.add_argument("--target", required=True)
    assess.add_argument("--categories", default=",".join(CATEGORIES))
    assess.add_argument("--connect", action="append", choices=["gsc"], default=[])
    assess.add_argument("--max-pages", type=int, default=100)
    assess.add_argument("--dataforseo-max-usd", type=float)
    assess.add_argument("--dataforseo-location-code", type=int)
    assess.add_argument("--dkim-selectors", help="Comma-separated DKIM selectors to check")
    assess.add_argument(
        "--mail-tls-check",
        action="store_true",
        help="Opt in to a bounded EHLO/STARTTLS probe of up to two MX hosts on port 25",
    )
    assess.add_argument("--no-ai", action="store_true")
    assess.add_argument("--formats", default="md,html,pdf")
    discover = commands.add_parser(
        "discover", help="Ingest structured business-discovery notes into a customer's assessment"
    )
    discover.add_argument("--customer", required=True)
    discover.add_argument("--customer-name")
    discover.add_argument("--notes", required=True, type=Path, help="Discovery notes JSON file")
    report = commands.add_parser(
        "report", help="Regenerate reports from one existing run or a customer's whole assessment"
    )
    report.add_argument("run_dir", type=Path, nargs="?")
    report.add_argument("--customer", help="Render from the customer store instead of a run")
    report.add_argument("--formats", default="md,html,pdf")
    report.add_argument(
        "--from-md",
        type=Path,
        help="With --customer: a human-edited report.md to save as a new revision and re-render",
    )
    args = parser.parse_args(argv)
    if args.command == "gsc-auth":
        return gsc_auth(args, parser)
    if args.command == "modules":
        for module in REGISTRY.values():
            print(
                f"{module.name}: available | {','.join(sorted(module.categories))} | "
                f"requires: {', '.join(module.requires)}"
            )
        for name, description, requirement, _ in FUTURE:
            print(f"{name}: future | {description} | requires: {requirement}")
        return 0
    if args.command == "discover":
        try:
            notes = load_notes(args.notes)
            _, counts = store.add_discovery(
                data_root(), args.customer, args.customer_name or args.customer, notes
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else "Cannot read notes")
        # Discovery needs no website evidence, so it is complete (and exits 0) on its own.
        print(
            f"Status: complete (discovery); {counts['statements']} statements "
            f"({counts['new']} new), {counts['findings']} findings"
        )
        return 0
    formats = csv(args.formats)
    if not formats or not formats <= {"md", "html", "pdf"}:
        parser.error("--formats must be a nonempty subset of md,html,pdf")
    if args.command == "report":
        if bool(args.run_dir) == bool(args.customer):
            parser.error("Provide either a run directory or --customer")
        if args.from_md and not args.customer:
            parser.error("--from-md requires --customer")
        try:
            if args.customer:
                outputs = render_customer(data_root(), args.customer, formats, args.from_md)
            else:
                outputs = render(args.run_dir.resolve(), formats)
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot render: {type(exc).__name__}: {exc}")
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
    settings = config()
    if args.dataforseo_location_code is not None:
        settings["DATAFORSEO_LOCATION_CODE"] = str(args.dataforseo_location_code)
    try:
        int(settings.get("DATAFORSEO_LOCATION_CODE", "2036"))
    except ValueError:
        parser.error("DATAFORSEO_LOCATION_CODE must be an integer")
    if args.dkim_selectors is not None:
        settings["AMS_DKIM_SELECTORS"] = args.dkim_selectors
    if not all(
        re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*", s)
        for s in csv(settings.get("AMS_DKIM_SELECTORS", ""))
    ):
        parser.error("DKIM selectors must be DNS labels separated by commas")
    if args.mail_tls_check:
        settings["AMS_MAIL_TLS_CHECK"] = "1"
    try:
        target = normalize_target(args.target)
        directory = create_run_dir(data_root(), args.customer)
    except ValueError as exc:
        parser.error(str(exc))
    ctx = Context(
        customer_id=args.customer,
        customer_name=args.customer_name or args.customer,
        target=target,
        categories=categories,
        connections=set(args.connect),
        config=settings,
        run_dir=directory,
        tools_dir=ROOT / "tools",
        store_root=data_root(),
        max_pages=args.max_pages,
        dataforseo_max_usd=args.dataforseo_max_usd,
    )
    data = run(ctx, no_ai=args.no_ai, formats=formats)
    print(f"Run directory: {directory}\nStatus: {data['status']}; {len(data['findings'])} findings")
    if data.get("store_status") == "failed":
        print(
            f"Store: merge failed; the run directory was written with status {data['status']} "
            "but the customer store was not updated"
        )
        return 3
    return 1 if data["status"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
