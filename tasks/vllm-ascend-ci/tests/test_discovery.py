from ascend_ci_monitor.discovery import discover


def test_discovery_merges_observed_runtime_names_and_preserves_rules():
    class Client:
        def __init__(self):
            self.job_paths = []

        def get(self, path, params=None):
            if path == "/repos/owner/repo":
                return {"default_branch": "main"}
            if path == "/repos/owner/repo/git/trees/main":
                return {"tree": [{"path": ".github/workflows/ci.yml", "type": "blob"}]}
            assert path.endswith("/workflows/1/runs")
            assert params["status"] == "completed"
            return {"workflow_runs": [
                {"id": 10, "run_attempt": 2, "conclusion": "failure"},
                {"id": 11, "run_attempt": 1, "conclusion": "failure"},
                {"id": 12, "run_attempt": 1, "conclusion": "success"},
            ]}

        def paginate(self, path, key, params=None):
            if key == "workflows":
                return iter([
                    {"id": 1, "name": "CI", "state": "active", "path": ".github/workflows/ci.yml"},
                    {"id": 2, "name": "Old", "state": "active", "path": ".github/workflows/deleted.yml"},
                ])
            self.job_paths.append(path)
            if "/10/" in path:
                return iter([{"name": "test (cpu)", "steps": [
                    {"number": 2, "name": "Run tests"},
                    {"number": 1, "name": "Checkout"},
                ]}])
            return iter([{"name": "test (cpu)", "steps": [
                {"number": 1, "name": "Checkout"},
                {"number": 2, "name": "Upload"},
            ]}, {"name": "build", "steps": []}])

    existing = {"repository": "old/repo", "repeat_threshold": 3, "workflows": [
        {"name": "CI", "exclude_jobs": ["gate"], "jobs": [
            {"name": "test (cpu)", "steps": ["Checkout"], "exclude": ["Run tests"],
             "exclude_chains": [["Run tests", "Upload"]]},
        ]},
        {"name": "Retired", "exclude_jobs": [], "jobs": []},
    ]}
    client = Client()
    result = discover(client, "owner/repo", existing)

    assert result["repository"] == "owner/repo"
    assert result["repeat_threshold"] == 3
    assert result["workflows"][0] == {
        "name": "CI", "exclude_jobs": ["gate"], "jobs": [
            {"name": "test (cpu)", "steps": ["Checkout", "Upload", "Run tests"],
             "exclude": ["Run tests"], "exclude_chains": [["Run tests", "Upload"]]},
            {"name": "build", "steps": [], "exclude": [], "exclude_chains": []},
        ],
    }
    assert result["workflows"][1]["name"] == "Retired"
    assert client.job_paths == [
        "/repos/owner/repo/actions/runs/12/attempts/1/jobs",
        "/repos/owner/repo/actions/runs/10/attempts/2/jobs",
    ]
    assert existing["workflows"][0]["jobs"][0]["steps"] == ["Checkout"]
