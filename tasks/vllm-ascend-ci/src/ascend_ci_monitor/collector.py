"""Incremental runtime collection; classification and logs belong to the agent."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
from pathlib import Path
from time import monotonic

import yaml

from .github import GitHubError
from .policy import Policy
from .publish import publish

LOG = logging.getLogger(__name__)


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def sync_policy(client, config: dict, state_dir: Path, local_path=None) -> Policy:
    if local_path:
        return _parse_policy(Path(local_path).read_text(encoding="utf-8"), "local")
    cache = state_dir / 'workflows.yaml'
    try:
        text = client.read_text(config['data_repository'], config['policy_path'], config.get('config_branch', 'main'))
        policy = _parse_policy(text, 'remote')
        temporary = cache.with_suffix('.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(cache)
        return policy
    except (RuntimeError, ValueError, yaml.YAMLError) as exc:
        if not cache.exists():
            raise
        LOG.warning('Using cached policy: %s', exc)
        return _parse_policy(cache.read_text(encoding="utf-8"), "cache")


def _parse_policy(text, source):
    policy = Policy.from_dict(yaml.safe_load(text))
    LOG.info('Policy source=%s sha256=%s', source, sha256(text.encode()).hexdigest()[:12])
    return policy


@contextmanager
def stage(name, client):
    started = monotonic()
    requests = getattr(client, 'requests', 0)
    outcome = 'ok'
    try:
        yield
    except Exception:
        outcome = 'error'
        raise
    finally:
        LOG.info('Stage %s outcome=%s elapsed=%.2fs requests=%s',
                 name, outcome, monotonic() - started, getattr(client, 'requests', 0) - requests)


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


def _collect_jobs(source, state, repository, run, deadline):
    path = f'/repos/{repository}/actions/runs/{run["id"]}/jobs'
    listed = list(source.paginate(path, 'jobs', {'filter': 'all'}))
    attempt = run['run_attempt']
    current_jobs = [job for job in listed if job.get('run_attempt', attempt) == attempt]
    reasons, jobs = [], []
    missing_steps = errors = 0
    if run['status'] == 'completed':
        if any(job['status'] != 'completed' for job in current_jobs):
            reasons.append('completed run has unfinished jobs')
        if not current_jobs and run.get('conclusion') in ('failure', 'timed_out'):
            reasons.append('failed run has no jobs')
    for job in listed:
        if job['status'] != 'completed' or state.has_job(job['id']):
            continue
        if not job.get('steps') and job.get('conclusion') in ('failure', 'timed_out'):
            if monotonic() >= deadline:
                errors += 1
                continue
            try:
                job = source.get(f'/repos/{repository}/actions/jobs/{job["id"]}')
            except RuntimeError as exc:
                LOG.warning('Job detail run=%s attempt=%s job=%s: %s',
                            run['id'], attempt, job['id'], exc)
                if isinstance(exc, GitHubError) and exc.status in (404, 410):
                    reasons.append(f'job {job["id"]} details unavailable')
                    missing_steps += 1
                else:
                    errors += 1
                continue
            if job['conclusion'] == 'cancelled':
                jobs.append((run, job))
                continue
            if job['status'] != 'completed' or not job.get('steps'):
                missing_steps += 1
                reasons.append(f'failed job {job["id"]} has incomplete steps')
                continue
        jobs.append((run, job))
    return jobs, reasons, missing_steps, errors


def _schedule_recheck(state, run_id, attempt, reasons, previous, now, *, api_error=False):
    first_seen = parse_time(previous[2]) if previous and previous[0] == attempt else now
    age = now - first_seen
    if not api_error and age >= timedelta(hours=24):
        LOG.warning('Incomplete source data after 24h run=%s attempt=%s: %s',
                    run_id, attempt, '; '.join(reasons))
        state.clear_recheck(run_id)
        return False
    delay = timedelta(minutes=45) if age < timedelta(hours=2) else timedelta(hours=6)
    if not api_error:
        delay = min(delay, timedelta(hours=24) - age)
    state.defer_run(run_id, attempt, '; '.join(reasons), utc(first_seen), utc(now + delay))
    LOG.warning('Deferred run=%s attempt=%s retry_at=%s: %s',
                run_id, attempt, utc(now + delay), '; '.join(reasons))
    return True


def _publish(writer, config, state):
    with stage('publish', writer):
        return publish(writer, config['data_repository'], config['data_branch'], state)


def collect(source, writer, config: dict, policy: Policy, state, *, now=None, upload=True) -> dict:
    try:
        result = _collect(source, config, policy, state, now)
    except RuntimeError:
        # Discovery failed before the cursor could advance. The old outbox is still safe to publish.
        if upload:
            try:
                _publish(writer, config, state)
            except RuntimeError as exc:
                LOG.error('Outbox publication also failed: %s', exc)
        raise
    result['published'] = _publish(writer, config, state) if upload else 0
    result['pending_upload'] = len(state.pending())
    return result


def _collect(source, config, policy, state, now):
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    repository = config['source_repository']
    if policy.repository != repository:
        raise ValueError('Policy repository does not match source_repository')
    previous = state.get_meta('scanned_until')
    initial = state.get_meta('collection_started_at')
    if initial is None:
        initial = previous or utc(now - timedelta(hours=config.get('initial_hours', 1), minutes=10))
        state.set_meta('collection_started_at', initial)
        if previous:
            LOG.warning('Legacy state has no initial boundary; using saved cursor %s', initial)
    collection_start = parse_time(initial)
    incremental_start = parse_time(previous) - timedelta(minutes=10) if previous else collection_start
    rerun_start = max(collection_start, now - timedelta(days=config.get('recheck_days', 2)))
    start = max(collection_start, min(incremental_start, rerun_start))
    with stage('run_list', source):
        runs = runs_between(source, repository, start, now, state)
    LOG.info('Scanning %s runs from %s to %s', len(runs), utc(start), utc(now))

    pending_ids = set(state.pending_run_ids())
    rechecks = state.rechecks()
    open_groups = state.unfinished_groups()
    jobs, processed, group_updates, retries = [], [], [], []
    out_of_scope = []
    queried = missing_steps = errors = 0
    deadline = monotonic() + 20 * 60
    interrupted = False
    with stage('run_details', source):
        for run_id in sorted(pending_ids - runs.keys()):
            if monotonic() >= deadline:
                interrupted = True
                break
            retry = rechecks.get(run_id)
            if retry and parse_time(retry[3]) > now:
                continue
            try:
                runs[run_id] = source.get(f'/repos/{repository}/actions/runs/{run_id}')
            except RuntimeError as exc:
                LOG.warning('Run metadata run=%s: %s', run_id, exc)
                errors += 1
                unavailable = isinstance(exc, GitHubError) and exc.status in (404, 410)
                retries.append((run_id, retry[0] if retry else 0,
                                ['run metadata unavailable'], not unavailable, True))
    with stage('job_details', source):
        for run in sorted(runs.values(), key=lambda item: item['created_at']):
            if monotonic() >= deadline:
                interrupted = True
                break
            run_id, attempt = run['id'], run['run_attempt']
            workflow_name = policy.workflow_name(run)
            if workflow_name is None:
                if run_id in pending_ids:
                    out_of_scope.append(run_id)
                continue
            run = dict(run, name=workflow_name)
            retry = rechecks.get(run_id)
            if retry and retry[0] == attempt and parse_time(retry[3]) > now:
                continue
            known = state.known_attempt(run_id)
            older_groups = [number for number in open_groups.get(run_id, ()) if number < attempt]
            if not retry and known == attempt and not older_groups:
                continue
            if known is not None and run_id not in pending_ids and not older_groups:
                if parse_time(run['created_at']) < rerun_start:
                    continue
            processed.append(run)
            reasons, api_error = [], False
            skip_jobs = (known == attempt and not retry) or (attempt == 1 and (
                run['status'] in ('queued', 'waiting', 'pending', 'requested')
                or run.get('conclusion') == 'skipped'
                or (run.get('conclusion') == 'success' and not policy.needs_success_steps(workflow_name))
            ))
            if not skip_jobs:
                queried += 1
                try:
                    ready, reasons, missing, job_errors = _collect_jobs(source, state, repository, run, deadline)
                    jobs.extend(ready)
                    missing_steps += missing
                    errors += job_errors
                    if job_errors:
                        reasons.append('job detail request failed')
                        api_error = True
                except RuntimeError as exc:
                    LOG.warning('Job list run=%s attempt=%s: %s', run_id, attempt, exc)
                    errors += 1
                    reasons.append('job list request failed')
                    api_error = not (isinstance(exc, GitHubError) and exc.status in (404, 410))
            for number in older_groups:
                if monotonic() >= deadline:
                    reasons.append('previous attempt status check deferred')
                    api_error = True
                    break
                try:
                    old = source.get(f'/repos/{repository}/actions/runs/{run_id}/attempts/{number}')
                    if old['status'] == 'completed':
                        group_updates.append((run_id, number, old['status'], old.get('conclusion')))
                    else:
                        reasons.append(f'previous attempt {number} still lacks a final status')
                except RuntimeError as exc:
                    LOG.warning('Attempt status run=%s attempt=%s: %s', run_id, number, exc)
                    errors += 1
                    reasons.append(f'previous attempt {number} status unavailable')
                    if not isinstance(exc, GitHubError) or exc.status not in (404, 410):
                        api_error = True
            retries.append((run_id, attempt, reasons, api_error, False))

    if interrupted:
        LOG.warning('Detail collection exceeded time budget; retaining discovery cursor')
    with stage('state', None), state.atomic():
        for run_id in out_of_scope:
            state.forget_run(run_id)
        added = _record_jobs(state, policy, repository, jobs)
        for run in processed:
            state.track_run(run)
            group_updates.append((run['id'], run['run_attempt'], run['status'], run.get('conclusion')))
        finished_groups = set()
        for run_id, attempt, status, conclusion in group_updates:
            if status == 'completed':
                finished_groups.add((run_id, attempt))
            for day in state.group_days(run_id, attempt):
                state.update_group(run_id, attempt, day, status, conclusion)
        for run_id, attempt, reasons, api_error, unresolved_run in retries:
            if not reasons:
                state.clear_recheck(run_id)
            elif not _schedule_recheck(state, run_id, attempt, reasons, rechecks.get(run_id), now,
                                       api_error=api_error):
                if unresolved_run:
                    state.forget_run(run_id)
                for number in open_groups.get(run_id, ()):
                    if (unresolved_run or number < attempt) and (run_id, number) not in finished_groups:
                        for day in state.group_days(run_id, number):
                            state.update_group(run_id, number, day, None, None)
        if not interrupted:
            state.set_meta('scanned_until', utc(now))
    state.prune_http_cache(utc(rerun_start))

    pending = state.rechecks()
    oldest_retry = min((entry[2] for entry in pending.values()), default=None)
    oldest_age = int((now - parse_time(oldest_retry)).total_seconds()) if oldest_retry else None
    return {'scan_start': utc(start), 'rerun_start': utc(rerun_start), 'runs_scanned': len(runs),
            'runs_queried': queried, 'jobs_processed': len(jobs), 'candidates_added': added,
            'missing_steps': missing_steps, 'detail_errors': errors, 'pending_rechecks': len(pending),
            'oldest_recheck_age_seconds': oldest_age, 'detail_budget_exhausted': interrupted}


def _record_jobs(state, policy, repository, jobs):
    added = 0
    for run, job in sorted(jobs, key=lambda pair: pair[1].get('completed_at') or pair[0]['updated_at']):
        if state.has_job(job['id']):
            continue
        state.mark_job(job['id'])
        if job.get('conclusion') == 'cancelled':
            continue
        result = policy.evaluate(run['name'], job)
        completed = job.get('completed_at') or run['updated_at']
        for step, outcome in result.repeat_observations.items():
            state.observe(run['name'], job['name'], step, run['id'], outcome, completed)
        repeat_count = max((state.streak(run['name'], job['name'], step, at=completed)
                            for step in result.repeat_steps), default=0)
        if result.excluded_job or (not result.remaining_steps and repeat_count < policy.repeat_threshold):
            continue
        job_attempt = job.get('run_attempt', run['run_attempt'])
        current = job_attempt == run['run_attempt']
        record = {'run_id': run['id'], 'run_attempt': job_attempt,
                  'head_sha': job.get('head_sha') or (run.get('head_sha') if current else None),
                  'run_status': run['status'] if current else None,
                  'run_conclusion': run.get('conclusion') if current else None,
                  'job_conclusion': job.get('conclusion'),
                  'time': completed, 'workflow': run['name'], 'job': job['name'],
                  'failed_steps': result.failed_steps, 'runner': job.get('runner_name'),
                  'base_repo': repository, 'url': job['html_url']}
        if repeat_count >= policy.repeat_threshold:
            record['consecutive_failures'] = repeat_count
        state.enqueue(job['id'], record)
        if completed[:10] not in state.group_days(run['id'], job_attempt):
            state.update_group(run['id'], job_attempt, completed[:10],
                               record['run_status'], record['run_conclusion'])
        added += 1
    return added
