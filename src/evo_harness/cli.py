"""CLI entry for evo-harness.

Usage:
    evo-harness run <goal> [--fake] [--detach] [--shared DIR] [--run-id ID]
                           [--plan-only] [--global-hooks]
    evo-harness review-cycle <goal> [--rounds N] [--fake] [--detach]
                                    [--global-hooks]
    evo-harness monitor [--run-id ID] [--shared DIR]
    evo-harness status [--run-id ID] [--shared DIR] [--json]
    evo-harness wait [--run-id ID] [--shared DIR] [--timeout SEC]
    evo-harness notifyd [--run-id ID] [--shared DIR] [--main-pane PANE]
    evo-harness abort [--run-id ID] [--shared DIR]

后台化（三端 agent 的编排触发姿势）：
    Claude Code / Kimi：Shell(run_in_background=true) 提交 run，等自动完成通知
    Codex（无 tracked 后台）：run --detach 秒回，然后轮询 status / 阻塞 wait
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import HarnessConfig

FAKE_SCRIPT = Path(__file__).resolve().parent / "scripts" / "fake_agent.py"  # 包内随发


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evo-harness",
        description="evo 六阶段 Graph-of-Loops 编排器（librmux 驱动后台 agent）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rc = sub.add_parser("review-cycle", help="循环到一致的生产就绪 review")
    rc.add_argument("goal")
    rc.add_argument("--rounds", type=int, default=3)
    rc.add_argument("--fake", action="store_true")
    rc.add_argument("--detach", action="store_true")
    rc.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
    rc.add_argument("--run-id", default=None)
    rc.add_argument("--global-hooks", action="store_true",
                    help="同时写用户全局 hook（~/.codex、~/.kimi-code）；"
                         "默认只装项目级 claude hook")
    rc.add_argument("--pane", choices=("auto", "right", "none"), default="auto",
                    help="控制台落位（monitor+notifyd）：auto=herdr 会话内（HERDR_ENV=1）"
                         "自动右分屏；right=强制落位；none=禁用")

    run = sub.add_parser("run", help="跑完整六阶段")
    run.add_argument("goal")
    run.add_argument("--fake", action="store_true",
                     help="确定性模式：用 fake agent，不调真 LLM")
    run.add_argument("--detach", action="store_true",
                     help="自守护后台运行：秒回，pid/日志落 shared/<run>/，"
                          "配 status 轮询或 wait 阻塞（Codex 等无原生后台通知的 agent 用）")
    run.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
    run.add_argument("--run-id", default=None)
    run.add_argument("--plan-only", action="store_true", help="跑到 plan 门禁即停")
    run.add_argument("--global-hooks", action="store_true",
                     help="同时写用户全局 hook（~/.codex、~/.kimi-code）；"
                          "默认只装项目级 claude hook")
    run.add_argument("--pane", choices=("auto", "right", "none"), default="auto",
                     help="控制台落位（monitor+notifyd）：auto=herdr 会话内（HERDR_ENV=1）"
                          "自动右分屏；right=强制落位；none=禁用")

    mon = sub.add_parser("monitor", help="TUI 监控仪表盘（双窗格操作台右侧）")
    mon.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
    mon.add_argument("--run-id", default=None)

    con = sub.add_parser(
        "contract", help="登记三契约（控制面 JSON 由程序生成，模型只填参数）")
    con.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
    con.add_argument("--run-id", default=None,
                     help="缺省取最新 run（模板未注入时兜底）")
    con_sub = con.add_subparsers(dest="what", required=True)
    g = con_sub.add_parser("goal", help="登记 goal_spec")
    g.add_argument("--goal", required=True)
    g.add_argument("--non-goal", action="append", default=[])
    g.add_argument("--criterion", action="append", required=True)
    st = con_sub.add_parser("step", help="登记 plan step")
    st.add_argument("--id", required=True)
    st.add_argument("--title", required=True)
    st.add_argument("--after", default="", help="依赖的 step id，逗号分隔")
    al = con_sub.add_parser("alloc", help="登记执行 unit")
    al.add_argument("--unit", required=True)
    al.add_argument("--branch", required=True)
    al.add_argument("--scope", action="append", required=True)
    al.add_argument("--criterion", action="append", default=[])
    mo = con_sub.add_parser("merge-order", help="定 merge_order（须覆盖全部 step）")
    mo.add_argument("order", help="逗号分隔的 step id 序")

    dec = sub.add_parser(
        "decide", help="主 agent 决策（关键决策节点参与，配 monitor 使用）")
    dec.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
    dec.add_argument("--run-id", default=None)
    dec.add_argument("--node", required=True)
    dec.add_argument("--choice", required=True)
    dec.add_argument("--rationale", default="", help="决策理由（入事件流）")

    cw = sub.add_parser(
        "clean-wt", help="清指定 run 的 worktree 与分支（hard stop 保现场后一键清场）")
    cw.add_argument("--run-id", required=True)

    wd = sub.add_parser(
        "wait-decision",
        help="阻塞等决策请求出现（非 herdr 环境的唤醒兜底；"
             "herdr 会话用 notifyd 守护注入）")
    wd.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
    wd.add_argument("--run-id", default=None)
    wd.add_argument("--timeout", type=float, default=3600.0)

    nd = sub.add_parser(
        "notifyd",
        help="决策唤醒守护（herdr 操作平面：主 agent idle 时注入决策简报；"
             "run/review-cycle 落位时自动起，也可手动起）")
    nd.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
    nd.add_argument("--run-id", default=None)
    nd.add_argument("--main-pane", default="",
                    help="主窗格 pane id（缺省 herdr pane current 自动定位）")
    nd.add_argument("--idle-wait-ms", type=int, default=None,
                    help="每轮等主 agent idle 的窗口（缺省取 Budget）")

    for name, help_ in (
        ("status", "查看 run 状态快照（轮询友好）"),
        ("wait", "阻塞等 run 结束（done/escalated/aborted）"),
        ("abort", "终止 run 并清理"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--shared", type=Path, default=Path(".evo_tasks"))
        p.add_argument("--run-id", default=None)
    sub.choices["status"].add_argument("--json", action="store_true",
                                       help="机器可读输出（agent 轮询用）")
    sub.choices["wait"].add_argument("--timeout", type=float, default=3600.0)
    return parser


def _resolve_run_id(args) -> str:
    if getattr(args, "run_id", None):
        return args.run_id
    import time

    return time.strftime("run-%Y%m%d-%H%M%S")


def _pid_alive(pid: int) -> bool:
    """进程存活探测（跨平台）。

    Windows 的 os.kill(pid, 0) 不是探测是杀伤：CPython 对非
    CTRL_C/CTRL_BREAK_EVENT 的信号一律 TerminateProcess(handle, sig)，
    status/monitor 扫一眼就会把 detached 守护进程杀掉（codex r2 must-fix）。
    Windows 走 OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) 只读探测。
    """
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _latest_run_id(shared: Path) -> str | None:
    """未传 --run-id 时取最新的 run 目录（status/wait 轮询便利）。"""
    root = Path(shared)
    if not root.is_dir():
        return None
    runs = [p for p in root.iterdir() if p.is_dir() and (p / "run.json").exists()]
    if not runs:
        return None
    return max(runs, key=lambda p: (p / "run.json").stat().st_mtime).name


def _run_snapshot(shared: Path, run_id: str) -> dict:
    """status 的数据源：run.json + pidfile + 每 unit 的产物/hook 状态。"""
    from .filebus import FileBus

    bus = FileBus(shared, run_id)
    snap = bus.read_run()
    pid_file = bus.root / "run.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text().strip())
        snap["pid"] = pid
        snap["pid_alive"] = _pid_alive(pid)
    units = []
    units_dir = bus.root / "units"
    if units_dir.is_dir():
        for d in sorted(units_dir.iterdir()):
            if not d.is_dir():
                continue
            state_file = d / "state.json"  # hook 状态通道（agent 自报三态）
            try:
                state = (
                    json.loads(state_file.read_text(encoding="utf-8")).get("state")
                    if state_file.exists() else None
                )
            except (json.JSONDecodeError, OSError):
                state = None
            units.append({
                "unit_id": d.name,
                "result_ready": (d / "result.json").exists(),
                "agent_state": state,
            })
    snap["units"] = units
    return snap


def _detach_popen_kwargs() -> dict:
    """脱离控制终端的跨平台姿势：POSIX 用独立会话；Windows 无此语义，
    用新进程组 + 游离控制台（DETACHED_PROCESS）。"""
    if os.name == "posix":
        return {"start_new_session": True}  # 脱离控制终端与进程组（nohup 语义）
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0),
    }


def preflight(require_rmux: bool) -> int | None:
    """启动预检（2026-08-25 用户定调）：依赖运行时没装好就警告退出，
    不许带病起跑后 hook/编排半路哑掉。

    - Python 依赖（librmux/rich/watchdog）必须可导入，缺了提示重装命令；
    - require_rmux（run/review-cycle）还要求 rmux 二进制在 PATH；
    - herdr 可选：缺席只影响控制台/注入层，各处 fail-open，不在预检拦。
    """
    missing = []
    for mod in ("librmux", "rich", "watchdog"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"[preflight] Python 依赖缺失: {', '.join(missing)}"
              f"；重装: uv tool install --force "
              f"git+https://github.com/raystyle/evo-harness",
              file=sys.stderr)
        return 3
    if require_rmux:
        import shutil
        if shutil.which("rmux") is None:
            print("[preflight] 未找到 rmux 二进制（控制平面 daemon）。"
                  "安装后加入 PATH 再跑；检查命令: rmux -V",
                  file=sys.stderr)
            return 3
    return None


def _sh_join(tokens) -> str:
    """跨平台 shell 拼接：POSIX 用 shlex.quote；Windows（cmd）用双引号
    包裹含空格/特殊字符的 token（内嵌双引号转 \"，尾部反斜杠保住）。
    rmux/herdr 的 pane run / new-session 在 Windows 侧走 cmd，POSIX 语义
    的 shlex.quote 会产出单引号导致命令不可执行。macOS 与 Linux 同为 POSIX。
    """
    import shlex
    toks = [str(x) for x in tokens]
    if os.name != "nt":
        return " ".join(shlex.quote(x) for x in toks)
    out = []
    for x in toks:
        if not x:
            out.append('""')
            continue
        if any(ch in x for ch in ' 	"&|<>^()'):
            body = x.replace('"', '\\"')
            if body.endswith("\\"):
                body += "\\"
            out.append(f'"{body}"')
        else:
            out.append(x)
    return " ".join(out)


def _record_console(shared: Path, run_id: str, key: str, value: dict) -> None:
    """控制台清单落盘（console.json）：哪个执行单元、哪个平面、哪个 pane。
    monitor 的 UNITS 面板数据源；落位序列单线程，read-modify-write 安全。"""
    p = Path(shared) / run_id / "console.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def _herdr_live_allowed() -> bool:
    """pytest 环境拒绝真实 herdr 操作（2026-08-25 事故沉淀）。

    测试 fixture 漏 patch 新集成点时，测试真调宿主 herdr，开 tab 投真
    flow，flow 的 execute unit 又在旧代码 worktree 里跑 pytest，形成
    自繁殖（一轮事故 1600+ 杂散 tab、真 agent run 烧 70 分钟）。测试要
    走 mock 路径须显式 EVO_HERDR_TEST_FAKE=1（_mk_* 夹具设）。
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
            "EVO_HERDR_TEST_FAKE"):
        return False
    return True


def _place_herdr_monitor(run_id: str, shared: Path) -> str | None:
    """P8.4 herdr 会话内 monitor 自动落位（herdr 集成点之一，0.8.2 实测）。

    ① ``pane split --current --direction right --no-focus --cwd``：右分屏
    不抢焦点，stdout JSON 的 ``result.pane.pane_id`` 即新 pane；
    ② ``pane wait-output --regex '[%$#]\\s*$'``：等 shell 提示符就绪（替代
    herdr SKILL 建议的盲 sleep 2-3s，shell 未稳时 pane run 吞首字符）；
    ③ ``pane run <pane_id> <解释器> -m evo_harness.cli monitor …``：用本
    进程解释器起 monitor，不依赖新 pane 的 PATH/venv。
    任一步失败仅告警不阻断 run（monitor 是辅助面，fail-open）。
    成功返回新 pane id（notifyd 在其下分屏用），失败返回 None。
    """
    import shlex
    import shutil

    if os.environ.get("EVO_HERDR_MONITOR_PLACED") == "1":
        return None  # --detach 自守护重拉起的子进程：父进程已落位，防双开
    if not _herdr_live_allowed():
        return None
    herdr = shutil.which("herdr")
    if herdr is None:
        print("[herdr] HERDR_ENV=1 但无 herdr CLI，monitor 落位跳过",
              file=sys.stderr)
        return None
    try:
        split = subprocess.run(
            [herdr, "pane", "split", "--current", "--direction", "right",
             "--no-focus", "--cwd", str(Path.cwd())],
            capture_output=True, text=True, timeout=10,
        )
        if split.returncode != 0:
            print(f"[herdr] pane split 失败: {split.stderr.strip()}",
                  file=sys.stderr)
            return None
        pane_id = json.loads(split.stdout)["result"]["pane"]["pane_id"]
        try:
            subprocess.run(
                [herdr, "pane", "wait-output", "--regex", r"[%$#]\s*$",
                 pane_id],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            pass  # 异壳/慢启动没等到提示符，照投（SKILL：未稳吞首字符的弱化）
        mon = subprocess.run(
            [herdr, "pane", "run", pane_id]
            + [_sh_join([t]) for t in (
                sys.executable, "-m", "evo_harness.cli", "monitor",
                "--shared", str(shared), "--run-id", run_id,
            )],
            capture_output=True, text=True, timeout=10,
        )
        if mon.returncode != 0:
            print(f"[herdr] pane run 失败: {mon.stderr.strip()}", file=sys.stderr)
            return None
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"[herdr] monitor 落位异常: {exc}", file=sys.stderr)
        return None
    print(f"[herdr] monitor 已落位右窗格 {pane_id}: run={run_id}",
          file=sys.stderr)
    _record_console(shared, run_id, "monitor",
                    {"plane": "herdr", "pane": pane_id})
    return pane_id



def _spawn_notifyd(run_id: str, shared: Path) -> bool:
    """决策唤醒守护无头拉起（不占窗格，2026-08-25 用户定调单窗格控制台）。

    守护动作写 notify_events.jsonl，由 monitor 融合渲染进事件流（※ 行），
    不再开裸 shell 窗格打日志。主窗格仍由 ``pane current`` 在拉起时钉死。
    fail-open：失败仅告警，run 照跑（决策仍可 wait-decision/monitor 手动参与）。
    """
    import shutil

    if os.environ.get("EVO_HERDR_MONITOR_PLACED") == "1":
        return False  # 控制台防双开（monitor/notifyd 共用一个守卫变量）
    if not _herdr_live_allowed():
        return False
    herdr = shutil.which("herdr")
    if herdr is None:
        return False  # monitor 落位已告过 herdr 缺失，这里静默
    try:
        cur = subprocess.run(
            [herdr, "pane", "current"],
            capture_output=True, text=True, timeout=10,
        )
        if cur.returncode != 0:
            print(f"[herdr] pane current 失败，notifyd 拉起跳过: "
                  f"{cur.stderr.strip()}", file=sys.stderr)
            return False
        main_pane = json.loads(cur.stdout)["result"]["pane"]["pane_id"]
        run_dir = Path(shared) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)  # 落位先于 flow：日志目录自建
        proc = subprocess.Popen(
            [sys.executable, "-m", "evo_harness.cli", "notifyd",
             "--shared", str(shared), "--run-id", run_id,
             "--main-pane", main_pane],
            stdout=(run_dir / "notifyd.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            **_detach_popen_kwargs(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[herdr] notifyd 拉起异常: {exc}", file=sys.stderr)
        return False
    _record_console(shared, run_id, "notifyd",
                    {"plane": "headless", "pid": proc.pid,
                     "target": {"plane": "herdr", "pane": main_pane}})
    print(f"[herdr] notifyd 已无头拉起 pid={proc.pid}"
          f"（注入目标 {main_pane}，事件入 monitor 流）: run={run_id}",
          file=sys.stderr)
    return True


def _write_pidfile(shared: Path, run_id: str) -> None:
    """flow 自写 pid（rmux tab 调度模式下无本地父进程可代写）。"""
    d = Path(shared) / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.pid").write_text(str(os.getpid()), encoding="utf-8")


def _spawn_flow_in_rmux_session(args, run_id: str) -> bool:
    """flow 新开 rmux 会话执行（2026-08-25 用户定调）。

    rmux daemon 独立，flow 只是它的 socket 客户端，放专属后台会话
    （``rmux new-session -d -s evo-<run_id>``）：不占 herdr tab、不挂
    宿主终端，herdr 关闭无碍；flow 退出会话随之自灭。命令内联
    EVO_HERDR_MONITOR_PLACED=1（防再调度）+ tee 落 harness.log。
    fail-open：rmux 侧失败回落本地路径（前台 / --detach）。
    """
    import shlex
    import shutil

    if not _herdr_live_allowed():
        return False
    rmux = shutil.which("rmux")
    if rmux is None:
        print("[rmux] 无 rmux 二进制，flow 会话调度跳过", file=sys.stderr)
        return False
    try:
        argv = [sys.executable, "-m", "evo_harness.cli",
                args.command, args.goal,
                "--shared", str(args.shared), "--run-id", run_id]
        if getattr(args, "fake", False):
            argv.append("--fake")
        if getattr(args, "plan_only", False):
            argv.append("--plan-only")
        if getattr(args, "rounds", None):
            argv += ["--rounds", str(args.rounds)]
        if getattr(args, "global_hooks", False):
            argv.append("--global-hooks")
        log = Path(args.shared) / run_id / "harness.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        # 环境前缀 + 重定向管道是 POSIX 语义；Windows 目标仓按 cmd 方言
        # 生成（set "K=V" && … 2>&1 | findstr 不等价，暂不追求，见 README 平台矩阵）
        cmd = ("EVO_HERDR_MONITOR_PLACED=1 "
               + _sh_join(argv)
               + f" 2>&1 | tee -a {_sh_join([str(log)])}")
        session = f"evo-{run_id}"
        proc = subprocess.run(
            [rmux, "new-session", "-d", "-s", session, cmd],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            print(f"[rmux] new-session 失败: "
                  f"{proc.stderr.strip() or proc.stdout.strip()}",
                  file=sys.stderr)
            return False
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[rmux] flow 会话调度异常: {exc}", file=sys.stderr)
        return False
    from .config import DEFAULT_AGENTS
    _record_console(
        Path(args.shared), run_id, "flow",
        {"plane": "rmux", "session": session,
         "workers": len([k for k in DEFAULT_AGENTS if k != "fake"])})
    print(f"[rmux] flow 已入会话 {session}（rmux daemon 调度）",
          file=sys.stderr)
    return True


def _detach(args) -> int:
    """自守护：以独立会话重新拉起自身，pid/日志落 shared/<run>/，秒回。"""
    from .filebus import FileBus

    run_id = _resolve_run_id(args)
    bus = FileBus(Path(args.shared), run_id)
    argv = [sys.executable, "-m", "evo_harness.cli",
            args.command, args.goal, "--shared", str(args.shared),
            "--run-id", run_id]
    if getattr(args, "fake", False):
        argv.append("--fake")
    if getattr(args, "plan_only", False):
        argv.append("--plan-only")
    if getattr(args, "rounds", None):
        argv += ["--rounds", str(args.rounds)]
    if getattr(args, "global_hooks", False):
        argv.append("--global-hooks")
    log = bus.root / "harness.log"
    proc = subprocess.Popen(
        argv,
        stdout=log.open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        env={**os.environ, "EVO_HERDR_MONITOR_PLACED": "1"},
        **_detach_popen_kwargs(),
    )
    (bus.root / "run.pid").write_text(str(proc.pid), encoding="utf-8")
    print(f"detached: run={run_id} pid={proc.pid}")
    print(f"  轮询: evo-harness status --run-id {run_id} --json")
    print(f"  阻塞: evo-harness wait --run-id {run_id}")
    print(f"  日志: {log}")
    return 0


async def _wait_command(shared: Path, run_id: str, timeout: float) -> int:
    """事件式等待：run.json 每次变化触发重查，终态即出（无 sleep 轮询）。"""
    from .events import AsyncFileWatcher

    run_json = Path(shared) / run_id / "run.json"
    if not run_json.exists():
        print(f"无 run.json: {run_json.parent}", file=sys.stderr)
        return 1

    def _terminal() -> tuple[bool, dict]:
        try:
            snap = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, {}
        return snap.get("status") in ("done", "escalated", "aborted"), snap

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    done, snap = _terminal()
    if not done:
        watcher = AsyncFileWatcher(run_json.parent)
        try:
            await watcher.wait_until(
                lambda: _terminal()[0], deadline - loop.time()
            )
        finally:
            watcher.close()
        done, snap = _terminal()
    if done:
        print(f"run={snap.get('run_id')} -> {snap.get('stage')}/{snap.get('status')}")
        return 0 if snap.get("status") == "done" else 2
    print(f"wait 超时（{timeout}s），run 仍在 {snap.get('stage', '?')}",
          file=sys.stderr)
    return 3


async def _run_flow(args) -> int:
    """run / review-cycle / abort 的异步主流程。"""
    from .stages import Harness
    from .statemachine import HardStop

    config = HarnessConfig(shared_root=args.shared)
    config.install_global_hooks = getattr(args, "global_hooks", False)
    run_id = _resolve_run_id(args)
    # abort 前置校验：run 目录不存在直接报错·否则 FileBus 构造会给
    # 错误的 run-id 空建一套目录（伪终态，真 run 毫发无损）
    if args.command == "abort" and not (
        Path(args.shared) / run_id / "run.json"
    ).exists():
        print(f"run 不存在: {Path(args.shared) / run_id}", file=sys.stderr)
        return 1
    if args.command != "abort":
        _write_pidfile(Path(args.shared), run_id)

    if getattr(args, "fake", False):
        config.fake_agent_script = FAKE_SCRIPT
    harness = Harness(config, run_id, repo_root=Path.cwd())

    if args.command == "review-cycle":
        return await harness.review_cycle(
            args.goal, max_rounds=getattr(args, "rounds", 3)
        )

    if args.command == "abort":
        await harness.abort()
        print(f"已终止 {run_id}（会话与 worktree 已清理）")
        return 0

    if getattr(args, "plan_only", False):
        await harness._prepare()
        try:
            harness.sm.enter("explore")
            await harness._stage_explore(args.goal)
            harness.sm.enter("research")
            await harness._stage_research(args.goal)
            harness.sm.enter("plan")
            await harness._stage_plan(args.goal)
        except HardStop as exc:
            harness.bus.set_stage("ESCALATE", "escalated")
            harness.bus.log_event("hard_stop", f"{exc.kind}: {exc.detail}")
            print(f"[evo-harness] 硬停止 {exc.kind}: {exc.detail}", flush=True)
            return 2
        print("plan-only 完成：三契约已落盘")
        return 0

    return await harness.run(args.goal)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    if args.command == "contract":
        from .contracts import ContractStore, ContractError
        run_id = args.run_id or _latest_run_id(Path(args.shared))
        if run_id is None:
            print("无 run（--run-id 缺省取最新失败）", file=sys.stderr)
            return 1
        store = ContractStore(args.shared, run_id)
        try:
            if args.what == "goal":
                store.goal(args.goal, args.non_goal, args.criterion)
            elif args.what == "step":
                store.step(args.id, args.title,
                           [x for x in args.after.split(",") if x])
            elif args.what == "alloc":
                store.alloc(args.unit, args.branch, args.scope,
                            args.criterion)
            elif args.what == "merge-order":
                store.merge_order(
                    [x for x in args.order.split(",") if x.strip()]
                )
        except ContractError as exc:
            print(f"contract: {exc}", file=sys.stderr)
            return 3
        print(f"contract {args.what}: ok", flush=True)
        return 0

    if args.command == "wait-decision":
        import time as _t
        run_id = args.run_id or _latest_run_id(Path(args.shared))
        ddir = Path(args.shared) / run_id / "decisions"
        deadline = _t.monotonic() + args.timeout
        while _t.monotonic() < deadline:
            pend = None
            if ddir.is_dir():
                for f in sorted(ddir.glob("*.json")):
                    try:
                        d = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if d.get("status") == "pending":
                        pend = d
                        break
            if pend:
                print(f"※ 决策请求 [{pend['node']}] run={run_id}")
                print(f"  简报: {pend.get('brief', '')}")
                print(f"  选项: {', '.join(pend.get('choices', []))}")
                print(f"  决策: uv run evo-harness decide --run-id {run_id} "
                      f"--node {pend['node']} --choice <选项> "
                      f"--rationale '理由'", flush=True)
                return 0
            _t.sleep(2.0)
        print("wait-decision 超时：无待决策", flush=True)
        return 1

    if args.command == "notifyd":
        from .config import HarnessConfig
        from .notifyd import run_notifyd
        run_id = args.run_id or _latest_run_id(Path(args.shared))
        if run_id is None:
            print("无 run（--run-id 缺省取最新失败）", file=sys.stderr)
            return 1
        budget = HarnessConfig(shared_root=args.shared).budget
        return run_notifyd(
            Path(args.shared), run_id, main_pane=args.main_pane,
            poll=budget.notify_poll_seconds,
            spacing=budget.notify_spacing_seconds,
            idle_wait_ms=(args.idle_wait_ms or budget.notify_idle_wait_ms),
        )

    if args.command == "clean-wt":
        from .worktrees import clean_run_worktrees
        cleaned = clean_run_worktrees(Path.cwd(), args.run_id)
        print(f"clean-wt {args.run_id}: {cleaned or '无可清'}", flush=True)
        return 0

    if args.command == "decide":
        run_id = args.run_id or _latest_run_id(Path(args.shared))
        if run_id is None:
            print("无 run", file=sys.stderr)
            return 1
        from .filebus import FileBus
        if FileBus(Path(args.shared), run_id).decide(
            args.node, args.choice, args.rationale
        ):
            print(f"decide {args.node} → {args.choice}: ok", flush=True)
            return 0
        print(f"decide 失败：节点不存在/已决/choice 非法", file=sys.stderr)
        return 4

    if args.command in ("status", "wait", "abort"):
        run_id = args.run_id or _latest_run_id(Path(args.shared))
        if run_id is None:
            print("无 run（shared/ 下没有 run.json）", file=sys.stderr)
            return 1
        args.run_id = run_id  # abort 缺省也锚定最新 run，不生成新时间戳 id

    if args.command == "monitor":
        from .monitor import run_monitor

        rid = args.run_id or _latest_run_id(Path(args.shared))
        if rid is None:
            print("无 run", file=sys.stderr)
            return 1
        return run_monitor(Path(args.shared), rid)

    if args.command == "status":
        # 与 abort 同款前置校验：run.json 不存在即报错·否则 FileBus 构造会
        # 给不存在的 run-id 空建目录并伪造 stage=IDLE status=running
        if not (Path(args.shared) / run_id / "run.json").exists():
            print(f"run 不存在: {Path(args.shared) / run_id}", file=sys.stderr)
            return 1
        snap = _run_snapshot(Path(args.shared), run_id)
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print(f"run={snap['run_id']} stage={snap['stage']} status={snap['status']}")
            if "pid" in snap:
                print(f"pid={snap['pid']} alive={snap['pid_alive']}")
            for u in snap["units"]:
                mark = "✓" if u["result_ready"] else "…"
                state = u["agent_state"] or "-"
                print(f"  {mark} {u['unit_id']}  agent_state={state}")
            for ev in snap["history"][-8:]:
                print(f"  [{ev['kind']}] {ev['detail']}")
        return 0

    if args.command == "wait":
        return asyncio.run(
            _wait_command(Path(args.shared), run_id, args.timeout)
        )

    if args.command in ("run", "review-cycle"):
        rc = preflight(require_rmux=True)
        if rc is not None:
            return rc

    # P8.4 monitor + notifyd 控制台自动落位 + flow rmux 调度：herdr 会话内
    # （HERDR_ENV=1 由 herdr 起 pane 时注入）。EVO_HERDR_MONITOR_PLACED=1
    # 是祖先守卫·被调度进 tab 的 flow 子进程带此标记，跳过调度分支直接
    # 本地执行（否则子生孙无限 fork：r5 事故 30s 200+ tab [实证: 2026-08-25]）。
    # run_id 先锚定，落位/自守护子进程/主流程三者同 id。
    if args.command in ("run", "review-cycle"):
        if not args.run_id:
            args.run_id = _resolve_run_id(args)
        if (
            args.pane != "none"
            and os.environ.get("EVO_HERDR_MONITOR_PLACED") != "1"
            and (
                args.pane == "right"
                or (args.pane == "auto"
                    and os.environ.get("HERDR_ENV") == "1")
            )
        ):
            _place_herdr_monitor(args.run_id, Path(args.shared))
            nd = _spawn_notifyd(args.run_id, Path(args.shared))
            if _spawn_flow_in_rmux_session(args, args.run_id):
                # CLI 即退：rmux 接管一切（flow 会话 + monitor + notifyd 注入）
                print(f"run={args.run_id} 已交 rmux 接管：flow 会话执行、"
                      f"monitor 右窗格"
                      + ("；※/✔ 将注入本窗格" if nd else "；决策走 monitor ※"))
                return 0
            # herdr 侧失败：fail-open 回落本地路径（前台 / --detach）

    if args.command in ("run", "review-cycle") and args.detach:
        return _detach(args)

    return asyncio.run(_run_flow(args))


if __name__ == "__main__":
    sys.exit(main())
