"""决策唤醒守护（herdr 操作平面信使）。

架构定调（2026-08-25 用户）：**控制平面 rmux，操作平面 herdr**，
编排（panes/agents 调度）恒走 rmux/librmux；宿主侧窗格操作与 agent
状态查询/注入走 herdr CLI。本守护是操作平面的信使：

    决策请求出现（decisions/*.json pending）
      → herdr agent wait <主窗格> --until idle   （idle 门：只在主 agent
        可接单的时刻注入，不打断工作中的 turn）
      → herdr pane read 末屏内容复核（双锁：状态通道会失明，末屏才是
        地面真相，spinner/计时/对话框在屏就不投）
      → herdr agent prompt <主窗格> <决策简报>    （注入即成为主 agent 的
        一条用户消息，携 decide 命令）

取代主 agent 手里阻塞的 ``wait-decision`` bash 通道：后台 shell 只在
进程退出时通知、且要求主 agent 记得先起它；守护注入则在恰好可接单的
时刻到达，主 agent 全程无需持有任何阻塞进程。

文件所有权（不越界）：``decisions/*.json`` 归 request/decide 写；
``notify_state.json`` 归本守护独占（重发节流账本）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

TERMINAL_STATUSES = ("done", "escalated", "aborted")
_FAIL_BACKOFF_S = 30.0  # 注入失败（blocked 拒发等）后的短退避
RUN_GRACE_S = 60.0  # 落位竞态宽限：run.json 由 _prepare 在落位之后才写

# 内容级复核特征（idle 门第二道锁）。单靠 herdr agent 状态不保险：状态
# 通道会失明（r6 实证：卡 working/Stop 不触发），末屏内容才是地面真相
# [实证: 2026-08-25 本机 herdr pane read 实拍·工作中尾部有 ✻ spinner
# 行 / ⎿ Running… / esc to interrupt，空闲只有 ❯ 输入框 + 状态行]。
_SPINNER_CHARS = ("✻", "✳", "◐", "◑", "◒", "◓", "⏳")  # 行首 spinner 字形
_BUSY_PHRASES = ("to interrupt", "running…")


def _tail_looks_busy(tail: str) -> tuple[bool, str]:
    """末屏是否像「正在工作/对话框开着」。返回 (busy, 命中特征)。

    spinner 字形只认行首（对话文本里引用这些字符不至于顶格）；对话框
    特征复用 config.DIALOGS 全 agent 并集（与 driver._sweep_dialogs 同源）。
    """
    for ln in tail.splitlines():
        if ln.strip()[:1] in _SPINNER_CHARS:
            return True, f"spinner:{ln.strip()[:8]}"
    low = tail.lower()
    for phrase in _BUSY_PHRASES:
        if phrase in low:
            return True, f"phrase:{phrase}"
    from .config import DIALOGS
    for marker, _keys in (s for keys in DIALOGS.values() for s in keys):
        if marker in low:
            return True, f"dialog:{marker}"
    return False, ""


class HerdrClient:
    """herdr CLI 薄封装，操作平面唯一进出口（测试 mock subprocess.run）。

    全部方法失败返回 None/False 而不抛：守护 fail-open，任何 herdr 侧
    异常都不许 crash 掉唤醒通道。
    """

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or shutil.which("herdr")

    def _run(self, argv: list[str], timeout: float):
        if not self.binary:
            return None
        try:
            return subprocess.run(
                [self.binary, *argv],
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def pane_current(self) -> dict | None:
        proc = self._run(["pane", "current"], timeout=10)
        if proc is None or proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)["result"]["pane"]
        except (ValueError, KeyError):
            return None

    def agent_wait_idle(self, target: str, timeout_ms: int) -> bool:
        """阻塞等目标 agent 到 idle（已 idle 即刻返回）。超时/失败 False。"""
        proc = self._run(
            ["agent", "wait", target, "--until", "idle",
             "--timeout", str(int(timeout_ms))],
            timeout=timeout_ms / 1000 + 5,
        )
        return bool(proc and proc.returncode == 0)

    def agent_prompt(self, target: str, text: str) -> bool:
        """注入提示（blocked 拒发/超时 → False，由账本短退避重试）。"""
        proc = self._run(["agent", "prompt", target, text], timeout=30)
        return bool(proc and proc.returncode == 0)

    def pane_read_tail(self, pane_id: str, lines: int = 8) -> str | None:
        """读末屏纯文本（内容级复核用）。读不到返回 None（fail-open：
        此时 herdr 的 idle 判定为唯一依据）。"""
        proc = self._run(
            ["pane", "read", "--source", "visible", "--lines", str(lines),
             "--format", "text", pane_id],
            timeout=10,
        )
        if proc is None or proc.returncode != 0:
            return None
        return proc.stdout or ""


# ------------------------------------------------------------ 纯逻辑 ----

def scan_pending(run_dir: Path) -> list[dict]:
    """读 decisions/*.json，取 status==pending（按文件名稳定排序）。"""
    out: list[dict] = []
    ddir = run_dir / "decisions"
    if ddir.is_dir():
        for f in sorted(ddir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if d.get("status") == "pending":
                d["_stem"] = f.stem  # 账本键：文件名比 node 字段更稳
                out.append(d)
    return out


def read_run_status(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_decision_message(run_id: str, shared: Path, d: dict) -> str:
    node = d.get("node", d.get("_stem", "?"))
    choices = " / ".join(d.get("choices", []))
    return (
        f"[evo-harness ※ 决策请求] run={run_id} node={node}\n"
        f"{d.get('brief', '')}\n"
        f"可选: {choices}\n"
        f"请执行: uv run evo-harness decide --shared {shared} "
        f"--run-id {run_id} --node {node} "
        f"--choice <选项> --rationale '<理由>'"
    )


def build_terminal_message(run_id: str, shared: Path, snap: dict) -> str:
    status = snap.get("status", "?")
    stage = snap.get("stage", "?")
    head = (f"[evo-harness ✔ run 结束] run={run_id} status={status} "
            f"stage={stage}")
    if status == "escalated":
        tail = (f"\n硬停止，现场: uv run evo-harness status --run-id {run_id}"
                f"；处置后清场: uv run evo-harness clean-wt {run_id}")
    elif status == "aborted":
        tail = "\nrun 已中止（会话与 worktree 已清理）"
    else:
        tail = "\n产物已落盘，monitor 窗格可关"
    return head + tail + f"\n产物目录: {Path(shared) / run_id}"


class NotifyLedger:
    """重发节流账本（notify_state.json，守护独占）。

    同一决策节点注入后 ``spacing`` 秒内不重发；新请求（requested_at
    晚于上次注入）立即到期；失败注入走 30s 短退避。终态通知只发一次。
    心跳线程与主循环并发写：所有变更经 _lock 串行化（rc-r2/r3 三方
    must-fix：beat 被 _deliver 的长阻塞饿死 → 独立线程）。
    """

    def __init__(self, path: Path) -> None:
        import threading
        self._lock = threading.Lock()
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._nodes: dict[str, dict] = data.get("nodes", {})
        self._terminal = bool(data.get("terminal"))
        self._daemon: dict = data.get("daemon", {})

    def _flush(self) -> None:
        self.path.write_text(
            json.dumps({"nodes": self._nodes, "terminal": self._terminal,
                        "daemon": self._daemon},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def beat(self, main_pane: str, pid: int | None = None) -> None:
        """值守心跳（monitor 状态面板数据源；线程安全）。"""
        with self._lock:
            self._daemon = {"main_pane": main_pane, "beat": time.time(),
                            "pid": pid}
            self._flush()

    @property
    def daemon(self) -> dict:
        return dict(self._daemon)

    def due(self, stem: str, requested_at: float, now: float,
            spacing: float) -> bool:
        rec = self._nodes.get(stem)
        if rec is None:
            return True
        if requested_at and requested_at > rec.get("last", 0.0):
            return True  # 新请求（requested_at 晚于上次注入）：立即到期
        gap = spacing if rec.get("ok") else _FAIL_BACKOFF_S
        return now - rec.get("last", 0.0) >= gap

    def mark(self, stem: str, now: float, ok: bool) -> None:
        with self._lock:
            self._nodes[stem] = {"last": now, "ok": ok}
            self._flush()

    def forget(self, stem: str) -> None:
        with self._lock:
            if stem in self._nodes:
                del self._nodes[stem]
                self._flush()

    def stems(self) -> list[str]:
        return list(self._nodes)

    @property
    def terminal_notified(self) -> bool:
        return self._terminal

    def mark_terminal(self) -> None:
        with self._lock:
            self._terminal = True
            self._flush()


# ------------------------------------------------------------- 守护环 ----

def log_notify(run_dir: Path, detail: str) -> None:
    """守护事件入 notify_events.jsonl（monitor 融合渲染进事件流）。

    单写者追加（只有守护写），与 run.json 的 read-modify-write 无交集；
    换 run.json 会在 harness 进程写历史的路上撞车，jsonl 追加无此险。
    """
    with (Path(run_dir) / "notify_events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(
            {"t": round(time.time(), 3), "kind": "notify", "detail": detail},
            ensure_ascii=False,
        ) + "\n")

def _deliver(client: HerdrClient, main_pane: str, text: str,
             idle_wait_ms: int) -> str:
    """双锁投递：herdr idle 门 + 末屏内容复核，过了才注入。

    返回三态：
    - "ok"   注入成功；
    - "skip" 还不到投递时机（idle 没等到 / 屏上还有活或对话框），
      不记账，下一拍重查（瞬时态，不该吃 30s 退避）；
    - "fail" 注入被拒（blocked 拒发/超时），记失败账走短退避。
    """
    wait_ok = client.agent_wait_idle(main_pane, idle_wait_ms)
    tail = client.pane_read_tail(main_pane)
    if tail is not None:
        # 内容复核是权威兜底（accept-notifyd-r6 实证：状态通道对主窗格
        # 失明，wait 每 600s 超时、决策干等 30+ 分钟零注入）·末屏无
        # 忙碌特征即视为可投，wait 结果仅作参考
        busy, _hit = _tail_looks_busy(tail)
        if busy:
            return "skip"
    elif not wait_ok:
        # 读不到屏（fail-open 失效）才退回以 herdr idle 门为准
        return "skip"
    return "ok" if client.agent_prompt(main_pane, text) else "fail"


def tick(run_dir: Path, shared: Path, run_id: str, ledger: NotifyLedger,
         client: HerdrClient, main_pane: str, spacing: float,
         idle_wait_ms: int, now: float, log=print) -> str:
    """守护单拍：处理全部到期通知。返回 "continue" / "exit"（终态已通知）。"""
    snap = read_run_status(run_dir)
    if snap.get("status") in TERMINAL_STATUSES:
        if not ledger.terminal_notified:
            verdict = _deliver(client, main_pane,
                               build_terminal_message(run_id, shared, snap),
                               idle_wait_ms)
            if verdict == "ok":
                ledger.mark_terminal()
                log(f"[notifyd] run={run_id} 终态 {snap.get('status')} "
                    f"已通知 {main_pane}")
                log_notify(run_dir, f"run 终态 {snap.get('status')}，"
                                    f"收尾通知已注入 {main_pane}")
        return "exit" if ledger.terminal_notified else "continue"

    pending = {d["_stem"]: d for d in scan_pending(run_dir)}
    for stem in ledger.stems():  # 已决节点清账：同 node 再来新请求立即到期
        if stem not in pending:
            ledger.forget(stem)
    for stem, d in pending.items():
        if not ledger.due(stem, d.get("requested_at", 0.0), now, spacing):
            continue
        verdict = _deliver(client, main_pane,
                           build_decision_message(run_id, shared, d),
                           idle_wait_ms)
        if verdict != "skip":  # skip 不记账：瞬时态下一拍重查
            ledger.mark(stem, now, verdict == "ok")
        log(f"[notifyd] {stem} 注入 {verdict} -> {main_pane}")
        if verdict != "skip":
            log_notify(run_dir, f"※ {stem} 决策简报注入 {verdict} → {main_pane}")
    return "continue"


def run_notifyd(shared: Path, run_id: str, main_pane: str = "",
                poll: float = 2.0, spacing: float = 600.0,
                idle_wait_ms: int = 600_000, grace_s: float = RUN_GRACE_S,
                beat_s: float = 5.0,
                client: HerdrClient | None = None) -> int:
    """守护入口（cli notifyd）。终态通知成功即退；run 目录消失即退。

    落位竞态宽限：落位发生在 main() 进 flow 之前，run.json 由 _prepare
    稍后才写（accept-notifyd-r1 实证 monitor 即死于这条竞态），宽限
    grace_s 等它出现，超时才报「无 run」。
    """
    run_dir = Path(shared) / run_id
    deadline = time.monotonic() + max(grace_s, 0.0)
    while not (run_dir / "run.json").exists():
        if time.monotonic() >= deadline:
            print(f"无 run: {run_dir}", file=sys.stderr, flush=True)
            return 1
        time.sleep(0.5)
    client = client or HerdrClient()
    if not main_pane:
        pane = client.pane_current()
        main_pane = (pane or {}).get("pane_id", "")
    if not main_pane:
        print("主窗格无法定位（herdr pane current 失败），守护退出",
              file=sys.stderr, flush=True)
        return 1
    ledger = NotifyLedger(run_dir / "notify_state.json")
    print(f"[notifyd] run={run_id} main_pane={main_pane} "
          f"spacing={spacing:.0f}s poll={poll:.0f}s", flush=True)
    log_notify(run_dir, f"值守开始：注入目标 {main_pane}"
                        f"（{spacing:.0f}s 节流 / 失败 30s 退避）")
    import os as _os
    import threading
    ledger.beat(main_pane, _os.getpid())
    stop = threading.Event()

    def _beat_loop() -> None:
        # 心跳独立线程：_deliver 的长阻塞（idle 等待至多 idle_wait_ms）
        # 不再饿死 beat → monitor 不再误报失联（rc-r2/r3 三方 must-fix）
        while not stop.wait(max(beat_s, 0.2)):
            ledger.beat(main_pane)

    heart = threading.Thread(target=_beat_loop, daemon=True)
    heart.start()

    def _log(msg: str) -> None:
        print(msg, flush=True)  # stdout 落 notifyd.log：块缓冲会吞行，必须 flush

    try:
        while True:
            verdict = tick(run_dir, Path(shared), run_id, ledger, client,
                           main_pane, spacing, idle_wait_ms, time.time(),
                           log=_log)
            if verdict == "exit":
                stop.set()
                return 0
            if not (run_dir / "run.json").exists():  # abort 清场：无声退
                stop.set()
                print(f"[notifyd] run 目录已消失，守护退出", flush=True)
                return 0
            time.sleep(poll)
    finally:
        stop.set()
