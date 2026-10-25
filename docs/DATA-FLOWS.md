# Assessment data flows and credential boundary

This describes implemented behaviour, not a provider retention or training guarantee.
Track A owns process credentials/lifetime, AI projection, and private artefacts. Network
admission, redirect/SSRF controls, connector destination pinning, browser context isolation,
HTML/PDF escaping and the local UI remain separate controls.

## What leaves the machine

| Operation | Destination | Outbound data and purpose |
| --- | --- | --- |
| Public collection | Selected website and URLs discovered by the existing collectors | HTTP requests for pages, robots/sitemaps, assets and WordPress public inventory; SiteOne/Chromium render or inspect public pages. The existing browser/network rules determine which requests are allowed. |
| Domain/email | DNS resolvers, RDAP, configured DNSBLs; SMTP servers only with the existing opt-in | Domain/IP lookups and optional TLS probes. A Spamhaus DQS key authenticates its DNS lookup. |
| Performance | Google PageSpeed Insights and Chrome UX Report APIs | Selected URLs/origins and device/strategy; configured API keys authenticate requests. |
| Public security | Mozilla Observatory | Target host for its public security scan. |
| Search discovery | DataForSEO | Business/profile search terms, location/language, selected keywords and domain; HTTP Basic credentials authenticate requests. Existing explicit paid-call caps apply. |
| Connected reads | Google Analytics, Search Console, Business Profile, Workspace; Microsoft Graph; Bing Webmaster | Explicitly bound resource IDs, selected date ranges, read operations and pagination; the connector's own OAuth token/API key. Provider responses are reduced/redacted before evidence is saved. |
| WordPress SSH | Explicitly configured SSH host | Fixed read-only WP-CLI/PHP inventory commands, approved path/resource selectors, SSH authentication. Private key contents are not command arguments or evidence. Local cancellation kills local SSH and its children; remote commands also retain their existing remote timeout. Disconnecting cannot guarantee termination of a remote daemon. |
| Anthropic AI (explicit choice) | Anthropic API through the SDK | The text projection below, schema and analyst instructions, authenticated by the configured API key. The endpoint is pinned to `https://api.anthropic.com`; `ANTHROPIC_BASE_URL` is ignored. No tools are supplied. |
| Claude CLI AI (explicit choice) | Providers contacted by the installed Claude CLI/account | The same text projection, schema and instructions via stdin. Uses the CLI's own login; provider/account type is recorded as `claude-cli`/`cli-login`. No claim is made about the installed CLI's internal telemetry or service retention. |

Provider retention/training: **see provider account settings**. AMS has no code-enforced
control over a provider's retained copies. Offline report rendering sends no AI request.
“No AI” disables AI interpretation only; selected public/connected collection still runs.

## AI choice and input

`AMS_AI_PROVIDER=anthropic|claude-cli|none` is an explicit choice. Unset defaults to `none`.
An API failure does not switch to CLI or another account. Invalid values produce an AI
configuration error while deterministic reporting continues. Run metadata records `provider`
and `account_type` (`api-key`, `cli-login`, or `none`), including failed attempts. Neither an
API key nor its account identifier is stored in this metadata.

Use `ams-assess run ... --no-ai`, or `AMS_AI_PROVIDER=none`, for zero AI calls. A customer
profile JSON supplied with `ams-assess customer --customer <slug> --file <file>` may set
`"no_ai": true`; that saved policy also disables AI when an operator chooses a provider.
These switches do not remove earlier saved AI narratives from historical reports.

`ai_payload.py` declares recursive field policies. The outbound envelope contains the customer
label, clean target/resolved URLs, categories, local-fixture flag, collection counts/statuses,
diagnostic reasons, finding descriptions/recommendations and evidence IDs/provenance.
Evidence data is projected separately by source:

- Browser: page title, language/canonical, bounded visible-text summary, headings, selected
  meta/image/form fields. Raw HTML, comments, hidden HTML, JSON-LD, scripts, request lists,
  cookies, browser profiles and screenshots are not included.
- SiteOne: response URL/status/type/title, crawl counts/limits and diagnostic reasons.
- Security/Observatory: selected header/TLS fields, grade, score and test counts.
- Performance: selected outcome, score and metric fields, URL/device and diagnostic detail.
- Domain/email: selected DNS/SPF/DMARC record/status fields.
- Presence/site checks: selected platform/status, sampled URLs and check descriptions.
- Profile/discovery: selected business context and statement fields, where present.
- Registered connectors: only `status`, `rows_returned`, `truncated` from `ai_aggregate`.
  Raw users, mailboxes, tenants, reviews/reviewer names and resource inventories have no
  projection. Legacy GSC/DataForSEO payloads have no data projection. Unknown fields/sources
  fail closed to empty data; citations and deterministic findings remain available.

Finding lifecycle history, scope/identity bindings, credential references, permissions,
budgets and destinations are omitted. Query strings, fragments and URL userinfo are removed
from outbound text. All emails are removed (including business contacts, a deliberate
minimisation). Configured secrets, FAKE canaries, bearer/basic tokens, common API-key shapes
and secret assignments are redacted, including URL-encoded and base64 representations.
Public third-party browser keys found in customer page source (Google `AIza...` keys) are
replaced everywhere evidence is saved, reported, shown or sent to AI by
`[redacted:google-key:...<last 4>:sha256-<8 hex>]`, so a finding remains traceable to the
key without storing it. An operator's own configured key is fully redacted without a
fingerprint; a percent-encoded key is redacted as a whole segment, also without a fingerprint.
This is pattern/known-value redaction, not a universal detector for arbitrary disguised
secrets or personal names embedded in free prose. Do not put private inventories into
public page text or customer narrative fields.

The parser maps only approved finding/text fields. It discards model-supplied scope, events,
resolution, bindings, permissions, commands and other fields. Existing deterministic
observations remain authoritative. CLI execution disables tools, MCP, settings sources,
hooks, slash commands and session persistence. Injection content can influence wording;
it cannot authorize reads, commands, paid calls, resource changes or tool access.

## Processes and local artefacts

`subproc.py` is the single process boundary. Every process gets an allowlisted environment,
a new process group/session, owner-only creation umask and a bounded lifetime. SiteOne and
DNS receive PATH/locale/temp values; SSH gets fixed PATH/locale only. Claude additionally
receives HOME/USER and its config-location variables to find its login. Playwright's Node
driver and browser/PDF process receive only PATH/locale/temp, HOME and the browser binary
location. Provider keys, proxy settings, SSH agent sockets and interpreter injection
variables are not inherited. The engine does not temporarily replace the parent's environment.

Timeout/cancellation kills the process group and enumerated descendants, including browser
children in separate groups. SIGINT/SIGTERM and Python task cancellation unwind cleanup.
The process-table snapshot is not an OS sandbox against an intentionally daemonising executable;
use trusted installed binaries. The Playwright driver spawn wrapper fails closed if its
expected startup path changes. Tests inspect the actual driver environment and real child PIDs.

Run/customer directories are 0700; private JSON/debug files, backups and rendered outputs
are created 0600. Provider diagnostics are redacted before raw writes; connector credentials
are removed before persistence; collection/SDK transport logs are suppressed during private
operations. These controls do not retroactively scrub arbitrary existing customer artefacts.

| Setting | Default | Behaviour |
| --- | --- | --- |
| `AMS_AI_DEBUG_RETENTION_DAYS` | `0` | Store only SHA-256 of sanitised prompt/response/stderr; no full prompt. Positive 1–3650 retains sanitised text for that many days. Expired debug files in this customer's runs are removed on the next AI debug write. Setting 0 removes retained text then; hashes remain with the run. No background cleanup while AMS is stopped. An invalid value logs a warning and uses 0. |
| `AMS_BACKUP_RETENTION_DAYS` | `30` | Environment, `credentials.env` or `AMS_ENV_FILE` setting, 0–3650; an invalid value logs a warning and uses 30. Old migration backups are pruned when the customer store is loaded; 0 removes prior backups on access. Newly required migration backups protect the migration until the next access. Backups are valid JSON copies of the pre-migration store with recognisable secret values redacted. |

`AMS_CONFIG_DIR` selects the private credential/config directory; `AMS_ENV_FILE` selects an
optional engine dotenv file. Tests replace HOME/config before imports, clear inherited
credentials, use fake provider transports, block external sockets/DNS, and allow only local
fixture servers. Test browser binaries live in `tools/playwright`; no operator browser
profile is opened. Synthetic canaries inspect actual prompts, persisted files, logs,
register/UI output, Markdown, HTML and extracted PDF text/metadata.

## Regression map

| Control | Regression files |
| --- | --- |
| Every child environment is allowlisted; raw process call-site inventory | `tests/test_security_processes.py`, existing crawler/DNS/SSH tests |
| Process groups, hard deadlines, SIGINT/SIGTERM and task cancellation; real browser children | `tests/test_security_processes.py`, `tests/test_wp_cli.py` |
| No AI capabilities or authority mutations; scope URLs derived from citations | `tests/test_security_ai.py`, `tests/test_security_processes.py` |
| Explicit provider/account metadata, no account fallback; run/provider/customer no-AI reports | `tests/test_security_ai.py`, `tests/test_security_artifacts.py` |
| Recursive payload field allowlists, URLs, emails and secret/encoded-secret removal | `tests/test_security_ai.py`, `tests/test_security_artifacts.py` |
| Public page-source keys fingerprinted in stored, rendered and AI output | `tests/test_security_public_keys.py` (fixture `fixtures/public-key/`) |
| Credential confinement in actual saved and rendered/UI/log surfaces and reflected errors | `tests/test_security_artifacts.py`; existing connector privacy tests |
| Private creation permissions, debug hashes, customer-scoped expiry and backup expiry | `tests/test_security_artifacts.py` |
| Hostile visible/hidden HTML, JSON-LD/meta, poisoned tool results/saved evidence, cross-customer and identifier/filename attacks | `tests/test_security_ai.py`, `tests/test_security_artifacts.py` |
| HOME/config isolation and external-network prohibition | `tests/conftest.py`, `tests/test_security_artifacts.py` |
