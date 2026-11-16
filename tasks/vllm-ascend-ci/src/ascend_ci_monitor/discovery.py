"""Discover workflow and job names from completed Actions runs."""

from copy import deepcopy
from urllib.parse import quote


def _sample_runs(runs, limit):
    """Prefer the newest success and failure, then fill with recent runs."""
    if limit <= 0:
        return []
    selected = []
    for conclusion in ("success", "failure"):
        run = next((item for item in runs if item.get("conclusion") == conclusion), None)
        if run is not None and len(selected) < limit:
            selected.append(run)
    for run in runs:
        if len(selected) >= limit:
            break
        if run not in selected:
            selected.append(run)
    return selected


def discover(client, repository, existing=None, samples_per_workflow=2):
    """Merge names observed in completed workflow runs into existing rules.

    The client exposes ``get(path, params=None)`` and
    ``paginate(path, key, params=None)``. Workflow source is not inspected.
    """
    result = deepcopy(existing or {})
    result["repository"] = repository
    result.setdefault("repeat_threshold", 10)
    workflows = result.setdefault("workflows", [])
    known_workflows = {workflow["name"]: workflow for workflow in workflows}
    base = f"/repos/{repository}/actions"
    repo = client.get(f"/repos/{repository}")
    branch = quote(repo["default_branch"], safe="")
    tree = client.get(f"/repos/{repository}/git/trees/{branch}", params={"recursive": 1})
    if tree.get("truncated"):
        raise ValueError("GitHub returned a truncated repository tree")
    current_paths = {entry["path"] for entry in tree["tree"] if entry["type"] == "blob"}

    for item in client.paginate(f"{base}/workflows", "workflows", params={"per_page": 100}):
        if item.get("state") != "active" or item.get("path") not in current_paths:
            continue
        name = item["name"]
        workflow = known_workflows.get(name)
        if workflow is None:
            workflow = {"name": name, "exclude_jobs": [], "jobs": []}
            workflows.append(workflow)
            known_workflows[name] = workflow
        workflow.setdefault("exclude_jobs", [])
        jobs = workflow.setdefault("jobs", [])
        known_jobs = {job["name"]: job for job in jobs}

        payload = client.get(
            f"{base}/workflows/{item['id']}/runs",
            params={"status": "completed", "per_page": 100},
        )
        for run in _sample_runs(payload.get("workflow_runs", []), samples_per_workflow):
            attempt = run.get("run_attempt", 1)
            path = f"{base}/runs/{run['id']}/attempts/{attempt}/jobs"
            for observed in client.paginate(path, "jobs", params={"per_page": 100}):
                job_name = observed["name"]
                job = known_jobs.get(job_name)
                if job is None:
                    job = {"name": job_name, "steps": [], "exclude": [], "exclude_chains": []}
                    jobs.append(job)
                    known_jobs[job_name] = job
                steps = job.setdefault("steps", [])
                job.setdefault("exclude", [])
                job.setdefault("exclude_chains", [])
                for step in sorted(observed.get("steps", []), key=lambda entry: entry["number"]):
                    if step["name"] not in steps:
                        steps.append(step["name"])
    return result
