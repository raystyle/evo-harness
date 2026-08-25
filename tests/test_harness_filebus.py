"""evo_harness filebus 门禁谓词回归。"""

import json

import pytest

from evo_harness.filebus import FileBus


def _bus(tmp_path) -> FileBus:
    return FileBus(tmp_path / "shared", "t1")


def test_explore_gate_requires_candidates_with_clone_url(tmp_path):
    bus = _bus(tmp_path)
    ok, _ = bus.explore_gate()
    assert ok is False
    p = bus.task_out("explore-rank", "candidates.json")
    p.write_text(json.dumps([{"repo": "a"}, {"clone_url": "x"},
                             {"clone_url": "y"}, {"clone_url": "z"}]),
                 encoding="utf-8")
    ok, detail = bus.explore_gate()
    assert ok is True and "3" in detail


def test_research_gate_rejects_stub(tmp_path):
    bus = _bus(tmp_path)
    s = bus.task_out("research-synth", "synthesis.md")
    s.write_text("太短", encoding="utf-8")
    assert bus.research_gate()[0] is False
    s.write_text("x" * 300, encoding="utf-8")
    assert bus.research_gate()[0] is True


def test_plan_gate_checks_three_contracts(tmp_path):
    bus = _bus(tmp_path)
    plan_dir = bus.task_dir("planner") / "out"
    plan_dir.mkdir(parents=True, exist_ok=True)
    assert bus.plan_gate()[0] is False  # 全缺
    (plan_dir / "goal_spec.json").write_text("{}", encoding="utf-8")
    (plan_dir / "plan.json").write_text("not-json", encoding="utf-8")
    assert bus.plan_gate()[0] is False  # 非法 JSON
    (plan_dir / "plan.json").write_text('{"steps": []}', encoding="utf-8")
    (plan_dir / "allocations.json").write_text("{}", encoding="utf-8")
    assert bus.plan_gate()[0] is False  # 缺 merge_order
    (plan_dir / "plan.json").write_text(
        '{"steps": [{"id": "a"}], "merge_order": ["u1"]}', encoding="utf-8"
    )
    (plan_dir / "allocations.json").write_text(
        '{"u1": {"branch": "agent/u1"}}', encoding="utf-8"
    )
    assert bus.plan_gate()[0] is True


def test_plan_gate_rejects_bad_allocations(tmp_path):
    """allocations 必须是非空 dict：[]/字符串/null 会让下游 .items() 抛
    AttributeError（run.json 永久 running）；{} 则零工作假 DONE（claude r2）。"""
    bus = _bus(tmp_path)
    plan_dir = bus.task_dir("planner") / "out"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "goal_spec.json").write_text("{}", encoding="utf-8")
    (plan_dir / "plan.json").write_text(
        '{"steps": [{"id": "a"}], "merge_order": ["u1"]}', encoding="utf-8"
    )
    for bad in ("[]", '"x"', "null", "{}", '{"u1": "not-a-dict"}'):
        (plan_dir / "allocations.json").write_text(bad, encoding="utf-8")
        ok, detail = bus.plan_gate()
        assert ok is False, f"应拒绝 allocations={bad}"
        assert "allocations" in detail
    (plan_dir / "allocations.json").write_text(
        '{"u1": {"branch": "agent/u1", "scope": []}}', encoding="utf-8"
    )
    assert bus.plan_gate()[0] is True
    # r5 实证：unit 对象数组（语义对形状错）→ 归一化为 dict 后过门禁
    (plan_dir / "allocations.json").write_text(
        '[{"unit_id": "u1", "branch": "agent/u1", "scope": []}]',
        encoding="utf-8",
    )
    ok, detail = bus.plan_gate()
    assert ok is True, detail
    from evo_harness.filebus import normalize_allocations as na
    assert na(["x"]) is None  # 无 unit_id 的数组仍拒
    assert na({"u1": {}}) == {"u1": {}}


def test_review_route_three_branches(tmp_path):
    bus = _bus(tmp_path)
    for uid, verdict in (("u1", "pass"), ("u2", "revise"), ("u3", "reject")):
        p = bus.review_verdict(uid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"verdict": verdict}), encoding="utf-8")
    # 任一 reject -> ARCH_FAIL
    assert bus.review_route(["u1", "u2", "u3"]) == "ARCH_FAIL"
    bus.review_verdict("u3").unlink()
    # 有 revise 无 reject -> LOCAL_FAIL
    assert bus.review_route(["u1", "u2"]) == "LOCAL_FAIL"
    bus.review_verdict("u2").unlink()
    # 全 pass -> PASS_ALL
    assert bus.review_route(["u1"]) == "PASS_ALL"


def test_run_json_atomic_stage_write(tmp_path):
    bus = _bus(tmp_path)
    bus.set_stage("PLAN_RUNNING")
    bus.log_event("x", "y")
    data = bus.read_run()
    assert data["stage"] == "PLAN_RUNNING"
    assert data["history"][-1] == {"t": data["history"][-1]["t"], "kind": "x", "detail": "y"}


def test_fingerprint_tracks_artifact_changes(tmp_path):
    bus = _bus(tmp_path)
    fp1 = bus.fingerprint()
    (bus.stage_dir("explore") / "a.txt").write_text("1", encoding="utf-8")
    fp2 = bus.fingerprint()
    assert fp1 != fp2


def test_decision_roundtrip_and_autopilot(tmp_path):
    """主 agent 决策节点：请求→决断→事件；超时默认仅 autopilot 模式（>0），
    真 run（<=0）无限期等人不默认放行。"""
    import asyncio
    bus = FileBus(tmp_path / "s", "r1")
    bus.request_decision("plan-approval", "3 steps", ["approve", "abort"],
                         default="approve", timeout_s=0.3)
    assert bus.read_decision("plan-approval")["status"] == "pending"
    assert bus.decide("plan-approval", "nope") is False      # 非法 choice
    assert bus.decide("plan-approval", "approve", "ok") is True
    assert asyncio.run(bus.await_decision("plan-approval", 1.0)) == "approve"
    assert bus.decide("plan-approval", "abort") is False     # 已决不可改

    bus2 = FileBus(tmp_path / "s2", "r1")
    bus2.request_decision("plan-approval", "b", ["approve", "abort"],
                          default="abort", timeout_s=0.2)
    assert asyncio.run(bus2.await_decision("plan-approval", 0.2)) == "abort"
    d = bus2.read_decision("plan-approval")
    assert d["choice"] == "abort" and "autopilot" in d["rationale"]

    # 真 run 语义：timeout<=0 时窗口内无人决策绝不自动放行
    bus3 = FileBus(tmp_path / "s3", "r1")
    bus3.request_decision("plan-approval", "b", ["approve", "abort"],
                          default="approve", timeout_s=0)
    import evo_harness.events as ev
    orig = ev.wait_for_files
    calls = {"n": 0}

    async def fast(_d, _p, t):
        calls["n"] += 1
        await asyncio.sleep(0)   # 让出控制权， cancellation 可达
        if calls["n"] >= 3:
            raise asyncio.CancelledError
        return None

    ev.wait_for_files = fast
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bus3.await_decision("plan-approval", 0.0))
    finally:
        ev.wait_for_files = orig
    assert calls["n"] == 3  # 循环在继续等，不是一次后放行
    assert bus3.read_decision("plan-approval")["status"] == "pending"
