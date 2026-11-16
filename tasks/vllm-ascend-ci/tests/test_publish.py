import json

import pytest

from ascend_ci_monitor.github import GitHubError
from ascend_ci_monitor.publish import publish
from ascend_ci_monitor.state import State


class Writer:
    def __init__(self):
        self.files = {}
        self.commits = []

    def ensure_branch(self, repository, branch):
        assert (repository, branch) == ("org/data", "ci")

    def branch_head(self, repository, branch):
        return f"commit-{len(self.commits)}"

    def read_text(self, repository, path, ref):
        if path not in self.files:
            raise GitHubError(404, "not found")
        return self.files[path]

    def commit_files(self, repository, branch, parent, files, message):
        assert parent == f"commit-{len(self.commits)}"
        self.commits.append(files.copy())
        self.files.update(files)


def record(job_id, date="2026-01-02"):
    return {"time": f"{date}T11:00:00Z", "workflow": "E2E", "job": f"job {job_id}",
            "failed_steps": ["build"], "runner": "node", "base_repo": "org/repo",
            "url": f"https://example.com/jobs/{job_id}"}


def test_atomic_daily_files_and_retry_after_ack_loss(tmp_path, monkeypatch):
    writer = Writer()
    with State(tmp_path / "state.sqlite") as state:
        state.enqueue(1, record(1))
        state.enqueue(2, record(2))
        real_acknowledge = state.acknowledge
        def lost_ack(job_ids):
            raise RuntimeError("lost local ack")
        monkeypatch.setattr(state, "acknowledge", lost_ack)
        with pytest.raises(RuntimeError, match="lost local ack"):
            publish(writer, "org/data", "ci", state)
        assert len(state.pending()) == 2
        assert len(writer.commits) == 1
        assert set(writer.commits[0]) == {"data/vllm-ascend-ci/2026-01-02.jsonl", "reports/vllm-ascend-ci/2026-01-02.md"}
        monkeypatch.setattr(state, "acknowledge", real_acknowledge)
        assert publish(writer, "org/data", "ci", state) == 2
        assert state.pending() == []
        assert len(writer.commits) == 1
        records = [json.loads(line) for line in writer.files["data/vllm-ascend-ci/2026-01-02.jsonl"].splitlines()]
        assert {item["url"] for item in records} == {record(1)["url"], record(2)["url"]}
        assert writer.files["reports/vllm-ascend-ci/2026-01-02.md"].count("[Actions]") == 2


def test_multiple_days_written_in_one_commit(tmp_path):
    writer = Writer()
    with State(tmp_path / "state.sqlite") as state:
        state.enqueue(1, record(1))
        state.enqueue(2, record(2, "2026-01-03"))
        assert publish(writer, "org/data", "ci", state) == 2
        assert len(writer.commits) == 1
        assert set(writer.commits[0]) == {
            "data/vllm-ascend-ci/2026-01-02.jsonl", "reports/vllm-ascend-ci/2026-01-02.md",
            "data/vllm-ascend-ci/2026-01-03.jsonl", "reports/vllm-ascend-ci/2026-01-03.md",
        }


def test_lost_ref_response_rechecks_remote_before_retry(tmp_path, monkeypatch):
    class LostResponse(Writer):
        def commit_files(self, *args):
            super().commit_files(*args)
            raise RuntimeError("connection lost after ref update")
    monkeypatch.setattr("ascend_ci_monitor.publish.time.sleep", lambda _: None)
    writer = LostResponse()
    with State(tmp_path / "state.sqlite") as state:
        state.enqueue(1, record(1))
        assert publish(writer, "org/data", "ci", state) == 1
        assert len(writer.commits) == 1
        assert state.pending() == []
