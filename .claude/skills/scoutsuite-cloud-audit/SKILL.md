---
name: scoutsuite-cloud-audit
description: Audit an explicitly identified cloud account (AWS/Azure/GCP) and summarize findings. Use ScoutSuite for authorised read-only configuration collection; remediation requires separate human approval.
---

# ScoutSuite cloud audit

Use [ScoutSuite](https://github.com/nccgroup/ScoutSuite) as an external CLI.
It is GPLv2; do not vendor its source, rules or report assets into this repository.
These are original operating instructions. Keep it out of `pyproject.toml` and `uv.lock`.

## Establish scope and identity

- Require explicit authorisation naming the provider, account IDs (AWS account,
  Azure tenant/subscription, or GCP project), allowed regions/services and output location.
  If the account or authorisation is missing, ask before any cloud access.
- Use a dedicated least-privilege, read-only audit identity per account. Check current
  [provider setup guidance](https://github.com/nccgroup/ScoutSuite/wiki/Setup) and official
  cloud IAM documentation; use only the reads needed for the agreed scope. Do not
  create identities, change IAM or elevate permissions as part of collection.
- Verify the authenticated account/project/subscription matches the authorised IDs with
  a read-only identity check before scanning. Do not rely on an implicit default profile,
  enumerate all accessible accounts or reuse one client's credentials for another.
- Use approved external credential storage or short-lived sessions. Never put secret
  values in commands, logs, reports shared in chat, or Git. Treat access denial and
  unsupported checks as coverage gaps; do not grant more access automatically.

## Install and verify locally

ScoutSuite supports Python 3.9–3.11 only; the engine uses Python >=3.12. From the
repository root, create a separate gitignored environment and install the pinned release:

```sh
uv venv --python 3.11 --seed .tools/scoutsuite-venv
.tools/scoutsuite-venv/bin/python -m pip install 'ScoutSuite==5.14.0'
.tools/scoutsuite-venv/bin/scout --help
```

If Python 3.11 cannot be obtained through uv, document the failure and skip installation
and execution; do not fall back to the engine's Python. Stop and report any install or
CLI failure rather than silently changing the version. Install the published package;
never run scripts from an upstream clone. Any upstream inspection must use a fresh
empty directory under `/tmp` and remain read-only.

## Collect only the authorised scope

Inspect `.tools/scoutsuite-venv/bin/scout <provider> --help` for the pinned version.
Set an explicit output directory outside Git or under gitignored `reports/`. Use a new
private directory per account/run (`umask 077`); keep HTML, JSON, logs and raw configuration
together and confidential. Never commit reports or credentials, including sanitized
copies of real client reports. Confirm the destination is outside the repo or ignored
with `git check-ignore` before running.

Build only the command for the authorised provider, with explicit scope selectors:

| Provider | Identity and scope arguments |
| --- | --- |
| AWS | `aws --profile APPROVED_READONLY_PROFILE`; verify its AWS account ID first and restrict regions/services to the agreed scope |
| Azure | `azure --cli --subscriptions APPROVED_SUBSCRIPTION_ID`; verify the CLI identity's tenant and subscription first |
| GCP | `gcp --user-account --project-id APPROVED_PROJECT_ID`; verify the authenticated read-only identity first |

Append `--report-dir PRIVATE_RUN_DIRECTORY --no-browser` and the agreed region/service
filters supported by that provider. Replace placeholders with authorised values; never
run all three examples by default. Do not use all-account/subscription/project options,
mutating commands, credential dumps or external report uploads. Confirm the exit status
and record permission/API errors and incomplete coverage without claiming a clean audit.

## Summarize and separate remediation

Return the account/scope, collection time, tool version, coverage and limitations, then
prioritized findings with severity, affected resource references and report evidence.
Keep sensitive identifiers and raw reports in the approved private output location;
use redacted references in any shared summary. Verify material findings against official
provider documentation and distinguish observations from recommendations.

Put proposed remediation in a separate plan with impact, owner, verification and rollback.
Human approval of that plan is required before any resource, policy or permission change;
authorisation to audit never authorises remediation.
