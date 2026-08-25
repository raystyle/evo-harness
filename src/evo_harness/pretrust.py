"""三 agent 目录预信任（spawn 前写入信任存储，根除信任框阻塞）。

存储格式全部本机实证（2026-08-24 WSL）：
- codex : ~/.codex/config.toml 追加 [projects."<abs>"] trust_level = "trusted"
- kimi  : ~/.kimi-code/workspaces.json 注册 wd_<name>_<sha256(root)[:12]> 条目
          + ~/.kimi-code/workspace-trust/wd_... 文件 {"root", "trustedAt"}（ms）
- claude: ~/.claude.json projects["<abs>"] 置 hasTrustDialogAccepted /
          hasTrustDialogHooksAccepted（后者是项目 hooks 的信任，装状态 hook 必需）

写入原则：只增不改、幂等、动前备份。上游口径（gh 实证）：
- codex 官方文档称信任是交互式、无通配符（issue #14345/#14547：--yolo 不绕过
  trust），但 [projects] 预写在实测中生效
- kimi v0.36.0 起信任框默认拒绝（直接 Enter=拒），预写是唯一无交互路径
- claude 的 bypassPermissions 只管工具权限，目录/hooks 信任仍是独立对话框
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path


def _codex_config() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _kimi_workspaces() -> Path:
    return Path.home() / ".kimi-code" / "workspaces.json"


def _kimi_trust_dir() -> Path:
    return Path.home() / ".kimi-code" / "workspace-trust"


def _claude_json() -> Path:
    return Path.home() / ".claude.json"


def _backup(path: Path) -> None:
    if path.exists():
        bak = path.with_suffix(path.suffix + ".evobak")
        if not bak.exists():  # 只留首份备份，避免版本堆积
            shutil.copy2(path, bak)


def kimi_workspace_key(root: Path) -> str:
    """wd_<basename>_<sha256(root)[:12]>（本机两组配对实证）。"""
    return f"wd_{root.name}_{hashlib.sha256(str(root).encode()).hexdigest()[:12]}"


def pretrust_codex(root: Path) -> bool:
    root = root.resolve()
    cfg = _codex_config()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    header = f'[projects."{root}"]'
    if header in text:
        return False
    _backup(cfg)
    block = f'\n{header}\ntrust_level = "trusted"\n'
    cfg.write_text(text + block, encoding="utf-8")
    return True


def pretrust_kimi(root: Path) -> bool:
    root = root.resolve()
    key = kimi_workspace_key(root)
    changed = False
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z"

    ws_file = _kimi_workspaces()
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"version": 1, "workspaces": {}}
    if ws_file.exists():
        _backup(ws_file)
        try:
            data = json.loads(ws_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"version": 1, "workspaces": {}}
    if key not in data.setdefault("workspaces", {}):
        data["workspaces"][key] = {
            "root": str(root), "name": root.name,
            "created_at": now_iso, "last_opened_at": now_iso,
        }
        changed = True
    ws_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    trust_dir = _kimi_trust_dir()
    trust_dir.mkdir(parents=True, exist_ok=True)
    trust_file = trust_dir / key
    if not trust_file.exists():
        trust_file.write_text(
            json.dumps({"root": str(root), "trustedAt": int(time.time() * 1000)}),
            encoding="utf-8",
        )
        changed = True
    return changed


def pretrust_claude(root: Path) -> bool:
    root = root.resolve()
    cj = _claude_json()
    data: dict = {}
    if cj.exists():
        _backup(cj)
        try:
            data = json.loads(cj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    proj = data.setdefault("projects", {}).setdefault(str(root), {})
    changed = False
    for flag in ("hasTrustDialogAccepted", "hasTrustDialogHooksAccepted"):
        if not proj.get(flag):
            proj[flag] = True
            changed = True
    if changed:
        cj.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return changed


def pretrust_all(root: Path) -> dict[str, bool]:
    """对给定目录预信任全部三端（返回各端是否发生了写入）。"""
    root = Path(root).resolve()
    return {
        "codex": pretrust_codex(root),
        "kimi": pretrust_kimi(root),
        "claude": pretrust_claude(root),
    }
