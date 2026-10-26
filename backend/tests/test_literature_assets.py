from types import SimpleNamespace
import json
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest
import urllib3
import psycopg
from psycopg.rows import dict_row

from app.services import literature_assets as assets
from test_auth_isolation import auth_database
from test_private_http_support import authenticated_client


class Body:
    def __init__(self, data):
        self.data, self.closed, self.released = data, False, False

    def read(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


@pytest.fixture
def storage(monkeypatch):
    class Storage:
        data = b'%PDF-test'
        etag = 'opaque-multipart-3'
        reads = []
        def stat_object(self, bucket, key, **kwargs):
            return SimpleNamespace(size=len(self.data), etag=self.etag, version_id=None)
        def get_object(self, bucket, key, **kwargs):
            assert kwargs['request_headers']['If-Match'] == '"opaque-multipart-3"'
            value = self.data[kwargs['offset']:]
            if kwargs['length']:
                value = value[:kwargs['length']]
            body = Body(value)
            self.reads.append(body)
            return body
    instance = Storage()
    monkeypatch.setattr(assets, '_client', lambda: instance)
    return instance


def manifest(storage, role='pdf'):
    return [dict(asset_uid='asset:registered', role=role, status='available', bucket='private',
                 object_key='private/key', filename='文献.pdf', etag=storage.etag,
                 size_bytes=len(storage.data), exact_input_verified=False)]


def client(storage):
    app = FastAPI()
    @app.api_route('/files/{uid}', methods=['GET', 'HEAD'])
    async def file(uid: str, request: Request):
        return await assets.get_literature_file_response(assets_manifest=manifest(storage),file_uid=uid,request=request)
    return TestClient(app)


def test_range_head_download_and_release(storage):
    with client(storage) as http:
        response = http.get('/files/asset:registered', headers={'Range': 'bytes=1-3'})
        assert response.status_code == 206 and response.content == b'PDF'
        assert response.headers['Content-Range'] == 'bytes 1-3/9'
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert storage.reads[-1].closed and storage.reads[-1].released
        before = len(storage.reads)
        assert http.head('/files/asset:registered').status_code == 200
        assert len(storage.reads) == before
        assert http.get('/files/asset:registered?download=true').headers['content-disposition'].startswith('attachment;')
        assert http.get('/files/asset:registered', headers={'Range': 'bytes=0-1,4-5'}).status_code == 416
        assert http.get('/files/asset:registered', headers={'Range': 'bytes=-0'}).status_code == 416
        assert http.get('/files/asset:unregistered').status_code == 404


@pytest.mark.parametrize('range_value', [
    'bytes=' + '9' * 5000 + '-',
    'bytes=0-' + '9' * 5000,
    'bytes=-' + '9' * 5000,
], ids=['oversized-start', 'oversized-end', 'oversized-suffix'])
def test_oversized_range_numbers_return_416(storage, range_value):
    with client(storage) as http:
        response = http.get('/files/asset:registered', headers={'Range': range_value})
    assert response.status_code == 416
    assert response.json()['detail']['code'] == 'LITERATURE_RANGE_INVALID'
    assert response.headers['Content-Range'] == 'bytes */9'
    assert not storage.reads


def test_changed_objects_never_read(storage):
    frozen = manifest(storage)[0]
    storage.etag = 'new'
    with pytest.raises(Exception) as error:
        assets._verify(frozen)
    assert error.value.detail['code'] == 'LITERATURE_ASSET_CHANGED'
    assert not storage.reads


def test_exact_selected_input_ignores_only_explicitly_nonselected_versions(storage):
    exact = dict(manifest(storage, 'full_text')[0], exact_input_verified=True)
    other = dict(exact, asset_uid='asset:other', status='ambiguous', exact_input_verified=False,
                 error_code='NON_SELECTED_EXTRACTION_INPUT')
    assert assets._select([other, exact], role='full_text') is exact
    for candidates in ([exact, dict(other, error_code='OTHER')],
                       [exact, dict(other, status='available')],
                       [exact, dict(exact, asset_uid='asset:second')]):
        with pytest.raises(HTTPException) as error:
            assets._select(candidates, role='full_text')
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_fulltext_order_safe_table_cursor_binding_and_release(storage):
    storage.data = json.dumps([{'idx': 23,'text': '<script>alert(1)</script><table><tr><td>PI-1</td><td>264</td></tr></table>'},
                               {'idx':24,'text':'Second block'}]).encode()
    items = manifest(storage,'full_text')
    result = await assets.get_literature_fulltext(assets_manifest=items,document_uid='doc',snapshot_uid='snap',limit=1)
    assert result['blocks'][0]['block_id'] == '23'
    assert result['blocks'][0]['parts'] == [{'type':'table','rows':[['PI-1','264']]}]
    assert not result['exact_input_verified']
    next_page = await assets.get_literature_fulltext(assets_manifest=items,document_uid='doc',snapshot_uid='snap',cursor=result['next_cursor'])
    assert next_page['blocks'][0]['block_id'] == '24' and next_page['next_cursor'] is None
    with pytest.raises(Exception) as error:
        await assets.get_literature_fulltext(assets_manifest=items,document_uid='other',snapshot_uid='snap',cursor=result['next_cursor'])
    assert error.value.detail['code'] == 'LITERATURE_CURSOR_INVALID'
    assert all(body.closed and body.released for body in storage.reads)
    safe = assets.get_literature_asset_summary(items)
    assert 'bucket' not in safe['assets'][0] and 'object_key' not in safe['assets'][0]


def test_suffix_range_and_empty():
    assert assets._range('bytes=-3', 9) == (6,3,True)
    assert assets._range('bytes=3-', 9) == (3,6,True)
    assert assets._range(None, 0) == (0,0,False)
    with pytest.raises(Exception):
        assets._range('bytes=9-',9)


@pytest.mark.parametrize('method', ['stat_object', 'get_object'])
@pytest.mark.parametrize('error,status,code', [
    (TimeoutError('private endpoint'),504,'LITERATURE_ASSET_TIMEOUT'),
    (urllib3.exceptions.ReadTimeoutError(None,'private endpoint','private key'),504,'LITERATURE_ASSET_TIMEOUT'),
    (RuntimeError('private secret'),503,'LITERATURE_ASSET_UNAVAILABLE'),
])
def test_sdk_transport_failures_are_safe(storage,monkeypatch,method,error,status,code):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(storage,method,fail)
    with client(storage) as http:
        response=http.get('/files/asset:registered')
    assert response.status_code==status and response.json()['detail']['code']==code
    assert 'private endpoint' not in response.text and 'private secret' not in response.text


@pytest.mark.parametrize('fulltext',[False,True])
@pytest.mark.asyncio
async def test_first_read_timeout_has_code_and_releases_body(storage,monkeypatch,fulltext):
    body=Body(b'')
    def fail(*args):
        raise urllib3.exceptions.ReadTimeoutError(None,'private endpoint','secret')
    monkeypatch.setattr(body,'read',fail)
    monkeypatch.setattr(storage,'get_object',lambda *args,**kwargs:body)
    with pytest.raises(HTTPException) as error:
        if fulltext:
            await assets.get_literature_fulltext(assets_manifest=manifest(storage,'full_text'),document_uid='doc',snapshot_uid='snap')
        else:
            await assets.get_literature_file_response(assets_manifest=manifest(storage),file_uid='asset:registered',request=Request({'type':'http','method':'GET','headers':[],'query_string':b''}))
    assert error.value.status_code==504 and error.value.detail['code']=='LITERATURE_ASSET_TIMEOUT'
    assert body.closed and body.released


@pytest.mark.parametrize('after_first_chunk',[False,True])
@pytest.mark.asyncio
async def test_cancel_closes_producer_even_before_iteration(storage,after_first_chunk):
    request=Request({'type':'http','method':'GET','headers':[],'query_string':b''})
    response=await assets.get_literature_file_response(assets_manifest=manifest(storage),file_uid='asset:registered',request=request)
    reached=asyncio.Event()
    async def send(message):
        if message['type']==('http.response.body' if after_first_chunk else 'http.response.start'):
            reached.set()
            await asyncio.Event().wait()
    async def receive():
        await asyncio.Event().wait()
    task=asyncio.create_task(response({'type':'http','method':'GET','asgi':{'version':'3.0','spec_version':'2.0'}},receive,send))
    await asyncio.wait_for(reached.wait(),3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert storage.reads[-1].closed and storage.reads[-1].released


@pytest.mark.asyncio
async def test_midstream_timeout_aborts_and_closes(storage,monkeypatch):
    storage.data=b'a'*(64*1024+10)
    response=await assets.get_literature_file_response(assets_manifest=manifest(storage),file_uid='asset:registered',request=Request({'type':'http','method':'GET','headers':[],'query_string':b''}))
    producer=response.private_content
    assert len(next(producer))==64*1024
    def fail(*args):
        raise TimeoutError('private secret')
    monkeypatch.setattr(storage.reads[-1],'read',fail)
    with pytest.raises(HTTPException) as error:
        next(producer)
    assert error.value.detail['code']=='LITERATURE_ASSET_TIMEOUT'
    assert storage.reads[-1].closed and storage.reads[-1].released


def test_native_file_context_fallback_is_bound_to_real_cookie_session(storage,auth_database):
    app=FastAPI()
    @app.api_route('/api/v1/knowledge/literature/doc/files/{uid}/content',methods=['GET','HEAD','POST'])
    async def file(uid:str,request:Request):
        return await assets.get_literature_file_response(assets_manifest=manifest(storage),file_uid=uid,request=request)
    @app.get('/api/v1/knowledge/sources')
    def sources():
        return {'ok':True}
    path='/api/v1/knowledge/literature/doc/files/asset:registered/content'
    with authenticated_client(app,auth_database) as http:
        context=http.headers.pop('x-session-context')
        assert http.get(path).status_code==409
        assert http.get(path,params={'session_context':'old-session'}).status_code==409
        response=http.get(path,params={'session_context':context})
        assert response.status_code==200 and response.content==b'%PDF-test'
        assert http.head(path,params={'session_context':context}).status_code==200
        assert http.get(path,params={'session_context':context,'download':'true'}).headers['content-disposition'].startswith('attachment;')
        assert http.get(path,params={'session_context':context},headers={'x-session-context':'wrong'}).status_code==409
        assert http.get(path,params={'session_context':'wrong'},headers={'x-session-context':context}).status_code==200
        assert http.get(path,params=[('session_context',context),('session_context',context)]).status_code==409
        assert http.get('/api/v1/knowledge/sources',params={'session_context':context}).status_code==409
        assert http.post(path,params={'session_context':context}).status_code==409
        with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
            user=connection.execute('SELECT username FROM auth.users WHERE user_id=%s',(http.test_owner_id,)).fetchone()
        login=http.post('/api/v1/auth/login',json={'username':user['username'],'password':'changed-test-password'})
        assert login.status_code==200
        replacement=login.json()['session_id']
        assert replacement!=context
        assert http.get(path,params={'session_context':context}).status_code==409
        assert http.get(path,params={'session_context':replacement}).status_code==200
    with TestClient(app) as guest:
        assert guest.get(path,params={'session_context':replacement}).status_code==401
