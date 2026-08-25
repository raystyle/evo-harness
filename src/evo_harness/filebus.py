"""File Bus（L5）：shared/ 目录契约 + 阶段门禁谓词。

交接规则（harness.md §6）：
- 下游只读上游**已确认**产物（过门禁后由控制面标记 confirmed）
- 关键事实不靠对话传递，全走文件
- 每个 unit 只写自己的 units/<id>/ 与被授权的阶段目录
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STAGES = ("explore", "research", "plan", "execute", "review", "merge")


def goal_brief(goal: str, limit: int = 44) -> str:
    """从 goal 提炼一句精练描述（生成任务时算好存 run.json.goal_brief）。

    取首个语义子句（最早的 。，（；换行 且子句 ≥8 字），超宽截断加 …。
    控制面确定性提取，不调模型，括号补充/逗号从句即细节边界 [实证: 目标文本形态]。
    """
    g = " ".join(goal.split())
    cuts = [i for i in (g.find(s) for s in ("。", "（", "，", "；", "\n")) if i >= 8]
    if cuts:
        g = g[: min(cuts)]
    if len(g) > limit:
        g = g[: limit - 1] + "…"
    return g


def normalize_allocations(data) -> dict | None:
    """allocations 归一化：list[{unit_id,...}] → {unit_id: {...}}（r5 实证：
    planner 按模板字面写出数组，语义对形状错）；dict 原样；垃圾返回 None。"""
    if isinstance(data, dict) and data:
        return data
    if (
        isinstance(data, list) and data
        and all(isinstance(x, dict) and x.get("unit_id") for x in data)
    ):
        return {
            x["unit_id"]: {k: v for k, v in x.items() if k != "unit_id"}
            for x in data
        }
    return None


class FileBus:
    """一次 run 的共享状态根：<shared_root>/<run_id>/。"""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = Path(root) / run_id
        self.run_id = run_id
        self.run_file = self.root / "run.json"
        self.root.mkdir(parents=True, exist_ok=True)
        for stage in STAGES:
            (self.root / "stages" / stage).mkdir(parents=True, exist_ok=True)
        (self.root / "units").mkdir(exist_ok=True)
        if not self.run_file.exists():
            self._write_run(
                {
                    "run_id": run_id,
                    "stage": "IDLE",
                    "status": "running",
                    "created_at": time.time(),
                    "history": [],
                }
            )

    # ------------------------------------------------------------ run.json ----

    def _write_run(self, data: dict) -> None:
        tmp = self.run_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.run_file)  # 原子写：控制面之外读不到半截状态

    def read_run(self) -> dict:
        return json.loads(self.run_file.read_text(encoding="utf-8"))

    def set_stage(self, stage: str, status: str = "running") -> None:
        data = self.read_run()
        data["stage"] = stage
        data["status"] = status
        self._write_run(data)

    def set_goal(self, goal: str) -> None:
        """任务目标持久化（监控可视化读取）：全文 + 生成任务时提炼的一句精练描述。"""
        data = self.read_run()
        data["goal"] = goal
        data["goal_brief"] = goal_brief(goal)
        self._write_run(data)

    def log_event(self, kind: str, detail: str = "") -> None:
        data = self.read_run()
        data["history"].append(
            {"t": round(time.time(), 3), "kind": kind, "detail": detail}
        )
        self._write_run(data)

    # ------------------------------------------------------------- 路径 ----

    def stage_dir(self, stage: str) -> Path:
        return self.root / "stages" / stage

    def task_dir(self, task_id: str) -> Path:
        """任务目录（win-rmux 任务派发模型）：prompt.md + out/ 产物。"""
        d = self.root / "tasks" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def task_out(self, task_id: str, name: str) -> Path:
        """任务产物路径：tasks/<task-id>/out/<name>（产物按任务归档）。"""
        d = self.task_dir(task_id) / "out"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def prompt_file(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "prompt.md"

    def unit_dir(self, unit_id: str) -> Path:
        d = self.root / "units" / unit_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def unit_result(self, unit_id: str) -> Path:
        """unit 完成信号（任务产物，出现即视为该 unit done）。"""
        return self.task_out(unit_id, "result.json")

    def review_verdict(self, unit_id: str) -> Path:
        # 复核独立任务名 review-<uid>：与 execute 任务同名会被 drive 的
        # 残留回显去重骗过（tag 前缀必须全局唯一，实证 tagfix-1）
        return self.task_out(f"review-{unit_id}", "verdict.json")

    # ------------------------------------------------------------ 决策节点 ----

    def request_decision(self, node: str, brief: str, choices: list[str],
                         default: str, timeout_s: float = 600.0) -> Path:
        """主 agent 决策节点：编排程序到关键节点请主 agent 参与决策，
        而非盲等最终产物（默认策略超时兜底，无人值守 run 不断流）。"""
        d = self.root / "decisions"
        d.mkdir(exist_ok=True)
        p = d / f"{node}.json"
        p.write_text(json.dumps({
            "node": node, "status": "pending", "brief": brief,
            "choices": choices, "default": default, "timeout_s": timeout_s,
            "requested_at": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log_event(
            "decision", f"{node} 请求决策（等主 agent）: {brief}",
        )
        # shell 回调：通知通道自由接入（notify-send / 终端钟声 / herdr …）
        cmd = getattr(self, "decision_notify_cmd", "")
        if cmd:
            import os
            import subprocess as sp
            env = {
                **os.environ,
                "EVO_RUN": self.run_id, "EVO_NODE": node,
                "EVO_BRIEF": brief, "EVO_CHOICES": ",".join(choices),
            }
            try:
                sp.run(cmd, shell=True, env=env,
                       capture_output=True, timeout=10)
            except (sp.SubprocessError, OSError):
                pass  # 通知失败不阻塞决策流
        return p

    def read_decision(self, node: str) -> dict:
        try:
            return json.loads(
                (self.root / "decisions" / f"{node}.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            return {}

    def decide(self, node: str, choice: str, rationale: str = "") -> bool:
        d = self.read_decision(node)
        if d.get("status") != "pending":
            return False
        if choice not in d.get("choices", []):
            return False
        d.update({"status": "decided", "choice": choice,
                  "rationale": rationale, "decided_at": time.time()})
        (self.root / "decisions" / f"{node}.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.log_event(
            "decision",
            f"{node} → {choice}" + (f"（{rationale[:60]}）" if rationale else ""),
        )
        return True

    async def await_decision(self, node: str, timeout_s: float) -> str:
        """等决策落 choice（inotify 事件驱动）。

        timeout_s <= 0：无限期等主 agent（真 run 语义，批准是责任）；
        >0：超时自动落 default（fake/E2E autopilot）。
        """
        from .events import wait_for_files as aff

        def _ok() -> bool:
            return self.read_decision(node).get("status") == "decided"

        ddir = self.root / "decisions"
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while True:
            wait = 30.0 if timeout_s <= 0 else max(0.5, deadline - time.monotonic())
            await aff(ddir if ddir.exists() else self.root, _ok, wait)
            d = self.read_decision(node)
            if d.get("status") == "decided":
                return d["choice"]
            if timeout_s > 0 and time.monotonic() >= deadline:
                self.decide(node, d.get("default", ""), "超时默认(autopilot)")
                return d.get("default", "")

    # --------------------------------------------------------- 门禁谓词 ----

    def explore_gate(self, min_candidates: int = 3) -> tuple[bool, str]:
        p = self.task_out("explore-rank", "candidates.json")
        if not p.exists():
            return False, "缺 candidates.json"
        try:
            items = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return False, f"candidates.json 解析失败: {exc}"
        ok = [
            it
            for it in items
            if isinstance(it, dict) and it.get("clone_url")
        ]
        if len(ok) < min_candidates:
            return False, f"候选仅 {len(ok)} 个（要求 >= {min_candidates} 且含 clone_url）"
        return True, f"{len(ok)} 个候选就绪"

    def research_gate(self) -> tuple[bool, str]:
        synth = self.task_out("research-synth", "synthesis.md")
        if not synth.exists():
            return False, "缺 synthesis.md"
        if len(synth.read_text(encoding="utf-8", errors="replace").strip()) < 200:
            return False, "synthesis.md 过短（<200 字符），疑似空壳"
        return True, "synthesis.md 就绪"

    def plan_gate(self) -> tuple[bool, str]:
        names = ("goal_spec.json", "plan.json", "allocations.json")
        plan_dir = self.task_dir("planner") / "out"
        for name in names:
            p = plan_dir / name
            if not p.exists():
                return False, f"缺契约 {name}"
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return False, f"{name} 不是合法 JSON: {exc}"
        plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            return False, "plan.json 不是 dict"
        if not plan.get("merge_order"):
            return False, "plan.json 缺 merge_order"
        if not plan.get("steps"):
            return False, "plan.json 缺 steps"
        # allocations 必须是非空 dict：[] / 字符串 / null 会让下游 .items() 抛
        # AttributeError（run 只捕 HardStop → run.json 永久 running）；{} 则
        # 零工作静默 DONE（claude r2 must-fix）
        allocs = normalize_allocations(
            json.loads((plan_dir / "allocations.json").read_text(encoding="utf-8"))
        )
        if allocs is None:
            return False, "allocations.json 不是非空 dict（也不可归一化为 dict）"
        bad = [k for k, v in allocs.items() if not isinstance(v, dict)]
        if bad:
            return False, f"allocations 的 spec 不是 dict: {bad}"
        return True, "三契约齐备"

    def execute_gate(self, unit_ids: list[str]) -> tuple[bool, list[str]]:
        """所有 unit 的 result.json 齐全且 ready_for_review。"""
        missing, not_ready = [], []
        for uid in unit_ids:
            p = self.unit_result(uid)
            if not p.exists():
                missing.append(uid)
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                not_ready.append(uid)
                continue
            if not data.get("ready_for_review", False):
                not_ready.append(uid)
        done = not missing and not not_ready
        return done, missing + not_ready

    def review_route(self, unit_ids: list[str]) -> str:
        """读全部 verdict，给路由判定：PASS_ALL / LOCAL_FAIL / ARCH_FAIL。"""
        verdicts = {}
        for uid in unit_ids:
            p = self.review_verdict(uid)
            if p.exists():
                try:
                    verdicts[uid] = json.loads(
                        p.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    verdicts[uid] = {"verdict": "revise", "severity": "local"}
        if not verdicts:
            return "LOCAL_FAIL"
        v_list = [v.get("verdict", "revise") for v in verdicts.values()]
        if any(v == "reject" for v in v_list):
            return "ARCH_FAIL"
        if all(v == "pass" for v in v_list):
            return "PASS_ALL"
        return "LOCAL_FAIL"

    # ------------------------------------------------------- 无进展指纹 ----

    def fingerprint(self) -> str:
        """artifact 树的 mtime+size 指纹（NO_PROGRESS 检测用）。

        纳秒级 mtime：整秒精度会把同一秒内的多轮写入误判为无变化。
        """
        parts = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and p.name != "run.json" \
                    and "_pool" not in p.parts:  # 池状态翻转不是进展（claude r1 #7）
                st = p.stat()
                parts.append(
                    f"{p.relative_to(self.root)}:{st.st_size}:{st.st_mtime_ns}"
                )
        return "|".join(parts)
