"""Tests for the pre-publish public hygiene gate."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "public-hygiene-gate.py"
_spec = importlib.util.spec_from_file_location("public_hygiene_gate", GATE_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
run_all_gates_for_test = _mod.run_all_gates


def _init_temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    return repo


def test_clean_repo_passes_hygiene_gate(tmp_path):
    repo = _init_temp_repo(tmp_path)
    (repo / "README.md").write_text("# Clean Repository\n\nEndpoint: http://127.0.0.1:8788\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feat: initial clean commit"], check=True, capture_output=True)

    report = run_all_gates_for_test(repo)
    assert report["overall"] == "pass"


def test_leaked_public_ip_or_token_fails_hygiene_gate(tmp_path):
    repo = _init_temp_repo(tmp_path)
    (repo / "config.py").write_text('HOST = "45.67.89.10"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "config.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feat: add config"], check=True, capture_output=True)

    report = run_all_gates_for_test(repo)
    assert report["overall"] == "fail"
    assert report["gates"]["worktree_content"]["status"] == "fail"
    assert report["gates"]["git_history"]["status"] == "fail"
