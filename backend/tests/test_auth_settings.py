from types import SimpleNamespace

import pytest

from app.auth.settings import AuthSettings


PUBLIC = 'http://114.214.255.154:9001'


def settings(origins=(PUBLIC,), environment='dev'):
    return SimpleNamespace(allowed_origins_list=origins, app_postgres_dsn='unused', gpu_broker_environment=environment)


@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch):
    for key in ('AUTH_COOKIE_SECURE', 'AUTH_COOKIE_NAME', 'AUTH_DEV_HTTP_ORIGINS'):
        monkeypatch.delenv(key, raising=False)


def test_secure_cookies_remain_default():
    assert AuthSettings.from_settings(settings()).cookie_secure


def test_remote_http_requires_explicit_development_origin(monkeypatch):
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    with pytest.raises(ValueError, match='loopback'):
        AuthSettings.from_settings(settings())
    monkeypatch.setenv('AUTH_DEV_HTTP_ORIGINS', PUBLIC)
    configured = AuthSettings.from_settings(settings(('http://localhost:9001', PUBLIC)))
    assert not configured.cookie_secure
    assert configured.cookie_name == 'nexpoly_dev_session'
    assert configured.allowed_origins == ('http://localhost:9001', PUBLIC)


@pytest.mark.parametrize('environment,secure,cookie', [
    ('prod', 'false', 'nexpoly_dev_session'),
    ('dev', 'true', 'nexpoly_dev_session'),
    ('dev', 'false', 'nexpoly_session'),
])
def test_exception_cannot_apply_to_production_or_secure_cookie(monkeypatch, environment, secure, cookie):
    monkeypatch.setenv('AUTH_COOKIE_SECURE', secure)
    monkeypatch.setenv('AUTH_COOKIE_NAME', cookie)
    monkeypatch.setenv('AUTH_DEV_HTTP_ORIGINS', PUBLIC)
    with pytest.raises(ValueError, match='development runtime'):
        AuthSettings.from_settings(settings(environment=environment))


@pytest.mark.parametrize('origin', [
    'http://example.test:9000', 'https://example.test:9001', 'http://*.example.test:9001',
    'http://example.test:9001/path', 'http://example.test:9001/', 'http://example.test:9001?query',
    'http://example.test:9001#fragment', 'http://user:secret@example.test:9001', 'http://example.test',
])
def test_exception_rejects_non_origin_or_non_development_address(monkeypatch, origin):
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    monkeypatch.setenv('AUTH_DEV_HTTP_ORIGINS', origin)
    with pytest.raises(ValueError, match='exact allowed HTTP origins'):
        AuthSettings.from_settings(settings((origin,)))


def test_exception_does_not_expand_allowed_origins(monkeypatch):
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    monkeypatch.setenv('AUTH_DEV_HTTP_ORIGINS', PUBLIC)
    with pytest.raises(ValueError, match='exact allowed HTTP origins'):
        AuthSettings.from_settings(settings(('http://localhost:9001',)))


def test_one_explicit_exception_does_not_allow_other_remote_host(monkeypatch):
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    monkeypatch.setenv('AUTH_DEV_HTTP_ORIGINS', PUBLIC)
    with pytest.raises(ValueError, match='loopback'):
        AuthSettings.from_settings(settings((PUBLIC, 'http://other.test:9001')))
