"""CLI safety and real disposable-PG configuration. Never uses a live DSN."""
from types import SimpleNamespace as NS
from uuid import uuid4
import copy
import json

import psycopg
from psycopg.conninfo import make_conninfo
import pytest

from app import entitlement_cli as cli
from app.auth.context import SYSTEM_USER_ID
from task_usage.entitlement_policy import example_policy, policy_sha256
from task_usage import _entitlement_store as store
from test_entitlement_repository import entitlement_db, usage_db, connection


@pytest.fixture
def policy_file(tmp_path):
    policy = example_policy()
    policy["version"] = "cli-test-" + uuid4().hex
    target = tmp_path / "policy.json"
    target.write_text(json.dumps(policy))
    return target, policy


def args_for(command, policy_file, *, apply=False):
    args = [command, "--file", str(policy_file[0]), "--allow-example"]
    return args + (["--apply"] if apply else [])


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.existing_policy = None
        self.owner = {"user_id": str(uuid4()), "is_system": False}
        self.tier = None
        self.authority = {"role": "untrusted", "superuser": False, "service": False, "schema_owner": False}
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=()):
        self.statements.append(sql)
        assert sql.lstrip().split()[0] in {"SELECT", "SET"}, "preview attempted a write"
        value = None
        if "SELECT current_user AS role" in sql: value = self.authority
        elif "SELECT policy_sha256,document" in sql: value = self.existing_policy
        elif "SELECT user_id,is_system" in sql: value = self.owner
        elif "SELECT tier FROM" in sql: value = self.tier
        return NS(fetchone=lambda: value)


@pytest.fixture
def fake_database(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", "synthetic-dsn-secret-do-not-print")
    monkeypatch.setattr(cli, "_connect", lambda _: connection)
    monkeypatch.setattr(cli, "_check_authority", lambda _: None)
    monkeypatch.setattr(cli, "_check_readiness", lambda _: None)
    return connection


def test_validate_policy_has_no_database_dependency(policy_file, monkeypatch, capsys):
    monkeypatch.delenv("APP_SERVICE_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(cli, "_connect", lambda _: pytest.fail("validate-policy must not connect"))
    assert cli.main(args_for("validate-policy", policy_file)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] and not output["written"]
    assert output["policy_sha256"] == policy_sha256(policy_file[1])


@pytest.mark.parametrize("command", ["validate-policy", "install-policy"])
def test_example_requires_explicit_opt_in_before_database(command, policy_file, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_connect", lambda _: pytest.fail("example rejection precedes database"))
    assert cli.main([command, "--file", str(policy_file[0])]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "example_policy_requires_allow_example"


def test_parser_and_connection_errors_never_echo_secret(policy_file, monkeypatch, capsys):
    secret = "postgresql://secret-user:secret-password@private-host/database"
    assert cli.main(["set-tier", "--owner", secret, "--tier", "member"]) == 2
    assert secret not in capsys.readouterr().err
    assert cli.main(["set-tier", "--owner", str(uuid4()), "--tier", secret]) == 2
    assert secret not in capsys.readouterr().err
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", secret)
    def fail(_): raise RuntimeError(secret)
    monkeypatch.setattr(cli, "_connect", fail)
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 3
    observed = capsys.readouterr()
    assert secret not in observed.out + observed.err
    assert json.loads(observed.err)["code"] == "database_operation_failed"


@pytest.mark.parametrize("text", ['{"version":"first","version":"secret"}', '{"x":NaN}', 'not JSON'])
def test_invalid_policy_is_rejected_without_content_in_error(tmp_path, text, capsys):
    file = tmp_path / "invalid.json"; file.write_text(text)
    assert cli.main(["validate-policy", "--file", str(file)]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "invalid_policy"


def test_install_preview_is_a_read_only_transaction_and_never_calls_store(policy_file, fake_database, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_admin_store", lambda: pytest.fail("preview must not call mutation interface"))
    assert cli.main(args_for("install-policy", policy_file)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "preview" and output["would_install"] and output["written"] is False
    assert fake_database.statements[0] == "SET TRANSACTION READ ONLY"


def test_set_tier_preview_reads_only_and_checks_account(fake_database, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_admin_store", lambda: pytest.fail("preview must not call mutation interface"))
    owner = str(uuid4())
    assert cli.main(["set-tier", "--owner", owner, "--tier", "free"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["owner_user_id"] == owner and output["would_change"] and not output["written"]
    assert fake_database.statements[0] == "SET TRANSACTION READ ONLY"


def test_wrong_role_is_rejected_before_readiness_or_mutation(policy_file, monkeypatch, capsys):
    fake = FakeConnection()
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", "synthetic")
    monkeypatch.setattr(cli, "_connect", lambda _: fake)
    monkeypatch.setattr(cli, "_check_readiness", lambda _: pytest.fail("untrusted role must stop first"))
    monkeypatch.setattr(cli, "_admin_store", lambda: pytest.fail("untrusted role must never write"))
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 3
    assert json.loads(capsys.readouterr().err)["code"] == "configuration_permission_denied"


def test_failed_readiness_cannot_write(policy_file, fake_database, monkeypatch, capsys):
    def failed(_): raise cli.CliError("database_readiness_failed", 3)
    monkeypatch.setattr(cli, "_check_readiness", failed)
    monkeypatch.setattr(cli, "_admin_store", lambda: pytest.fail("failed readiness must never write"))
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 3
    assert json.loads(capsys.readouterr().err)["code"] == "database_readiness_failed"


def test_install_apply_uses_explicit_admin_interface_even_with_collection_off(policy_file, fake_database, monkeypatch, capsys):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
    writes = []
    admin = NS(install_policy=lambda connection, policy: writes.append((connection, policy)) or {"installed": True})
    monkeypatch.setattr(cli, "_admin_store", lambda: admin)
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 0
    assert len(writes) == 1 and writes[0] == (fake_database, policy_file[1])
    assert "SET TRANSACTION READ ONLY" not in fake_database.statements
    assert json.loads(capsys.readouterr().out)["written"] is True


def test_existing_policy_content_conflict_cannot_mutate(policy_file, fake_database, monkeypatch, capsys):
    fake_database.existing_policy = {"document": {}, "policy_sha256": "0" * 64}
    monkeypatch.setattr(cli, "_admin_store", lambda: pytest.fail("immutable version conflict must not write"))
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "policy_version_conflict"


@pytest.mark.parametrize("user,code", [(None, "owner_not_found"), ({"is_system": True}, "system_owner_forbidden")])
def test_missing_or_system_owner_cannot_mutate(fake_database, monkeypatch, capsys, user, code):
    fake_database.owner = user
    monkeypatch.setattr(cli, "_admin_store", lambda: pytest.fail("invalid owner must not mutate"))
    assert cli.main(["set-tier", "--owner", str(uuid4()), "--tier", "free", "--apply"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == code


def _env_role(monkeypatch, dsn, role):
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", make_conninfo(dsn, options="-c role=" + role))
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")


def _policy_row(dsn, version):
    with psycopg.connect(dsn) as active:
        return active.execute("SELECT policy_sha256,document FROM usage_entitlements.policy_versions WHERE version=%s", (version,)).fetchone()


def _create_account(dsn):
    owner = str(uuid4())
    with psycopg.connect(dsn) as active:
        active.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,'!',false)",
                       (owner, "cli-owner-" + uuid4().hex[:12]))
    return owner


def test_postgres_install_preview_apply_repeat_and_conflict_with_collection_off(entitlement_db, policy_file, monkeypatch, capsys):
    _env_role(monkeypatch, entitlement_db, "nexpoly_service")
    assert cli.main(args_for("install-policy", policy_file)) == 0
    assert json.loads(capsys.readouterr().out)["would_install"] is True
    assert _policy_row(entitlement_db, policy_file[1]["version"]) is None
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is True
    row = _policy_row(entitlement_db, policy_file[1]["version"])
    assert row == (policy_sha256(policy_file[1]), policy_file[1])
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is False
    changed = copy.deepcopy(policy_file[1]); changed["tiers"]["member"]["pools"]["assistant"] += 1
    policy_file[0].write_text(json.dumps(changed))
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "policy_version_conflict"
    assert _policy_row(entitlement_db, policy_file[1]["version"]) == row


def test_postgres_set_tier_preview_apply_with_collection_off_and_invalid_owner_denied(entitlement_db, monkeypatch, capsys):
    _env_role(monkeypatch, entitlement_db, "nexpoly_service")
    owner = _create_account(entitlement_db)
    args = ["set-tier", "--owner", owner, "--tier", "free"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["would_change"] is True
    with connection(entitlement_db, owner, "nexpoly_service") as active:
        assert store.get_owner_tier(active, owner_user_id=owner) == "member"
    assert cli.main([*args, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True
    with connection(entitlement_db, owner, "nexpoly_service") as active:
        assert store.get_owner_tier(active, owner_user_id=owner) == "free"
    for invalid, code in [(str(uuid4()), "owner_not_found"), (SYSTEM_USER_ID, "system_owner_forbidden")]:
        assert cli.main(["set-tier", "--owner", invalid, "--tier", "free", "--apply"]) == 2
        assert json.loads(capsys.readouterr().err)["code"] == code
        with psycopg.connect(entitlement_db) as active:
            assert active.execute("SELECT 1 FROM usage_entitlements.owner_entitlements WHERE owner_user_id=%s", (invalid,)).fetchone() is None


def test_postgres_wrong_role_never_writes(entitlement_db, policy_file, monkeypatch, capsys):
    _env_role(monkeypatch, entitlement_db, "nexpoly_api")
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 3
    assert json.loads(capsys.readouterr().err)["code"] == "configuration_permission_denied"
    assert _policy_row(entitlement_db, policy_file[1]["version"]) is None


def test_postgres_tampered_schema_and_release_fail_readiness_without_write(entitlement_db, policy_file, monkeypatch, capsys):
    _env_role(monkeypatch, entitlement_db, "nexpoly_service")
    with psycopg.connect(entitlement_db) as active:
        active.execute("ALTER TABLE usage_entitlements.operations DISABLE ROW LEVEL SECURITY")
    try:
        assert cli.main(args_for("install-policy", policy_file, apply=True)) == 3
        assert json.loads(capsys.readouterr().err)["code"] == "database_readiness_failed"
        assert _policy_row(entitlement_db, policy_file[1]["version"]) is None
    finally:
        with psycopg.connect(entitlement_db) as active:
            active.execute("ALTER TABLE usage_entitlements.operations ENABLE ROW LEVEL SECURITY")
    with psycopg.connect(entitlement_db) as active:
        original = active.execute("SELECT checksum FROM governance.schema_migrations WHERE version='0022_usage_entitlements'").fetchone()[0]
        active.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version='0022_usage_entitlements'", ("0" * 64,))
    try:
        assert cli.main(args_for("install-policy", policy_file, apply=True)) == 3
        assert json.loads(capsys.readouterr().err)["code"] == "database_readiness_failed"
        assert _policy_row(entitlement_db, policy_file[1]["version"]) is None
    finally:
        with psycopg.connect(entitlement_db) as active:
            active.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version='0022_usage_entitlements'", (original,))


def test_postgres_admin_configuration_supported_without_runtime_enable(entitlement_db, policy_file, monkeypatch, capsys):
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", entitlement_db)
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
    assert cli.main(args_for("install-policy", policy_file, apply=True)) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is True
    assert _policy_row(entitlement_db, policy_file[1]["version"]) is not None
