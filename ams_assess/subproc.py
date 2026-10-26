"""One environment and lifetime boundary for assessment child processes (POSIX).

Commands are argv lists, never shells. Every child owns a session/process group. Cleanup
also visits descendants in separate groups (Chromium creates these). No parent environment
is temporarily changed, including when Playwright starts its Node driver.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Generator, Mapping, Sequence
from typing import Any, Literal

Kind = Literal["claude-cli", "siteone", "browser", "pdf", "ssh", "dns"]
_COMMON = {"PATH", "LANG", "LC_ALL", "TMPDIR"}
_KEYS = {
    "claude-cli": _COMMON | {"HOME", "USER", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME"},
    "siteone": _COMMON,
    "browser": _COMMON | {"HOME", "PLAYWRIGHT_BROWSERS_PATH"},
    "pdf": _COMMON | {"HOME", "PLAYWRIGHT_BROWSERS_PATH"},
    "ssh": set(),
    "dns": _COMMON,
}


def environment(kind: Kind, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allow names, never remove a growing blacklist of provider credentials."""
    source = os.environ if source is None else source
    allowed = _KEYS[kind]  # Unknown process types fail closed.
    result = {key: source[key] for key in allowed if key in source}
    result.setdefault("PATH", "/usr/bin:/bin")
    result.setdefault("LANG", "C.UTF-8")
    if kind == "ssh":
        return {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    return result


def kill_tree(pid: int) -> None:
    """Kill descendants before the group leader, including detached browser groups.

    /bin/ps is a local process-table read, itself bounded and given the same minimal
    environment. Group kill remains the fallback if the process-table read fails.
    """
    descendants: list[int] = []
    live: set[int] | None = None
    groups: set[int] = set()
    try:
        listing = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat="],
            capture_output=True,
            text=True,
            env=environment("ssh"),
            start_new_session=True,
            timeout=2,
        )
        parents: dict[int, list[int]] = {}
        live = set()
        for line in listing.stdout.splitlines():
            pid_text, parent_text, group_text, state = line.split()
            child, parent = int(pid_text), int(parent_text)
            parents.setdefault(parent, []).append(child)
            if not state.startswith("Z"):
                live.add(child)
                groups.add(int(group_text))
        pending = [pid]
        while pending:
            children = parents.get(pending.pop(), [])
            descendants.extend(children)
            pending.extend(children)
    except (OSError, ValueError, subprocess.SubprocessError):
        live = None
    for child in reversed(descendants):
        if live is None or child in live:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
    # macOS returns EPERM for a group containing only unreaped zombies. Such children
    # cannot run/fork and are reaped by the owning Popen/asyncio transport below.
    if live is None or pid in groups:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


_ACTIVE: set[int] = set()
_LOCK = threading.RLock()


@contextlib.contextmanager
def cancellation() -> Generator[None]:
    """Restore the caller's handlers; SIGTERM is a cancellation, not leaked children."""
    previous = {}
    received: list[int] = []

    def cancel(signum, frame):
        received.append(signum)
        with _LOCK:
            for pid in tuple(_ACTIVE):
                kill_tree(pid)
        handler = previous[signum]
        if callable(handler):
            handler(signum, frame)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, cancel)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        # Cleanup after a cancellation can fail (a killed browser driver raises on close),
        # and callers may catch Exception. Neither may replace or swallow the cancellation.
        if received:
            if received[0] == signal.SIGINT:
                raise KeyboardInterrupt from None
            raise SystemExit(128 + received[0]) from None


@contextlib.contextmanager
def process(argv: Sequence[str], *, kind: Kind, **kwargs: Any) -> Generator[subprocess.Popen]:
    if any(key in kwargs for key in ("env", "shell", "start_new_session", "preexec_fn")):
        raise ValueError("Process environment and session are controlled by subproc")
    with cancellation():
        child = subprocess.Popen(
            argv,
            env=environment(kind),
            shell=False,
            start_new_session=True,
            umask=0o077,
            **kwargs,
        )
        with _LOCK:
            _ACTIVE.add(child.pid)
        try:
            yield child
        finally:
            kill_tree(child.pid)
            child.wait(timeout=5)
            with _LOCK:
                _ACTIVE.discard(child.pid)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe:
                    pipe.close()


def run(
    argv: Sequence[str],
    *,
    kind: Kind,
    timeout: float,
    input: str | bytes | None = None,
    capture_output: bool = False,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with process(argv, kind=kind, **kwargs) as child:
        stdout, stderr = child.communicate(input, timeout=timeout)
        result = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


# Playwright supplies no public option for the Node driver's environment/session. A
# thread-scoped spawn policy wraps only its startup, then immediately restores asyncio.
_START_LOCK = threading.RLock()


@contextlib.contextmanager
def playwright(*, kind: Kind = "browser", timeout: float = 300) -> Generator[Any]:
    from playwright.sync_api import sync_playwright

    pids: list[int] = []
    expired = threading.Event()
    manager = sync_playwright()
    entered = False
    owner = threading.get_ident()

    def expire():
        expired.set()
        for pid in pids:
            kill_tree(pid)

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    with cancellation():
        try:
            with _START_LOCK:
                original = asyncio.create_subprocess_exec

                async def spawn(
                    program: Any, *args: Any, **kwargs: Any
                ) -> asyncio.subprocess.Process:
                    if threading.get_ident() != owner or args[-1:] != ("run-driver",):
                        return await original(program, *args, **kwargs)
                    kwargs.update(env=environment(kind), start_new_session=True, umask=0o077)
                    child = await original(program, *args, **kwargs)
                    pids.append(child.pid)
                    with _LOCK:
                        _ACTIVE.add(child.pid)
                    return child

                # Dynamic SDK shim: preserve forwarding across asyncio signature versions.
                setattr(asyncio, "create_subprocess_exec", spawn)  # noqa: B010
                timer.start()
                try:
                    instance = manager.__enter__()
                    entered = True
                    if not pids:
                        raise RuntimeError("Playwright driver did not use the process boundary")
                finally:
                    asyncio.create_subprocess_exec = original
            yield instance
        finally:
            # Keep the deadline active during SDK shutdown too, and clean up even if
            # startup or the SDK's own shutdown raises after launching the driver.
            try:
                if entered:
                    try:
                        manager.__exit__()
                    except Exception:
                        if not expired.is_set():
                            raise
            finally:
                timer.cancel()
                for pid in pids:
                    kill_tree(pid)
                with _LOCK:
                    _ACTIVE.difference_update(pids)
            if expired.is_set():
                raise subprocess.TimeoutExpired("playwright", timeout) from None
