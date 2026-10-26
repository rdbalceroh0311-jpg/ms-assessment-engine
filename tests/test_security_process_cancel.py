"""A real SIGTERM during each production child-process path leaves no surviving process.

SiteOne, Chromium (browser and PDF rendering) and OpenSSH are the real installed binaries,
started through their production call sites. No pandoc process exists: PDFs are rendered by
Chromium. Only loopback fixtures are contacted.
"""

import os
import signal
import socket
import threading
import time
from pathlib import Path

import pytest
from test_security_processes import assert_dead, descendants

from ams_assess import network, report, runner, subproc
from ams_assess.connectors import wp_cli
from ams_assess.models import Context, create_run_dir
from ams_assess.modules import browser, siteone

ROOT = Path(__file__).resolve().parents[1]


def sigterm_once(ready, minimum=1):
    """SIGTERM this process once a tracked child tree is running and `ready()` holds."""
    seen: list[int] = []

    def watch():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with subproc._LOCK:
                active = list(subproc._ACTIVE)
            tree = [pid for root in active for pid in descendants(root)]
            if active and ready() and len(tree) >= minimum:
                seen.extend(tree)
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return seen, thread


def cancelled(call, ready, minimum=1):
    seen, watcher = sigterm_once(ready, minimum)
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exit:
        call()
    watcher.join(5)
    assert exit.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == previous
    assert seen, "Child process tree was never observed"
    assert_dead(seen)
    with subproc._LOCK:
        assert not subproc._ACTIVE
    return seen


def test_sigterm_kills_real_siteone_mid_crawl(tmp_path, http_server, monkeypatch):
    if not (ROOT / "tools/siteone/siteone-crawler/siteone-crawler").is_file():
        pytest.skip("Run scripts/setup.sh for SiteOne")
    release = threading.Event()

    def hang(request):
        release.wait(30)
        return 200, {"Content-Type": "text/html"}, b"<html><title>late</title></html>"

    site = http_server(hang)
    monkeypatch.setattr(network, "_fixture_origins", frozenset({site.origin}))
    monkeypatch.setattr(siteone, "resolve_target", lambda target: {"resolved": target})
    monkeypatch.setattr(siteone, "crawl_seeds", lambda ctx, result: [])
    ctx = Context(
        "synthetic",
        "Synthetic",
        site.origin + "/",
        {"seo"},
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        ROOT / "tools",
        max_pages=3,
    )
    try:
        cancelled(lambda: siteone.run(ctx), lambda: bool(site.received))
    finally:
        release.set()


@pytest.mark.parametrize("stage", ["browser", "pdf"])
def test_sigterm_kills_real_chromium_tree_and_stops_the_run(tmp_path, monkeypatch, stage):
    """Browser collection and PDF rendering through runner.run, held once Chromium runs.

    Closing the killed browser raises; neither that nor the runner's per-module and
    report-failure handling may turn the cancellation into a continuing run.
    """
    if not (ROOT / "tools/axe.min.js").is_file():
        pytest.skip("Run scripts/setup.sh for axe-core")
    hold = threading.Event()
    owner = browser if stage == "browser" else report
    original = owner.isolate_context

    def isolate(chromium, **kwargs):
        session = original(chromium, **kwargs)
        hold.set()
        time.sleep(30)  # the signal interrupts this with the browser tree running
        return session

    monkeypatch.setattr(owner, "isolate_context", isolate)
    monkeypatch.setattr(runner, "REGISTRY", {"browser": browser} if stage == "browser" else {})
    monkeypatch.setattr(
        runner.coverage, "collect", lambda ctx: {"status": "unavailable", "reason": "Synthetic"}
    )
    ctx = Context(
        "synthetic",
        "Synthetic",
        "http://127.0.0.1:9/",
        set(browser.categories),
        set(),
        {},
        create_run_dir(tmp_path, "synthetic"),
        ROOT / "tools",
        pages=["http://127.0.0.1:9/"],
    )
    # Driver, Chromium and its helper processes (in separate process groups).
    pids = cancelled(lambda: runner.run(ctx, no_ai=True, formats={"pdf"}), hold.is_set, 3)
    assert len(pids) >= 3
    assert not (ctx.run_dir / "results.json").exists() or stage == "pdf"
    if stage == "pdf":
        assert not (ctx.run_dir / "report.pdf").exists()


def test_sigterm_kills_real_ssh_child(tmp_path):
    if not Path("/usr/bin/ssh").is_file():
        pytest.skip("OpenSSH client unavailable")
    # A loopback peer that accepts but never sends an SSH banner keeps ssh waiting.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    accepted = []

    def accept():
        try:
            connection, _ = listener.accept()
            accepted.append(connection)
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    for name in ("synthetic-key", "synthetic-known-hosts"):
        (tmp_path / name).write_text("")
        (tmp_path / name).chmod(0o600)
    config = wp_cli.SSHConfig(
        host="127.0.0.1",
        port=listener.getsockname()[1],
        user="synthetic",
        key_path=str(tmp_path / "synthetic-key"),
        known_hosts=str(tmp_path / "synthetic-known-hosts"),
        path="/var/www/synthetic",
    )
    argv = wp_cli.ssh_argv(config, "version", None, 60)
    try:
        cancelled(lambda: wp_cli.run_bounded(argv, 60), lambda: bool(accepted))
    finally:
        for connection in accepted:
            connection.close()
        listener.close()
