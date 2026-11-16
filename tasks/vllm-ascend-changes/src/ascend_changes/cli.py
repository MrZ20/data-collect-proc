"""Check monitored source files and immediately publish each completed check."""

from collections import defaultdict
from datetime import datetime, timezone
import fcntl
from fnmatch import fnmatchcase
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

import yaml

from .github import GitHub, GitHubError

LOG = logging.getLogger(__name__)
DATA_REPOSITORY = 'MrZ20/data_repo'
CONFIG_PATH = 'configs/vllm-ascend-changes/paths.yaml'
DATA_BRANCH = 'data/vllm-ascend-changes'
STATE_DIR = Path(os.environ.get('ASCEND_CHANGES_STATE_DIR', '/state'))


def git(*args, cwd=None, retry=False):
    for attempt in range(3 if retry else 1):
        try:
            result = subprocess.run(['git', *args], cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=300, check=True)
            return result.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if not retry or attempt == 2:
                raise RuntimeError(f'Git command {args[0]} failed; retry later') from exc
            time.sleep(2 ** attempt)


def validate_config(text):
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(data.get('repository', ''))):
        raise ValueError('Invalid source repository')
    if data.get('branch') != 'main':
        raise ValueError('Only the main branch is supported')
    paths = data.get('paths')
    if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p or p.startswith('/') or '..' in p.split('/') for p in paths):
        raise ValueError('paths must contain relative file paths or patterns')
    return data


def load_config(client, state_dir):
    cache = state_dir / 'paths.yaml'
    try:
        text = client.read_text(DATA_REPOSITORY, CONFIG_PATH, 'main')
        data = validate_config(text)
        temp = cache.with_suffix('.tmp')
        temp.write_text(text, encoding='utf-8')
        temp.replace(cache)
        return data
    except (RuntimeError, ValueError, yaml.YAMLError) as exc:
        if not cache.exists():
            raise
        LOG.warning('Using cached paths configuration: %s', exc)
        return validate_config(cache.read_text(encoding='utf-8'))


def matches(path, patterns):
    parts = path.split('/')
    for pattern in patterns:
        wanted = pattern.split('/')
        if len(parts) == len(wanted) and all(fnmatchcase(a, b) for a, b in zip(parts, wanted)):
            return True
    return False


def check(config, state_dir, db, now=None):
    repo = state_dir / 'repo'
    source = f"https://github.com/{config['repository']}.git"
    identity = config['repository'] + ':' + config['branch']
    row = db.execute("SELECT value FROM metadata WHERE key='source'").fetchone()
    if row and row[0] != identity:
        raise ValueError('Source repository changed; use a new state directory')
    if not repo.exists():
        temp = state_dir / 'repo.tmp'
        for attempt in range(3):
            if temp.exists():
                shutil.rmtree(temp)
            try:
                git('clone', '--depth=1', '--single-branch', '--branch', config['branch'], '--no-tags',
                    source, str(temp))
                temp.rename(repo)
                break
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    actual = git('remote', 'get-url', 'origin', cwd=repo).decode().strip()
    if actual != source:
        raise ValueError('Local clone belongs to a different source repository')
    row = db.execute("SELECT value FROM metadata WHERE key='commit'").fetchone()
    previous = row[0] if row else git('rev-parse', 'HEAD', cwd=repo).decode().strip()
    git('update-ref', 'refs/monitor/baseline', previous, cwd=repo)
    if row is None:
        # Persist the initial baseline before pull, so a crash after pull cannot
        # silently move the starting point without recording the comparison.
        with db:
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('source',?)", (identity,))
            db.execute("INSERT INTO metadata(key,value) VALUES('commit',?)", (previous,))
    git('pull', '--ff-only', '--no-tags', 'origin', config['branch'], cwd=repo, retry=True)
    current = git('rev-parse', 'HEAD', cwd=repo).decode().strip()
    git('update-ref', 'refs/monitor/candidate', current, cwd=repo)
    output = git('diff', '--no-renames', '--name-only', '-z', previous, current, '--', cwd=repo)
    files = sorted(path for item in output.split(b'\0') if item
                   if matches(path := item.decode('utf-8', errors='surrogateescape'), config['paths']))
    timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    record = {'time': timestamp, 'commit': current, 'changed': bool(files), 'files': files}
    with db:
        db.execute("INSERT INTO metadata(key,value) VALUES('source',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (identity,))
        db.execute("INSERT INTO metadata(key,value) VALUES('commit',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (current,))
        db.execute('INSERT INTO outbox(time,record) VALUES(?,?)', (timestamp, json.dumps(record, ensure_ascii=True)))
    git('update-ref', 'refs/monitor/baseline', current, cwd=repo)
    git('update-ref', '-d', 'refs/monitor/candidate', cwd=repo)
    return record


def publish(client, db):
    pending = db.execute('SELECT time,record FROM outbox ORDER BY time').fetchall()
    if not pending:
        return 0
    client.ensure_branch(DATA_REPOSITORY, DATA_BRANCH)
    days = defaultdict(list)
    for timestamp, record in pending:
        days[timestamp[:10]].append(json.loads(record))
    for attempt in range(3):
        parent = client.branch_head(DATA_REPOSITORY, DATA_BRANCH)
        files = {}
        for day, records in days.items():
            path = f'data/{day}.jsonl'
            try:
                old = client.read_text(DATA_REPOSITORY, path, parent)
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                old = ''
            existing = {entry['time']: entry for line in old.splitlines() if line.strip() for entry in [json.loads(line)]}
            count = len(existing)
            for entry in records:
                existing.setdefault(entry['time'], entry)
            if len(existing) != count:
                files[path] = ''.join(json.dumps(entry, ensure_ascii=True) + '\n' for entry in sorted(existing.values(), key=lambda e: e['time']))
        if not files:
            break
        try:
            client.commit_files(DATA_REPOSITORY, DATA_BRANCH, parent, files, 'Append monitored file checks')
            break
        except (GitHubError, RuntimeError) as exc:
            if isinstance(exc, GitHubError) and exc.status not in (409, 422, 500, 502, 503, 504):
                raise
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    with db:
        db.executemany('DELETE FROM outbox WHERE time=?', [(timestamp,) for timestamp, _ in pending])
    return len(pending)


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        token = Path(os.environ['GITHUB_TOKEN_FILE']).read_text(encoding='utf-8').strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError('Token file must contain one token')
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / 'collector.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.info('Another check is running; skipping')
                return 0
            client = GitHub(token)
            with sqlite3.connect(STATE_DIR / 'state.sqlite3') as db:
                db.executescript('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS outbox(time TEXT PRIMARY KEY,record TEXT NOT NULL);')
                publish(client, db)
                config = load_config(client, STATE_DIR)
                record = check(config, STATE_DIR, db)
                published = publish(client, db)
                print(json.dumps({'check': record, 'published': published, 'api_requests': client.requests}, ensure_ascii=True))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error, yaml.YAMLError) as exc:
        LOG.error('%s', exc)
        return 1
