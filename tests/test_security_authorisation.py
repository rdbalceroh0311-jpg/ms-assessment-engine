"""Real-customer runs and connected reads require a recorded authorisation and confirmation."""

import datetime as dt
import json
import os
import socket
import sys
import threading

import httpx
import pytest

from ams_assess import authorisation, cli, network, store
from ams_assess.connectors import service
from ams_assess.ui import LocalServer

CUSTOMER = "harbour-demo"  # not a test-exempt synthetic customer
TODAY = dt.date.today().isoformat()


@pytest.fixture
def no_side_effects(monkeypatch):
    """Any credential read, run directory, network or connector use fails the test."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Used before authorisation was checked")

    monkeypatch.setattr(cli, "config", forbidden)
    monkeypatch.setattr(cli, "create_run_dir", forbidden)
    monkeypatch.setattr(cli, "run", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(service, "prepare", forbidden)


def run(*extra):
    return cli.main(["run", "--customer", CUSTOMER, "--target", "harbour.example.com", *extra])


def authorise(*scope, who="Pat Owner (Director)", date=TODAY):
    args = ["authorise", "--customer", CUSTOMER, "--authorised-by", who, "--date", date]
    return cli.main([*args, *(a for s in scope for a in ("--scope", s))])


def refused(capsys, call, message):
    with pytest.raises(SystemExit) as exit:
        call()
    assert exit.value.code == 2
    assert message in capsys.readouterr().err


def test_run_without_recorded_authorisation_fails_closed(capsys, no_side_effects):
    refused(capsys, lambda: run(authorisation.CONFIRM_FLAG), "No recorded authorisation")
    assert not (cli.data_root() / CUSTOMER).exists()


def test_run_requires_scope_and_explicit_confirmation(capsys, monkeypatch, no_side_effects):
    assert authorise("https://harbour.example.com/", "connectors") == 0
    refused(capsys, run, authorisation.CONFIRM_FLAG)
    refused(
        capsys,
        lambda: cli.main(
            [
                "run",
                "--customer",
                CUSTOMER,
                "--target",
                "other.example.org",
                authorisation.CONFIRM_FLAG,
            ]
        ),
        "does not cover this target",
    )
    # Recorded, in scope (subdomains included) and confirmed: the run may proceed.
    seen = []
    monkeypatch.setattr(cli, "config", dict)
    monkeypatch.setattr(cli, "create_run_dir", lambda root, customer: root / customer / "run")
    monkeypatch.setattr(
        cli, "run", lambda ctx, **kw: seen.append(ctx) or {"status": "complete", "findings": []}
    )
    assert (
        cli.main(
            [
                "run",
                "--customer",
                CUSTOMER,
                "--target",
                "www.harbour.example.com",
                authorisation.CONFIRM_FLAG,
            ]
        )
        == 0
    )
    assert seen[0].target == "https://www.harbour.example.com/"


def test_connected_reads_require_connectors_scope_and_confirmation(
    capsys, monkeypatch, no_side_effects
):
    collect = ["connect", "ga4", "collect", "--customer", CUSTOMER]
    refused(capsys, lambda: cli.main([*collect, authorisation.CONFIRM_FLAG]), "No recorded")
    # The service entry used by the local UI refuses too, before any credential use.
    with pytest.raises(authorisation.Unauthorised):
        service.execute(cli.data_root(), CUSTOMER, "ga4", collect=True)
    assert authorise("harbour.example.com") == 0
    refused(
        capsys,
        lambda: cli.main([*collect, authorisation.CONFIRM_FLAG]),
        "does not cover connected reads",
    )
    assert authorise("harbour.example.com", "connectors") == 0
    refused(capsys, lambda: cli.main(collect), authorisation.CONFIRM_FLAG)
    refused(
        capsys,
        lambda: cli.main(["connect", "ga4", "test", "--customer", CUSTOMER]),
        authorisation.CONFIRM_FLAG,
    )
    calls = []
    monkeypatch.setattr(
        service, "execute", lambda *a, **k: calls.append(k) or {"status": "connected"}
    )
    assert cli.main([*collect, authorisation.CONFIRM_FLAG]) == 0
    assert calls and calls[0]["collect"] is True


@pytest.mark.parametrize(
    "args, message",
    [
        (["--authorised-by", "Pat", "--date", "2999-01-01", "--scope", "a.example"], "future"),
        (["--authorised-by", "Pat", "--date", "25/10/2026", "--scope", "a.example"], "YYYY-MM-DD"),
        (["--authorised-by", "Pat", "--date", TODAY, "--scope", "10.0.0.1"], "IP addresses"),
        (["--authorised-by", "Pat", "--date", TODAY, "--scope", "intranet"], "website hosts"),
        (["--authorised-by", " ", "--date", TODAY, "--scope", "a.example"], "authorised_by"),
        (["--authorised-by", "Pat", "--date", TODAY], "authorisation.scope"),
        (["--revoke", "--authorised-by", "Pat"], "--revoke alone"),
    ],
)
def test_invalid_authorisation_is_rejected(capsys, args, message):
    refused(capsys, lambda: cli.main(["authorise", "--customer", CUSTOMER, *args]), message)


def test_existing_records_load_unchanged_and_authorisation_keeps_history(monkeypatch):
    root = cli.data_root()
    store.set_profile(root, CUSTOMER, {"name": "Harbour Demo Plumbing", "no_ai": True})
    path = store.store_file(root, CUSTOMER)
    legacy = json.loads(path.read_text())
    # A record written before this field existed has no authorisation key at all.
    legacy["profile"]["fields"].pop("authorisation")
    for evidence in legacy["evidence"]:
        evidence["data"].pop("authorisation", None)
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    loaded = store.load(root, CUSTOMER)
    assert loaded is not None and loaded["profile"] == legacy["profile"]
    assert path.read_bytes() == before
    assert not list(path.parent.glob("assessment.json.backup-*"))
    with pytest.raises(authorisation.Unauthorised):
        authorisation.require(loaded, CUSTOMER, "https://harbour.example.com/", confirmed=True)

    assert authorise("harbour.example.com", who="Pat Owner", date="2026-10-01") == 0
    assert authorise("harbour.example.com", "connectors", who="Sam Owner") == 0
    current = store.load(root, CUSTOMER)
    assert current is not None
    fields = current["profile"]["fields"]
    assert fields["name"] == "Harbour Demo Plumbing" and fields["no_ai"] is True
    assert fields["authorisation"] == {
        "authorised_by": "Sam Owner",
        "date": TODAY,
        "scope": ["harbour.example.com", "connectors"],
    }
    profiles = [e for e in current["evidence"] if e["source"] == "profile"]
    assert [
        e["data"].get("authorisation", {}) and e["data"]["authorisation"]["authorised_by"]
        for e in profiles
    ][-2:] == ["Pat Owner", "Sam Owner"]
    assert profiles[-1]["data"]["supersedes"] == profiles[-2]["id"]
    authorisation.require(current, CUSTOMER, "https://harbour.example.com/", confirmed=True)

    assert cli.main(["authorise", "--customer", CUSTOMER, "--revoke"]) == 0
    with pytest.raises(authorisation.Unauthorised):
        authorisation.require(
            store.load(root, CUSTOMER), CUSTOMER, "https://harbour.example.com/", confirmed=True
        )


def test_fixture_exemption_is_exact_test_only(monkeypatch):
    origin = "http://127.0.0.1:8123"
    monkeypatch.setenv("AMS_ALLOW_UNAUTHORISED", "1")
    monkeypatch.setattr(authorisation, "_fixture_customers", frozenset())
    with pytest.raises(authorisation.Unauthorised):
        authorisation.require(None, "synthetic", origin + "/", confirmed=False)
    monkeypatch.setattr(network, "_fixture_origins", frozenset({origin}))
    authorisation.require(None, CUSTOMER, origin + "/", confirmed=False)
    with pytest.raises(authorisation.Unauthorised):
        authorisation.require(None, CUSTOMER, "http://127.0.0.1:8124/", confirmed=True)
    with pytest.raises(authorisation.Unauthorised):
        authorisation.require(None, CUSTOMER, None, confirmed=True)


_OPENED: list[str] = []
_WATCH: list[str] = []


def _audit(event, args):
    if event == "open" and _WATCH and isinstance(args[0], (str, os.PathLike)):
        if (path := os.fspath(args[0])) in _WATCH:
            _OPENED.append(path)


sys.addaudithook(_audit)  # Permanent per process; inert unless a test sets _WATCH.


@pytest.fixture
def credential_canary(tmp_path, monkeypatch):
    """Canary credentials.env and AMS_ENV_FILE; yields the paths opened while watched."""
    monkeypatch.setenv("AMS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AMS_ENV_FILE", str(tmp_path / "canary.env"))
    credentials = tmp_path / "config/credentials.env"
    credentials.parent.mkdir()
    credentials.write_text("PAGESPEED_API_KEY=canary-secret\nAMS_BACKUP_RETENTION_DAYS=30\n")
    (tmp_path / "canary.env").write_text("DATAFORSEO_PASSWORD=canary-env\n")
    _OPENED.clear()
    _WATCH[:] = [str(credentials), str(tmp_path / "canary.env")]
    yield _OPENED
    _WATCH.clear()


def test_refusals_never_open_the_credential_file(capsys, credential_canary, no_side_effects):
    # A stored customer without an authorisation record: the store is read, credentials are not.
    assert cli.main(["customer", "--customer", CUSTOMER, "--name", "Harbour Demo"]) == 0
    collect = ["connect", "ga4", "collect", "--customer", CUSTOMER]
    credential_canary.clear()
    refused(capsys, lambda: run(authorisation.CONFIRM_FLAG), "No recorded authorisation")
    refused(capsys, lambda: cli.main([*collect, authorisation.CONFIRM_FLAG]), "No recorded")
    # Authorised, but not confirmed.
    assert authorise("harbour.example.com", "connectors") == 0
    credential_canary.clear()
    refused(capsys, run, authorisation.CONFIRM_FLAG)
    refused(capsys, lambda: cli.main(collect), authorisation.CONFIRM_FLAG)
    refused(
        capsys,
        lambda: cli.main(["connect", "ga4", "test", "--customer", CUSTOMER]),
        authorisation.CONFIRM_FLAG,
    )
    assert credential_canary == []


def test_confirmed_run_still_loads_credentials(credential_canary, monkeypatch):
    assert authorise("harbour.example.com") == 0
    seen = []
    monkeypatch.setattr(cli, "create_run_dir", lambda root, customer: root / customer / "run")
    monkeypatch.setattr(
        cli, "run", lambda ctx, **kw: seen.append(ctx) or {"status": "complete", "findings": []}
    )
    credential_canary.clear()
    assert run(authorisation.CONFIRM_FLAG) == 0
    assert credential_canary and seen[0].config["PAGESPEED_API_KEY"] == "canary-secret"


def test_ui_connector_refusals_never_open_settings_files(tmp_path, credential_canary, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Connector used before authorisation was checked")

    monkeypatch.setattr(service, "prepare", forbidden)
    root = tmp_path / "ui-root"
    store.set_profile(
        root, CUSTOMER, {"name": "Harbour Demo", "websites": ["https://harbour.example.com"]}
    )
    credential_canary.clear()
    with LocalServer(root, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(
                base_url=server.origin,
                trust_env=False,
                headers={"Authorization": "Bearer " + server.token, "Origin": server.origin},
            ) as client:
                for action in ("test", "collect"):
                    response = client.post(
                        server.url("/connect"),
                        data={
                            "csrf": server.csrf,
                            "customer": CUSTOMER,
                            "connector": "ga4",
                            "action": action,
                        },
                    )
                    assert response.status_code == 400, action
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert credential_canary == []
