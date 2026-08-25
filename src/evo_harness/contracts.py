"""三契约的 CLI 生成器（控制面 JSON 一律程序生成，模型只填参数）。

设计原则（r5/r6 教训定稿）：
- **markdown 是模型的产物**（报告/研究/评审，自然语言内容）
- **JSON 是编排程序的结构化数据**，形状由本模块保证，模型通过 CLI 参数
  填内容，绝不手写 JSON 文件（手写两次死于形状：r5 数组/map、r1 缺文件）

planner agent 在任务里执行 `evo-harness contract ...` 登记三契约；
文件原子落 tasks/planner/out/，门禁照常校验（双保险）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ContractError(Exception):
    """契约登记违规（CLI 退出码 3）。"""


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_write(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(p)


class ContractStore:
    """run 级三契约存储：goal_spec / plan / allocations。"""

    def __init__(self, shared: Path, run_id: str) -> None:
        out = Path(shared) / run_id / "tasks" / "planner" / "out"
        self.goal_spec_p = out / "goal_spec.json"
        self.plan_p = out / "plan.json"
        self.allocs_p = out / "allocations.json"

    # ------------------------------------------------------------ 登记 ----

    def goal(self, goal: str, non_goals: list[str],
             criteria: list[str]) -> None:
        if not goal.strip():
            raise ContractError("goal 不能为空")
        if not criteria:
            raise ContractError("success_criteria 至少一条（可测试判据）")
        _atomic_write(self.goal_spec_p, {
            "goal": goal, "non_goals": non_goals,
            "success_criteria": criteria,
        })

    def step(self, step_id: str, title: str, after: list[str]) -> None:
        self._check_id(step_id, "step id")
        plan = _load(self.plan_p, {"steps": [], "merge_order": []})
        ids = [s["id"] for s in plan["steps"]]
        if step_id in ids:
            raise ContractError(f"step 重复: {step_id}")
        for dep in after:
            if dep not in ids:
                raise ContractError(f"依赖的 step 尚未登记: {dep}")
        plan["steps"].append(
            {"id": step_id, "title": title, "depends_on": after}
        )
        _atomic_write(self.plan_p, plan)

    def alloc(self, unit_id: str, branch: str, scope: list[str],
              criteria: list[str]) -> None:
        self._check_id(unit_id, "unit id")
        allocs = _load(self.allocs_p, {})
        if not isinstance(allocs, dict):
            raise ContractError("allocations.json 已被外部写坏（非 dict），"
                                "禁止手写 JSON，只能用 contract 命令登记")
        if unit_id in allocs:
            raise ContractError(f"unit 重复: {unit_id}")
        if not branch or "/" not in branch:
            raise ContractError(f"branch 需形如 <topic>/<name>: {branch!r}")
        if not scope:
            raise ContractError("scope 至少一个路径")
        allocs[unit_id] = {
            "branch": branch, "scope": scope, "success_criteria": criteria,
        }
        _atomic_write(self.allocs_p, allocs)

    def merge_order(self, order: list[str]) -> None:
        plan = _load(self.plan_p, {"steps": [], "merge_order": []})
        ids = [s["id"] for s in plan["steps"]]
        unknown = [u for u in order if u not in ids]
        if unknown:
            raise ContractError(f"merge_order 含未登记 step: {unknown}")
        if sorted(order) != sorted(ids):
            raise ContractError("merge_order 必须恰好覆盖全部 step（不重不漏）")
        plan["merge_order"] = order
        _atomic_write(self.plan_p, plan)
        # 计划完成标记（最后一步落）：CLI 逐条原子登记使「三文件存在」不再是
        # 完成信号（r7 实证：第一条 alloc 就齐了三文件，等待提前返回而
        # planner 还在继续登记）·merge-order 是模板收口步，由它落 COMPLETE
        _atomic_write(self.plan_p.parent / "COMPLETE.json",
                      {"merge_order": order, "units": len(
                          _load(self.allocs_p, {}))})

    # ------------------------------------------------------------ 校验 ----

    @staticmethod
    def _check_id(name: str, what: str) -> None:
        if not _UNIT_RE.match(name):
            raise ContractError(
                f"{what} 需匹配 [a-z0-9][a-z0-9-]*: {name!r}"
            )
