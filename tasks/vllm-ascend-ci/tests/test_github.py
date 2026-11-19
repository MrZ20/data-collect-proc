import base64

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from ascend_ci_monitor.github import GitHub, GitHubError


class Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = CaseInsensitiveDict(headers or {})
        self.content = b'x' if payload is not None else b''

    def json(self):
        return self.payload


class Session:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client_with(monkeypatch, *outcomes):
    session = Session(*outcomes)
    monkeypatch.setattr('ascend_ci_monitor.github.requests.Session', lambda: session)
    client = GitHub('secret-token')
    return client, session


def test_session_reused_and_get_retries_transient_network_and_server(monkeypatch):
    client, session = client_with(monkeypatch, requests.ConnectionError('failure'),
                                  Response(503, {'message': 'busy'}),
                                  Response(200, {'ok': 1}, {'X-RateLimit-Remaining': '40'}),
                                  Response(200, {'ok': 2}))
    sleeps = []
    monkeypatch.setattr('ascend_ci_monitor.github.time.sleep', sleeps.append)
    monkeypatch.setattr('ascend_ci_monitor.github.random.uniform', lambda a, b: 0)
    assert client.get('/first') == {'ok': 1}
    assert client.remaining == '40'
    assert client.get('/second') == {'ok': 2}
    assert len(session.calls) == client.requests == 4
    assert [call[1] for call in session.calls] == ['https://api.github.com/first'] * 3 + ['https://api.github.com/second']
    assert sleeps == [1, 2]


@pytest.mark.parametrize('status', [401, 403, 404])
def test_non_rate_errors_do_not_retry_and_do_not_leak_token(monkeypatch, status):
    client, session = client_with(monkeypatch, Response(status, {'message': 'bad secret-token'}))
    with pytest.raises(GitHubError) as error:
        client.get('/failure')
    assert error.value.status == status
    assert 'secret-token' not in str(error.value)
    assert len(session.calls) == 1


def test_rate_limit_respects_retry_after_and_long_reset_fails_fast(monkeypatch):
    monkeypatch.setattr('ascend_ci_monitor.github.time.time', lambda: 1000)
    waits = []
    monkeypatch.setattr('ascend_ci_monitor.github.time.sleep', waits.append)
    client, session = client_with(monkeypatch, Response(403, {'message': 'secondary rate limit'},
                                                        {'Retry-After': '12'}), Response(200, {'ok': True}))
    assert client.get('/limited') == {'ok': True}
    assert waits == [12]
    assert len(session.calls) == 2
    client, session = client_with(monkeypatch, Response(429, {'message': 'limited'},
                                                        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1400'}))
    with pytest.raises(GitHubError, match='retry later'):
        client.get('/limited')
    assert len(session.calls) == 1 and waits == [12]


def test_mutation_network_error_is_not_retried_or_leaked(monkeypatch):
    client, session = client_with(monkeypatch, requests.Timeout('secret-token in timeout'))
    with pytest.raises(RuntimeError) as error:
        client.request('POST', '/write', data={'x': 1})
    assert 'secret-token' not in str(error.value)
    assert len(session.calls) == 1


def test_large_content_uses_blob_fallback(monkeypatch):
    encoded = base64.b64encode('policy text'.encode()).decode()
    client, session = client_with(monkeypatch, Response(200, {'encoding': 'none', 'sha': 'abc'}),
                                  Response(200, {'encoding': 'base64', 'content': encoded}))
    assert client.read_text('org/repo', 'file.yaml', 'main') == 'policy text'
    assert session.calls[1][1].endswith('/repos/org/repo/git/blobs/abc')


def test_conditional_cache_revalidates_and_replaces_changed_data(monkeypatch, tmp_path):
    from ascend_ci_monitor.state import State

    client, session = client_with(monkeypatch,
        Response(200, {'items': [1]}, {'ETag': '"first"'}),
        Response(304, headers={'X-RateLimit-Remaining': '20'}),
        Response(200, {'items': [2]}, {'ETag': '"second"'}),
        Response(304),
    )
    with State(tmp_path / 'cache.sqlite') as state:
        params = {'per_page': 100, 'page': 1}
        assert client.get_cached('/runs', params, state, '2026-01-02') == {'items': [1]}
        assert client.get_cached('/runs', {'page': 1, 'per_page': 100}, state, '2026-01-02') == {'items': [1]}
        assert client.get_cached('/runs', params, state, '2026-01-02') == {'items': [2]}
        assert client.get_cached('/runs', params, state, '2026-01-02') == {'items': [2]}
    headers = [call[2]['headers'] for call in session.calls]
    assert 'If-None-Match' not in headers[0]
    assert [item['If-None-Match'] for item in headers[1:]] == ['"first"', '"first"', '"second"']
    assert client.requests == 4 and client.cache_hits == 2


def test_no_etag_clears_old_cache_and_failed_read_keeps_it(monkeypatch, tmp_path):
    from ascend_ci_monitor.state import State

    client, session = client_with(monkeypatch,
        Response(200, {'items': [1]}, {'ETag': '"first"'}),
        Response(404, {'message': 'missing'}),
        Response(200, {'items': [2]}),
        Response(200, {'items': [3]}),
    )
    with State(tmp_path / 'cache.sqlite') as state:
        assert client.get_cached('/runs', {}, state, '2026-01-02') == {'items': [1]}
        with pytest.raises(GitHubError):
            client.get_cached('/runs', {}, state, '2026-01-02')
        assert client.get_cached('/runs', {}, state, '2026-01-02') == {'items': [2]}
        assert client.get_cached('/runs', {}, state, '2026-01-02') == {'items': [3]}
    headers = [call[2]['headers'] for call in session.calls]
    assert headers[1]['If-None-Match'] == headers[2]['If-None-Match'] == '"first"'
    assert 'If-None-Match' not in headers[3]


def test_job_pagination_rejects_missing_unique_items(monkeypatch):
    client = GitHub('test-token')
    first = [{'id': n} for n in range(100)]
    pages = iter([{'total_count': 101, 'jobs': first}, {'total_count': 101, 'jobs': first[:1]}])
    monkeypatch.setattr(client, 'get', lambda path, params: next(pages))
    with pytest.raises(RuntimeError, match='pagination changed'):
        client.paginate('/actions/runs/1/jobs', 'jobs')
