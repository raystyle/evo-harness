"""P8 接口契约面：五态语义 + blocked 守卫 / 三段式发送 / monitor 落位的唯一声明处。

背景与边界（docs/harness-herdr.md；r8 iface 探索 dbef158 在当前 main 上重做）：

- 五态思想来自 herdr（idle/working/blocked/done/unknown）；hook 通道仍为权威
  （hook_authority 模式不变），本模块只做**映射、判定与接口声明**。
- 纯度约束（契约可被任意层 import）：不依赖终端 SDK、不做文件 I/O、不读进程
  环境变量（env 一律以参数传入）。driver / pool / monitor / cli 各自接线。
- 三个 runtime_checkable Protocol 是 P8.1/P8.4 实现单元的**接口契约**：方法名、
  参数与返回语义在此钉死；runtime 检查只保证方法存在，语义以各 Protocol 的
  docstring 为准。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Protocol, runtime_checkable


class AgentState(str, Enum):
    """agent 生命周期五态（值即 hook/worker.json 落盘的稳定字符串）。"""

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------- 五态映射 ----

# hook 事件 → 五态（小写键）。与 scripts/agent_state_hook.py 的三态
# STATE_MAP 对齐并扩展 done/unknown 直传；未知事件不在表内 → UNKNOWN。
HOOK_STATE_MAP: dict[str, str] = {
    "session": "idle",
    "sessionstart": "idle",
    "idle": "idle",
    "stop": "idle",
    "interrupt": "idle",
    "sessionend": "unknown",  # hook 脚本无此键 → unknown（对拍测试锁定）
    "notification": "idle",
    "userpromptsubmit": "working",
    "pretooluse": "working",
    "posttooluse": "working",
    "posttoolusefailure": "working",
    "subagentstart": "working",
    "subagentstop": "working",
    "precompact": "working",
    "permissionresult": "working",
    "permissionrequest": "blocked",
    "working": "working",
    "blocked": "blocked",
    "done": "done",
    "unknown": "unknown",
}

# worker.json 持久化字段名（pool 写入 / monitor 读取）。
WORKER_STATE_KEY = "state"
WORKER_SEEN_KEY = "seen"


def hook_state(event: str) -> AgentState:
    """hook 事件 → 五态。未知/空事件 → UNKNOWN（识别不了不装懂，绝不硬猜）。"""
    key = str(event).strip().lower()
    return AgentState(HOOK_STATE_MAP.get(key, "unknown"))


def worker_state(hook_state_value: str | AgentState | None, seen: bool) -> AgentState:
    """把 hook 通道状态投影到 worker.json 展示五态。

    done=idle+未见：hook 报 idle 且 `seen=False` 时投影为 done（herdr 的
    「done 但 tab 未见」展示区分）；`seen=True` 后才回到 idle。working/
    blocked/done 直传；无 hook 状态或未识别 → UNKNOWN（不装懂）。
    """
    if isinstance(hook_state_value, AgentState):
        state = hook_state_value
    elif isinstance(hook_state_value, str):
        try:
            state = AgentState(hook_state_value.strip().lower())
        except ValueError:
            state = AgentState.UNKNOWN
    else:
        state = AgentState.UNKNOWN

    if state is AgentState.UNKNOWN:
        return AgentState.UNKNOWN
    if state is AgentState.IDLE and not seen:
        return AgentState.DONE
    return state


# ---------------------------------------------------- P8.1 blocked 守卫契约 ----

def sendable(state: str | AgentState | None) -> bool:
    """blocked 守卫的判定核：先查再发，只有确证 BLOCKED 才拒发。

    UNKNOWN 放行：herdr 的 agent_blocked 只在确证审批/问询框时触发；无 hook
    信号（识别不了）时若一律拒发，无 hook 的 agent 将永远发不出去，此时由
    屏幕对话框扫描兜底。DONE 与 IDLE 等价（就绪，可接下一单）。
    """
    if isinstance(state, AgentState):
        s = state
    elif isinstance(state, str):
        try:
            s = AgentState(state.strip().lower())
        except ValueError:
            s = AgentState.UNKNOWN
    else:
        s = AgentState.UNKNOWN
    return s is not AgentState.BLOCKED


# ------------------------------------------------------ P8.1 三段式发送契约 ----

# bracketed-paste 包裹序列（DECSET 2004 启用的 TUI 会把包裹内容当粘贴整块
# 处理，不做逐键解释）。仅在目标 pane 应用已开 bracketed 模式时才有意义·
# 「感知」= 发前先判模式，未开启则退化为普通 send-text。
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"

# Enter 的两种编码：键 token（现状 send_keys "Enter"）与 hex 编码
# （send-keys -H 0d·逐字节下发、不被当文本回显前缀吞掉的编码 Enter）。
ENTER_KEY = "Enter"
ENTER_HEX = "0d"

# 提交时序常量：文本与 Enter 之间的原生静默等待（wait-pane --stable-for）
# 时长，毫秒。driver.drive 的原 300ms 内联值收敛到此唯一常量。
SUBMIT_DELAY_MS = 300


def bracketed_wrap(text: str) -> str:
    """bracketed-paste 包裹（START ... END）。调用方负责先判 pane 模式。"""
    return f"{BRACKETED_PASTE_START}{text}{BRACKETED_PASTE_END}"


# ------------------------------------------------------ P8.4 monitor 落位契约 ----

# herdr 会话身份环境变量（=1 时在 herdr pane 内）与落位默认窗格。
HERDR_ENV_VAR = "HERDR_ENV"
MONITOR_PANE = "right"


def herdr_active(env: Mapping[str, str]) -> bool:
    """是否在 herdr 会话内（严格 HERDR_ENV=1，"true"/"0" 均不算）。"""
    return env.get(HERDR_ENV_VAR) == "1"


def placement_enabled(env: Mapping[str, str], pane: str | None) -> bool:
    """落位条件：herdr 会话内 **且** 显式指定了 --pane；其余一律静默跳过。"""
    return herdr_active(env) and bool(pane)


# ------------------------------------------------------------ 接口 Protocol ----

@runtime_checkable
class BlockedGuard(Protocol):
    """P8.1 blocked 先查再发，**纯契约声明（runtime 未接线，P8 后续）**。

    drive 发送 prompt 前调用：实现扫描目标 pane 的审批/问询框（屏幕对话框
    特征 + hook blocked 状态），返回 True 允许发送；False = 拒发（等价
    herdr 的 agent_blocked），调用方转 resolve_dialogs 或重试。判定核见
    `sendable()`。
    """

    async def ensure_sendable(self, pane: object) -> bool: ...


@runtime_checkable
class ThreePhaseSender(Protocol):
    """P8.1 三段式发送，**纯契约声明（runtime 未接线，P8 后续）**。

    一段：bracketed-paste 感知发送文本（`bracketed_wrap` + 模式判断）；
    二段：短延迟（`SUBMIT_DELAY_MS`）后编码 Enter（`ENTER_HEX`/`-H`）；
    三段：等 settled（rmux 原生 quiet --stable-for）。返回是否提交成功。
    """

    async def send_three_phase(
        self, pane: object, text: str, *, bracketed: bool = True
    ) -> bool: ...


@runtime_checkable
class MonitorPlacement(Protocol):
    """P8.4 monitor 自动落位，**纯契约声明**（cli._place_herdr_monitor
    是现行实现，未走本 Protocol 接线）。

    `placement_enabled` 为真时：pane split（--no-focus、--current 定位）+
    shell 稳定等待 + pane run "… monitor --run-id <run_id>"；非 herdr 会话
    静默跳过（返回 False，不报错）。不 close 非自开 pane。
    """

    async def place_monitor(self, run_id: str, pane: str = MONITOR_PANE) -> bool: ...
