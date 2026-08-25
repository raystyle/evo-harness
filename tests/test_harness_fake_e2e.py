"""evo-harness fake E2E：merge 集成自修（模板同步重跑） + clean-wt 清场。

这两条是 r10 集成门死因的回归回边：
- 合并后集成测试失败时，先程序同步 `.evotools/evo_harness →
  skills/evo/templates/tools/evo_harness`（机械漂移自修），再重跑集成；
- hard stop 后 `clean_run_worktrees` 只清指定 run 的 worktree 与分支。

全部用真实 git 临时仓 + FileBus/WorktreeRegistry，不依赖真 LLM/PTY。
"""

from __future__ import annotations

import asyncio
import json
import subprocess as sp
from pathlib import Path

import pytest

from evo_harness.filebus import FileBus
from evo_harness.stages import Harness
from evo_harness.worktrees import WorktreeRegistry, clean_run_worktrees

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _run(repo: Path, *args: str, env: dict | None = None) -> sp.CompletedProcess:
    proc = sp.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} 失败: {proc.stderr.strip() or proc.stdout.strip()}"
    )
    return proc


def _repo(tmp_path: Path) -> Path:
    """造一个已提交、可 merge 的临时 git 仓。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@t")
    _run(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "base", env=GIT_ENV)
    return repo


def _commit(path: Path, msg: str) -> None:
    _run(path, "add", "-A")
    _run(path, "commit", "-q", "-m", msg, env=GIT_ENV)


def _tool_src(repo: Path) -> Path:
    return repo / ".evotools" / "evo_harness" / "src" / "evo_harness"


def _template_src(repo: Path) -> Path:
    return (
        repo / "skills" / "evo" / "templates" / "tools"
        / "evo_harness" / "src" / "evo_harness"
    )


def _install_twin_copies(repo: Path, marker: str = "v1") -> None:
    """两个拷贝各放一个 marker 文件，作为同步漂移的可观察对象。"""
    for d in (_tool_src(repo), _template_src(repo)):
        d.mkdir(parents=True, exist_ok=True)
        (d / "marker.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")


def _write_plan(bus: FileBus, unit_id: str = "u1") -> None:
    out = bus.task_dir("planner") / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps({
        "steps": [{"id": "impl", "title": "示例实现", "depends_on": []}],
        "merge_order": [unit_id],
    }, ensure_ascii=False), encoding="utf-8")
    (out / "allocations.json").write_text(json.dumps({
        unit_id: {
            "branch": f"agent/{unit_id}",
            "scope": ["src/**"],
            "success_criteria": ["diff 非空"],
        }
    }, ensure_ascii=False), encoding="utf-8")


def _harness(repo: Path, run_id: str) -> Harness:
    h = Harness.__new__(Harness)
    h.repo_root = repo
    h.run_id = run_id
    h.bus = FileBus(repo.parent / "shared", run_id)
    h.worktrees = WorktreeRegistry(repo, h.bus)
    h._fake = False
    return h


def test_fake_e2e_merge_self_repair_syncs_templates_and_cleans(tmp_path):
    """合并后集成首败 → 模板同步自修 → 重跑通过 → worktree/分支清场。"""
    repo = _repo(tmp_path)
    _install_twin_copies(repo)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "tool copies", env=GIT_ENV)

    h = _harness(repo, "fake-e2e-merge")
    _write_plan(h.bus, "u1")

    wt = h.worktrees.ensure("u1", "agent/u1")
    (wt / "u1.txt").write_text("u1\n", encoding="utf-8")
    _commit(wt, "u1 change")

    # 制造 r10 死因：本体已改，模板没跟。
    (_tool_src(repo) / "marker.py").write_text("MARKER = 'v2'\n", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "drift body", env=GIT_ENV)

    calls = []
    def flaky_integration(command=None):
        calls.append(command)
        if len(calls) == 1:
            return False, "integration failed: template drift"
        return True, "integration ok"

    h.worktrees.integration_test = flaky_integration

    asyncio.run(h._stage_merge())

    # 自修回边确实重跑了一次（首败 → 同步 → 二跑通过）。
    assert calls == [None, None]
    assert (_template_src(repo) / "marker.py").read_text(
        encoding="utf-8") == "MARKER = 'v2'\n"

    # merge 真发生：主仓拿到 u1.txt。
    assert (repo / "u1.txt").read_text(encoding="utf-8") == "u1\n"

    # merge 成功后 worktree + 分支清场。
    assert not (repo.parent / f"evo-wt-{h.run_id}" / "u1").exists()
    branches = _run(repo, "branch").stdout
    assert "agent/u1" not in branches

    # 清场后登记表也已收回。
    assert "u1" not in h.worktrees._load()


def test_fake_e2e_merge_self_repair_still_hardstops_when_repair_fails(tmp_path):
    """自修重跑仍败才升级 HardStop，证明回边只给机械漂移一次机会。"""
    repo = _repo(tmp_path)
    _install_twin_copies(repo)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "tool copies", env=GIT_ENV)

    h = _harness(repo, "fake-e2e-merge-hardstop")
    _write_plan(h.bus, "u1")
    wt = h.worktrees.ensure("u1", "agent/u1")
    (wt / "u1.txt").write_text("u1\n", encoding="utf-8")
    _commit(wt, "u1 change")

    def always_fail(command=None):
        return False, "real regression, not drift"

    h.worktrees.integration_test = always_fail

    from evo_harness.statemachine import HardStop
    with pytest.raises(HardStop):
        asyncio.run(h._stage_merge())


def test_fake_e2e_clean_wt_only_targets_requested_run(tmp_path):
    """clean-wt 清场只删指定 run 的 worktree/分支，其它 run 不连坐。"""
    repo = _repo(tmp_path)
    _run(repo, "worktree", "add", "-b", "topic/u1", str(tmp_path / "evo-wt-rX" / "u1"))
    _run(repo, "worktree", "add", "-b", "topic/u2", str(tmp_path / "evo-wt-rY" / "u2"))

    cleaned = clean_run_worktrees(repo, "rX")

    assert cleaned == ["u1"]
    assert not (tmp_path / "evo-wt-rX").exists()
    assert (tmp_path / "evo-wt-rY" / "u2").exists()
    branches = _run(repo, "branch").stdout
    assert "topic/u1" not in branches
    assert "topic/u2" in branches


def test_fake_e2e_template_copies_no_drift_after_sync(tmp_path):
    """模板同步幂等：同步后两拷贝逐文件一致，再次同步无漂移。"""
    repo = _repo(tmp_path)
    _install_twin_copies(repo)
    (_tool_src(repo) / "extra.py").write_text("x = 1\n", encoding="utf-8")
    (_tool_src(repo) / "marker.py").write_text("MARKER = 'v2'\n", encoding="utf-8")

    h = _harness(repo, "fake-e2e-sync")
    h._sync_tool_templates()
    h._sync_tool_templates()  # 幂等：第二遍不引入新漂移

    tool_files = sorted(p.relative_to(_tool_src(repo))
                        for p in _tool_src(repo).rglob("*") if p.is_file())
    assert tool_files, "fixture 应有待同步文件"
    for rel in tool_files:
        tool = (_tool_src(repo) / rel).read_text(encoding="utf-8")
        tpl = (_template_src(repo) / rel).read_text(encoding="utf-8")
        assert tool == tpl, f"{rel} 同步后仍漂移"


def test_fake_e2e_sync_ignores_missing_optional_surfaces(tmp_path):
    """两拷贝齐全才同步；缺 scripts/README/pyproject 不抛错（幂等结构容忍）。"""
    repo = _repo(tmp_path)
    _install_twin_copies(repo)
    (_tool_src(repo) / "marker.py").write_text("MARKER = 'v3'\n", encoding="utf-8")

    h = _harness(repo, "fake-e2e-sync-sparse")
    h._sync_tool_templates()

    assert (_template_src(repo) / "marker.py").read_text(
        encoding="utf-8") == "MARKER = 'v3'\n"
