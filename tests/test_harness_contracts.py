"""三契约 CLI 生成回归，控制面 JSON 由程序生成，模型只填参数。

r5/r6 两轮死于模型手写 JSON 形状（数组/map、缺文件）；contracts.ContractStore
保证形状，门禁照常校验（双保险）。
"""

import pytest

from evo_harness.contracts import ContractError, ContractStore
from evo_harness.filebus import FileBus


def _store(tmp_path) -> ContractStore:
    return ContractStore(tmp_path / ".evo_tasks", "r1")


def test_full_roundtrip_passes_plan_gate(tmp_path):
    store = _store(tmp_path)
    store.goal("目标", ["不做 X"], ["判据1", "判据2"])
    store.step("iface", "接口契约", [])
    store.step("impl", "实现", ["iface"])
    store.alloc("impl", "p8/impl", ["src/**"], ["测试绿"])
    store.merge_order(["iface", "impl"])
    bus = FileBus(tmp_path / ".evo_tasks", "r1")
    assert bus.plan_gate() == (True, "三契约齐备")


def test_contract_store_rejects_garbage(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ContractError):
        store.goal("", [], ["x"])          # 空 goal
    with pytest.raises(ContractError):
        store.goal("g", [], [])            # 零判据
    with pytest.raises(ContractError):
        store.step("BAD_ID!", "x", [])     # id 字符集
    with pytest.raises(ContractError):
        store.step("a", "x", ["ghost"])    # 依赖未登记
    store.goal("g", [], ["c"])
    store.step("a", "x", [])
    with pytest.raises(ContractError):
        store.step("a", "x", [])           # 重复
    with pytest.raises(ContractError):
        store.alloc("u1", "no-slash", ["s"], [])   # branch 形状
    with pytest.raises(ContractError):
        store.alloc("u1", "t/u1", [], [])          # 空 scope
    store.alloc("u1", "t/u1", ["s"], [])
    store.step("b", "y", ["a"])
    with pytest.raises(ContractError):
        store.merge_order(["a"])           # 未覆盖全部 step（缺 b）
    with pytest.raises(ContractError):
        store.merge_order(["a", "ghost"])  # 未登记 step
    store.merge_order(["a", "b"])          # 完整覆盖即通过
    # 手写破坏（allocations 变数组）后再 alloc 必须拒·逼回 CLI 通道
    store.allocs_p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ContractError):
        store.alloc("u2", "t/u2", ["s"], [])


def test_alloc_branch_unique_across_units(tmp_path):
    """rc-r1 must-fix 同源：1 unit = 1 branch，重名 branch 会让第二个
    `worktree add -b` 必败（且异常裸穿——另见 escalation 兜底），登记期即拒。"""
    store = _store(tmp_path)
    store.goal("g", [], ["c"])
    store.alloc("u1", "agent/feat", ["src/**"], [])
    with pytest.raises(ContractError, match="已被 unit"):
        store.alloc("u2", "agent/feat", ["docs/**"], [])
    store.alloc("u2", "agent/other", ["docs/**"], [])  # 不同 branch 通过
