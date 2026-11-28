"""The deploy workflow's security properties (P5B plan review): they must not regress silently."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
WF = yaml.safe_load(TEXT)
JOBS = WF["jobs"]


def _norm(s: str) -> str:
    return " ".join(s.split())


def test_workflow_run_gate_requires_a_push_to_this_repos_main():
    gate = _norm(JOBS["gate"]["if"])
    for condition in (
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'",
    ):
        assert condition in gate, condition
    on = WF.get("on", WF.get(True))  # PyYAML reads the key `on` as True
    assert on["workflow_run"] == {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}


def test_everything_runs_from_the_gated_sha():
    assert "github.event.workflow_run.head_sha" in TEXT
    checkout = next(s for s in JOBS["build"]["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ needs.gate.outputs.sha }}"
    assert JOBS["deploy"]["env"]["SHA"] == "${{ needs.gate.outputs.sha }}"


def test_least_privilege_per_job():
    assert WF["permissions"] == {"contents": "read"}
    assert JOBS["build"]["permissions"] == {"contents": "read", "packages": "write"}
    assert JOBS["deploy"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert "id-token" not in JOBS["build"]["permissions"] and "packages" not in JOBS["deploy"]["permissions"]


def test_no_environment_so_the_oidc_subject_stays_the_main_ref():
    assert "environment" not in JOBS["deploy"]


def test_ssm_fetches_deploy_sh_pinned_to_the_commit():
    run = next(s["run"] for s in JOBS["deploy"]["steps"] if "send-command" in s.get("run", ""))
    assert 'url="https://raw.githubusercontent.com/${GITHUB_REPOSITORY}/${SHA}/deploy/deploy.sh"' in run
    assert "/opt/pagecite/deploy.sh" not in TEXT
    assert "AWS-RunShellScript" in run


def test_no_long_lived_aws_keys_or_ssh():
    for needle in ("aws-access-key-id", "aws-secret-access-key", "AWS_SECRET_ACCESS_KEY", "ssh ", "scp "):
        assert needle not in TEXT
