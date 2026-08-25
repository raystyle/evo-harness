"""收尾回边回归：模板同步自修 + clean-wt（r10 集成门死因）。"""

import subprocess as sp
from pathlib import Path

from evo_harness.worktrees import clean_run_worktrees


def _mk_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def g(*a):
        sp.run(["git", *a], cwd=repo, check=True, capture_output=True)
    g("init", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / ".evotools" / "evo_harness" / "src").mkdir(parents=True)
    tpl = repo / "skills" / "evo" / "templates" / "tools" / "evo_harness" / "src"
    tpl.mkdir(parents=True)
    (repo / ".evotools" / "evo_harness" / "src" / "a.py").write_text("x = 1\n")
    (tpl / "a.py").write_text("x = 1\n")
    (repo / "f.txt").write_text("hi\n")
    g("add", "-A")
    g("commit", "-m", "init")
    return repo


def test_sync_tool_templates_repairs_drift(tmp_path):
    from evo_harness.stages import Harness

    repo = _mk_repo(tmp_path)
    h = Harness.__new__(Harness)  # 只用同步方法，不起全链
    h.repo_root = repo
    # 制造漂移：本体改了，模板没跟（r10 集成门死因）
    (repo / ".evotools" / "evo_harness" / "src" / "a.py").write_text("x = 2\n")
    h._sync_tool_templates()
    assert (repo / "skills" / "evo" / "templates" / "tools"
            / "evo_harness" / "src" / "a.py").read_text() == "x = 2\n"


def test_clean_run_worktrees_removes_only_that_run(tmp_path):
    repo = _mk_repo(tmp_path)
    def g(*a):
        sp.run(["git", *a], cwd=repo, check=True, capture_output=True)
    g("worktree", "add", "-b", "topic/u1", str(tmp_path / "evo-wt-rX" / "u1"))
    g("worktree", "add", "-b", "topic/u2", str(tmp_path / "evo-wt-rY" / "u2"))
    cleaned = clean_run_worktrees(repo, "rX")
    assert cleaned == ["u1"]
    assert not (tmp_path / "evo-wt-rX").exists()
    assert (tmp_path / "evo-wt-rY" / "u2").exists()  # 其它 run 不连坐
    branches = sp.run(["git", "branch"], cwd=repo,
                      capture_output=True, text=True).stdout
    assert "topic/u1" not in branches and "topic/u2" in branches
