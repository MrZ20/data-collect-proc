from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor
from watch_api import SOURCE

START = datetime(2026, 11, 19, tzinfo=timezone.utc)
ITEM = SOURCE + '#12'


class Upstream:
    def __init__(self):
        self.sha = 'a'
        self.state = 'OPEN'
        self.personal = set()
        self.queries = []

    def node(self):
        return {'headRefOid': self.sha, 'number': 12, 'state': self.state, 'isDraft': False,
                'baseRefName': 'main', 'author': {'login': 'contributor'},
                'commits': {'nodes': [{'commit': {'oid': self.sha, 'committedDate': '2026-11-18T22:00:00Z'}}]}}

    def recent(self, cutoff):
        assert cutoff == '2026-11-05T00:00:00Z'
        return {ITEM: self.node()}

    def lookup(self, keys, users, cutoff, existing):
        self.queries.append(set(keys))
        return ({ITEM: self.node()} if ITEM in keys or ITEM in self.personal else {}), self.personal


def test_head_changes_membership_cooldown_and_terminal(monkeypatch, tmp_path):
    api, state, checks = Upstream(), {}, []
    monkeypatch.setattr(monitor, 'head_refs', lambda: {'12': api.sha, '1': 'old'})

    def matches(_, numbers, paths, excludes):
        checks.append((paths, numbers))
        return {'12': (api.sha, api.sha in ('b', 'c'))}

    monkeypatch.setattr(monitor, 'path_matches', matches)

    def poll(minutes):
        monitor.collect(api, tmp_path, state, [], ['.github'], [], 'MrZ20', START + timedelta(minutes=minutes))
        return json.loads(monitor.snapshot(state['prs']))

    assert not poll(0)
    assert not poll(15)
    assert api.queries[-1] == set()
    assert len(checks) == 1
    api.sha = 'b'
    assert poll(30)[0]['code_updated_at'] == '2026-11-19T00:30:00Z'
    api.sha = 'c'
    poll(45)
    api.sha = 'd'
    assert poll(60)[0]['code_updated_at'] == '2026-11-19T01:00:00Z'
    assert len(checks) == 3
    assert not poll(285)
    api.personal = {ITEM}
    assert poll(300)[0]['status'] == 'open'
    api.personal.clear()
    api.state = 'MERGED'
    assert poll(315)[0]['status'] == 'merged'
    assert poll(330)[0]['status'] == 'merged'
    assert api.queries[-1] == set()


def test_remote_path_edit_and_mention_withdrawal(monkeypatch, tmp_path):
    api, state = Upstream(), {}
    monkeypatch.setattr(monitor, 'head_refs', lambda: {'12': api.sha})
    monkeypatch.setattr(monitor, 'path_matches', lambda _, ns, paths, excludes: {'12': (api.sha, 'docs' in paths)})

    def poll(paths):
        monitor.collect(api, tmp_path, state, [], paths, [], 'MrZ20', START)
        return json.loads(monitor.snapshot(state['prs']))

    assert not poll(['.github'])
    assert poll(['docs'])[0]['status'] == 'open'
    api.personal = {ITEM}
    assert poll([])[0]['status'] == 'open'
    api.personal.clear()
    assert not poll([])


def test_git_sees_push_before_pr_metadata(monkeypatch, tmp_path):
    api, state = Upstream(), {}
    refs = {'12': 'a'}
    monkeypatch.setattr(monitor, 'head_refs', lambda: refs.copy())
    monkeypatch.setattr(monitor, 'path_matches', lambda _, ns, paths, excludes: {'12': (api.sha, api.sha == 'b')})
    for minute, git_sha, api_sha in [(0, 'a', 'a'), (15, 'b', 'a'), (30, 'b', 'b')]:
        refs['12'], api.sha = git_sha, api_sha
        monitor.collect(api, tmp_path, state, [], ['.github'], [], 'MrZ20', START + timedelta(minutes=minute))
    assert json.loads(monitor.snapshot(state['prs']))[0]['code_updated_at'] == '2026-11-19T00:30:00Z'


def test_path_exclusions_and_own_author(monkeypatch, tmp_path):
    assert monitor.included('.github/workflows/a.yml', ['.github'], ['.github/workflows/configs'])
    assert not monitor.included('.github/workflows/configs/test.yml', ['.github'], ['.github/workflows/configs'])
    assert monitor.included('.github/workflows/configs-other/test.yml', ['.github'], ['.github/workflows/configs'])
    api, state = Upstream(), {}
    node = api.node()
    node['author']['login'] = 'mrz20'
    monkeypatch.setattr(api, 'recent', lambda _: {ITEM: node})
    monkeypatch.setattr(monitor, 'head_refs', lambda: {'12': api.sha})
    monitor.collect(api, tmp_path, state, ['MrZ20'], ['.github'], [], 'MrZ20', START)
    assert json.loads(monitor.snapshot(state['prs'])) == []


def test_compare_target_branch_and_excluded_directory(monkeypatch, tmp_path):
    import subprocess
    import watch_git

    remote = tmp_path / 'remote.git'
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    subprocess.run(['git', 'init', '-b', 'main', str(work)], check=True, capture_output=True)

    def git(*args):
        return subprocess.run(['git', *args], cwd=work, check=True, capture_output=True)

    git('config', 'user.email', 'test@example.com')
    git('config', 'user.name', 'Test')
    (work / '.github' / 'workflows' / 'configs').mkdir(parents=True)
    (work / '.github' / 'workflows' / 'configs' / 'main.yml').write_text('main')
    git('add', '.')
    git('commit', '-m', 'main')
    git('remote', 'add', 'origin', str(remote))
    git('push', 'origin', 'main')
    git('checkout', '-b', 'release')
    (work / '.github' / 'workflows' / 'configs' / 'main.yml').write_text('release')
    git('commit', '-am', 'release')
    git('push', 'origin', 'release')
    (work / 'code.py').write_text('code')
    git('add', '.')
    git('commit', '-m', 'code')
    git('push', 'origin', 'HEAD:refs/pull/1/head')
    (work / '.github' / 'workflows' / 'configs' / 'main.yml').write_text('config')
    git('commit', '-am', 'config')
    git('push', 'origin', 'HEAD:refs/pull/2/head')
    (work / '.github' / 'workflows' / 'run.yml').write_text('workflow')
    git('add', '.')
    git('commit', '-m', 'workflow')
    git('push', 'origin', 'HEAD:refs/pull/3/head')
    monkeypatch.setattr(watch_git, 'REMOTE', str(remote))
    result = watch_git.path_matches(tmp_path / 'state', {'1': 'release', '2': 'release', '3': 'release'},
                                    ['.github'], ['.github/workflows/configs'])
    assert [result[n][1] for n in ('1', '2', '3')] == [False, False, True]
