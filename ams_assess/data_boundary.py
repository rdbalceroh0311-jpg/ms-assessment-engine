"""Secret removal and private artefact writes, independent of provider/network code."""

import base64
import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

_UMASK_LOCK = threading.RLock()


@contextlib.contextmanager
def private_artifacts():
    """Restrictive creation mode for third-party writers (PDFs/screenshots/copies)."""
    with _UMASK_LOCK:
        old = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(old)


SECRET_KEY = re.compile(
    r"password|secret|token|api.?key|authorization|cookie|private.?key|login|credential", re.I
)
TOKEN = re.compile(
    r"CANARY[-_.][A-Za-z0-9_./+=:-]+|\bBearer\s+[^\s\"'<>]+"
    r"|\b(?:AIza[\w-]{15,}|ya29[.-][\w.-]+|sk-(?:ant-)?[\w-]{16,}"
    r"|gh[pousr]_[\w]{15,}|github_pat_[\w]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)",
    re.I,
)
ASSIGNMENT = re.compile(
    r"\b(?:key|token|access_token|refresh_token|sig|signature|password|secret|api[_-]?key)"
    r"\s*[=:]\s*[^\s&\"'<>]+",
    re.I,
)
EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
BASIC = re.compile(r"\bBasic\s+([A-Za-z0-9+/]+={0,2})", re.I)
BASE64 = re.compile(r"(?<![\w+/-])[A-Za-z0-9+/_-]{16,}={0,2}(?![\w+/=-])")
URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def configured_secrets(config: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(value for key, value in config.items() if SECRET_KEY.search(key) and value)


def clean_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
    except ValueError:
        return "[redacted URL]"


def text(value: str, secrets: tuple[str, ...] = (), *, ai: bool = False) -> str:
    # Decode URL escapes only to inspect for secrets; preserve harmless URL semantics.
    decoded = value
    for _ in range(5):
        decoded = unquote(decoded)
    if TOKEN.search(decoded) or ASSIGNMENT.search(decoded) or any(s in decoded for s in secrets):
        value = decoded
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        variants = {
            secret,
            json.dumps(secret)[1:-1],
            base64.b64encode(secret.encode()).decode(),
            base64.urlsafe_b64encode(secret.encode()).decode(),
        }
        for variant in variants:
            value = value.replace(variant, "[redacted]")
            if variant != secret:
                value = value.replace(variant.rstrip("="), "[redacted]")

    def encoded(match: re.Match[str]) -> str:
        try:
            candidate = base64.urlsafe_b64decode(match[0] + "=" * (-len(match[0]) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return match[0]
        decoded = unquote(candidate)
        if (
            TOKEN.search(decoded)
            or ASSIGNMENT.search(decoded)
            or any(s in decoded for s in secrets)
        ):
            return "[redacted]"
        return match[0]

    def basic(match: re.Match[str]) -> str:
        try:
            credentials = base64.b64decode(match[1] + "=" * (-len(match[1]) % 4), validate=True)
        except ValueError:
            return match[0]
        # Preserve ordinary prose such as "basic voicemail" and "Basic API Access".
        return "[redacted]" if b":" in credentials else match[0]

    value = BASIC.sub(basic, value)
    value = BASE64.sub(encoded, value)
    value = TOKEN.sub("[redacted]", value)
    # Strip complete URLs first so query secrets cannot leave partial bearer strings behind.
    if ai:
        value = URL.sub(lambda m: clean_url(m[0]), value)
        value = EMAIL.sub("[redacted email]", value)
    return ASSIGNMENT.sub("[redacted]", value)


def sanitise(value: Any, secrets: tuple[str, ...] = (), *, ai: bool = False) -> Any:
    if isinstance(value, Mapping):
        return {text(str(k), secrets, ai=ai): sanitise(v, secrets, ai=ai) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitise(v, secrets, ai=ai) for v in value]
    if isinstance(value, str):
        return text(value, secrets, ai=ai)
    return value


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Artefact directory must not be a symlink")
    if not path.exists():
        private_dir(path.parent)
        path.mkdir(mode=0o700)


def private_write(path: Path, content: str) -> None:
    private_dir(path.parent)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ValueError("Artefact must be an unshared regular file")
    fd, temporary = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def retention_days(config: Mapping[str, str], key: str, default: int) -> int:
    value = config.get(key, str(default))
    if not re.fullmatch(r"\d{1,4}", value) or int(value) > 3650:
        raise ValueError(f"{key} must be 0-3650 days")
    return int(value)


def prune(directory: Path, pattern: str, days: int) -> None:
    cutoff = time.time() - days * 86400
    for path in directory.glob(pattern):
        if days == 0 and path.name.endswith(".sha256"):
            continue
        if (
            not path.is_symlink()
            and path.is_file()
            and (days == 0 or path.stat().st_mtime < cutoff)
        ):
            path.unlink()


def ai_debug(run_dir: Path, name: str, content: str, config: Mapping[str, str]) -> None:
    if not re.fullmatch(r"ai-[a-z.-]+", name):
        raise ValueError("Invalid AI artefact name")
    days = retention_days(config, "AMS_AI_DEBUG_RETENTION_DAYS", 0)
    # Sweep this customer's prior runs as well; never traverse another customer's directory.
    for raw in run_dir.parent.glob("*/raw"):
        if not raw.is_symlink() and not raw.parent.is_symlink():
            prune(raw, "ai-*", days)
    cleaned = text(content, configured_secrets(config), ai=True)
    if days == 0:
        name += ".sha256"
        cleaned = hashlib.sha256(cleaned.encode()).hexdigest() + "\n"
    private_write(run_dir / "raw" / name, cleaned)


def json_text(value: Any, secrets: tuple[str, ...] = ()) -> str:
    return json.dumps(sanitise(value, secrets), indent=2, ensure_ascii=False, default=str) + "\n"
