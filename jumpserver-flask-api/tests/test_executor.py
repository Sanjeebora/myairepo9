import sys

import pytest

from dbaops_api.executor import LocalExecutor
from dbaops_api.runner import CommandTimeout


class Config:
    api_token_env = "DBAOPS_API_TOKEN"
    ccps_secret_env = "CCPS_SECRET"
    max_output_bytes = 1024


def test_local_executor_removes_secrets_from_child_environment(monkeypatch):
    monkeypatch.setenv("DBAOPS_API_TOKEN", "api-secret")
    monkeypatch.setenv("CCPS_SECRET", "ccps-secret")
    code = (
        "import os,sys; "
        "data=sys.stdin.buffer.readline(); "
        "print(os.environ.get('DBAOPS_API_TOKEN','missing')); "
        "print(os.environ.get('CCPS_SECRET','missing')); "
        "print(len(data))"
    )
    result = LocalExecutor(Config()).exec(
        [sys.executable, "-c", code], 5, "ccps-secret")
    assert result.returncode == 0
    assert result.stdout.splitlines() == [b"missing", b"missing", b"12"]
    assert b"api-secret" not in result.stdout + result.stderr
    assert b"ccps-secret" not in result.stdout + result.stderr


def test_local_executor_bounds_retained_output():
    code = "import sys; sys.stdout.write('x' * 200000)"
    result = LocalExecutor(Config()).exec(
        [sys.executable, "-c", code], 5, "ccps-secret")
    assert result.returncode == 0
    assert result.stdout_truncated
    assert len(result.stdout) == Config.max_output_bytes + 65536


def test_local_executor_enforces_timeout():
    code = "import time; time.sleep(30)"
    with pytest.raises(CommandTimeout):
        LocalExecutor(Config()).exec(
            [sys.executable, "-c", code], 0.01, "ccps-secret")
