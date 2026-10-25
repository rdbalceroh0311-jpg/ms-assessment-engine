import argparse
import json
import math
import os
import re
from pathlib import Path

from ams_assess import authorisation, register, store, subproc
from ams_assess.connectors import REGISTRY as CONNECTORS
from ams_assess.connectors.auth import google_desktop_flow
from ams_assess.connectors.commands import add_parsers, safe_dispatch
from ams_assess.discovery import load_notes
from ams_assess.models import (
    CATEGORIES,
    Context,
    create_run_dir,
    customer_path,
    normalize_target,
)
from ams_assess.modules import FUTURE, REGISTRY, gsc, performance
from ams_assess.platforms import BUYING_MODELS, INDUSTRIES
from ams_assess.presence_discovery import MAX_KEYWORDS, keyword_problem
from ams_assess.profile import Location, Profile, load_verifications, read_json
from ams_assess.report import render, render_customer
from ams_assess.runner import run
from ams_assess.settings import load as load_settings

ROOT = Path(__file__).resolve().parent.parent


def data_root() -> Path:
    return ROOT / "data/customers"


def config() -> dict[str, str]:
    return load_settings()


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
            creds = google_desktop_flow(client, (gsc.SCOPE,))
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


def profile_changes(args: argparse.Namespace) -> dict:
    """Fields to set: the --file profile first, then any field flags on top of it."""
    changes = {}
    if args.file:
        loaded = read_json(args.file, "Profile")
        if not isinstance(loaded, dict) or set(loaded) - Profile.model_fields.keys():
            raise ValueError(
                "Profile file must be an object with fields: " + ", ".join(Profile.model_fields)
            )
        changes.update(loaded)
    flags = {
        "name": args.name,
        "aliases": args.alias,
        "websites": args.website,
        "abn": args.abn,
        "industry": args.industry,
        "buying_model": args.buying_model,
        "service_area": args.service_area,
        "phones": args.phone,
        "locations": [Location.parse(v).model_dump() for v in args.location]
        if args.location
        else None,
    }
    changes.update({k: v for k, v in flags.items() if v is not None})
    return changes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ams-assess", description="AMS evidence-backed assessment"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add_parsers(commands)
    commands.add_parser("modules", help="List available, connected and future capabilities")
    interface = commands.add_parser("ui", help="Local customer review interface (Ctrl-C to stop)")
    interface.add_argument("--port", type=int, default=8765)
    register_cmd = commands.add_parser("register", help="Customer systems and presence register")
    register_sub = register_cmd.add_subparsers(dest="register_command", required=True)
    show = register_sub.add_parser("show")
    show.add_argument("--customer", required=True)
    edit = register_sub.add_parser("set")
    edit.add_argument("--customer", required=True)
    edit.add_argument("--platform", required=True, choices=register.PLATFORMS)
    edit.add_argument("--site", help="Existing website scope; omitted/empty reuses a sole entry")
    edit.add_argument(
        "--location", default="", help="Location scope; a new nonempty value creates it"
    )
    remove = register_sub.add_parser("remove", help="Remove an exact scoped row; retain history")
    remove.add_argument("--customer", required=True)
    remove.add_argument("--platform", required=True, choices=register.PLATFORMS)
    remove.add_argument("--site", required=True, help="Exact site; empty string for customer-wide")
    remove.add_argument("--location", default="", help="Exact location; defaults to empty")
    remove.add_argument("--who", default="local operator")
    for field, choices in register.CHOICES.items():
        edit.add_argument(
            "--access" if field == "ams_access" else "--" + field, dest=field, choices=choices
        )
    for field in (
        "evidence_url",
        "identifier",
        "method",
        "note",
        "next_action",
        "responsible",
        "tested",
        "who",
        "date",
    ):
        edit.add_argument("--" + field.replace("_", "-"))
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
    assess.add_argument(
        "--connect", action="append", choices=sorted({"gsc", *CONNECTORS}), default=[]
    )
    assess.add_argument("--max-pages", type=int, default=100)
    assess.add_argument(
        "--browser-pages",
        type=int,
        default=6,
        help="Maximum successful crawl pages inspected in the browser (1-50)",
    )
    assess.add_argument(
        "--wp-inventory", type=Path, help="Authorised WordPress URL inventory export (JSON or CSV)"
    )
    assess.add_argument(
        "--performance-key-pages",
        type=int,
        default=3,
        help="Maximum key pages in addition to homepage (0-10); each gets mobile + desktop PSI",
    )
    assess.add_argument(
        "--performance-page",
        action="append",
        default=[],
        help="Representative page URL (repeatable, same origin); prioritised over crawled pages",
    )
    assess.add_argument("--dataforseo-max-usd", type=float)
    assess.add_argument("--dataforseo-location-code", type=int)
    assess.add_argument(
        "--keywords",
        help=f"Up to {MAX_KEYWORDS} comma-separated keywords for organic position samples "
        "(paid; requires --dataforseo-max-usd)",
    )
    assess.add_argument("--dkim-selectors", help="Comma-separated DKIM selectors to check")
    assess.add_argument(
        "--mail-tls-check",
        action="store_true",
        help="Opt in to a bounded EHLO/STARTTLS probe of up to two MX hosts on port 25",
    )
    assess.add_argument("--no-ai", action="store_true")
    assess.add_argument(
        authorisation.CONFIRM_FLAG,
        dest="confirm_authorisation",
        action="store_true",
        help="Confirm this run is within the customer's recorded authorisation",
    )
    assess.add_argument("--formats", default="md,html,pdf")
    discover = commands.add_parser(
        "discover", help="Ingest structured business-discovery notes into a customer's assessment"
    )
    discover.add_argument("--customer", required=True)
    discover.add_argument("--customer-name")
    discover.add_argument("--notes", required=True, type=Path, help="Discovery notes JSON file")
    customer = commands.add_parser(
        "customer", help="Create or update a customer's business identity profile"
    )
    customer.add_argument("--customer", required=True)
    customer.add_argument("--file", type=Path, help="Profile JSON (fields as below)")
    customer.add_argument("--name", help="Business name")
    customer.add_argument("--alias", action="append", help="Other trading name (repeatable)")
    customer.add_argument("--website", action="append", help="Website URL (repeatable)")
    customer.add_argument("--abn", help="ABN (format and check digits only; never looked up)")
    customer.add_argument("--industry", help="One of: " + ", ".join(INDUSTRIES))
    customer.add_argument("--buying-model", help="One of: " + ", ".join(BUYING_MODELS))
    customer.add_argument("--service-area", action="append", help="Suburb/region (repeatable)")
    customer.add_argument(
        "--location", action="append", help="'12 Example St, Sydney NSW 2000' (repeatable)"
    )
    customer.add_argument("--phone", action="append", help="Phone number (repeatable)")
    authorise = commands.add_parser(
        "authorise", help="Record who authorised assessing a customer, when, and its scope"
    )
    authorise.add_argument("--customer", required=True)
    authorise.add_argument("--authorised-by", help="Customer representative who authorised it")
    authorise.add_argument("--date", help="Date the authorisation was given (YYYY-MM-DD)")
    authorise.add_argument(
        "--scope",
        action="append",
        help=f"Authorised website host, subdomains included, or '{authorisation.CONNECTORS_SCOPE}' "
        "for connected reads (repeatable)",
    )
    authorise.add_argument("--revoke", action="store_true", help="Remove the authorisation")
    verify = commands.add_parser(
        "presence-verify", help="Import human-verified platform presence facts"
    )
    verify.add_argument("--customer", required=True)
    verify.add_argument("--file", required=True, type=Path, help="Verifications JSON file")
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
    report.add_argument(
        "--based-on",
        type=int,
        help="With --from-md: the report revision the edit started from (else taken from its "
        "reports/<revision>/ directory, or recorded as unknown)",
    )
    return parser


@subproc.cancellation()
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"connect", "connectors"}:
        return safe_dispatch(args, data_root(), parser)
    if args.command == "ui":
        from ams_assess.ui import serve

        try:
            serve(data_root(), args.port)
        except (OSError, ValueError):
            parser.error("Cannot start local interface; check the port and local data directory")
        return 0
    if args.command == "register":
        try:
            if args.register_command == "show":
                result = store.register_store(data_root(), args.customer)["systems_register"]
            elif args.register_command == "remove":
                store.remove_register(
                    data_root(), args.customer, args.platform, args.site, args.location, args.who
                )
                result = {"removed": True, "history_retained": True}
            else:
                fields = register.EDIT_FIELDS | {
                    "evidence_url",
                    "identifier",
                    "method",
                    "note",
                    "tested",
                    "who",
                    "date",
                }
                changes = {k: getattr(args, k) for k in fields if getattr(args, k) is not None}
                result = store.set_register(
                    data_root(), args.customer, args.platform, changes, args.site, args.location
                )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "gsc-auth":
        return gsc_auth(args, parser)
    if args.command == "modules":
        for module in REGISTRY.values():
            availability = "available"
            if module.name == "siteone":
                binary = ROOT / "tools/siteone/siteone-crawler/siteone-crawler"
                if not binary.is_file() or not os.access(binary, os.X_OK):
                    availability = "unavailable (run scripts/setup.sh)"
            print(
                f"{module.name}: {availability} | {','.join(sorted(module.categories))} | "
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
    if args.command == "customer":
        try:
            changes = profile_changes(args)
            if not changes:
                parser.error("Provide --file or at least one profile field")
            profile, eid, changed = store.set_profile(data_root(), args.customer, changes)
        except (OSError, ValueError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else "Cannot read profile")
        print(
            f"Profile {'saved' if changed else 'unchanged'}: {profile['name'] or '-'} "
            f"(industry: {profile['industry'] or 'not recorded'}; buying model: "
            f"{profile['buying_model'] or 'not recorded'}); evidence {eid} (customer statement)"
        )
        return 0
    if args.command == "authorise":
        if args.revoke == bool(args.authorised_by or args.date or args.scope):
            parser.error("Provide --authorised-by, --date and --scope, or --revoke alone")
        record = (
            None
            if args.revoke
            else {
                "authorised_by": args.authorised_by or "",
                "date": args.date or "",
                "scope": args.scope or [],
            }
        )
        try:
            profile, eid, _ = store.set_profile(
                data_root(), args.customer, {"authorisation": record}
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else "Cannot save authorisation")
        recorded = profile["authorisation"]
        print(
            "Authorisation revoked"
            if recorded is None
            else f"Authorisation recorded: {recorded['authorised_by']} on {recorded['date']}; "
            f"scope: {', '.join(recorded['scope'])}"
        )
        print(f"Evidence {eid} (customer statement); earlier versions remain in history")
        return 0
    if args.command == "presence-verify":
        try:
            records = load_verifications(args.file)
            counts = store.add_presence_verifications(data_root(), args.customer, records)
        except (OSError, ValueError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else "Cannot read verifications")
        print(
            f"Presence verification: {counts['entries']} entries ({counts['new']} new, "
            f"{counts['unchanged']} unchanged)"
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
        if args.based_on is not None and not args.from_md:
            parser.error("--based-on requires --from-md")
        try:
            if args.customer:
                outputs = render_customer(
                    data_root(), args.customer, formats, args.from_md, args.based_on
                )
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
    if not 1 <= args.browser_pages <= 50:
        parser.error("--browser-pages must be between 1 and 50")
    if args.wp_inventory:
        from ams_assess.coverage import load_export

        try:
            load_export(args.wp_inventory)
        except (OSError, ValueError):
            parser.error("Cannot read WordPress inventory: expected bounded JSON/CSV URLs")
    if not 0 <= args.performance_key_pages <= performance.MAX_KEY_PAGES:
        parser.error("--performance-key-pages must be between 0 and 10")
    try:
        for page in args.performance_page:
            normalize_target(page)
            if performance.origin(page) != performance.origin(args.target):
                raise ValueError("--performance-page must use the target origin")
    except ValueError as exc:
        parser.error(str(exc))
    try:
        customer_path(data_root(), args.customer)
        target = normalize_target(args.target)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        existing = store.load(data_root(), args.customer)
    except (OSError, ValueError):
        existing = None  # An invalid slug is reported below; a bad store fails at merge.
    # Before any credential is read or any network request is made.
    try:
        authorisation.require(existing, args.customer, target, confirmed=args.confirm_authorisation)
    except authorisation.Unauthorised as exc:
        parser.error(str(exc))
    settings = config()
    settings["AMS_PERFORMANCE_KEY_PAGES"] = str(args.performance_key_pages)
    settings["AMS_PERFORMANCE_PAGES"] = json.dumps(args.performance_page)
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
    keywords = list(dict.fromkeys(k.strip() for k in (args.keywords or "").split(",")))
    keywords = [k for k in keywords if k]
    if args.keywords is not None and not 1 <= len(keywords) <= MAX_KEYWORDS:
        parser.error(f"--keywords must list 1 to {MAX_KEYWORDS} keywords")
    if any(len(k) > 100 for k in keywords):
        parser.error("Each keyword must be at most 100 characters")
    for keyword in keywords:
        if problem := keyword_problem(keyword):
            parser.error(f"--keywords must be plain search terms ({keyword!r}: {problem})")
    profile = ((existing or {}).get("profile") or {}).get("fields")
    try:
        directory = create_run_dir(data_root(), args.customer)
    except ValueError as exc:
        parser.error(str(exc))
    ctx = Context(
        customer_id=args.customer,
        customer_name=args.customer_name or (profile or {}).get("name") or args.customer,
        target=target,
        categories=categories,
        connections=set(args.connect),
        config=settings,
        run_dir=directory,
        tools_dir=ROOT / "tools",
        store_root=data_root(),
        max_pages=args.max_pages,
        browser_pages=args.browser_pages,
        wp_inventory=args.wp_inventory,
        dataforseo_max_usd=args.dataforseo_max_usd,
        profile=profile,
        keywords=keywords,
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
