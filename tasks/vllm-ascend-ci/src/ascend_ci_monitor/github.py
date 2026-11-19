"""Small GitHub REST client and atomic text-file publication."""

import base64
from email.utils import parsedate_to_datetime
import json
import random
import time
from urllib.parse import quote

import requests


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"GitHub HTTP {status}: {message}")


class GitHub:
    def __init__(self, token: str):
        self.token = token.strip()
        self.requests = 0
        self.cache_hits = 0
        self.remaining = None
        self.session = requests.Session()

    def request(self, method: str, path: str, data=None, params=None):
        return self._request(method, path, data=data, params=params)

    def _request(self, method: str, path: str, data=None, params=None, cache_entry=None):
        url = 'https://api.github.com' + path
        headers = {
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10',
            'User-Agent': 'ascend-ci-collector/0.1',
        }
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if cache_entry is not None and cache_entry[3] is not None:
            headers['If-None-Match'] = cache_entry[3][0]
        method = method.upper()
        for attempt in range(3):
            self.requests += 1
            try:
                response = self.session.request(method, url, headers=headers, json=data,
                                                params=params, timeout=60)
            except requests.exceptions.RequestException as exc:
                if (method == 'GET' and attempt < 2 and
                        isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))):
                    time.sleep(2 ** attempt + random.uniform(0, 0.25))
                    continue
                raise RuntimeError(f'GitHub network request failed for {method} {path}; retry later') from None

            self.remaining = response.headers.get('X-RateLimit-Remaining')
            if response.status_code == 304 and cache_entry is not None and cache_entry[3] is not None:
                self.cache_hits += 1
                return cache_entry[3][1]
            if 200 <= response.status_code < 300:
                payload = response.json() if response.content else None
                if response.status_code == 200 and cache_entry is not None:
                    cache, key, window_end, _ = cache_entry
                    etag = response.headers.get('ETag')
                    if etag:
                        cache.put_http_cache(key, etag, payload, window_end)
                    else:
                        cache.delete_http_cache(key)
                return payload
            try:
                message = response.json().get('message', 'Request failed')
            except (ValueError, AttributeError):
                message = 'Request failed'
            message = str(message)
            if self.token:
                message = message.replace(self.token, '[redacted]')
            retry_after = response.headers.get('Retry-After')
            rate_limited = (response.status_code == 429 or
                            (response.status_code == 403 and
                             (self.remaining == '0' or retry_after is not None or
                              'secondary rate limit' in message.lower())))
            retryable = rate_limited or response.status_code in (500, 502, 503, 504)
            if method == 'GET' and retryable and attempt < 2:
                wait = 2 ** attempt + random.uniform(0, 0.25)
                if rate_limited:
                    waits = []
                    if retry_after is not None:
                        try:
                            waits.append(float(retry_after))
                        except ValueError:
                            try:
                                waits.append(parsedate_to_datetime(retry_after).timestamp() - time.time())
                            except (TypeError, ValueError, OverflowError):
                                pass
                    if self.remaining == '0':
                        try:
                            waits.append(float(response.headers['X-RateLimit-Reset']) - time.time())
                        except (KeyError, ValueError):
                            pass
                    wait = max([0, *waits]) if waits else 60
                    if wait > 60:
                        raise GitHubError(response.status_code, 'Rate limited; retry later') from None
                time.sleep(wait)
                continue
            if rate_limited:
                message += '; retry later'
            raise GitHubError(response.status_code, message) from None

    def get(self, path: str, params=None):
        return self.request('GET', path, params=params)

    def get_cached(self, path: str, params, cache, window_end: str):
        key = json.dumps([path, params or {}], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        entry = cache.get_http_cache(key)
        return self._request('GET', path, params=params, cache_entry=(cache, key, window_end, entry))

    def paginate(self, path: str, key: str, params=None):
        params = dict(params or {}, per_page=100)
        first = self.get(path, dict(params, page=1))
        total = first['total_count']
        items = {item['id']: item for item in first[key]}
        consistent = True
        for page in range(2, (total + 99) // 100 + 1):
            data = self.get(path, dict(params, page=page))
            consistent &= data['total_count'] == total
            items.update((item['id'], item) for item in data[key])
        if not consistent or len(items) != total:
            raise RuntimeError(f'GitHub pagination changed for {path}; retry later')
        return items.values()

    def read_text(self, repository: str, path: str, ref: str) -> str:
        data = self.get(f'/repos/{repository}/contents/{quote(path, safe="/")}', {'ref': ref})
        if data.get('encoding') == 'none' and data.get('sha'):
            data = self.get(f'/repos/{repository}/git/blobs/{data["sha"]}')
        if data.get('encoding') != 'base64':
            raise ValueError(f'{path}: expected a small UTF-8 text file')
        return base64.b64decode(data['content']).decode('utf-8')

    def branch_head(self, repository: str, branch: str) -> str:
        return self.get(f'/repos/{repository}/git/ref/heads/{quote(branch, safe="/")}')['object']['sha']

    def ensure_branch(self, repository: str, branch: str, base: str = 'main') -> None:
        try:
            self.branch_head(repository, branch)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            sha = self.branch_head(repository, base)
            self.request('POST', f'/repos/{repository}/git/refs', {'ref': f'refs/heads/{branch}', 'sha': sha})

    def commit_files(self, repository: str, branch: str, parent: str,
                     files: dict[str, str], message: str) -> str:
        """One commit for a batch; a concurrent branch update is never forced."""
        commit = self.get(f'/repos/{repository}/git/commits/{parent}')
        tree = self.request('POST', f'/repos/{repository}/git/trees', {
            'base_tree': commit['tree']['sha'],
            'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'content': content}
                     for path, content in files.items()],
        })
        new = self.request('POST', f'/repos/{repository}/git/commits', {
            'message': message, 'tree': tree['sha'], 'parents': [parent],
        })
        self.request('PATCH', f'/repos/{repository}/git/refs/heads/{quote(branch, safe="/")}',
                     {'sha': new['sha'], 'force': False})
        return new['sha']
