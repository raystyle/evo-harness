"""evo_harness revise 重试预算回归（claude r1 must-fix #1）。

旧实现：_stage_execute 每轮用全新 ExecutionUnit（retries=0）覆写 unit.json，
retries += 1 只发生在内存对象上，check_unit_retry 恒真，BUDGET_EXCEEDED
永不触发，LOCAL_FAIL 死循环只靠全局墙钟兜底。
"""

import asyncio
import json
from pathlib import Path

import pytest

from evo_harness.config import HarnessConfig
from evo_harness.stages import Harness
from evo_harness.statemachine import HardStop

UID = "exec-fake-01"


def _harness(tmp_path, fake: bool = True) -> Harness:
    cfg = HarnessConfig(
        shared_root=tmp_path / "shared",
        fake_agent_script=Path("fake") if fake else None,
    )
    h = Harness(cfg, "r1", repo_root=tmp_path)

    plan_dir = h.bus.task_out("planner", "plan.json").parent  # task_out 会建 out/
    (plan_dir / "plan.json").write_text(
        json.dumps({"steps": [{"id": "impl"}], "merge_order": [UID]}),
        encoding="utf-8",
    )
    (plan_dir / "allocations.json").write_text(
        json.dumps({UID: {"branch": "agent/feat-fake", "scope": ["src/**"]}}),
        encoding="utf-8",
    )

    async def _noop(stage, tasks):
        return None

    h._run_tasks_checked = _noop  # 不真跑 pool，只走 unit.json 读写路径
    return h


def test_revise_retry_count_persists_across_rounds(tmp_path):
    """revise 轮重建 unit.json 后，retries 必须从磁盘继承而非归零。"""
    h = _harness(tmp_path)
    asyncio.run(h._stage_execute("goal"))
    assert h._unit_of(UID).retries == 0

    h._bump_retry(UID)  # 第 1 轮 revise
    asyncio.run(h._stage_execute("goal", only=[UID]))
    assert h._unit_of(UID).retries == 1  # 旧实现这里读回 0

    h._bump_retry(UID)
    asyncio.run(h._stage_execute("goal", only=[UID]))
    assert h._unit_of(UID).retries == 2


def test_revise_budget_hard_stops_after_max_retries(tmp_path):
    """N 轮 revise 后 HardStop(BUDGET_EXCEEDED)，不再无限重试。"""
    h = _harness(tmp_path)
    asyncio.run(h._stage_execute("goal"))
    max_retries = h._unit_of(UID).max_retries
    assert max_retries == h.config.budget.unit_max_retries  # 预算有消费者

    for _ in range(max_retries - 1):
        h._bump_retry(UID)  # 预算内
    with pytest.raises(HardStop) as exc:
        h._bump_retry(UID)  # 第 max_retries 轮：超上限
    assert exc.value.kind == "BUDGET_EXCEEDED"


def test_merge_gate_blocks_on_failed_unit_merge(tmp_path, monkeypatch):
    """merge 报告任一 merged=false 即 HardStop，且绝不进 cleanup
    （旧实现只查集成测试，cleanup 的 branch -D 会静默丢未合入工作）。"""
    h = _harness(tmp_path, fake=False)  # 非 fake 才走真实 merge 门禁
    cleaned = []

    class _FakeWorktrees:
        def merge_in_order(self, unit_ids, order):
            return [
                {"unit_id": UID, "merged": False, "detail": "CONFLICT (content)"},
            ]

        def integration_test(self):
            raise AssertionError("合并失败时不许跑集成测试")

        def cleanup(self, uid):
            cleaned.append(uid)

    monkeypatch.setattr(h, "worktrees", _FakeWorktrees())

    # 控制模型 v2：真模式 merge 冲突先问主 agent（无限期等人）·测试注入
    # 决策「abort」走硬停止分支（skip-debt 分支由 fake E2E 覆盖）
    async def _decide_abort(node, timeout_s):
        return "abort"
    monkeypatch.setattr(h.bus, "await_decision", _decide_abort)

    with pytest.raises(HardStop) as exc:
        asyncio.run(h._stage_merge())
    assert exc.value.kind == "GATE_FAILED"
    assert UID in exc.value.detail
    assert cleaned == []


def test_merge_conflict_skip_debt_continues(tmp_path, monkeypatch):
    """决策 skip-debt：跳过失败 unit 继续集成（债入事件流），cleanup 只清成功的。"""
    h = _harness(tmp_path, fake=False)
    cleaned, integrated = [], []

    class _FakeWorktrees:
        def merge_in_order(self, unit_ids, order):
            return [
                {"unit_id": "u-ok", "merged": True, "detail": ""},
                {"unit_id": UID, "merged": False, "detail": "CONFLICT"},
            ]

        def integration_test(self):
            integrated.append(True)
            return True, "3 passed"

        def cleanup(self, uid):
            cleaned.append(uid)

    monkeypatch.setattr(h, "worktrees", _FakeWorktrees())

    async def _decide_skip(node, timeout_s):
        return "skip-debt"
    monkeypatch.setattr(h.bus, "await_decision", _decide_skip)

    asyncio.run(h._stage_merge())
    assert integrated == [True]      # 集成照跑
    assert cleaned == ["u-ok"]       # 只清成功 unit，失败的留现场


def test_escalate_review_extend_runs_extra_round(tmp_path, monkeypatch):
    """审计 r3：extend 必须真的多跑一轮（旧 range 定值实现无效）。"""
    h = _harness(tmp_path, fake=True)
    decisions, review_no = [], {"n": 0}

    async def _prep():
        pass
    monkeypatch.setattr(h, "_prepare", _prep)

    async def _fake_rtc(stage, tasks):
        if stage.startswith("review"):
            review_no["n"] += 1
        for tid, (prompt, artifact) in tasks.items():
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if stage.startswith("review") and review_no["n"] == 1:
                artifact.write_text(
                    "# r\n\n- [must-fix] x\n\nVERDICT: REGRESSION\n",
                    encoding="utf-8")
            else:
                artifact.write_text(
                    "# r\n\nVERDICT: AGREE\n"
                    if stage.startswith("review") else "{}",
                    encoding="utf-8")
    monkeypatch.setattr(h, "_run_tasks_checked", _fake_rtc)

    async def _decide(node, timeout_s):
        decisions.append(node)
        return "extend"
    monkeypatch.setattr(h.bus, "await_decision", _decide)
    monkeypatch.setattr(h.bus, "request_decision", lambda *a, **k: None)

    rc = asyncio.run(h.review_cycle("g", max_rounds=1))
    assert decisions == ["escalate-review"]
    hist = h.bus.read_run()["history"]
    rounds = [e for e in hist if e.get("kind") == "round"]
    assert len(rounds) >= 2, "extend 后必须再跑至少一轮"
    assert h.bus.read_run()["status"] == "done"


async def _approve_then_merge(h):
    """批准后由 run 流负责调 _stage_merge，这里只验证批准不抛。"""
    await h._gate_merge_approval([UID])


def test_merge_approval_blocks_before_merging(tmp_path, monkeypatch):
    """审计 r3 should：merge-approval（动 main 前最后一闸）零测试。"""
    h = _harness(tmp_path, fake=False)
    merged = []

    async def _fake_merge():
        merged.append(True)
    monkeypatch.setattr(h, "_stage_merge", _fake_merge)

    async def _decide_abort(node, timeout_s):
        return "abort"
    monkeypatch.setattr(h.bus, "await_decision", _decide_abort)
    requests = []
    monkeypatch.setattr(
        h.bus, "request_decision",
        lambda node, brief, choices, **k: requests.append((node, choices)))

    with pytest.raises(HardStop) as exc:
        asyncio.run(h._gate_merge_approval([UID]))
    assert exc.value.kind == "ABORTED_BY_DECISION"
    assert requests and requests[0][0] == "merge-approval"
    assert merged == []  # 否决时绝不动 main

    async def _decide_ok(node, timeout_s):
        return "approve"
    monkeypatch.setattr(h.bus, "await_decision", _decide_ok)
    asyncio.run(_approve_then_merge(h))
