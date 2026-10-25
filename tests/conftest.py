"""Every test starts without operator credentials; network is fixture/loopback only."""

import ipaddress
import os
import socket
import tempfile
from pathlib import Path

import pytest

# Do this before collection imports application modules (some calculate default paths).
_ROOT = Path(__file__).resolve().parents[1]
(_ROOT / "tmp").mkdir(exist_ok=True)
_HOME = tempfile.TemporaryDirectory(prefix="test-home-", dir=_ROOT / "tmp")
_SAFE = {key: os.environ[key] for key in ("PATH", "LANG", "SYSTEMROOT") if key in os.environ}
os.environ.clear()
os.environ.update(_SAFE)
os.environ.update(
    HOME=_HOME.name,
    XDG_CONFIG_HOME=str(Path(_HOME.name) / "config"),
    AMS_CONFIG_DIR=str(Path(_HOME.name) / "config/ams-assess"),
    AMS_ENV_FILE=str(Path(_HOME.name) / "absent.env"),
    AMS_AI_PROVIDER="none",
    AMS_LIVE_SMOKE="0",
    PLAYWRIGHT_BROWSERS_PATH=str(_ROOT / "tools/playwright"),
)


_CUSTOMERS = _ROOT / "data/customers"


def _customer_tree(root=_CUSTOMERS):
    """Names and stat metadata only: never read operator customer data."""
    if not root.exists():
        return None
    return {
        (str(Path(directory) / name), os.lstat(Path(directory) / name).st_mtime_ns)
        for directory, dirs, files in os.walk(root)
        for name in dirs + files
    }


def _loopback(host):
    if host in {"localhost", b"localhost", None, "", b""}:
        return
    try:
        address = ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
    except ValueError:
        raise AssertionError("Tests must mock external DNS/network access") from None
    if not address.is_loopback:
        raise AssertionError("Tests must mock external network access")


@pytest.fixture(autouse=True)
def isolated_customer_root(tmp_path, monkeypatch):
    """Customer stores go to tmp; fail any test that writes under the repo's data/customers."""
    from ams_assess import cli

    before = _customer_tree()
    # Tests that relocate cli.ROOT keep their own <ROOT>/data/customers.
    monkeypatch.setattr(
        cli,
        "data_root",
        lambda: (
            (tmp_path / "data/customers")
            if cli.ROOT.resolve() == _ROOT
            else cli.ROOT / "data/customers"
        ),
    )
    yield
    assert _customer_tree() == before, f"Test wrote under {_CUSTOMERS}"


@pytest.fixture(autouse=True)
def isolated_operator_and_network(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("AMS_CONFIG_DIR", str(home / "config/ams-assess"))
    monkeypatch.setenv("AMS_ENV_FILE", str(home / "absent.env"))
    original_lookup = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_sendto = socket.socket.sendto

    def lookup(host, *args, **kwargs):
        _loopback(host)
        return original_lookup(host, *args, **kwargs)

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            _loopback(address[0])
        return original_connect(sock, address)

    def sendto(sock, data, *args):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            _loopback(args[-1][0])
        return original_sendto(sock, data, *args)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
