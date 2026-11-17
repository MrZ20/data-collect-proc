"""Publish daily run-attempt groups, deduplicating their jobs by URL."""
from collections import defaultdict
import json
import time

from .github import GitHubError


def merge_records(records, additions, updates=()):
    groups = {(r['run_id'], r['run_attempt']): r for r in records}
    for record in additions:
        key = record['run_id'], record['run_attempt']
        group = groups.setdefault(key, {
            'run_id': key[0], 'run_attempt': key[1], 'workflow': record['workflow'],
            'base_repo': record['base_repo'], 'head_sha': record.get('head_sha'),
            'status': record.get('run_status'), 'conclusion': record.get('run_conclusion'), 'jobs': [],
        })
        job = {k: v for k, v in record.items() if k not in
               ('run_id', 'run_attempt', 'workflow', 'base_repo', 'head_sha', 'run_status', 'run_conclusion')}
        job['conclusion'] = job.pop('job_conclusion', None)
        if not any(existing['url'] == job['url'] for existing in group['jobs']):
            group['jobs'].append(job)
    for run_id, attempt, _, status, conclusion in updates:
        group = groups.get((run_id, attempt))
        if group:
            group['status'], group['conclusion'] = status, conclusion
    for group in groups.values():
        group['jobs'].sort(key=lambda job: (job['time'], job['url']))
    return sorted(groups.values(), key=lambda group: (group['jobs'][0]['time'], group['run_id'], group['run_attempt']))


def render_report(groups):
    def cell(value):
        return str(value or '').replace('|', '\\|').replace('\n', ' ')
    lines = ['| UTC 时间 | Workflow | Run / attempt | Job | 失败步骤 | Runner | 链接 |',
             '|---|---|---|---|---|---|---|']
    for group in groups:
        for job in group['jobs']:
            values = [job['time'], group['workflow'], f"{group['run_id']} / {group['run_attempt']}",
                      job['job'], '; '.join(job['failed_steps']), job['runner'], f"[Actions]({job['url']})"]
            lines.append('| ' + ' | '.join(cell(value) for value in values) + ' |')
    return '\n'.join(lines) + '\n'


def publish(client, repository, branch, state):
    pending, updates = state.pending(), state.pending_groups()
    if not pending and not updates:
        return 0
    client.ensure_branch(repository, branch)
    days = defaultdict(list)
    changes = defaultdict(list)
    for _, record in pending:
        days[record['time'][:10]].append(record)
    for update in updates:
        changes[update[2]].append(update)
    for attempt in range(3):
        parent = client.branch_head(repository, branch)
        files = {}
        for day in days.keys() | changes.keys():
            path = f'data/vllm-ascend-ci/{day}.jsonl'
            try:
                old = client.read_text(repository, path, parent)
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                old = ''
            records = [json.loads(line) for line in old.splitlines() if line.strip()]
            ordered = merge_records(records, days[day], changes[day])
            content = ''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in ordered)
            if content != old:
                files[path] = content
                files[f'reports/vllm-ascend-ci/{day}.md'] = render_report(ordered)
        if not files:
            break
        try:
            client.commit_files(repository, branch, parent, files, 'Update CI failure candidates')
            break
        except GitHubError as exc:
            if exc.status not in (409, 422, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    with state.atomic():
        state.acknowledge([job_id for job_id, _ in pending])
        state.acknowledge_groups(updates)
    return len(pending)
