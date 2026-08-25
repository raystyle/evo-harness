"""evo_harness statemachine 路由与硬停止回归。"""

import pytest

from evo_harness.config import Budget
from evo_harness.filebus import FileBus
from evo_harness.statemachine import HardStop, StateMachine
from evo_harness.units import ExecutionUnit, TaskSpec


def _sm(tmp_path) -> StateMachine:
    bus = FileBus(tmp_path / "shared", "t1")
    return StateMachine(bus, Budget(), no_progress_interval=0)


def test_route_table_three_edges(tmp_path):
    sm = _sm(tmp_path)
    assert sm.route_after_review("PASS_ALL") == "merge"
    assert sm.route_after_review("LOCAL_FAIL") == "execute"
    assert sm.route_after_review("ARCH_FAIL") == "plan"
    with pytest.raises(HardStop):
        sm.route_after_review("WHATEVER")


def test_no_progress_hard_stop(tmp_path):
    sm = _sm(tmp_path)
    sm.enter("execute")
    budget = sm.budget
    for _ in range(budget.no_progress_rounds):
        sm.note_progress()  # 无 artifact 变化
    with pytest.raises(HardStop) as exc:
        sm.enter("review")
    assert exc.value.kind == "NO_PROGRESS"


def test_progress_resets_counter(tmp_path):
    sm = _sm(tmp_path)
    sm.enter("execute")
    (sm.bus.stage_dir("execute") / "x").parent.mkdir(parents=True, exist_ok=True)
    for i in range(sm.budget.no_progress_rounds + 2):
        (sm.bus.stage_dir("execute") / "x").write_text(str(i), encoding="utf-8")
        sm.note_progress()
    sm.enter("review")  # 不抛


def test_stage_budget_hard_stop(tmp_path):
    sm = _sm(tmp_path)
    sm.budget.stage_max_minutes = 0.0
    sm.enter("execute")
    with pytest.raises(HardStop) as exc:
        sm.check_stage_budget()
    assert exc.value.kind == "BUDGET_EXCEEDED"


def test_unit_retry_budget():
    unit = ExecutionUnit("u1", "r", "execute", "executor", "fake",
                         TaskSpec("t"), retries=2, max_retries=3)
    sm = object.__new__(StateMachine)  # 不需要 bus 的纯判定
    assert StateMachine.check_unit_retry(sm, unit) is True
    unit.retries = 3
    assert StateMachine.check_unit_retry(sm, unit) is False
