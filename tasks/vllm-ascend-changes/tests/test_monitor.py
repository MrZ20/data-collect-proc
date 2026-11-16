from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from ascend_changes import cli
from ascend_changes.github import GitHubError


def command(*args, cwd):
    return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.DEVNULL).decode().strip()


def test_checks_git_changes_and_preserves_baseline_on_failure(tmp_path, monkeypatch):
    remote = tmp_path / 'upstream'
    remote.mkdir()
    command('init', '-b', 'main', cwd=remote)
    command('config', 'user.email', 'test@example.com', cwd=remote)
    command('config', 'user.name', 'Test', cwd=remote)
    target = remote / '.github/workflows/a.yaml'
    target.parent.mkdir(parents=True)
    target.write_text('first')
    (target.parent / 'nested').mkdir()
    (target.parent / 'nested/ignored.yaml').write_text('first')
    command('add', '.', cwd=remote)
    command('commit', '-m', 'first', cwd=remote)
    state = tmp_path / 'state'
    state.mkdir()
    config = {'repository': 'test/source', 'branch': 'main', 'paths': ['.github/workflows/*.yaml']}
    original_git = cli.git
    source = 'https://github.com/test/source.git'

    def local_git(*args, **kwargs):
        args = tuple('file://' + str(remote) if arg == source else arg for arg in args)
        result = original_git(*args, **kwargs)
        if args[:2] == ('remote', 'get-url'):
            return (source + '\n').encode()
        return result

    monkeypatch.setattr(cli, 'git', local_git)
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE outbox(time TEXT PRIMARY KEY,record TEXT);')
        t1 = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        first = cli.check(config, state, db, now=t1)
        assert not first['changed']
        target.write_text('second')
        (target.parent / 'nested/ignored.yaml').write_text('second')
        command('add', '.', cwd=remote)
        command('commit', '-m', 'second', cwd=remote)
        second = cli.check(config, state, db, now=t1.replace(minute=15))
        assert second['changed'] and second['files'] == ['.github/workflows/a.yaml']
        target.unlink()
        command('add', '.', cwd=remote)
        command('commit', '-m', 'delete', cwd=remote)
        third = cli.check(config, state, db, now=t1.replace(minute=30))
        assert third['files'] == ['.github/workflows/a.yaml']
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 3
        before = db.execute("SELECT value FROM metadata WHERE key='commit'").fetchone()
        monkeypatch.setattr(cli, 'git', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('network')))
        with pytest.raises(RuntimeError):
            cli.check(config, state, db, now=t1.replace(minute=45))
        assert db.execute("SELECT value FROM metadata WHERE key='commit'").fetchone() == before


def test_publish_deduplicates_after_lost_response(monkeypatch):
    class Writer:
        def __init__(self):
            self.data = ''
            self.writes = 0
        def ensure_branch(self, repo, branch):
            assert (repo, branch) == ('MrZ20/data_repo', 'main')
        def branch_head(self, repo, branch):
            assert (repo, branch) == ('MrZ20/data_repo', 'main')
            return 'head'
        def read_text(self, *args):
            assert args[1] == 'data/vllm-ascend-changes/2026-01-01.jsonl'
            if not self.data:
                raise GitHubError(404, 'missing')
            return self.data
        def commit_files(self, repo, branch, parent, files, message):
            assert branch == 'main'
            assert set(files) == {'data/vllm-ascend-changes/2026-01-01.jsonl'}
            self.data = next(iter(files.values()))
            self.writes += 1
            raise RuntimeError('response lost')
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    record = {'time': '2026-01-01T00:00:00.000000Z', 'commit': 'abc', 'changed': False, 'files': []}
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE outbox(time TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO outbox VALUES(?,?)', (record['time'], json.dumps(record)))
        writer = Writer()
        assert cli.publish(writer, db) == 1
        assert writer.writes == 1 and json.loads(writer.data) == record
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
