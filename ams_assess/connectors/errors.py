"""Bounded error inputs and provider-independent classification before retries."""

import json
from collections.abc import Mapping
from typing import Any

from ams_assess.connectors.core import ErrorClassification
from ams_assess.connectors.privacy import SENSITIVE, redact

MAX_ERROR_BODY_BYTES = 16_384
MAX_ERROR_DETAIL_CHARS = 2_000
# Keep machine-readable error information ahead of potentially large diagnostics.
_PRIORITY = ("error", "status", "code", "reason", "details", "metadata", "quota_limit_value")


def error_body(value: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    """Copy/redact an error into <=16 KiB of JSON, <=8 levels and <=64 items per container."""

    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=True).encode())

    def bounded(value: Any, budget: int, depth: int = 0) -> Any:
        if depth >= 8:
            return None
        if isinstance(value, dict):
            output = {}
            remaining = budget - 2
            keys = sorted(
                value, key=lambda key: _PRIORITY.index(key) if key in _PRIORITY else len(_PRIORITY)
            )
            for key in keys[:64]:
                safe_key = redact(str(key), secrets)[:128]
                remaining -= size(safe_key) + 4  # colon, comma and spacing
                if remaining < 16:
                    break
                item = "[redacted]" if SENSITIVE.search(str(key)) else value[key]
                clean = bounded(item, remaining, depth + 1)
                output[safe_key] = clean
                remaining -= size(clean)
            return output
        if isinstance(value, list):
            output = []
            remaining = budget - 2
            for item in value[:64]:
                if remaining < 16:
                    break
                clean = bounded(item, remaining - 2, depth + 1)
                output.append(clean)
                remaining -= size(clean) + 2
            return output
        clean = redact(value, secrets)
        if isinstance(clean, str):
            while size(clean) > budget:
                clean = clean[: len(clean) // 2]
        return clean if size(clean) <= budget else None

    return bounded(value, MAX_ERROR_BODY_BYTES) if isinstance(value, dict) else {}


def default_error_classifier(status_code: int, body: Mapping[str, Any]) -> ErrorClassification:
    codes: set[str] = set()
    zero_quota = False

    def inspect(value: Any) -> None:
        nonlocal zero_quota
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"status", "code", "reason"} and isinstance(item, str):
                    codes.add(item.upper())
                if key == "quota_limit_value" and str(item) == "0":
                    zero_quota = True
                inspect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                inspect(item)

    inspect(body)
    if codes & {"SERVICE_DISABLED", "ACCESSNOTCONFIGURED", "API_DISABLED"}:
        return ErrorClassification(
            "api_not_enabled", "Enable the provider API in the OAuth project"
        )
    if codes & {"APPROVAL_REQUIRED", "API_NOT_APPROVED"}:
        return ErrorClassification("approval_required", "Provider API approval required")
    if "RESOURCE_EXHAUSTED" in codes and zero_quota:
        return ErrorClassification(
            "approval_required", "Provider API approval required (zero quota)"
        )
    if codes & {"LICENCE_REQUIRED", "LICENSE_REQUIRED", "LICENSEREQUIRED", "LICENCEREQUIRED"}:
        return ErrorClassification(
            "licence_required", "A provider licence is required for this read"
        )
    if status_code == 401:
        return ErrorClassification("credential_expired")
    if status_code == 403 or codes & {
        "PERMISSION_DENIED",
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        "INSUFFICIENTPERMISSIONS",
        "INSUFFICIENT_SCOPES",
        "INSUFFICIENTSCOPE",
        "INSUFFICIENTSCOPES",
    }:
        return ErrorClassification("permission_required")
    if status_code == 404:
        return ErrorClassification("not_found", "Bound resource was not found")
    if status_code == 429 or "RESOURCE_EXHAUSTED" in codes:
        return ErrorClassification("rate_limited", "Provider rate limit reached; retry later")
    if status_code >= 500:
        return ErrorClassification(
            "provider_unavailable", "Provider unavailable after bounded retries"
        )
    return ErrorClassification("failed", f"Provider rejected read (HTTP {status_code})")
