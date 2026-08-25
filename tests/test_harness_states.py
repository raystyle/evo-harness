"""evo_harness states 契约回归：五态类型 + blocked 守卫 / 三段式 / 落位 API。

P8 iface-contracts：把四个实现单元（p81-driver/p82-pool-states/
p83-hook-display/p84-placement）共同依赖的接口面钉死，值、映射、判定、
常量与 Protocol 形状。契约在此全绿，实现单元按此接线。
"""

import inspect

import pytest

from evo_harness import states
from evo_harness.states import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    ENTER_HEX,
    ENTER_KEY,
    HERDR_ENV_VAR,
    HOOK_STATE_MAP,
    MONITOR_PANE,
    SUBMIT_DELAY_MS,
    WORKER_SEEN_KEY,
    WORKER_STATE_KEY,
    AgentState,
    BlockedGuard,
    MonitorPlacement,
    ThreePhaseSender,
    bracketed_wrap,
    herdr_active,
    hook_state,
    placement_enabled,
    sendable,
    worker_state,
)


# ---------------------------------------------------------------- 五态类型 ----

def test_five_states_exact_values():
    """五态且仅五态；值即落盘稳定字符串（worker.json/hook 通道共用）。"""
    assert {s.value for s in AgentState} == {
        "idle", "working", "blocked", "done", "unknown",
    }
    assert all(isinstance(s, str) for s in AgentState)  # str Enum，可直接 json 落盘


@pytest.mark.parametrize(
    "event,want",
    [
        ("sessionstart", AgentState.IDLE),
        ("stop", AgentState.IDLE),
        ("notification", AgentState.IDLE),  # claude 回合结束提示非 blocked
        ("userpromptsubmit", AgentState.WORKING),
        ("pretooluse", AgentState.WORKING),
        ("posttoolusefailure", AgentState.WORKING),
        ("permissionresult", AgentState.WORKING),
        ("permissionrequest", AgentState.BLOCKED),
        ("done", AgentState.DONE),  # 直传态
    ],
)
def test_hook_state_map_spot(event, want):
    assert hook_state(event) == want


def test_hook_state_unknown_not_guessed():
    """未知/空事件 → UNKNOWN（不装懂）；大小写与空白归一。"""
    assert hook_state("some-future-event") is AgentState.UNKNOWN
    assert hook_state("") is AgentState.UNKNOWN
    assert hook_state("  PermissionRequest ") is AgentState.BLOCKED
    assert hook_state("UserPromptSubmit") is AgentState.WORKING


def test_hook_map_wellformed():
    """映射表自身契约：键全小写非空，值皆合法五态。"""
    for key, value in HOOK_STATE_MAP.items():
        assert key and key == key.lower()
        assert AgentState(value) in AgentState


# ------------------------------------------------------- worker.json 投影 ----

def test_worker_projection_done_is_idle_unseen():
    """done=idle+未见；seen 后回 idle。"""
    assert worker_state("idle", seen=False) is AgentState.DONE
    assert worker_state("idle", seen=True) is AgentState.IDLE
    assert worker_state(AgentState.IDLE, seen=False) is AgentState.DONE


def test_worker_projection_passthrough():
    """working/blocked/done 直传，与 seen 无关。"""
    assert worker_state("working", seen=False) is AgentState.WORKING
    assert worker_state("blocked", seen=True) is AgentState.BLOCKED
    assert worker_state("done", seen=False) is AgentState.DONE


def test_worker_projection_unknown_not_guessed():
    """无 hook 状态/垃圾值 → UNKNOWN；字符串归一后识别。"""
    assert worker_state(None, seen=False) is AgentState.UNKNOWN
    assert worker_state("garbage", seen=False) is AgentState.UNKNOWN
    assert worker_state(" WORKING ", seen=True) is AgentState.WORKING


def test_worker_json_keys_stable():
    assert WORKER_STATE_KEY == "state"
    assert WORKER_SEEN_KEY == "seen"


# ---------------------------------------------------- blocked 守卫 API 契约 ----

def test_sendable_blocks_only_blocked():
    """先查再发：唯 BLOCKED 拒发；idle/working/done 放行。"""
    assert sendable(AgentState.BLOCKED) is False
    assert sendable("blocked") is False
    assert sendable(" BLOCKED ") is False  # 归一
    for s in (AgentState.IDLE, AgentState.WORKING, AgentState.DONE):
        assert sendable(s) is True


def test_sendable_unknown_passes():
    """UNKNOWN 放行（positive-evidence 守卫）：无 hook 的 agent 不得被锁死。"""
    assert sendable(AgentState.UNKNOWN) is True
    assert sendable(None) is True
    assert sendable("nonsense") is True


def test_blocked_guard_protocol_shape():
    """BlockedGuard 可运行时检查：有 ensure_sendable 才算实现。"""

    class _Good:
        async def ensure_sendable(self, pane):
            return True

    class _Bad:
        pass

    assert isinstance(_Good(), BlockedGuard)
    assert not isinstance(_Bad(), BlockedGuard)
    assert inspect.iscoroutinefunction(BlockedGuard.ensure_sendable)


# ------------------------------------------------------ 三段式发送 API 契约 ----

def test_bracketed_paste_sequences_exact():
    """DEC 2004 bracketed-paste 包裹序列逐字节钉死。"""
    assert BRACKETED_PASTE_START == "\x1b[200~"
    assert BRACKETED_PASTE_END == "\x1b[201~"


def test_bracketed_wrap_roundtrip():
    assert bracketed_wrap("hello") == "\x1b[200~hello\x1b[201~"
    assert bracketed_wrap("") == "\x1b[200~\x1b[201~"
    wrapped = bracketed_wrap("多行\n文本")
    assert wrapped.startswith(BRACKETED_PASTE_START)
    assert wrapped.endswith(BRACKETED_PASTE_END)


def test_enter_encodings():
    """编码 Enter：token 与 hex 两路常量钉死（send-keys Enter / -H 0d）。"""
    assert ENTER_KEY == "Enter"
    assert ENTER_HEX == "0d"


def test_submit_delay_canonical():
    """提交间隔唯一常量：300ms，亚秒级短延迟（driver 内联值已收敛到此）。"""
    assert SUBMIT_DELAY_MS == 300
    assert 0 < SUBMIT_DELAY_MS < 1000


def test_three_phase_sender_protocol_shape():
    class _Good:
        async def send_three_phase(self, pane, text, *, bracketed=True):
            return True

    class _Bad:
        pass

    assert isinstance(_Good(), ThreePhaseSender)
    assert not isinstance(_Bad(), ThreePhaseSender)
    assert inspect.iscoroutinefunction(ThreePhaseSender.send_three_phase)


# --------------------------------------------------------- 落位 API 契约 ----

def test_herdr_env_contract():
    """环境变量名与「=1」严格语义钉死。"""
    assert HERDR_ENV_VAR == "HERDR_ENV"
    assert herdr_active({"HERDR_ENV": "1"}) is True
    assert herdr_active({}) is False
    assert herdr_active({"HERDR_ENV": "0"}) is False
    assert herdr_active({"HERDR_ENV": "true"}) is False  # 严格，不宽松解析


def test_placement_enabled_gate():
    """落位 = herdr 会话内 ∧ 显式 --pane；其余静默跳过。"""
    assert placement_enabled({"HERDR_ENV": "1"}, "right") is True
    assert placement_enabled({"HERDR_ENV": "1"}, None) is False
    assert placement_enabled({}, "right") is False
    assert MONITOR_PANE == "right"  # --pane right 默认落右窗格


def test_monitor_placement_protocol_shape():
    class _Good:
        async def place_monitor(self, run_id, pane=MONITOR_PANE):
            return True

    class _Bad:
        pass

    assert isinstance(_Good(), MonitorPlacement)
    assert not isinstance(_Bad(), MonitorPlacement)
    assert inspect.iscoroutinefunction(MonitorPlacement.place_monitor)
    # 契约默认窗格与常量一致
    sig = inspect.signature(MonitorPlacement.place_monitor)
    assert sig.parameters["pane"].default == MONITOR_PANE


# ------------------------------------------------------------- 模块纯度 ----

def test_module_stays_pure():
    """契约可被任意层 import：不依赖终端 SDK、不读进程环境变量、不做文件 I/O。"""
    src = inspect.getsource(states)
    assert "import librmux" not in src
    assert "os.environ" not in src
    assert "Path(" not in src
