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


def _runs_window(client, repository: str, start: datetime, end: datetime, state=None) -> dict[int, dict]:
    """Read one stable interval, retrying when pagination drops/duplicates rows."""
    path = f'/repos/{repository}/actions/runs'
    params = {'created': f'{utc(start)}..{utc(end)}', 'per_page': 100, 'exclude_pull_requests': 'true'}

    def page(number):
        query = dict(params, page=number)
        if state is not None:
            return client.get_cached(path, query, state, utc(end))
        return client.get(path, query)

    for attempt in range(2):
        first = page(1)
        total = first['total_count']
        if total >= 1000:
            if start == end:
                raise RuntimeError('Too many workflow runs in one second; collection window is incomplete')
            middle = start + timedelta(seconds=int((end - start).total_seconds() // 2))
            return (_runs_window(client, repository, start, middle, state)
                    | _runs_window(client, repository, middle + timedelta(seconds=1), end, state))
        records = {run['id']: run for run in first['workflow_runs']}
        consistent = True
        for number in range(2, (total + 99) // 100 + 1):
            data = page(number)
            consistent &= data['total_count'] == total
            records.update((run['id'], run) for run in data['workflow_runs'])
        if consistent and len(records) == total:
            return records
        LOG.warning('Run pagination changed in %s..%s; retrying interval', utc(start), utc(end))
    raise RuntimeError('Run pagination remained inconsistent; cursor was not advanced')


def runs_between(client, repository: str, start: datetime, end: datetime, state=None) -> dict[int, dict]:
    """Hour-aligned intervals keep older metadata pages eligible for ETag reuse."""
    records = {}
    while start <= end:
        hour_end = start.replace(minute=0, second=0) + timedelta(hours=1, seconds=-1)
        boundary = min(hour_end, end)
        records.update(_runs_window(client, repository, start, boundary, state))
        start = boundary + timedelta(seconds=1)
    return records


def collect(source, writer, config: dict, policy: Policy, state, *, now=None, upload=True) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    repository = config['source_repository']
    if policy.repository != repository:
        raise ValueError('Policy repository does not match source_repository')
    previous = state.get_meta('scanned_until')
    initial = state.get_meta('collection_started_at')
    if initial is None:
        # An old database cannot reveal its original starting point reliably.
        # Anchor at its saved cursor rather than silently importing older data.
        initial = previous or utc(now - timedelta(hours=config.get('initial_hours', 1), minutes=10))
        state.set_meta('collection_started_at', initial)
        if previous:
            LOG.warning('Legacy state has no initial boundary; using saved cursor %s', initial)
    collection_start = parse_time(initial)
    incremental_start = parse_time(previous) - timedelta(minutes=10) if previous else collection_start
    rerun_start = max(collection_start, now - timedelta(days=config.get('recheck_days', 2)))
    start = max(collection_start, min(incremental_start, rerun_start))
    runs = runs_between(source, repository, start, now, state)
    pending_ids = set(state.pending_run_ids())
    for run_id in pending_ids:
        if run_id not in runs:
            runs[run_id] = source.get(f'/repos/{repository}/actions/runs/{run_id}')

    jobs = []
    processed_runs = []
    out_of_scope = []
    queried = 0
    LOG.info('Scanning %s runs from %s to %s', len(runs), utc(start), utc(now))
    for index, run in enumerate(sorted(runs.values(), key=lambda run: run['created_at']), 1):
        workflow_name = policy.workflow_name(run)
        if workflow_name is None:
            if run['id'] in pending_ids:
                out_of_scope.append(run['id'])
            continue
        run = dict(run, name=workflow_name)
        attempt = run['run_attempt']
        known = state.known_attempt(run['id'])
        if known == attempt:
            continue
        if known is not None and parse_time(run['created_at']) < rerun_start:
            # A long downtime may extend new-run catch-up beyond the rerun window.
            continue
        tracked_run = dict(run)
        processed_runs.append(tracked_run)
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
            elif job['status'] != 'completed':
                # The run and job endpoints may become consistent at different
                # times. Keep polling until the listed jobs have also finished.
                tracked_run['status'] = 'in_progress'
        queried += 1
        if index % 50 == 0:
            LOG.info('Fetched jobs for %s/%s runs', index, len(runs))

    added = 0
    missing_steps = 0
    with state.atomic():
        for run_id in out_of_scope:
            state.forget_run(run_id)
        for run, job in sorted(jobs, key=lambda pair: pair[1].get('completed_at') or pair[0]['updated_at']):
            if state.has_job(job['id']):
                continue
            if job.get('conclusion') == 'cancelled':
                state.mark_job(job['id'])
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
                job_attempt = job.get('run_attempt', run['run_attempt'])
                current_attempt = job_attempt == run['run_attempt']
                record = {'run_id': run['id'], 'run_attempt': job_attempt,
                          'head_sha': job.get('head_sha') or (run.get('head_sha') if current_attempt else None),
                          'run_status': run['status'] if current_attempt else None,
                          'run_conclusion': run.get('conclusion') if current_attempt else None,
                          'job_conclusion': job.get('conclusion'),
                          'time': completed, 'workflow': run['name'], 'job': job['name'],
                          'failed_steps': result.failed_steps, 'runner': job.get('runner_name'),
                          'base_repo': repository, 'url': job['html_url']}
                if repeat_count >= policy.repeat_threshold:
                    record['consecutive_failures'] = repeat_count
                state.enqueue(job['id'], record)
                state.update_group(run['id'], job_attempt, completed[:10],
                                   record['run_status'], record['run_conclusion'])
                added += 1
            state.mark_job(job['id'])
        for run in processed_runs:
            for day in state.group_days(run['id'], run['run_attempt']):
                state.update_group(run['id'], run['run_attempt'], day, run['status'], run.get('conclusion'))
            state.track_run(run)
        state.set_meta('scanned_until', utc(now))
    state.prune_http_cache(utc(rerun_start))
    published = publish(writer, config['data_repository'], config['data_branch'], state) if upload else 0
    return {'scan_start': utc(start), 'rerun_start': utc(rerun_start),
            'runs_scanned': len(runs), 'runs_queried': queried, 'jobs_processed': len(jobs), 'candidates_added': added,
            'published': published, 'pending_upload': len(state.pending()), 'missing_steps': missing_steps}
