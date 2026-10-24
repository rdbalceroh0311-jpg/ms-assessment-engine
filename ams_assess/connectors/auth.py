"""Private credential references and separate, least-privilege Google Desktop tokens."""

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ams_assess.connectors.core import AuthMaterial, Connector, ConnectorError
from ams_assess.connectors.privacy import private_logs

REF = re.compile(r"[a-z][a-z0-9-]{0,47}")


def config_dir() -> Path:
    return Path.home() / ".config/ams-assess"


def reference_name(value: str) -> str:
    if not REF.fullmatch(value):
        raise ValueError("Credential reference must be a short lowercase name, not a secret")
    # Also reject recognisable token prefixes even when they fit the name grammar.
    if value.startswith(("sk-", "ghp-", "ya29-")):
        raise ValueError("A credential reference is a name, never a secret")
    return value


def private_dir(directory: Path) -> Path:
    if directory.is_symlink():
        raise ValueError("Credential directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def private_file(directory: Path, path: Path) -> Path:
    if path.is_symlink() or path.parent.resolve() != directory.resolve():
        raise ValueError("Credential files must be directly inside the private config directory")
    if path.exists():
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("Credential file must be a regular unshared file")
        path.chmod(0o600)
    return path


def atomic_private(path: Path, value: Any) -> None:
    private_file(private_dir(path.parent), path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".connector-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def config_lock(directory: Path) -> Generator[None]:
    private_dir(directory)
    fd = os.open(directory / ".connectors.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConnectorError(
                        "provider_unavailable", "Credentials busy; retry later"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def references(directory: Path) -> dict[str, Any]:
    path = private_file(directory, directory / "connectors.json")
    return json.loads(path.read_text()) if path.exists() else {}


def register_reference(
    ref: str,
    connector: Connector,
    *,
    file: Path | None = None,
    env_var: str = "",
    directory: Path | None = None,
) -> None:
    directory = directory or config_dir()
    reference_name(ref)
    if bool(file) == bool(env_var):
        raise ValueError("Specify one private credential file or environment variable name")
    if env_var and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", env_var):
        raise ValueError("Invalid environment variable name")
    with config_lock(directory):
        refs = references(directory)
        entry = {
            "provider": connector.provider,
            "connector": connector.key,
            "auth_kind": connector.auth_kind,
        }
        if file:
            entry["file"] = str(private_file(directory, file).absolute())
        else:
            entry["env_var"] = env_var
        if ref in refs and refs[ref] != entry:
            raise ValueError("Credential reference already identifies different credentials")
        refs[ref] = entry
        atomic_private(directory / "connectors.json", refs)


class BoundedRequest(Request):
    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = 10
        return super().__call__(*args, **kwargs)


@private_logs()
def google_desktop_flow(client_secrets: Path, scopes: tuple[str, ...]) -> Credentials:
    client = json.loads(client_secrets.read_text())
    installed = client.get("installed")
    if not isinstance(installed, dict) or "web" in client:
        raise ValueError
    if installed.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth" or (
        installed.get("token_uri") != "https://oauth2.googleapis.com/token"
    ):
        raise ValueError
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes=list(scopes))
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=True,
        access_type="offline",
        prompt="consent",
        include_granted_scopes="false",
        authorization_prompt_message="",
        success_message="Authorisation complete. You may close this window.",
        timeout_seconds=180,
    )
    if not creds.refresh_token or set(creds.granted_scopes or creds.scopes or []) != set(scopes):
        raise ValueError
    return creds


@private_logs()
def google_authorize(
    connector: Connector,
    ref: str,
    client_secrets: Path,
    *,
    directory: Path | None = None,
) -> None:
    directory = directory or config_dir()
    reference_name(ref)
    try:
        creds = google_desktop_flow(client_secrets, connector.scopes)
        token = directory / f"{connector.key}-{ref}-token.json"
        # Check existing references before replacing a file, and never touch legacy GSC tokens.
        with config_lock(directory):
            refs = references(directory)
            entry = {
                "provider": connector.provider,
                "connector": connector.key,
                "auth_kind": connector.auth_kind,
                "file": str(token.absolute()),
            }
            if ref in refs and refs[ref] != entry:
                raise ValueError
            atomic_private(token, json.loads(creds.to_json()))
            refs[ref] = entry
            atomic_private(directory / "connectors.json", refs)
    except Exception:
        raise ConnectorError(
            "credential_missing", "Authorisation failed; check the Desktop OAuth file and consent"
        ) from None


@private_logs()
def google_authenticate(
    connector: Connector,
    ref: str,
    *,
    directory: Path | None = None,
) -> AuthMaterial:
    directory = directory or config_dir()
    reference_name(ref)
    try:
        with config_lock(directory):
            entry = references(directory).get(ref, {})
            if (
                entry.get("provider") != connector.provider
                or entry.get("connector") != (connector.key)
                or not entry.get("file")
            ):
                raise ConnectorError("credential_missing", "Credential reference is not configured")
            path = private_file(directory, Path(entry["file"]))
            data = json.loads(path.read_text())
            # Never silently add scopes to a previously issued token.
            if set(data.get("scopes", [])) != set(connector.scopes):
                raise ConnectorError(
                    "permission_required",
                    "Required scope: "
                    + ", ".join(connector.scopes)
                    + "; re-authorise with "
                    + connector.auth_command,
                )
            creds = Credentials.from_authorized_user_info(data)
            if not creds.valid:
                creds.refresh(BoundedRequest())
                atomic_private(path, json.loads(creds.to_json()))
            if not creds.token:
                raise ValueError
            return AuthMaterial(
                {"Authorization": f"Bearer {creds.token}"},
                tuple(str(v) for v in (creds.token, creds.refresh_token, creds.client_secret) if v),
            )
    except ConnectorError:
        raise
    except RefreshError:
        raise ConnectorError(
            "credential_expired",
            "Credential expired/revoked — re-authorise with " + connector.auth_command,
        ) from None
    except TransportError:
        raise ConnectorError(
            "provider_unavailable", "Token refresh unavailable; retry later"
        ) from None
    except Exception:
        raise ConnectorError(
            "credential_missing", "Credential file is missing or invalid"
        ) from None


def load_reference(connector: Connector, ref: str, *, directory: Path | None = None) -> Any:
    """Adapter helper for API keys, app/SSH config: private JSON or named env/credentials.env.

    The provider module converts this secret material to AuthMaterial and chooses its auth
    protocol. Nothing returned by this function belongs in evidence or error messages.
    """
    from dotenv import dotenv_values

    directory = directory or config_dir()
    reference_name(ref)
    try:
        with config_lock(directory):
            entry = references(directory).get(ref, {})
            if (
                entry.get("provider") != connector.provider
                or entry.get("connector") != connector.key
            ):
                raise ValueError
            if entry.get("file"):
                return json.loads(private_file(directory, Path(entry["file"])).read_text())
            variable = entry.get("env_var", "")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", variable):
                raise ValueError
            values = dotenv_values(
                private_file(directory, directory / "credentials.env"), interpolate=False
            )
            value = os.environ.get(variable) or values.get(variable)
            if not value:
                raise ValueError
            return value
    except Exception:
        raise ConnectorError(
            "credential_missing", "Credential reference is missing or invalid"
        ) from None
