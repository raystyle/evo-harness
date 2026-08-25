"""evo-harness 监控：三 agent 状态 + 执行日志双区专注视图。

上半：三 agent 卡片（agent 名 / 当前任务 / hook 态 / 完成数）
下半：事件流（agent 过滤高亮 + 时间轴 + 完整详情）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from rich import box
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.align import Align
from rich.text import Text

from .filebus import goal_brief

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# 赛博朋克霓虹板（真彩 hex；终端不支持时 rich 自动降级 ANSI16）
NEON_CYAN = "#00ffff"      # 主电网青 ， 流程/结构/系统域（GRAPH、AGENTS、派发）
NEON_MAGENTA = "#ff00ff"   # 主霓虹品红 ， 唤醒域主色（NOTIFY 面板 + ※/※ 事件）
NEON_PINK = "#ff2d95"      # 热粉
NEON_YELLOW = "#ffd300"    # 高压钠黄 ， AGENTS 面板主色 + 注意/在途语义
NEON_GREEN = "#00ff87"     # 荧光绿
NEON_VIOLET = "#875fff"    # 紫电 ， GOAL 专属身份色（全屏唯一）
DIM_GRID = "#5f5f87"       # 暗紫网格（结构线）

KIND_STYLE = {
    "decision": f"bold {NEON_MAGENTA}",  # ※ 与 ※ 同属唤醒域，单紫不混
    "notify": f"bold {NEON_MAGENTA}",  # notifyd 守护事件（※，事件流融合）
    "dispatch": NEON_CYAN, "tasks_done": NEON_GREEN, "gate": f"bold {NEON_GREEN}",
    "aggregate": NEON_CYAN, "round": NEON_YELLOW, "nudge": NEON_YELLOW,
    "hard_stop": f"bold {NEON_PINK}", "consensus": f"bold {NEON_GREEN}",
    "final": f"bold {NEON_GREEN}",
}

STAGE_ICON = {"REVIEW_RUNNING": "◇", "EXECUTE_RUNNING": "◆", "MERGE_RUNNING": "▣",
              "DONE": "✓", "ESCALATE": "✗"}
STATE_STYLE = {"working": f"bold {NEON_CYAN}", "idle": NEON_GREEN,
               "blocked": f"bold {NEON_PINK}", "done": f"bold {NEON_GREEN}",
               "unknown": NEON_YELLOW}
STATE_DOT = {"working": "●", "idle": "○", "blocked": "⊘", "done": "✓",
             "unknown": "?"}
AGENT_COLOR = {"claude": "#ff5fff", "kimi": "#00d7ff", "codex": NEON_GREEN,
               "grok": "#ff8c00"}  # grok 霓虹橙（五色域外的新身份色）

import re as _re


def _fmt_event(kind: str, detail: str) -> tuple[str, str]:
    """把原始事件格式化为 (icon, 精简详情)。返回图标和美化后的文本。

    所有返回值必须单视觉行（≤~72 字符）：滚动窗口按逻辑行计数，长行折行
    会让面板物理超高、最新事件被裁出视野（p8-herdr-r1 实证「不滚动」假象）。
    """
    if kind == "run_start":
        return "⌖", goal_brief(detail)  # 全文在 GOAL 面板，流里只留一句
    if kind == "notify":
        return "※", detail[:72]  # notifyd 守护动作（注入/收尾通知）
    if kind == "decision":
        # "plan-approval 请求决策（默认 approve…）: brief" / "plan-approval → approve"
        if " → " in detail:
            return "※", detail[:72]
        m = _re.match(r"(\S+) 请求决策", detail)
        return "※", (f"{m.group(1)} 等待主 agent 决策" if m else detail[:72])
    if kind == "hooks":
        return "·", "claude/codex/kimi 三端状态 hook 已安装"
    if kind == "pool":
        m = _re.search(r"(\d+) worker.*?(\d+)s", detail)
        if m:
            return "≡", f"常驻池 {m.group(1)} worker 就绪 ({m.group(2)}s)"
        return "≡", detail
    if kind == "enter_stage":
        descs = {"review": "三端并行复核", "execute": "修复 must-fix", "merge": "终审整合",
                 "plan": "生成执行计划", "research": "深度分析", "explore": "广域搜索"}
        d = descs.get(detail.strip(), "")
        return "▶", f"{detail.strip().upper()} ── {d}"
    if kind == "round":
        m = _re.search(r"第 (\d+)/(\d+) 轮 (\w+)", detail)
        if m:
            cur, total, name = int(m.group(1)), int(m.group(2)), m.group(3)
            dots = "●" * cur + "○" * (total - cur)
            return "↻", f"{dots} Round {cur}/{total} ── {name} 阶段开始"
        return "↻", detail
    if kind == "dispatch":
        # "rc-r1-claude → worker-claude（claude）file=prompt.md" → "claude ← rc-r1-claude (prompt.md)"
        m = _re.search(r"(\S+) → worker-\w+（(\w+)）(?:file=(\S+))?", detail)
        if m:
            extra = f" ({m.group(3)})" if m.group(3) else ""
            return "»", f"{m.group(2)} ← {m.group(1)}{extra}"
        return "»", detail
    if kind == "tasks_done":
        # "explore: {"t1": "done", ...}" → 报任务名的完成行（读者视角：
        # 「哪批活、成了几个」；批次名+数字对读者无信息量）
        m = _re.match(r"(\S+?): (.*)$", detail, _re.S)
        if m:
            try:
                res = json.loads(m.group(2))
                ok_ids = [k for k, v in res.items() if v == "done"]
                bad_ids = [k for k, v in res.items() if v != "done"]
                if len(ok_ids) <= 3:
                    txt = "、".join(ok_ids) + " 完成"
                else:
                    txt = f"{ok_ids[0]}…{ok_ids[-1]} 共 {len(ok_ids)} 个完成"
                if bad_ids:
                    txt += f"（未成: {'、'.join(bad_ids[:2])}）"
                return "✓" if not bad_ids else "◐", txt
            except json.JSONDecodeError:
                pass
        return "✓", detail[:60]
    if kind == "aggregate":
        r_count = detail.count("REGRESSION")
        a_count = detail.count("AGREE")
        m = _re.search(r"must-fix=(\d+)", detail)
        fixes = m.group(1) if m else "0"
        rm = _re.search(r"r(\d+)", detail)
        rn = f"R{rm.group(1)}" if rm else ""
        status = "需要修复" if r_count > 0 else "✓ 一致"
        return "Σ", f"{rn} REGRESSION×{r_count} AGREE×{a_count} → {fixes} must-fix ({status})"
    if kind == "nudge":
        m = _re.search(r"催写.*?(\S+)", detail)
        return "↯", detail[:60]
    if kind == "hard_stop":
        return "✗", detail
    if kind == "consensus":
        return "★", detail
    if kind == "final":
        return "▤", "终审报告已生成"
    if kind == "revise":
        return "↺", detail
    return "", detail


def _rj(p, d=None):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return d


def _age(ts):
    s = max(0, int(time.time() - ts))
    h, rem = divmod(s, 3600); m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _clk(ts): return time.strftime("%H:%M:%S", time.localtime(ts))


def _daemon_state(root: Path, max_age: float = 15.0) -> tuple[str, dict]:
    """notifyd 值守态：(fresh|stale|off, daemon 块)。off=未拉起/无心跳。"""
    d = _rj(root / "notify_state.json", {}).get("daemon") or {}
    if not d.get("beat"):
        return "off", d
    return ("fresh" if time.time() - d["beat"] <= max_age else "stale"), d


def _decision_rows(root: Path) -> list[dict]:
    """decisions/*.json 逐节点状态（pending/decided + 注入账本交叉）。"""
    ddir = root / "decisions"
    out: list[dict] = []
    if ddir.is_dir():
        ledger = _rj(root / "notify_state.json", {}).get("nodes", {})
        for f in sorted(ddir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            d["_stem"] = f.stem
            d["_injected"] = ledger.get(f.stem)
            out.append(d)
    return out


def _log_usable(th: int, units: bool, notify_rows: int) -> int:
    """事件流可用行数 = 终端高 - 固定面板 - 可选面板（units/notify）。
    漏扣可选面板会让最新事件被裁出视野（rc-r2/r3 三方 must-fix）。"""
    fixed = (1 + 3 + 4 + 8 + 2
             + (3 if units else 0)
             + ((3 + notify_rows) if notify_rows else 0))
    return max(4, th - fixed)


def _units_panel(root: Path) -> Panel | None:
    """执行单元清单（console.json，落位时记录）：哪个单元、哪个平面、
    哪个 pane，rmux 控制平面 / herdr 操作平面 / 无头，一眼分明。"""
    inv = _rj(root / "console.json")
    if not inv:
        return None
    ps = {"rmux": f"bold {NEON_CYAN}", "herdr": NEON_MAGENTA,
          "headless": "grey78"}

    t = Text()

    def _u(icon: str, name: str, plane: str, loc: str = "",
           arrow: tuple[str, str] = ()) -> None:
        if t.plain:
            t.append("  ·  ", style=DIM_GRID)
        t.append(f"{icon} ", style="white")
        t.append(name, style="bold white")
        t.append(f" {plane}", style=ps.get(plane, "white"))
        if loc:
            t.append(f"·{loc}", style="grey50")
        if arrow:
            t.append(" → ", style=DIM_GRID)
            t.append(arrow[1], style=ps.get(arrow[0], "grey50"))

    f = inv.get("flow") or {}
    if f:
        _u("▶", "flow", f.get("plane", "rmux"),
           str(f.get("session") or f.get("pane") or ""))
        if f.get("workers"):
            _u("≡", f"workers×{f['workers']}", "rmux")
    mon = inv.get("monitor") or {}
    if mon:
        _u("▤", "monitor", mon.get("plane", "herdr"),
           str(mon.get("pane", "")))
    # notifyd 不入此行：NOTIFY 面板有完整值守可视化（含平面标注），不重复
    if not t.plain:
        return None
    return Panel(Align(t, align="center"),
                 title=f"[bold {DIM_GRID}]▤ UNITS[/]",
                 border_style=DIM_GRID, box=box.ROUNDED, padding=(0, 1))


def _notify_panel(root: Path, spin: str) -> Panel:
    """值守状态 + 决策节点全景（用户定调：一切状态都要可视化；icon 素化，
    沿用全局既有标记 ※ 决策 / ✓ 成 / ✗ 败，值守行靠颜色+spinner 表身份，
    排版对齐 GOAL 面板：行内居中、段间 · 分隔）。"""
    state, dm = _daemon_state(root)
    rows: list[Align] = []
    if state == "fresh":
        t = Text()
        t.append(f"{spin} ", style=f"bold {NEON_MAGENTA}")
        t.append("notifyd 值守 ", style=f"bold {NEON_MAGENTA}")
        t.append("无头 ", style="grey78")
        t.append("→ ", style=DIM_GRID)
        t.append("herdr", style=NEON_MAGENTA)
        t.append(f"·{dm.get('main_pane', '?')}", style="grey50")
        t.append("   ·   ", style=DIM_GRID)
        t.append(f"心跳 {_age(dm.get('beat', 0))}", style="grey50")
        rows.append(Align(t, align="center"))
    elif state == "stale":
        t = Text()
        t.append("✗ ", style=f"bold {NEON_PINK}")
        t.append("notifyd 失联 ", style=f"bold {NEON_PINK}")
        t.append("   ·   ", style=DIM_GRID)
        t.append(f"心跳 {_age(dm.get('beat', 0))} 前   ·   pid={dm.get('pid', '?')}",
                 style="grey50")
        rows.append(Align(t, align="center"))
    else:
        rows.append(Align(
            Text("notifyd 未拉起（非 herdr 会话或已退出）", style="dim"),
            align="center"))

    pend = 0
    for d in _decision_rows(root):
        stem = d.get("node", d.get("_stem", "?"))
        if d.get("status") == "decided":
            t = Text()
            t.append("✓ ", style=f"bold {NEON_GREEN}")
            t.append(f"{stem}", style=f"bold {NEON_GREEN}")
            t.append("  →  ", style=DIM_GRID)
            t.append(str(d.get("choice", "?")), style=NEON_GREEN)
            rows.append(Align(t, align="center"))
            continue
        pend += 1
        t = Text()
        t.append("※ ", style=f"bold {NEON_MAGENTA}")
        t.append(f"{stem}", style=f"bold {NEON_MAGENTA}")
        t.append("   ·   ", style=DIM_GRID)
        t.append(f"[{' · '.join(d.get('choices', []))}]", style="grey78")
        inj = d.get("_injected")
        t.append("   ·   ", style=DIM_GRID)
        if inj:
            t.append(f"已注入 {_clk(inj.get('last', 0))}"
                     + ("" if inj.get("ok") else "（退避中）"),
                     style=NEON_MAGENTA if inj.get("ok") else NEON_YELLOW)
        else:
            t.append("待注入", style="grey50")
        rows.append(Align(t, align="center"))
    border = NEON_MAGENTA if pend or state == "stale" else DIM_GRID
    return Panel(Group(*rows),
        title=f"[bold {NEON_MAGENTA}]※ NOTIFY / DECISIONS[/]",
        border_style=border, box=box.ROUNDED, padding=(0, 1),
    )


def _display_state(state, worker, task, agent):
    """五态展示：hook 通道四态权威，done 由 idle + worker.json.seen=False 派生。

    herdr 的分层建模（research-synth §五）：底层检测只有
    idle/working/blocked/unknown 四态；`done` 是「idle 但 tab 未见」的
    展示态，`unknown` 不装懂。缺 hook 状态时的兜底仅用于 fake/旧 run，
    避免把真 agent 的未知静默误报成 idle。
    """
    if state == "idle":
        return "done" if not worker.get("seen", True) else "idle"
    if state in ("working", "blocked", "unknown"):
        return state
    if task:
        return "working"  # fake/旧 run：领活但 hook 未上报，按在干显示
    if worker.get("agent", agent) == "fake" or agent == "fake":
        return "idle"
    return "unknown"


def _merged_history(root: Path, history: list[dict]) -> list[dict]:
    """事件流融合（2026-08-25 用户定调：notifyd 不占窗格，动作进 monitor 流）。

    notify_events.jsonl 由守护单写追加，这里只读合并、按 t 排序与
    run.json 历史交织成一条时间轴。
    """
    p = root / "notify_events.jsonl"
    if not p.exists():
        return history
    extra = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("kind") == "notify":
            extra.append(d)
    return sorted([*history, *extra], key=lambda e: e.get("t", 0)) if extra \
        else history


def build(root: Path, tick: int) -> Layout:
    run = _rj(root / "run.json", {})
    history = _merged_history(root, run.get("history", []))
    status = run.get("status", "?")
    stage = run.get("stage", "?")
    sc = {"done": NEON_GREEN, "escalated": NEON_PINK, "aborted": NEON_YELLOW}.get(status, NEON_CYAN)
    icon = STAGE_ICON.get(stage, "▶")
    spin = SPIN[tick % len(SPIN)]
    pulse = tick % 2 == 0

    layout = Layout()
    # ※ NOTIFY 面板：守护拉起过或出现过决策节点才占行（fake/非 herdr run 不留空框）
    dec_rows = _decision_rows(root)
    show_notify = _daemon_state(root)[0] != "off" or bool(dec_rows)
    units = _units_panel(root)
    layout.split_column(
        Layout(name="top", size=1),
        *( [Layout(name="units", size=3)] if units else [] ),
        Layout(name="goal", size=3),
        Layout(name="pipe", size=4),
        *( [Layout(name="notify", size=3 + min(len(dec_rows), 4))]
           if show_notify else [] ),
        Layout(name="agents", size=8),
        Layout(name="log"),
    )

    # ---- 顶栏（1行紧凑）----
    top = Text()
    top.append(f" {spin} ", style=f"bold {NEON_CYAN}")
    top.append(f" {root.name} ", style=f"bold #0a0a12 on {NEON_CYAN}")
    top.append(f" {icon} {stage}·{status} ", style=f"bold {sc}")
    top.append(f" ⏱{_age(run.get('created_at', time.time()))} ", style="dim")
    pf = root / "run.pid"
    if pf.exists():
        from .cli import _pid_alive  # 跨平台探测（Windows 的 kill(pid,0) 是杀伤）

        try:
            alive = _pid_alive(int(pf.read_text().strip()))
        except (OSError, ValueError):
            alive = False
        top.append(" ✓" if alive else " ✗", style=NEON_GREEN if alive else NEON_PINK)
    layout["top"].update(top)

    # ---- GOAL 独立可视化区（一句精练描述，生成任务时存 goal_brief）----
    goal = str(run.get("goal", "")).strip()
    brief = goal_brief(goal) if goal else ""
    layout["goal"].update(Panel(
        Align(Text(brief if brief else "—", style="grey78" if brief else "dim"),
              align="center", vertical="middle"),
        title=f"[bold {NEON_VIOLET}]⌖ GOAL[/]",
        border_style=NEON_VIOLET if goal else DIM_GRID,
        box=box.ROUNDED,
        padding=(0, 1),
    ))

    # ---- graph loop 两行可视化（全词节点 + 循环回路线）----
    stage_lower = stage.lower().replace("_running", "")
    in_loop = stage_lower in ("execute", "review")

    def _node_style(name):
        if name == stage_lower:
            # 运行中节点呼吸闪烁：亮底 ↔ 亮字
            if name == "execute":
                return f"bold #0a0a12 on {NEON_YELLOW}" if pulse else f"bold {NEON_YELLOW}"
            return f"bold #0a0a12 on {NEON_CYAN}" if pulse else f"bold {NEON_CYAN}"
        passed = stage_lower in ("execute", "review", "merge") or stage in ("DONE", "ESCALATE")
        if name in ("explore", "research", "plan") and passed:
            return f"dim {NEON_GREEN}"
        if name == "merge" and stage == "DONE":
            return f"dim {NEON_GREEN}"
        return DIM_GRID

    # 每节点任务进度：派发过的任务 × out/ 已有产物（逐任务实时口径）
    stage_tasks: dict[str, list[str]] = {}
    _cur = "prepare"
    for ev0 in history:
        k0 = ev0.get("kind", "")
        if k0 == "enter_stage":
            _cur = str(ev0.get("detail", "")).strip()
            stage_tasks.setdefault(_cur, [])
        elif k0 == "dispatch" and _cur in stage_tasks:
            m0 = _re.match(r"(\S+) → ", str(ev0.get("detail", "")))
            if m0:
                stage_tasks[_cur].append(m0.group(1))

    def _badge(name: str) -> str:
        tids = stage_tasks.get(name)
        if not tids:
            return ""
        done = sum(1 for t in tids if any((root / "tasks" / t / "out").glob("*")))
        return f" {done}/{len(tids)}"

    # ---- GRAPH 上行：节点带进度徽标 ----
    l1 = Text()
    l1.append(" ", style="")
    pre = 1
    for s in ("explore", "research", "plan"):
        lbl = s.upper() + _badge(s)
        l1.append(lbl, style=_node_style(s))
        l1.append(" ─→ ", style=DIM_GRID)
        pre += len(lbl) + 4
    loop_c = NEON_CYAN if in_loop else DIM_GRID
    e_lbl = "EXECUTE" + _badge("execute")
    r_lbl = "REVIEW" + _badge("review")
    l1.append("╭", style=loop_c)
    l1.append(e_lbl, style=_node_style("execute"))
    l1.append(" ↻ ", style=loop_c)
    l1.append(r_lbl, style=_node_style("review"))
    l1.append("╮", style=loop_c)
    l1.append(" ─→ ", style=DIM_GRID)
    l1.append("MERGE" + _badge("merge"), style=_node_style("merge"))
    l1.append(" ─→ ", style=DIM_GRID)
    if stage == "DONE":
        l1.append("✓ DONE", style=f"bold {NEON_GREEN}")
    elif stage == "ESCALATE":
        l1.append("✗ HALT", style=f"bold {NEON_PINK}")
    else:
        l1.append("DONE", style=DIM_GRID)

    # ---- GRAPH 下行：回路线（动态对齐）+ 循环轮数 ----
    inner = len(e_lbl) + 3 + len(r_lbl)
    h2 = (inner - 1) // 2
    l2 = Text()
    l2.append(" " * pre, style="")
    l2.append("╰", style=loop_c)
    if in_loop:
        span = inner - 1
        pos = span - 1 - (tick % span)
        for i in range(inner):
            if i == h2:
                l2.append("←", style=loop_c)
            elif i == pos:
                l2.append("●", style=f"bold {NEON_YELLOW}")
            else:
                l2.append("─", style=loop_c)
    else:
        l2.append("─" * h2, style=loop_c)
        l2.append("←", style=loop_c)
        l2.append("─" * (inner - 1 - h2), style=loop_c)
    l2.append("╯", style=loop_c)
    # 轮次计数已删（验收反馈：事件流的 ↻ Round 头已表轮次，loop 行不重复）

    from rich.console import Group as _G
    layout["pipe"].update(Panel(
        Align(_G(l1, l2), align="center"),
        title=f"[bold {NEON_CYAN}]◇ GRAPH / LOOP[/]",
        border_style=NEON_CYAN if in_loop else DIM_GRID,
        box=box.ROUNDED,
        padding=(0, 1),
    ))

    # ---- 三 agent 卡片（品牌色，working 亮框 + 转轮）----
    cards = Table.grid(padding=(0, 1), expand=True)
    for _ in range(3):
        cards.add_column(ratio=1)
    wdir = root / "units" / "_pool"
    entries = []
    running: set[str] = set()  # 领活中的 task（派发行尾追动态转轮用）
    if wdir.is_dir():
        for d in sorted(wdir.iterdir()):
            if not d.is_dir():
                continue
            cur = _rj(d / "current.json", {})
            worker = _rj(d / "worker.json", {})
            state = _rj(d / "state.json", {}).get("state")
            agent = cur.get("agent", worker.get("agent", d.name.replace("worker-", "")))
            task = cur.get("task", "")
            st = _display_state(state, worker, task, agent)
            if task and st == "working":
                running.add(task)
            n = sum(1 for e in history if e.get("kind") == "dispatch"
                    and f"（{agent}）" in e.get("detail", ""))
            ac = AGENT_COLOR.get(agent, "white")
            ss = STATE_STYLE.get(st, "dim")
            border = ac if st in ("working", "blocked") else (
                NEON_GREEN if st == "done" else DIM_GRID)
            card = Table.grid(padding=(0, 1))
            card.add_column(justify="left")
            card.add_row(Text(f"{STATE_DOT.get(st, '·')} {agent}", style=f"bold {ac}"))
            card.add_row(Text(f"  {st}" + (f" {spin}" if st == "working" else ""),
                              style=ss))
            card.add_row(Text(f"  {task or '空闲'}", style=ss if task else "dim"))
            card.add_row(Text(f"  ×{n}", style="dim"))
            entries.append(Panel(card, border_style=border,
                                 box=box.ROUNDED, padding=(0, 1)))
    # 卡格随池伸缩：≤3 端三列，四端一列四卡（面板高度按单行设计）
    if entries:
        per_row = 4 if len(entries) > 3 else 3
        while len(entries) % per_row:
            entries.append(Text(""))
        for i in range(0, len(entries), per_row):
            cards.add_row(*entries[i:i + per_row])
    if units:
        layout["units"].update(units)
    if show_notify:
        layout["notify"].update(_notify_panel(root, spin))
    layout["agents"].update(Panel(
        cards,
        title=f"[bold {NEON_YELLOW}]≡ AGENTS[/]",
        border_style=NEON_YELLOW,
        box=box.ROUNDED,
        padding=(0, 1),
    ))

    # ---- 事件流：任务行「开始 → 结束」；进行中结束槽 = 转轮+跳动时长 ----
    HIDDEN_KINDS = {"hard_stop", "final", "run_exit", "tasks_done"}
    last_stage_ev = next(
        (e for e in reversed(history) if e.get("kind") == "enter_stage"), None)
    last_round_ev = next(
        (e for e in reversed(history) if e.get("kind") == "round"), None)
    STAGE_ICONS_MAP = {"review": "◇", "execute": "◆", "merge": "▣", "plan": "▤",
                       "research": "◎", "explore": "○", "prepare": "·"}
    lines: list[Text] = []

    _MULTI = {"planner": ("goal_spec.json", "plan.json", "allocations.json")}

    def _end_ts(tid: str) -> float | None:
        td = root / "tasks" / tid
        # 权威：pool 的完成标记（多产物任务全齐才落）
        dj = td / "done.json"
        if dj.exists():
            try:
                if json.loads(dj.read_text(encoding="utf-8")).get("ok"):
                    return dj.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                pass
        od = td / "out"
        if not od.is_dir():
            return None
        outs = [o for o in od.iterdir() if o.stat().st_size > 0]
        if not outs:
            return None
        # 旧 run 兜底：planner 三契约全齐才算完（任意产物启发式会过早报 ✓）
        if tid in _MULTI:
            need = _MULTI[tid]
            if not all((od / n).is_file() and (od / n).stat().st_size > 0
                       for n in need):
                return None
        return max(o.stat().st_mtime for o in outs)

    for ev in history:
        kind = ev.get("kind", "")
        if kind in HIDDEN_KINDS:
            continue
        raw = str(ev.get("detail", ""))
        ts = ev.get("t", 0)
        ks = KIND_STYLE.get(kind, "white")
        icon, detail = _fmt_event(kind, raw)

        if kind == "enter_stage":
            cur_stage = raw.strip()
            t = Text()
            t.append(f"  {STAGE_ICONS_MAP.get(cur_stage, '▶')} {cur_stage.upper()} ",
                     style=f"bold {NEON_YELLOW}")
            if ev is last_stage_ev and status == "running":
                t.append(spin, style=f"bold {NEON_CYAN}")
            t.append(" " + "─" * 28, style=DIM_GRID)
            lines.append(t)
            continue

        if kind == "round":
            m = _re.search(r"第 (\d+)/(\d+)", raw)
            t = Text("  ", style="")
            t.append(f"↻ Round {m.group(1)}/{m.group(2)}" if m else "↻ Round",
                     style=f"bold {NEON_CYAN}")
            if ev is last_round_ev and status == "running":
                t.append(f" {spin}", style=f"bold {NEON_CYAN}")
            t.append(" " + "─" * 18, style=DIM_GRID)
            lines.append(t)
            continue

        if kind == "dispatch":
            m = _re.search(r"(\S+) → worker-\w+（(\w+)）", raw)
            if not m:
                continue
            tid, agent = m.group(1), m.group(2)
            t = Text("  ")
            t.append(f"{_clk(ts)} ", style="grey50")
            end = _end_ts(tid)
            if end is not None:
                t.append(f"→ {_clk(end)} ", style="grey50")
                t.append("✓ ", style=NEON_GREEN)
                t.append(f"{agent:<7}", style=f"bold {AGENT_COLOR.get(agent, 'white')}")
                t.append(tid, style="")
            else:
                el = max(0, int(time.time() - ts))
                t.append(f"→ {spin} {el // 60}:{el % 60:02d} ",
                         style=f"bold {NEON_CYAN}")
                t.append("» ", style=NEON_CYAN)
                t.append(f"{agent:<7}", style=f"bold {AGENT_COLOR.get(agent, 'white')}")
                t.append(tid, style=f"bold {NEON_CYAN}")
            lines.append(t)
            continue

        # 注记行（gate/pool/hooks/decision/nudge/stalled…）
        t = Text("  ")
        t.append(f"{_clk(ts)} ", style="grey50")
        if icon:
            t.append(f"{icon} ", style=ks)
        t.append(detail, style=ks if kind in ("aggregate", "gate", "decision") else "")
        if len(t.plain) > 76:
            t = Text(t.plain[:75] + "…")
        lines.append(t)

    # 滚动窗口：超出可视高度的旧事件上滚收起，可视区只留最新
    try:
        th = os.get_terminal_size().lines
    except OSError:
        th = 40
    usable = _log_usable(
        th, units=bool(units),
        notify_rows=(min(len(dec_rows), 4) if show_notify else 0))
    if len(lines) > usable:
        hidden_n = len(lines) - (usable - 1)
        lines = [Text(f"  ⋯ ↑ 已滚动收起 {hidden_n} 行 · 最新事件在下", style="dim")
                 ] + lines[-(usable - 1):]

    # 居中自适应：按最长内容行计算缩进，往两边均匀填充
    try:
        tw = os.get_terminal_size().columns
    except OSError:
        tw = 80
    max_len = max((len(l.plain) for l in lines), default=0)
    pad = min(4, max(0, (tw - max_len - 4) // 2))
    centered = Text()
    for line in lines:
        centered.append(" " * pad)
        centered.append(line)
        centered.append("\n")

    layout["log"].update(Panel(centered,
                               title=f"[bold {DIM_GRID}]» EVENT STREAM[/]",
                               border_style=DIM_GRID, box=box.ROUNDED,
                               padding=(0, 0)))
    return layout


def _await_run_json(root: Path, wait_s: float = 60.0) -> bool:
    """控制台落位竞态宽限（accept-notifyd-r1 实证）：落位发生在 main()
    进 flow 之前，run.json 由 _prepare 稍后才写，「无 run 即退」会让
    monitor 在起跑线上死掉。等到文件出现或宽限耗尽。"""
    import time as _t

    deadline = _t.monotonic() + wait_s
    while _t.monotonic() < deadline:
        if (root / "run.json").exists():
            return True
        _t.sleep(0.5)
    return (root / "run.json").exists()


def run_monitor(shared: Path, run_id: str, shown: int = 0,
                wait_s: float = 60.0) -> int:
    root = Path(shared) / run_id
    if not _await_run_json(root, wait_s=wait_s):
        print(f"无 run: {root}", flush=True)
        return 1
    tick = 0
    try:
        # 4fps：数据每帧重建（run.json 很小），换事件流/转轮/呼吸灯流畅动画
        with Live(build(root, tick), refresh_per_second=4, screen=True) as live:
            while True:
                time.sleep(0.25)
                tick += 1
                live.update(build(root, tick))
    except KeyboardInterrupt:
        return 0
