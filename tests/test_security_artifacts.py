"""Inspect real persisted/UI/report surfaces and creation permissions with FAKE secrets."""

import base64
import json
import logging
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from test_engine import context
from test_security_ai import CANARY, OPAQUE, RecordingAI
from test_ui import running

from ams_assess import cli, runner, store
from ams_assess.connectors import auth
from ams_assess.connectors.privacy import redact
from ams_assess.data_boundary import ai_debug, private_write
from ams_assess.models import ModuleResult, create_run_dir


def test_suite_cannot_load_operator_credentials(tmp_path, monkeypatch):
    assert Path.home().is_relative_to(tmp_path)
    assert auth.config_dir().is_relative_to(tmp_path)
    assert not cli.config().get("ANTHROPIC_API_KEY")
    assert not cli.config().get("DATAFORSEO_PASSWORD")
    assert os.environ["AMS_LIVE_SMOKE"] == "0"
    private = auth.config_dir()
    private.mkdir(parents=True)
    (private / "credentials.env").write_text("ANTHROPIC_API_KEY=" + CANARY)
    assert cli.config()["ANTHROPIC_API_KEY"] == CANARY
    monkeypatch.setenv("ANTHROPIC_API_KEY", "CANARY-environment-precedence")
    assert cli.config()["ANTHROPIC_API_KEY"] == "CANARY-environment-precedence"


def test_credentials_confined_across_actual_saved_and_presentation_surfaces(
    tmp_path,
    monkeypatch,
    caplog,
    capsys,
):
    import anthropic

    encoded = base64.b64encode(OPAQUE.encode()).decode()
    reflected = f"Provider reflected {CANARY} {encoded} Bearer hidden-token"
    provider = RecordingAI(error=True)
    monkeypatch.setattr(anthropic, "Anthropic", provider.client)

    def collect(ctx):
        logging.getLogger("httpx").warning(reflected)
        result = ModuleResult(name="security", reason=reflected)
        eid = result.add_evidence(
            ctx.target,
            reflected,
            {
                "status": 200,
                "error": reflected,
                "screenshot_metadata": {"title": reflected, "source": reflected},
            },
        )
        result.finding("synthetic", reflected, "security", [eid], reflected, "Review safely")
        result.raw_paths = [ctx.raw("security", {"error": reflected})]
        return result

    monkeypatch.setattr(
        runner, "REGISTRY", {"security": SimpleNamespace(categories={"security"}, run=collect)}
    )
    monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
    ctx = context(
        tmp_path,
        categories={"security"},
        store_root=tmp_path,
        config={
            "AMS_AI_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": CANARY,
            "DATAFORSEO_PASSWORD": OPAQUE,
            "AMS_AI_DEBUG_RETENTION_DAYS": "1",
        },
    )
    result = runner.run(ctx, formats={"md", "html", "pdf"})
    assert result.get("store_status") != "failed"
    assert set(result["report_files"]) == {"report.md", "report.html", "report.pdf"}
    current = store.load(tmp_path, "test")
    assert current is not None
    assert current["runs"][-1]["ai"]["provider"] == "anthropic"
    with running(tmp_path) as (server, client):
        response = client.get(server.url(customer="test"))
        assert response.status_code == 200
        ui = response.text
    texts = [
        json.dumps(result),
        json.dumps(current),
        ui,
        caplog.text,
        capsys.readouterr().out,
        json.dumps(provider.received),
    ]
    for path in (tmp_path / "test").rglob("*"):
        if path.is_file():
            if path.suffix == ".pdf":
                reader = PdfReader(path)
                texts.append("\n".join(p.extract_text() or "" for p in reader.pages))
                texts.append(str(reader.metadata))
            else:
                texts.append(path.read_text())
    combined = "\n".join(texts)
    for secret in [CANARY, OPAQUE, encoded, "hidden-token"]:
        assert secret not in combined
    assert not list(ctx.run_dir.glob("*.sh"))


@pytest.mark.parametrize("value", [CANARY, OPAQUE])
def test_connector_redacts_encoded_secrets_in_keys_and_reflected_errors(value):
    encoded = base64.b64encode(value.encode()).decode()
    result = redact({encoded: value, "error": {"message": encoded}}, (value,))
    assert value not in json.dumps(result) and encoded not in json.dumps(result)


def test_private_creation_modes_even_with_permissive_umask(tmp_path, monkeypatch):
    created = []
    original_open = os.open

    def inspect(path, flags, mode=0o777, **kwargs):
        fd = original_open(path, flags, mode, **kwargs)
        if flags & os.O_CREAT:
            created.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    monkeypatch.setattr(os, "open", inspect)
    old = os.umask(0)
    try:
        run = create_run_dir(tmp_path / "new-root", "synthetic")
        ai_debug(run, "ai-prompt.txt", CANARY, {"AMS_AI_DEBUG_RETENTION_DAYS": "1"})
        ai_debug(run, "ai-claude.stderr.txt", CANARY, {"AMS_AI_DEBUG_RETENTION_DAYS": "1"})
        private_write(run / "new/nested/file.txt", "synthetic")
    finally:
        os.umask(old)
    assert created and set(created) == {0o600}
    for path in (tmp_path / "new-root").rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert CANARY not in (run / "raw/ai-prompt.txt").read_text()


def test_debug_default_hash_and_configurable_retention_are_customer_scoped(tmp_path):
    current = create_run_dir(tmp_path, "one")
    other = create_run_dir(tmp_path, "two")
    ai_debug(other, "ai-prompt.txt", "other customer", {"AMS_AI_DEBUG_RETENTION_DAYS": "7"})
    ai_debug(current, "ai-prompt.txt", "sensitive full prompt", {})
    assert not (current / "raw/ai-prompt.txt").exists()
    digest = current / "raw/ai-prompt.txt.sha256"
    assert len(digest.read_text().strip()) == 64
    ai_debug(
        current, "ai-response.json", "sanitised response", {"AMS_AI_DEBUG_RETENTION_DAYS": "1"}
    )
    retained = current / "raw/ai-response.json"
    assert retained.read_text() == "sanitised response"
    old = time.time() - 2 * 86400
    os.utime(retained, (old, old))
    following = create_run_dir(tmp_path, "one")
    ai_debug(following, "ai-prompt.txt", "new prompt", {"AMS_AI_DEBUG_RETENTION_DAYS": "1"})
    assert not retained.exists()
    assert (other / "raw/ai-prompt.txt").read_text() == "other customer"
    ai_debug(following, "ai-response.json", "fresh response", {})
    assert not list(following.glob("raw/*.txt"))
    assert (following / "raw/ai-response.json.sha256").exists()
    assert digest.exists()


@pytest.mark.parametrize("value", ["-1", "forever", "3651", "1.5"])
def test_invalid_retention_fails_before_writing(tmp_path, value):
    run = create_run_dir(tmp_path, "one")
    with pytest.raises(ValueError):
        ai_debug(run, "ai-prompt.txt", "private", {"AMS_AI_DEBUG_RETENTION_DAYS": value})
    assert not list((run / "raw").iterdir())


def test_backup_permissions_redaction_and_expiry(tmp_path, monkeypatch):
    store.set_profile(tmp_path, "one", {"name": "Synthetic"})
    path = tmp_path / "one/assessment.json"
    value = json.loads(path.read_text())
    value["schema_version"] = 2
    value["legacy_diagnostic"] = CANARY
    path.write_text(json.dumps(value))
    monkeypatch.setenv("AMS_BACKUP_RETENTION_DAYS", "1")
    store.load(tmp_path, "one")
    backups = list(path.parent.glob("assessment.json.backup-*"))
    assert len(backups) == 1 and CANARY not in backups[0].read_text()
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    old = time.time() - 2 * 86400
    os.utime(backups[0], (old, old))
    store.load(tmp_path, "one")
    assert not backups[0].exists()


def test_debug_symlinks_and_filenames_cannot_escape(tmp_path):
    run = create_run_dir(tmp_path, "one")
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    (run / "raw/ai-prompt.txt").symlink_to(outside)
    with pytest.raises(ValueError):
        ai_debug(run, "ai-prompt.txt", "modified", {"AMS_AI_DEBUG_RETENTION_DAYS": "1"})
    for name in ["../outside", "ai-../../outside", "ai-x\n.sh", "ai-x/other"]:
        with pytest.raises(ValueError):
            ai_debug(run, name, "modified", {})
    assert outside.read_text() == "unchanged"


def test_debug_redacts_json_escaped_credentials_and_encoded_canary_assignments(tmp_path):
    run = create_run_dir(tmp_path, "synthetic")
    secret = 'private"\\password-000000000000é'
    encoded = base64.b64encode(CANARY.encode()).decode()
    body = json.dumps({"diagnostic": secret, "other": "error=" + encoded})
    ai_debug(
        run,
        "ai-response.json",
        body,
        {"ANTHROPIC_API_KEY": secret, "AMS_AI_DEBUG_RETENTION_DAYS": "1"},
    )
    stored = (run / "raw/ai-response.json").read_text()
    assert json.dumps(secret)[1:-1] not in stored
    assert secret not in stored and encoded not in stored


def test_report_files_are_private_before_runner_permission_sweep(tmp_path):
    from ams_assess.report import write_outputs

    old = os.umask(0)
    try:
        outputs = write_outputs(tmp_path, "# Synthetic private report\n", {"md", "html", "pdf"})
    finally:
        os.umask(old)
    assert set(outputs) == {"report.md", "report.html", "report.pdf"}
    assert all(stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600 for name in outputs)
