# SiteOne pinned HTTPS transport

The installed SiteOne version is **2.5.1.20260627**. Its `--resolve=host:port:ip`
parser and exact host/port lookup work, but its HTTP client substitutes the IP
into the request URL and restores only the HTTP Host header. TLS therefore uses
the IP instead of the original DNS hostname: SNI is absent and a hostname-only
certificate cannot authenticate that IP. The resulting connection error is
reported as `-1:CONN-FAIL` / exit 3. This explains the supplied symptom without
making a claim about all Google-hosted sites.

Upstream evidence at the v2.5.1 commit:

- [resolve parser and cache](https://github.com/janreges/siteone-crawler/blob/12b49168679dd67934d3743895a2fabb50088c02/src/engine/crawler.rs#L179)
- [Host header and IP URL substitution](https://github.com/janreges/siteone-crawler/blob/12b49168679dd67934d3743895a2fabb50088c02/src/engine/http_client.rs#L139)
- [proxy support and certificate verification](https://github.com/janreges/siteone-crawler/blob/12b49168679dd67934d3743895a2fabb50088c02/src/engine/http_client.rs#L57)

Both bare and www hosts need separate validated pins when crawled. The existing
host discovery already supplies them for an apex/www pair or a resolved redirect.
Ports 80 and 443 have separate effective destinations; a nonstandard explicit
port remains restricted to that port. These were not the HTTPS failure. The
existing IPv4 selection policy remains: IPv6-only DNS hosts are refused. The
analyzer allowlist is unchanged; disabling DNS/SSL analyzers does not disable the
HTTP client's TLS setup or verification.

There is no CLI option to keep `--resolve` while restoring the TLS hostname.
`--accept-invalid-certs` is unacceptable and would not repair missing SNI. Instead,
a loopback relay supplies numeric connection pinning using the existing public
URL validator, while SiteOne's explicit HTTP proxy option retains original URLs,
Host, SNI and certificate verification. No production SiteOne binary is modified.

The relay only accepts the current crawl's prevalidated host/port destinations,
rechecks all DNS answers before opening each connection, and requires the original
pin to remain present. HTTP redirects retain their original status and Location;
SiteOne uses `Policy::none`, and each next-hop GET or CONNECT must independently
pass the destination check before opening a connection. Refused or failed HTTP
forwards close without an HTTP response, so they are transport failures rather
than invented origin 403 responses or confirmed broken-URL findings.
SiteOne sends `Connection: close`, so each request gets a checked connection.
Unpinned destinations are refused even if crawler URL filters fail. The relay
never resolves or connects through an automatic fallback. SiteOne itself can drop
its proxy on client construction or option parsing failure. The relay counts HTTP
requests sent upstream and tunnels carrying client bytes. If SiteOne reports any
fetched HTTP responses while this count is zero, the module fails closed before
retaining crawl rows, tables, findings, pages or raw stdout/stderr. This is a
run-level zero-traffic check, not proof against a partial bypass after relay use.
HTTP forwarding strips credentials/cookies and arbitrary headers. CONNECT forwards TLS bytes only, not
proxy headers; SiteOne receives no HTTP auth configuration or inherited secrets.
Its URL handling does not propagate linked/redirected userinfo into authentication,
which is exercised against the HTTPS receiving fixture.

The listener binds only 127.0.0.1 on an ephemeral port and is restricted to the
finite crawl destination list; it is not a general/open proxy. SiteOne's CLI
rejects proxy authentication syntax, so it cannot be authenticated. Connections
are limited to eight handlers, socket operations and tunnels have deadlines,
and context exit closes the listener and all tracked sockets, including on
SIGTERM, process startup failure and timeout. The existing process boundary kills
SiteOne and descendants. Public source URLs remain in evidence; table filtering
remains a secondary integrity check, not the egress boundary.

## Local reproduction and tests

`tests/test_siteone_https.py` generates a CA and server certificate with bare/www
DNS SANs, enforces SNI, redirects bare to www, and permits only exact ephemeral
fixture origins through the test-only network hook. It contacts no customer site.

Before the fix, running
`uv run pytest -q tests/test_siteone_https.py::test_real_siteone_pinned_https_preserves_sni_host_and_redirect`
produced **1 failed test case**: `-1:CONN-FAIL (exit 3)`, and the server observed
`None` as the SNI name. The existing pinned Python transport succeeded against
that same hostname and IP. The retained old-command regression reproduces this
with both the installed release binary and the fixture-CA build.

macOS SiteOne uses platform certificate verification and ignores `SSL_CERT_FILE`.
To avoid changing the operator's keychain or disabling verification, build a
separate test-only copy:

```sh
# Requires cargo >= 1.94, plus the normal scripts/setup.sh tools.
scripts/setup-siteone-test-ca.sh
uv run pytest -q tests/test_siteone_https.py tests/test_security_siteone_pinning.py tests/test_security_tls_crawl.py tests/test_security_process_cancel.py
```

The build script verifies the exact upstream commit and adds only an explicit
fixture CA root hook to its HTTP client; hostname, chain, signature, validity and
SNI handling stay enabled. Source and binaries stay under gitignored `tools/`.
The production crawler and environment allowlist are unchanged. Missing test
setup fails explicitly rather than silently skipping these regressions.

Tests cover successful TLS and legitimate crawl/preflight redirects; rejection
of untrusted chains and hostname mismatch before HTTP; private/metadata redirects;
DNS changes to private, metadata, another loopback or a different public address;
exact ports; credential stripping; no direct fallback; and SIGTERM/timeout/startup
cleanup. The normal HTTP regression suite still uses the installed binary.

Residual limitations: the conservative IPv4-only DNS-host policy is unchanged;
no production/customer verification was attempted. HTTPS is an opaque verified
TLS tunnel, not an HTTP interception service. The relay relies on this pinned
SiteOne version's credential-free, connection-close HTTP client and its standard
TLS verifier. Tests exercise those behaviors, including linked URL userinfo.

## Verification at head 8da71e4

- `uv run pytest -q` (all `tests/`): **2,693 passed, 1 skipped**, 2,694 collected cases.
  The skip is the opt-in live-site smoke test; five unrelated SWIG deprecation warnings.
- `uv run pytest -q tests/test_siteone_https.py tests/test_security_siteone_pinning.py tests/test_security_tls_crawl.py tests/test_security_process_cancel.py`:
  **47 passed cases across 29 test functions**. The new HTTPS file contains 31 cases
  across 14 test functions.
- `uv run ruff check .`: passed.
- `uv run ty check`: All checks passed! This is the project type-checking gate.
- `bash -n scripts/setup-siteone-test-ca.sh` and `git diff --check`: passed.

## HTTP forwarding review correction

`tests/test_siteone_http_forwarding.py` reproduces external HTTP redirects being
rewritten to 403, plus refused private-DNS and temporary-DNS forwards being recorded
as broken URLs. All four real-SiteOne cases failed before the correction. They now
require the real 302 or a negative transport status, with no broken-URL finding.
The file also covers unpinned host/port and body refusals, zero-traffic rejection of
200/302/404 rows, and a real SiteOne proxy bypass against a loopback fixture. No
external redirect target is contacted. Existing mocked-crawler unit tests explicitly
simulate relay traffic; bypass tests and real crawls use the actual counter.

Validation for the HTTP forwarding correction:

- `uv run ty check`: All checks passed!
- `uv run ruff check .`: All checks passed!
- `uv run pytest -q` (all `tests/`): **2,705 passed, 1 skipped**, 2,706 collected
  cases, 5 SWIG deprecation warnings. The only skip is the opt-in live-site test.
  This includes 12 new cases across 6 functions in `tests/test_siteone_http_forwarding.py`.
- `git diff --check`: passed.
