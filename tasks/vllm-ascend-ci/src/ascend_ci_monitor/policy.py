"""Evaluate runtime GitHub Actions failures against the monitor policy."""

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Evaluation:
    excluded_job: bool
    failed_steps: list[str]
    remaining_steps: list[str]
    repeat_steps: list[str]
    # Includes successes and skips so the collector can update streaks.
    repeat_observations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _JobRule:
    name: str
    steps: tuple[str, ...]
    exclude: tuple[str, ...]
    chains: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _Workflow:
    name: str
    file: str | None
    exclude_jobs: tuple[str, ...]
    jobs: tuple[_JobRule, ...]


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return tuple(_name(item, label) for item in value)


def _matches(name: str, pattern: str) -> bool:
    return name == pattern or fnmatchcase(name, pattern)


class Policy:
    def __init__(self, repository: str, repeat_threshold: int, workflows: tuple[_Workflow, ...]):
        self.repository = repository
        self.repeat_threshold = repeat_threshold
        self._workflows = workflows

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        data = _mapping(data, "policy")
        repository = _name(data.get("repository"), "repository")
        threshold = data.get("repeat_threshold", 10)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("repeat_threshold must be a positive integer")
        workflow_data = data.get("workflows", [])
        if not isinstance(workflow_data, list):
            raise ValueError("workflows must be a list")
        workflows = []
        for workflow in workflow_data:
            workflow = _mapping(workflow, "workflow")
            name = _name(workflow.get("name"), "workflow name")
            workflow_file = workflow.get("file")
            if workflow_file is not None:
                workflow_file = _name(workflow_file, "workflow file")
            exclude_jobs = _names(workflow.get("exclude_jobs", []), "exclude_jobs")
            jobs_data = workflow.get("jobs", [])
            if not isinstance(jobs_data, list):
                raise ValueError("jobs must be a list")
            jobs = []
            for job in jobs_data:
                job = _mapping(job, "job rule")
                job_name = _name(job.get("name"), "job name")
                steps = _names(job.get("steps", []), "steps")
                exclude = _names(job.get("exclude", []), "exclude")
                chain_data = job.get("exclude_chains", [])
                if not isinstance(chain_data, list):
                    raise ValueError("exclude_chains must be a list")
                chains = []
                for chain in chain_data:
                    chain = _mapping(chain, "chain")
                    if len(chain) != 1:
                        raise ValueError("each chain must have exactly one name")
                    chain_name, chain_steps = next(iter(chain.items()))
                    _name(chain_name, "chain name")
                    chain_steps = _names(chain_steps, "chain steps")
                    if len(chain_steps) < 2:
                        raise ValueError("a chain must contain at least two steps")
                    chains.append(chain_steps)
                jobs.append(_JobRule(job_name, steps, exclude, tuple(chains)))
            workflows.append(_Workflow(name, workflow_file, exclude_jobs, tuple(jobs)))
        return cls(repository, threshold, tuple(workflows))

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        with Path(path).open(encoding="utf-8") as stream:
            return cls.from_dict(yaml.safe_load(stream))

    def needs_success_steps(self, workflow_name: str) -> bool:
        """Successful runs are needed only to reset configured failure streaks."""
        return any(rule.exclude or rule.chains for workflow in self._workflows
                   if workflow.name == workflow_name for rule in workflow.jobs)

    def workflow_name(self, run: dict) -> str | None:
        """Resolve the maintained workflow name despite a dynamic run display name."""
        for workflow in self._workflows:
            if workflow.file:
                if workflow.file == run.get('path'):
                    return workflow.name
            elif workflow.name == run['name']:
                return workflow.name
        return None

    def evaluate(self, workflow_name: str, job: dict) -> Evaluation:
        workflows = [workflow for workflow in self._workflows if workflow.name == workflow_name]
        job_name = job.get("name", "")
        if any(_matches(job_name, pattern) for wf in workflows for pattern in wf.exclude_jobs):
            return Evaluation(True, [], [], [])

        rules = [rule for wf in workflows for rule in wf.jobs if _matches(job_name, rule.name)]
        exclude = [pattern for rule in rules for pattern in rule.exclude]
        chains = [chain for rule in rules for chain in rule.chains]
        first_names = {chain[0] for chain in chains}
        downstream_names = {name for chain in chains for name in chain[1:]} - first_names
        steps = job.get("steps") or []
        failures = [step for step in steps if step.get("conclusion") in ("failure", "timed_out")]

        def repeatable(name: str) -> bool:
            return name not in downstream_names and (
                name in first_names or any(_matches(name, pattern) for pattern in exclude)
            )

        observations: dict[str, str] = {}
        rank = {"skip": 0, "success": 1, "failure": 2}
        for step in steps:
            name = step.get("name")
            if not isinstance(name, str) or not repeatable(name):
                continue
            conclusion = step.get("conclusion")
            status = ("failure" if conclusion in ("failure", "timed_out") else
                      "success" if conclusion == "success" else "skip")
            if name not in observations or rank[status] > rank[observations[name]]:
                observations[name] = status

        suppressed = {i for i, step in enumerate(failures)
                      if any(_matches(step.get("name", ""), pattern) for pattern in exclude)}
        # Only a complete, ordered chain in this job suppresses its failing members.
        for chain in chains:
            def match(position: int, previous_number: int | float, chosen: list[int]) -> None:
                if position == len(chain):
                    suppressed.update(chosen)
                    return
                for index, step in enumerate(failures):
                    number = step.get("number")
                    if (step.get("name") == chain[position]
                            and isinstance(number, (int, float)) and not isinstance(number, bool)
                            and number > previous_number):
                        match(position + 1, number, chosen + [index])
            match(0, float("-inf"), [])

        failed_names = [step["name"] for step in failures]
        return Evaluation(
            False,
            failed_names,
            [step["name"] for i, step in enumerate(failures) if i not in suppressed],
            [step["name"] for step in failures if repeatable(step["name"])],
            observations,
        )
