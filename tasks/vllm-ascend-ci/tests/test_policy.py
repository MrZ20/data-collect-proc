import pytest

from ascend_ci_monitor.policy import Policy


def policy():
    return Policy.from_dict({
        "repository": "example/repo",
        "repeat_threshold": 10,
        "workflows": [{
            "name": "CI",
            "exclude_jobs": ["docs*"],
            "jobs": [
                {"name": "test*", "steps": ["listed"], "exclude": ["flaky*"],
                 "exclude_chains": [{"cascade": ["build", "test"]}]},
                {"name": "test (gpu)", "exclude": ["other"]},
            ],
        }],
    })


def job(*steps, name="test (gpu)"):
    return {"name": name, "conclusion": "failure", "steps": [
        {"name": step_name, "number": number, "conclusion": conclusion}
        for number, step_name, conclusion in steps
    ]}


def test_exclusions_combine_and_job_exclusion_wins():
    p = policy()
    result = p.evaluate("CI", job((1, "flaky one", "failure"), (2, "other", "timed_out"),
                                  (3, "new step", "failure")))
    assert result.failed_steps == ["flaky one", "other", "new step"]
    assert result.remaining_steps == ["new step"]
    assert result.repeat_steps == ["flaky one", "other"]
    assert result.repeat_observations == {"flaky one": "failure", "other": "failure"}
    excluded = p.evaluate("CI", job((1, "flaky one", "failure"), name="docs build"))
    assert excluded.excluded_job and excluded.repeat_observations == {} and excluded.repeat_steps == []


def test_complete_ordered_chain_excludes_members_but_not_unrelated_failure():
    result = policy().evaluate("CI", job((1, "build", "failure"), (2, "unrelated", "failure"),
                                         (4, "test", "timed_out")))
    assert result.remaining_steps == ["unrelated"]
    assert result.repeat_steps == ["build"]
    assert result.repeat_observations == {"build": "failure"}


@pytest.mark.parametrize("steps, remaining", [
    ([(1, "build", "failure"), (2, "test", "success")], ["build"]),
    ([(1, "build", "failure"), (2, "test", "skipped")], ["build"]),
    ([(1, "test", "failure"), (2, "build", "failure")], ["test", "build"]),
    ([(1, "build", "failure")], ["build"]),
])
def test_incomplete_or_reversed_chain_excludes_nothing(steps, remaining):
    assert policy().evaluate("CI", job(*steps)).remaining_steps == remaining


def test_chain_does_not_cross_jobs_and_setup_is_a_normal_failure():
    p = policy()
    assert p.evaluate("CI", job((1, "build", "failure"))).remaining_steps == ["build"]
    assert p.evaluate("CI", job((2, "test", "failure"))).remaining_steps == ["test"]
    assert p.evaluate("CI", job((1, "Set up job", "failure"))).remaining_steps == ["Set up job"]
    assert p.evaluate("CI", job()).remaining_steps == []


def test_exact_workflow_and_unmatched_job_include_failures():
    p = policy()
    failed = job((1, "flaky one", "failure"), (2, "build", "failure"), (3, "test", "failure"))
    assert p.evaluate("ci", failed).remaining_steps == ["flaky one", "build", "test"]
    assert p.evaluate("CI", {**failed, "name": "another"}).remaining_steps == ["flaky one", "build", "test"]


def test_repeat_observes_only_actual_relevant_steps():
    result = policy().evaluate("CI", job((1, "flaky one", "success"), (2, "build", "skipped"),
                                         (3, "test", "failure")))
    assert result.repeat_steps == []
    assert result.repeat_observations == {"flaky one": "success", "build": "skip"}
    assert result.remaining_steps == ["test"]


def test_repeat_exclude_only_disables_reentry_not_normal_failure_capture():
    p = Policy.from_dict({"repository": "org/repo", "workflows": [{"name": "CI", "jobs": [
        {"name": "test*", "exclude": ["approval*", "flaky"],
         "repeat_exclude": ["approval*"]},
    ]}]})
    result = p.evaluate("CI", job((1, "approval check", "failure"), (2, "flaky", "failure"),
                                  (3, "Set up job", "failure")))
    assert result.remaining_steps == ["Set up job"]
    assert result.repeat_steps == ["flaky"]
    assert result.repeat_observations == {"flaky": "failure"}
    unexcluded = Policy.from_dict({"repository": "org/repo", "workflows": [{"name": "CI", "jobs": [
        {"name": "test*", "repeat_exclude": ["approval*"]},
    ]}]})
    assert unexcluded.evaluate("CI", job((1, "approval check", "failure"))).remaining_steps == ["approval check"]


def test_load_yaml_and_validation(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("repository: org/repo\nworkflows: []\n", encoding="utf-8")
    loaded = Policy.load(path)
    assert loaded.repository == "org/repo" and loaded.repeat_threshold == 10
    with pytest.raises(ValueError, match="at least two"):
        Policy.from_dict({"repository": "org/repo", "workflows": [{"name": "CI", "jobs": [
            {"name": "job", "exclude_chains": [{"broken": ["one"]}]}
        ]}]})
    with pytest.raises(ValueError):
        Policy.from_dict({"repository": "org/repo", "workflows": "CI"})
