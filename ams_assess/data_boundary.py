"""Secret removal and private artefact writes, independent of provider/network code."""

import base64
import bisect
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
# Page source may also spell a secret with HTML character references, JS/JSON escapes or
# percent escapes, nested or mixed ("AIza&#83;y", "AIza\\u0053y", "AIza&amp;#x53;y").
# Digit runs are unbounded: browsers accept any number of leading zeros.
_ESCAPE = re.compile(
    r"&#[xX](?P<hex>[0-9a-fA-F]+);?|&#(?P<dec>[0-9]+);?|&[A-Za-z][A-Za-z0-9]{0,31};?"
    r"|\\u\{(?P<brace>[0-9a-fA-F]+)\}|\\u(?P<u4>[0-9a-fA-F]{4})|\\x(?P<x2>[0-9a-fA-F]{2})"
    r"|%(?P<pct>[0-9a-fA-F]{2})|\\\r\n|\\.",
    re.S,
)
_CONTROL_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
# JS line continuations: a backslash before LF, CR, CRLF, U+2028 or U+2029 decodes to nothing.
_CONTROL_ESCAPES.update(dict.fromkeys(("\n", "\r", "\r\n", "\u2028", "\u2029"), ""))
# Decoding stops at a fixed point; anything still decoding after this many levels is
# redacted rather than inspected (no legitimate page nests encodings this deep).
_MAX_LEVELS = 64
_TOKEN_BREAK = re.compile(r"[^!-~]|[\"'<>]")
# The standard library's character-reference syntax, for a display unescape that cannot raise.
_HTML_REF = re.compile(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
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
# atob() ignores ASCII whitespace: a data: URI payload, and base64 split by whitespace,
# are inspected whole.
_B64_SPACE = "\t\n\f\r "
DATA_URI = re.compile(
    r"data:[^,\s\"'<>]*;base64,(?P<payload>[A-Za-z0-9+/=_\t\n\f\r -]*[A-Za-z0-9+/=_-])", re.I
)
BASE64_GROUP = re.compile(
    r"(?<![\w+/=-])[A-Za-z0-9+/_-]+={0,2}(?:[\t\n\f\r ]+[A-Za-z0-9+/_-]+={0,2})+(?![\w+/=-])"
)
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


def _charref(digits: str, base: int) -> str:
    """A numeric character reference as browsers decode it, at any length."""
    digits = digits.lstrip("0") or "0"
    if len(digits) > 8:  # far beyond U+10FFFF; never converted (int() digit limits)
        return "�"
    return html.unescape(f"&#{'x' if base == 16 else ''}{digits};")


def unescape_html(value: str) -> str:
    """html.unescape for display that never raises on overlong numeric references."""

    def ref(match: re.Match[str]) -> str:
        body = match[1]
        if body[0] != "#":
            return html.unescape(match[0])
        hexadecimal = body[1:2] in ("x", "X")
        return _charref(body[2 if hexadecimal else 1 :].rstrip(";"), 16 if hexadecimal else 10)

    return _HTML_REF.sub(ref, value) if "&" in value else value


def _unescape(match: re.Match[str]) -> str:
    """One escape's characters; an escape no decoder would accept is returned unchanged."""
    unit = match[0]
    if match["hex"] is not None or match["dec"] is not None:
        return _charref(match["hex"] or match["dec"], 16 if match["hex"] is not None else 10)
    if unit.startswith("&"):
        return html.unescape(unit)
    if match["pct"] is not None:
        return chr(int(match["pct"], 16))
    digits = match["brace"] or match["u4"] or match["x2"]
    if digits is not None:
        digits = digits.lstrip("0") or "0"
        if len(digits) > 6 or int(digits, 16) > 0x10FFFF:
            return unit  # not a valid JS escape
        return chr(int(digits, 16))
    return _CONTROL_ESCAPES.get(unit[1:], unit[1:])


Level = tuple[list[tuple[int, int, int, int]], list[int]]


def _decode_level(value: str) -> tuple[str, Level] | None:
    """One decoding pass; each decoded escape's (out start, out end, in start, in end)."""
    parts: list[str] = []
    units: list[tuple[int, int, int, int]] = []
    position = out = 0
    for match in _ESCAPE.finditer(value):
        decoded = _unescape(match)
        if decoded == match[0]:
            continue
        parts += [value[position : match.start()], decoded]
        out += match.start() - position
        units.append((out, out + len(decoded), match.start(), match.end()))
        out += len(decoded)
        position = match.end()
    if not units:
        return None
    parts.append(value[position:])
    return "".join(parts), (units, [unit[0] for unit in units])


def _back(level: Level, start: int, end: int) -> tuple[int, int]:
    """Map a span of one level's output back to that level's input."""
    units, starts = level

    def position(index: int, first: bool) -> int:
        i = bisect.bisect_right(starts, index) - 1
        if i >= 0 and index < units[i][1]:
            return units[i][2] if first else units[i][3]
        shift = 0 if i < 0 else units[i][3] - units[i][1]
        return index + shift + (0 if first else 1)

    return position(start, True), position(end - 1, False)


def _decode(value: str) -> tuple[str, list[Level], list[tuple[int, int]]]:
    """Decoded view to a fixed point, its levels, and source spans still decoding at the bound."""
    levels: list[Level] = []
    current = value
    while (step := _decode_level(current)) is not None:
        if len(levels) == _MAX_LEVELS:
            return current, levels, [_source(levels, u[2], u[3]) for u in step[1][0]]
        current, level = step
        levels.append(level)
    return current, levels, []


def _source(levels: list[Level], start: int, end: int) -> tuple[int, int]:
    for level in reversed(levels):
        start, end = _back(level, start, end)
    return start, end


def _basic_credentials(encoded: str) -> bool:
    try:
        credentials = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except ValueError:
        return False
    # Preserve ordinary prose such as "basic voicemail" and "Basic API Access".
    return b":" in credentials


def _findings(value: str, secrets: tuple[str, ...]) -> list[tuple[int, int, int, str | None]]:
    """Every detector's (match start, redact start, end, replacement), by priority.

    A replacement of None marks an existing fingerprint, which is kept as it is.
    """
    found: list[tuple[int, int, int, str | None]] = [
        (m.start(), m.start(), m.end(), "[redacted]")
        for secret in secrets
        if secret
        for m in re.finditer(re.escape(secret), value)
    ]
    found += [(m.start(), m.start(), m.end(), None) for m in PUBLIC_KEY_MARK.finditer(value)]
    found += [
        (m.start(), m.start(), m.end(), public_key_fingerprint(m))
        for m in PUBLIC_KEY.finditer(value)
    ]
    for pattern in (TOKEN, BEARER):
        found += [(m.start(), m.start(), m.end(), "[redacted]") for m in pattern.finditer(value)]
    found += [
        (m.start(), m.start(1), m.end(), "[redacted]")
        for m in BASIC.finditer(value)
        if _basic_credentials(m[1])
    ]
    found += [
        (m.start(), m.start() + len(m["name"] + (m["eq"] or m["sep"])), m.end(), "[redacted]")
        for m in ASSIGNMENT.finditer(value)
    ]
    return found


def _secret_in(value: str, secrets: tuple[str, ...]) -> bool:
    """Any secret detector, public browser keys included, on the value or its decoded view."""
    if any(f[3] is not None for f in _findings(value, secrets)):
        return True
    decoded, levels, unresolved = _decode(value)
    return bool(unresolved) or (
        bool(levels) and any(f[3] is not None for f in _findings(decoded, secrets))
    )


def _base64_secret(payload: str, secrets: tuple[str, ...]) -> bool:
    """Decode as atob() would (whitespace ignored), at each 4-character alignment so that a
    secret after a non-base64 prefix in the same run is still found."""
    payload = "".join(payload.split())
    for offset in range(4):
        part = payload[offset:].rstrip("=")
        try:
            raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        except ValueError:
            continue
        if _secret_in(unquote(raw.decode("utf-8", "replace")), secrets):
            return True
    return False


def _base64_spans(value: str, secrets: tuple[str, ...]) -> list[tuple[int, int]]:
    """Spans of data: URI payloads, base64 runs and whitespace-split base64 hiding a secret."""
    hits = [
        m.span("payload") for m in DATA_URI.finditer(value) if _base64_secret(m["payload"], secrets)
    ]
    hits += [m.span() for m in BASE64.finditer(value) if _base64_secret(m[0], secrets)]
    hit = sorted(hits)
    for match in BASE64_GROUP.finditer(value):
        start, end = match.span()
        i = bisect.bisect_left(hit, (start, -1))
        inside = i < len(hit) and hit[i][0] < end
        joined = "".join(match[0].split())
        if not inside and len(joined) >= 16 and _base64_secret(joined, secrets):
            hits.append((start, end))
    return hits


class _Covered:
    """Disjoint covered intervals, kept sorted: claiming a span costs O(log n) amortised."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []

    def claim(self, start: int, end: int) -> list[tuple[int, int]]:
        """Cover [start, end); return the pieces of it that were not covered before."""
        i = j = bisect.bisect_right(self.ends, start)
        pieces, position = [], start
        while j < len(self.starts) and self.starts[j] < end:
            if self.starts[j] > position:
                pieces.append((position, self.starts[j]))
            position = max(position, self.ends[j])
            j += 1
        if position < end:
            pieces.append((position, end))
        if pieces:
            low = min(start, self.starts[i]) if i < j else start
            high = max(end, self.ends[j - 1]) if i < j else end
            self.starts[i:j], self.ends[i:j] = [low], [high]
        return pieces


def _apply(value: str, edits: list[tuple[int, int, str]]) -> str:
    """Replace source spans; spans that overlap (one source escape) merge into one redaction."""
    merged: list[list[Any]] = []
    for start, end, replacement in sorted(edits):
        if merged and start < merged[-1][1]:
            if (start, end, replacement) != tuple(merged[-1]):
                merged[-1][1:] = [max(end, merged[-1][1]), "[redacted]"]
            continue
        merged.append([start, end, replacement])
    result, position = [], 0
    for start, end, replacement in merged:
        result += [value[position:start], replacement]
        position = end
    return "".join(result) + value[position:]


def _decoded_secrets(value: str, secrets: tuple[str, ...]) -> str:
    """Decode escapes only to inspect: run every detector on the decoded view and replace the
    source span of each secret spelt with escapes (public keys by their fingerprint)."""
    if not any(c in value for c in "&\\%"):
        return value
    decoded, levels, unresolved = _decode(value)
    if not levels:
        return value
    taken = _Covered()
    # Fail closed past the bound: withhold the whole token around each still-encoded escape.
    breaks = [m.start() for m in _TOKEN_BREAK.finditer(value)] if unresolved else []
    edits = []
    for start, end in unresolved:
        before = bisect.bisect_left(breaks, start) - 1
        after = bisect.bisect_left(breaks, end)
        edits.append(
            (
                breaks[before] + 1 if before >= 0 else 0,
                breaks[after] if after < len(breaks) else len(value),
                "[redacted]",
            )
        )
    findings = _findings(decoded, secrets) + [
        (start, start, end, "[redacted]") for start, end in _base64_spans(decoded, secrets)
    ]
    for match_start, start, end, replacement in findings:
        source_start, source_end = _source(levels, match_start, end)
        literal = value[source_start:source_end]
        is_key = replacement is not None and replacement.startswith("[redacted:google-key")
        if replacement is not None and not is_key and literal == decoded[match_start:end]:
            continue  # spelt without escapes: the ordinary pass below handles it
        for a, b in taken.claim(start, end):
            if replacement is not None:
                whole = (a, b) == (start, end)
                edits.append((*_source(levels, a, b), replacement if whole else "[redacted]"))
    return _apply(value, edits)


def _assignment(match: re.Match[str]) -> str:
    separator = match["eq"] or match["sep"]
    # A value already reduced to a fingerprint keeps it; anything after is still redacted.
    mark = PUBLIC_KEY_MARK.match(match.string, match.start() + len(match["name"] + separator))
    if mark:
        return match.string[match.start() : mark.end()] + (
            "[redacted]" if mark.end() < match.end() else ""
        )
    return match["name"] + separator + "[redacted]"


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
    """Never raises: a value that cannot be inspected is withheld entirely."""
    try:
        return _text(value, secrets, ai=ai)
    except Exception:
        LOG.warning("A value could not be inspected for secrets and was withheld")
        return "[redacted]"


def _text(value: str, secrets: tuple[str, ...], *, ai: bool) -> str:
    # Configured secrets first: an operator's own key never becomes a fingerprint.
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
    value = _decoded_secrets(value, secrets)
    value = _encoded_spans(value, secrets)

    def basic(match: re.Match[str]) -> str:
        return "[redacted]" if _basic_credentials(match[1]) else match[0]

    value = PUBLIC_KEY.sub(public_key_fingerprint, value)
    value = BASIC.sub(basic, value)
    value = _apply(value, [(a, b, "[redacted]") for a, b in _base64_spans(value, secrets)])
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
