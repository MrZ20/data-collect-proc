"""Publish compact daily JSONL and Markdown, deduplicated by job URL."""

from collections import defaultdict
import json
import time

from .github import GitHubError


def render_report(records: list[dict]) -> str:
    def cell(value):
        return str(value or '').replace('|', '\\|').replace('\n', ' ')
    lines = ['| UTC 时间 | Workflow | Job | 失败步骤 | Runner | 连续失败 | 链接 |',
             '|---|---|---|---|---|---|---|']
    for record in records:
        values = [record['time'], record['workflow'], record['job'],
                  '; '.join(record['failed_steps']), record['runner'],
                  record.get('consecutive_failures', ''), f"[Actions]({record['url']})"]
        lines.append('| ' + ' | '.join(cell(value) for value in values) + ' |')
    return '\n'.join(lines) + '\n'


def publish(client, repository: str, branch: str, state) -> int:
    pending = state.pending()
    if not pending:
        return 0
    client.ensure_branch(repository, branch)
    days = defaultdict(list)
    for _, record in pending:
        days[record['time'][:10]].append(record)
    for attempt in range(3):
        parent = client.branch_head(repository, branch)
        files = {}
        for day, additions in days.items():
            path = f'data/vllm-ascend-ci/{day}.jsonl'
            try:
                old = client.read_text(repository, path, parent)
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                old = ''
            records = {record['url']: record for line in old.splitlines()
                       if line.strip() for record in [json.loads(line)]}
            before = len(records)
            for record in additions:
                records.setdefault(record['url'], record)
            if len(records) == before:
                continue
            ordered = sorted(records.values(), key=lambda record: (record['time'], record['url']))
            files[path] = ''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in ordered)
            files[f'reports/vllm-ascend-ci/{day}.md'] = render_report(ordered)
        if not files:
            break
        try:
            client.commit_files(repository, branch, parent, files, 'Append CI failure candidates')
            break
        except GitHubError as exc:
            if exc.status not in (409, 422, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
        except RuntimeError:
            # The ref update may have succeeded before its response was lost.
            # Re-read the remote files and deduplicate instead of replaying POSTs.
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    state.acknowledge([job_id for job_id, _ in pending])
    return len(pending)
