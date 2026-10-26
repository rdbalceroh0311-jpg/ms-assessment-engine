"""CLI registration and dispatch; the operations are also used by the local UI."""

import argparse
import json
from pathlib import Path

from ams_assess import authorisation, store
from ams_assess.connectors import REGISTRY, service
from ams_assess.connectors.auth import register_reference
from ams_assess.connectors.core import ConnectorError


def add_parsers(commands) -> None:
    connect = commands.add_parser("connect", help="Authorise and read explicitly bound resources")
    connect.add_argument("connector", choices=REGISTRY)
    connect.add_argument("action", choices=["auth", "list-resources", "bind", "test", "collect"])
    connect.add_argument("--customer")
    connect.add_argument("--credential-ref")
    connect.add_argument("--resource")
    connect.add_argument("--credential-file", type=Path)
    connect.add_argument("--env-var")
    connect.add_argument("--client-secrets", type=Path)
    connect.add_argument("--tenant-id")
    connect.add_argument("--client-id")
    connect.add_argument("--force", action="store_true")
    connect.add_argument("--reason", default="")
    connect.add_argument("--days", type=int, default=28)
    connect.add_argument("--start", default="")
    connect.add_argument("--end", default="")
    connect.add_argument(
        authorisation.CONFIRM_FLAG,
        dest="confirm_authorisation",
        action="store_true",
        help="Confirm test/collect is within the customer's recorded authorisation",
    )
    status = commands.add_parser("connectors", help="Bound connector status table")
    status.add_argument("--customer", required=True)


def dispatch(args: argparse.Namespace, root: Path) -> int:
    if args.command == "connectors":
        current = store.load(root, args.customer) or {}
        print(
            "Connector\tSelected resource\tAccess status\tConnection status\t"
            "Last successful collection\tCoverage\tNext action\tRead details"
        )
        for row in service.rows(current):
            print(
                "\t".join(
                    str(row[k])
                    for k in (
                        "label",
                        "resource",
                        "access",
                        "status",
                        "last_successful_collection",
                        "coverage_text",
                        "next_action",
                        "detail",
                    )
                )
            )
        return 0
    spec = service.connector(args.connector)
    if args.action in {"auth", "list-resources", "bind"} and not args.credential_ref:
        raise ValueError("--credential-ref is required")
    if args.action == "auth":
        flow = spec.effective_auth_flow
        if (args.client_secrets and flow != "client_secrets") or (
            (args.tenant_id or args.client_id) and flow != "device_code"
        ):
            raise ConnectorError("not_configured", "Use this auth flow: " + spec.auth_command)
        if args.credential_file or args.env_var:
            if args.client_secrets or args.tenant_id or args.client_id:
                raise ValueError("Use a credential reference source or OAuth, not both")
            register_reference(
                args.credential_ref, spec, file=args.credential_file, env_var=args.env_var or ""
            )
            print("Credential reference registered; run a bound-resource test")
            return 0
        if spec.authorize is None or flow == "reference":
            raise ConnectorError("not_configured", "Register credentials with " + spec.auth_command)
        if flow == "client_secrets":
            if not args.client_secrets:
                raise ConnectorError("not_configured", "Authorise with " + spec.auth_command)
            spec.authorize(spec, args.credential_ref, args.client_secrets)
        else:
            if not args.tenant_id or not args.client_id:
                raise ConnectorError("not_configured", "Authorise with " + spec.auth_command)
            spec.authorize(
                spec, args.credential_ref, tenant_id=args.tenant_id, client_id=args.client_id
            )
        print(f"Authorised {spec.label}; reference {args.credential_ref}. No resource was bound.")
        return 0
    if args.action == "list-resources":
        resources = service.list_resources(args.connector, args.credential_ref)
        print(
            json.dumps(
                {
                    "status": resources.status,
                    "resources": resources.data.get("rows", []),
                    "truncated": resources.truncated,
                    "detail": resources.detail,
                },
                indent=2,
            )
        )
        return 0 if resources.status == "connected" else 1
    if not args.customer:
        raise ValueError("--customer is required")
    if args.action == "bind":
        if not args.resource:
            raise ValueError("--resource is required; listing never selects a resource")
        state = service.set_binding(
            root,
            args.customer,
            args.connector,
            args.credential_ref,
            args.resource,
            force=args.force,
            reason=args.reason,
        )
    else:
        authorisation.require(
            # No settings file is read before the gate; execute() loads (and prunes) after it.
            store.load(root, args.customer, prune_backups=False),
            args.customer,
            None,
            confirmed=args.confirm_authorisation,
        )
        state = service.execute(
            root,
            args.customer,
            args.connector,
            collect=args.action == "collect",
            days=args.days,
            start=args.start,
            end=args.end,
        )
    print(json.dumps(state, indent=2))
    return (
        0 if args.action == "bind" or state["status"] in {"connected", "connected_partial"} else 1
    )


def safe_dispatch(args: argparse.Namespace, root: Path, parser: argparse.ArgumentParser) -> int:
    try:
        return dispatch(args, root)
    except ConnectorError as exc:
        print(f"{exc.status}: {exc.detail}")
        return 1
    except (service.BindingError, authorisation.Unauthorised) as exc:
        parser.error(str(exc))
    except (ValueError, OSError):
        # Never echo a rejected argument, credential path or provider exception.
        parser.error(
            "Invalid connector request; check required flags, binding and credential reference"
        )
