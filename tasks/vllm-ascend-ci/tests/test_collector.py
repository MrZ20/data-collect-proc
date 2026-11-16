from datetime import datetime, timezone

import pytest

from ascend_ci_monitor.collector import collect
from ascend_ci_monitor.policy import Policy
from ascend_ci_monitor.state import State


CONFIG = {"source_repository": "org/repo", "data_repository": "org/data", "data_branch": "ci"}
NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


def run(run_id, minute=0, status="completed", attempt=1):
    return {"id": run_id, "name": "E2E", "status": status, "run_attempt": attempt,
            "created_at": f"2026-01-02T11:{minute:02}:00Z", "updated_at": "2026-01-02T11:59:00Z"}


def job(job_id, name, *steps, minute=0, status="completed"):
    return {"id": job_id, "name": name, "status": status, "conclusion": "failure",
            "completed_at": f"2026-01-02T11:{minute:02}:30Z", "runner_name": "node",
            "html_url": f"https://example.com/jobs/{job_id}",
            "steps": [{"name": name, "number": i, "conclusion": conclusion}
                      for i, (name, conclusion) in enumerate(steps, 1)]}


def rules():
    return Policy.from_dict({"repository": "org/repo", "repeat_threshold": 2, "workflows": [{
        "name": "E2E", "exclude_jobs": ["ci-gate", "ignore [gpu]"], "jobs": [
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

    def get(self, path, params=None):
        if self.fail:
            raise RuntimeError("source unavailable")
        if path.endswith("/actions/runs"):
            values = list(self.runs.values()) if self.listed_ids is None else [self.runs[i] for i in self.listed_ids]
            return {"total_count": len(values), "workflow_runs": values}
        if "/actions/runs/" in path:
            return self.runs[int(path.rsplit("/", 1)[-1])]
        if "/actions/jobs/" in path:
            raise RuntimeError("job unavailable")
        raise AssertionError(path)

    def paginate(self, path, key, params=None):
        if self.fail:
            raise RuntimeError("source unavailable")
        assert key == "jobs"
        assert params == {"filter": "all"}
        run_id = int(path.split("/")[-2])
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


def test_source_failure_does_not_advance_state(tmp_path):
    source = Source([run(1)], {})
    source.fail = True
    with State(tmp_path / "state.sqlite") as state:
        with pytest.raises(RuntimeError, match="source unavailable"):
            collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert state.get_meta("scanned_until") is None
        assert state.pending_run_ids() == []


def test_batch_metadata_avoids_unnecessary_job_requests(tmp_path):
    irrelevant = dict(run(1), name="Bot", conclusion="success")
    relevant = dict(run(2), conclusion="success")
    queued = run(3, status="queued")
    source = Source([irrelevant, relevant, queued], {})
    with State(tmp_path / "state.sqlite") as state:
        result = collect(source, None, CONFIG, rules(), state, now=NOW, upload=False)
        assert result["runs_scanned"] == 3
        assert result["runs_queried"] == 1
        assert state.pending_run_ids() == [3]


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
