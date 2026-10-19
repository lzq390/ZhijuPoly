"""Fixed-path read-only native status bridge, with no live operator or GPU."""
from datetime import datetime,timedelta,timezone
import json

import pytest

from app.services import acceptance_gpu_status as s
from app.services.dev_gpu_operator import DevGpuOperatorError


NOW=datetime(2026,10,19,6,tzinfo=timezone.utc)

@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(s,'datetime',Clock)


def envelope():
    return dict(schema_version=1,scope_id='acceptance-a-20261019',source_sha='a'*40,source_tree='b'*40,
        cohort_sha256='c'*64,observed_at=NOW.isoformat(),expires_at='2026-10-20T18:53:34Z',
        native_status=dict(schema_version=1,operator_available=True,phase='stopped',controller_status='stopped',
            can_recover=True,operation_id=None,message='native stopped',source_sha='a'*40,source_tree='b'*40,updated_at=None))


def client(monkeypatch,tmp_path,value):
    path=tmp_path/'status.public.json';path.write_text(json.dumps(value));path.chmod(0o644)
    monkeypatch.setattr(s,'STATUS_PATH',path)
    return s.AcceptanceGpuStatusClient(expected_source_sha='a'*40,expected_source_tree='b'*40),path


def test_native_fields_are_preserved_but_recovery_has_no_transport(monkeypatch,tmp_path):
    obj,path=client(monkeypatch,tmp_path,envelope())
    result=obj.request('status')
    assert result['can_recover'] is True and result['phase']=='stopped'
    with pytest.raises(DevGpuOperatorError,match='cannot execute'):obj.request('recover')
    assert obj.request('status')==result


@pytest.mark.parametrize('change',[
    {'source_sha':'f'*40},{'scope_id':'other'},{'expires_at':'2026-10-21T00:00:00Z'},
    {'observed_at':None},{'schema_version':True},{'unexpected':'field'},
    {'observed_at':(NOW-timedelta(seconds=16)).isoformat()},
    {'observed_at':(NOW+timedelta(seconds=16)).isoformat()},
])
def test_stale_changed_or_malformed_status_closes_without_socket_fallback(monkeypatch,tmp_path,change):
    obj,_=client(monkeypatch,tmp_path,{**envelope(),**change})
    with pytest.raises(DevGpuOperatorError):obj.request('status')


def test_public_projection_must_be_nonwritable_by_other_identities(monkeypatch,tmp_path):
    obj,path=client(monkeypatch,tmp_path,envelope());path.chmod(0o666)
    with pytest.raises(DevGpuOperatorError):obj.request('status')
