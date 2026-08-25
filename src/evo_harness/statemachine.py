"""L1 控制面：阶段状态机 + 路由表 + 硬停止。

状态图（harness.md §7）：
  IDLE → EXPLORE_RUNNING → RESEARCH_RUNNING → PLAN_RUNNING
       → EXECUTE_RUNNING → REVIEW_RUNNING → ROUTE:
            PASS_ALL    → MERGE_RUNNING → DONE
            LOCAL_FAIL  → EXECUTE_RUNNING（对应 unit 带 feedback 重试）
            ARCH_FAIL   → PLAN_RUNNING（回规划，可能重切 worktree）
            BUDGET_EXCEEDED / NO_PROGRESS → ESCALATE（exit 2，人工介入）

硬停止四条全部在 advance()/loop 保护里强制执行，不依赖模型自觉：
  每 unit max_retries / 每阶段 max_minutes / 全局 max_minutes / 无进展指纹
"""

from __future__ import annotations

import time

# 复合状态："<STAGE>_RUNNING" 为执行态；GATE 判定后由本模块翻到下一态
STAGE_ORDER = ("explore", "research", "plan", "execute", "review", "merge")

ROUTE = {
    "PASS_ALL": "merge",
    "LOCAL_FAIL": "execute",   # unit 级 revise 重试（不清空其它 unit 进度）
    "ARCH_FAIL": "plan",       # 架构/目标级失败回规划
}


class HardStop(Exception):
    """硬停止触发（预算/无进展）。携带 reason 供 run.json 记录。"""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class StateMachine:
    def __init__(self, bus, budget, no_progress_interval: float = 60.0) -> None:
        """no_progress_interval：指纹采样间隔（秒）。

        真实 agent 一轮思考可能数分钟不落盘——按轮询间隔（2s）数「无变化」
        会误杀；按本间隔采样、连续 budget.no_progress_rounds 次无变化才停。
        """
        self.bus = bus
        self.budget = budget
        self.no_progress_interval = no_progress_interval
        self._stage_started = time.time()
        self._run_started = time.time()
        self._last_fingerprint = ""
        self._no_progress_rounds = 0
        self._last_fp_check = 0.0

    # ------------------------------------------------------------ 推进 ----

    def enter(self, stage: str) -> None:
        self._check_hard_stops()
        self._stage_started = time.time()
        self.bus.set_stage(f"{stage.upper()}_RUNNING")
        self.bus.log_event("enter_stage", stage)

    def stage_elapsed(self) -> float:
        return time.time() - self._stage_started

    def route_after_review(self, verdict_route: str) -> str:
        """review 后的条件边。返回下一 stage 名。"""
        self.bus.log_event("route", verdict_route)
        nxt = ROUTE.get(verdict_route)
        if nxt is None:
            raise HardStop(
                "NO_ROUTE", f"未知路由判定: {verdict_route}"
            )
        return nxt

    # ------------------------------------------------------------ 硬停止 ----

    def reset_progress(self) -> None:
        """worker 正在干活时清零无进展计数（状态权威于指纹，herdr 教训：
        长任务执行期产物树静止是常态，不是 NO_PROGRESS）。"""
        self._no_progress_rounds = 0

    def note_progress(self) -> None:
        """observe 轮询回调：按采样间隔评估指纹，变了清零计数。"""
        now = time.time()
        if now - self._last_fp_check < self.no_progress_interval:
            return
        self._last_fp_check = now
        fp = self.bus.fingerprint()
        if fp != self._last_fingerprint:
            self._last_fingerprint = fp
            self._no_progress_rounds = 0
        else:
            self._no_progress_rounds += 1

    def check_unit_retry(self, unit) -> bool:
        """unit revise 重试是否仍在预算内。"""
        return unit.retries < unit.max_retries

    def _check_hard_stops(self) -> None:
        now = time.time()
        if now - self._run_started > self.budget.global_max_minutes * 60:
            raise HardStop(
                "BUDGET_EXCEEDED",
                f"全局墙钟 {self.budget.global_max_minutes}min 已过",
            )
        # 阶段墙钟由 stages 在 observe 循环里查 stage_elapsed；这里只管全局与无进展。
        if self._no_progress_rounds >= self.budget.no_progress_rounds:
            raise HardStop(
                "NO_PROGRESS",
                f"连续 {self._no_progress_rounds} 轮 artifact 无变化",
            )

    def check_stage_budget(self) -> None:
        if self.stage_elapsed() > self.budget.stage_max_minutes * 60:
            raise HardStop(
                "BUDGET_EXCEEDED",
                f"阶段墙钟 {self.budget.stage_max_minutes}min 已过",
            )
