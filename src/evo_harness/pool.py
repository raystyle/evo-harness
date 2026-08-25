"""常驻 Worker 池 + 任务队列（win-rmux 常驻三 agent × herdr 空闲派发的合成）。

资源模型（P7 定稿）：
- run 启动时**一次性**拉起固定池（claude/kimi/codex 各一 pane，含
  预信任/hook/就绪/对话框处置），此后零 spawn，争锁窗口只发生一次
- 所有阶段产物任务进队列；**谁空闲谁领下一个任务**（idle 派发）
- 任务完成 = 产物文件出现（权威）；hook 状态通道用于 worker 空闲确认
  （Stop→idle）与 status 展示

一个 worker = 一个常驻 pane。任务（unit）的产物路径与六阶段门禁完全不变。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .driver import HarnessDriver
from .filebus import FileBus


@dataclass
class Worker:
    name: str          # worker-claude / worker-kimi / worker-codex / worker-fake-N
    agent: str
    pane: object = None
    state_file: Path | None = None
    current_task: str = ""
    tasks_done: int = 0
    busy: bool = False


class WorkerPool:
    """固定池：acquire() 阻塞等空闲 worker，release() 归还。"""

    def __init__(self, driver: HarnessDriver, bus: FileBus, agents: tuple[str, ...],
                 statemachine=None) -> None:
        self.driver = driver
        self.bus = bus
        self.agents = agents
        self.sm = statemachine  # 硬停止接线（claude r1 #3：护栏不能是死代码）
        self.workers: list[Worker] = []
        self._idle: asyncio.Queue[Worker] = asyncio.Queue()
        self._started = False

    # ------------------------------------------------------------- 生命周期 ----

    async def start(self, stage_window: str = "pool") -> None:
        """一次性拉起全池（顺序 spawn 保 split 有序，就绪并发等）。"""
        if self._started:
            return
        self._started = True
        t0 = time.time()
        for agent in self.agents:
            w = Worker(name=f"worker-{agent}", agent=agent)
            w.state_file = self.bus.unit_dir(f"_pool/{w.name}") / "state.json"
            w.pane = await self.driver.spawn_unit(
                stage_window, agent, unit_id=w.name, state_file=w.state_file,
            )
            # 持久化 worker→pane 映射（monitor 下钻实时屏幕用）
            self.bus.unit_dir(f"_pool/{w.name}").joinpath("worker.json").write_text(
                json.dumps({"agent": agent, "pane": str(getattr(w.pane, "target", ""))}),
                encoding="utf-8",
            )
            self.workers.append(w)

        async def _ready(w: Worker) -> None:
            ok = await self.driver.wait_ready(
                w.pane, w.agent, state_file=w.state_file
            )
            if not ok:
                ok = await self.driver.wait_ready(
                    w.pane, w.agent, timeout=150.0, state_file=w.state_file
                )
            if not ok:
                from .statemachine import HardStop
                raise HardStop("LAUNCH_FAILED", f"{w.name}（{w.agent}）未就绪")
            n = await self.driver.resolve_dialogs(w.pane, w.agent)
            if n:
                self.bus.log_event("dialogs", f"{w.name} 处置 {n} 个确认框")

        await asyncio.gather(*(_ready(w) for w in self.workers))
        for w in self.workers:
            w.busy = False
            self._idle.put_nowait(w)
        self.bus.log_event(
            "pool", f"常驻池就绪 {len(self.workers)} worker（{time.time()-t0:.0f}s）: "
            + ", ".join(w.name for w in self.workers)
        )

    async def shutdown(self) -> None:
        await self.driver.kill_session()

    # ------------------------------------------------------------- 派发 ----

    async def acquire(self) -> Worker:
        """阻塞等一个空闲 worker（谁空闲谁执行）。"""
        w = await self._idle.get()
        w.busy = True
        return w

    def release(self, w: Worker) -> None:
        w.busy = False
        w.tasks_done += 1
        self._idle.put_nowait(w)

    async def run_task(self, task_id: str, prompt: str, artifact: Path | tuple[Path, ...],
                       cwd: str | None = None, nudge_rounds: int | None = None) -> tuple[str, bool]:
        """单个任务：acquire → 领活登记 → 写任务文件 → drive 短指令 → 等产物 → release。

        提示词走文件（P7 定稿）：完整 prompt（角色模板+任务+输出契约，多行
        无碍）写入 shared/<run>/tasks/<task_id>.md，drive 只发一句短的
        「读取任务文件并执行」，send-keys 长度/转义/折形问题一劳永逸。
        fake worker 收 "FILE <path>" 行，从文件读 FAKE 指令。
        """
        from .statemachine import HardStop

        if nudge_rounds is None:
            nudge_rounds = getattr(
                getattr(self.sm, "budget", None), "nudge_rounds", 2
            )
        w = await self.acquire()
        w.current_task = task_id
        self._set_current(w, task_id)
        task_file = self.bus.prompt_file(task_id)  # tasks/<id>/prompt.md
        task_file.write_text(prompt, encoding="utf-8")
        self.bus.log_event(
            "dispatch", f"{task_id} → {w.name}（{w.agent}）file={task_file.name}"
        )
        try:
            tui = w.agent != "fake"
            # 消息带任务唯一前缀：drive 的「迟到确认」按 head 去重，相同前缀
            # （如 FILE /tmp）会被上一任务的残留回显骗过而跳过发送（实证）
            tag = f"[{task_id}]"
            if tui:
                reader = (
                    f"{tag} 请读取任务文件 {task_file.resolve()} 并严格执行"
                    f"其中全部指令，完成即止。"
                )
            else:
                reader = f"{tag} FILE {task_file.resolve()}"
            baseline = self._state_sig(w)
            ok = await self.driver.drive(
                w.pane, reader, tui=tui,
                state_file=w.state_file if tui else None,
            )
            if not ok:
                # 失败留证（r2 实证：硬停止即关会话，pane 屏幕证据全灭无法复盘）
                # + 处置对话框后重试一次（信任框/错误模态框会吞掉提交文本）
                try:
                    snap = self.bus.task_dir(task_id) / "drive-fail.txt"
                    snap.write_text(
                        await self.driver.screen(w.pane), encoding="utf-8"
                    )
                    self.bus.log_event(
                        "drive_fail", f"{task_id} @ {w.name} 屏幕快照: {snap}"
                    )
                except Exception:
                    pass
                n = await self.driver.resolve_dialogs(w.pane, w.agent)
                if n:
                    self.bus.log_event(
                        "dialogs", f"{w.name} 处置 {n} 个确认框（drive 失败后重试前）"
                    )
                    ok = await self.driver.drive(
                        w.pane, reader, tui=tui,
                        state_file=w.state_file if tui else None,
                    )
            if not ok:
                raise HardStop("DRIVE_FAILED", f"{task_id} 提交失败 @ {w.name}")
            # P8.2 提交级 stalled：非 working 提交后 5s 无 hook 生命周期变化
            # 重提一次任务行，而不是等 120s 死会话阈值才反应。
            await self._ensure_prompt_effect(w, task_id, reader, baseline)
            done = await self._wait_artifact(
                w, task_id, artifact, nudge_rounds, reader=reader
            )
            # 任务完成标记（权威 end 信号）：monitor 据此显「开始→结束」，
            # 免得多产物任务写了一半就被「任意产物」启发式误判完成
            (self.bus.task_dir(task_id) / "done.json").write_text(
                json.dumps({"ok": done, "at": time.time()}),
                encoding="utf-8",
            )
            return task_id, done
        finally:
            if w.state_file and w.agent != "fake":
                await self.driver.wait_state(w.state_file, "idle", 10.0)
            w.current_task = ""
            self._set_current(w, "")
            self.release(w)

    async def run_tasks(self, tasks: dict[str, tuple[str, Path]],
                        on_wake=None) -> dict[str, str]:
        """并发跑一批任务：{task_id: (prompt, artifact)} → {task_id: done|timeout}。

        队列派发：worker 空闲即领；无空闲则任务在 acquire 处排队。
        """
        results = await asyncio.gather(
            *(self.run_task(tid, prompt, artifact)
              for tid, (prompt, artifact) in tasks.items()),
            return_exceptions=True,
        )
        out: dict[str, str] = {}
        for r in results:
            if isinstance(r, Exception):
                raise r
            tid, done = r
            out[tid] = "done" if done else "timeout"
        return out

    # ------------------------------------------------------------- 内部 ----

    def _set_current(self, w: Worker, task_id: str) -> None:
        d = self.bus.unit_dir(f"_pool/{w.name}")
        (d / "current.json").write_text(
            json.dumps({"task": task_id, "agent": w.agent}, ensure_ascii=False),
            encoding="utf-8",
        )

    async def _wait_artifact(self, w: Worker, task_id: str, artifact: Path,
                             nudge_rounds: int, reader: str | None = None) -> bool:
        """等产物（含三轮自愈：stalled 重提 / idle 催写）。"""
        from .events import AsyncFileWatcher

        def _guard(_) -> None:
            # 硬停止护栏接线（claude r1 #3）：无进展指纹 + 阶段墙钟。
            # worker 正在干活（hook 态 working）时清零无进展·产物静止
            # 是长任务常态（prod-r2 实证：fixer 干 17 分钟被误计 17 轮）
            if self.sm is not None:
                if self._read_state(w) == "working":
                    self.sm.reset_progress()
                else:
                    self.sm.note_progress()
                self.sm.check_stage_budget()

        wait_s = getattr(getattr(self.sm, "budget", None), "task_wait_seconds", 120.0)
        watch_dir = (
            artifact[0].parent if isinstance(artifact, tuple)
            else (artifact.parent if artifact.parent.exists()
                  else artifact.parents[-2])
        )
        # 30s 切片轮询（r5 实证：900s 一档的轮边界太钝·worker 已 idle 且
        # 产物缺，本可立即催写，却要干等下一个轮边界才被看见）
        deadline = time.monotonic() + wait_s
        nudges_left = nudge_rounds
        last_nudge = 0.0
        round_no = 0
        while True:
            slice_s = min(30.0, max(0.5, deadline - time.monotonic()))
            watcher = AsyncFileWatcher(watch_dir)
            try:
                await watcher.wait_until(
                    lambda: self._artifacts_ok(artifact), slice_s, on_wake=_guard
                )
            finally:
                watcher.close()
            if self._artifacts_ok(artifact):
                return True
            now = time.monotonic()
            if now >= deadline:
                return False
            # 自愈 1（P8.2 stalled v1）：提交死会话·userpromptsubmit 后零后续
            # hook 事件（没调过任何工具）超阈值 = 会话没起来（k3 config.invalid
            # 实证：卡 15 分钟 nudge 永远被 working 态挡住）→ 重提任务行
            if (reader and nudges_left > 0 and self._submit_stale(w)
                    and now - last_nudge >= 60.0):
                nudges_left -= 1   # 审计 r3 must-fix：三路自愈共享总预算，
                last_nudge = now   # 死会话也不许每 30s 无限重提到 900s
                self.bus.log_event(
                    "stalled", f"{task_id} @ {w.name} 提交疑似未生效（"
                    f"零工具调用超 {self._stalled_s():.0f}s），第{round_no+1}轮重提"
                )
                await self.driver.drive(
                    w.pane, reader,
                    tui=w.agent != "fake",
                    state_file=w.state_file if w.agent != "fake" else None,
                )
                round_no += 1
                continue
            # 自愈 1.5（r6 死态）：hook 通道整体静默·重提任务行（会进
            # 消息队列，agent 收尾后 drain 处理；幂等前缀可去重）
            if (reader and nudges_left > 0 and self._worker_silent(w)
                    and now - last_nudge >= 60.0):
                nudges_left -= 1
                last_nudge = now
                round_no += 1
                self.bus.log_event(
                    "stalled", f"{task_id} @ {w.name} hook 通道静默超 "
                    f"{self._silent_s():.0f}s，重提任务行"
                )
                await self.driver.drive(
                    w.pane, reader,
                    tui=w.agent != "fake",
                    state_file=w.state_file if w.agent != "fake" else None,
                )
                continue
            # 自愈 2：催写·worker 已 idle（真做完没写/幻写）且距上次催 ≥60s
            # 即催，不等轮边界；催满 nudge_rounds 次后只能等超时
            state = self._read_state(w)
            if state not in ("idle", None) or nudges_left <= 0:
                continue
            if now - last_nudge < 60.0:
                continue
            nudges_left -= 1
            last_nudge = now
            round_no += 1
            self.bus.log_event("nudge", f"{task_id} @ {w.name} 第{round_no}轮催写")
            await self.driver.drive(
                w.pane,
                f"[{task_id}] 你上一轮的结果没有落盘。立即用文件写入工具把最终"
                f"结果写入 {self._fmt_missing(artifact)}"
                f"（文件名逐字使用，禁止自创），写完即止。",
                tui=w.agent != "fake",
                state_file=w.state_file if w.agent != "fake" else None,
            )

    # ------------------------------------------------- 提交级 effect gate ----

    def _prompt_effect_s(self) -> float:
        return float(getattr(
            getattr(self.sm, "budget", None), "prompt_effect_seconds", 5.0
        ))

    def _state_sig(self, w: Worker) -> tuple[str | None, str | None, float]:
        """hook 状态通道的完整生命周期指纹：state/event/ts 三元组。"""
        if not w.state_file or not w.state_file.exists():
            return (None, None, 0.0)
        try:
            d = json.loads(w.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return (None, None, 0.0)
        try:
            ts = float(d.get("ts", 0.0))
        except (TypeError, ValueError):
            ts = 0.0
        return (d.get("state"), d.get("event"), ts)

    async def _wait_lifecycle(self, w: Worker, baseline: tuple,
                              timeout: float) -> bool:
        """等 hook 状态指纹相对 baseline 发生变化（herdr state_change_seq 同思路）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._state_sig(w) != baseline:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.25, remaining))

    async def _ensure_prompt_effect(self, w: Worker, task_id: str,
                                    reader: str, baseline: tuple) -> None:
        """提交级 stalled：非 working 提交后 5s 内 hook 无生命周期变化 -> 重提一次。

        herdr `agent_prompt_stalled`/orca `workingSequence` 的双源同值（5s）。
        只对 tui 且已装 hook 通道的 worker 生效；fake 无 hook 直接跳过。
        """
        if w.agent == "fake" or not w.state_file or not w.state_file.exists():
            return
        if baseline[0] == "working":
            return
        if await self._wait_lifecycle(w, baseline, self._prompt_effect_s()):
            return
        self.bus.log_event(
            "stalled",
            f"{task_id} @ {w.name} 提交后 {self._prompt_effect_s():.0f}s "
            "无 hook 生命周期变化，重提任务行",
        )
        await self.driver.drive(
            w.pane, reader, tui=True, state_file=w.state_file,
        )

    def _stalled_s(self) -> float:
        return float(getattr(
            getattr(self.sm, "budget", None), "submit_stalled_seconds", 120.0
        ))

    def _silent_s(self) -> float:
        return float(getattr(
            getattr(self.sm, "budget", None), "worker_silent_seconds", 300.0
        ))

    def _worker_silent(self, w: Worker) -> bool:
        """hook 通道整体静默：state.json 超阈值无任何新事件（r6 实证第三种
        死态，state 卡 working、Stop 未触发、消息队列路径 hook 失明，
        idle 催写与 userpromptsubmit 重提两路都探测不到）。"""
        if not w.state_file or not w.state_file.exists():
            return False
        try:
            d = json.loads(w.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (time.time() - float(d.get("ts", 0))) > self._silent_s()

    def _submit_stale(self, w: Worker) -> bool:
        """提交死会话判定：最后一个 hook 事件是 userpromptsubmit 且超阈值。

        活跃会话在思考时同样无 PreToolUse，但超过 stalled 秒（默认 120s）
        仍零工具调用，大概率是会话创建失败（模型配置/登录态问题）。
        宁可重提一次（幂等：任务行带 [task-id] 前缀，agent 侧可去重），
        不可静默烧完整个 task_wait（p8-herdr-r1 实证损失 15 分钟）。
        """
        if not w.state_file or not w.state_file.exists():
            return False
        try:
            d = json.loads(w.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if d.get("event") != "userpromptsubmit":
            return False
        return (time.time() - float(d.get("ts", 0))) > self._stalled_s()

    @staticmethod
    def _fmt_missing(artifact: Path | tuple[Path, ...]) -> str:
        if isinstance(artifact, tuple):
            miss = [a.name for a in artifact if not a.exists()]
            return f"目录 {artifact[0].parent} 下缺的文件：{', '.join(miss)}"
        return str(artifact)

    @staticmethod
    def _artifacts_ok(artifact: Path | tuple[Path, ...]) -> bool:
        """完成判定：单产物存在 / 多产物全存在。"""
        if isinstance(artifact, tuple):
            return all(a.exists() for a in artifact)
        return artifact.exists()

    def _read_state(self, w: Worker) -> str | None:
        if not w.state_file or not w.state_file.exists():
            return None
        try:
            return json.loads(
                w.state_file.read_text(encoding="utf-8")
            ).get("state")
        except (OSError, json.JSONDecodeError):
            return None
