"""evo-harness 配置：agent 命令表、预算硬上限、会话与路径约定。

预算是控制面的「硬停止」参数，写死在代码里由 statemachine 执行，
不依赖模型自觉（见 references/harness.md §7 硬停止）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentSpec:
    """一个可驱动的终端 agent（TUI 模式跑在 pane 里）。"""

    name: str
    cmd: str
    args: tuple[str, ...] = ()
    ready_text: str = ""  # pane 出现该片段视为启动完成（空则退化为固定等待）

    @property
    def cmdline(self) -> str:
        return " ".join((self.cmd, *self.args))


# 启动 flags 来自 win-rmux launch-unit.ps1 的实测沉淀（troubleshooting 验证过）。
# ready_text：TUI 就绪标志（输入框出现才算可接收输入；空串=退化为固定等待）。
DEFAULT_AGENTS: dict[str, AgentSpec] = {
    "codex": AgentSpec(
        name="codex",
        cmd="codex",
        args=(
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--no-alt-screen",
        ),
        ready_text="› Ask",  # codex 输入态标记（0.148 实测：› Ask Codex...）
    ),
    # kimi 输入符是 `>`（│ > │ 线框，0.38 实测）；单字符泛，但 wait_ready
    # 只是就绪启发，真活性判官是探针（--next-text 回显）→ 误命中无害
    "kimi": AgentSpec(name="kimi", cmd="kimi", args=("--auto",), ready_text=">"),
    "claude": AgentSpec(
        name="claude",
        cmd="claude",
        args=(
            # 预接受旗标（2.1.238+）：跳过 bypass 模式的启动安全确认框·
            # r3 实证该框默认选中 1. No, exit，会吞掉一切提交文本
            "--allow-dangerously-skip-permissions",
            "--dangerously-skip-permissions",
        ),
        # 就绪特征用状态行而非 ❯（对话框选项光标也是 ❯，r3 实证误判就绪）
        ready_text="bypass permissions on",
    ),
    # fake：确定性 E2E 用（读 prompt -> sleep -> 写 result.json），不依赖真 LLM。
    "fake": AgentSpec(
        name="fake", cmd="bash", args=(),
        ready_text="fake-agent ready",  # 夹具启动即打印（scripts/fake_agent.py）
    ),
}


@dataclass
class Budget:
    """硬停止四条（statemachine 强制执行）。"""

    unit_max_retries: int = 3          # 每 unit revise 重试上限
    stage_max_minutes: float = 40.0    # 每阶段墙钟
    global_max_minutes: float = 240.0  # 整个 run 墙钟
    no_progress_rounds: int = 3        # 连续 N 轮 artifact 指纹无变化 -> NO_PROGRESS
    task_wait_seconds: float = 900.0    # 单任务产物等待上限（真实 LLM review
    #                                   # 实测 120s 必误杀，claude r1 报告 #2）
    prompt_effect_seconds: float = 5.0     # 非 working 提交后 5s 无 hook 生命周期
    #                                       # 变化即判 stalled（herdr/orca 双源同值）
    submit_stalled_seconds: float = 120.0  # 提交后零工具调用超此值判死会话
    #                                       # （k3 config.invalid 实证：烧满 900s）
    worker_silent_seconds: float = 420.0   # hook 通道整体静默超此值（r6 实证：
    #                                       # state 卡 working/Stop 未触发/消息队列
    #                                       # 路径 hook 失明，三路全都探测不到）
    nudge_rounds: int = 2               # 催写轮数
    # 决策节点（控制模型 v2）：真 agent run 无限期等主 agent decide（无人来
    # 也不默认放行·批准是责任不是形式）；fake/E2E 模式自动放行走默认
    decision_autopilot_s: float = 0.0    # 0=等人；>0=fake 超时自动默认
    decision_notify_cmd: str = ""        # 请求决策时的 shell 回调（env 注入
    #                                    # EVO_RUN/EVO_NODE/EVO_BRIEF/EVO_CHOICES）
    # notifyd 决策唤醒守护（herdr 操作平面）：idle 门 + prompt 注入
    notify_poll_seconds: float = 2.0     # 决策目录轮询间隔
    notify_spacing_seconds: float = 600.0  # 同一决策节点注入重发间隔
    #                                       # （失败注入 30s 短退避，notifyd 内建）
    notify_idle_wait_ms: int = 600_000   # 每轮等主 agent idle 的窗口


# 首次信任/确认框特征与按键序列（CoreSkills agent-trust-mechanisms.md 实测沉淀）。
# 匹配大小写不敏感；kimi 默认高亮在 "Don't trust"，必须 Up×3 到信任项再 Enter。
DIALOGS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "codex": [
        ("do you trust the contents", ("Enter",)),
        ("do you trust this directory", ("1", "Enter")),
    ],
    "kimi": [
        ("don't trust", ("Up", "Up", "Up", "Enter")),
        ("trust this folder", ("Enter",)),
    ],
    "claude": [
        ("allow external imports", ("Enter",)),
        ("do you trust the folder", ("Enter",)),
        ("quick safety check", ("Enter",)),
        # bypass permissions 启动安全确认（r3 实证：默认选中 1. No, exit，
        # 会吞掉一切提交文本；选 2 接受才进会话）
        ("no, exit", ("2", "Enter")),
        # 防御：settings 异常时的模态框，选 3「不带这些设置继续」保 run 不死
        ("settings error", ("3", "Enter")),
    ],
}


@dataclass
class HarnessConfig:
    """一次 harness 部署的运行配置。"""

    session_prefix: str = "evo-harness"
    socket_name: str = "evo-harness"
    # 任务产物根（win-rmux .rmux_tasks 同款点目录约定；gitignore 必须包含）
    shared_root: Path = field(default_factory=lambda: Path(".evo_tasks"))
    agents: dict[str, AgentSpec] = field(
        default_factory=lambda: dict(DEFAULT_AGENTS)
    )
    budget: Budget = field(default_factory=Budget)
    # fake 模式：把所有 agent 的启动命令替换成该脚本（确定性 E2E）。
    fake_agent_script: Path | None = None
    launch_settle_seconds: float = 45.0 # agent TUI 欢迎屏渲染等待（多实例并发
    #                                    # 下第 4+ 个 pane 明显变慢，8s 实测不够）
    # fan-out 轮转序：同阶段多 unit 依次分配不同 agent·跨模型并行才有多样性
    # （同模型并行共享盲区；win-rmux review-cycle 的三方一致同理）
    fanout_agents: tuple[str, ...] = ("claude", "kimi", "codex")
    # 全局 hook 安装（写 ~/.codex/config.toml 与 ~/.kimi-code/config.toml）默认关：
    # 动用户全局配置必须显式授权（--global-hooks）；默认只装项目级 claude hook
    install_global_hooks: bool = False

    def session_name(self, run_id: str) -> str:
        return f"{self.session_prefix}-{run_id}"

    def agent_cmdline(self, name: str) -> str:
        """fake 模式优先；否则查表。"""
        if self.fake_agent_script is not None:
            import sys

            return f"{sys.executable} {self.fake_agent_script}"
        spec = self.agents.get(name)
        if spec is None:
            raise KeyError(f"未知 agent: {name}（可用: {sorted(self.agents)}）")
        return spec.cmdline
