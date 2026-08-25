"""L0 运行时驱动（async/await）：librmux 封装——后台会话、四铁律 drive、事件等待。

异步铁则（P7 重构 2026-08-24）：
- librmux 是同步 subprocess SDK → 一律 `asyncio.to_thread` 包裹，不阻塞事件循环
- 屏幕等待 → rmux 原生 `wait-pane --text/--quiet --stable-for`（阻塞在线程池）
- 文件/状态等待 → watchdog/inotify 异步事件（events.AsyncFileWatcher）
- 协作式节奏用 `asyncio.sleep`（让出循环）；**禁止 time.sleep**（阻塞循环，lint 固化）
- fan-out（多 unit 就绪/驱动）用 `asyncio.gather` 真并发

drive 铁律（win-rmux 实测沉淀）：① 绝不发 C-c 预清 ② 文本与 Enter 分发
（中间用原生静默等待同步）③ 提交后正向确认（短头 ≤10 字符；hook 状态通道优先）
④ pane_pid 反查定位 ⑤ prompt 拍平单行。

P8.1 三段式（herdr 研究 r10，synthesis 收敛）：发前先扫对话框（先查再发，
替代「发了再修」的 resolve_dialogs 顺序）；文本走 bracketed-paste 感知路
（load-buffer + paste-buffer -p，daemon 按 pane 模式包壳，发送侧勿自包壳）
→ 原生静默同步 → 编码 Enter（send-keys -H 0d，hex 字节非键名）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from librmux import Pane, Session

from .config import HarnessConfig

# pane 环境守卫（launch-unit.ps1 沉淀）
DRIVER_ENV = {
    "RMUX_DISABLE_TINY_CLI": "1",
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
}


class DriverError(RuntimeError):
    pass


def _to_thread(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


def _agent_in_pane(info: dict, agent: str,
                   proc_root: Path = Path("/proc")) -> bool:
    """pane 进程名反查（跨平台）：Linux 读 /proc/<pid>/comm；macOS/Windows
    无 /proc，回退 tmux 跨平台字段 pane_current_command。"""
    pid = info.get("pane_pid")
    if not pid:
        return False
    comm = proc_root / str(pid) / "comm"
    if comm.exists():
        return agent in comm.read_text(errors="replace").strip()
    if proc_root.is_dir():
        return False  # 有 /proc 但该 pid 已退出
    return agent in str(info.get("pane_current_command", ""))


class HarnessDriver:
    """一个 run 对应一个 rmux session；window=stage，pane=unit。"""

    def __init__(self, config: HarnessConfig, run_id: str) -> None:
        from librmux import RMUX

        self.config = config
        self.run_id = run_id
        # socket 按 run 隔离：多 run 并存互不干扰，kill-server 只清自己
        # （smoke-12 实证：共用 socket 时调试清场会连坐正在跑的 run）
        self.socket = f"{config.socket_name}-{run_id}"
        self._rmux = RMUX(socket_name=self.socket, env=dict(DRIVER_ENV))
        self.session: Session | None = None

    # ------------------------------------------------------ 原生等待原语 ----

    async def _wait_pane(self, pane: Pane, *args: object, timeout: str = "30s") -> bool:
        """rmux 原生 wait-pane（阻塞在线程池；args 如 --text x / --quiet）。"""
        def _run() -> bool:
            run = self._rmux.cmd(
                "wait-pane", "-t", pane.target, *args, "--timeout", timeout,
                check=False,
            )
            return run.returncode == 0
        return await _to_thread(_run)

    async def _wait_stable(self, pane: Pane, stable_for: str = "800ms",
                           timeout: str = "30s") -> bool:
        """等 pane 输出静默 stable_for（发送前的同步点）。"""
        return await self._wait_pane(
            pane, "--quiet", "--stable-for", stable_for, "--timeout", timeout
        )

    async def _wait_screen_text(self, pane: Pane, text: str, timeout: float) -> bool:
        """SDK 原生 wait_for_text（线程池阻塞；超时返回 False 不抛）。"""
        def _run() -> bool:
            try:
                pane.wait_for_text(text, timeout=timeout)
                return True
            except Exception:
                return False
        return await _to_thread(_run)

    async def _screen(self, pane: Pane) -> str:
        def _run() -> str:
            try:
                return pane.capture_text()
            except Exception:
                return ""
        return await _to_thread(_run)

    async def screen(self, pane: Pane) -> str:
        """公开屏幕快照（drive 失败留证用）。"""
        return await self._screen(pane)

    # ------------------------------------------------------------ 生命周期 ----

    async def ensure_session(self) -> Session:
        """探针先行：绝不静默清/叠已有会话。"""
        from librmux import RmuxCommandError

        def _run() -> Session:
            try:
                existing = [
                    s.get("session_name", "")
                    for s in self._rmux.list_sessions()
                ]
            except RmuxCommandError:
                self._rmux.start_server()
                existing = []
            name = self.config.session_name(self.run_id)
            if name in existing:
                self.session = self._rmux.session(name)
                return self.session
            self.session = self._rmux.ensure_session(
                name, detached=True, start_directory=str(Path.cwd())
            )
            names = [w.get("window_name", "") for w in self.session.list_windows()]
            if "control" not in names:
                self.session.new_window(name="control")
            return self.session

        return await _to_thread(_run)

    async def window_for(self, stage: str, cwd: str | None = None):
        assert self.session is not None, "先 ensure_session()"

        def _run():
            names = [w.get("window_name", "") for w in self.session.list_windows()]
            if stage in names:
                return self.session.window(names.index(stage))
            return self.session.new_window(
                name=stage, start_directory=cwd or str(Path.cwd())
            )
        return await _to_thread(_run)

    async def spawn_unit(self, stage: str, agent: str, cwd: str | None = None,
                         unit_id: str = "", state_file: Path | None = None) -> Pane:
        """在 stage window 开 pane 跑 agent；注入身份 env（hook 状态通道用）。"""
        window = await self.window_for(stage)
        cmdline = self.config.agent_cmdline(agent)
        prefix = ""
        if state_file is not None:
            prefix = (
                f"env EVO_RUN={self.run_id} EVO_UNIT={unit_id} "
                f"EVO_STATE_FILE={state_file.resolve()} "
            )
        if cwd:
            prefix += f"cd {cwd} && exec "
        full = prefix + cmdline

        def _run() -> Pane:
            panes = window.list_panes()
            if len(panes) == 1 and panes[0].get("pane_current_command", "") in (
                "bash", "zsh", "sh", "",
            ):
                pane = window.pane(0)
                pane.respawn(shell_command=full, kill=True)
                return pane
            return window.pane(0).split(shell_command=full)
        return await _to_thread(_run)

    async def kill_session(self) -> None:
        """杀本 run 的 rmux session。

        abort 由独立进程执行，self.session 必为 None——必须按会话名反查
        再杀，否则跨进程 abort 完全不生效。无 server / 会话不存在时安静跳过。
        """

        def _run() -> None:
            session = self.session
            if session is None:
                try:
                    names = [
                        s.get("session_name", "")
                        for s in self._rmux.list_sessions()
                    ]
                except Exception:
                    return  # 无 rmux server（或 socket 从未启动）：无可杀
                name = self.config.session_name(self.run_id)
                if name not in names:
                    return
                session = self._rmux.session(name)
            try:
                session.kill()
            except Exception:
                pass

        await _to_thread(_run)
        self.session = None

    # ------------------------------------------------------------ 定位 ----

    async def locate(self, agent: str, stage: str) -> Pane | None:
        """铁律 4：pane_pid 反查进程名，不信任布局索引。"""
        assert self.session is not None

        def _run() -> Pane | None:
            names = [w.get("window_name", "") for w in self.session.list_windows()]
            if stage not in names:
                return None
            window = self.session.window(names.index(stage))
            for info in window.list_panes():
                if _agent_in_pane(info, agent):
                    return window.pane(int(str(info.get("pane_index", 0))))
            return None
        return await _to_thread(_run)

    # ------------------------------------------------------------ drive ----

    async def drive(self, pane: Pane, text: str, tui: bool = True,
                    state_file: Path | None = None) -> bool:
        """四铁律 + P8.1 三段式驱动提交。确认优先级：hook 状态通道 > 屏幕短头。"""
        text = " ".join(text.split())  # 铁律 5：单行化
        head = text[:10]
        # P8.1 先查再发：对话框在场先处置——模态框会吞提交文本，也会让
        # _ensure_input_live 的 zz 探针失真（输入被对话框截走）
        await self._sweep_dialogs(pane)
        if tui and not await self._ensure_input_live(pane):
            return False
        for attempt in range(3):
            if head and head in await self._screen(pane):
                return True  # 迟到确认，勿重发
            # 重试退避 = 原生长静默等待
            await self._wait_stable(
                pane, "6s" if attempt else "800ms", "8s" if attempt else "5s"
            )
            # 铁律 2 + P8.1 三段式：① bracketed-paste 感知文本（buffer 路失败
            # 退字面量 send-text）② 原生静默同步 ③ 编码 Enter（hex 字节）
            if not await self._send_paste(pane, text):
                await _to_thread(pane.send_text, text)
            await self._wait_stable(pane, "300ms", "3s")
            await self._send_enter(pane)
            if head and await self._wait_screen_text(pane, head, 8.0):
                return True
            # 通用正向信号：提交后输入行回到占位符（› Ask…/空 ❯）= TUI 已接收
            # （codex 无 hook 状态、head 回显渲染慢时的可靠确认）
            # 原生静默同步替代固定 sleep：输出静默 2s = TUI 已消化提交；
            # 若已开始流式输出则 3s 上限后直接走后续确认路径（不劣于盲等）
            await self._wait_stable(pane, "2s", "3s")
            if head and not await self._input_line_contains(pane, head):
                return True
            if state_file is not None and await self.wait_state(
                state_file, "working", 15.0
            ):
                return True  # hook 状态通道确认（UserPromptSubmit→working）
            if await self._prompt_residual(pane, text):
                await self._send_enter(pane)  # 只补 Enter 防重复
                if head and await self._wait_screen_text(pane, head, 8.0):
                    return True
        return False

    async def _ensure_input_live(self, pane: Pane, timeout: float = 240.0) -> bool:
        """探针确认输入通道已被 TUI 接管（并发多实例窗口可达 2-3 分钟）。

        检测用原生 `wait-pane --next-text zz`：TUI 读取输入才会**回显**到
        输出流——屏幕子串匹配会被折行骗过（❯ 与文本分行，实测堆积 80 个 z
        也不命中）。活后批量退格清探针（预活探针被 TUI 丢弃，无残留）。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await _to_thread(pane.send_text, "zz")
            if await self._wait_pane(pane, "--next-text", "zz", timeout="5s"):
                await _to_thread(
                    pane.send_keys, *(["BSpace"] * 10)
                )
                return True
        return False

    async def _input_line_contains(self, pane: Pane, text: str) -> bool:
        """输入行（›/❯ 开头的最后一处）是否仍挂着 text（未提交残留）。"""
        screen = await self._screen(pane)
        input_lines = [
            l for l in screen.splitlines()
            if l.strip().startswith(("›", "❯"))
        ]
        if not input_lines:
            return False  # 找不到输入行（备屏/滚动）——保守视为无残留
        return any(text in l for l in input_lines[-2:])

    async def _prompt_residual(self, pane: Pane, text: str) -> bool:
        screen = await self._screen(pane)
        for line in screen.splitlines():
            if "❯" in line and text[:10] in line:
                return True
            if "queued" in line:
                return True
        return False

    async def resolve_dialogs(self, pane: Pane, agent: str,
                              timeout: float = 45.0) -> int:
        """confirm-box 移植：分 agent 键序批量发送 + 原生静默等界面切换。"""
        from .config import DIALOGS

        specs = DIALOGS.get(agent, [])
        if not specs:
            return 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        handled = 0
        idle_rounds = 0
        while loop.time() < deadline:
            screen = (await self._screen(pane)).lower()
            hit = next((s for s in specs if s[0] in screen), None)
            if hit is None:
                idle_rounds += 1
                if idle_rounds >= 3:
                    break
                await self._wait_stable(pane, "1s", "2s")
                continue
            idle_rounds = 0
            await _to_thread(pane.send_keys, *hit[1])  # 整个键序一次发出
            await self._wait_stable(pane, "800ms", "3s")
            handled += 1
        return handled

    # ------------------------------------------- P8.1 先查再发 + 三段式 ----

    async def _sweep_dialogs(self, pane: Pane, rounds: int = 3) -> int:
        """发前扫对话框（先查再发）：全 agent marker 并集，命中即按键处置。

        替代 r10 前主路的「发了再修」（drive 失败后才 resolve_dialogs，
        提交文本已被模态框吞掉）。marker 文本本身即对话框身份、键序随
        marker 走——与 pane 里是哪个 agent 无关，故扫并集而非按 agent 过滤。
        """
        from .config import DIALOGS

        specs = [s for keys in DIALOGS.values() for s in keys]
        handled = 0
        for _ in range(rounds):
            screen = (await self._screen(pane)).lower()
            hit = next((s for s in specs if s[0] in screen), None)
            if hit is None:
                break
            await _to_thread(pane.send_keys, *hit[1])
            await self._wait_stable(pane, "800ms", "3s")
            handled += 1
        return handled

    async def _send_paste(self, pane: Pane, text: str) -> bool:
        """三段式①：bracketed-paste 感知文本注入（rmux 原生 buffer 路）。

        load-buffer 载入 → paste-buffer -p：daemon 仅当目标 pane 已启用
        bracketed 模式才包 \\x1b[200~…\\x1b[201~（感知式包壳——发送侧自包壳
        会双重包裹，r10 研究 C3 冲突裁决）。payload 剥裸 ESC（orca 实证：
        防控制序列注入）。librmux cmd 不支持 stdin → load-buffer 走文件参数。
        """
        import tempfile

        payload = text.replace("\x1b", "")
        buf = "evo-" + re.sub(
            r"[^A-Za-z0-9_-]", "_", f"{self.run_id}-{pane.target}"
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix="evo-paste-", encoding="utf-8",
            delete=False,
        ) as f:
            f.write(payload)
            path = f.name
        try:
            load = await _to_thread(
                self._rmux.cmd, "load-buffer", "-b", buf, path, check=False
            )
            if load.returncode != 0:
                return False
            paste = await _to_thread(
                self._rmux.cmd,
                "paste-buffer", "-p", "-d", "-b", buf, "-t", pane.target,
                check=False,
            )
            return paste.returncode == 0
        finally:
            Path(path).unlink(missing_ok=True)

    async def _send_enter(self, pane: Pane) -> bool:
        """三段式③：编码 Enter——send-keys -H 0d（hex 字节，非键名 token）。

        herdr 同义（Enter 按协商协议编码而非固定键名）；老 daemon 无 -H
        时退 pane.send_keys("Enter")，保底可用。
        """
        run = await _to_thread(
            self._rmux.cmd, "send-keys", "-t", pane.target, "-H", "0d",
            check=False,
        )
        if run.returncode == 0:
            return True
        await _to_thread(pane.send_keys, "Enter")
        return False

    # ------------------------------------------------------------ observe ----

    async def wait_state(self, state_file: Path, want: str = "working",
                         timeout: float = 20.0) -> bool:
        """hook 状态通道：等 state.json 报告目标状态（inotify 异步事件）。"""
        from .events import wait_for_files as aff

        sf = Path(state_file)

        def _ok() -> bool:
            try:
                return json.loads(
                    sf.read_text(encoding="utf-8")
                ).get("state") == want
            except (OSError, json.JSONDecodeError, ValueError):
                return False

        watch_dir = sf.parent if sf.parent.exists() else sf.parents[-2]
        return await aff(watch_dir, _ok, timeout)

    async def wait_ready(self, pane: Pane, agent: str,
                         timeout: float | None = None,
                         state_file: Path | None = None) -> bool:
        """agent 就绪判定（herdr hook_authority 原则）：

        权威信号 = hook 状态通道**收到首份上报**（SessionStart→idle，状态
        文件出现即 agent 已起且交互系统活了）；屏幕输入符只是无 hook 通道时
        的回退启发（fake；codex 的 SessionStart 不触发时用 › Ask 标记）。
        两路并发，先到先得。
        """
        timeout = timeout or self.config.launch_settle_seconds

        async def _state_reported() -> bool:
            from .events import wait_for_files as aff

            sf = Path(state_file)
            return await aff(
                sf.parent if sf.parent.exists() else sf.parents[-2],
                lambda: sf.exists(), timeout,
            )

        async def _marker_seen() -> bool:
            spec = self.config.agents.get(agent)
            markers = [spec.ready_text] if spec and spec.ready_text else []
            markers += [m for m in ("❯", "›", ">") if m not in markers]
            slice_t = timeout / max(1, len(markers))
            for marker in markers:
                if await self._wait_screen_text(pane, marker, slice_t):
                    return True
            return False

        if state_file is None:
            return await _marker_seen()  # 无状态通道：只等标记

        tasks = [
            asyncio.ensure_future(_state_reported()),
            asyncio.ensure_future(_marker_seen()),
        ]
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
        return any(p.result() for p in done)
        return False

    async def observe_files(
        self, paths: dict[str, Path], timeout: float,
        on_wake=None,
    ) -> dict[str, str]:
        """产物等待：inotify 异步事件驱动。on_wake(事件|None) 每次唤醒回调。"""
        import os

        if not paths:
            return {}
        if all(p.exists() for p in paths.values()):
            return {k: "done" for k in paths}

        try:
            root = Path(os.path.commonpath(
                [str(p.parent) for p in paths.values()]
            ))
        except ValueError:
            root = Path.cwd()
        probe = root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent

        def _all_done() -> bool:
            return all(p.exists() for p in paths.values())

        from .events import AsyncFileWatcher

        watcher = AsyncFileWatcher(probe)
        try:
            await watcher.wait_until(_all_done, timeout, on_wake=on_wake)
        finally:
            watcher.close()
        return {
            key: ("done" if p.exists() else "timeout")
            for key, p in paths.items()
        }
