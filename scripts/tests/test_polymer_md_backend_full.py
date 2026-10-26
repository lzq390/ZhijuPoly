"""Full-app harness wiring only; no lifespan, database writes or engines."""
import json
from pathlib import Path

import pytest

from scripts import polymer_md_backend_dev as harness


def write_fixture(path, value):
    """Create or alter only this test's invented private input."""
    import os
    data = value if isinstance(value, str) else json.dumps(value)
    if path.exists():
        path.write_text(data)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream: stream.write(data)


@pytest.fixture
def private_runtime(tmp_path, monkeypatch):
    # Full-app preparation changes this process's env, as it does before an
    # actual import/serve. Restore it after every pure fixture test.
    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "environ", dict(harness.os.environ))
        runtime=tmp_path/'private'
        runtime.mkdir(mode=0o700)
        config={'database':harness.DATABASE,'dsns':{kind:
            'host=127.0.0.1 port=32952 dbname='+harness.DATABASE+' user='+harness.ROLE_PREFIX+kind+' password=fixture-password'
            for kind in ('api','auth','service')}}
        write_fixture(runtime/'backend-private.json',config)
        write_fixture(runtime/'dev.env','\n'.join([
            'POLYMER_MD_DEV_ENABLED=true','POLYMER_MD_ENVIRONMENT=dev','POLYMER_MD_SUBMIT_ENABLED=false',
            'POLYMER_MD_WORKER_UDS='+str(runtime/'worker.sock'),
            'POLYMER_MD_START_AUTHORIZATION_TOKEN=fixture-only-token',
            'POLYMER_MD_ARTIFACT_ROOT='+str(runtime/'jobs')])+'\n')
        assets=tmp_path/'immutable-assets'
        (assets/'database').mkdir(parents=True)
        patch.delenv('POLYMER_MD_DEV_FRONTEND_ORIGIN',raising=False)
        for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE", "PGPASSWORD"):
            patch.delenv(key, raising=False)
        yield runtime,assets,config,patch
        from app.config import get_settings
        get_settings.cache_clear()


def test_full_settings_bind_same_private_identity_pause_cookie_and_assets(private_runtime):
    runtime,assets,config,patch=private_runtime
    patch.setenv('APP_POSTGRES_DSN','shared-do-not-adopt')
    patch.setenv('MONOMER_MD_WORKER_BASE_URL','http://shared-do-not-call')
    patch.setenv('POLYMER_MD_SUBMIT_ENABLED','true')
    patch.setenv('POLYMER_MD_DEV_FRONTEND_ORIGIN','http://127.0.0.1:39872')
    settings=harness.full_app_settings(runtime,asset_root=assets)
    from app.auth.settings import AuthSettings
    from app.config import get_settings
    auth=AuthSettings.from_settings(settings)
    assert settings.app_postgres_dsn==config['dsns']['api']==settings.pi_postgres_dsn
    assert auth.auth_dsn==config['dsns']['auth'] and auth.service_dsn==config['dsns']['service']
    assert auth.cookie_name=='nexpoly_dev_polymer_md_session' and not auth.cookie_secure
    assert auth.allowed_origins==(harness.ORIGIN,'http://127.0.0.1:39872')
    assert settings.polymer_md_dev_enabled and not settings.polymer_md_submit_enabled
    assert settings.polymer_md_artifact_root==str(runtime/'jobs')
    assert settings.property_filter_csv_file==assets/'database/PolymerDatabaseV2.0_reliable085_standardized.csv'
    assert get_settings().property_filter_csv_file==settings.property_filter_csv_file
    assert not settings.gpu_broker_enabled and not settings.model_enabled and not settings.gen_model_enabled
    assert not settings.monomer_md_worker_base_url and not settings.monomer_dft_worker_uds
    assert not settings.monomer_md_submit_enabled and not settings.monomer_dft_submit_enabled


@pytest.mark.parametrize('kind', ['api','auth','service'])
def test_full_app_cannot_retarget_a_shared_database_or_role(private_runtime, kind):
    runtime,assets,config,_=private_runtime
    config['dsns'][kind]='host=127.0.0.1 port=5432 dbname=shared user=shared password=fixture'
    write_fixture(runtime/'backend-private.json',config)
    with pytest.raises(ValueError,match='non-task database or role'):
        harness.full_app_settings(runtime,asset_root=assets)


def test_full_app_keeps_the_complete_main_router_without_starting_it(private_runtime):
    runtime,assets,_,_=private_runtime
    app=harness.create_full_app(runtime,asset_root=assets)
    paths={route.path for route in app.routes}
    assert app.title=='PolyProp API'
    assert '/api/v1/polymer-md/jobs' in paths and '/api/v1/auth/login' in paths
    assert '/internal/deployment/status' in paths
    assert app.state.polymer_md_reconciler is not None and app.state.polymer_md_reconciler.task is None


def test_full_preflight_neither_starts_lifespan_nor_hides_missing_platform_data(private_runtime, monkeypatch):
    runtime,assets,_,_=private_runtime
    from app import postgres_preflight
    from app.auth.service import AuthService
    observed=[]

    def probe(settings, **options):
        observed.append(options)
        return {'schema_target':'polymer-md-0023','postgres':{'reachable':True,'tables':{'core.polymer_property_filter_records':0}},
            'analytics_snapshot':{'source':'postgres-missing'},'files':{'property_filter_csv':{'exists':True}},
            'strict_errors':['Property filter records are empty','Required Postgres analytics snapshot is missing or invalid',
                             'private error host=db password=must-not-escape']}

    monkeypatch.setattr(postgres_preflight,'run_preflight',probe)
    monkeypatch.setattr(postgres_preflight,'preflight_blockers',lambda report:report['strict_errors'])
    monkeypatch.setattr(AuthService,'assert_application_ready',lambda _self:None)
    result=harness.preflight_full_app(runtime,asset_root=assets)
    assert observed==[{'mode':'runtime','strict':True,'schema_target':postgres_preflight.SCHEMA_TARGET_CURRENT_ISOLATION}]
    assert not result['ready'] and result['blocker_count']==3 and result['property_filter_records']==0
    assert set(result['blocker_codes'])=={'property_filter_source_or_dataset_not_ready',
        'analytics_snapshot_missing_or_inconsistent','runtime_preflight_blocked'}
    assert 'password' not in json.dumps(result) and 'must-not-escape' not in json.dumps(result)


def test_private_reader_rejects_links_permissions_and_oversize(tmp_path):
    import os
    runtime = tmp_path / "private-reader"
    runtime.mkdir(mode=0o700)
    target = runtime / "target"
    write_fixture(target, "synthetic")
    linked = runtime / "linked"
    linked.symlink_to(target)
    with pytest.raises(ValueError): harness.read_private(linked)
    hard = runtime / "hard"
    os.link(target, hard)
    with pytest.raises(ValueError): harness.read_private(target)
    hard.unlink()
    public = runtime / "public"
    public.write_text("synthetic")
    public.chmod(0o644)
    with pytest.raises(ValueError): harness.read_private(public)
    ancestor = tmp_path / "linked-parent"
    ancestor.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(ValueError): harness.read_private(ancestor / "target")
    big = runtime / "big"
    write_fixture(big, "x" * (harness.MAX_PRIVATE_BYTES + 1))
    with pytest.raises(ValueError): harness.read_private(big)


def test_private_reader_rejects_concurrent_replacement(tmp_path, monkeypatch):
    runtime = tmp_path / "private-race"
    runtime.mkdir(mode=0o700)
    target = runtime / "config"
    write_fixture(target, "first synthetic value")
    original = harness.os.read
    replaced = False
    def race(fd, size):
        nonlocal replaced
        value = original(fd, size)
        if not replaced:
            replaced = True
            target.rename(runtime / "original")
            write_fixture(target, "replacement synthetic value")
        return value
    monkeypatch.setattr(harness.os, "read", race)
    # Renaming can first change the open inode ctime, or be detected by the
    # final path identity check. Both must reject, without returning either value.
    with pytest.raises(ValueError, match=r"runtime configuration (?:changed while reading|path identity changed)"):
        harness.read_private(target)
    assert replaced
    assert target.read_text() == "replacement synthetic value"


def test_cli_exposes_no_initialization_or_identity_mutation(monkeypatch):
    import sys
    for action in ("initialize", "login", "submit", "cancel"):
        monkeypatch.setattr(sys, "argv", ["polymer_md_backend_dev.py", action, "--runtime", "/unused"])
        with pytest.raises(SystemExit) as exc:
            harness.main()
        assert exc.value.code == 2


@pytest.mark.parametrize("extra", ["hostaddr=192.0.2.10", "service=synthetic-service", "options='-c role=postgres'", "passfile=/unused"])
def test_full_settings_refuse_noncanonical_libpq_fields(private_runtime, extra):
    runtime, assets, config, _ = private_runtime
    config["dsns"]["api"] += " " + extra
    write_fixture(runtime / "backend-private.json", config)
    with pytest.raises(ValueError, match="canonical task connection fields"):
        harness.full_app_settings(runtime, asset_root=assets)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE", "PGPASSWORD"])
def test_full_settings_refuse_inherited_libpq_routing_or_credentials(private_runtime, variable):
    runtime, assets, _, patch = private_runtime
    patch.setenv(variable, "synthetic-not-used")
    with pytest.raises(ValueError, match="inherited libpq"):
        harness.full_app_settings(runtime, asset_root=assets)
