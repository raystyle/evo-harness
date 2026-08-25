"""hook 安装器（移植 install-agent-hooks.ps1 的幂等设计）。

安全边界：**默认只写 run 工作目录下的项目级配置**（.claude/settings.json）。
用户全局配置（~/.codex/config.toml / ~/.kimi-code/config.toml）只在显式授权
（`install_all(..., include_global=True)`，CLI 侧 `--global-hooks`）时才写，
那是用户的地盘，动它必须 opt-in。

claude hook 注册（win-rmux 实测要点）：
- 事件需要 matcher: "*"，否则可能不触发
- PermissionRequest 非标准事件（claude 永不报 blocked），用 Notification 顶
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# 脚本随包分发（src/evo_harness/scripts/）：uv tool / pip 装出的环境里
# 仓顶 scripts/ 不存在，包内路径才跨安装形态成立
HOOK_SCRIPT = Path(__file__).resolve().parent / "scripts" / "agent_state_hook.py"
HOOK_NAME = "evo_agent_state_hook.py"  # 落进 agent 配置目录的文件名


HOOK_HOME = ".evo-harness"  # 全局唯一落点目录（~/.evo-harness/）


def land_hook_script() -> Path:
    """hook 脚本全局唯一落点：~/.evo-harness/evo_agent_state_hook.py。

    四端（claude/codex/kimi/grok）配置统一指向这一份（用户定调：hook
    统一成一个 py 文件）。命令若指向工具安装路径，升级/重装/迁移即断链
    （accept-v04-r1 实证）；落全局单副本后自包含，且不再按 agent 目录
    散落多份、版本只看一处。幂等：内容相同不重写，不同则覆盖刷新。
    """
    dest_dir = Path.home() / HOOK_HOME
    dest = dest_dir / HOOK_NAME
    src = HOOK_SCRIPT.read_bytes()
    if not dest.exists() or dest.read_bytes() != src:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src)
    return dest


def _backup(path: Path) -> None:
    if path.exists():
        bak = path.with_suffix(path.suffix + ".evobak")
        if not bak.exists():  # 只留首份备份
            shutil.copy2(path, bak)

# (事件, matcher)
CLAUDE_EVENTS = [
    ("SessionStart", "*"),
    ("UserPromptSubmit", "*"),
    ("PreToolUse", "*"),
    ("PostToolUse", "*"),   # 工具结束也刷 ts：长 bash/pytest 期间不算静默（r8）
    ("Stop", "*"),
    ("Notification", "*"),
]


def hook_command(script: Path | None = None) -> str:
    """hook 命令（解释器绝对路径 + 落点脚本）。

    Windows 标准安装通常没有 python3（只有 python/py），sys.executable
    三平台都指向可用的那个解释器；script 缺省用包内源（测试直跑用），
    安装路径一律传 land_hook_script 的落点。
    """
    return f'"{sys.executable}" "{script or HOOK_SCRIPT}"'


CODEX_HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "Stop", "SessionEnd",
)


def install_codex_hooks() -> Path:
    """codex 状态 hook（源码实证 2026-08-24，codex-rs/config/src/hook_config.rs）：

    - 用户层声明在 **~/.codex/config.toml 的 [hooks.<Event>] 表**，
      `~/.codex/hooks.json` 不是用户配置加载点（仅插件源用 HooksFile）
    - handler 是 `#[serde(tag = "type")]` 枚举：条目必须含 `type = "command"`，
      超时键名 `timeout`（alias timeout_sec）
    - 信任按 `hooks.state.<key>.trusted_hash` 记忆（HookStateToml），
      启动 flags 带 `--dangerously-bypass-hook-trust` 跳过 hash 检查
    - codex Stop hook 实测不触发（win-rmux 沉淀），judge 以产物为准
    """
    import re

    cfg = Path.home() / ".codex" / "config.toml"
    script = land_hook_script()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""

    # features.hooks 总开关（缺失则补）
    if not re.search(r"(?m)^\s*hooks\s*=\s*", text):
        _backup(cfg)
        if re.search(r"(?m)^\s*\[features\]\s*$", text):
            text = text.replace(
                "[features]",
                "[features]\n# evo-harness: enable agent-state hooks\nhooks = true\n",
                1,
            )
        else:
            text += "\n[features]\n# evo-harness: enable agent-state hooks\nhooks = true\n"

    esc = hook_command(script).replace("\\", "\\\\").replace('"', '\\"')
    # 幂等按事件块判（整文件 command 早退会让后补的事件永远装不上）
    if all(f"[[hooks.{ev}]]" in text for ev in CODEX_HOOK_EVENTS):
        return cfg  # 全事件已装

    _backup(cfg)
    block = ["\n# evo-harness: agent-state hook（状态回写 EVO_STATE_FILE）"]
    for ev in CODEX_HOOK_EVENTS:
        # SessionEnd 会被 codex 钳到 3s（v0.148.0 实测警告
        # "clamping SessionEnd hook timeout to 3s"）·直接写 3 免告警
        to = 3 if ev == "SessionEnd" else 10
        block += [
            f"[[hooks.{ev}]]",
            'matcher = "*"',
            "  [[hooks.{ev}.hooks]]".replace("{ev}", ev),
            'type = "command"',
            f'command = "{esc}"',
            f"timeout = {to}",
            "",
        ]
    cfg.write_text(text + "\n".join(block), encoding="utf-8")
    return cfg


def install_kimi_hooks() -> Path:
    """kimi 全局 hooks（~/.kimi-code/config.toml 追加 [[hooks]] 块，幂等+备份）。"""
    cfg = Path.home() / ".kimi-code" / "config.toml"
    script = land_hook_script()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    content = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    esc = hook_command(script).replace("\\", "\\\\").replace('"', '\\"')
    _K = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PermissionRequest", "Stop", "Interrupt")
    if all(f'event = "{ev}"' in content for ev in _K):
        return cfg  # 全事件已装
    _backup(cfg)
    block = []
    for ev in ("SessionStart", "UserPromptSubmit", "PreToolUse",
               "PostToolUse", "PermissionRequest", "Stop", "Interrupt"):
        block += ["", "[[hooks]]", f'event = "{ev}"',
                  f'command = "{esc}"', "timeout = 10"]
    cfg.write_text(content + "\n".join(block) + "\n", encoding="utf-8")
    return cfg


def install_grok_hooks() -> Path:
    """grok 全局 hook（~/.grok/hooks/*.json，恒信任，claude 同构格式）。

    grok 兼容读 .claude/settings.json：项目级 hook 已由 claude 安装覆盖
    （launch 带 --trust 过 folder-trust 门）；全局侧写自己的目录，不污染
    ~/.claude。事件名与 claude 全同构（SessionStart/UserPromptSubmit/
    PreToolUse/PostToolUse/Stop/Notification）。
    """
    hooks_dir = Path.home() / ".grok" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = land_hook_script()
    hooks = hooks_dir / "evo-state.json"
    ours = {"type": "command", "command": hook_command(script), "timeout": 10}
    data: dict = {"hooks": {}}
    if hooks.exists():
        try:
            data = json.loads(hooks.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"hooks": {}}
    ev = data.setdefault("hooks", {})
    for event, _matcher in CLAUDE_EVENTS:
        entries = ev.setdefault(event, [])
        if any(HOOK_NAME in e.get("hooks", [{}])[0].get("command", "")
               for e in entries if isinstance(e, dict)):
            continue
        entries.append({"matcher": "*", "hooks": [ours]})
    hooks.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return hooks


def install_all(project_dir: Path, include_global: bool = False) -> dict[str, str]:
    """装状态 hook：项目级 claude 恒装（grok 项目级同源兼容读）；codex/
    kimi/grok 全局配置仅 include_global（显式授权）时写，默认绝不碰
    用户全局配置。"""
    installed = {"claude": str(install_claude_project_hooks(project_dir))}
    if include_global:
        installed["codex"] = str(install_codex_hooks())
        installed["kimi"] = str(install_kimi_hooks())
        installed["grok"] = str(install_grok_hooks())
    return installed


def install_claude_project_hooks(project_dir: Path) -> Path:
    """把状态 hook 写进 <project>/.claude/settings.json（幂等 + 备验）。

    只追加我们的 hook，保留用户已有的其它 hook；重复安装不产生重复条目。
    """
    claude_dir = Path(project_dir) / ".claude"
    script = land_hook_script()
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_dir / "settings.json"

    data: dict = {}
    if settings.exists():
        backup = settings.with_suffix(".json.bak")
        shutil.copy2(settings, backup)
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    hooks = data.setdefault("hooks", {})
    cmd = hook_command(script)
    # schema 要求 type 字段；缺了 claude 启动即弹 Settings Error 模态框
    ours = {"type": "command", "command": cmd, "timeout": 10}

    for event, matcher in CLAUDE_EVENTS:
        entries = hooks.setdefault(event, [])
        # 幂等：已有任一指向本脚本的条目即跳过（按脚本名判，换解释器
        # 路径/工具重装不产生重复注册）
        existing = {
            e.get("hooks", [{}])[0].get("command", "")
            for e in entries if isinstance(e, dict)
        }
        if any(HOOK_NAME in c for c in existing):
            continue
        entries.append({"matcher": matcher, "hooks": [ours]})

    settings.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return settings
