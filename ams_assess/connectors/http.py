"""Fail-closed read transport with finite request, retry, response and wall-time budgets."""

import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from ams_assess.connectors.core import AuthMaterial, Connector, ConnectorError
from ams_assess.connectors.privacy import private_logs


class ReadClient:
    def __init__(
        self,
        connector: Connector,
        auth: AuthMaterial,
        *,
        transport=None,
        seconds: float = 45,
        max_requests: int = 80,
    ):
        self.connector, self.auth = connector, auth
        self.deadline = time.monotonic() + seconds
        self.max_requests, self.requests = max_requests, 0
        if transport is None and connector.transport_factory:
            transport = connector.transport_factory(auth)
        self.client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)
        self.quota_exhausted: set[str] = set()

    def close(self) -> None:
        self.client.close()

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorError("provider_unavailable", "Collection deadline reached")
        return remaining

    @private_logs()
    def request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        # Queries are supplied separately so a pattern can never accidentally allow a suffix.
        if not any(op.allows(method, url) for op in self.connector.read_allowlist):
            raise ConnectorError("failed", "Read allowlist refused this method or endpoint")
        if set(kwargs) - {"params", "json"}:
            raise ConnectorError("failed", "Read transport options are not allowlisted")
        host = urlsplit(url).hostname or ""
        if host in self.quota_exhausted:
            raise ConnectorError("provider_unavailable", "Provider quota exhausted; retry later")
        for attempt in range(3):
            if self.requests >= self.max_requests:
                raise ConnectorError("provider_unavailable", "Request cap reached")
            self.requests += 1
            retry_after = 0.0
            try:
                with self.client.stream(
                    method,
                    url,
                    headers=self.auth.headers,
                    timeout=min(10.0, self.remaining()),
                    **kwargs,
                ) as response:
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        self.remaining()
                        chunks.extend(chunk)
                        if len(chunks) > 2_000_000:
                            raise ConnectorError("failed", "Provider response exceeded size cap")
                    response._content = bytes(chunks)
                    status = response.status_code
                    if status == 200:
                        body = response.json()
                        if not isinstance(body, dict):
                            raise ConnectorError("failed", "Invalid provider response")
                        quota = body.get("propertyQuota", {})
                        if any(
                            isinstance(v, dict) and v.get("remaining") == 0
                            for k, v in quota.items()
                            if k != "concurrentRequests"
                        ):
                            self.quota_exhausted.add(host)
                        return body
                    if status == 401:
                        raise ConnectorError(
                            "credential_expired",
                            "Credential expired/revoked — re-authorise with "
                            + self.connector.auth_command,
                        )
                    if status == 403:
                        # Inspect only machine codes, never echo the provider's message/body.
                        try:
                            error = response.json().get("error", {})
                            reasons = {
                                d.get("reason")
                                for d in error.get("details", [])
                                if isinstance(d, dict)
                            } | {
                                d.get("reason")
                                for d in error.get("errors", [])
                                if isinstance(d, dict)
                            }
                        except (ValueError, AttributeError, TypeError):
                            reasons = set()
                        if reasons & {"SERVICE_DISABLED", "accessNotConfigured", "API_DISABLED"}:
                            raise ConnectorError(
                                "api_not_enabled", "Enable the provider API in the OAuth project"
                            )
                        if reasons & {"APPROVAL_REQUIRED", "API_NOT_APPROVED"}:
                            raise ConnectorError("approval_required", "Provider approval required")
                        raise ConnectorError(
                            "permission_required",
                            "Read permission required: "
                            + ", ".join(self.connector.scopes)
                            + "; verify the resource reader role",
                        )
                    if status == 404:
                        raise ConnectorError("not_found", "Bound resource was not found")
                    if status != 429 and status < 500:
                        raise ConnectorError("failed", f"Provider rejected read (HTTP {status})")
                    try:
                        retry_after = float(response.headers.get("Retry-After", "0"))
                    except ValueError:
                        retry_after = 0.0
            except (httpx.TimeoutException, httpx.TransportError):
                pass
            except (ValueError, TypeError):
                raise ConnectorError("failed", "Invalid provider response") from None
            if attempt < 2:
                delay = max(0.25 * 2**attempt, min(retry_after, 2))
                if self.remaining() <= delay:
                    break
                time.sleep(delay)
        raise ConnectorError("provider_unavailable", "Provider unavailable after bounded retries")
