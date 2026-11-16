import pytest

from ascend_ci_monitor.state import State


@pytest.fixture
def state(tmp_path):
    with State(tmp_path / "state.sqlite") as store:
        yield store


def observe(state, run_id, status, date, workflow="CI", job="test", step="build"):
    state.observe(workflow, job, step, run_id, status, date)


def test_run_and_metadata_state(state):
    assert state.get_meta("cursor", "missing") == "missing"
    state.set_meta("cursor", "one")
    state.set_meta("cursor", "two")
    assert state.get_meta("cursor") == "two"
    state.track_run({"id": 4, "run_attempt": 1, "status": "queued"})
    assert state.pending_run_ids() == [4]
    assert state.known_attempt(4) is None
    state.track_run({"id": 4, "run_attempt": 2, "status": "completed"})
    assert state.pending_run_ids() == []
    assert state.known_attempt(4) == 2
    assert state.known_attempt(5) is None


def test_same_run_is_counted_once_and_skip_is_ignored(state):
    observe(state, 1, "skip", "2026-01-01T00:00:00Z")
    observe(state, 1, "failure", "2026-01-01T00:01:00Z")
    observe(state, 1, "success", "2026-01-01T00:02:00Z")
    observe(state, 1, "failure", "2026-01-01T00:03:00Z")
    assert state.streak("CI", "test", "build") == 1
    observe(state, 2, "failure", "2026-01-02T00:00:00Z")
    assert state.streak("CI", "test", "build") == 2
    assert state.streak("CI", "other", "build") == 0


def test_streak_uses_completion_time_and_success_resets(state):
    observe(state, 3, "failure", "2026-01-03T00:00:00Z")
    observe(state, 1, "failure", "2026-01-01T00:00:00Z")
    observe(state, 2, "success", "2026-01-02T00:00:00Z")
    observe(state, 4, "failure", "2026-01-04T00:00:00Z")
    assert state.streak("CI", "test", "build") == 2
    assert state.streak("CI", "test", "build", at="2026-01-02T00:00:00Z") == 0
    assert state.streak("CI", "test", "build", at="2026-01-01T00:00:00Z") == 1


def test_atomic_rollback_includes_observation_marker_and_outbox(state):
    with pytest.raises(RuntimeError):
        with state.atomic():
            observe(state, 1, "failure", "2026-01-01T00:00:00Z")
            state.mark_job(10)
            state.enqueue(10, {"message": "failure"})
            raise RuntimeError("abort")
    assert state.streak("CI", "test", "build") == 0
    assert not state.has_job(10)
    assert state.pending() == []
    with state.atomic():
        observe(state, 1, "failure", "2026-01-01T00:00:00Z")
        state.mark_job(10)
        state.enqueue(10, {"message": "failure"})
    assert state.has_job(10) and state.streak("CI", "test", "build") == 1
    assert state.pending() == [(10, {"message": "failure"})]


def test_outbox_persists_and_acknowledged_job_cannot_be_enqueued_twice(tmp_path):
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        state.enqueue(7, {"message": "失败"})
        state.enqueue(7, {"message": "duplicate"})
    with State(path) as state:
        assert state.pending() == [(7, {"message": "失败"})]
        state.acknowledge([7, 999])
        state.enqueue(7, {"message": "duplicate after ack"})
    with State(path) as state:
        assert state.pending() == []


def test_http_cache_persists_compressed_and_prunes_old_windows(tmp_path):
    path = tmp_path / 'cache.sqlite'
    payload = {'workflow_runs': [{'name': '构建' * 100, 'id': 1}]}
    with State(path) as state:
        state.put_http_cache('old', '"etag1"', payload, '2026-01-01T00:00:00Z')
        state.put_http_cache('current', '"etag2"', payload, '2026-01-02T00:00:00Z')
        blob = state._db.execute('SELECT payload FROM http_cache WHERE key = ?', ('old',)).fetchone()[0]
        assert len(blob) < len(str(payload).encode('utf-8'))
    with State(path) as state:
        assert state.get_http_cache('old') == ('"etag1"', payload)
        state.prune_http_cache('2026-01-02T00:00:00Z')
        assert state.get_http_cache('old') is None
        assert state.get_http_cache('current') == ('"etag2"', payload)
