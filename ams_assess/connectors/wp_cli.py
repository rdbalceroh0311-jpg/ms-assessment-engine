"""Bounded WP-CLI reads through OpenSSH; command contracts are linked in README.

The HTTPS-shaped routes are private dispatch keys for ReadClient, never network URLs.
Only this transport builds remote argv. No arbitrary command/config/PHP is accepted.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import selectors
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ams_assess.connectors.auth import config_dir, load_reference, private_file
from ams_assess.connectors.core import (
    AuthMaterial,
    Connector,
    ConnectorContext,
    ConnectorError,
    Endpoint,
    ReadOperation,
)
from ams_assess.connectors.privacy import redact
from ams_assess.lifecycle import observe, site_key
from ams_assess.models import ModuleResult

BASE = "https://wp-cli.invalid/"
MAX_BYTES = 2_000_000
COMMAND_SECONDS = 10.0
PAGE_SIZE = 100
MAX_PAGES = 5
PUBLIC_TYPES = ("post", "page")
OPTIONS = (
    "blog_public",
    "siteurl",
    "home",
    "permalink_structure",
    "blogdescription",
    "default_comment_status",
    "users_can_register",
    "default_role",
    "WPLANG",
    "timezone_string",
)
FIELDS = "name,status,version,update,update_version,auto_update"
COMMANDS = {
    "installation": ("core", "is-installed"),
    "runtime": ("cli", "info", "--format=json"),
    "version": ("core", "version", "--extra"),
    # Stable WP-CLI releases do not support JSON for core checksums/version/is-installed.
    "core_checksums": ("core", "verify-checksums"),
    "plugin_checksums": ("plugin", "verify-checksums", "--all", "--format=json"),
    "plugins": ("plugin", "list", "--format=json", "--fields=" + FIELDS, "--skip-update-check"),
    "themes": ("theme", "list", "--format=json", "--fields=" + FIELDS, "--skip-update-check"),
    **{f"option_{name}": ("option", "get", name, "--format=json") for name in OPTIONS},
}
OPERATIONS = (*COMMANDS, *("posts_" + kind for kind in PUBLIC_TYPES))
RESOURCE_PATTERN = r"wp-install:[a-f0-9]{64}"
ROUTE_PATTERN = re.escape(BASE) + RESOURCE_PATTERN + "/(?:" + "|".join(OPERATIONS) + ")"


def normalize_resource(value: str) -> str:
    if not re.fullmatch(RESOURCE_PATTERN, value):
        raise ValueError("Use the opaque installation ID from wp_cli list-resources")
    return value


def install_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value)
        or len(value) > 500
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Use a canonical absolute installation path without metacharacters")
    return value


@dataclass(frozen=True, repr=False)
class SSHConfig:
    host: str
    port: int
    user: str
    key_path: str
    known_hosts: str
    path: str

    @property
    def resource(self) -> str:
        # User/key/reference independent: the same installation cannot evade binding isolation.
        identity = json.dumps([self.host, self.port, self.path], separators=(",", ":"))
        return "wp-install:" + hashlib.sha256(identity.encode()).hexdigest()


def authenticate(spec: Connector, ref: str, *, directory: Path | None = None) -> AuthMaterial:
    directory = directory or config_dir()
    try:
        data = load_reference(spec, ref, directory=directory)
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict) or set(data) != {
            "host",
            "port",
            "user",
            "key_path",
            "known_hosts",
            "install_path",
        }:
            raise ValueError
        host = data["host"]
        if (
            not isinstance(host, str)
            or len(host) > 253
            or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)
        ):
            raise ValueError
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9]+(?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                raise ValueError from None
            if any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in host.split(".")
            ):
                raise ValueError from None
            host = host.lower()
        port, user = data["port"], data["user"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", user):
            raise ValueError
        paths = []
        for key in ("key_path", "known_hosts"):
            path = private_file(directory, Path(install_path(data[key])))
            if not path.is_file() or not path.stat().st_size:
                raise ValueError
            paths.append(str(path))
        config = SSHConfig(host, port, user, *paths, install_path(data["install_path"]))
        return AuthMaterial(secrets=(*paths, config.path), private_data=config)
    except Exception:
        raise ConnectorError(
            "credential_missing",
            "Invalid SSH credential: supply host, port, user, canonical install_path, key_path "
            "and pinned known_hosts; files must be private under ~/.config/ams-assess. "
            "Register with " + spec.auth_command + ".",
        ) from None


def command_argv(operation: str, path: str, page: int | None = None) -> list[str]:
    path = install_path(path)
    if operation in COMMANDS and page is None:
        command = list(COMMANDS[operation])
    elif operation in {"posts_post", "posts_page"} and type(page) is int and 1 <= page <= MAX_PAGES:
        command = [
            "post",
            "list",
            "--post_type=" + operation.removeprefix("posts_"),
            "--post_status=publish",
            "--format=json",
            "--fields=ID,post_type,post_name,post_modified,url",
            f"--posts_per_page={PAGE_SIZE}",
            f"--paged={page}",
            "--orderby=ID",
            "--order=ASC",
        ]
    else:
        raise ConnectorError("failed", "WP-CLI read allowlist refused command or arguments")
    return [
        "wp",
        *command,
        "--path=" + path,
        "--skip-plugins",
        "--skip-themes",
        "--skip-packages",
        "--context=cli",
        "--no-color",
    ]


def ssh_argv(config: SSHConfig, operation: str, page: int | None, seconds: float) -> list[str]:
    wp = command_argv(operation, config.path, page)
    # The only shell syntax is this constant prefix. All argv values are shlex-quoted.
    # Cwd / and empty global/system config avoid install/home wp-cli.yml require/exec hooks.
    remote = "cd / && exec " + shlex.join(
        [
            "env",
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "HOME=/nonexistent",
            "LC_ALL=C",
            "WP_CLI_CONFIG_PATH=/dev/null",
            "WP_CLI_SYSTEM_SETTINGS_PATH=/dev/null",
            "WP_CLI_CACHE_DIR=/dev/null",
            "WP_CLI_DISABLE_AUTO_CHECK_UPDATE=1",
            "DISABLE_WP_CRON=1",
            "timeout",
            "--signal=TERM",
            "--kill-after=1s",
            f"{max(0.05, seconds - 1.1):.3f}s",
            *wp,
        ]
    )
    options = (
        "StrictHostKeyChecking=yes",
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "PreferredAuthentications=publickey",
        "ForwardAgent=no",
        "ForwardX11=no",
        "ClearAllForwardings=yes",
        "PermitLocalCommand=no",
        "ProxyCommand=none",
        "ProxyJump=none",
        "ControlMaster=no",
        "ControlPath=none",
        "ConnectionAttempts=1",
        "ConnectTimeout=5",
        "ServerAliveInterval=3",
        "ServerAliveCountMax=1",
        "GlobalKnownHostsFile=/dev/null",
        "UpdateHostKeys=no",
        "VerifyHostKeyDNS=no",
        "LogLevel=ERROR",
        "UserKnownHostsFile=" + config.known_hosts,
    )
    return [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-T",
        "-n",
        "-a",
        "-x",
        *(arg for option in options for arg in ("-o", option)),
        "-i",
        config.key_path,
        "-p",
        str(config.port),
        "-l",
        config.user,
        "--",
        config.host,
        remote,
    ]


def run_bounded(argv: list[str], seconds: float) -> tuple[int, str, str]:
    """Drain both pipes incrementally; neither subprocess buffers nor remote time are unbounded."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except OSError:
        raise ConnectorError("not_found", "Local OpenSSH executable is unavailable") from None
    outputs = [bytearray(), bytearray()]
    try:
        with selectors.DefaultSelector() as selector:
            for index, pipe in enumerate((process.stdout, process.stderr)):
                assert pipe is not None
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, index)
            while selector.get_map():
                remaining = seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise httpx.ReadTimeout("SSH command deadline reached")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    outputs[key.data].extend(chunk)
                    if sum(map(len, outputs)) > MAX_BYTES:
                        raise ConnectorError("failed", "SSH output exceeded the 2 MB combined cap")
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise httpx.ReadTimeout("SSH command deadline reached")
            code = process.wait(timeout=remaining)
        return code, outputs[0].decode("utf-8", "replace"), outputs[1].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        raise httpx.ReadTimeout("SSH command deadline reached") from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()


def check_failure(code: int, stdout: str, stderr: str, operation: str) -> None:
    diagnostic = (stdout + "\n" + stderr).lower()
    if code == 255:
        if any(
            s in diagnostic
            for s in (
                "host key verification failed",
                "host identification",
                "no matching host key",
                "host key is known",
            )
        ):
            raise ConnectorError(
                "permission_required",
                "SSH host key not trusted or changed; obtain and verify "
                "the pinned known_hosts entry with the host administrator. Never auto-accept.",
            )
        if "expired" in diagnostic or "revoked" in diagnostic:
            raise ConnectorError(
                "credential_expired",
                "SSH key/certificate expired or revoked; "
                "ask the administrator to renew the authorised read identity",
            )
        if any(
            s in diagnostic
            for s in ("permission denied", "authentication failed", "load key", "bad permissions")
        ):
            raise ConnectorError(
                "permission_required",
                "SSH public-key authentication denied; "
                "authorise the selected user's key for the bound installation",
            )
        raise httpx.ConnectError("SSH connection unavailable")
    if code in {124, 137}:
        raise httpx.ReadTimeout("Remote WP-CLI deadline reached")
    if "permission denied" in diagnostic or "not permitted" in diagnostic:
        raise ConnectorError(
            "permission_required",
            "SSH user needs read access to this "
            "installation and WordPress SELECT access; no write grant requested",
        )
    if code in {126, 127}:
        raise ConnectorError("not_found", "Remote wp or GNU timeout unavailable in the fixed PATH")
    if code and any(
        s in diagnostic
        for s in (
            "not a wordpress installation",
            "does not seem to be a wordpress installation",
            "not installed",
        )
    ):
        raise ConnectorError("not_found", "WordPress not installed at the bound path")
    if code and operation not in {"core_checksums", "plugin_checksums"}:
        if operation == "installation" and not diagnostic.strip():
            raise ConnectorError("not_found", "WordPress tables not installed at the bound path")
        raise ConnectorError("failed", "WP-CLI read failed; diagnostics withheld for privacy")


def safe_word(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError
    text = str(value)
    if (not text and empty) or re.fullmatch(r"[A-Za-z0-9_.+-]{1,120}", text):
        return redact(text)
    raise ValueError


def public_link(value: Any) -> str:
    """Retain public permalinks (including plain ?p= IDs), never URL secrets or email paths."""
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        raise ValueError
    parts = urlsplit(value)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError
    if redact(parts.path) != parts.path or "@" in parts.path or "\\" in value:
        raise ValueError
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if k in {"p", "page_id"} and re.fullmatch(r"[1-9][0-9]{0,19}", v)
    ]
    host = parts.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    port = parts.port
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path or "/", urlencode(query), ""))


def checksum_payload(operation: str, code: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Report confirmed mismatches separately from skipped/unverifiable checksums."""
    if operation == "core_checksums":
        mismatches = len(
            re.findall(r"^Warning: File doesn't verify against checksum:", stderr, re.M)
        )
        missing = len(re.findall(r"^Warning: File doesn't exist:", stderr, re.M))
        extra = len(re.findall(r"^Warning: File should not exist:", stderr, re.M))
        success = (
            code == 0 and "Success: WordPress installation verifies against checksums." in stdout
        )
        expected = all(
            line.startswith(
                (
                    "Warning: File doesn't verify against checksum:",
                    "Warning: File doesn't exist:",
                    "Warning: File should not exist:",
                    "Error: WordPress installation doesn't verify against checksums.",
                )
            )
            for line in stderr.splitlines()
            if line.strip()
        )
        complete = expected and (success or code == 1 and bool(mismatches + missing))
        return {
            "mismatches": mismatches,
            "missing": missing,
            "extra": extra,
            "complete": complete,
            "verified": success and not extra and complete,
        }
    rows: list[Any] = []
    rest = stdout.strip()
    if rest.startswith("["):
        rows, end = json.JSONDecoder().raw_decode(rest)
        rest = rest[end:].strip()
    # WP-CLI emits no JSON at all on an all-passing run; it emits the batch success message.
    success = re.fullmatch(r"Success: Verified (\d+) of (\d+) plugins?\.", rest)
    mismatches = missing = extra = unknown = 0
    for row in rows:
        message = row.get("message") if isinstance(row, dict) else None
        if message == "Checksum does not match":
            mismatches += 1
        elif message == "File is missing":
            missing += 1
        elif message == "File was added":
            extra += 1
        else:
            unknown += 1
    # Even a successful exit can skip premium/custom plugins: stderr must be accounted for.
    expected_error = bool(rows) and all(
        re.fullmatch(r"Error: Only verified \d+ of \d+ plugins?(?: \(.*\))?\.", line)
        for line in stderr.splitlines()
        if line.strip()
    )
    complete = not unknown and (
        bool(success and success[1] == success[2])
        and code == 0
        and not stderr.strip()
        or bool(rows)
        and code == 1
        and expected_error
        and not rest
    )
    return {
        "mismatches": mismatches,
        "missing": missing,
        "extra": extra,
        "complete": complete,
        "verified": complete and not rows,
        "unverifiable": not complete,
    }


def payload(operation: str, code: int, stdout: str, stderr: str) -> dict[str, Any]:
    data: dict[str, Any]
    check_failure(code, stdout, stderr, operation)
    if operation in {"core_checksums", "plugin_checksums"}:
        return checksum_payload(operation, code, stdout, stderr)
    if operation == "installation":
        if stdout.strip() or stderr.strip():
            raise ValueError
        return {"installed": True}
    if operation == "version":
        match = re.search(r"^WordPress version:\s*(\S+)\s*$", stdout, re.M)
        if not match:
            raise ValueError
        data = {"wordpress_version": safe_word(match[1])}
    else:
        body = json.loads(stdout)
        if operation == "runtime":
            data = {key: safe_word(body[key]) for key in ("php_version", "wp_cli_version")}
        elif operation in {"plugins", "themes"}:
            if not isinstance(body, list) or not all(isinstance(r, dict) for r in body):
                raise ValueError
            data = {
                "rows": [
                    {key: safe_word(row[key], empty=True) for key in FIELDS.split(",")}
                    for row in body[:1000]
                ],
                "truncated": len(body) > 1000,
                "update_source": "existing cached update metadata; freshness unknown",
            }
        elif operation.startswith("posts_"):
            if not isinstance(body, list) or len(body) > PAGE_SIZE:
                raise ValueError
            rows = []
            for row in body:
                if (
                    not isinstance(row, dict)
                    or row.get("post_type") != operation[6:]
                    or not re.fullmatch(r"[1-9][0-9]{0,19}", str(row.get("ID", "")))
                ):
                    raise ValueError
                try:
                    url = public_link(row.get("url"))
                except ValueError:
                    url = ""
                rows.append({"id": str(row["ID"]), "url": url})
            data = {"rows": rows}
        elif operation in {"option_blog_public", "option_users_can_register"}:
            if type(body) not in {str, int} or str(body) not in {"0", "1"}:
                raise ValueError
            data = {"value": str(body)}
        elif operation in {"option_home", "option_siteurl"}:
            data = {"value": public_link(body)}
        elif operation in {"option_blogdescription", "option_permalink_structure"}:
            if not isinstance(body, str):
                raise ValueError
            data = {"nonempty": bool(body)}  # Never persist arbitrary prose/server paths.
        elif operation == "option_timezone_string":
            if not isinstance(body, str) or not re.fullmatch(r"[A-Za-z0-9_+/-]{0,80}", body):
                raise ValueError
            data = {"configured": bool(body)}
        else:
            data = {"value": safe_word(body, empty=True)}
    if stderr.strip():
        data["warnings"] = True  # Raw PHP diagnostics can contain secrets and server paths.
    return data


class SSHTransport(httpx.BaseTransport):
    def __init__(self, auth: AuthMaterial):
        self.config: SSHConfig = auth.private_data

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        if request.method != "GET" or request.content:
            raise ConnectorError("failed", "SSH read allowlist refused method or body")
        if url == BASE + "resource":
            operation = "installation"
        elif url.startswith(BASE + self.config.resource + "/") and re.fullmatch(ROUTE_PATTERN, url):
            operation = url.rsplit("/", 1)[-1]
        else:
            raise ConnectorError(
                "not_found", "Requested installation does not match credential binding"
            )
        params = list(request.url.params.multi_items())
        page = None
        if operation.startswith("posts_"):
            if (
                len(params) != 1
                or params[0][0] != "page"
                or not re.fullmatch(r"[1-5]", params[0][1])
            ):
                raise ConnectorError("failed", "SSH read allowlist refused pagination arguments")
            page = int(params[0][1])
        elif params:
            raise ConnectorError("failed", "SSH read allowlist refused arguments")
        seconds = min(COMMAND_SECONDS, request.extensions.get("timeout", {}).get("read") or 10)
        code, out, err = run_bounded(ssh_argv(self.config, operation, page, seconds), seconds)
        try:
            data = payload(operation, code, out, err)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise ConnectorError(
                "failed", "Empty or invalid WP-CLI response; no conclusion drawn"
            ) from None
        if operation == "installation":
            data.update(
                resource_id=self.config.resource,
                permission_evidence="Pinned SSH identity read the bound WordPress installation",
            )
        return httpx.Response(200, json=data)


def read(ctx: ConnectorContext, operation: str) -> Endpoint:
    url = BASE + ctx.resource + "/" + operation

    def fetch(endpoint: Endpoint) -> None:
        endpoint.data = ctx.http.request("GET", url)
        endpoint.data["snapshot"] = True
        endpoint.rows = len(endpoint.data["rows"]) if "rows" in endpoint.data else 1
        endpoint.truncated = bool(endpoint.data.get("truncated"))
        if endpoint.data.get("complete") is False or endpoint.data.get("warnings"):
            endpoint.status = "connected_partial"
            endpoint.detail = (
                "Checksum coverage incomplete; premium/custom plugins or provider errors can be "
                "unverifiable. No passing integrity conclusion."
                if "checksums" in operation
                else "WP-CLI emitted diagnostics; coverage needs verification"
            )

    return ctx.capture(operation, url, fetch)


def list_resources(ctx: ConnectorContext) -> Endpoint:
    def fetch(endpoint: Endpoint) -> None:
        data = ctx.http.request("GET", BASE + "resource")
        endpoint.data = {
            "rows": [
                {
                    "resource_id": data["resource_id"],
                    "label": "Explicitly configured WordPress installation",
                }
            ]
        }
        endpoint.rows = 1

    return ctx.capture("resource", BASE + "resource", fetch)


def test(ctx: ConnectorContext) -> Endpoint:
    endpoint = read(ctx, "installation")
    if endpoint.status == "connected" and endpoint.data.get("resource_id") != ctx.resource:
        endpoint.status, endpoint.detail = (
            "not_found",
            "Installation identity did not match binding",
        )
    return endpoint


def collect(ctx: ConnectorContext) -> None:
    for operation in COMMANDS:
        if operation != "installation":
            read(ctx, operation)
    for kind in PUBLIC_TYPES:
        operation = "posts_" + kind
        url = BASE + ctx.resource + "/" + operation

        def inventory(endpoint: Endpoint, kind=kind, url=url) -> None:
            endpoint.data = {
                "inventory_source": "wp_cli",
                "post_type": kind,
                "post_status": "publish",
                "urls": [],
                "count": 0,
                "count_is_lower_bound": True,
                "snapshot": True,
                "rejected_urls": 0,
            }
            seen: set[str] = set()
            for page in range(1, min(MAX_PAGES, ctx.max_pages) + 1):
                body = ctx.http.request("GET", url, params={"page": page})
                rows = body["rows"]
                for row in rows:
                    if row["id"] in seen:
                        raise ConnectorError(
                            "connected_partial", "Inventory changed during pagination"
                        )
                    seen.add(row["id"])
                    endpoint.rows = endpoint.data["count"] = len(seen)
                    if row["url"]:
                        endpoint.data["urls"].append(row["url"])
                    else:
                        endpoint.data["rejected_urls"] += 1
                if body.get("warnings"):
                    raise ConnectorError(
                        "connected_partial", "Inventory diagnostics; counts are a lower bound"
                    )
                if len(rows) < PAGE_SIZE:
                    endpoint.data["count_is_lower_bound"] = False
                    if endpoint.data["rejected_urls"]:
                        endpoint.status = "connected_partial"
                        endpoint.detail = "Unsafe inventory URLs excluded"
                    return
            endpoint.truncated = True
            endpoint.detail = "Published inventory page cap reached; count is a lower bound"

        ctx.capture(operation, url, inventory)


def analyse(ctx: ConnectorContext, result: ModuleResult) -> None:
    endpoints = {e.key: e for e in ctx.endpoints}

    def check(
        key: str,
        failed: bool,
        title: str,
        detail: str,
        endpoint: Endpoint,
        category: str = "technical",
        extra_refs: tuple[str, ...] = (),
    ) -> None:
        component, site = ctx.resource, "wp-cli://" + ctx.resource.removeprefix("wp-install:")
        refs = [endpoint.evidence_id, *extra_refs]
        outcome = observe(
            "wp_cli", "wp_cli:" + key, category, component, "fail" if failed else "pass", refs
        )
        outcome.site = site
        result.outcomes.append(outcome)
        if failed:
            result.finding(
                "wp_cli:" + key,
                title,
                category,
                refs,
                detail,
                "Review the connected evidence with the site administrator; "
                "verify intent before making changes.",
                site=site,
                component=component,
                coverage=[component],
                confidence="confirmed",
                severity="medium",
            )

    plugins = endpoints.get("plugins")
    if plugins and plugins.complete:
        updates = sum(row["update"] == "available" for row in plugins.data["rows"])
        inactive = sum(row["status"] == "inactive" for row in plugins.data["rows"])
        # Cached negative update data is not an equivalent passing freshness recheck.
        if updates:
            check(
                "updates",
                True,
                "Plugin update available — not a confirmed vulnerability",
                f"{updates} plugins have an update in cached metadata; freshness is unknown. "
                "No vulnerability feed was queried and no exploitability is established.",
                plugins,
            )
        check(
            "inactive",
            inactive > 0,
            "Inactive WordPress plugins present",
            f"{inactive} plugins are inactive; review whether they are still needed.",
            plugins,
        )
    for key in ("core_checksums", "plugin_checksums"):
        endpoint = endpoints.get(key)
        if endpoint and any(endpoint.data.get(k, 0) for k in ("mismatches", "missing", "extra")):
            check(
                key,
                True,
                "WordPress checksum integrity discrepancy",
                f"{endpoint.data['mismatches']} checksum mismatches and "
                f"{endpoint.data['missing']} missing files reported by {key}. "
                f"{endpoint.data['extra']} files outside the published checksums. "
                "This confirms an integrity discrepancy, not malware or a vulnerability.",
                endpoint,
                "security",
            )
        elif endpoint and endpoint.complete and endpoint.data.get("verified"):
            check(
                key,
                False,
                "WordPress checksums verified",
                "Published checksums matched",
                endpoint,
                "security",
            )

    home = endpoints.get("option_home")
    site = home.data.get("value", "") if home and home.complete else ""
    blog = endpoints.get("option_blog_public")
    if blog and blog.complete:
        refs = {e.id for e in ctx.public_evidence if site and site_key(e.url) == site_key(site)}
        related = [
            f["id"]
            for f in ctx.public_findings
            if set(f.get("evidence_ids", [])) & refs
            and f.get("lifecycle_status", "current") == "current"
            and re.search(r"noindex|indexing-restricted", f.get("check_key", ""))
        ]
        comparison: dict[str, Any] = {
            "evidence_ids": sorted(refs),
            "related_finding_ids": related,
        }
        blog.data["public_comparison"] = comparison
        # The setting is connected evidence supporting the existing public noindex finding.
        if blog.data["value"] == "0" and related:
            comparison["detail"] = (
                "blog_public=0 confirms search discouragement; "
                "see the linked public noindex finding"
            )
            blog.detail = (
                "blog_public=0 supports public findings "
                + ", ".join(related)
                + "; public evidence: "
                + ", ".join(sorted(refs))
            )
        else:
            check(
                "blog_public",
                blog.data["value"] == "0",
                "WordPress discourages search indexing",
                "blog_public=0; verify that search discouragement is intended. "
                "This setting is not access control.",
                blog,
                "seo",
            )
    registration, role = (
        endpoints.get("option_users_can_register"),
        endpoints.get("option_default_role"),
    )
    if registration and registration.complete and role and role.complete:
        check(
            "registration",
            registration.data["value"] == "1",
            "WordPress user registration enabled",
            "users_can_register=1 with default_role="
            + role.data["value"]
            + ". This may be intentional; review the default role and registration controls.",
            registration,
            "security",
            (role.evidence_id,),
        )
    crawled: set[str] = set()
    for evidence in ctx.public_evidence:
        if (
            evidence.source == "siteone"
            and evidence.description == "SiteOne HTTP response"
            and isinstance(evidence.data, Mapping)
            and re.match(r"^2[0-9]{2}(?:\D|$)", str(evidence.data.get("status", "")))
        ):
            try:
                crawled.add(public_link(evidence.url))
            except ValueError:
                continue
    for kind in PUBLIC_TYPES:
        endpoint = endpoints.get("posts_" + kind)
        if endpoint and endpoint.data:
            urls = set(endpoint.data.get("urls", []))
            same_site = {u for u in urls if site and site_key(u) == site_key(site)}
            endpoint.data["public_reconciliation"] = {
                "matched_crawl_urls": len(same_site & crawled),
                "not_seen_in_crawl": len(same_site - crawled) if crawled else None,
                "public_crawl_available": bool(crawled),
                "detail": "Known published URLs only; neither crawled nor browser-tested. "
                "Only post/page publish status is covered; custom/private types are excluded.",
            }


CONNECTOR = Connector(
    key="wp_cli",
    label="WordPress WP-CLI",
    provider="wordpress",
    auth_kind="ssh",
    auth_flow="reference",
    auth_hint=(
        "ams-assess connect wp_cli auth --credential-ref <name> --credential-file <private-json>"
    ),
    collection_seconds=90,
    max_requests=40,
    scopes=(
        "SSH public-key login, pinned host key, installation read access "
        "and WordPress SELECT access",
    ),
    resource_type="install",
    read_allowlist=(
        ReadOperation("GET", ROUTE_PATTERN),
        ReadOperation("GET", re.escape(BASE) + "resource"),
    ),
    normalize_resource=normalize_resource,
    list_resources=list_resources,
    test=test,
    collect=collect,
    analysers=(analyse,),
    authenticate=authenticate,
    transport_factory=SSHTransport,
    categories=frozenset({"technical", "security", "seo"}),
)
