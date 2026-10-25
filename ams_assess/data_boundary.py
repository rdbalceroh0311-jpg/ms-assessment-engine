"""Secret removal and private artefact writes, independent of provider/network code."""

import base64
import contextlib
import hashlib
import html
import json
import logging
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


# Credential-bearing setting names, including provider/connector variables such as
# SPAMHAUS_DQS_KEY, BING_WMT_API_KEY, M365_CLIENT_SECRET or GITHUB_TOKEN.
SECRET_KEY = re.compile(
    r"password|passwd|secret|token|api.?key|access.?key|private.?key|authorization|cookie"
    r"|login|credential|(?:^|[_.-])(?:key|pass|pwd|pat|auth|sig|signature)(?:[_.-](?:file|path|json))?$",
    re.I,
)
# Token alphabet value that looks like a credential, not a word of prose.
_CREDENTIAL = r"(?:(?=[A-Za-z0-9._~+/-]*\d)[A-Za-z0-9._~+/-]{8,}|[A-Za-z0-9._~+/-]{20,})=*"
# After "Bearer" a punctuated value also counts ("Bearer hidden-token", not "bearer of").
_BEARER_VALUE = (
    r"(?:(?=[A-Za-z0-9._~+/-]*[\d._~+/-])[A-Za-z0-9._~+/-]{8,}|[A-Za-z0-9._~+/-]{20,})=*"
)
# Provider prefixes are case-sensitive, so "canary yellow" or "Sk-" prose stays intact.
TOKEN = re.compile(
    r"CANARY[-_.][A-Za-z0-9_./+=:-]+"
    r"|\b(?:AIza[\w-]{15,}|ya29[.-][\w.-]+|sk-(?:ant-)?[\w-]{16,}"
    r"|gh[pousr]_[\w]{15,}|github_pat_[\w]+|eyJ[\w-]+\.[\w-]+\.[\w-]+|GOCSPX-[\w-]{10,}"
    r"|1//[\w-]{20,})"
)
# Public browser keys (Google Maps/Firebase "AIza...") embedded in customer page source.
# No word boundary: page source may join them to other characters (e.g. "\\nAIza...").
PUBLIC_KEY = re.compile(r"AIza[\w-]{15,}")
PUBLIC_KEY_MARK = re.compile(r"\[redacted:google-key:\.\.\.[\w-]{4}:sha256-[0-9a-f]{8}\]")
# Page source may also spell a key with HTML character references, JS/JSON escapes or
# percent escapes, nested or mixed ("AIza&#83;y", "AIza\\u0053y", "AIza&amp;#x53;y").
_ESCAPED_RUN = re.compile(r"[\w&#;\\{}%-]{19,}")
_ESCAPE = re.compile(
    r"&#[0-9]+;?|&#[xX][0-9a-fA-F]+;?|&[A-Za-z][A-Za-z0-9]{1,31};"
    r"|\\u\{[0-9a-fA-F]{1,6}\}|\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\.|%[0-9a-fA-F]{2}|.",
    re.S,
)
_CONTROL_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
BEARER = re.compile(rf"\b(?:Bearer|bearer|BEARER)\s+{_BEARER_VALUE}")
_NAMES = (
    r"access_token|refresh_token|id_token|auth_token|client_secret|api[_-]?key|apikey"
    r"|key|token|sig|signature|password|passwd|secret"
)
# "key=..." (query/assignment) is always a secret; "Primary key: missing" is prose unless
# the value has a credential shape.
ASSIGNMENT = re.compile(
    rf"\b(?P<name>{_NAMES})(?:(?P<eq>=)[^\s&\"'<>]+|(?P<sep>\s*[=:]\s*){_CREDENTIAL})", re.I
)
EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
BASIC = re.compile(r"\bBasic\s+([A-Za-z0-9+/]+={0,2})", re.I)
BASE64 = re.compile(r"(?<![\w+/-])[A-Za-z0-9+/_-]{16,}={0,2}(?![\w+/=-])")
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
# Starts only at a run boundary: a long run without escapes is scanned once, not per offset.
PERCENT_RUN = re.compile(r"(?<![^\s\"'<>])[^\s\"'<>]*%[0-9A-Fa-f]{2}[^\s\"'<>]*")
LOG = logging.getLogger(__name__)


def configured_secrets(config: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(value for key, value in config.items() if SECRET_KEY.search(key) and value)


def clean_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
    except ValueError:
        return "[redacted URL]"


def public_key_fingerprint(match: re.Match[str]) -> str:
    """Traceable without the value: last four characters and a SHA-256 prefix."""
    key = match[0]
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"[redacted:google-key:...{key[-4:]}:sha256-{digest}]"


def _unescape(unit: str) -> str:
    if unit.startswith("&"):
        return html.unescape(unit)
    if unit.startswith("%"):
        return chr(int(unit[1:], 16))
    if unit.startswith(("\\u", "\\x")) and len(unit) > 3:
        return chr(min(int(unit[2:].strip("{}"), 16), 0x10FFFF))
    if unit.startswith("\\") and len(unit) == 2:
        return _CONTROL_ESCAPES.get(unit[1], unit[1])
    return unit


def _escaped_public_keys(value: str) -> str:
    """Decode escapes only to inspect; replace each encoded key's source span by its print."""

    def run(match: re.Match[str]) -> str:
        source = match[0]
        if not any(c in source for c in "&\\%"):
            return source
        # spans[i] is the source range that decoded character i came from, across levels.
        decoded, spans = source, [(i, i + 1) for i in range(len(source))]
        for _ in range(5):
            units = [(m[0], m.start(), m.end()) for m in _ESCAPE.finditer(decoded)]
            if all(len(unit) == 1 for unit, _, _ in units):
                break
            parts, next_spans = [], []
            for unit, start, end in units:
                for char in _unescape(unit):
                    parts.append(char)
                    next_spans.append((spans[start][0], spans[end - 1][1]))
            decoded, spans = "".join(parts), next_spans
        result, position = [], 0
        for key in PUBLIC_KEY.finditer(decoded):
            start, end = spans[key.start()][0], spans[key.end() - 1][1]
            if start >= position:
                result += [source[position:start], public_key_fingerprint(key)]
                position = end
        return "".join(result) + source[position:]

    return _ESCAPED_RUN.sub(run, value)


def _assignment(match: re.Match[str]) -> str:
    separator = match["eq"] or match["sep"]
    # A value already reduced to a fingerprint keeps it; anything after is still redacted.
    mark = PUBLIC_KEY_MARK.match(match.string, match.start() + len(match["name"] + separator))
    if mark:
        return match.string[match.start() : mark.end()] + (
            "[redacted]" if mark.end() < match.end() else ""
        )
    return match["name"] + separator + "[redacted]"


def _secret_in(value: str, secrets: tuple[str, ...]) -> bool:
    return bool(
        TOKEN.search(value)
        or BEARER.search(value)
        or ASSIGNMENT.search(value)
        or any(s in value for s in secrets)
    )


def _encoded_spans(value: str, secrets: tuple[str, ...]) -> str:
    """Decode percent escapes only to inspect; redact just the offending original segment."""

    def segment(part: str) -> str:
        decoded = part
        for _ in range(5):
            decoded = unquote(decoded)
        if decoded == part or not _secret_in(decoded, secrets):
            return part
        name, equals, _ = part.partition("=")
        return name + "=[redacted]" if equals and "%" not in name else "[redacted]"

    def run(match: re.Match[str]) -> str:
        return "".join(segment(part) for part in re.split(r"([?&#;])", match[0]) if part)

    return PERCENT_RUN.sub(run, value)


def text(value: str, secrets: tuple[str, ...] = (), *, ai: bool = False) -> str:
    value = _escaped_public_keys(value)
    value = _encoded_spans(value, secrets)
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
        return "[redacted]" if _secret_in(decoded, secrets) else match[0]

    def basic(match: re.Match[str]) -> str:
        try:
            credentials = base64.b64decode(match[1] + "=" * (-len(match[1]) % 4), validate=True)
        except ValueError:
            return match[0]
        # Preserve ordinary prose such as "basic voicemail" and "Basic API Access".
        return "[redacted]" if b":" in credentials else match[0]

    value = PUBLIC_KEY.sub(public_key_fingerprint, value)
    value = BASIC.sub(basic, value)
    value = BASE64.sub(encoded, value)
    value = TOKEN.sub("[redacted]", value)
    value = BEARER.sub("[redacted]", value)
    # Strip complete URLs first so query secrets cannot leave partial bearer strings behind.
    if ai:
        value = URL.sub(lambda m: clean_url(m[0]), value)
        value = EMAIL.sub("[redacted email]", value)
    return ASSIGNMENT.sub(_assignment, value)


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
    """An invalid setting never blocks loading; it falls back to the safe default."""
    value = str(config.get(key, default)).strip()
    if not re.fullmatch(r"\d{1,4}", value) or int(value) > 3650:
        LOG.warning("%s must be 0-3650 days; using the default of %d days", key, default)
        return default
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
