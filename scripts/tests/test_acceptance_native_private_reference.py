"""Metadata pinning never consumes a credential byte or broadens file scope."""
import os
from types import SimpleNamespace

import pytest

from ops.acceptance import native_private_reference as r


@pytest.fixture
def private_path(monkeypatch,tmp_path):
    path=tmp_path/'backend.auth.env'
    path.write_text('PUBLIC_FAKE_VALUE=not-a-credential\n')
    path.chmod(0o600)
    monkeypatch.setattr(r,'PRIVATE_PATH',path)
    return path


def test_reference_reads_metadata_only(monkeypatch,private_path):
    def forbidden(*args,**kwargs):pytest.fail('credential contents may not be read')
    original_open=os.open
    def metadata_open(path,flags):
        assert flags&os.O_PATH
        return original_open(path,flags)
    monkeypatch.setattr(r,'os',SimpleNamespace(O_PATH=os.O_PATH,O_CLOEXEC=os.O_CLOEXEC,O_NOFOLLOW=os.O_NOFOLLOW,
        open=metadata_open,close=os.close,fstat=os.fstat,read=forbidden))
    value=r.snapshot()
    assert r.verify(value)==private_path
    assert value['mode']==0o600 and value['nlink']==1


def test_wrong_native_path_is_rejected(private_path):
    value=r.snapshot()
    value['path']='/etc/shadow'
    with pytest.raises(r.ReferenceError,match='invalid_native_private'):r.verify(value)


def test_metadata_pin_detects_in_place_change(private_path):
    value=r.snapshot()
    private_path.write_text('PUBLIC_CHANGED_FAKE_VALUE=still-not-a-credential\n')
    with pytest.raises(r.ReferenceError,match='reference_changed'):r.verify(value)


def test_metadata_pin_detects_replacement(private_path):
    value=r.snapshot()
    replacement=private_path.with_name('replacement.public.env')
    replacement.write_text('PUBLIC_FAKE_VALUE=not-a-credential\n')
    replacement.chmod(0o600)
    replacement.replace(private_path)
    with pytest.raises(r.ReferenceError,match='reference_changed'):r.verify(value)


def test_symlink_and_broad_permissions_are_rejected(private_path,tmp_path,monkeypatch):
    private_path.chmod(0o640)
    with pytest.raises(r.ReferenceError,match='unsafe_native_private'):r.snapshot()
    private_path.chmod(0o600)
    link=tmp_path/'linked.env'
    link.symlink_to(private_path)
    monkeypatch.setattr(r,'PRIVATE_PATH',link)
    with pytest.raises(r.ReferenceError,match='reference_symlink'):r.snapshot()


def test_unknown_or_boolean_metadata_fields_are_rejected(private_path):
    value=r.snapshot()
    value['inode']=True
    with pytest.raises(r.ReferenceError,match='invalid_native_private'):r.verify(value)


def native_cohort_document(tmp_path):
    from scripts.dev_gpu_cohort import canonical_json
    root=tmp_path/'candidate'
    path=root/'.runtime/cohorts/r2-a-20261019/cohort.json'
    path.parent.mkdir(parents=True)
    for directory in [root,root/'.runtime',root/'.runtime/cohorts',path.parent]:directory.chmod(0o700)
    limits=dict(cpu_quota_percent=100,memory_max_bytes=1024**3,memory_swap_max_bytes=0,tasks_max=32)
    value=dict(schema_version=3,cohort_id=r.COHORT_ID,repo_root=str(root),source_sha='a'*40,source_tree='b'*40,
        compose_project='nexpoly_accept_reference_fixture',frontend_port=38965,backend_port=37799,
        auth_env_file=str(r.PRIVATE_PATH),auth_env_reference=r.snapshot(),md_resources=limits,dft_resources=limits,
        backend_resources=limits,prior_backend=None,systemd_inventory_provider='root-unix-v1')
    raw=canonical_json(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return root,path,raw


def test_existing_native_private_reference_can_prepare_exact_cohort_without_values(monkeypatch,private_path,tmp_path):
    from scripts.dev_gpu_cohort import DevGpuCohort
    import hashlib
    root,path,raw=native_cohort_document(tmp_path)
    monkeypatch.setattr(r,'WORKTREE',root)
    cohort=DevGpuCohort.load(path,hashlib.sha256(raw).hexdigest(),repo_root=root)
    cohort.prepare()
    cohort.require_prepared()
    assert cohort.auth_env_file==private_path and cohort.auth_env_reference
    private_path.write_text('PUBLIC_CHANGED_FAKE_VALUE=not-a-credential\n')
    with pytest.raises(RuntimeError,match='metadata changed'):cohort.verify()


def test_other_worktree_cannot_adopt_external_auth_reference(private_path,tmp_path):
    from scripts.dev_gpu_cohort import DevGpuCohort,CohortError
    import hashlib
    root,path,raw=native_cohort_document(tmp_path)
    with pytest.raises(CohortError,match='outside the finite acceptance'):DevGpuCohort.load(path,hashlib.sha256(raw).hexdigest(),repo_root=root)
