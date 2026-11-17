from datetime import datetime, timedelta, timezone

import pytest

from ascend_ci_monitor.collector import collect
from ascend_ci_monitor.policy import Policy
from ascend_ci_monitor.state import State


CONFIG = {"source_repository": "org/repo", "data_repository": "org/data", "data_branch": "ci"}
NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


def run(run_id, minute=0, status="completed", attempt=1):
    return {"id": run_id, "name": "E2E", "status": status, "run_attempt": attempt,
            "path": ".github/workflows/e2e.yaml",
            "created_at": f"2026-01-02T11:{minute:02}:00Z", "updated_at": "2026-01-02T11:59:00Z"}


def job(job_id, name, *steps, minute=0, status="completed"):
    return {"id": job_id, "name": name, "status": status, "conclusion": "failure",
            "completed_at": f"2026-01-02T11:{minute:02}:30Z", "runner_name": "node",
            "html_url": f"https://example.com/jobs/{job_id}",
            "steps": [{"name": name, "number": i, "conclusion": conclusion}
                      for i, (name, conclusion) in enumerate(steps, 1)]}


def rules():
    return Policy.from_dict({"repository": "org/repo", "repeat_threshold": 2, "workflows": [{
        "name": "E2E", "file": ".github/workflows/e2e.yaml",
        "exclude_jobs": ["ci-gate", "ignore [gpu]"], "jobs": [
            {"name": "test [gpu]", "steps": ["known"], "exclude": ["flaky [gpu]"],
             "exclude_chains": [{"cascade": ["build", "test"]}]},
        ],
    }]})


class Source:
    def __init__(self, runs, jobs):
        self.runs = {item["id"]: item for item in runs}
        self.jobs = jobs
        self.fail = False
        self.listed_ids = None
        self.cached_queries = []
        self.job_queries = []

    def get(self, path, params=None):
        if self.fail:
            raise RuntimeError("source unavailable")
        if path.endswith("/actions/runs"):
            values = list(self.runs.values()) if self.listed_ids is None else [self.runs[i] for i in self.listed_ids]
            start, end = params['created'].split('..')
            values = [item for item in values if start <= item['created_at'] <= end]
            values.sort(key=lambda item: item['id'])
            size = params.get('per_page', 100)
            offset = (params.get('page', 1) - 1) * size
            return {"total_count": len(values), "workflow_runs": values[offset:offset + size]}
        if "/actions/runs/" in path:
            return self.runs[int(path.rsplit("/", 1)[-1])]
        if "/actions/jobs/" in path:
            raise RuntimeError("job unavailable")
        raise AssertionError(path)

    def get_cached(self, path, params, cache, window_end):
        self.cached_queries.append((path, dict(params), window_end))
        return self.get(path, params)

    def paginate(self, path, key, params=None):
        if self.fail:
            raise RuntimeError("source unavailable")
        assert key == "jobs"
        assert params == {"filter": "all"}
        run_id = int(path.split("/")[-2])
        self.job_queries.append(run_id)
        return iter([item for (id_, _), jobs in self.jobs.items() if id_ == run_id for item in jobs])


def test_collect_filters_and_avoids_second_pass_duplicates(tmp_path):
    source = Source([run(1)], {(1, 1): [
        job(1, "ci-gate", ("gate", "failure")),
        job(2, "test [gpu]", ("flaky [gpu]", "failure")),
        job(3, "test [gpu]", ("build", "failure"), ("test", "failure")),
        job(4, "test [gpu]", ("Set up job", "failure")),
        job(5, "test [gpu]", ("new step", "failure")),
        job(6, "ignore [gpu]", ("other", "failure")),
    ]})
    with State(tmp_path / "state.sqlite") as state:
        first = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert first["candidates_added"] == 2
        assert [record["failed_steps"] for _, record in state.pending()] == [["Set up job"], ["new step"]]
        assert collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)["candidates_added"] == 0
        assert len(state.pending()) == 2


def test_pending_run_outside_window_is_fetched_on_completion(tmp_path):
    source = Source([run(1, status="in_progress")], {(1, 1): []})
    with State(tmp_path / "state.sqlite") as state:
        collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert state.pending_run_ids() == [1]
        source.runs[1] = run(1)
        source.jobs[(1, 1)] = [job(1, "test [gpu]", ("new step", "failure"))]
        source.listed_ids = []
        result = collect(source, None, CONFIG, rules(), state, now=NOW.replace(hour=13), upload=False)
        assert result["candidates_added"] == 1
        assert state.pending_run_ids() == []


def test_repeat_exclusion_reenters_at_threshold_rerun_does_not_inflate_streak(tmp_path):
    source = Source([run(1, 1), run(2, 2)], {
        (1, 1): [job(1, "test [gpu]", ("flaky [gpu]", "failure"), minute=1)],
        (2, 1): [job(2, "test [gpu]", ("flaky [gpu]", "failure"), minute=2)],
    })
    with State(tmp_path / "state.sqlite") as state:
        assert collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)["candidates_added"] == 1
        assert state.pending()[0][1]["consecutive_failures"] == 2
        source.runs[2] = run(2, 2, attempt=2)
        source.jobs[(2, 2)] = [job(3, "test [gpu]", ("flaky [gpu]", "failure"), minute=3)]
        collect(source, None, CONFIG, rules(), state, now=NOW.replace(hour=13), upload=False)
        assert state.streak("E2E", "test [gpu]", "flaky [gpu]") == 2
        assert all(record["consecutive_failures"] == 2 for _, record in state.pending())


def test_repeat_whitelisted_approval_never_reenters_but_setup_failures_do(tmp_path):
    policy = Policy.from_dict({"repository": "org/repo", "repeat_threshold": 10, "workflows": [{
        "name": "E2E", "jobs": [{"name": "codeowner-approval",
            "exclude": ["CODEOWNERS approval check"],
            "repeat_exclude": ["CODEOWNERS approval check"]}],
    }]})
    runs = [run(i, minute=i) for i in range(1, 13)]
    jobs = {(i, 1): [job(i, "codeowner-approval", ("CODEOWNERS approval check", "failure"), minute=i)]
            for i in range(1, 12)}
    jobs[(12, 1)] = [job(12, "codeowner-approval", ("CODEOWNERS approval check", "failure"),
                         ("Initialize containers", "failure"), minute=12)]
    source = Source(runs, jobs)
    with State(tmp_path / "state.sqlite") as state:
        state.observe("E2E", "codeowner-approval", "CODEOWNERS approval check", 99,
                      "failure", "2026-01-02T10:00:00Z")
        result = collect(source, None, CONFIG, policy, state, now=NOW, upload=False)
        assert result["candidates_added"] == 1
        assert state.pending()[0][1]["failed_steps"] == ["CODEOWNERS approval check", "Initialize containers"]
        assert "consecutive_failures" not in state.pending()[0][1]
        assert state.streak("E2E", "codeowner-approval", "CODEOWNERS approval check") == 1


def test_source_failure_does_not_advance_state(tmp_path):
    source = Source([run(1)], {})
    source.fail = True
    with State(tmp_path / "state.sqlite") as state:
        with pytest.raises(RuntimeError, match="source unavailable"):
            collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert state.get_meta("scanned_until") is None
        assert state.pending_run_ids() == []


def test_batch_metadata_avoids_unnecessary_job_requests(tmp_path):
    irrelevant = dict(run(1), name="Bot", path=".github/workflows/bot.yaml", conclusion="success")
    relevant = dict(run(2), conclusion="success")
    queued = run(3, status="queued")
    source = Source([irrelevant, relevant, queued], {})
    with State(tmp_path / "state.sqlite") as state:
        result = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert result["runs_scanned"] == 3
        assert result["runs_queried"] == 1
        assert state.pending_run_ids() == [3]


def test_only_registered_workflows_fetch_jobs_and_removed_workflows_stop_tracking(tmp_path):
    removed = dict(run(1), path='.github/workflows/removed.yaml')
    current = dict(run(2), name='E2E (PR) 123', head_branch='pull-request-branch')
    source = Source([removed, current], {
        (1, 1): [job(1, 'old job', ('failure', 'failure'))],
        (2, 1): [job(2, 'test [gpu]', ('Set up job', 'failure'))],
    })
    with State(tmp_path / 'state.sqlite') as state:
        state.track_run(dict(removed, status='in_progress'))
        result = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert result['candidates_added'] == 1
        assert source.job_queries == [2]
        assert state.pending_run_ids() == []
        assert not state.has_job(1)
        assert state.pending()[0][1]['workflow'] == 'E2E'


def test_source_failure_during_processing_rolls_back_previous_job(tmp_path):
    source = Source([run(1)], {(1, 1): [
        job(1, "test [gpu]", ("new step", "failure"), minute=1),
        job(2, "test [gpu]", minute=2),
    ]})
    with State(tmp_path / "state.sqlite") as state:
        # The missing-step job is re-read after the preceding job was processed.
        with pytest.raises(RuntimeError, match="job unavailable"):
            collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert not state.has_job(1)
        assert state.pending() == []
        assert state.get_meta("scanned_until") is None


def test_publication_failure_keeps_outbox(tmp_path):
    class Writer:
        def ensure_branch(self, repository, branch):
            raise RuntimeError("publication unavailable")
    source = Source([run(1)], {(1, 1): [job(1, "test [gpu]", ("new step", "failure"))]})
    with State(tmp_path / "state.sqlite") as state:
        with pytest.raises(RuntimeError, match="publication unavailable"):
            collect(source, Writer(), CONFIG, rules(), state, now=NOW)
        assert len(state.pending()) == 1
        assert state.has_job(1)
        assert state.get_meta("scanned_until") == "2026-01-02T12:00:00Z"


def test_initial_boundary_remains_after_24_hours_and_old_queries_are_reused(tmp_path):
    source = Source([run(1)], {})
    with State(tmp_path / 'state.sqlite') as state:
        first = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        initial_queries = source.cached_queries.copy()
        initial = '2026-01-02T10:50:00Z'
        assert first['scan_start'] == state.get_meta('collection_started_at') == initial
        source.cached_queries.clear()
        result = collect(source, None, CONFIG, rules(), state, now=NOW + timedelta(hours=25), upload=False)
        assert result['scan_start'] == initial
        assert state.get_meta('collection_started_at') == initial
        assert initial_queries[:2] == source.cached_queries[:2]
        assert all(query['created'].split('..')[0] >= initial for _, query, _ in source.cached_queries)
        assert source.job_queries == [1]  # Completed unchanged runs do not fetch jobs again.


def test_completed_rerun_outside_incremental_overlap_within_two_days(tmp_path):
    source = Source([run(1)], {(1, 1): [job(1, 'test [gpu]', ('old', 'failure'))]})
    with State(tmp_path / 'state.sqlite') as state:
        collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        source.runs[1] = run(1, attempt=2)
        source.jobs[(1, 2)] = [job(2, 'test [gpu]', ('new', 'failure'))]
        result = collect(source, None, CONFIG, rules(), state, now=NOW + timedelta(hours=24), upload=False)
        assert result['candidates_added'] == 1
        assert [item['failed_steps'] for _, item in state.pending()] == [['old'], ['new']]
        assert state.known_attempt(1) == 2


def test_expired_rerun_is_ignored_but_old_pending_run_is_completed(tmp_path):
    old_done = run(1)
    old_pending = run(2)
    source = Source([dict(old_done, run_attempt=2), old_pending], {
        (1, 2): [job(1, 'test [gpu]', ('expired', 'failure'))],
        (2, 1): [job(2, 'test [gpu]', ('pending', 'failure'))],
    })
    with State(tmp_path / 'state.sqlite') as state:
        state.set_meta('collection_started_at', '2026-01-01T00:00:00Z')
        state.set_meta('scanned_until', '2026-01-05T11:50:00Z')
        state.track_run(old_done)
        state.track_run(dict(old_pending, status='in_progress'))
        now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
        result = collect(source, None, CONFIG, rules(), state, now=now, upload=False)
        assert result['rerun_start'] == '2026-01-03T12:00:00Z'
        assert result['candidates_added'] == 1
        assert [item['failed_steps'] for _, item in state.pending()] == [['pending']]
        assert state.known_attempt(1) == 1
        assert state.known_attempt(2) == 1
        assert source.job_queries == [2]


def test_legacy_database_anchors_saved_cursor_and_never_queries_earlier(tmp_path):
    source = Source([run(1)], {})
    with State(tmp_path / 'state.sqlite') as state:
        state.set_meta('scanned_until', '2026-01-02T11:30:00Z')
        result = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert state.get_meta('collection_started_at') == result['scan_start'] == '2026-01-02T11:30:00Z'
        assert all(query['created'].split('..')[0] >= result['scan_start'] for _, query, _ in source.cached_queries)
        assert source.job_queries == []


def test_inconsistent_paginated_window_retries_before_advancing_cursor(tmp_path):
    class ChangingSource(Source):
        def __init__(self, runs):
            super().__init__(runs, {})
            self.changed = False

        def get(self, path, params=None):
            result = super().get(path, params)
            if path.endswith('/actions/runs') and params['page'] == 2 and not self.changed:
                self.changed = True
                result['workflow_runs'] = [self.runs[1]]  # Duplicate causes a missing ID.
            return result

    source = ChangingSource([dict(run(i), conclusion='success', name='Bot') for i in range(1, 102)])
    with State(tmp_path / 'state.sqlite') as state:
        result = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert result['runs_scanned'] == 101
        assert state.get_meta('scanned_until') == '2026-01-02T12:00:00Z'
        pages = [query['page'] for _, query, _ in source.cached_queries if '11:00:00Z..' in query['created']]
        assert pages == [1, 2, 1, 2]


def test_unstable_pagination_does_not_advance_cursor(tmp_path):
    class UnstableSource(Source):
        def get(self, path, params=None):
            result = super().get(path, params)
            if path.endswith('/actions/runs') and params['page'] == 2:
                result['workflow_runs'] = [self.runs[1]]
            return result

    source = UnstableSource([run(i) for i in range(1, 102)], {})
    with State(tmp_path / 'state.sqlite') as state:
        with pytest.raises(RuntimeError, match='pagination remained inconsistent'):
            collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert state.get_meta('scanned_until') is None


def test_completed_run_with_incomplete_job_is_rechecked(tmp_path):
    source = Source([run(1)], {(1, 1): [job(1, 'test [gpu]', ('new', 'failure'), status='in_progress')]})
    with State(tmp_path / 'state.sqlite') as state:
        assert collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)['candidates_added'] == 0
        assert state.pending_run_ids() == [1]
        source.jobs[(1, 1)] = [job(1, 'test [gpu]', ('new', 'failure'))]
        result = collect(source, None, CONFIG, rules(), state, now=NOW + timedelta(hours=1), upload=False)
        assert result['candidates_added'] == 1
        assert state.pending_run_ids() == []


def test_cancelled_job_ignored_but_failure_in_cancelled_run_is_kept(tmp_path):
    cancelled = job(1, 'test [gpu]', ('flaky [gpu]', 'failure'))
    cancelled['conclusion'] = 'cancelled'
    failed = job(2, 'test [gpu]', ('new step', 'failure'))
    source = Source([dict(run(1), conclusion='cancelled')], {(1, 1): [cancelled, failed]})
    with State(tmp_path / 'state.sqlite') as state:
        result = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert result['candidates_added'] == 1
        assert state.pending()[0][1]['job_conclusion'] == 'failure'
        assert state.streak('E2E', 'test [gpu]', 'flaky [gpu]') == 0


def test_run_completion_updates_existing_group_without_new_job(tmp_path):
    source = Source([run(1, status='in_progress')], {(1, 1): [job(1, 'test [gpu]', ('new step', 'failure'))]})
    with State(tmp_path / 'state.sqlite') as state:
        collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        state.acknowledge_groups(state.pending_groups())
        source.runs[1] = dict(run(1), conclusion='failure')
        collect(source, None, CONFIG, rules(), state, now=NOW.replace(hour=13), upload=False)
        assert state.pending_groups() == [(1, 1, '2026-01-02', 'completed', 'failure')]
