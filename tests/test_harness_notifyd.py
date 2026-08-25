"""notifyd 决策唤醒守护回归（herdr 操作平面信使）。

架构：控制平面 rmux / 操作平面 herdr。守护语义，决策请求 pending →
herdr agent wait <主窗格> --until idle（idle 门）→ agent prompt 注入
决策简报（携 decide 命令）；重发按 spacing 节流、失败 30s 短退避；
run 终态通知一次即退。全部 fake（mock HerdrClient / subprocess），零真注入。
"""

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from evo_harness import notifyd
from evo_harness.notifyd import (
    HerdrClient,
    _tail_looks_busy,
    NotifyLedger,
    build_decision_message,
    build_terminal_message,
    run_notifyd,
    scan_pending,
    tick,
)


class FakeClient:
    """HerdrClient 替身：记录调用，可脚本化 wait/prompt 成败。"""

    def __init__(self, wait_ok=True, prompt_ok=True, pane=None, tail=""):
        self.calls = []
        self.wait_ok = wait_ok
        self.prompt_ok = prompt_ok
        self.pane = pane
        self.tail = tail  # 末屏文本（缺省空串 = 空闲屏）

    def pane_current(self):
        return self.pane

    def agent_wait_idle(self, target, timeout_ms):
        self.calls.append(("wait", target, timeout_ms))
        return self.wait_ok

    def agent_prompt(self, target, text):
        self.calls.append(("prompt", target, text))
        return self.prompt_ok

    def pane_read_tail(self, pane_id, lines=8):
        return self.tail


def _mk_run(tmp_path, status="running", pending=None, decided=None):
    """搭 shared/<run>/ 骨架：run.json + decisions/*.json。"""
    run_dir = tmp_path / "r1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "r1", "stage": "plan", "status": status}),
        encoding="utf-8",
    )
    d = run_dir / "decisions"
    d.mkdir(exist_ok=True)
    for node, brief in (pending or {}).items():
        (d / f"{node}.json").write_text(json.dumps({
            "node": node, "status": "pending", "brief": brief,
            "choices": ["approve", "abort"], "default": "approve",
            "requested_at": 100.0,
        }, ensure_ascii=False), encoding="utf-8")
    for node, choice in (decided or {}).items():
        (d / f"{node}.json").write_text(json.dumps({
            "node": node, "status": "decided", "choice": choice,
        }, ensure_ascii=False), encoding="utf-8")
    return run_dir


# ------------------------------------------------------------- 纯逻辑 ----

def test_scan_pending_filters_status_and_bad_json(tmp_path):
    _mk_run(tmp_path, pending={"plan-approval": "x"}, decided={"merge-conflict": "skip-debt"})
    (tmp_path / "r1" / "decisions" / "broken.json").write_text("{oops", encoding="utf-8")
    got = scan_pending(tmp_path / "r1")
    assert [d["node"] for d in got] == ["plan-approval"]
    assert got[0]["_stem"] == "plan-approval"


def test_decision_message_carries_decide_command(tmp_path):
    msg = build_decision_message("r1", tmp_path, {
        "node": "plan-approval", "brief": "三契约已就绪",
        "choices": ["approve", "revise", "abort"],
    })
    assert "node=plan-approval" in msg and "三契约已就绪" in msg
    assert "approve / revise / abort" in msg
    assert "--node plan-approval" in msg and "--run-id r1" in msg
    assert f"--shared {tmp_path}" in msg


def test_terminal_message_per_status(tmp_path):
    done = build_terminal_message("r1", tmp_path, {"status": "done", "stage": "merge"})
    assert "status=done" in done and "产物" in done
    esc = build_terminal_message("r1", tmp_path, {"status": "escalated", "stage": "execute"})
    assert "escalated" in esc and "status --run-id r1" in esc and "clean-wt" in esc
    abt = build_terminal_message("r1", tmp_path, {"status": "aborted", "stage": "merge"})
    assert "中止" in abt


def test_ledger_due_mark_forget_spacing(tmp_path):
    led = NotifyLedger(tmp_path / "n.json")
    assert led.due("n1", requested_at=0.0, now=1000.0, spacing=600.0)
    led.mark("n1", now=1000.0, ok=True)
    assert not led.due("n1", requested_at=0.0, now=1300.0, spacing=600.0)
    assert led.due("n1", requested_at=0.0, now=1600.0, spacing=600.0)
    led.forget("n1")
    assert led.due("n1", requested_at=0.0, now=1601.0, spacing=600.0)


def test_ledger_new_request_overrides_spacing(tmp_path):
    """同 node 重发新请求（requested_at 更新）：立即到期，不被旧账拖住。"""
    led = NotifyLedger(tmp_path / "n.json")
    led.mark("n1", now=1000.0, ok=True)
    assert led.due("n1", requested_at=1200.0, now=1200.0, spacing=600.0)


def test_ledger_failed_notify_short_backoff(tmp_path):
    led = NotifyLedger(tmp_path / "n.json")
    led.mark("n1", now=1000.0, ok=False)  # blocked 拒发等失败
    assert not led.due("n1", requested_at=0.0, now=1010.0, spacing=600.0)
    assert led.due("n1", requested_at=0.0, now=1030.0, spacing=600.0)


def test_ledger_persists(tmp_path):
    p = tmp_path / "notify_state.json"
    NotifyLedger(p).mark("n1", now=5.0, ok=True)
    led2 = NotifyLedger(p)
    assert not led2.due("n1", requested_at=0.0, now=6.0, spacing=600.0)
    led2.mark_terminal()
    assert NotifyLedger(p).terminal_notified


# ------------------------------------------------------------- 守护环 ----

def _tick(tmp_path, client, ledger, now, status="running", **mk):
    client.calls.clear()  # FakeClient.calls 累积：每拍只看本拍新增
    run_dir = _mk_run(tmp_path, status=status, **mk)
    return tick(run_dir, tmp_path, "r1", ledger, client, "w4:p1",
                spacing=600.0, idle_wait_ms=600_000, now=now), client.calls


def test_tick_pending_notifies_with_idle_gate(tmp_path):
    led, c = NotifyLedger(tmp_path / "r1" / "notify_state.json"), FakeClient()
    verdict, calls = _tick(tmp_path, c, led, now=1000.0,
                           pending={"plan-approval": "三契约已就绪"})
    assert verdict == "continue"
    assert calls[0] == ("wait", "w4:p1", 600_000)  # idle 门在前
    assert calls[1][0] == "prompt" and "plan-approval" in calls[1][2]
    assert "decide" in calls[1][2]
    # 立即再拍：spacing 内不重发
    _, calls2 = _tick(tmp_path, c, led, now=1001.0,
                      pending={"plan-approval": "三契约已就绪"})
    assert calls2 == []


def test_tick_renotify_after_spacing(tmp_path):
    led, c = NotifyLedger(tmp_path / "r1" / "notify_state.json"), FakeClient()
    _tick(tmp_path, c, led, now=1000.0, pending={"plan-approval": "x"})
    _, calls = _tick(tmp_path, c, led, now=1700.0, pending={"plan-approval": "x"})
    assert len(calls) == 2 and calls[0][0] == "wait"


def test_tick_decided_node_forgotten_then_new_request_fires(tmp_path):
    led, c = NotifyLedger(tmp_path / "r1" / "notify_state.json"), FakeClient()
    _tick(tmp_path, c, led, now=1000.0, pending={"escalate-review": "x"})
    _, calls = _tick(tmp_path, c, led, now=1001.0, decided={"escalate-review": "finalize"})
    assert calls == []  # 已决：不注入
    # 同 node 新请求（新一轮 review）：requested_at 新于旧账 → 立即注入
    _, calls = _tick(tmp_path, c, led, now=1002.0,
                     pending={"escalate-review": "第二轮"})
    assert len(calls) == 2


def test_tick_wait_timeout_but_content_idle_falls_back(tmp_path):
    """内容复核权威兜底（accept-notifyd-r6 实证）：状态通道失明、wait 每
    600s 超时，但末屏无忙碌特征，照样投，决策不再干等。"""
    led = NotifyLedger(tmp_path / "r1" / "notify_state.json")
    c = FakeClient(wait_ok=False, tail="❯\n  ⏵⏵ bypass permissions on")
    _, calls = _tick(tmp_path, c, led, now=1000.0, pending={"plan-approval": "x"})
    assert [x[0] for x in calls] == ["wait", "prompt"]  # 超时但内容空闲：投
    assert led.stems() == ["plan-approval"]  # 落 ok 账


def test_tick_wait_timeout_and_content_busy_skips(tmp_path):
    """wait 超时 + 末屏忙碌（spinner/对话框）：skip 不投不记账。"""
    led = NotifyLedger(tmp_path / "r1" / "notify_state.json")
    c = FakeClient(wait_ok=False, tail="✻ Working…\n❯")
    _, calls = _tick(tmp_path, c, led, now=1000.0, pending={"plan-approval": "x"})
    assert [x[0] for x in calls] == ["wait"]
    assert led.stems() == []


def test_tick_no_content_trusts_wait_gate(tmp_path):
    """读不到屏（复核失效）：退回 herdr idle 门，wait 超时即 skip。"""
    led = NotifyLedger(tmp_path / "r1" / "notify_state.json")
    c = FakeClient(wait_ok=False, tail=None)
    _, calls = _tick(tmp_path, c, led, now=1000.0, pending={"plan-approval": "x"})
    assert [x[0] for x in calls] == ["wait"]
    assert led.stems() == []


def test_tick_prompt_fail_marked_backoff(tmp_path):
    led, c = NotifyLedger(tmp_path / "r1" / "notify_state.json"), FakeClient(prompt_ok=False)
    _tick(tmp_path, c, led, now=1000.0, pending={"plan-approval": "x"})
    _, calls = _tick(tmp_path, c, led, now=1010.0, pending={"plan-approval": "x"})
    assert calls == []  # 30s 退避内
    _, calls = _tick(tmp_path, c, led, now=1035.0, pending={"plan-approval": "x"})
    assert len(calls) == 2


def test_tick_busy_tail_skips_without_ledger(tmp_path):
    """双锁第二道：herdr 说 idle 但末屏有 spinner/计时，不投、不记账
    （瞬时态，下一拍重查，不吃 30s 退避）。[实证: 2026-08-25 实拍特征]"""
    led = NotifyLedger(tmp_path / "r1" / "notify_state.json")
    busy = "✻ 写 notifyd 测试… (10m 2s · ↓ 43.5k tokens)\n❯\n  ⏵⏵ bypass permissions on"
    c = FakeClient(tail=busy)
    _, calls = _tick(tmp_path, c, led, now=1000.0, pending={"plan-approval": "x"})
    assert [x[0] for x in calls] == ["wait"]  # 复核不过：零注入
    assert led.stems() == []  # 不记账
    # 屏清了（agent 真闲下来）：下一拍立即投
    c.tail = "❯\n  ⏵⏵ bypass permissions on (shift+tab to cycle)"
    _, calls = _tick(tmp_path, c, led, now=1000.5, pending={"plan-approval": "x"})
    assert [x[0] for x in calls] == ["wait", "prompt"]


def test_tail_looks_busy_markers():
    idle = "❯\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
    assert _tail_looks_busy(idle) == (False, "")
    assert _tail_looks_busy("✻ Thinking…\n❯")[0]
    assert _tail_looks_busy("  ⎿  Running…")[0]      # 工具在跑
    assert _tail_looks_busy("  esc to interrupt")[0]
    assert _tail_looks_busy("Do you trust the contents of this folder?")[0]
    # 对话文本里引用这些字符不至于顶格：不误判
    assert not _tail_looks_busy("tuple 里有 ✻ 这个字符但不在行首")[0]


def test_tick_terminal_notifies_once_and_exits(tmp_path):
    led, c = NotifyLedger(tmp_path / "r1" / "notify_state.json"), FakeClient()
    verdict, calls = _tick(tmp_path, c, led, now=1000.0, status="done")
    assert verdict == "exit"
    assert calls[1][0] == "prompt" and "status=done" in calls[1][2]
    verdict2, calls2 = _tick(tmp_path, c, led, now=1001.0, status="done")
    assert verdict2 == "exit" and calls2 == []


def test_tick_terminal_escalated_carries_triage(tmp_path):
    led, c = NotifyLedger(tmp_path / "r1" / "notify_state.json"), FakeClient()
    _, calls = _tick(tmp_path, c, led, now=1000.0, status="escalated")
    assert "clean-wt" in calls[1][2]


# ------------------------------------------------------------ 守护入口 ----

def test_run_notifyd_terminal_returns(tmp_path):
    _mk_run(tmp_path, status="done")
    c = FakeClient()
    rc = run_notifyd(tmp_path, "r1", main_pane="w4:p1", poll=0.01,
                     client=c)
    assert rc == 0
    assert any(x[0] == "prompt" for x in c.calls)


def test_run_notifyd_missing_run(tmp_path, capsys):
    assert run_notifyd(tmp_path, "nope", grace_s=0.0) == 1
    assert "无 run" in capsys.readouterr().err


def test_run_notifyd_grace_waits_for_run_json(tmp_path):
    """落位竞态（accept-notifyd-r1 实证）：落位先于 flow，run.json 由
    _prepare 稍后才写，守护须宽限等待而非即退。"""
    run_dir = tmp_path / "r1"
    run_dir.mkdir(parents=True)

    def _late_write():
        time.sleep(1.0)
        (run_dir / "run.json").write_text(
            json.dumps({"run_id": "r1", "stage": "merge", "status": "done"}),
            encoding="utf-8")

    threading.Thread(target=_late_write, daemon=True).start()
    c = FakeClient()
    rc = run_notifyd(tmp_path, "r1", main_pane="w4:p1", poll=0.05,
                     grace_s=30.0, client=c)
    assert rc == 0  # 宽限期内等到 run.json：正常走完终态通知
    assert any(x[0] == "prompt" for x in c.calls)


def test_monitor_awaits_run_json(tmp_path):
    """monitor 同款竞态：无 run 即退会让它在起跑线上死掉（右窗格空白）。"""
    from evo_harness.monitor import _await_run_json
    # 宽限耗尽仍无 run.json：False（run_monitor 由此走 rc=1）
    assert _await_run_json(tmp_path / "ghost", wait_s=0.0) is False
    # 已存在：即刻 True
    d = tmp_path / "live"
    d.mkdir()
    (d / "run.json").write_text("{}", encoding="utf-8")
    assert _await_run_json(d, wait_s=0.0) is True
    # 稍后出现：等到 True
    late = tmp_path / "late"

    def _touch():
        time.sleep(0.8)
        late.mkdir(parents=True)
        (late / "run.json").write_text("{}", encoding="utf-8")

    threading.Thread(target=_touch, daemon=True).start()
    assert _await_run_json(late, wait_s=10.0) is True


def test_run_notifyd_resolves_main_pane_from_current(tmp_path):
    _mk_run(tmp_path, status="done")
    c = FakeClient(pane={"pane_id": "w9:p2"})
    rc = run_notifyd(tmp_path, "r1", poll=0.01, client=c)
    assert rc == 0
    assert c.calls[0][1] == "w9:p2"


def test_run_notifyd_no_herdr_no_pane(tmp_path, capsys):
    _mk_run(tmp_path, status="running")
    assert run_notifyd(tmp_path, "r1", client=FakeClient(pane=None)) == 1
    assert "主窗格无法定位" in capsys.readouterr().err


# --------------------------------------------------------- HerdrClient ----

def test_herdr_client_no_binary_is_inert():
    c = HerdrClient.__new__(HerdrClient)
    c.binary = None
    assert c.pane_current() is None
    assert c.agent_wait_idle("w4:p1", 1000) is False
    assert c.agent_prompt("w4:p1", "hi") is False


def test_herdr_client_argv_and_parsing(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(tuple(argv))
        if tuple(argv[1:3]) == ("pane", "current"):
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(
                    {"result": {"pane": {"pane_id": "w4:p1"}}}), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(notifyd.shutil, "which", lambda n: "/fake/herdr")
    monkeypatch.setattr(notifyd.subprocess, "run", fake_run)
    c = HerdrClient()
    assert c.pane_current()["pane_id"] == "w4:p1"
    assert c.agent_wait_idle("w4:p1", 600_000)
    assert c.agent_prompt("w4:p1", "⚖ 文本")
    assert calls[1][1:6] == ("agent", "wait", "w4:p1", "--until", "idle")
    assert calls[1][-2:] == ("--timeout", "600000")
    assert calls[2][1:4] == ("agent", "prompt", "w4:p1")
    assert calls[2][4] == "⚖ 文本"


def test_herdr_client_timeout_and_bad_json(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(notifyd.shutil, "which", lambda n: "/fake/herdr")
    monkeypatch.setattr(notifyd.subprocess, "run", boom)
    c = HerdrClient()
    assert c.pane_current() is None
    assert c.agent_wait_idle("w4:p1", 1000) is False

    def bad_json(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="{nope", stderr="")

    monkeypatch.setattr(notifyd.subprocess, "run", bad_json)
    assert c.pane_current() is None


# ------------------------------------------------------ 无头拉起（cli） ----

CURRENT_JSON = json.dumps({
    "id": "cli:pane:current",
    "result": {"pane": {"pane_id": "w4:p1", "agent": "claude"}},
})


def _mk_spawn(monkeypatch, calls, cur_rc=0, popen_exc=None):
    from evo_harness import cli

    def fake_run(argv, **kw):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(
            argv, cur_rc, stdout=CURRENT_JSON, stderr="boom")

    class _P:
        pid = 777

    def fake_popen(argv, **kw):
        calls.append(("POPEN",) + tuple(argv))
        calls.append(("POPEN-KW", dict(kw)))
        if popen_exc:
            raise popen_exc
        return _P()

    monkeypatch.setattr("shutil.which", lambda n: "/fake/herdr")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    return cli


def test_spawn_notifyd_headless(monkeypatch, capsys, tmp_path):
    """单窗格控制台：notifyd 无头拉起（不占窗格），主窗格钉死，日志落 run 目录。"""
    calls = []
    cli = _mk_spawn(monkeypatch, calls)
    assert cli._spawn_notifyd("r1", tmp_path) is True
    cur = calls[0]
    argv = next(c for c in calls if c[0] == "POPEN")
    assert cur[1:3] == ("pane", "current")
    assert argv[2:5] == ("-m", "evo_harness.cli", "notifyd")
    assert argv[argv.index("--main-pane") + 1] == "w4:p1"
    assert argv[argv.index("--run-id") + 1] == "r1"
    kw = next(c for c in calls if c[0] == "POPEN-KW")[1]
    assert kw.get("start_new_session") is True  # POSIX 脱离控制终端
    assert (tmp_path / "r1" / "notifyd.log").exists()  # stdout 落日志
    inv = json.loads((tmp_path / "r1" / "console.json").read_text(encoding="utf-8"))
    assert inv["notifyd"]["plane"] == "headless" and inv["notifyd"]["pid"] == 777
    assert inv["notifyd"]["target"] == {"plane": "herdr", "pane": "w4:p1"}
    assert "无头拉起" in capsys.readouterr().err


def test_spawn_notifyd_creates_run_dir_for_log(monkeypatch, tmp_path):
    """落位先于 flow：run 目录尚未存在也要能落日志（宽限等待配套）。"""
    calls = []
    cli = _mk_spawn(monkeypatch, calls)
    assert cli._spawn_notifyd("fresh", tmp_path) is True
    assert (tmp_path / "fresh").is_dir()


def test_spawn_notifyd_fail_open(monkeypatch, capsys, tmp_path):
    calls = []
    cli = _mk_spawn(monkeypatch, calls, cur_rc=1)
    assert cli._spawn_notifyd("r1", tmp_path) is False
    assert "pane current 失败" in capsys.readouterr().err

    calls.clear()
    cli = _mk_spawn(monkeypatch, calls, popen_exc=OSError("boom"))
    assert cli._spawn_notifyd("r1", tmp_path) is False
    assert "拉起异常" in capsys.readouterr().err


def test_spawn_notifyd_guard_and_no_herdr(monkeypatch, tmp_path):
    calls = []
    cli = _mk_spawn(monkeypatch, calls)
    monkeypatch.setenv("EVO_HERDR_MONITOR_PLACED", "1")  # 控制台已落位
    assert cli._spawn_notifyd("r1", tmp_path) is False
    assert calls == []
    monkeypatch.delenv("EVO_HERDR_MONITOR_PLACED")
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert cli._spawn_notifyd("r1", tmp_path) is False
    assert calls == []


# ------------------------------------------------- 事件流融合（monitor） ----

def test_log_notify_appends_jsonl(tmp_path):
    from evo_harness.notifyd import log_notify
    log_notify(tmp_path, "⚖ plan-approval 决策简报注入 ok → w4:p1")
    log_notify(tmp_path, "run 终态 done，收尾通知已注入 w4:p1")
    lines = (tmp_path / "notify_events.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    d = json.loads(lines[0])
    assert d["kind"] == "notify" and "plan-approval" in d["detail"]
    assert d["t"] > 0


def test_tick_writes_notify_event(tmp_path):
    """投递动作落 jsonl：monitor 事件流的 ✉ 行数据源。"""
    led = NotifyLedger(tmp_path / "r1" / "notify_state.json")
    c = FakeClient()
    _tick(tmp_path, c, led, now=1000.0, pending={"plan-approval": "x"})
    lines = (tmp_path / "r1" / "notify_events.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert any("plan-approval" in ln and "注入" in ln for ln in lines)


def test_merged_history_weaves_notify_events(tmp_path):
    """monitor 事件流融合：notify 事件按 t 与 run.json 历史交织成一条轴。"""
    from evo_harness.monitor import _merged_history
    hist = [{"t": 100.0, "kind": "dispatch", "detail": "a"},
            {"t": 300.0, "kind": "decision", "detail": "b"}]
    # 无 jsonl：原样
    assert _merged_history(tmp_path, hist) is hist
    (tmp_path / "notify_events.jsonl").write_text(
        json.dumps({"t": 200.0, "kind": "notify", "detail": "✉ n1"})
        + "\n{broken\n"
        + json.dumps({"t": 50.0, "kind": "notify", "detail": "✉ n0"}) + "\n",
        encoding="utf-8")
    merged = _merged_history(tmp_path, hist)
    assert [e["t"] for e in merged] == [50.0, 100.0, 200.0, 300.0]
    assert merged[0]["detail"] == "✉ n0"


def test_monitor_renders_notify_kind_icon():
    from evo_harness.monitor import KIND_STYLE, _fmt_event
    icon, detail = _fmt_event("notify", "⚖ n1 注入 ok → w4:p1")
    assert icon == "✉" and "n1" in detail
    assert "notify" in KIND_STYLE


# ------------------------------------------------- 可视化（状态面板） ----

def test_ledger_beat_persists(tmp_path):
    led = NotifyLedger(tmp_path / "n.json")
    led.beat("w4:p1", pid=123)
    again = NotifyLedger(tmp_path / "n.json")
    assert again.daemon["main_pane"] == "w4:p1"
    assert again.daemon["pid"] == 123
    assert again.daemon["beat"] > 0


def test_daemon_state_fresh_stale_off(tmp_path):
    from evo_harness.monitor import _daemon_state
    assert _daemon_state(tmp_path)[0] == "off"  # 无状态文件
    (tmp_path / "notify_state.json").write_text(json.dumps(
        {"daemon": {"main_pane": "w4:p1", "beat": time.time() - 2, "pid": 1}}),
        encoding="utf-8")
    assert _daemon_state(tmp_path)[0] == "fresh"
    (tmp_path / "notify_state.json").write_text(json.dumps(
        {"daemon": {"main_pane": "w4:p1", "beat": time.time() - 999, "pid": 1}}),
        encoding="utf-8")
    assert _daemon_state(tmp_path)[0] == "stale"


def test_decision_rows_crosses_ledger(tmp_path):
    from evo_harness.monitor import _decision_rows
    _mk_run(tmp_path, pending={"plan-approval": "x"}, decided={"merge": "skip-debt"})
    (tmp_path / "r1" / "notify_state.json").write_text(json.dumps(
        {"nodes": {"plan-approval": {"last": 123.0, "ok": True}}}),
        encoding="utf-8")
    rows = {d["_stem"]: d for d in _decision_rows(tmp_path / "r1")}
    assert rows["plan-approval"]["_injected"]["ok"] is True
    assert rows["merge"]["status"] == "decided"


def test_notify_panel_renders_daemon_and_decisions(tmp_path):
    """面板全量可视化：值守行（spin/目标/心跳）+ 决策行（choices/注入态/已决）。"""
    import io
    from rich.console import Console
    from evo_harness.monitor import _notify_panel
    run_dir = _mk_run(tmp_path, pending={"escalate-review": "x"},
                      decided={"plan-approval": "approve"})
    (run_dir / "notify_state.json").write_text(json.dumps({
        "daemon": {"main_pane": "w4:p1", "beat": time.time() - 1, "pid": 42},
        "nodes": {"escalate-review": {"last": time.time() - 3, "ok": True}},
    }), encoding="utf-8")
    buf = io.StringIO()
    Console(file=buf, width=100).print(_notify_panel(run_dir, "⠋"))
    out = buf.getvalue()
    assert "notifyd 值守" in out and "w4:p1" in out and "心跳" in out
    assert "无头" in out and "herdr·w4:p1" in out  # 平面标注（rmux/herdr 分明）
    assert "escalate-review" in out and "approve · abort" in out and "已注入" in out
    assert "plan-approval" in out and "approve" in out


def test_run_notifyd_startup_event_and_beat(tmp_path):
    _mk_run(tmp_path, status="done")
    c = FakeClient()
    assert run_notifyd(tmp_path, "r1", main_pane="w4:p1", poll=0.01,
                       client=c) == 0
    first = (tmp_path / "r1" / "notify_events.jsonl").read_text(
        encoding="utf-8").splitlines()[0]
    assert "值守开始" in first and "w4:p1" in first
    dm = json.loads((tmp_path / "r1" / "notify_state.json").read_text(
        encoding="utf-8"))["daemon"]
    assert dm["main_pane"] == "w4:p1"


# -------------------------------------------- host 清债回归（rc-r2/r3） ----

def test_heartbeat_fresh_during_blocking_wait(tmp_path):
    """心跳独立线程：_deliver 长阻塞（idle 等待）期间 beat 仍刷新，
    monitor 不再误报失联（rc-r2/r3 三方 must-fix）。"""
    _mk_run(tmp_path, pending={"plan-approval": "x"})  # running + 待决策

    class SlowWait(FakeClient):
        def agent_wait_idle(self, target, timeout_ms):
            self.calls.append(("wait", target, timeout_ms))
            time.sleep(1.2)  # 模拟长阻塞
            return False

    c = SlowWait(pane={"pane_id": "w4:p1"})
    import threading
    beats = []

    def _sample():
        for _ in range(6):  # 采样 1.2s 阻塞窗内的心跳
            time.sleep(0.2)
            try:
                b = json.loads(
                    (tmp_path / "r1" / "notify_state.json").read_text(
                        encoding="utf-8"))["daemon"]["beat"]
                beats.append(b)
            except (OSError, KeyError, ValueError):
                pass

    th = threading.Thread(target=_sample, daemon=True)
    th.start()
    # 只跑一拍：长 wait 阻塞主循环，但心跳线程仍在刷
    from evo_harness.notifyd import NotifyLedger, tick
    led = NotifyLedger(tmp_path / "r1" / "notify_state.json")
    import evo_harness.notifyd as nd
    # 手动起心跳线程（run_notifyd 会在终态才退，这里单拍验证）
    stop = threading.Event()

    def _beat():
        while not stop.wait(0.3):
            led.beat("w4:p1")

    ht = threading.Thread(target=_beat, daemon=True)
    ht.start()
    tick(tmp_path / "r1", tmp_path, "r1", led, c, "w4:p1",
         spacing=600.0, idle_wait_ms=600_000, now=time.time())
    stop.set()
    th.join(1.5)
    assert beats, "阻塞窗内应采到心跳"
    assert beats[-1] > beats[0], "长阻塞期间 beat 仍在前进（未饿死）"


def test_log_usable_accounts_for_optional_panels():
    """事件流可用高度扣 units/notify 面板（rc-r2/r3 三方 must-fix：小终端
    最新事件被裁出视野）。"""
    from evo_harness.monitor import _log_usable
    base = _log_usable(40, units=False, notify_rows=0)
    assert _log_usable(40, units=True, notify_rows=3) == base - 3 - 6
    assert _log_usable(40, units=True, notify_rows=4) == base - 3 - 7  # 行数由调用方封顶 4
    assert _log_usable(20, units=True, notify_rows=9) >= 4  # 极小终端保底 4 行


# ------------------------------------------------------ rmux 调度 flow ----

def _mk_session(monkeypatch, calls, rc=0, err=""):
    from evo_harness import cli

    def fake_run(argv, **kw):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=err)

    monkeypatch.setattr("shutil.which", lambda n: "/fake/rmux" if n == "rmux" else None)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return cli


def _flow_args(tmp_path, command="review-cycle"):
    import types
    return types.SimpleNamespace(
        command=command, goal="验收", shared=tmp_path,
        fake=False, plan_only=False, rounds=2, global_hooks=False)


def test_spawn_flow_in_rmux_session(monkeypatch, capsys, tmp_path):
    """flow 新开 rmux 会话执行：new-session -d -s evo-<run_id>，命令内联
    PLACED 守卫 + tee；不占 herdr tab、不挂宿主终端。"""
    calls = []
    cli = _mk_session(monkeypatch, calls)
    assert cli._spawn_flow_in_rmux_session(_flow_args(tmp_path), "r1") is True
    (argv,) = calls
    assert argv[:4] == ("/fake/rmux", "new-session", "-d", "-s")
    assert argv[4] == "evo-r1"
    cmd = argv[5]
    assert cmd.startswith("EVO_HERDR_MONITOR_PLACED=1 ")
    assert "review-cycle" in cmd and "验收" in cmd and "--run-id" in cmd
    assert "tee -a" in cmd and "harness.log" in cmd
    assert " --detach" not in cmd  # 会话即 detach
    inv = json.loads((tmp_path / "r1" / "console.json").read_text(encoding="utf-8"))
    assert inv["flow"]["plane"] == "rmux"
    assert inv["flow"]["session"] == "evo-r1" and inv["flow"]["workers"] == 3
    assert "已入会话" in capsys.readouterr().err


def test_spawn_flow_session_fail_open(monkeypatch, capsys, tmp_path):
    calls = []
    cli = _mk_session(monkeypatch, calls, rc=1, err="boom")
    assert cli._spawn_flow_in_rmux_session(_flow_args(tmp_path), "r1") is False
    assert "new-session 失败" in capsys.readouterr().err


def test_spawn_flow_session_guard(monkeypatch, tmp_path):
    """pytest 护栏同样盖 rmux 会话调度（宿主真实副作用零容忍）。"""
    calls = []
    cli = _mk_session(monkeypatch, calls)
    monkeypatch.delenv("EVO_HERDR_TEST_FAKE", raising=False)
    import os
    monkeypatch.setattr(cli.os, "environ",
                        {**os.environ, "PYTEST_CURRENT_TEST": "x"})
    assert cli._spawn_flow_in_rmux_session(_flow_args(tmp_path), "r1") is False
    assert calls == []


def test_write_pidfile(tmp_path):
    from evo_harness import cli
    cli._write_pidfile(tmp_path, "r9")
    assert (tmp_path / "r9" / "run.pid").read_text().strip() == str(cli.os.getpid())


def test_main_herdr_mode_handover_exits_clean(console, monkeypatch, capsys):
    """herdr 会话：CLI 纯 rmux 操作，flow 调度成功即单行交接退出，
    _run_flow 绝不在本地跑。"""
    from evo_harness import cli
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr("evo_harness.cli._spawn_flow_in_rmux_session",
                        lambda args, rid: True)
    assert cli.main(["run", "目标", "--shared", "s"]) == 0
    out = capsys.readouterr().out
    assert "已交 rmux 接管" in out and "※/✔ 将注入本窗格" in out
    assert len(out.strip().splitlines()) == 1  # 单行交接


# -------------------------------------------- pytest 真 herdr 护栏 ----

def test_pytest_guard_blocks_real_herdr(monkeypatch, tmp_path):
    """事故回归（2026-08-25）：PYTEST_CURRENT_TEST 环境下无 EVO_HERDR_TEST_FAKE
    即拒绝真实 herdr 操作，fixture 漏 patch 也不许打到宿主（1600+ 杂散
    tab + 真 agent run 自繁殖事故的编程护栏）。"""
    from evo_harness import cli
    import types
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")  # 本身在 pytest 下已置，显式钉
    monkeypatch.delenv("EVO_HERDR_TEST_FAKE", raising=False)
    monkeypatch.setattr("shutil.which", lambda n: "/fake/herdr")  # 有真二进制也不放行
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: calls.append(a) or None)
    assert cli._place_herdr_monitor("r", tmp_path) is None
    assert cli._spawn_notifyd("r", tmp_path) is False
    args = types.SimpleNamespace(
        command="run", goal="g", shared=tmp_path,
        fake=False, plan_only=False, rounds=None, global_hooks=False)
    assert cli._spawn_flow_in_rmux_session(args, "r") is False
    assert calls == []  # 零 subprocess：连 pane current 都不许发


# ---------------------------------------------------- UNITS 执行单元 ----

def _mk_console(run_dir):
    (run_dir / "console.json").write_text(json.dumps({
        "flow": {"plane": "rmux", "pane": "w4:pR9", "label": "r1",
                 "workers": 3},
        "monitor": {"plane": "herdr", "pane": "w4:pR8"},
        "notifyd": {"plane": "headless", "pid": 42,
                    "target": {"plane": "herdr", "pane": "w4:p1"}},
    }), encoding="utf-8")


def test_units_panel_lists_planes(tmp_path):
    """执行单元清单：哪个单元、哪个平面、哪个 pane（console.json 数据源）。"""
    import io
    from rich.console import Console
    from evo_harness.monitor import _units_panel
    run_dir = _mk_run(tmp_path)
    assert _units_panel(tmp_path / "ghost") is None  # 无清单：不占行
    _mk_console(run_dir)
    buf = io.StringIO()
    Console(file=buf, width=88).print(_units_panel(run_dir))
    out = buf.getvalue()
    assert "▶ flow" in out and "rmux·w4:pR9" in out
    assert "≡ workers×3" in out
    assert "▤ monitor" in out and "herdr·w4:pR8" in out
    assert len([l for l in out.splitlines() if l.strip()]) == 3  # 单行不折行


# ------------------------------------------------------------ main 集成 ----

@pytest.fixture(autouse=True)
def _env_clean(monkeypatch):
    monkeypatch.delenv("EVO_HERDR_MONITOR_PLACED", raising=False)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.setenv("EVO_HERDR_TEST_FAKE", "1")  # mock 路径放行（护栏开关）


@pytest.fixture
def console(monkeypatch):
    """记录 monitor 落位/notifyd 拉起调用；_run_flow 空转。"""
    seen = {"mon": [], "nd": []}

    async def _noop_flow(args):
        return 0

    monkeypatch.setattr(
        "evo_harness.cli._place_herdr_monitor",
        lambda rid, shared: seen["mon"].append((rid, shared)) or "w4:pM")
    monkeypatch.setattr(
        "evo_harness.cli._spawn_notifyd",
        lambda rid, shared: seen["nd"].append((rid, shared)) or True)
    monkeypatch.setattr("evo_harness.cli._spawn_flow_in_rmux_session",
                        lambda args, rid: False)  # 缺省回落本地路径
    monkeypatch.setattr("evo_harness.cli._run_flow", _noop_flow)
    return seen


def test_main_places_console_monitor_and_spawns_notifyd(console, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    from evo_harness import cli
    assert cli.main(["run", "目标", "--shared", "s"]) == 0
    assert console["mon"] and console["nd"]


def test_main_placed_child_runs_flow_locally(console, monkeypatch):
    """r5 fork 事故回归：调度进 tab 的 flow 子进程（EVO_HERDR_MONITOR_PLACED=1）
    不得再走调度分支，否则子生孙无限 fork（30s 200+ tab）。"""
    from evo_harness import cli
    flows, ran = [], []

    async def _spy_flow(args):
        ran.append(args.command)
        return 0

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("EVO_HERDR_MONITOR_PLACED", "1")  # 祖先已调度
    monkeypatch.setattr("evo_harness.cli._spawn_flow_in_rmux_session",
                        lambda args, rid: flows.append(rid) or True)
    monkeypatch.setattr("evo_harness.cli._run_flow", _spy_flow)
    assert cli.main(["run", "目标", "--shared", "s"]) == 0
    assert flows == []          # 不再调度
    assert console["mon"] == [] and console["nd"] == []  # 也不再落位
    assert ran == ["run"]       # flow 本地真跑（tab 内执行）


def test_main_pane_none_disables_whole_console(console, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    from evo_harness import cli
    assert cli.main(["run", "目标", "--shared", "s", "--pane", "none"]) == 0
    assert console["mon"] == [] and console["nd"] == []


def test_main_notifyd_dispatch(monkeypatch, tmp_path):
    from evo_harness import cli
    _mk_run(tmp_path)  # latest run 可解析
    got = {}

    def fake_notifyd(shared, run_id, main_pane="", poll=2.0, spacing=600.0,
                     idle_wait_ms=600000, grace_s=60.0):
        got.update(shared=shared, run_id=run_id, main_pane=main_pane,
                   spacing=spacing)
        return 0

    # main() 内局部 from .notifyd import run_notifyd：patch 源头模块生效
    monkeypatch.setattr("evo_harness.notifyd.run_notifyd", fake_notifyd)
    assert cli.main(["notifyd", "--shared", str(tmp_path),
                     "--main-pane", "w4:p9"]) == 0
    assert got["run_id"] == "r1" and got["main_pane"] == "w4:p9"
    assert got["shared"] == tmp_path
