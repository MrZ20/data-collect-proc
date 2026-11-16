"""Incremental runtime collection; classification and logs belong to the agent."""

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

import yaml

from .policy import Policy
from .publish import publish

LOG = logging.getLogger(__name__)


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def sync_policy(client, config: dict, state_dir: Path, local_path=None) -> Policy:
    if local_path:
        return Policy.load(local_path)
    cache = state_dir / 'workflows.yaml'
    try:
        text = client.read_text(config['data_repository'], config['policy_path'], config.get('config_branch', 'main'))
        policy = Policy.from_dict(yaml.safe_load(text))
        temporary = cache.with_suffix('.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(cache)
        return policy
    except (RuntimeError, ValueError, yaml.YAMLError) as exc:
        if not cache.exists():
            raise
        LOG.warning('Using cached policy: %s', exc)
        return Policy.load(cache)


def runs_between(client, repository: str, start: datetime, end: datetime) -> dict[int, dict]:
    """Split busy ranges to avoid GitHub's 1,000-result search ceiling."""
    path = f'/repos/{repository}/actions/runs'
    params = {'created': f'{utc(start)}..{utc(end)}', 'per_page': 100}
    first = client.get(path, params)
    if first['total_count'] >= 1000:
        if (end - start).total_seconds() <= 1:
            raise RuntimeError('Too many workflow runs in one second; collection window is incomplete')
        middle = start + timedelta(seconds=int((end - start).total_seconds() // 2))
        return runs_between(client, repository, start, middle) | runs_between(client, repository, middle, end)
    records = {run['id']: run for run in first['workflow_runs']}
    for page in range(2, (first['total_count'] + 99) // 100 + 1):
        data = client.get(path, dict(params, page=page))
        records.update((run['id'], run) for run in data['workflow_runs'])
    return records


def collect(source, writer, config: dict, policy: Policy, state, *, now=None, upload=True) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    repository = config['source_repository']
    if policy.repository != repository:
        raise ValueError('Policy repository does not match source_repository')
    previous = state.get_meta('scanned_until')
    start = parse_time(previous) if previous else now - timedelta(hours=config.get('initial_hours', 1))
    start -= timedelta(minutes=10)
    last_sweep = state.get_meta('last_sweep')
    sweep = previous and (not last_sweep or now - parse_time(last_sweep) >= timedelta(hours=24))
    if sweep:
        start = min(start, now - timedelta(days=config.get('recheck_days', 7)))
    runs = runs_between(source, repository, start, now)
    for run_id in state.pending_run_ids():
        if run_id not in runs:
            runs[run_id] = source.get(f'/repos/{repository}/actions/runs/{run_id}')

    jobs = []
    processed_runs = []
    queried = 0
    LOG.info('Scanning %s runs from %s to %s', len(runs), utc(start), utc(now))
    for index, run in enumerate(sorted(runs.values(), key=lambda run: run['created_at']), 1):
        attempt = run['run_attempt']
        known = state.known_attempt(run['id'])
        if known == attempt:
            continue
        processed_runs.append(run)
        # Analyse the batched run metadata before paying for job requests.
        if attempt == 1 and (
            run['status'] in ('queued', 'waiting', 'pending', 'requested')
            or run.get('conclusion') == 'skipped'
            or (run.get('conclusion') == 'success' and not policy.needs_success_steps(run['name']))
        ):
            continue
        # The job list already includes every step, runner and job link. One
        # paginated request also covers all attempts instead of one per attempt.
        path = f'/repos/{repository}/actions/runs/{run["id"]}/jobs'
        for job in source.paginate(path, 'jobs', {'filter': 'all'}):
            if job['status'] == 'completed' and not state.has_job(job['id']):
                jobs.append((run, job))
        queried += 1
        if index % 50 == 0:
            LOG.info('Fetched jobs for %s/%s runs', index, len(runs))

    added = 0
    missing_steps = 0
    with state.atomic():
        for run, job in sorted(jobs, key=lambda pair: pair[1].get('completed_at') or pair[0]['updated_at']):
            if state.has_job(job['id']):
                continue
            if not job.get('steps') and job.get('conclusion') in ('failure', 'timed_out'):
                # A second read can recover a transiently incomplete step list.
                job = source.get(f'/repos/{repository}/actions/jobs/{job["id"]}')
                if not job.get('steps'):
                    missing_steps += 1
                    LOG.warning('No runtime steps for failed job %s; no candidate generated', job['id'])
            result = policy.evaluate(run['name'], job)
            completed = job.get('completed_at') or run['updated_at']
            for step, outcome in result.repeat_observations.items():
                state.observe(run['name'], job['name'], step, run['id'], outcome, completed)
            repeat_count = max((state.streak(run['name'], job['name'], step, at=completed)
                                for step in result.repeat_steps), default=0)
            if not result.excluded_job and (result.remaining_steps or repeat_count >= policy.repeat_threshold):
                record = {'time': completed, 'workflow': run['name'], 'job': job['name'],
                          'failed_steps': result.failed_steps, 'runner': job.get('runner_name'),
                          'base_repo': repository, 'url': job['html_url']}
                if repeat_count >= policy.repeat_threshold:
                    record['consecutive_failures'] = repeat_count
                state.enqueue(job['id'], record)
                added += 1
            state.mark_job(job['id'])
        for run in processed_runs:
            state.track_run(run)
        state.set_meta('scanned_until', utc(now))
        if sweep or not previous:
            state.set_meta('last_sweep', utc(now))
    published = publish(writer, config['data_repository'], config['data_branch'], state) if upload else 0
    return {'runs_scanned': len(runs), 'runs_queried': queried, 'jobs_processed': len(jobs), 'candidates_added': added,
            'published': published, 'pending_upload': len(state.pending()), 'missing_steps': missing_steps}
