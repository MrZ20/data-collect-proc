"""Batch PR metadata and read/write the private watch files."""

import base64
import hashlib
from collections import defaultdict
import json
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE = 'vllm-project/vllm-ascend'
DATA = 'MrZ20/data_repo'
CONFIG = 'configs/github-work-watch/watch.yaml'
OUTPUT = 'data/github-work-watch/prs.json'
FIELDS = 'number repository{nameWithOwner} state isDraft headRefOid baseRefName author{login}'
WITH_DATE = FIELDS + ' commits(last:1){nodes{commit{oid committedDate}}}'
PAGE = 'pageInfo{hasNextPage endCursor}'


def key(node):
    return f"{node['repository']['nameWithOwner']}#{node['number']}"


class API:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}'})
        # All POST requests in this client are read-only GraphQL queries.
        retry = Retry(total=2, backoff_factor=1, allowed_methods={'GET', 'POST'},
                      status_forcelist=(429, 500, 502, 503, 504), respect_retry_after_header=True)
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self.calls = 0

    def request(self, method, path, **kwargs):
        self.calls += 1
        return self.session.request(method, 'https://api.github.com' + path, timeout=(10, 60), **kwargs)

    def query(self, fields):
        r = self.request('POST', '/graphql', json={'query': 'query {' + fields + '}'})
        r.raise_for_status()
        payload = r.json()
        if payload.get('errors'):
            raise RuntimeError('; '.join(error['message'] for error in payload['errors']))
        return payload['data']

    def viewer(self):
        return self.query('viewer{login}')['viewer']['login']

    def config(self):
        r = self.request('GET', f'/repos/{DATA}/contents/{CONFIG}', params={'ref': 'main'})
        r.raise_for_status()
        return base64.b64decode(r.json()['content']).decode()

    def recent(self, cutoff):
        after = 'null'
        rows = {}
        while True:
            page = self.query('repository(owner:"vllm-project",name:"vllm-ascend"){'
                              'pullRequests(states:OPEN,first:100,orderBy:{field:UPDATED_AT,direction:DESC},'
                              f'after:{after}){{nodes{{updatedAt {WITH_DATE}}} {PAGE}}}}}')['repository']['pullRequests']
            for node in page['nodes']:
                if node['updatedAt'] < cutoff:
                    return rows
                rows[key(node)] = node
            if not page['pageInfo']['hasNextPage']:
                return rows
            after = json.dumps(page['pageInfo']['endCursor'])

    def lookup(self, keys, users, cutoff, existing):
        """Combine targeted mention/reviewer searches and known PRs in the first batch."""
        searches = [f'is:pr is:open updated:>={cutoff} {qualifier}:{user}'
                    for user in users for qualifier in ('mentions', 'user-review-requested')]
        pending = {i: 'null' for i in range(len(searches))}
        keys = sorted(keys)
        rows = {}
        personal = set()
        while pending or keys:
            fields = []
            for i, after in pending.items():
                fields.append(f's{i}:search(type:ISSUE,query:{json.dumps(searches[i])},first:100,after:{after})'
                              f'{{issueCount nodes{{... on PullRequest{{{WITH_DATE}}}}} {PAGE}}}')
            batch, keys = keys[:50], keys[50:]
            groups = defaultdict(list)
            for item in batch:
                repo, number = item.split('#')
                selection = FIELDS if item in existing else WITH_DATE
                groups[repo].append(f'p{int(number)}:pullRequest(number:{int(number)}){{{selection}}}')
            for i, (repo, queries) in enumerate(groups.items()):
                owner, name = repo.split('/')
                prs = ' '.join(queries)
                fields.append(f'r{i}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{{prs}}}')
            data = self.query(' '.join(fields))
            for i in list(pending):
                page = data[f's{i}']
                if page['issueCount'] > 1000:
                    raise RuntimeError(f'Search exceeds GitHub result limit: {searches[i]}')
                for node in page['nodes']:
                    rows[key(node)] = node
                    personal.add(key(node))
                if page['pageInfo']['hasNextPage']:
                    pending[i] = json.dumps(page['pageInfo']['endCursor'])
                else:
                    del pending[i]
            for i in range(len(groups)):
                for node in data[f'r{i}'].values():
                    rows[key(node)] = node
        return rows, personal

    def changed_files(self, repo, number):
        names = []
        for page in range(1, 32):
            r = self.request('GET', f'/repos/{repo}/pulls/{number}/files',
                             params={'per_page': 100, 'page': page})
            r.raise_for_status()
            items = r.json()
            for item in items:
                names.append(item['filename'])
                if item.get('previous_filename'):
                    names.append(item['previous_filename'])
            if len(items) < 100:
                return names
        raise RuntimeError(f'PR {repo}#{number} has too many changed files')

    def commit_date(self, repo, sha):
        r = self.request('GET', f'/repos/{repo}/git/commits/{sha}')
        r.raise_for_status()
        return r.json()['committer']['date']

    def publish(self, text):
        path = f'/repos/{DATA}/contents/{OUTPUT}'
        for attempt in range(3):
            args = {'branch': 'main', 'message': 'Update watched pull requests',
                    'content': base64.b64encode(text.encode()).decode()}
            old = self.request('GET', path, params={'ref': 'main'})
            if old.status_code != 404:
                old.raise_for_status()
                blob = old.json()
                # Compare blob IDs even when the snapshot exceeds the Contents API inline limit.
                raw = text.encode()
                oid = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
                if blob['sha'] == oid:
                    return
                args['sha'] = blob['sha']
            r = self.request('PUT', path, json=args)
            if r.status_code not in (409, 422) or attempt == 2:
                r.raise_for_status()
                return
            time.sleep(2 ** attempt)
