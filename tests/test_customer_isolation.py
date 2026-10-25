"""Two synthetic businesses prove customer isolation through stored files and captured traffic.

Every credential is a FAKE canary. Google, Bing, Microsoft Graph/MSAL and SSH are replaced at
their transport boundary, so the engine's real auth, binding, allowlist and persistence code runs
while every outbound request is recorded. Assertions inspect what was actually requested, stored,
sent to AI or written to report files, not only return values.
"""

import json
import shlex
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote

import httpx
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from test_engine import context
from test_security_ai import RecordingAI
from test_wp_cli import SSH

from ams_assess import cli, onboarding, runner, store
from ams_assess.connectors import REGISTRY, auth, bing_wmt, m365, service, wp_cli
from ams_assess.connectors import gsc as gsc_connector
from ams_assess.models import Evidence, ModuleResult, create_run_dir
from ams_assess.report import render_customer
from ams_assess.ui import LocalServer

GOOGLE = ("gsc", "ga4", "gbp", "workspace")
CONNECTORS = (*GOOGLE, "bing_wmt", "m365", "wp_cli")
CLIENT = "33333333-3333-4333-8333-333333333333"

BUSINESSES = {
    "alpha": SimpleNamespace(
        name="Alpha Plumbing Pty Ltd",
        site="https://alpha-plumbing.test/",
        domain="alpha-plumbing.test",
        canary="Quokkamarker",
        markers=("alpha-plumbing", "properties/918273645", "locations/7700112233", "Calpha01"),
        tenant="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ssh=dict(host="ssh.alpha-host.test", port=2222, user="alpharead"),
        install="/home/alpha/public_html",
        resources={
            "gsc": "sc-domain:alpha-plumbing.test",
            "ga4": "properties/918273645",
            "gbp": "locations/7700112233",
            "workspace": "Calpha01/alpha-plumbing.test",
            "bing_wmt": "https://alpha-plumbing.test/",
        },
    ),
    "bravo": SimpleNamespace(
        name="Bravo Dental Studio",
        site="https://bravo-dental.test/",
        domain="bravo-dental.test",
        canary="Pelicanmarker",
        markers=("bravo-dental", "properties/564738291", "locations/8800445566", "Cbravo02"),
        tenant="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        # Not a SiteGround layout: arbitrary host, non-default port, user and path.
        ssh=dict(host="203.0.113.40", port=31337, user="bravo_ro"),
        install="/var/www/bravo_123/public",
        resources={
            "gsc": "https://bravo-dental.test/",
            "ga4": "properties/564738291",
            "gbp": "locations/8800445566",
            "workspace": "Cbravo02/bravo-dental.test",
            "bing_wmt": "https://bravo-dental.test/",
        },
    ),
}
OWNERS = ("alpha", "bravo")
for _owner, _b in BUSINESSES.items():
    _b.resources["m365"] = "tenant:" + _b.tenant
    _b.markers += (_b.tenant,)


def secret(owner: str, key: str) -> str:
    return f"FAKE-{owner.upper()}-{key.upper()}-CANARY-TOKEN"


def ref(owner: str, key: str) -> str:
    return f"{owner}-{key.replace('_', '-')}"


def owners_in(text: str) -> set[str]:
    return {o for o in OWNERS if any(m in text for m in BUSINESSES[o].markers)}


def other(owner: str) -> str:
    return "bravo" if owner == "alpha" else "alpha"


class Cache:
    def __init__(self):
        self.has_state_changed = False
        self.value = "{}"

    def deserialize(self, value):
        self.value = value

    def serialize(self):
        return self.value


class Providers:
    """Google, Bing and Graph behind one recording MockTransport with a per-credential ACL.

    A customer credential may read only its own customer's resources; the agency credential
    may read both. Any other request is answered as the real provider would (403/404).
    """

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.failures: dict[str, str] = {}
        self.swap_identity = False
        self.labels: dict[str, str] = {}
        for owner in (*OWNERS, "agency"):
            for key in CONNECTORS:
                self.labels[secret(owner, key)] = owner
        self.revoked_tenants: set[str] = set()

    def credential(self, request: httpx.Request) -> str:
        if request.url.host == "ssl.bing.com":
            return self.labels.get(request.url.params.get("apikey", ""), "unknown")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return self.labels.get(token, "unknown")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        credential = self.credential(request)
        visible = unquote(str(request.url.copy_remove_param("apikey")))
        visible += unquote(request.content.decode() if request.content else "")
        targets = owners_in(visible)
        self.calls.append(
            SimpleNamespace(
                credential=credential,
                method=request.method,
                url=visible,
                targets=frozenset(targets),
            )
        )
        failure = self.failures.get(credential)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "401":
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        if failure == "403":
            return httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED"}})
        if failure == "approval":
            return httpx.Response(
                403,
                json={"error": {"code": 403, "details": [{"reason": "API_NOT_APPROVED"}]}},
            )
        if failure == "bing_revoked":
            return httpx.Response(400, json={"ErrorCode": 3, "Message": "InvalidApiKey"})
        allowed = set(OWNERS) if credential == "agency" else {credential}
        if targets - allowed:
            return httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED"}})
        if request.url.host == "ssl.bing.com":
            sites = [{"Url": BUSINESSES[o].site, "IsVerified": True} for o in sorted(allowed)]
            return httpx.Response(200, json={"d": sites})
        if request.url.host == "graph.microsoft.com":
            business = BUSINESSES[credential]
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": business.tenant, "verifiedDomains": [{"name": business.domain}]}
                    ]
                },
            )
        if len(targets) != 1:
            return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND"}})
        (owner,) = targets
        if self.swap_identity:
            owner = other(owner)
        return httpx.Response(200, json=self.google(owner, request))

    def google(self, owner: str, request: httpx.Request) -> dict:
        business, path = BUSINESSES[owner], request.url.path
        if request.url.host == "www.googleapis.com":
            if path.endswith("/searchAnalytics/query"):
                return {
                    "rows": [
                        {
                            "keys": [business.canary + " emergency"],
                            "clicks": 3,
                            "impressions": 40,
                            "ctr": 0.075,
                            "position": 2.5,
                        }
                    ]
                }
            if path.endswith("/sitemaps"):
                return {"sitemap": []}
            return {"siteUrl": business.resources["gsc"], "permissionLevel": "siteFullUser"}
        if request.url.host == "analyticsadmin.googleapis.com":
            return {
                "name": business.resources["ga4"],
                "displayName": business.canary + " property",
                "timeZone": "UTC",
            }
        if request.url.host == "mybusinessbusinessinformation.googleapis.com":
            return {
                "name": business.resources["gbp"],
                "title": business.canary + " location",
                "websiteUri": business.site,
            }
        identifier, domain = business.resources["workspace"].split("/")
        if path.endswith("/domains"):
            return {"domains": [{"domainName": domain, "verified": True, "isPrimary": True}]}
        return {"id": identifier, "customerDomain": domain}


class FakeSSH:
    """Record every SSH argv; answer per configured host like an independent server."""

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.failures: dict[str, str] = {}
        self.servers = {o: SSH() for o in OWNERS}
        self.hosts = {BUSINESSES[o].ssh["host"]: o for o in OWNERS}

    def __call__(self, argv, seconds):
        host = argv[argv.index("--") + 1]
        owner = self.hosts[host]
        remote = shlex.split(argv[-1])
        path = next(w.removeprefix("--path=") for w in remote if w.startswith("--path="))
        self.calls.append(
            SimpleNamespace(
                owner=owner,
                host=host,
                port=int(argv[argv.index("-p") + 1]),
                user=argv[argv.index("-l") + 1],
                path=path,
                argv=argv,
            )
        )
        failure = self.failures.get(owner)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic SSH timeout")
        if failure == "403":
            return 255, "", "bravo_ro@203.0.113.40: Permission denied (publickey).\n"
        if failure == "revoked":
            return 255, "", "Authentication key revoked.\n"
        return self.servers[owner](argv, seconds)


def google_token(private: Path, owner: str, key: str, expired: bool = False) -> Path:
    spec = REGISTRY[key]
    path = private / f"{key}-{owner}-token.json"
    expiry = datetime.now(UTC) + (timedelta(days=-1) if expired else timedelta(days=2))
    auth.atomic_private(
        path,
        {
            "token": secret(owner, key),
            "refresh_token": secret(owner, key) + "-REFRESH",
            "client_id": "fake-client.apps.googleusercontent.com",
            "client_secret": secret(owner, key) + "-CLIENT",
            "scopes": list(spec.scopes),
            "type": "authorized_user",
            "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    root, private = tmp_path / "customers", tmp_path / "private"
    private.mkdir(mode=0o700)
    for target in (service, auth, m365):
        monkeypatch.setattr(target, "config_dir", lambda: private)
    monkeypatch.setattr(cli, "data_root", lambda: root)
    monkeypatch.setattr("ams_assess.connectors.http.time.sleep", lambda _: None)

    providers, ssh = Providers(), FakeSSH()
    real_client = service.ReadClient

    def client(spec, material, transport=None, **kwargs):
        if spec.key == "wp_cli":
            return real_client(spec, material, **kwargs)  # Real SSHTransport → fake ssh.
        inner = httpx.MockTransport(providers)
        if spec.key == "bing_wmt":
            inner = bing_wmt.BingTransport(material, inner=inner)
        return real_client(spec, material, transport=inner, **kwargs)

    monkeypatch.setattr(service, "ReadClient", client)
    monkeypatch.setattr(wp_cli, "run_bounded", ssh)

    def msal_app(client_id, *, authority, token_cache, **_):
        tenant = authority.rsplit("/", 1)[1]
        owner = next(o for o in OWNERS if BUSINESSES[o].tenant == tenant)

        def silent(scopes, account):
            if tenant in providers.revoked_tenants:
                return {"error": "invalid_grant", "error_description": "FAKE revoked"}
            return {
                "access_token": secret(owner, "m365"),
                "scope": " ".join(m365.SCOPES),
                "id_token_claims": {"tid": tenant},
            }

        return SimpleNamespace(
            get_accounts=lambda: [{"realm": tenant, "home_account_id": "acct-" + tenant}],
            acquire_token_silent_with_error=silent,
        )

    monkeypatch.setattr(
        m365,
        "_sdk",
        lambda: SimpleNamespace(SerializableTokenCache=Cache, PublicClientApplication=msal_app),
    )

    env_lines = []
    for owner in (*OWNERS, "agency"):
        for key in GOOGLE:
            token = google_token(private, owner, key)
            auth.register_reference(ref(owner, key), REGISTRY[key], file=token, directory=private)
        variable = f"{owner.upper()}_BING_KEY"
        env_lines.append(f"{variable}={secret(owner, 'bing_wmt')}")
    (private / "credentials.env").write_text("\n".join(env_lines) + "\n")
    (private / "credentials.env").chmod(0o600)
    for owner in (*OWNERS, "agency"):
        auth.register_reference(
            ref(owner, "bing_wmt"),
            REGISTRY["bing_wmt"],
            env_var=f"{owner.upper()}_BING_KEY",
            directory=private,
        )

    wp_resources = {}
    for owner in OWNERS:
        business = BUSINESSES[owner]
        token = private / f"m365-{owner}-token.json"
        auth.atomic_private(
            token,
            {
                "client_id": CLIENT,
                "tenant_id": business.tenant,
                "account_id": "acct-" + business.tenant,
                "cache": "{}",
                "scopes": list(m365.SCOPES),
            },
        )
        auth.register_reference(ref(owner, "m365"), REGISTRY["m365"], file=token, directory=private)
        key, known = private / f"{owner}_ed25519", private / f"{owner}_known_hosts"
        key.write_text(secret(owner, "ssh-private-key"))
        known.write_text(f"{business.ssh['host']} ssh-ed25519 FAKE-{owner}-HOSTKEY\n")
        config = private / f"wp-{owner}.json"
        auth.atomic_private(
            config,
            {
                **business.ssh,
                "key_path": str(key),
                "known_hosts": str(known),
                "install_path": business.install,
            },
        )
        auth.register_reference(
            ref(owner, "wp_cli"), REGISTRY["wp_cli"], file=config, directory=private
        )
        material = wp_cli.authenticate(REGISTRY["wp_cli"], ref(owner, "wp_cli"), directory=private)
        wp_resources[owner] = material.private_data.resource
        business.resources["wp_cli"] = material.private_data.resource
        business.markers = tuple(dict.fromkeys((*business.markers, wp_resources[owner])))
        store.set_profile(root, owner, {"name": business.name, "websites": [business.site]})
    return SimpleNamespace(root=root, private=private, providers=providers, ssh=ssh)


def bind(world, owner, key, credential_owner=None, resource=None):
    business = BUSINESSES[owner]
    return service.set_binding(
        world.root,
        owner,
        key,
        ref(credential_owner or owner, key),
        resource or business.resources[key],
    )


def execute(world, owner, key, collect=False):
    return service.execute(world.root, owner, key, collect=collect, seconds=5)


def store_text(world, owner) -> str:
    return (world.root / owner / "assessment.json").read_text()


def customer_tree_text(world, owner) -> str:
    """Every stored regular file below the customer's directory, including reports and runs."""
    texts = []
    for path in sorted((world.root / owner).rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix in {".json", ".md", ".html"}:
            texts.append(path.read_text(errors="replace"))
    return "\n".join(texts)


def assert_no_trace_of(text: str, owner: str) -> None:
    business = BUSINESSES[owner]
    for value in (business.canary, business.name, *business.markers):
        assert value not in text, f"{owner} value leaked: {value}"


def connector_reads(world, key):
    """Outbound reads attributable to a connector, as (credential, targets/owner) records."""
    if key == "wp_cli":
        return list(world.ssh.calls)
    hosts = {
        "gsc": "www.googleapis.com",
        "ga4": "analyticsadmin.googleapis.com",
        "gbp": "mybusinessbusinessinformation.googleapis.com",
        "workspace": "//admin.googleapis.com",
        "bing_wmt": "ssl.bing.com",
        "m365": "graph.microsoft.com",
    }
    return [c for c in world.providers.calls if hosts[key] in c.url]


def fully_onboard(world, collect_keys=("gsc", "wp_cli")):
    for owner in OWNERS:
        for key in CONNECTORS:
            bind(world, owner, key)
            state = execute(world, owner, key, collect=key in collect_keys)
            assert state["status"] in {"connected", "connected_partial"}, (owner, key, state)
        store.set_register(
            world.root,
            owner,
            "ga4",
            {
                "ams_access": "granted",
                "note": BUSINESSES[owner].canary + " customer granted Viewer",
            },
        )


@contextmanager
def ui(root):
    with LocalServer(root, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(
                base_url=server.origin,
                trust_env=False,
                headers={"Authorization": "Bearer " + server.token},
            ) as client:
                yield server, client
        finally:
            server.shutdown()
            thread.join(timeout=5)


# 1. Each customer sees only its own resources, register rows, evidence and reports.


def test_each_customer_sees_only_its_own_data_in_store_cli_ui_and_files(world, capsys):
    fully_onboard(world)
    for owner in OWNERS:
        render_customer(world.root, owner, {"md", "html"})
    for owner in OWNERS:
        text = customer_tree_text(world, owner)
        assert BUSINESSES[owner].canary in text
        assert_no_trace_of(text, other(owner))
        current = store.load(world.root, owner)
        assert current is not None
        bound = {k: v["resource_id"] for k, v in current["connectors"].items()}
        assert bound == BUSINESSES[owner].resources
        for evidence in current["evidence"]:
            data = evidence.get("data")
            if isinstance(data, dict) and data.get("connector"):
                assert data["resource_id"] == BUSINESSES[owner].resources[data["connector"]]

    for owner in OWNERS:
        for argv in (
            ["register", "show", "--customer", owner],
            ["connectors", "--customer", owner],
        ):
            capsys.readouterr()
            assert cli.main(argv) == 0
            output = capsys.readouterr().out
            assert (
                BUSINESSES[owner].domain
                if "register" in argv
                else BUSINESSES[owner].resources["ga4"]
            ) in output
            assert_no_trace_of(output, other(owner))

    with ui(world.root) as (server, client):
        for owner in OWNERS:
            page = client.get(server.url(customer=owner))
            assert page.status_code == 200
            assert BUSINESSES[owner].canary in page.text
            assert_no_trace_of(page.text, other(owner))
            current = store.load(world.root, owner)
            assert current is not None
            for revision in current["report_revisions"]:
                path = revision["directory"] + "/report.md"
                served = client.get(server.url("/file", customer=owner, path=path))
                assert served.status_code == 200
                assert BUSINESSES[owner].canary in served.text
                assert_no_trace_of(served.text, other(owner))
            for escape in (
                f"../{other(owner)}/report.md",
                f"../{other(owner)}/assessment.json",
                "assessment.json",
                f"/{world.root}/{other(owner)}/report.md",
            ):
                response = client.get(server.url("/file", customer=owner, path=escape))
                assert response.status_code == 403


# 2. A credential reads only its own resource; agency credentials read only explicit bindings.


@pytest.mark.parametrize("key", CONNECTORS)
def test_customer_credential_cannot_read_other_customers_resource(world, key):
    bind(world, "bravo", key)
    before = store_text(world, "bravo")
    # The engine refuses to bind B's resource to A at all.
    with pytest.raises(ValueError, match="different customer"):
        bind(world, "alpha", key, resource=BUSINESSES["bravo"].resources[key])
    assert store_text(world, "bravo") == before
    assert connector_reads(world, key) == []

    # Using A's own credential on B's explicit binding is refused by the provider or engine.
    bind(world, "bravo", key, credential_owner="alpha")
    state = execute(world, "bravo", key, collect=True)
    assert state["status"] not in {"connected", "connected_partial"}
    for read in connector_reads(world, key):
        # No request ever returned data: the provider/SSH boundary saw A's identity only.
        if key == "wp_cli":
            raise AssertionError("SSH must not run for a mismatched installation binding")
        assert read.credential == "alpha"
    text = store_text(world, "bravo")
    assert BUSINESSES["bravo"].canary not in text
    assert "last_successful_test" not in state


@pytest.mark.parametrize("key", ("gsc", "ga4", "gbp", "workspace", "bing_wmt"))
def test_shared_agency_credential_collects_only_each_explicit_binding(world, key):
    for owner in OWNERS:
        bind(world, owner, key, credential_owner="agency")
    for owner in OWNERS:
        world.providers.calls.clear()
        state = execute(world, owner, key, collect=key == "gsc")
        assert state["status"] == "connected"
        reads = connector_reads(world, key)
        assert reads and all(r.credential == "agency" for r in reads)
        # Every request names only this customer's resource (Bing's account-level site
        # enumeration names none), never the other customer's resource.
        assert all(r.targets <= {owner} for r in reads)
        assert any(r.targets == {owner} for r in reads) or key == "bing_wmt"
        text = store_text(world, owner)
        assert BUSINESSES[owner].resources[key] in text
        assert_no_trace_of(text, other(owner))


# 3. Incorrect or mismatched bindings fail closed.


@pytest.mark.parametrize("key", GOOGLE)
def test_provider_identity_mismatch_fails_closed_without_storing_other_data(world, key):
    bind(world, "alpha", key, credential_owner="agency")
    world.providers.swap_identity = True
    state = execute(world, "alpha", key, collect=True)
    assert state["status"] not in {"connected", "connected_partial"}
    assert "last_successful_test" not in state
    text = store_text(world, "alpha")
    assert_no_trace_of(text, "bravo")


@pytest.mark.parametrize(
    "key, resource",
    [
        ("gsc", "https://unbound-site.test/"),
        ("ga4", "properties/999999"),
        ("gbp", "locations/9999"),
        ("workspace", "Cunknown9/unbound-site.test"),
        ("bing_wmt", "https://unbound-site.test/"),
        ("m365", "tenant:cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        ("wp_cli", "wp-install:" + "0" * 64),
    ],
)
def test_wrong_resource_binding_fails_closed_without_discovery_fallback(world, key, resource):
    bind(world, "bravo", key)
    execute(world, "bravo", key)
    world.providers.calls.clear()
    world.ssh.calls.clear()
    bind(world, "alpha", key, resource=resource)
    state = execute(world, "alpha", key, collect=True)
    assert state["status"] in {"not_found", "permission_required", "failed"}
    reads = connector_reads(world, key)
    # Only the explicitly bound identity was requested: no listing or "all accessible" sweep.
    assert all("bravo" not in r.targets for r in reads if hasattr(r, "targets"))
    assert not any(
        r.url.endswith(("/accountSummaries", "/sites")) for r in reads if key != "wp_cli"
    )
    if key in {"m365", "wp_cli"}:
        assert reads == []  # The engine refuses before contacting the provider.
    assert_no_trace_of(store_text(world, "alpha"), "bravo")


def test_invalid_resource_identifiers_are_rejected_before_binding(world):
    for key, value in {
        "ga4": "all",
        "gbp": "accounts/1",
        "workspace": "my_customer",
        "m365": "common",
        "wp_cli": "/home/alpha/public_html",
    }.items():
        with pytest.raises(ValueError):
            bind(world, "alpha", key, resource=value)
    assert "connectors" not in (store.load(world.root, "alpha") or {})


# 4. Errors never fall back to another customer's or a default resource.


@pytest.mark.parametrize("failure", ["401", "403", "timeout", "missing"])
@pytest.mark.parametrize("key", CONNECTORS)
def test_connector_errors_never_fall_back(world, key, failure):
    bind(world, "bravo", key)
    assert execute(world, "bravo", key)["status"] == "connected"
    bravo_before = store_text(world, "bravo")
    world.providers.calls.clear()
    world.ssh.calls.clear()
    if failure == "missing":
        service.set_binding(world.root, "alpha", key, ref("alpha", key))  # reference, no resource
    else:
        bind(world, "alpha", key)
        if key == "wp_cli":
            # An SSH server reports a rejected/revoked identity rather than an HTTP 401.
            world.ssh.failures["alpha"] = "revoked" if failure == "401" else failure
        else:
            world.providers.failures["alpha"] = failure
    state = execute(world, "alpha", key, collect=True)
    expected = {
        "missing": {"not_configured"},
        "timeout": {"provider_unavailable"},
        "401": {"credential_expired"},
        "403": {"permission_required"},
    }[failure]
    assert state["status"] in expected, state
    assert state["resource_id"] == (
        "" if failure == "missing" else BUSINESSES["alpha"].resources[key]
    )
    reads = connector_reads(world, key)
    if failure == "missing":
        assert reads == []
    for read in reads:
        if key == "wp_cli":
            assert (read.owner, read.host) == ("alpha", BUSINESSES["alpha"].ssh["host"])
        else:
            assert read.credential == "alpha" and "bravo" not in read.targets
    assert store_text(world, "bravo") == bravo_before
    assert_no_trace_of(store_text(world, "alpha"), "bravo")


def test_provider_api_approval_blocker_is_distinct_from_permission(world):
    bind(world, "alpha", "gbp")
    world.providers.failures["alpha"] = "approval"
    state = execute(world, "alpha", "gbp")
    assert state["status"] == "approval_required"
    current = store.load(world.root, "alpha")
    assert current is not None
    entry = next(e for e in current["systems_register"]["entries"] if e["platform"] == "gbp")
    assert entry["integration"] == "approval_required"
    row = next(r for r in onboarding.rows(current) if r["key"] == "gbp")
    assert row["engine"] == "Blocked — provider API approval pending"
    assert row["connected"] is False


# 5. AI payloads for one customer contain none of the other customer's data.


def test_ai_payload_for_one_customer_contains_no_other_customer_data(world, monkeypatch):
    fully_onboard(world, collect_keys=("gsc",))
    render_customer(world.root, "bravo", {"md"})
    recording = RecordingAI()
    monkeypatch.setattr("anthropic.Anthropic", recording.client)
    alpha = BUSINESSES["alpha"]
    page = Evidence(
        id="ev-browser-001",
        source="browser",
        url=alpha.site,
        description="Synthetic page",
        data={"page": {"title": alpha.canary + " home", "visible_text_summary": alpha.canary}},
    )
    monkeypatch.setattr(
        runner,
        "REGISTRY",
        {
            "browser": SimpleNamespace(
                categories={"seo"},
                run=lambda ctx: ModuleResult(name="browser", evidence=[page]),
            ),
            "gsc": runner.REGISTRY["gsc"],
        },
    )
    monkeypatch.setattr(runner.coverage, "collect", lambda ctx: {})
    ctx = context(
        world.root,
        customer_id="alpha",
        customer_name=alpha.name,
        target=alpha.site,
        categories={"seo"},
        connections={"gsc"},
        store_root=world.root,
        run_dir=create_run_dir(world.root, "alpha"),
        config={"AMS_AI_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "FAKE-ANTHROPIC-CANARY"},
    )
    world.providers.calls.clear()
    result = runner.run(ctx, formats={"md"})
    assert result["ai"]["status"] == "ok"
    sent = json.dumps(recording.received)
    assert alpha.canary in sent  # Positive control: the payload carries this customer's data.
    assert_no_trace_of(sent, "bravo")
    assert all(c.targets <= {"alpha"} for c in world.providers.calls)
    assert_no_trace_of(customer_tree_text(world, "alpha"), "bravo")


# 6. Report generation keeps separation (markdown master, HTML and latest copies).


def test_reports_are_generated_per_customer_without_cross_contamination(world):
    fully_onboard(world)
    bravo_before = customer_tree_text(world, "bravo")
    assert cli.main(["report", "--customer", "alpha", "--formats", "md,html"]) == 0
    assert customer_tree_text(world, "bravo") == bravo_before
    assert cli.main(["report", "--customer", "bravo", "--formats", "md,html"]) == 0
    for owner in OWNERS:
        directory = world.root / owner
        outputs = [directory / "report.md", directory / "report.html"]
        outputs += sorted((directory / "reports").glob("*/report.*"))
        assert len(outputs) == 4
        for path in outputs:
            text = path.read_text()
            assert BUSINESSES[owner].canary in text
            assert_no_trace_of(text, other(owner))


# 7. Data survives updates, failed collections and UI restarts.


def test_data_survives_updates_failed_collections_and_ui_restart(world):
    fully_onboard(world)
    before = store.load(world.root, "alpha")
    assert before is not None
    evidence_ids = {e["id"] for e in before["evidence"]}
    findings = {f["id"] for f in before["findings"]}
    assert findings, "WP-CLI collection should produce findings to preserve"
    previous = before["connectors"]["wp_cli"]["last_successful_collection"]
    bravo_before = store_text(world, "bravo")

    world.ssh.failures["alpha"] = "timeout"
    assert execute(world, "alpha", "wp_cli", collect=True)["status"] == "provider_unavailable"
    world.providers.failures["alpha"] = "403"
    assert execute(world, "alpha", "gsc", collect=True)["status"] == "permission_required"

    with ui(world.root) as (server, client):
        response = client.post(
            "/update",
            data={
                "csrf": server.csrf,
                "customer": "alpha",
                "platform": "bing_places",
                "site": BUSINESSES["alpha"].site,
                "presence": "publicly_observed",
                "note": "Listing found; details checked",
            },
            headers={"Origin": server.origin},
        )
        assert response.status_code == 303
        old_token = server.token
    with ui(world.root) as (server, client):
        assert server.token != old_token
        stale = client.get("/", headers={"Authorization": "Bearer " + old_token})
        assert stale.status_code == 403
        page = client.get(server.url(customer="alpha"))
        assert "Listing found; details checked" in page.text

    after = store.load(world.root, "alpha")
    assert after is not None
    assert evidence_ids <= {e["id"] for e in after["evidence"]}
    assert findings <= {f["id"] for f in after["findings"]} | {
        f["id"] for f in after.get("finding_history", [])
    }
    kept = {f["id"]: f for f in after["findings"]}
    assert all(kept[f]["lifecycle_status"] != "resolved" for f in findings if f in kept)
    assert after["connectors"]["wp_cli"]["last_successful_collection"] == previous
    assert after["connectors"]["wp_cli"]["status"] == "provider_unavailable"
    entry = next(e for e in after["systems_register"]["entries"] if e["platform"] == "bing_places")
    assert entry["presence"] == "publicly_observed"
    assert store_text(world, "bravo") == bravo_before


# 8. Revoked credentials stop working, record the right status and keep earlier evidence.


@pytest.mark.parametrize("key", ["gsc", "bing_wmt", "m365", "wp_cli"])
def test_revoked_credential_records_status_and_preserves_earlier_results(world, key, monkeypatch):
    bind(world, "alpha", key)
    first = execute(world, "alpha", key, collect=key in {"gsc", "wp_cli"})
    assert first["status"] in {"connected", "connected_partial"}
    before = store.load(world.root, "alpha")
    assert before is not None
    evidence_ids = {e["id"] for e in before["evidence"]}
    findings = {f["id"]: f for f in before["findings"]}
    world.providers.calls.clear()
    world.ssh.calls.clear()

    if key == "gsc":
        google_token(world.private, "alpha", "gsc", expired=True)

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

        monkeypatch.setattr(Credentials, "refresh", refresh)
    elif key == "bing_wmt":
        world.providers.failures["alpha"] = "bing_revoked"
    elif key == "m365":
        world.providers.revoked_tenants.add(BUSINESSES["alpha"].tenant)
    else:
        world.ssh.failures["alpha"] = "revoked"

    state = execute(world, "alpha", key, collect=True)
    assert state["status"] == "credential_expired"
    assert state["detail"] and secret("alpha", key) not in state["detail"]
    if key in {"gsc", "m365"}:
        assert connector_reads(world, key) == []  # Refresh failed: no provider read at all.
    after = store.load(world.root, "alpha")
    assert after is not None
    assert evidence_ids <= {e["id"] for e in after["evidence"]}
    for finding_id, finding in findings.items():
        kept = next(f for f in after["findings"] if f["id"] == finding_id)
        assert kept["lifecycle_status"] != "resolved"
        assert kept["evidence_ids"] == finding["evidence_ids"]
    assert after["connectors"][key].get("last_successful_test") == before["connectors"][key].get(
        "last_successful_test"
    )
    entry = next(e for e in after["systems_register"]["entries"] if e["platform"] == key)
    assert entry["integration"] == "credential_expired"
    row = next(r for r in service.rows(after) if r["key"] == key)
    assert "re-authorise" in row["next_action"].lower()
    assert secret("alpha", key) not in store_text(world, "alpha")


# WP-CLI host neutrality: every connection parameter comes from the credential reference.


def test_wp_cli_has_no_host_specific_assumptions(world):
    for owner in OWNERS:
        bind(world, owner, "wp_cli")
        assert execute(world, owner, "wp_cli")["status"] == "connected"
    for call in world.ssh.calls:
        business = BUSINESSES[call.owner]
        assert (call.host, call.port, call.user, call.path) == (
            business.ssh["host"],
            business.ssh["port"],
            business.ssh["user"],
            business.install,
        )
        argv = " ".join(call.argv).lower()
        assert f"userknownhostsfile={str(world.private).lower()}/{call.owner}_known_hosts" in argv
        assert "siteground" not in argv and "18765" not in argv
    assert {c.owner for c in world.ssh.calls} == set(OWNERS)


def test_gsc_bound_resource_url_is_exact(world):
    bind(world, "bravo", "gsc")
    execute(world, "bravo", "gsc")
    expected = gsc_connector.BASE + "/" + quote(BUSINESSES["bravo"].resources["gsc"], safe="")
    assert [c.url for c in world.providers.calls] == [unquote(expected)]


# Regression: website-property aliases are the same customer resource for binding isolation.


@pytest.mark.parametrize(
    "key, alias",
    [
        ("gsc", "https://alpha-plumbing.test/"),
        ("gsc", "https://www.alpha-plumbing.test/"),
        ("gsc", "http://shop.alpha-plumbing.test/"),
        ("bing_wmt", "http://alpha-plumbing.test/"),
        ("bing_wmt", "https://www.alpha-plumbing.test/blog/"),
    ],
)
def test_site_property_alias_of_another_customer_is_refused(world, key, alias):
    bind(world, "alpha", key)
    world.providers.calls.clear()
    with pytest.raises(ValueError, match="different customer"):
        bind(world, "bravo", key, credential_owner="agency", resource=alias)
    assert "connectors" not in (store.load(world.root, "bravo") or {})
    assert world.providers.calls == []


def test_distinct_sites_and_providers_do_not_conflict(world):
    bind(world, "alpha", "gsc")
    bind(
        world, "bravo", "gsc", credential_owner="agency", resource="https://alpha-plumbing.example/"
    )
    bind(world, "bravo", "bing_wmt", credential_owner="agency")
    current = store.load(world.root, "bravo")
    assert current is not None and set(current["connectors"]) == {"gsc", "bing_wmt"}
