# AGENTS.md

The working rules for this repository are in [CLAUDE.md](CLAUDE.md). Follow them as
written, including the task-to-skill routing table and safety rules. They apply to
every AI agent, including Codex.

## Skills

Skills live in `.claude/skills/<name>/SKILL.md`. `.agents/skills` is a symlink to the
same folder, so Codex discovers the same `scoutsuite-cloud-audit` and `api-discovery`
files. Python skills from ECC are managed separately at user scope; see CLAUDE.md.
