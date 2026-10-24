"""Minimise provider output before it reaches persistence, UI, reports or AI."""

import logging
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

SENSITIVE = re.compile(
    r"token|secret|password|authorization|credential|api.?key|private.?key", re.I
)
EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Z0-9.-]+\.[A-Z]{2,})", re.I)
TOKEN = re.compile(r"\b(?:ya29\.[\w.-]+|Bearer\s+\S+|eyJ[\w.-]+)", re.I)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): "[redacted]"
            if SENSITIVE.search(str(k)) and k != "credential_ref"
            else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for _ in range(3):
            value = unquote(value)
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        value = TOKEN.sub("[redacted]", value)
        value = re.sub(
            r"(?i)(?:access_token|refresh_token|password|secret|api_key)\s*[=:]\s*[^\s&]+",
            "[redacted]",
            value,
        )
        return EMAIL.sub(r"[redacted]@\1", value)[:2000]
    return value


def safe_url(value: str) -> str:
    """Landing paths and URLs have no query, fragment or credentials in stored evidence."""
    parts = urlsplit(value)
    if parts.scheme and parts.scheme not in {"http", "https"}:
        return "[redacted URL]"
    return redact(urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", "")))


# Transport/SDK debug logging can include OAuth bodies or pagination tokens. Silence it
# only within the current connector operation; other threads retain their normal logging.

_PRIVATE_IO: ContextVar[bool] = ContextVar("connector_private_io", default=False)


class _PrivateIOFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_IO.get()


_PRIVATE_FILTER = _PrivateIOFilter()


@contextmanager
def private_logs():
    loggers = [
        logging.getLogger(),
        *[
            item
            for item in list(logging.Logger.manager.loggerDict.values())
            if isinstance(item, logging.Logger)
            and item.name.startswith(
                ("httpx", "httpcore", "oauthlib", "requests_oauthlib", "google.auth", "urllib3")
            )
        ],
    ]
    for logger in loggers:
        if logger.name != "root" and _PRIVATE_FILTER not in logger.filters:
            logger.addFilter(_PRIVATE_FILTER)
        for handler in logger.handlers:
            if _PRIVATE_FILTER not in handler.filters:
                handler.addFilter(_PRIVATE_FILTER)
    token = _PRIVATE_IO.set(True)
    try:
        yield
    finally:
        _PRIVATE_IO.reset(token)
