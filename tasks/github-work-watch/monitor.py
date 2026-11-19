"""Poll watched pull requests and publish the current list."""

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

import yaml

from watch_api import API, SOURCE
from watch_git import head_refs, path_matches

STATE = Path(os.environ.get('WATCH_STATE_DIR', '/state'))


def stamp(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


def write(path, text):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


def config(text):
    values = yaml.safe_load(text)
    users, paths = values['users'], values['paths']
    excludes = values.get('exclude_paths', [])
    if not isinstance(users, list) or not all(isinstance(u, str) and re.fullmatch(r'[a-zA-Z0-9-]+', u) for u in users):
        raise ValueError('users must be a list of GitHub logins')
    for label, entries in (('paths', paths), ('exclude_paths', excludes)):
        if not isinstance(entries, list) or not all(
        isinstance(p, str) and all(part not in ('', '.', '..') for part in p.rstrip('/').split('/'))
            for p in entries
        ):
            raise ValueError(f'{label} must contain repository-relative names')
    return sorted(set(users)), sorted({p.rstrip('/') for p in paths}), sorted({p.rstrip('/') for p in excludes})


def active(row):
    return row.get('personal', False) or row.get('matched', False)


def included(path, paths, excludes):
    def matches(prefix):
        return path == prefix or path.startswith(prefix + '/')
    return any(map(matches, paths)) and not any(map(matches, excludes))


def terminal(row):
    return row.get('status') in ('merged', 'closed')


def collect(api, directory, state, users, paths, excludes, viewer, now):
    refs = head_refs()
    rows = state.setdefault('prs', {})
    old_refs = state.get('refs', {})
    changed = {f'{SOURCE}#{n}' for n, sha in refs.items() if n not in old_refs or old_refs[n] != sha}
    if 'cutoff' not in state:
        state['cutoff'] = stamp(now - timedelta(days=14))
        known = api.recent(state['cutoff'])
        changed.clear()
    else:
        known = {}
    path_edit = (paths != state.get('paths') or excludes != state.get('exclude_paths')
                 or state.get('policy_version') != 2)
    candidates = {item for item, row in rows.items()
                  if (not terminal(row) and (active(row) or row.get('pending') or
                      (path_edit and item.startswith(SOURCE + '#')))) or
                  (path_edit and terminal(row) and active(row))}
    candidates.update(item for item in changed if not terminal(rows.get(item, {})))
    fetched, personal = api.lookup(candidates, users, state['cutoff'], rows)
    known.update(fetched)
    due = {}
    for item, node in known.items():
        author = (node.get('author') or {}).get('login', '')
        if author.lower() == viewer.lower():
            rows.pop(item, None)
            continue
        row = rows.setdefault(item, {})
        was_active = active(row)
        sha = node['headRefOid']
        number = str(node['number'])
        local = item.startswith(SOURCE + '#')
        if local and number in refs and sha != refs[number]:
            # GraphQL and Git can observe opposite sides of a push. Check again next poll.
            refs[number] = sha
        if row.get('sha') != sha or not row.get('code_updated_at'):
            previous = row.get('sha') or (old_refs.get(number) if local else None)
            if previous and previous != sha:
                row['code_updated_at'] = stamp(now)
            else:
                commits = node.get('commits', {}).get('nodes', [])
                date = next((c['commit']['committedDate'] for c in commits if c['commit']['oid'] == sha), None)
                row['code_updated_at'] = date or api.commit_date(node['repository']['nameWithOwner'], sha)
            row['sha'] = sha
        row['status'] = 'draft' if node['state'] == 'OPEN' and node['isDraft'] else node['state'].lower()
        if terminal(row):
            if path_edit and local and was_active:
                files = api.changed_files(node['repository']['nameWithOwner'], number)
                row['matched'] = any(included(name, paths, excludes) for name in files)
            if not active(row):
                del rows[item]
            continue
        row['personal'] = item in personal
        if not local:
            if not row['personal']:
                del rows[item]
            continue
        base = node.get('baseRefName')
        if path_edit or row.get('base') != base:
            row.update(hits=0, checked_sha=None)
        row['base'] = base
        if not paths:
            row['matched'] = False
        row['pending'] = bool(paths and not row['personal'] and row.get('checked_sha') != sha)
        if row['pending'] and (row.get('hits', 0) < 2 or now.timestamp() >= row['checked_at'] + 4 * 3600):
            due[number] = base

    if due:
        matches = path_matches(directory, due, paths, excludes)
        for number, (sha, matched) in matches.items():
            row = rows[f'{SOURCE}#{number}']
            if row['sha'] != sha:
                raise RuntimeError(f'PR {number} changed during path comparison; retry later')
            row.update(checked_sha=sha, matched=matched, checked_at=now.timestamp(), pending=False,
                       hits=row.get('hits', 0) + 1 if matched else 0)

    state.update(refs=refs, paths=paths, exclude_paths=excludes, policy_version=2)
    print(f'PR refs={len(refs)} changed={len(changed)} metadata={len(known)} path_checks={len(due)}', flush=True)


def snapshot(rows):
    records = []
    for item, row in rows.items():
        if active(row):
            repo, number = item.split('#')
            records.append((int(number), repo, {'pr': f'https://github.com/{repo}/pull/{number}',
                            'code_updated_at': row['code_updated_at'], 'status': row['status']}))
    return json.dumps([r for _, _, r in sorted(records, key=lambda v: (-v[0], v[1]))],
                      ensure_ascii=False, indent=2) + '\n'


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'watch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        api = API(Path(os.environ['GITHUB_TOKEN_FILE']).read_text().strip())
        text = api.config()
        users, paths, excludes = config(text)
        viewer = api.viewer()
        write(STATE / 'watch.yaml', text)
        path = STATE / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        collect(api, STATE, state, users, paths, excludes, viewer, datetime.now(timezone.utc))
        output = snapshot(state['prs'])
        digest = hashlib.sha256(output.encode()).hexdigest()
        write(path, json.dumps(state))
        dirty = digest != state.get('published')
        if dirty:
            api.publish(output)
            state['published'] = digest
            write(path, json.dumps(state))
        print(f'watched={len(json.loads(output))} snapshot_changed={dirty} API_calls={api.calls}', flush=True)


if __name__ == '__main__':
    main()
