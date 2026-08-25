"""非 HardStop 异常兜底回归（rc-r1 must-fix）。

旧实现：run()/review_cycle() 只捕 HardStop，WorktreeError（非 git 目录、
branch 重名、git 缺位皆可达）裸穿——CLI 栈回溯退出，run.json 永停
running，notifyd 只认终态而无限空转、wait 等满超时。
"""

import asyncio
from pathlib import Path

from evo_harness.config import HarnessConfig
from evo_harness.stages import Harness
from evo_harness.worktrees import WorktreeError


def _h(tmp_path) -> Harness:
    cfg = HarnessConfig(shared_root=tmp_path / "shared")
    return Harness(cfg, "r-esc", repo_root=tmp_path)


def _silence_pool(monkeypatch, h) -> None:
    async def _noop_shutdown() -> None:
        return None
    monkeypatch.setattr(h.pool, "shutdown", _noop_shutdown)


def _boom():
    async def _prepare() -> None:
        raise WorktreeError("git rev-parse 失败: not a git repository")
    return _prepare


def test_run_escalates_on_uncaught_exception(tmp_path, monkeypatch):
    h = _h(tmp_path)
    _silence_pool(monkeypatch, h)
    monkeypatch.setattr(h, "_prepare", _boom())

    rc = asyncio.run(h.run("目标"))
    assert rc == 2
    snap = h.bus.read_run()
    assert snap["status"] == "escalated"   # 终态落定，不假活 running
    assert snap["stage"] == "ESCALATE"
    events = [e["kind"] for e in snap["history"]]
    assert "hard_stop" in events
    assert any("UNCAUGHT WorktreeError" in e["detail"] for e in snap["history"])
    assert "run_exit" in events            # finally 照走（池收尾记账）


def test_review_cycle_escalates_on_uncaught_exception(tmp_path, monkeypatch):
    h = _h(tmp_path)
    _silence_pool(monkeypatch, h)
    monkeypatch.setattr(h, "_prepare", _boom())

    rc = asyncio.run(h.review_cycle("目标", max_rounds=1))
    assert rc == 2
    snap = h.bus.read_run()
    assert snap["status"] == "escalated"
    assert snap["stage"] == "ESCALATE"


def test_run_prepares_repo_root_from_cwd_default(tmp_path):
    """构造冒烟：Harness 直构即可跑兜底路径（不依赖 rmux/herdr）。"""
    h = _h(tmp_path)
    assert h.repo_root == tmp_path.resolve()
    assert (h.bus.root / "run.json").exists()


def test_plan_only_escalates_on_uncaught_exception(tmp_path, monkeypatch):
    """rc-r4 must-fix：run --plan-only 路径（含 _prepare）非 HardStop 异常
    不得裸穿——旧实现 _prepare 在 try 之外且只捕 HardStop，run.json 假活
    running（与 run()/review_cycle() 的 rc-r1 兜底同源）。"""
    from evo_harness.cli import _run_flow, build_parser
    from evo_harness.stages import Harness

    async def _boom(self) -> None:
        raise WorktreeError("git rev-parse 失败: not a git repository")

    monkeypatch.setattr(Harness, "_prepare", _boom)
    args = build_parser().parse_args(
        ["run", "目标", "--plan-only", "--shared", str(tmp_path / "shared"),
         "--run-id", "r-esc-po"]
    )
    rc = asyncio.run(_run_flow(args))
    assert rc == 2
    import json as _json

    snap = _json.loads(
        (tmp_path / "shared" / "r-esc-po" / "run.json").read_text(
            encoding="utf-8")
    )
    assert snap["status"] == "escalated"
    assert snap["stage"] == "ESCALATE"
    assert any("UNCAUGHT WorktreeError" in e["detail"]
               for e in snap["history"])
