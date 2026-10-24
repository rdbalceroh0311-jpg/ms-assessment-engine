"""Loopback review UI with explicit, bounded connector reads; no secret input or shell."""

import hmac
import mimetypes
import os
import secrets
import stat
import webbrowser
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from ams_assess import register, store
from ams_assess.connectors import service as connectors
from ams_assess.models import CUSTOMER_RE, customer_path

EXTENSIONS = {".html", ".pdf", ".md", ".png", ".json"}
RUN_FILES = {
    "report.html",
    "report.pdf",
    "report.md",
    "results.json",
    "findings.json",
    "evidence.json",
}
REPORT_FILES = {"report.html", "report.pdf", "report.md"}
MAX_BODY = 32_768


@contextmanager
def open_output(directory: Path, relative: str) -> Generator[int, None, None]:
    """Open a regular output beneath the customer directory without following symlinks.

    Every component is opened relative to the preceding directory descriptor. Renaming a
    parent after it is opened cannot redirect later opens to a replacement or outside path.
    """
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("Invalid output path")
    directory_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    with ExitStack() as stack:
        parent = os.open(directory, directory_flags)
        stack.callback(os.close, parent)
        for part in path.parts[:-1]:
            parent = os.open(part, directory_flags, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink > 1:
            raise ValueError("Output must be a regular file")
        yield fd


def output_files(root: Path, customer: str, current: dict[str, Any]) -> dict[str, Path]:
    return {name: path for name, (path, _) in checked_outputs(root, customer, current).items()}


def checked_outputs(
    root: Path, customer: str, current: dict[str, Any]
) -> dict[str, tuple[Path, os.stat_result]]:
    """Exact per-customer output allowlist, never raw/credentials or arbitrary JSON files.

    Open every component relative to the customer directory with O_NOFOLLOW. Each entry
    keeps the opened regular file's identity for a second descriptor walk in read_checked().
    """
    directory = customer_path(root, customer)
    allowed: dict[str, tuple[Path, os.stat_result]] = {}

    def add(relative: str):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            return
        target = directory / relative
        if target.suffix not in EXTENSIONS:
            return
        try:
            with open_output(directory, relative) as fd:
                allowed[relative] = (target, os.fstat(fd))
        except (OSError, ValueError):
            return  # Missing files, symlinks and non-regular outputs are not allowed.

    for revision in current.get("report_revisions", []):
        path = PurePosixPath(revision["directory"])
        if len(path.parts) == 2 and path.parts[0] == "reports" and ".." not in path.parts:
            for name in revision["files"]:
                if name in REPORT_FILES:
                    add(str(path / name))
    for run in current.get("runs", []):
        name = run.get("run_dir")
        if not name or len(PurePosixPath(name).parts) != 1 or name in {".", ".."}:
            continue
        for filename in RUN_FILES:
            add(f"{name}/{filename}")
        screenshots = directory / name / "screenshots"
        if not screenshots.is_symlink() and screenshots.is_dir():
            for screenshot in screenshots.glob("*.png"):
                add(f"{name}/screenshots/{screenshot.name}")
    return allowed


def read_checked(path: Path, checked: os.stat_result, *, directory: Path) -> bytes | None:
    """Repeat the no-symlink descriptor walk and read only the allowlisted file identity.

    O_NOFOLLOW is applied to every parent and the final file. fstat verifies a regular file
    and compares device/inode with the allowlisted descriptor, never a path-based stat.
    """
    try:
        with open_output(directory, str(path.relative_to(directory))) as fd:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
                return None
            with os.fdopen(os.dup(fd), "rb") as handle:
                return handle.read()
    except (OSError, ValueError):
        return None


def finding_groups(current):
    review = {
        fid
        for r in current.get("needs_review", [])
        for fid in [r.get("finding_id"), *r.get("finding_ids", [])]
        if fid
    }
    groups: dict[str, list[dict[str, Any]]] = {
        "Current": [],
        "Resolved": [],
        "Not rechecked": [],
        "Needs review": [],
    }
    labels = {
        "current": "Current",
        "resolved": "Resolved",
        "not_rechecked": "Not rechecked",
        "needs_review": "Needs review",
    }
    for finding in current.get("findings", []):
        label = (
            "Needs review"
            if finding["id"] in review
            else labels.get(finding.get("lifecycle_status", "current"), "Needs review")
        )
        groups[label].append(finding)
    return groups


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, port: int = 8765, host: str = "127.0.0.1"):
        if host != "127.0.0.1":
            raise ValueError("The local interface must bind to 127.0.0.1")
        if not 0 <= port <= 65535:
            raise ValueError("Invalid port")
        self.root = root
        self.token = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.template = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=True,
            undefined=StrictUndefined,
        ).get_template("ui.html.j2")
        super().__init__((host, port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def url(self, route="/", **params):
        return route + ("?" + urlencode(params) if params else "")

    def bootstrap_url(self):
        return self.origin + "/?" + urlencode({"token": self.token})


class Handler(BaseHTTPRequestHandler):
    @property
    def app(self) -> LocalServer:
        return cast(LocalServer, self.server)

    def parse_request(self):
        if not super().parse_request():
            return False
        try:
            path, params = self.parameters()
            if (
                self.command == "GET"
                and path == "/"
                and "token" in params
                and self.valid_host()
                and hmac.compare_digest(params["token"].encode(), self.app.token.encode())
            ):
                params.pop("token")
                self.reply(303, location=self.app.url(**params), session=True)
                return False
            if self.authenticated(params):
                return True
        except ValueError:
            pass
        self.reply(403, b"Forbidden")
        return False

    def log_message(self, format, *args):
        # Bootstrap credentials and request bodies must never enter access/error logs.
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, b"Request refused")

    def reply(
        self, status, body=b"", content_type="text/plain; charset=utf-8", location="", session=False
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # Saved HTML is untrusted too; disallow scripts, remote resources and embedded forms.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "img-src 'none'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'"
            + ("; sandbox" if getattr(self, "path", "").startswith("/file?") else ""),
        )
        if location:
            self.send_header("Location", location)
        if session:
            self.send_header(
                "Set-Cookie", f"ams_session={self.app.token}; HttpOnly; SameSite=Strict; Path=/"
            )
        self.end_headers()
        self.wfile.write(body)

    def parameters(self):
        parts = urlsplit(self.path)
        params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=30)
        if any(len(v) != 1 for v in params.values()):
            raise ValueError
        return parts.path, {k: v[0] for k, v in params.items()}

    def valid_host(self):
        return len(self.headers.get_all("Host", [])) == 1 and self.headers.get("Host") in {
            f"127.0.0.1:{self.app.server_port}",
            f"localhost:{self.app.server_port}",
        }

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return cookie["ams_session"].value if "ams_session" in cookie else ""

    def authenticated(self, params):
        if not self.valid_host() or "token" in params:
            return False
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return any(
            hmac.compare_digest(value.encode(), self.app.token.encode())
            for value in (supplied, self.session_token())
        )

    def same_origin_write(self):
        origin = "http://" + self.headers["Host"]
        origins = self.headers.get_all("Origin", [])
        referers = self.headers.get_all("Referer", [])
        if len(referers) > 1 or (
            referers
            and (urlsplit(referers[0]).scheme + "://" + urlsplit(referers[0]).netloc != origin)
        ):
            return False
        if origins == [origin]:
            return True
        # Chromium sends an opaque Origin under no-referrer for native form POSTs.
        # Require browser-controlled same-origin Fetch Metadata plus the session cookie;
        # neither cross-origin pages nor sandboxed/opaque initiators satisfy this.
        return (
            origins == ["null"]
            and self.headers.get("Sec-Fetch-Site") == "same-origin"
            and hmac.compare_digest(self.session_token().encode(), self.app.token.encode())
        )

    def do_GET(self):
        try:
            path, params = self.parameters()
            if not self.authenticated(params):
                self.reply(403, b"Forbidden")
                return
            if path not in {"/", "/file"}:
                self.reply(404, b"Not found")
                return
            customer = params.get("customer", "")
            # GET never saves a backfill or performs a lifecycle migration. Unseen register
            # evidence is projected in memory; a CLI refresh or validated POST persists it.
            current = (
                store.load(self.app.root, customer, migrate_legacy=False) if customer else None
            )
            if current:
                register.refresh(current)
            if path == "/file":
                allowed = checked_outputs(self.app.root, customer, current) if current else {}
                entry = allowed.get(params.get("path", ""))
                # Rebuild the allowlist on every request; symlink changes are never cached.
                body = (
                    read_checked(*entry, directory=customer_path(self.app.root, customer))
                    if entry
                    else None
                )
                if entry is None or body is None:
                    self.reply(403, b"Output is not allowed for this customer")
                    return
                target = entry[0]
                self.reply(200, body, mimetypes.guess_type(target.name)[0] or "text/plain")
                return
            customers = []
            if self.app.root.is_dir():
                for child in sorted(self.app.root.iterdir()):
                    if (
                        CUSTOMER_RE.fullmatch(child.name)
                        and not child.is_symlink()
                        and (child / store.STORE_FILE).is_file()
                    ):
                        customers.append(child.name)
            files = output_files(self.app.root, customer, current) if current else {}
            html = self.app.template.render(
                customers=customers,
                customer=customer,
                current=current,
                entries=current.get("systems_register", {}).get("entries", []) if current else [],
                connectors=connectors.rows(current) if current else [],
                groups=finding_groups(current) if current else {},
                files=files,
                url=self.app.url,
                csrf=self.app.csrf,
                choices=register.CHOICES,
                platforms=register.PLATFORMS,
            )
            self.reply(200, html.encode(), "text/html; charset=utf-8")
        except TemplateError:
            self.reply(500, b"Unable to render local interface")
        except (ValueError, OSError, KeyError):
            self.reply(400, b"Invalid customer or output request")

    def do_POST(self):
        try:
            path, params = self.parameters()
            if (
                not self.authenticated(params)
                or path not in {"/update", "/connect"}
                or not self.same_origin_write()
            ):
                self.reply(403, b"Forbidden")
                return
            if self.headers.get(
                "Content-Type"
            ) != "application/x-www-form-urlencoded" or self.headers.get("Transfer-Encoding"):
                self.reply(400, b"Expected a form")
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.reply(413, b"Form too large or empty")
                return
            fields = parse_qs(
                self.rfile.read(length).decode(), keep_blank_values=True, max_num_fields=30
            )
            if any(len(v) != 1 for v in fields.values()):
                raise ValueError
            values = {k: v[0] for k, v in fields.items()}
            if not hmac.compare_digest(values.pop("csrf", "").encode(), self.app.csrf.encode()):
                self.reply(403, b"CSRF verification failed")
                return
            customer = values.pop("customer")
            if path == "/connect":
                key, action = values.pop("connector"), values.pop("action")
                if action in {"reference", "bind"}:
                    resource = values.pop("resource", "") if action == "bind" else ""
                    if action == "bind" and not resource:
                        raise ValueError
                    connectors.set_binding(
                        self.app.root, customer, key, values.pop("credential_ref"), resource
                    )
                elif action in {"test", "collect"}:
                    connectors.execute(
                        self.app.root,
                        customer,
                        key,
                        collect=action == "collect",
                        days=int(values.pop("days", "28")),
                        seconds=40,
                    )
                else:
                    raise ValueError
                self.reply(303, location=self.app.url(customer=customer))
                return

            platform, site = values.pop("platform"), values.pop("site", "")
            location = values.pop("location", "")
            changes = {k: v for k, v in values.items() if v}
            store.set_register(self.app.root, customer, platform, changes, site, location)
            self.reply(303, location=self.app.url(customer=customer))
        except TemplateError:
            self.reply(500, b"Unable to render local interface")
        except connectors.BindingError:
            self.reply(409, b"Connector binding could not be updated")
        except (ValueError, UnicodeError, OSError, KeyError):
            # Do not echo rejected secrets, filesystem paths or untrusted values.
            self.reply(
                400, b"Invalid update; check statuses, binding, evidence and non-secret identifiers"
            )


def serve(root: Path, port: int = 8765):
    with LocalServer(root, port) as server:
        webbrowser.open(server.bootstrap_url())
        print("Local review interface: " + server.origin + " (opened in your browser)", flush=True)
        print("Stop with Ctrl-C. Assessments remain in the CLI.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
