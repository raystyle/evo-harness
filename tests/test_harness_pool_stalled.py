"""提交死会话（stalled submit）自愈回归，p8-herdr-r1 实证。

k3 config.invalid 卡 15 分钟：hook 态停在陈旧 working（最后事件
userpromptsubmit，零 PreToolUse），nudge 被 `state not in (idle, None)` 挡死，
烧满整档 task_wait 后 GATE_FAILED 硬停止。
"""

import asyncio
import json
import time
from pathlib import Path

from evo_harness.pool import Worker, WorkerPool


class _FakeDriver:
    def __init__(self):
        self.sent: list[str] = []

    async def drive(self, pane, text, tui=True, state_file=None):
        self.sent.append(text)
        return True


class _FakeSM:
    budget = None  # 走默认值：task_wait 120s 兜底 / stalled 120s


def _mk_pool(tmp_path, state_event, state_age_s, state_val="working"):
    bus_dir = tmp_path / "shared" / "r1"
    bus_dir.mkdir(parents=True)
    (bus_dir / "run.json").write_text("{}", encoding="utf-8")
    sf = bus_dir / "state.json"
    sf.write_text(json.dumps({
        "state": state_val, "event": state_event,
        "ts": time.time() - state_age_s,
    }), encoding="utf-8")
    drv = _FakeDriver()
    pool = WorkerPool(drv, type("B", (), {"root": bus_dir, "unit_dir": staticmethod(
        lambda n: bus_dir / "units" / n), "log_event": staticmethod(lambda *a, **k: None),
        "prompt_file": staticmethod(lambda t: bus_dir / f"{t}.md"),
    })(), ("kimi",), statemachine=_FakeSM())
    w = Worker(name="worker-kimi", agent="kimi")
    w.state_file = sf
    return pool, w, drv


def test_submit_stale_requires_old_userpromptsubmit(tmp_path):
    pool, w, _ = _mk_pool(tmp_path, "userpromptsubmit", 300)
    assert pool._submit_stale(w) is True
    # 新鲜提交（思考中属正常）
    pool2, w2, _ = _mk_pool(tmp_path / "b", "userpromptsubmit", 10)
    assert pool2._submit_stale(w2) is False
    # 已有工具调用 = 会话活着，永不判 stale
    pool3, w3, _ = _mk_pool(tmp_path / "c", "pretooluse", 3600)
    assert pool3._submit_stale(w3) is False


def test_wait_artifact_resubmits_on_stale(tmp_path):
    """stalled 轮应重提任务行（reader），而不是傻等烧满 task_wait。"""
    pool, w, drv = _mk_pool(tmp_path, "userpromptsubmit", 300)
    artifact = tmp_path / "out" / "allocations.json"
    artifact.parent.mkdir()

    async def go():
        # wait_s 走默认 120s：把 stalled 阈值压到 1s 让轮次快速推进；
        # 第一轮 stalled 重提时落产物，验证「重提后产物出现即返回 True」
        pool._stalled_s = lambda: 1.0
        import evo_harness.events as ev
        orig = ev.AsyncFileWatcher.wait_until

        async def fast_wait(self, pred, timeout, on_wake=None):
            # 跳过真实等待：立即返回（产物不存在），驱动轮次逻辑
            return None

        ev.AsyncFileWatcher.wait_until = fast_wait
        try:
            def _write_and_true():
                artifact.write_text("{}", encoding="utf-8")
                return True
            # 第一次调用（stalled 检查前）返回 False 触发轮次；重提后产物就位
            calls = {"n": 0}

            async def go2():
                return await pool._wait_artifact(
                    w, "planner", artifact, 2, reader="[planner] FILE x"
                )
            # 让 wait_until 在第二轮看到产物
            async def fast_wait2(self, pred, timeout, on_wake=None):
                calls["n"] += 1
                if calls["n"] >= 2:
                    artifact.write_text("{}", encoding="utf-8")
                return pred()
            ev.AsyncFileWatcher.wait_until = fast_wait2
            return await go2()
        finally:
            ev.AsyncFileWatcher.wait_until = orig

    assert asyncio.run(go()) is True
    assert drv.sent == ["[planner] FILE x"]  # 重提的是任务行，不是催写话术


def test_multi_artifact_requires_all(tmp_path):
    """planner 三契约：tuple 产物必须全存在才算完成（r1/r4 GATE_FAILED 实证
    ，单盯第一份会在 agent 写后续契约时误判 done 提前释放）。"""
    from evo_harness.pool import WorkerPool as P
    a1 = tmp_path / "goal_spec.json"
    a2 = tmp_path / "plan.json"
    a3 = tmp_path / "allocations.json"
    tup = (a1, a2, a3)
    assert P._artifacts_ok(tup) is False
    a1.write_text("{}", encoding="utf-8")
    assert P._artifacts_ok(tup) is False  # 第一份落盘不算完
    a2.write_text("{}", encoding="utf-8")
    assert P._artifacts_ok(tup) is False
    a3.write_text("{}", encoding="utf-8")
    assert P._artifacts_ok(tup) is True
    # 单产物路径行为不变
    assert P._artifacts_ok(a1) is True
    assert P._artifacts_ok(tmp_path / "nope.json") is False
    # nudge 话术点名缺失文件
    assert "plan.json" not in P._fmt_missing(tup)  # 已写的不点名
    a2.unlink()
    assert "plan.json" in P._fmt_missing(tup)


def test_wait_artifact_nudges_immediately_when_idle(tmp_path):
    """idle + 缺产物 → 切片边界即催写（不等 900s 轮边界，r5 幻写实证）。"""
    pool, w, drv = _mk_pool(tmp_path, "stop", 30, state_val="idle")
    artifact = tmp_path / "out2" / "summary.md"
    artifact.parent.mkdir()
    import evo_harness.events as ev
    calls = {"n": 0}
    orig = ev.AsyncFileWatcher.wait_until

    async def fast_wait(self, pred, timeout, on_wake=None):
        calls["n"] += 1
        if calls["n"] >= 2:  # 催写后的下一个切片看到产物
            artifact.write_text("# s", encoding="utf-8")
        return pred()

    ev.AsyncFileWatcher.wait_until = fast_wait
    try:
        ok = asyncio.run(pool._wait_artifact(
            w, "research-1", artifact, 2, reader="[research-1] FILE x"
        ))
    finally:
        ev.AsyncFileWatcher.wait_until = orig
    assert ok is True
    # 第一轮就催了（旧逻辑要等 900s 轮边界），且话术点名落盘
    assert len(drv.sent) == 1 and "没有落盘" in drv.sent[0]
    assert "summary.md" in drv.sent[0]


def test_wait_artifact_recovers_silent_worker(tmp_path):
    """r6 死态：state 卡 working（ts 陈旧、非 userpromptsubmit），idle 催写与
    submit-stale 两路都探测不到，hook 静默判定重提任务行。"""
    pool, w, drv = _mk_pool(tmp_path, "pretooluse", 400)  # working，7 分钟前
    artifact = tmp_path / "out3" / "summary.md"
    artifact.parent.mkdir()
    import evo_harness.events as ev
    calls = {"n": 0}
    orig = ev.AsyncFileWatcher.wait_until

    async def fast_wait(self, pred, timeout, on_wake=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            artifact.write_text("# s", encoding="utf-8")
        return pred()

    ev.AsyncFileWatcher.wait_until = fast_wait
    try:
        ok = asyncio.run(pool._wait_artifact(
            w, "research-1", artifact, 2, reader="[research-1] FILE x"
        ))
    finally:
        ev.AsyncFileWatcher.wait_until = orig
    assert ok is True and drv.sent == ["[research-1] FILE x"]


def test_prompt_effect_stalled_resubmits_once(tmp_path):
    """P8.2 提交级 stalled：非 working 提交后 5s 无 hook 生命周期变化 -> 重提一次。"""
    pool, w, drv = _mk_pool(tmp_path, "stop", 1, state_val="idle")
    baseline = pool._state_sig(w)
    assert baseline[0] == "idle"

    async def _no_lifecycle(w, baseline, timeout):
        return False

    pool._wait_lifecycle = _no_lifecycle
    asyncio.run(pool._ensure_prompt_effect(
        w, "planner", "[planner] FILE x", baseline
    ))
    assert drv.sent == ["[planner] FILE x"]


def test_prompt_effect_skips_when_already_working(tmp_path):
    pool, w, drv = _mk_pool(tmp_path, "pretooluse", 1, state_val="working")
    baseline = pool._state_sig(w)
    assert baseline[0] == "working"
    asyncio.run(pool._ensure_prompt_effect(
        w, "planner", "[planner] FILE x", baseline
    ))
    assert drv.sent == []


def test_prompt_effect_no_resubmit_on_lifecycle_change(tmp_path):
    pool, w, drv = _mk_pool(tmp_path, "stop", 1, state_val="idle")
    baseline = pool._state_sig(w)

    async def _changed(w, baseline, timeout):
        return True

    pool._wait_lifecycle = _changed
    asyncio.run(pool._ensure_prompt_effect(
        w, "planner", "[planner] FILE x", baseline
    ))
    assert drv.sent == []


def test_timeout_redispatch_second_window(tmp_path):
    """超时重派：第一窗 timeout -> 第二窗重派同任务；第二窗 done 即通过。"""
    from evo_harness.stages import Harness

    h = Harness.__new__(Harness)

    class _Bus:
        def log_event(self, *args, **kwargs):
            pass

    class _Pool:
        def __init__(self):
            self.calls = []

        async def run_tasks(self, tasks):
            self.calls.append(dict(tasks))
            if len(self.calls) == 1:
                return {t: "timeout" for t in tasks}
            return {t: "done" for t in tasks}

    h.pool = _Pool()
    h.bus = _Bus()
    tasks = {"research-1": ("p", Path("summary.md"))}
    asyncio.run(h._run_tasks_checked("research", tasks))
    assert len(h.pool.calls) == 2
    assert set(h.pool.calls[0]) == {"research-1"}
    assert set(h.pool.calls[1]) == {"research-1"}


def test_timeout_redispatch_hard_stops_after_second_miss(tmp_path):
    """第二窗仍 timeout 才升级 GATE_FAILED，不提前陪葬。"""
    from evo_harness.stages import Harness
    from evo_harness.statemachine import HardStop

    h = Harness.__new__(Harness)

    class _Bus:
        def log_event(self, *args, **kwargs):
            pass

    class _Pool:
        async def run_tasks(self, tasks):
            return {t: "timeout" for t in tasks}

    h.pool = _Pool()
    h.bus = _Bus()
    tasks = {"research-1": ("p", Path("summary.md"))}
    try:
        asyncio.run(h._run_tasks_checked("research", tasks))
    except HardStop as exc:
        assert exc.kind == "GATE_FAILED"
        assert "research-1" in exc.detail
    else:
        raise AssertionError("第二窗仍缺产物应 HardStop")


def test_submit_stale_respects_nudge_budget(tmp_path):
    """审计 r3 must：submit-stale 分支也吃 nudges_left 总预算，死会话
    不许每 30s 无限重提到 900s（三路自愈共享预算的文档才是契约）。"""
    pool, w, drv = _mk_pool(tmp_path, "userpromptsubmit", 400)
    artifact = tmp_path / "out4" / "x.json"
    artifact.parent.mkdir()
    import evo_harness.events as ev
    calls = {"n": 0}
    orig = ev.AsyncFileWatcher.wait_until

    async def fast_wait(self, pred, timeout, on_wake=None):
        calls["n"] += 1
        await asyncio.sleep(0)
        return pred()  # 永远 False（产物不落）

    ev.AsyncFileWatcher.wait_until = fast_wait
    from types import SimpleNamespace
    pool.sm.budget = SimpleNamespace(task_wait_seconds=0.3, nudge_rounds=2)
    try:
        # 切片极小 + task_wait 0.3s：让等待快速到期
        pool._stalled_s = lambda: 0.01
        ok = asyncio.run(pool._wait_artifact(
            w, "t", artifact, 2, reader="[t] FILE x"
        ))
    finally:
        ev.AsyncFileWatcher.wait_until = orig
    assert ok is False
    # 重提次数 ≤ nudge_rounds（预算 2）：不许无限
    assert len(drv.sent) <= 2, f"重提 {len(drv.sent)} 次超出预算"



def test_idle_nudge_requires_positive_idle_signal(tmp_path):
    """accept-v04-r1 实证回归：state 文件缺失（hook 盲，codex 默认形态）
    不得被当 idle 触发催写（旧判 `not in ("idle", None)` 30s 级狂催）；
    hook 盲归静默判定（120/420s）管，本测试阈值压 0 也不许走 nudge。"""
    pool, w, drv = _mk_pool(tmp_path, "userpromptsubmit", 10)
    w.state_file.unlink()  # hook 盲：state 文件不存在
    artifact = tmp_path / "out" / "result.json"
    artifact.parent.mkdir()
    events = []
    pool.bus.log_event = staticmethod(
        lambda kind, detail="": events.append(kind))
    pool.sm = type("B2", (), {"budget": type(
        "B3", (), {"task_wait_seconds": 0.3})()})()  # 压窗：0.3s 出结果

    async def go():
        import evo_harness.events as ev

        async def fast_wait(self, pred, timeout, on_wake=None):
            return None  # 产物永不出现，快速推进轮次

        ev.AsyncFileWatcher.wait_until = fast_wait
        try:
            pool._stalled_s = lambda: 1e9   # 静默/停滞判定都不到期
            pool._silent_s = lambda: 1e9
            return await pool._wait_artifact(w, "t1", artifact,
                                             nudge_rounds=2)
        finally:
            import importlib
            importlib.reload(ev)

    assert asyncio.run(go()) is False
    assert "nudge" not in events  # 缺失态不催
