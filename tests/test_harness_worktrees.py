"""evo_harness worktree 生命周期回归（真实 git，临时仓）。"""

import subprocess

from evo_harness.filebus import FileBus
from evo_harness.worktrees import WorktreeRegistry

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    # merge 会产生提交，仓库本地身份必须先配（真实仓库都有）
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"],
                   cwd=repo, check=True, env=GIT_ENV)
    return repo


def _commit(path, msg):
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", msg],
                   check=True, env=GIT_ENV)


def test_worktree_lifecycle(tmp_path):
    repo = _repo(tmp_path)
    bus = FileBus(tmp_path / "shared", "wt1")
    wt = WorktreeRegistry(repo, bus)

    wa = wt.ensure("exec-a", "agent/feat-a")
    wb = wt.ensure("exec-b", "agent/feat-b")
    assert wa != wb  # 禁止两个 unit 共享目录/分支
    (wa / "a.txt").write_text("a", encoding="utf-8")
    (wb / "b.txt").write_text("b", encoding="utf-8")
    _commit(wa, "a"); _commit(wb, "b")

    # 幂等复用
    assert wt.ensure("exec-a", "agent/feat-a") == wa

    # 按序 merge
    report = wt.merge_in_order(["exec-a", "exec-b"], ["exec-a", "exec-b"])
    assert all(r["merged"] for r in report)
    assert (repo / "a.txt").exists() and (repo / "b.txt").exists()

    ok, _ = wt.integration_test(["true"])
    assert ok is True

    # 清理零残留
    wt.cleanup_all()
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                            capture_output=True, text=True).stdout
    assert len(listed.strip().splitlines()) == 1
    branches = subprocess.run(["git", "-C", str(repo), "branch"],
                              capture_output=True, text=True).stdout.strip()
    assert "agent/" not in branches
