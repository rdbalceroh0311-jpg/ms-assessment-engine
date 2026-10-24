"""Real local process trees and synthetic credentials; no providers are contacted."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from test_engine import context

from ams_assess import ai, subproc

KEYS = (
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "GOOGLE_API_KEY",
    "BING_WMT_API_KEY",
    "M365_CLIENT_SECRET",
    "SPAMHAUS_DQS_KEY",
    "PAGESPEED_API_KEY",
    "CRUX_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SSH_AUTH_SOCK",
    "CLAUDECODE",
    "PYTHONPATH",
    "NODE_OPTIONS",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
)


@pytest.mark.parametrize("kind", ["claude-cli", "siteone", "browser", "pdf", "ssh", "dns"])
def test_actual_child_environment_is_allowlisted(kind, monkeypatch):
    for key in KEYS:
        monkeypatch.setenv(key, "CANARY-" + key + "-000000000000")
    output = subproc.run(
        [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
        kind=kind,
        timeout=5,
        capture_output=True,
        text=True,
    )
    environment = json.loads(output.stdout)
    assert set(environment) <= set(subproc.environment(kind)) | {
        "LC_CTYPE",
        "__CF_USER_TEXT_ENCODING",
    }
    assert not set(environment) & set(KEYS)
    assert "CANARY" not in output.stdout
    assert os.environ["ANTHROPIC_API_KEY"].startswith("CANARY")


def test_claude_selects_restricted_process_and_no_capabilities(tmp_path, monkeypatch):
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"structured_output": {"executive_summary": ""}}),
            stderr="CANARY-stderr-secret-000000000000",
        )

    monkeypatch.setattr(ai.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(ai.subproc, "run", record)
    ctx = context(tmp_path, config={"AMS_AI_PROVIDER": "claude-cli"})
    state, _ = ai.analyse(ctx, [], [], False)
    assert state["provider"] == "claude-cli" and state["account_type"] == "cli-login"
    command, kwargs = calls[0]
    assert kwargs["kind"] == "claude-cli" and kwargs["timeout"] == 300
    for flag, value in {
        "--tools": "",
        "--mcp-config": '{"mcpServers":{}}',
        "--setting-sources": "",
        "--settings": '{"disableAllHooks":true}',
        "--max-turns": "2",
    }.items():
        assert command[command.index(flag) + 1] == value
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(
        command
    )
    assert all("CANARY" not in p.read_text() for p in (ctx.run_dir / "raw").iterdir())


def assert_dead(pids):
    deadline = time.monotonic() + 5
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.remove(pid)
        if remaining:
            time.sleep(0.02)
    assert not remaining, f"Surviving child PIDs: {remaining}"


def tree_script(path):
    grandchild = "import time; time.sleep(60)"
    child = (
        "import subprocess,sys,os,time,json; "
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
        f"open({str(path)!r},'w').write(json.dumps([os.getppid(),os.getpid(),p.pid])); "
        "print('ready',flush=True); time.sleep(60)"
    )
    return (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)"
    )


@pytest.mark.parametrize("cancellation", ["interrupt", "sigterm", "job"])
def test_cancel_kills_entire_tree(tmp_path, cancellation):
    path = tmp_path / "pids.json"
    errors = {"interrupt": KeyboardInterrupt, "sigterm": SystemExit, "job": asyncio.CancelledError}
    old_handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(errors[cancellation]):
        with subproc.process(
            [sys.executable, "-c", tree_script(path)],
            kind="siteone",
            stdout=subprocess.PIPE,
        ) as child:
            assert child.stdout is not None
            assert child.stdout.readline() == b"ready\n"
            assert os.getpgid(child.pid) == child.pid
            if cancellation == "sigterm":
                os.kill(os.getpid(), signal.SIGTERM)
            else:
                raise errors[cancellation]
    assert signal.getsignal(signal.SIGTERM) == old_handler
    assert_dead(json.loads(path.read_text()))


def test_timeout_kills_entire_tree(tmp_path):
    path = tmp_path / "pids.json"
    with pytest.raises(subprocess.TimeoutExpired):
        subproc.run(
            [sys.executable, "-c", tree_script(path)],
            kind="dns",
            timeout=1,
            capture_output=True,
        )
    assert_dead(json.loads(path.read_text()))


@pytest.mark.parametrize("kind", ["browser", "pdf"])
def test_playwright_driver_and_browser_environment_and_cleanup(kind, monkeypatch):
    seen = []
    original = asyncio.create_subprocess_exec
    monkeypatch.setenv("ANTHROPIC_API_KEY", "CANARY-AIza-000000000000000000000")

    async def record(*args, **kwargs):
        seen.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    with subproc.playwright(kind=kind, timeout=15) as playwright:
        driver = playwright._impl_obj._connection._transport._proc.pid
        browser = playwright.chromium.launch(env=subproc.environment(kind))
        page = browser.new_page()
        page.set_content("<p>Synthetic offline page</p>")
        assert page.inner_text("body") == "Synthetic offline page"
        browser.close()
    assert seen and seen[0]["start_new_session"]
    assert "ANTHROPIC_API_KEY" not in seen[0]["env"]
    assert seen[0]["umask"] == 0o077
    assert_dead([driver])
    # A second context catches leaked asyncio/greenlet state on teardown.
    with subproc.playwright(kind=kind, timeout=15):
        pass


def descendants(pid):
    listing = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid="],
        capture_output=True,
        text=True,
        env=subproc.environment("ssh"),
        start_new_session=True,
        timeout=2,
    )
    parents = {}
    for line in listing.stdout.splitlines():
        child, parent = map(int, line.split())
        parents.setdefault(parent, []).append(child)
    found = [pid]
    for parent in found:
        found.extend(parents.get(parent, []))
    return found


@pytest.mark.parametrize("cancel", ["interrupt", "deadline"])
def test_real_browser_tree_is_cancelled(cancel):
    from playwright.sync_api import Error

    pids = []
    error = KeyboardInterrupt if cancel == "interrupt" else (Error, subprocess.TimeoutExpired)
    with pytest.raises(error):
        with subproc.playwright(timeout=3) as playwright:
            driver = playwright._impl_obj._connection._transport._proc.pid
            browser = playwright.chromium.launch(env=subproc.environment("browser"))
            page = browser.new_page()
            page.set_content("<p>Local test</p>")
            pids = descendants(driver)
            assert len(pids) > 1
            if cancel == "interrupt":
                raise KeyboardInterrupt
            page.evaluate("() => new Promise(() => {})")
    assert_dead(pids)


def test_all_production_process_sites_use_boundary():
    """Fail when a new raw subprocess/shell call bypasses the reviewed process helper."""
    import ast
    from pathlib import Path

    root = Path(ai.__file__).parent
    for path in root.rglob("*.py"):
        if path.name == "subproc.py":
            continue
        tree = ast.parse(path.read_text())
        forbidden = {
            "subprocess.run",
            "subprocess.Popen",
            "os.system",
            "os.popen",
            "asyncio.create_subprocess_exec",
            "asyncio.create_subprocess_shell",
        }
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        assert not calls & forbidden, path


def test_cli_sigterm_cleans_processes_started_by_worker_threads(tmp_path, monkeypatch):
    import threading

    from ams_assess import cli

    path = tmp_path / "worker-pids.json"
    ready = threading.Event()
    failures = []

    def worker():
        try:
            with subproc.process(
                [sys.executable, "-c", tree_script(path)],
                kind="siteone",
                stdout=subprocess.PIPE,
            ) as child:
                assert child.stdout is not None and child.stdout.readline() == b"ready\n"
                ready.set()
                child.wait(timeout=10)
        except BaseException as exc:
            failures.append(type(exc).__name__)

    thread = threading.Thread(target=worker)

    def run(ctx, **kwargs):
        thread.start()
        assert ready.wait(5)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "data_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "run", run)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["run", "--customer", "test", "--target", "example.test", "--no-ai"])
        assert exc.value.code == 128 + signal.SIGTERM
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive() and not failures
    assert_dead(json.loads(path.read_text()))
