"""Synthetic SSH responses only. No customer credentials, hosts or live SSH calls."""

import json
import logging
import shlex
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from ams_assess import ai, cli, coverage, store
from ams_assess.connectors import auth, service, wp_cli
from ams_assess.connectors.core import ConnectorError
from ams_assess.connectors.http import ReadClient
from ams_assess.models import Context, Evidence, ModuleResult, create_run_dir
from ams_assess.report import render_customer
from ams_assess.ui import LocalServer

SITE = "https://example.test/"
INSTALL = "/srv/tenant-private/wordpress"
PRIVATE = "synthetic-private-key-never-output"


class SSH:
    def __init__(self):
        self.calls = []
        self.override = lambda operation, page: None
        self.options = {
            "blog_public": "0",
            "siteurl": SITE,
            "home": SITE,
            "permalink_structure": "/%postname%/",
            "blogdescription": "Private administrator person@example.test",
            "default_comment_status": "closed",
            "users_can_register": "1",
            "default_role": "subscriber",
            "WPLANG": "en_AU",
            "timezone_string": "Australia/Sydney",
        }
        self.plugins = [
            {
                "name": "sample-plugin",
                "status": "inactive",
                "version": "1.0",
                "update": "available",
                "update_version": "1.1",
                "auto_update": "off",
            }
        ]
        self.posts = {"post": [], "page": [self.post(1, "page")]}

    @staticmethod
    def post(identifier, kind="post"):
        return {
            "ID": identifier,
            "post_type": kind,
            "post_name": f"page-{identifier}",
            "post_modified": "2026-01-01 00:00:00",
            "url": SITE + f"{kind}/{identifier}",
        }

    def __call__(self, argv, seconds):
        assert argv[0] == "/usr/bin/ssh"
        assert 0 < seconds <= 10
        words = shlex.split(argv[-1])
        command = words[words.index("wp") + 1 :]
        args = [
            a
            for a in command
            if not a.startswith(
                (
                    "--path=",
                    "--skip-plugins",
                    "--skip-themes",
                    "--skip-packages",
                    "--context=",
                    "--no-color",
                )
            )
        ]
        page = next((int(a.split("=")[1]) for a in args if a.startswith("--paged=")), None)
        if args[:2] == ["post", "list"]:
            kind = next(a.split("=")[1] for a in args if a.startswith("--post_type="))
            operation = "posts_" + kind
        else:
            operation = next(k for k, v in wp_cli.COMMANDS.items() if tuple(args) == v)
        self.calls.append((operation, page, argv))
        response = self.override(operation, page)
        if response is not None:
            return response
        if operation == "installation":
            return 0, "", ""
        if operation == "version":
            return (
                0,
                "WordPress version: 6.8.3\nDatabase revision: 60421\nPackage language: en_US\n",
                "",
            )
        if operation == "core_checksums":
            return 0, "Success: WordPress installation verifies against checksums.\n", ""
        if operation == "plugin_checksums":
            return 0, "Success: Verified 1 of 1 plugins.\n", ""
        if operation == "runtime":
            body = {
                "php_version": "8.3.20",
                "wp_cli_version": "2.12.0",
                "php_binary_path": "/usr/local/private/php",
                "php_ini_used": INSTALL + "/php.ini",
                "secret": PRIVATE,
            }
        elif operation == "plugins":
            body = self.plugins
        elif operation == "themes":
            body = []
        elif operation.startswith("posts_"):
            assert page is not None
            start = (page - 1) * wp_cli.PAGE_SIZE
            body = self.posts[operation[6:]][start : start + wp_cli.PAGE_SIZE]
        else:
            body = self.options[operation[7:]]
        return 0, json.dumps(body), ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    private, root = tmp_path / "private", tmp_path / "customers"
    private.mkdir(mode=0o700)
    key, known = private / "id_ed25519", private / "known_hosts"
    key.write_text(PRIVATE)
    known.write_text("ssh.example.test ssh-ed25519 synthetic-pinned-key\n")
    data = {
        "host": "ssh.example.test",
        "port": 22,
        "user": "assessor",
        "key_path": str(key),
        "known_hosts": str(known),
        "install_path": INSTALL,
    }
    config = private / "ssh.json"
    auth.atomic_private(config, data)
    auth.register_reference("synthetic", wp_cli.CONNECTOR, file=config, directory=private)
    monkeypatch.setattr(service, "config_dir", lambda: private)
    monkeypatch.setattr(auth, "config_dir", lambda: private)
    monkeypatch.setattr(cli, "data_root", lambda: root)
    ssh = SSH()
    monkeypatch.setattr(wp_cli, "run_bounded", ssh)
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", lambda _: None)
    material = wp_cli.authenticate(wp_cli.CONNECTOR, "synthetic", directory=private)
    resource = material.private_data.resource
    store.set_profile(root, "synthetic", {"name": "Synthetic", "websites": [SITE]})
    service.set_binding(root, "synthetic", "wp_cli", "synthetic", resource)
    return SimpleNamespace(
        root=root,
        private=private,
        config=config,
        data=data,
        material=material,
        resource=resource,
        ssh=ssh,
        monkeypatch=monkeypatch,
    )


def prepare(env, **kwargs):
    return service.prepare(snapshot(env), "wp_cli", collect=True, **kwargs)


def snapshot(env, customer="synthetic"):
    current = store.load(env.root, customer)
    assert current is not None
    return current


def endpoints(result):
    return {e.data["operation"]: e.data for e in result.evidence}


def test_success_reads_only_explicit_installation_and_fixed_commands(env):
    listed = service.list_resources("wp_cli", "synthetic")
    assert listed.data["rows"][0]["resource_id"] == env.resource
    assert env.ssh.calls[0][0] == "installation"
    result, state = prepare(env)
    assert state["status"] == "connected"
    data = endpoints(result)
    assert data["runtime"]["data"] == {
        "php_version": "8.3.20",
        "wp_cli_version": "2.12.0",
        "snapshot": True,
    }
    assert data["posts_post"]["data"]["count"] == 0
    assert data["posts_page"]["data"]["urls"] == [SITE + "page/1"]
    assert data["posts_page"]["data"]["post_status"] == "publish"
    assert all(
        e["connector"] == "wp_cli"
        and e["resource_id"] == env.resource
        and e["collected_at"]
        and e["period"]
        and "coverage" in e
        for e in data.values()
    )
    assert any("not a confirmed vulnerability" in f.title for f in result.findings)
    assert any(
        f.check_key == "wp_cli:registration" and "subscriber" in f.detail for f in result.findings
    )
    operations = [operation for operation, _, _ in env.ssh.calls]
    assert set(operations) == set(wp_cli.OPERATIONS)
    assert all("--path=" + INSTALL in argv[-1] for _, _, argv in env.ssh.calls)
    assert "private_data" not in repr(env.material) and INSTALL not in repr(env.material)


def test_ssh_host_key_and_forwarding_controls_are_mandatory(env):
    prepare(env)
    for _, _, argv in env.ssh.calls:
        assert argv[:4] == ["/usr/bin/ssh", "-F", "/dev/null", "-T"]
        for required in (
            "StrictHostKeyChecking=yes",
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "IdentityAgent=none",
            "ForwardAgent=no",
            "ForwardX11=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "ConnectTimeout=5",
            "GlobalKnownHostsFile=/dev/null",
            "UpdateHostKeys=no",
            "VerifyHostKeyDNS=no",
            "ControlPath=none",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
        ):
            assert required in argv
        assert "UserKnownHostsFile=" + env.data["known_hosts"] in argv
        assert "-i" in argv and argv[argv.index("-i") + 1] == env.data["key_path"]
        assert argv[argv.index("--") + 1] == "ssh.example.test"
        for required in (
            "--skip-plugins",
            "--skip-themes",
            "--skip-packages",
            "--context=cli",
            "DISABLE_WP_CRON=1",
            "WP_CLI_DISABLE_AUTO_CHECK_UPDATE=1",
            "timeout",
            "WP_CLI_CONFIG_PATH=/dev/null",
            "WP_CLI_CACHE_DIR=/dev/null",
        ):
            assert required in shlex.split(argv[-1])
        assert "accept-new" not in argv[-1]


@pytest.mark.parametrize(
    ("code", "diagnostic", "status", "hint"),
    [
        (255, "Permission denied (publickey)", "permission_required", "public-key"),
        (255, "Host key verification failed", "permission_required", "host key"),
        (255, "REMOTE HOST IDENTIFICATION HAS CHANGED", "permission_required", "host key"),
        (255, "Certificate invalid: expired", "credential_expired", "expired"),
        (255, "Authentication key revoked", "credential_expired", "revoked"),
        (1, "Permission denied /srv/private/wp-config.php", "permission_required", "read access"),
        (1, "This does not seem to be a WordPress installation", "not_found", "bound path"),
        (1, "", "not_found", "tables"),
        (127, "wp: command not found", "not_found", "fixed PATH"),
    ],
)
def test_auth_host_key_permissions_and_missing_installation(env, code, diagnostic, status, hint):
    env.ssh.override = lambda *_: (code, "", diagnostic)
    result, state = prepare(env)
    assert state["status"] == status and hint in state["detail"]
    assert len(env.ssh.calls) == 1
    assert not result.findings
    assert "/srv/private" not in json.dumps(state)


@pytest.mark.parametrize("failure", ["network", "timeout"])
def test_transient_retry_is_bounded_and_never_marks_connection_success(env, failure):
    env.ssh.override = lambda *_: (
        (255, "", "Connection timed out") if failure == "network" else (124, "", "")
    )
    result, state = prepare(env)
    assert state["status"] == "provider_unavailable"
    assert len(env.ssh.calls) == 3 and not result.findings


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "relative",
        "~/public_html",
        "/srv/../secret",
        "/srv/./wp",
        "/srv//wp",
        "/srv/wp/",
        "/srv/wp;id",
        "/srv/$(id)",
        "/srv/`id`",
        "/srv/wp\nwhoami",
        "/srv/wp name",
        "/srv/wp'",
        "/srv/*",
        "/srv/wp%20",
    ],
)
def test_install_path_validation_rejects_shell_and_aliases(path):
    with pytest.raises(ValueError):
        wp_cli.install_path(path)
    with pytest.raises(ValueError):
        wp_cli.command_argv("runtime", path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "-oProxyCommand=id"),
        ("host", "host;id"),
        ("host", "host..test"),
        ("host", "fe80::1%$(id)"),
        ("host", "fe80::1%eth0"),
        ("user", "assessor;id"),
        ("user", "-root"),
        ("port", "22;id"),
        ("port", True),
        ("port", 0),
        ("install_path", "/srv/wp;id"),
        ("key_path", "/tmp/id_ed25519"),
        ("known_hosts", "/dev/null"),
        ("command", "wp eval evil"),
    ],
)
def test_invalid_auth_configuration_is_private_and_never_executes(env, field, value):
    auth.atomic_private(env.config, {**env.data, field: value})
    _, state = prepare(env)
    assert state["status"] == "credential_missing" and not env.ssh.calls
    assert "private-json" in state["detail"]


def test_missing_key_and_symlink_known_hosts_fail_closed(env):
    key = env.private / "id_ed25519"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    key.unlink()
    assert prepare(env)[1]["status"] == "credential_missing"
    key.write_text(PRIVATE)
    known = env.private / "known_hosts"
    known.unlink()
    known.symlink_to(key)
    assert prepare(env)[1]["status"] == "credential_missing"
    assert not env.ssh.calls


@pytest.mark.parametrize(
    "command",
    [
        "eval",
        "config get",
        "config list",
        "db query",
        "shell",
        "plugin update",
        "plugin install",
        "plugin activate",
        "core update",
        "option list",
        "option_admin_email",
        "option_active_plugins",
        "posts_private",
        "runtime;id",
        "runtime --exec=evil",
    ],
)
def test_read_allowlist_refuses_dangerous_commands(env, command):
    with pytest.raises(ConnectorError):
        wp_cli.command_argv(command, INSTALL)
    client = ReadClient(wp_cli.CONNECTOR, env.material)
    try:
        with pytest.raises(ConnectorError):
            client.request("GET", wp_cli.BASE + env.resource + "/" + command)
    finally:
        client.close()
    assert not env.ssh.calls


@pytest.mark.parametrize(
    ("method", "operation", "kwargs"),
    [
        ("POST", "runtime", {}),
        ("DELETE", "runtime", {}),
        ("GET", "runtime", {"params": {"exec": "evil"}}),
        ("GET", "runtime", {"json": {"command": "id"}}),
        ("GET", "posts_post", {"params": {"page": "1;id"}}),
        ("GET", "posts_post", {"params": {"page": "6"}}),
        ("GET", "posts_post", {"params": {"page": "1", "post_status": "private"}}),
        ("GET", "posts_post", {"params": [("page", "1"), ("page", "2")]}),
    ],
)
def test_transport_refuses_extra_or_malformed_arguments(env, method, operation, kwargs):
    client = ReadClient(wp_cli.CONNECTOR, env.material)
    try:
        with pytest.raises(ConnectorError):
            client.request(method, wp_cli.BASE + env.resource + "/" + operation, **kwargs)
    finally:
        client.close()
    assert not env.ssh.calls


def test_empty_json_data_differs_from_empty_invalid_output(env):
    env.ssh.plugins = []
    env.ssh.posts["page"] = []
    result, state = prepare(env)
    assert (
        state["status"] == "connected"
        and endpoints(result)["plugins"]["coverage"]["rows_returned"] == 0
    )
    env.ssh.calls.clear()
    env.ssh.override = lambda op, _: (0, "", "") if op == "plugins" else None
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    assert endpoints(result)["plugins"]["status"] == "failed"
    assert "no conclusion" in endpoints(result)["plugins"]["detail"]
    assert endpoints(result)["runtime"]["status"] == "connected"
    assert not any(f.check_key == "wp_cli:updates" for f in result.findings)


def test_pagination_and_partial_failure_keep_successful_evidence(env):
    env.ssh.posts["post"] = [SSH.post(i) for i in range(1, 102)]
    result, _ = prepare(env)
    inventory = endpoints(result)["posts_post"]
    assert inventory["data"]["count"] == 101 and not inventory["data"]["count_is_lower_bound"]
    env.ssh.override = lambda op, page: (
        (1, "", "Permission denied") if (op, page) == ("posts_post", 2) else None
    )
    result, state = prepare(env)
    inventory = endpoints(result)["posts_post"]
    assert state["status"] == "connected_partial"
    assert inventory["status"] == "permission_required" and inventory["coverage"]["truncated"]
    assert inventory["data"]["count"] == 100 and len(inventory["data"]["urls"]) == 100
    assert inventory["data"]["count_is_lower_bound"]
    assert endpoints(result)["posts_page"]["data"]["count"] == 1


def test_page_cap_duplicate_pages_and_invalid_urls_are_partial(env):
    env.ssh.posts["post"] = [SSH.post(i) for i in range(1, 601)]
    result, state = prepare(env)
    inventory = endpoints(result)["posts_post"]
    assert inventory["coverage"]["truncated"] and inventory["data"]["count"] == 500
    assert state["status"] == "connected_partial"
    assert max(page for op, page, _ in env.ssh.calls if op == "posts_post") == 5
    env.ssh.posts["post"] = [SSH.post(i) for i in range(1, 101)] + [SSH.post(1)]
    result, _ = prepare(env)
    assert "changed during pagination" in endpoints(result)["posts_post"]["detail"]
    env.ssh.posts["page"] = [{**SSH.post(1, "page"), "url": "file:///srv/private"}]
    result, _ = prepare(env)
    assert endpoints(result)["posts_page"]["status"] == "connected_partial"
    assert endpoints(result)["posts_page"]["data"]["urls"] == []


def test_checksum_discrepancy_is_not_confused_with_premium_unverifiable(env):
    def responses(operation, _):
        if operation == "core_checksums":
            return (
                1,
                "",
                "Warning: File doesn't verify against checksum: /private/wp-includes/version.php\n"
                "Error: WordPress installation doesn't verify against checksums.\n",
            )
        if operation == "plugin_checksums":
            return (
                0,
                "Success: Verified 0 of 1 plugins (1 skipped).\n",
                "Warning: Could not retrieve the checksums for version 1.0 "
                "of plugin premium, skipping.\n",
            )

    env.ssh.override = responses
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    assert endpoints(result)["core_checksums"]["status"] == "connected"
    assert endpoints(result)["plugin_checksums"]["data"]["unverifiable"]
    assert any(f.check_key == "wp_cli:core_checksums" for f in result.findings)
    assert not any(o.check_id == "wp_cli:plugin_checksums" for o in result.outcomes)
    assert "/private/" not in result.model_dump_json()


def test_plugin_checksum_mismatch_preserved_alongside_skips(env):
    data = [
        {
            "plugin_name": "sample",
            "file": "/srv/private/file.php",
            "message": "Checksum does not match",
        }
    ]
    env.ssh.override = lambda op, _: (
        (
            1,
            json.dumps(data),
            "Warning: premium skipped\nError: Only verified 0 of 2 plugins (1 skipped).",
        )
        if op == "plugin_checksums"
        else None
    )
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    assert any(f.check_key == "wp_cli:plugin_checksums" for f in result.findings)
    assert "/srv/private" not in result.model_dump_json()


def test_unknown_checksum_output_never_creates_integrity_pass(env):
    env.ssh.override = lambda op, _: (
        (1, "", "Error: couldn't get checksums") if "checksums" in op else None
    )
    result, state = prepare(env)
    assert state["status"] == "connected_partial"
    assert not any("checksums" in o.check_id for o in result.outcomes)


def test_public_noindex_is_linked_without_duplicate_finding(env):
    public = Evidence(
        id="ev-site_checks-001",
        source="site_checks",
        url=SITE,
        description="Noindex",
        data={"noindex": True},
    )
    other = Evidence(
        id="ev-site_checks-002",
        source="site_checks",
        url="https://other.test/",
        description="Noindex",
        data={"noindex": True},
    )
    findings = [
        {"id": "public-noindex", "check_key": "site_checks:noindex", "evidence_ids": [public.id]},
        {"id": "other-noindex", "check_key": "site_checks:noindex", "evidence_ids": [other.id]},
    ]
    result, _ = prepare(env, public_evidence=[public, other], public_findings=findings)
    comparison = endpoints(result)["option_blog_public"]["data"]["public_comparison"]
    assert comparison["related_finding_ids"] == ["public-noindex"]
    assert not any(f.check_key == "wp_cli:blog_public" for f in result.findings)


def test_resource_binding_isolation_and_credential_mismatch(env):
    auth.register_reference("second", wp_cli.CONNECTOR, file=env.config, directory=env.private)
    with pytest.raises(ValueError):
        service.set_binding(env.root, "another", "wp_cli", "second", env.resource)
    with pytest.raises(ValueError):
        service.set_binding(env.root, "another", "wp_cli", "second", env.resource, force=True)
    service.set_binding(
        env.root,
        "another",
        "wp_cli",
        "second",
        env.resource,
        force=True,
        reason="Authorised shared synthetic installation",
    )
    history = snapshot(env, "another")["connector_history"]
    assert history[-1]["forced"] and history[-1]["reason"]
    service.set_binding(env.root, "synthetic", "wp_cli", "synthetic", "wp-install:" + "0" * 64)
    _, state = prepare(env)
    assert state["status"] == "not_found" and not env.ssh.calls


def test_resource_identity_is_user_key_independent_and_path_bound(env):
    config = env.material.private_data
    assert replace(config, user="other", key_path="/other/key").resource == env.resource
    assert replace(config, path="/srv/other").resource != env.resource
    assert replace(config, host="other.test").resource != env.resource
    assert replace(config, port=2222).resource != env.resource


def test_framework_auth_flow_and_ssh_budgets_are_used(env):
    assert wp_cli.CONNECTOR.auth_flow == "reference"
    assert "--credential-file" in wp_cli.CONNECTOR.auth_command
    client = ReadClient(wp_cli.CONNECTOR, env.material, seconds=900, max_requests=900)
    try:
        assert client.max_requests == 40 and 0 < client.remaining() <= 90
    finally:
        client.close()
    result, state = prepare(env, seconds=0.000001)
    assert state["status"] == "provider_unavailable" and not env.ssh.calls
    assert not result.findings


def test_transient_failure_then_success_and_request_cap(env, monkeypatch):
    env.ssh.override = lambda op, _: (
        (255, "", "Connection timed out")
        if op == "runtime" and sum(call[0] == "runtime" for call in env.ssh.calls) == 1
        else None
    )
    _, state = prepare(env)
    assert state["status"] == "connected"
    assert sum(call[0] == "runtime" for call in env.ssh.calls) == 2
    env.ssh.calls.clear()
    real_client = service.ReadClient
    monkeypatch.setattr(
        service, "ReadClient", lambda *a, **kw: real_client(*a, max_requests=1, **kw)
    )
    result, state = prepare(env)
    assert state["status"] == "connected_partial" and len(env.ssh.calls) == 1
    assert endpoints(result)["installation"]["status"] == "connected"
    assert endpoints(result)["plugins"]["status"] == "provider_unavailable"


def test_failure_then_passing_recheck_resolves_only_equivalent_findings(env):
    service.execute(env.root, "synthetic", "wp_cli", collect=True)
    env.ssh.override = lambda op, _: (
        (1, "", "Permission denied") if op == "option_blog_public" else None
    )
    service.execute(env.root, "synthetic", "wp_cli", collect=True)
    findings = snapshot(env)["findings"]
    assert (
        next(f for f in findings if f["check_key"] == "wp_cli:blog_public")["lifecycle_status"]
        != "resolved"
    )
    env.ssh.override = lambda *_: None
    env.ssh.options["blog_public"] = "1"
    env.ssh.plugins[0]["update"] = "none"
    service.execute(env.root, "synthetic", "wp_cli", collect=True)
    findings = snapshot(env)["findings"]
    assert (
        next(f for f in findings if f["check_key"] == "wp_cli:blog_public")["lifecycle_status"]
        == "resolved"
    )
    assert (
        next(f for f in findings if f["check_key"] == "wp_cli:updates")["lifecycle_status"]
        != "resolved"
    )


@contextmanager
def ui_server(root):
    with LocalServer(root, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=server.origin, trust_env=False) as client:
                yield server, client
        finally:
            server.shutdown()
            thread.join(5)


def test_cli_ui_report_status_privacy_and_ai_aggregate_projection(env, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    env.ssh.override = lambda op, _: (
        (1, "", "Permission denied " + INSTALL + " " + PRIVATE) if op == "themes" else None
    )
    with ui_server(env.root) as (server, client):
        response = client.post(
            server.url("/connect"),
            headers={"Origin": server.origin},
            data={
                "customer": "synthetic",
                "connector": "wp_cli",
                "csrf": server.csrf,
                "action": "collect",
                "credential_ref": "synthetic",
                "resource": env.resource,
            },
        )
        assert response.status_code == 303
        page = client.get(server.url(customer="synthetic")).text
    assert cli.main(["connectors", "--customer", "synthetic"]) == 0
    output = capsys.readouterr().out
    render_customer(env.root, "synthetic", {"md"})
    report = (env.root / "synthetic/report.md").read_text().replace("\\_", "_")
    current = snapshot(env)
    assert current["connectors"]["wp_cli"]["status"] == "connected_partial"
    for text in ("connected_partial", env.resource, "themes: permission_required"):
        assert text in page and text in report and text in output
    assert "Plugin update available" in report.split("## 7.")[1]
    assert any(
        e["platform"] == "wp_cli" and e["integration"] == "connected_partial"
        for e in current["systems_register"]["entries"]
    )
    dumped = page + report + output + caplog.text + json.dumps(current)
    for secret in (
        PRIVATE,
        INSTALL,
        "person@example.test",
        env.data["key_path"],
        env.data["known_hosts"],
    ):
        assert secret not in dumped
    compact = json.dumps(
        ai.compact_evidence(
            [Evidence.model_validate(e) for e in current["evidence"] if e["source"] == "wp_cli"]
        )
    )
    assert "sample-plugin" not in compact and SITE not in compact and "8.3.20" not in compact
    assert "rows_returned" in compact


def test_plain_permalink_and_url_redaction():
    assert wp_cli.public_link(SITE + "?p=123&token=private#section") == SITE + "?p=123"
    with pytest.raises(ValueError):
        wp_cli.public_link("https://name:password@example.test/")
    with pytest.raises(ValueError):
        wp_cli.public_link(SITE + "person%40example.test")


def test_real_bounded_pipe_reader_caps_both_streams_and_cleans_up(monkeypatch):
    # Executes only a local Python producer, never SSH. Exercise the real pipe reader.
    monkeypatch.setattr(wp_cli, "MAX_BYTES", 4096)
    for stream in ("stdout", "stderr"):
        with pytest.raises(ConnectorError, match="size|cap"):
            wp_cli.run_bounded(
                [sys.executable, "-c", f"import sys; sys.{stream}.write('x'*100000)"], 2
            )
    assert wp_cli.run_bounded([sys.executable, "-c", "print('ok')"], 2) == (0, "ok\n", "")
    with pytest.raises(ConnectorError, match="cap"):
        wp_cli.run_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x'*3000); sys.stderr.write('x'*3000)",
            ],
            2,
        )


def test_real_bounded_pipe_reader_timeout(monkeypatch):
    # Avoid patching time.sleep here: the child owns its own interpreter/time module.
    with pytest.raises(httpx.ReadTimeout):
        wp_cli.run_bounded([sys.executable, "-c", "import time; time.sleep(3)"], 0.1)


def coverage_context(env, result, state):
    ctx = Context(
        "synthetic",
        "Synthetic",
        SITE,
        {"seo"},
        {"wp_cli"},
        {},
        create_run_dir(env.root, "fixture"),
        env.root / "tools",
    )
    ctx.results = [result]
    ctx.connector_results = {"wp_cli": state}
    return ctx


def test_inventory_adds_known_urls_only_with_source_and_no_network(env):
    result, state = prepare(env)
    ctx = coverage_context(env, result, state)
    ctx.pages = [SITE]
    browser = ModuleResult(name="browser")
    browser.add_evidence(SITE, "Rendered page evidence", {"page": {"viewport": "desktop"}})
    ctx.results.append(browser)

    def refuse(_):
        pytest.fail("Coverage must not fetch WP-CLI URLs")

    inventory = coverage.collect(ctx, httpx.MockTransport(refuse))
    assert {"url": SITE + "page/1", "sources": ["wp_cli"]} in inventory["discovered"]
    assert inventory["crawled"] == [SITE] and inventory["browser_tested"] == [SITE]
    assert inventory["known_urls"] == 2
    assert SITE + "page/1" in inventory["untested"]
    assert inventory["wordpress"]["wp_cli"]["source"] == "wp_cli"


def test_coverage_keeps_partial_urls_but_rejects_wrong_site_and_old_resource(env):
    env.ssh.posts["post"] = [SSH.post(i) for i in range(1, 102)]
    env.ssh.posts["page"] = [{**SSH.post(3, "page"), "url": "https://other.test/page"}]
    env.ssh.override = lambda op, page: (
        (1, "", "Permission denied") if (op, page) == ("posts_post", 2) else None
    )
    result, state = prepare(env)
    ctx = coverage_context(env, result, state)
    inventory = coverage.wp_cli_inventory(ctx)
    assert inventory is not None
    assert inventory["status"] == "partial" and len(inventory["urls"]) == 100
    assert inventory["counts"]["post"]["lower_bound"]
    assert "https://other.test/page" not in inventory["urls"]
    assert any("outside-site" in reason for reason in inventory["stop_reasons"])
    ctx.connector_results["wp_cli"] = {**state, "resource_id": "wp-install:" + "0" * 64}
    assert (coverage.wp_cli_inventory(ctx) or {})["urls"] == []
    ctx.connector_results["wp_cli"] = {**state, "evidence_ids": ["old-evidence"]}
    assert (coverage.wp_cli_inventory(ctx) or {})["urls"] == []
    ctx.connector_results["wp_cli"] = {**state, "operation": "test"}
    assert coverage.wp_cli_inventory(ctx) is None


def test_coverage_coexists_with_manual_export_and_bounds_known_urls(env, monkeypatch):
    result, state = prepare(env)
    ctx = coverage_context(env, result, state)
    export = env.root / "export.json"
    export.write_text(json.dumps([SITE + "export"]))
    ctx.wp_inventory = export
    data = coverage.collect(ctx, httpx.MockTransport(lambda _: pytest.fail("Unexpected fetch")))
    assert {"url": SITE + "export", "sources": ["authorised WordPress export"]} in data[
        "discovered"
    ]
    assert {"url": SITE + "page/1", "sources": ["wp_cli"]} in data["discovered"]
    monkeypatch.setattr(coverage, "MAX_URLS", 1)
    endpoint = next(e for e in result.evidence if e.data["operation"] == "posts_page")
    endpoint.data["data"]["urls"] = [SITE + "a", SITE + "b"]
    bounded = coverage.wp_cli_inventory(ctx)
    assert bounded is not None
    assert bounded["status"] == "partial" and len(bounded["urls"]) == 1
    assert "WP-CLI known URL limit reached" in bounded["stop_reasons"]
