"""Customer-local, site-scoped snapshots of other connected sources."""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ams_assess.connectors.core import EvidenceView
from ams_assess.connectors.privacy import redact
from ams_assess.models import Evidence


def site_scope(value: Any) -> tuple[str, str, int | None, str] | None:
    """Keep URL-prefix properties distinct; never infer a site from an opaque resource ID."""
    if not isinstance(value, str):
        return None
    if value.startswith("sc-domain:"):
        host = value.removeprefix("sc-domain:").lower().removesuffix(".")
        return ("domain", host, None, "/") if re.fullmatch(r"[a-z0-9.-]+\.[a-z]+", host) else None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            return None
        return (
            parts.scheme,
            parts.hostname.removesuffix("."),
            parts.port or (443 if parts.scheme == "https" else 80),
            parts.path or "/",
        )
    except ValueError:
        return None


def connected_evidence(
    current: dict[str, Any],
    key: str,
    sources: set[str],
    incoming: list[Evidence] | None = None,
) -> tuple[EvidenceView, ...]:
    """Latest collection per other source for this binding, without reading any store/files.

    A domain property can match a root URL of the same host; URL-prefix properties must
    match exactly. Broader parent domains/prefixes and unrelated customer websites are excluded.
    Keep failed/partial samples, so analysers cannot fall back to older successful evidence.
    """
    target = site_scope(current.get("connectors", {}).get(key, {}).get("resource_id"))
    if target is None:
        return ()

    def matches(item: Evidence) -> bool:
        if item.source == key or item.source not in sources or not isinstance(item.data, Mapping):
            return False
        scope = site_scope(item.data.get("resource_id", item.data.get("property", item.url)))
        return scope is not None and (
            scope == target
            or (
                "domain" in {scope[0], target[0]}
                and scope[1] == target[1]
                and scope[3] == target[3] == "/"
            )
        )

    fresh = [item for item in incoming or [] if matches(item)]
    replaced = {item.source for item in fresh}
    latest: dict[str, int] = {}
    selected = []
    for value in current.get("evidence", []):
        if value.get("source") not in sources or value.get("source") in replaced | {key}:
            continue
        item = Evidence.model_validate(value)
        if not matches(item):
            continue
        match = re.search(r"-r(\d+)-", item.id)
        revision = int(match[1]) if match else 0
        latest[item.source] = max(latest.get(item.source, -1), revision)
        selected.append((item, revision))
    evidence = [item for item, revision in selected if revision == latest[item.source]] + fresh
    return tuple(
        EvidenceView.of(Evidence.model_validate(redact(item.model_dump()))) for item in evidence
    )
