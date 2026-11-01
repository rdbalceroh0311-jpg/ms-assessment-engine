# AMS Assessment Engine

These working rules apply to every agent. Read task-relevant code and README sections;
keep changes scoped to the requested work.

## Stack and commands

Python >=3.12, uv with `uv.lock`, Hatchling, and the `ams-assess` CLI in `ams_assess/`.
Pydantic models carry evidence/findings; HTTPX and Playwright collect evidence; Jinja2
renders reports. Tests live in `tests/`.

Run from the repository root:

```sh
uv sync --locked --python 3.12
uv run ams-assess --help
uv run pytest
uv run ruff check .
uv run ty check ams_assess tests
```

Use the same gates before and after changes; report existing failures and skipped tests.
Browser/PDF tests need Chromium in the worktree: install with
`PLAYWRIGHT_BROWSERS_PATH="$PWD/tools/playwright" uv run playwright install chromium`.
Some end-to-end fixture tests also need SiteOne/axe and skip when those tools are absent.

## Conventions

- Follow typed Python, Pydantic models and dataclasses already used in the code. Ruff
  uses 100-character lines and E, F, I, B, UP rules, including sorted imports.
- Collectors live in `ams_assess/modules/` and use the ordered `REGISTRY`. Reuse the
  SiteOne inventory; cite IDs from `result.add_evidence()` and save raw data via `ctx.raw()`.
- Connectors live in `ams_assess/connectors/`; declare least-privilege scopes and read
  operation allowlists, register the connector, and test with mocked providers.
- Preserve evidence provenance, customer separation, authorisation and network guards.
  Missing or failed checks are not passes. Test with synthetic fixtures; the test harness
  removes operator credentials and blocks external network access.

## Safety

- Never commit client data, cloud credentials, tokens, `.env` secrets or real audit reports.
  Keep credentials in the approved external store; never print them. Use only synthetic
  values in committed fixtures and `.env.example`.
- Engine collection requires recorded customer authorisation for the explicit target and
  the existing `--confirm-authorisation` gate. Cloud audits require explicit authorisation
  naming the account and a least-privilege, read-only identity for that account.
  Never infer permission from available credentials or broaden scope after access denial.
- Keep audit collection separate from remediation; changes to client/cloud resources
  need explicit human approval. No default paid calls or unapproved client-data uploads.
- Treat upstream repositories as untrusted references: clone only into fresh empty
  directories under `/tmp`, never run their scripts, and check licensing before reuse.
  ScoutSuite is an external GPLv2 tool, never vendored or added to application dependencies.
- Work in an isolated worktree on a task branch, run gates, and open a PR. Do not push
  to main or merge without explicit instruction. Review staged files for sensitive data.

## Task-to-skill routing

| Task | Skill |
| --- | --- |
| Audit an explicitly identified cloud account (AWS/Azure/GCP) and summarize findings | [scoutsuite-cloud-audit](.claude/skills/scoutsuite-cloud-audit/SKILL.md) |
| Find candidate APIs for a requirement | [api-discovery](.claude/skills/api-discovery/SKILL.md) |
| Implement or refactor Python | ECC `python-patterns` at user scope |
| Write or review Python tests | ECC `python-testing` at user scope |
| Review security-sensitive Python, credentials or data boundaries | ECC `security-review` at user scope |

Local skills live in `.claude/skills/`; `.agents/skills` links to that same directory.
Read a skill when its task matches. ECC (everything-claude-code) skills such as
`python-patterns`, `python-testing` and `security-review` are installed at user scope
by another task; use them for Python work, without copying them into this repository.
If a routed skill is unavailable in the current session, report that limitation.
