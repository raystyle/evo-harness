"""L2 阶段图编排（池化任务流）：常驻 Worker 池 + 任务队列 + 空闲派发。

资源模型（P7 定稿，win-rmux 常驻三 agent × herdr idle 派发）：
- run 启动拉起固定池（claude/kimi/codex）一次，此后零 spawn
- 阶段任务全部进队列（pool.run_tasks），谁空闲谁领
- 阶段间门禁（filebus 谓词）与条件路由不变；催写在 pool 内闭环
- 编程任务的 worktree 通过 prompt 内「工作目录」指令下达（池 spawn 时
  不能再指定 per-task cwd）

图（harness.md §3）：explore → research → plan → execute ⇄ review → merge
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from . import roles
from .config import HarnessConfig
from .driver import HarnessDriver
from .filebus import FileBus
from .pool import WorkerPool
from .statemachine import HardStop, StateMachine
from .units import ExecutionUnit, TaskSpec
from .worktrees import WorktreeRegistry


class Harness:
    """一次 run 的编排器：固定池 + 六阶段任务流。"""

    def __init__(self, config: HarnessConfig, run_id: str, repo_root: Path) -> None:
        self.config = config
        self.run_id = run_id
        self.repo_root = Path(repo_root).resolve()
        self.bus = FileBus(config.shared_root, run_id)
        self.bus.decision_notify_cmd = config.budget.decision_notify_cmd
        self.driver = HarnessDriver(config, run_id)
        self.sm = StateMachine(self.bus, config.budget)
        self.worktrees = WorktreeRegistry(self.repo_root, self.bus)
        self._fake = config.fake_agent_script is not None
        agents = ("fake",) if self._fake else config.fanout_agents
        self.pool = WorkerPool(self.driver, self.bus, agents, statemachine=self.sm)

    # ---------------------------------------------------------- 通用件 ----

    def _gate(self, stage: str) -> None:
        checks = {
            "explore": self.bus.explore_gate,
            "research": self.bus.research_gate,
            "plan": self.bus.plan_gate,
        }
        fn = checks.get(stage)
        if fn is None:
            return
        ok, detail = fn()
        self.bus.log_event("gate", f"{stage}: {'PASS' if ok else 'FAIL'} ({detail})")
        if not ok:
            raise HardStop("GATE_FAILED", f"{stage} 门禁未过: {detail}")

    async def _run_tasks_checked(self, stage: str,
                                 tasks: dict[str, tuple[str, Path]]) -> None:
        """并发跑一批任务（空闲派发）；单 unit 超时先重派一轮，再缺才升级。

        r6/r9 实证：慢 unit（长研究/长编码）撞 task_wait 一刀切，整 run 陪葬
        ，超时是「还没干完」不是「失败」，重派给它第二窗（阶段墙钟仍兜底）。
        """
        results = await self.pool.run_tasks(tasks)
        missing = [t for t, r in results.items() if r != "done"]
        if missing:
            self.bus.log_event(
                "revise", f"{stage} 超时重派（第二窗）: {missing}"
            )
            results.update(
                await self.pool.run_tasks({t: tasks[t] for t in missing})
            )
            missing = [t for t, r in results.items() if r != "done"]
        self.bus.log_event(
            "tasks_done", f"{stage}: {json.dumps(results, ensure_ascii=False)}"
        )
        if missing:
            raise HardStop("GATE_FAILED", f"{stage} 产物缺失: {missing}")

    # ---------------------------------------------------------- 阶段 ----

    async def _stage_explore(self, goal: str) -> None:
        exp = self.bus.stage_dir("explore")
        raw = exp / "raw"
        raw.mkdir(exist_ok=True)
        if self._fake:
            candidates = [
                {"repo": f"example/r{i}", "clone_url": f"https://x/{i}.git",
                 "relevance": 0.9 - i * 0.1}
                for i in range(3)
            ]
            await self._run_tasks_checked("explore", {
                "explore-rank": (
                    roles.fake_directive(str(self.bus.task_out("explore-rank", "candidates.json")), candidates),
                    self.bus.task_out("explore-rank", "candidates.json"),
                ),
            })
        else:
            await self._run_tasks_checked("explore", {
                f"explore-{s}": (
                    roles.render(
                        "explorer", goal=goal,
                        out=str(self.bus.task_out(f"explore-{s}", f"{s}.json")), extra=s,
                    ),
                    self.bus.task_out(f"explore-{s}", f"{s}.json"),
                )
                for s in ("repos", "code", "issues")
            })
            await self._run_tasks_checked("explore", {
                "explore-rank": (
                    roles.render(
                        "ranker", out=str(self.bus.task_out("explore-rank", "candidates.json")),
                        extra=str(raw),
                    ),
                    self.bus.task_out("explore-rank", "candidates.json"),
                ),
            })
        self._gate("explore")

    async def _stage_research(self, goal: str) -> None:
        exp = self.bus.stage_dir("research")
        exp.mkdir(parents=True, exist_ok=True)
        if self._fake:
            synthesis = "# synthesis（fake）\n\n" + ("可复用模式与风险清单。" * 20)
            await self._run_tasks_checked("research", {
                "research-synth": (
                    roles.fake_directive(
                        str(self.bus.task_out("research-synth", "synthesis.md")), {"x": 1},
                        text_writes={str(self.bus.task_out("research-synth", "synthesis.md")): synthesis},
                    ),
                    self.bus.task_out("research-synth", "synthesis.md"),
                ),
            })
        else:
            cand = self.bus.task_out("explore-rank", "candidates.json")
            candidates = json.loads(cand.read_text(encoding="utf-8"))
            tasks = {}
            for i, c in enumerate(candidates[:5]):
                out = self.bus.task_out(f"research-{i}", "summary.md")
                tasks[f"research-{i}"] = (
                    roles.render(
                        "researcher", goal=goal,
                        out=str(out),
                        extra=c.get("clone_url", ""),
                    ),
                    out,
                )
            await self._run_tasks_checked("research", tasks)
            await self._run_tasks_checked("research", {
                "research-synth": (
                    roles.render(
                        "synthesizer", out=str(self.bus.task_out("research-synth", "synthesis.md")),
                        extra=str(exp),
                    ),
                    self.bus.task_out("research-synth", "synthesis.md"),
                ),
            })
        self._gate("research")

    async def _gate_merge_approval(self, unit_ids: list[str]) -> None:
        """merge-approval 决策节点：review 全过也须主 agent 点头才动 main。"""
        auto = (getattr(self.config.budget, "decision_autopilot_s", 0.0)
                or 5.0) if self._fake else 0.0
        verdicts = {u: self._verdict_of(u).get("verdict") for u in unit_ids}
        self.bus.request_decision(
            "merge-approval",
            f"review 全过待合入：{len(unit_ids)} units（{verdicts}）",
            ["approve", "abort"],
            default="approve", timeout_s=auto if auto else 600.0,
        )
        if await self.bus.await_decision(
            "merge-approval", auto if auto else 0.0
        ) == "abort":
            raise HardStop("ABORTED_BY_DECISION", "主 agent 否决 merge")

    def _request_plan_approval(self) -> None:
        """plan→execute 决策节点：请求主 agent 批准。"""
        try:
            plan, allocs = self._load_plan()
            brief = (
                f"{len(plan['steps'])} steps / {len(allocs)} units，"
                f"merge_order: {' → '.join(plan['merge_order'])}；"
                f"synthesis: {self.bus.task_out('research-synth', 'synthesis.md').stat().st_size}B"
            )
        except Exception:
            brief = "三契约已过门禁（摘要提取失败，可直接查 out/）"
        node = "plan-approval"
        # autopilot 仅 fake/E2E；真 run timeout 0 = 无限期等主 agent decide
        auto = (getattr(self.config.budget, "decision_autopilot_s", 0.0) or 5.0) \
            if self._fake else 0.0
        self.bus.request_decision(
            node, brief, ["approve", "revise", "abort"],
            default="approve", timeout_s=auto if auto else 600.0,
        )

    def _plan_artifacts(self, complete_marker: bool = False) -> tuple[Path, ...]:
        """planner 完成信号 = 三契约全齐（单盯第一份会在 agent 写后续契约时
        误判 done 提前释放，r1/r4 实证）；真 agent 模式再加 COMPLETE.json
        （merge-order 登记时落，CLI 逐条原子写使「三文件存在」仍会过早
        触发，r7 实证）。"""
        names = ["goal_spec.json", "plan.json", "allocations.json"]
        if complete_marker:
            names.append("COMPLETE.json")
        return tuple(self.bus.task_out("planner", n) for n in names)

    async def _stage_plan(self, goal: str) -> None:
        plan_dir = self.bus.stage_dir("plan")
        if self._fake:
            payload = {
                "goal_spec.json": {
                    "goal": goal, "non_goals": [],
                    "success_criteria": ["fake 验收通过"],
                },
                "plan.json": {
                    "steps": [{"id": "impl", "title": "示例实现", "depends_on": []}],
                    "merge_order": ["exec-fake-01"],
                },
                "allocations.json": {
                    "exec-fake-01": {
                        "branch": "agent/feat-fake", "scope": ["src/**"],
                        "success_criteria": ["diff 非空"],
                    }
                },
            }
            out = lambda n: str(self.bus.task_out("planner", n))
            prompt = roles.fake_directive(
                out("plan.json"), payload["plan.json"],
                extra_writes={
                    out("goal_spec.json"): payload["goal_spec.json"],
                    out("allocations.json"): payload["allocations.json"],
                },
            )
            tasks = {"planner": (prompt, self._plan_artifacts())}
        else:
            prompt = roles.render(
                "planner", goal=goal, out=str(self.bus.task_dir("planner") / "out"),
                extra=str(self.bus.stage_dir("research")),
                run_id=self.run_id,
            )
            tasks = {"planner": (
                prompt, self._plan_artifacts(complete_marker=True)
            )}
        await self._run_tasks_checked("plan", tasks)
        self._gate("plan")

    # ------------------------------------------------- execute ⇄ review ----

    def _load_plan(self) -> tuple[dict, dict]:
        plan_dir = self.bus.task_dir("planner") / "out"
        plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        from .filebus import normalize_allocations
        allocs = normalize_allocations(
            json.loads((plan_dir / "allocations.json").read_text(encoding="utf-8"))
        )
        assert isinstance(allocs, dict)  # plan_gate 已验（到达即合法）
        return plan, allocs

    async def _stage_execute(self, goal: str,
                             only: list[str] | None = None) -> list[str]:
        plan, allocs = self._load_plan()
        targets = {
            uid: spec for uid, spec in allocs.items()
            if only is None or uid in only
        }
        tasks, unit_ids = {}, []
        for uid, spec in targets.items():
            # revise 重试轮必须保留上一轮 retries·每轮重建 unit.json 会把
            # 计数重置为 0，重试预算永不触发（claude r1 must-fix #1）
            prev_retries = self._unit_of(uid).retries if (
                self.bus.unit_dir(uid) / "unit.json"
            ).exists() else 0
            unit = ExecutionUnit(
                unit_id=uid, run_id=self.run_id, stage="execute", role="executor",
                agent="pool",
                retries=prev_retries,
                max_retries=self.config.budget.unit_max_retries,
                task_spec=TaskSpec(
                    title=spec.get("title", uid),
                    scope=spec.get("scope", []),
                    success_criteria=spec.get("success_criteria", []),
                ),
                artifact_dir=str(self.bus.unit_dir(uid)),
                result_path=str(self.bus.unit_result(uid)),
            )
            self.bus.unit_dir(uid).joinpath("unit.json").write_text(
                json.dumps(unit.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 重试轮：清上一轮完成信号
            self.bus.unit_result(uid).unlink(missing_ok=True)
            self.bus.review_verdict(uid).unlink(missing_ok=True)

            if self._fake:
                result = {
                    "unit_id": uid, "diff_summary": "fake diff",
                    "tests": "passed", "out_of_scope": [],
                    "ready_for_review": True,
                }
                prompt = roles.fake_directive(
                    str(self.bus.unit_result(uid)), result, sleep_s=1.5
                )
            else:
                wt = await asyncio.to_thread(
                    self.worktrees.ensure, uid,
                    spec.get("branch", f"agent/{uid}"),
                )
                prompt = (
                    f"工作目录：{wt}（所有命令先进入该目录执行）。"
                    + roles.render(
                        "executor", goal=goal, title=unit.task_spec.title,
                        criteria="；".join(unit.task_spec.success_criteria)
                        or "（规划未给）",
                        out=str(self.bus.unit_result(uid)),
                        extra="；".join(spec.get("scope", []))
                        or "（无 scope 约束）",
                    )
                )
            tasks[uid] = (prompt, self.bus.unit_result(uid))
            unit_ids.append(uid)

        await self._run_tasks_checked("execute", tasks)
        return unit_ids

    async def _stage_review(self, unit_ids: list[str]) -> str:
        fb = self.bus.stage_dir("review") / "feedback"
        fb.mkdir(parents=True, exist_ok=True)
        forced = ""
        if self._fake:
            force = self.bus.stage_dir("review") / "force_route"
            if force.exists():
                forced = force.read_text(encoding="utf-8").strip()
                force.unlink()
        tasks = {}
        for uid in unit_ids:
            verdict_path = self.bus.review_verdict(uid)
            verdict_path.unlink(missing_ok=True)
            task_id = f"review-{uid}"
            if self._fake:
                verdict = {
                    "unit_id": uid,
                    "verdict": forced or "pass",
                    "severity": "architectural" if forced == "reject" else "local",
                    "findings": [], "required_changes": [],
                }
                prompt = roles.fake_directive(str(verdict_path), verdict)
            else:
                prompt = roles.render(
                    "reviewer", extra=f"units/{uid}/（result.json + diff）",
                    out=str(verdict_path),
                )
            tasks[task_id] = (prompt, verdict_path)
        await self._run_tasks_checked("review", tasks)
        route = self.bus.review_route(unit_ids)
        self.bus.log_event("review_route", route)
        return route

    def _sync_tool_templates(self) -> None:
        """.evotools/evo_harness → skills/evo/templates/tools/evo_harness 双拷贝
        同步（集成门最常见的机械性失败，单元改了本体忘拷模板，r10 实证）。"""
        import shutil
        src = self.repo_root / ".evotools" / "evo_harness"
        dst = (self.repo_root / "skills" / "evo" / "templates" / "tools"
               / "evo_harness")
        if not (src.is_dir() and dst.is_dir()):
            return
        for sub in ("src", "scripts", "README.md", "pyproject.toml"):
            s_path, d_path = src / sub, dst / sub
            if s_path.is_dir():
                if d_path.exists():
                    shutil.rmtree(d_path)
                shutil.copytree(s_path, d_path)
            elif s_path.is_file():
                shutil.copy2(s_path, d_path)

    async def _stage_merge(self) -> None:
        plan, allocs = self._load_plan()
        unit_ids = list(allocs.keys())
        merge_dir = self.bus.stage_dir("merge")
        if self._fake:
            report = {"merged": unit_ids, "integration": "skipped(fake)"}
            await self._run_tasks_checked("merge", {
                "merger-fake": (
                    roles.fake_directive(str(self.bus.task_out("merger-fake", "report.json")), report),
                    self.bus.task_out("merger-fake", "report.json"),
                ),
            })
        else:
            report = await asyncio.to_thread(
                self.worktrees.merge_in_order,
                unit_ids, plan.get("merge_order", []),
            )
            # 逐条 merged 必须全真：merge_one 冲突只返回 (False, detail) 不抛错，
            # 不查就放行进 cleanup·git branch -D 把未合入分支删掉，静默丢工作
            # （codex r1 must-fix）
            failed = [r for r in report if not r.get("merged")]
            if failed:
                detail = "; ".join(
                    f"{r['unit_id']}: {r['detail'][:120]}" for r in failed
                )
                # 决策节点：merge 冲突处置交主 agent·skip-debt 跳过失败单元
                # 继续集成（债入事件流）/ abort 终止；fake 走 autopilot 默认
                auto = (getattr(self.config.budget,
                                "decision_autopilot_s", 0.0) or 5.0
                        if self._fake else 0.0)
                self.bus.request_decision(
                    "merge-conflict",
                    f"合并失败 units: {[r['unit_id'] for r in failed]}; {detail}",
                    ["skip-debt", "abort"],
                    default="skip-debt", timeout_s=auto if auto else 600.0,
                )
                choice = await self.bus.await_decision(
                    "merge-conflict", auto if auto else 0.0
                )
                if choice == "abort":
                    raise HardStop("GATE_FAILED", f"unit 分支合并失败: {detail}")
                self.bus.log_event(
                    "decision",
                    f"跳过失败 units 继续集成（债）: "
                    f"{[r['unit_id'] for r in failed]}"
                )
                unit_ids = [r["unit_id"] for r in report if r.get("merged")]
            ok, tail = await asyncio.to_thread(self.worktrees.integration_test)
            if not ok:
                # 自修回边（r10 实证死因）：双拷贝漂移是机械问题·程序同步
                # 模板重跑一次；仍败才升级（此时才是真回归）
                self.bus.log_event("repair", f"集成测试失败，尝试模板同步自修: {tail[:80]}")
                self._sync_tool_templates()
                ok, tail = await asyncio.to_thread(self.worktrees.integration_test)
                if not ok:
                    raise HardStop("GATE_FAILED", f"合并后集成测试失败(自修后仍败): {tail}")
            for uid in unit_ids:
                await asyncio.to_thread(self.worktrees.cleanup, uid)
            self.bus.task_out("merger", "report.json").write_text(
                json.dumps(
                    {"units": report, "integration": tail},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )

    # ------------------------------------------------------------- 主循环 ----

    async def _prepare(self) -> None:
        await self.driver.ensure_session()
        if not self._fake:
            from .install_hooks import install_all
            from .pretrust import pretrust_all

            installed = await asyncio.to_thread(
                install_all, self.repo_root,
                include_global=self.config.install_global_hooks,
            )
            self.bus.log_event("hooks", f"状态 hook: {installed}")
            wrote = await asyncio.to_thread(pretrust_all, self.repo_root)
            if any(wrote.values()):
                self.bus.log_event("pretrust", f"{self.repo_root}: {wrote}")
        await self.pool.start()

    async def run(self, goal: str) -> int:
        self.bus.set_goal(goal)  # 目标进 run.json（监控顶栏展示）
        self.bus.log_event("run_start", goal)
        started = time.time()
        try:
            self.sm.enter("prepare")
            await self._prepare()
            self.sm.enter("explore")
            await self._stage_explore(goal)
            self.sm.enter("research")
            await self._stage_research(goal)
            self.sm.enter("plan")
            await self._stage_plan(goal)

            # 关键决策节点（控制模型 v2）：plan 契约过门禁后，主 agent 参与
            # 批准决策·读 markdown 产物与契约摘要再放行，而非盲等最终产物；
            # 默认 approve 超时兜底，无人值守 run 不断流
            self._request_plan_approval()
            auto = (getattr(self.config.budget, "decision_autopilot_s", 0.0)
                    or 5.0) if self._fake else 0.0
            choice = await self.bus.await_decision(
                "plan-approval", auto if auto else 0.0
            )
            if choice == "abort":
                raise HardStop("ABORTED_BY_DECISION", "主 agent 否决 plan")
            if choice == "revise":
                self.bus.log_event("revise", "主 agent 要求重规划")
                await self._stage_plan(goal)
                self._gate("plan")

            revise_round: list[str] | None = None
            while True:
                self.sm.enter("execute")
                unit_ids = await self._stage_execute(goal, only=revise_round)
                self.sm.enter("review")
                route = await self._stage_review(unit_ids)
                nxt = self.sm.route_after_review(route)
                if nxt == "merge":
                    break
                if nxt == "plan":
                    self.sm.enter("plan")
                    await self._stage_plan(goal)
                    revise_round = None
                    continue
                revise_round = [
                    uid for uid in unit_ids
                    if self._verdict_of(uid).get("verdict") != "pass"
                ]
                self.bus.log_event("revise", f"重试 units: {revise_round}")
                if not revise_round:
                    break
                for uid in revise_round:
                    self._bump_retry(uid)

            # 决策节点（控制模型 v2）：合入 main 是重动作，主 agent 批准才动
            await self._gate_merge_approval(unit_ids)

            self.sm.enter("merge")
            await self._stage_merge()
            self.bus.set_stage("DONE", "done")
            self.bus.log_event("run_done", f"{time.time() - started:.1f}s")
            return 0
        except HardStop as exc:
            self.bus.set_stage("ESCALATE", "escalated")
            self.bus.log_event("hard_stop", f"{exc.kind}: {exc.detail}")
            print(f"[evo-harness] 硬停止 {exc.kind}: {exc.detail}", flush=True)
            return 2
        except Exception as exc:
            # rc-r1 must-fix：非 HardStop 异常（WorktreeError/OSError 等，
            # 非 git 目录、branch 重名、git 缺位皆可达）不得裸穿——
            # run.json 假活 running 会让 notifyd 无限空转、wait 等满超时
            self.bus.set_stage("ESCALATE", "escalated")
            self.bus.log_event(
                "hard_stop", f"UNCAUGHT {type(exc).__name__}: {exc}",
            )
            print(f"[evo-harness] 未预期异常 {type(exc).__name__}: {exc}",
                  flush=True)
            return 2
        finally:
            # 不变式④：执行单元（常驻窗格池）只在整任务终态后关闭
            try:
                await self.pool.shutdown()
                self.bus.log_event("pool", "常驻池已关闭（run 终态）")
            except Exception:
                pass
            self.bus.log_event("run_exit", f"{time.time() - started:.1f}s")

    # ------------------------------------------------------------ 辅助 ----

    def _verdict_of(self, uid: str) -> dict:
        p = self.bus.review_verdict(uid)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _unit_of(self, uid: str) -> ExecutionUnit:
        return ExecutionUnit.from_dict(
            json.loads(
                self.bus.unit_dir(uid).joinpath("unit.json").read_text(
                    encoding="utf-8"
                )
            )
        )

    def _save_unit(self, unit: ExecutionUnit) -> None:
        self.bus.unit_dir(unit.unit_id).joinpath("unit.json").write_text(
            json.dumps(unit.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _bump_retry(self, uid: str) -> None:
        """revise 一轮：重试计数 +1 并**落盘**，超上限硬停止。

        计数必须持久化，_stage_execute 每轮重建 unit.json，只改内存对象
        会让预算检查永远读到 0（claude r1 must-fix #1 的死代码根因）。
        """
        unit = self._unit_of(uid)
        unit.retries += 1
        if not self.sm.check_unit_retry(unit):
            raise HardStop(
                "BUDGET_EXCEEDED",
                f"unit {uid} 重试超上限 {unit.max_retries}",
            )
        self._save_unit(unit)

    async def abort(self) -> None:
        """跨进程可生效的终止：先停 detached 守护进程，再杀 rmux 会话，
        最后置终态（顺序不能反，活进程会把 aborted 覆盖回 running）。"""
        await asyncio.to_thread(self._kill_detached_pid)
        await self.driver.kill_session()
        self.bus.set_stage("ABORTED", "aborted")
        self.bus.log_event("abort", "人工终止")
        await asyncio.to_thread(self.worktrees.cleanup_all)
        from .worktrees import clean_run_worktrees
        cleaned = await asyncio.to_thread(
            clean_run_worktrees, self.repo_root, self.run_id
        )
        if cleaned:
            self.bus.log_event("abort", f"worktree 清场: {cleaned}")

    def _kill_detached_pid(self) -> None:
        """run --detach 写下的守护进程：独立会话（pid==pgid），整组 SIGTERM。"""
        pid_file = self.bus.root / "run.pid"
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        _terminate_pid(pid)

    # ------------------------------------------------- review-cycle ----

    async def review_cycle(self, goal: str, max_rounds: int = 3) -> int:
        """win-rmux review-cycle 的池化版：循环到三方一致。

        每轮：三 agent 并行 review（维度分工）→ host 聚合 must-fix →
        单 fixer 执行修复（跑 pytest+gate 作为成功标准）→ 下一轮 recheck。
        一致判据：三个最新报告全部 `VERDICT: AGREE`（无 must-fix）。
        """
        started = time.time()
        try:
            self.bus.set_goal(goal)  # 目标进 run.json（监控顶栏展示）
            self.sm.enter("prepare")
            await self._prepare()
            dimensions = {
                "claude": "正确性与实现质量（源码逻辑/边界/并发/测试覆盖）",
                "kimi": "完整性与文档（SKILL/references/CHANGELOG/门禁/安装路径）",
                "codex": "可生产性（部署/分发/宿主接入/CI/跨平台缺口）",
                "grok": "安全与健壮性（攻击面/边界输入/资源泄漏/依赖供应链）",
            }
            round_no = 0
            while True:  # extend 决策可动态加轮，审计 r3：range 定值使 extend 无效
                if round_no >= max_rounds:
                    # 决策节点（控制模型 v2）：轮次耗尽未一致，处置交主 agent
                    auto = (getattr(self.config.budget,
                                    "decision_autopilot_s", 0.0) or 5.0
                            if self._fake else 0.0)
                    self.bus.request_decision(
                        "escalate-review",
                        f"{max_rounds} 轮未达成一致（最近 must-fix 见各报告）",
                        ["extend", "finalize", "abort"],
                        default="finalize", timeout_s=auto if auto else 600.0,
                    )
                    choice = await self.bus.await_decision(
                        "escalate-review", auto if auto else 0.0
                    )
                    if choice == "extend":
                        max_rounds += 1
                        self.bus.log_event("decision", "主 agent 加轮，继续 review")
                        continue
                    if choice == "abort":
                        self.bus.log_event(
                            "hard_stop", f"主 agent 终止于 {round_no} 轮未一致"
                        )
                        await self._final_verdict(goal, round_no,
                                                  consensus=False)
                        self.bus.set_stage("ESCALATE", "escalated")  # 终态最后翻
                        return 2
                    await self._final_verdict(goal, round_no,
                                              consensus=False)
                    self.bus.set_stage("DONE", "done")  # 带债收尾（债在报告里）
                    return 0
                round_no += 1
                self.sm.enter("review")
                self.bus.log_event("round", f"第 {round_no}/{max_rounds} 轮 review 开始")
                tasks = {}
                for agent, dim in dimensions.items():
                    tid = f"rc-r{round_no}-{agent}"
                    out = self.bus.task_out(tid, "report.md")
                    out.unlink(missing_ok=True)
                    fixlist = ""
                    if round_no > 1:
                        fl = self.bus.root / f"tasks/round{round_no - 1}-fixlist.md"
                        if fl.exists():
                            fixlist = fl.read_text(encoding="utf-8")
                    if self._fake:
                        # fake：r1 出 must-fix（走修复回边），r2 起一致收敛
                        verdict = "AGREE" if round_no >= 2 else "REGRESSION"
                        body = (
                            f"# fake review（r{round_no}，维度：{dim}）\n\n"
                            + ("- [must-fix] demo 示例问题（r1 必出一轮走修复回边）\n"
                               if verdict == "REGRESSION" else "")
                            + f"\nVERDICT: {verdict}\n"
                        )
                        prompt = json.dumps(
                            {"sleep": 2, "write_text": {str(out.resolve()): body}},
                            ensure_ascii=False,
                        )
                        tasks[tid] = (prompt, out)
                        continue
                    prompt = (
                        f"# 第 {round_no} 轮生产就绪 review（维度：{dim}）\n\n"
                        f"目标：{goal}\n\n"
                        "审查对象：仓库当前状态（README、docs/、源码树、tests/、"
                        "CI 与构建配置；以仓内实际存在者为准，不预设布局）。\n"
                        "要求：逐文件抽查而非全读；每条发现标 "
                        "[must-fix]/[should-fix]/[ok]，给位置与一句话修法。\n"
                    )
                    if fixlist:
                        prompt += (
                            f"\n## 上一轮 must-fix 清单（先核对是否已修）\n{fixlist}\n"
                        )
                    prompt += (
                        f"\n输出写到 {out}（markdown）。最后一行必须是结论：\n"
                        "`VERDICT: AGREE`（无 must-fix，可生产）或 "
                        "`VERDICT: REGRESSION`（仍有 must-fix）。\n"
                    )
                    tasks[tid] = (prompt, out)
                await self._run_tasks_checked(f"review-r{round_no}", tasks)

                # host 聚合：must-fix 行 + 三方 verdict
                must, verdicts = [], []
                for agent in dimensions:
                    out = self.bus.task_out(f"rc-r{round_no}-{agent}", "report.md")
                    text = out.read_text(encoding="utf-8", errors="replace")
                    # verdict 严格化（claude r1 #5）：只认最后一个非空行
                    last = next(
                        (l.strip() for l in reversed(text.splitlines()) if l.strip()),
                        "",
                    )
                    verdicts.append(
                        "AGREE" if last.startswith("VERDICT: AGREE") else "REGRESSION"
                    )
                    for line in text.splitlines():
                        if "[must-fix]" in line:
                            must.append(f"- [{agent}] {line.strip()}")
                self.bus.log_event(
                    "aggregate",
                    f"r{round_no}: verdicts={verdicts} must-fix={len(must)}",
                )
                if all(v == "AGREE" for v in verdicts):
                    self.bus.log_event(
                        "consensus", f"第 {round_no} 轮三方一致 AGREE，可生产"
                    )
                    await self._final_verdict(goal, round_no, consensus=True)
                    # 终态必须最后翻：_final_verdict 会 enter(merge) 盖阶段
                    self.bus.set_stage("DONE", "done")
                    return 0

                # fixer：单任务修完本轮全部 must-fix（避免多 agent 并发写冲突）
                self.sm.enter("execute")
                fl_path = self.bus.root / f"tasks/round{round_no}-fixlist.md"
                fl_path.parent.mkdir(parents=True, exist_ok=True)
                fl_path.write_text(
                    "\n".join(must) or "（无 must-fix，仅 should-fix）",
                    encoding="utf-8",
                )
                fix_out = self.bus.task_out(f"rc-r{round_no}-fix", "result.json")
                fix_out.unlink(missing_ok=True)
                if self._fake:
                    fix_prompt = json.dumps(
                        {"sleep": 2, "write": {
                            str(fix_out.resolve()): {
                                "fixed": ["demo-fix（fake）"], "tests": "pass",
                                "gate": "pass",
                            }
                        }},
                        ensure_ascii=False,
                    )
                else:
                    fix_prompt = (
                    "# 修复任务\n\n按下列 must-fix 清单逐条修复仓库：\n"
                    f"{fl_path}\n\n"
                    "范围边界（硬约束）：改动逐条原子落地——每修完一条，仓库"
                    "保持可导入、全量测试通过，才可开下一条；严禁重构无关代码"
                    "（编排进程正在运行，半成品状态会破坏后续新进程，prod-r3"
                    "实证）。\n"
                    "每条修完跑本仓测试（如 `uv run pytest -q`，仓内如有专门"
                    "门禁脚本一并跑），全绿才算完成；必要处同步 CHANGELOG "
                    "[Unreleased]。\n"
                    f"完成后写 {fix_out}："
                    '{"fixed": ["每条一句话"], "tests": "pass|fail", '
                    '"gate": "pass|fail"}\n'
                )
                await self._run_tasks_checked(
                    f"fix-r{round_no}",
                    {f"rc-r{round_no}-fix": (fix_prompt, fix_out)},
                )

            # while True 只经决策/一致路径 return·无尾出口（审计 r3：
            # 旧 for 尾兜底曾把 extend 静默退化成空终审 + DONE）
        except HardStop as exc:
            self.bus.set_stage("ESCALATE", "escalated")
            self.bus.log_event("hard_stop", f"{exc.kind}: {exc.detail}")
            print(f"[evo-harness] 硬停止 {exc.kind}: {exc.detail}", flush=True)
            return 2
        except Exception as exc:
            # rc-r1 must-fix：同 run()——非 HardStop 异常一律落 ESCALATE 终态，
            # 不许 run.json 假活 running 拖死 notifyd/wait
            self.bus.set_stage("ESCALATE", "escalated")
            self.bus.log_event(
                "hard_stop", f"UNCAUGHT {type(exc).__name__}: {exc}",
            )
            print(f"[evo-harness] 未预期异常 {type(exc).__name__}: {exc}",
                  flush=True)
            return 2
        finally:
            try:
                await self.pool.shutdown()
            except Exception:
                pass
            self.bus.log_event("run_exit", f"{time.time() - started:.1f}s")

    async def _final_verdict(self, goal: str, rounds: int, consensus: bool) -> None:
        self.sm.enter("merge")  # 终审归入 merge 节点
        out = self.bus.task_out("rc-final", "verdict.md")
        reports = []
        for agent in ("claude", "kimi", "codex"):
            p = self.bus.task_out(f"rc-r{rounds}-{agent}", "report.md")
            if p.exists():
                reports.append(p.read_text(encoding="utf-8", errors="replace"))
        out.write_text(
            f"# 生产就绪终审（{rounds} 轮，"
            f"{'三方一致 AGREE' if consensus else '未达一致'}）\n\n目标：{goal}\n\n"
            + "\n\n---\n\n".join(reports),
            encoding="utf-8",
        )
        self.bus.log_event("final", f"终审报告: {out}")


def _terminate_pid(pid: int) -> None:
    """整组终止（POSIX）/ 单进程终止（Windows 无 getpgid/killpg，跳过组语义）。"""
    import os
    import signal

    try:
        if hasattr(os, "getpgid") and hasattr(os, "killpg"):
            if os.getpgid(pid) == pid:  # 会话组长：整组终止，不留子进程
                os.killpg(pid, signal.SIGTERM)
                return
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # 进程已退出 / 无权限：终态照置
