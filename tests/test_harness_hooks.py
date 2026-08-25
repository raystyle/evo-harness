"""evo_harness hook 通道与确认框配置回归。"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from evo_harness.config import DIALOGS
from evo_harness import monitor
from evo_harness.cli import FAKE_SCRIPT
from evo_harness.install_hooks import (
    HOOK_SCRIPT,
    install_all,
    install_claude_project_hooks,
)


def test_hook_script_event_mapping(tmp_path):
    sf = tmp_path / "deep" / "state.json"
    env = {
        **os.environ,
        "EVO_RUN": "r1", "EVO_UNIT": "u1", "EVO_STATE_FILE": str(sf),
    }
    for payload, want in (
        ('{"hook_event_name": "UserPromptSubmit"}', "working"),
        ('{"hook_event_name": "PreToolUse"}', "working"),
        ('{"hook_event_name": "Stop"}', "idle"),
        ('{"hook_event_name": "SessionStart"}', "idle"),
        # claude 的 Notification 是「回合结束等输入」提示 → idle（smoke-9 实证）
        ('{"hook_event_name": "Notification"}', "idle"),
        ('{"hook_event_name": "PermissionRequest"}', "blocked"),
    ):
        subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=payload, text=True, env=env, check=True,
        )
        assert json.loads(sf.read_text(encoding="utf-8"))["state"] == want


def test_hook_script_silent_without_env(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input='{"hook_event_name": "Stop"}', text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("EVO_")},
        check=True,
    )
    assert proc.returncode == 0
    assert not list(tmp_path.glob("*.json"))


def test_hook_script_unknown_event_records_unknown(tmp_path):
    sf = tmp_path / "deep" / "state.json"
    env = {
        **os.environ,
        "EVO_RUN": "r1", "EVO_UNIT": "u1", "EVO_STATE_FILE": str(sf),
    }
    subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input='{"hook_event_name": "Frobnicate"}', text=True, env=env, check=True,
    )
    data = json.loads(sf.read_text(encoding="utf-8"))
    assert data["state"] == "unknown"
    assert data["event"] == "frobnicate"


def test_hook_script_done_derives_idle_unseen_and_working_resets(tmp_path):
    """五态里的 done 是展示派生态：hook 落 idle，worker.json 标 unseen。"""
    d = tmp_path / "deep"
    d.mkdir(parents=True)
    sf = d / "state.json"
    wf = d / "worker.json"
    wf.write_text(json.dumps({"agent": "claude", "pane": "%1"}), encoding="utf-8")
    env = {
        **os.environ,
        "EVO_RUN": "r1", "EVO_UNIT": "worker-claude",
        "EVO_STATE_FILE": str(sf),
    }

    subprocess.run([sys.executable, str(HOOK_SCRIPT), "done"], env=env, check=True)
    assert json.loads(sf.read_text(encoding="utf-8"))["state"] == "idle"
    worker = json.loads(wf.read_text(encoding="utf-8"))
    assert worker["state"] == "idle"
    assert worker["seen"] is False

    subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input='{"hook_event_name": "PreToolUse"}', text=True, env=env, check=True,
    )
    worker = json.loads(wf.read_text(encoding="utf-8"))
    assert worker["state"] == "working"
    assert worker["seen"] is True


def test_display_state_derives_done_from_unseen_idle():
    """monitor 卡片五态：done=idle+unseen，unknown 不装懂。"""
    assert monitor._display_state("idle", {"seen": False}, "", "claude") == "done"
    assert monitor._display_state("idle", {"seen": True}, "", "claude") == "idle"
    assert monitor._display_state("working", {}, "", "claude") == "working"
    assert monitor._display_state("blocked", {}, "", "claude") == "blocked"
    assert monitor._display_state("unknown", {}, "", "claude") == "unknown"
    # 缺 hook 状态：有领活按 working，真 agent 无任务按 unknown，fake 按 idle
    assert monitor._display_state(None, {}, "t1", "claude") == "working"
    assert monitor._display_state(None, {}, "", "claude") == "unknown"
    assert monitor._display_state(None, {"agent": "fake"}, "", "fake") == "idle"


def test_monitor_five_state_visuals_cover_all():
    assert set(monitor.STATE_STYLE) == {
        "idle", "working", "blocked", "done", "unknown"
    }
    assert set(monitor.STATE_DOT) == {
        "idle", "working", "blocked", "done", "unknown"
    }


def test_fake_agent_writes_lifecycle_state(tmp_path):
    """fake agent 没有 hook，自己把 working→idle 写回状态通道。"""
    d = tmp_path / "fake"
    d.mkdir(parents=True)
    sf = d / "state.json"
    wf = d / "worker.json"
    wf.write_text(json.dumps({"agent": "fake", "pane": "%1"}), encoding="utf-8")
    env = {
        **os.environ,
        "EVO_RUN": "r1", "EVO_UNIT": "worker-fake-0",
        "EVO_STATE_FILE": str(sf),
    }
    subprocess.run(
        [sys.executable, str(FAKE_SCRIPT)],
        input='FAKE {"sleep": 0, "write": {}}', text=True, env=env,
        timeout=15, check=True,
    )
    assert json.loads(sf.read_text(encoding="utf-8"))["state"] == "idle"
    worker = json.loads(wf.read_text(encoding="utf-8"))
    assert worker["state"] == "idle"
    assert worker["seen"] is False


def test_install_hooks_schema_and_idempotent(tmp_path):
    p1 = install_claude_project_hooks(tmp_path)
    data = json.loads(p1.read_text(encoding="utf-8"))
    entry = data["hooks"]["UserPromptSubmit"][0]
    assert entry["matcher"] == "*"
    hook = entry["hooks"][0]
    # schema 三件套·缺 type 会弹 Settings Error 模态框（实证坑）
    assert hook["type"] == "command"
    # 跨平台：当前解释器绝对路径（Windows 无 python3，sys.executable 三端可用）
    assert hook["command"].startswith(f'"{sys.executable}"')
    n_events = len(data["hooks"])

    install_claude_project_hooks(tmp_path)  # 再装一次
    data2 = json.loads(p1.read_text(encoding="utf-8"))
    assert len(data2["hooks"]["UserPromptSubmit"]) == 1  # 幂等：不重复
    assert len(data2["hooks"]) == n_events
    assert (tmp_path / ".claude" / "settings.json.bak").exists()  # 备份


def test_dialogs_cover_three_agents():
    for agent in ("codex", "kimi", "claude"):
        assert agent in DIALOGS
        for kw, keys in DIALOGS[agent]:
            assert kw and keys
    # kimi 的坑：默认高亮 Don't trust，必须先 Up
    kimi = dict(DIALOGS["kimi"])
    assert any("don't trust" in k for k in kimi)


def test_install_all_default_never_touches_global(tmp_path, monkeypatch):
    """opt-in 边界（codex r1 must-fix）：默认只装项目级 claude hook，
    绝不写 ~/.codex / ~/.kimi-code 全局配置。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    installed = install_all(tmp_path / "proj")
    assert list(installed) == ["claude"]
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".kimi-code").exists()


def test_install_all_opt_in_writes_global(tmp_path, monkeypatch):
    """显式授权后才写全局 hook（幂等 + 备份）。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    installed = install_all(tmp_path / "proj", include_global=True)
    assert set(installed) == {"claude", "codex", "kimi"}
    assert "hooks" in (tmp_path / ".codex" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert "[[hooks]]" in (tmp_path / ".kimi-code" / "config.toml").read_text(
        encoding="utf-8"
    )


def test_codex_hooks_session_end_timeout_clamped(tmp_path, monkeypatch):
    """SessionEnd hook 写 3s（codex v0.148.0 clamp 上限，写 10 会告警
    "clamping SessionEnd hook timeout to 3s"），其余事件 10s。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from evo_harness.install_hooks import install_codex_hooks

    install_codex_hooks()
    text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    m = re.search(r"\[\[hooks\.SessionEnd\]\].*?timeout = (\d+)", text, re.S)
    assert m and m.group(1) == "3"
    m = re.search(r"\[\[hooks\.SessionStart\]\].*?timeout = (\d+)", text, re.S)
    assert m and m.group(1) == "10"
