"""Loopback-only review UI. No collectors, credentials, subprocesses or general file server."""

import hmac
import mimetypes
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from ams_assess import register, store
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


def output_files(root: Path, customer: str, current: dict[str, Any]) -> dict[str, Path]:
    """Exact per-customer output allowlist, never raw/credentials or arbitrary JSON files.

    Reject every symlink component (also within the customer). A stored directory name
    must be a simple relative path with the expected shape, never an arbitrary path.
    """
    directory = customer_path(root, customer)
    allowed: dict[str, Path] = {}

    def add(relative: str):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            return
        target = directory / relative
        if target.suffix not in EXTENSIONS:
            return
        if any(p.is_symlink() for p in [target, *target.parents] if p != directory.parent):
            return
        resolved = target.resolve()
        if resolved.is_relative_to(directory.resolve()) and resolved.is_file():
            allowed[relative] = resolved

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
        return route + "?" + urlencode({"token": self.token, **params})


class Handler(BaseHTTPRequestHandler):
    @property
    def app(self) -> LocalServer:
        return cast(LocalServer, self.server)

    def parse_request(self):
        if not super().parse_request():
            return False
        try:
            _, params = self.parameters()
            if self.authenticated(params):
                return True
        except ValueError:
            pass
        self.reply(403, b"Forbidden")
        return False

    def log_message(self, format, *args):
        # Request URLs contain the per-start token. Never log URLs or request bodies.
        pass

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8", location=""):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # no-referrer makes Chromium send Origin: null on native form POSTs. same-origin
        # keeps their Origin check usable while suppressing token leakage to external links.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # Saved HTML is untrusted too; disallow scripts, remote resources and embedded forms.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'" + ("; sandbox" if self.path.startswith("/file?") else ""),
        )
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def parameters(self):
        parts = urlsplit(self.path)
        params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=30)
        if any(len(v) != 1 for v in params.values()):
            raise ValueError
        return parts.path, {k: v[0] for k, v in params.items()}

    def authenticated(self, params):
        return (
            len(self.headers.get_all("Host", [])) == 1
            and self.headers.get("Host") == self.app.origin.removeprefix("http://")
            and hmac.compare_digest(params.get("token", "").encode(), self.app.token.encode())
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
                allowed = output_files(self.app.root, customer, current) if current else {}
                target = allowed.get(params.get("path", ""))
                if target is None:
                    self.reply(403, b"Output is not allowed for this customer")
                    return
                # Rebuild the allowlist on every request; symlink changes are never cached.
                self.reply(
                    200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "text/plain"
                )
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
                groups=finding_groups(current) if current else {},
                files=files,
                url=self.app.url,
                token=self.app.token,
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
                or path != "/update"
                or self.headers.get_all("Origin", []) != [self.app.origin]
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
            platform, site = values.pop("platform"), values.pop("site", "")
            location = values.pop("location", "")
            changes = {k: v for k, v in values.items() if v}
            store.set_register(self.app.root, customer, platform, changes, site, location)
            self.reply(303, location=self.app.url(customer=customer))
        except TemplateError:
            self.reply(500, b"Unable to render local interface")
        except (ValueError, UnicodeError, OSError, KeyError):
            # Do not echo rejected secrets, filesystem paths or untrusted values.
            self.reply(
                400, b"Invalid register update; check statuses, evidence and non-secret identifiers"
            )


def serve(root: Path, port: int = 8765):
    with LocalServer(root, port) as server:
        print("Local review interface: " + server.origin + server.url(), flush=True)
        print("Stop with Ctrl-C. Assessments remain in the CLI.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
