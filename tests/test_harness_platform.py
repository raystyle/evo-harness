"""evo_harness 跨平台缺口回归（codex r1 must-fix：Windows/macOS 无
getpgid/killpg、无 /proc、无 python3、无 start_new_session 语义）。

在 POSIX 开发机上用参数化/monkeypatch 覆盖两个平台分支。
"""

import os
import subprocess
import sys
from pathlib import Path

from evo_harness.cli import _detach_popen_kwargs
from evo_harness.driver import _agent_in_pane
from evo_harness.install_hooks import hook_command
from evo_harness.stages import _terminate_pid


def test_terminate_pid_posix_group(tmp_path):
    """POSIX：独立会话的守护进程被整组 SIGTERM。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    try:
        _terminate_pid(proc.pid)
        proc.wait(timeout=10)  # 未终止 -> TimeoutExpired 红
    finally:
        proc.kill()
        proc.wait()


def test_terminate_pid_without_process_group_api(monkeypatch):
    """无 getpgid/killpg 的平台（Windows）：回退单进程 os.kill，不 AttributeError。"""
    monkeypatch.delattr(os, "getpgid")
    monkeypatch.delattr(os, "killpg")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        _terminate_pid(proc.pid)
        proc.wait(timeout=10)
    finally:
        proc.kill()
        proc.wait()


def test_detach_popen_kwargs_by_platform(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert _detach_popen_kwargs() == {"start_new_session": True}
    monkeypatch.setattr(os, "name", "nt")
    kwargs = _detach_popen_kwargs()
    assert "start_new_session" not in kwargs
    assert "creationflags" in kwargs  # Windows：新进程组 + 游离控制台


def test_agent_in_pane_proc_comm(tmp_path):
    """Linux 主路径：/proc/<pid>/comm 命中进程名。"""
    comm = tmp_path / "4242" / "comm"
    comm.parent.mkdir(parents=True)
    comm.write_text("kimi\n", encoding="utf-8")
    assert _agent_in_pane({"pane_pid": 4242}, "kimi", proc_root=tmp_path)
    assert not _agent_in_pane({"pane_pid": 4242}, "codex", proc_root=tmp_path)
    # 有 /proc 但 pid 已退出：不匹配（不回退）
    assert not _agent_in_pane({"pane_pid": 9999}, "kimi", proc_root=tmp_path)


def test_agent_in_pane_fallback_without_proc(tmp_path):
    """macOS/Windows 无 /proc：回退 pane_current_command（tmux 跨平台字段）。"""
    no_proc = tmp_path / "nonexistent"
    info = {"pane_pid": 4242, "pane_current_command": "kimi"}
    assert _agent_in_pane(info, "kimi", proc_root=no_proc)
    assert not _agent_in_pane(info, "codex", proc_root=no_proc)
    assert not _agent_in_pane({"pane_current_command": "kimi"}, "kimi",
                              proc_root=no_proc)  # 无 pid 不匹配


def test_hook_command_uses_current_interpreter():
    """Windows 标准安装通常没有 python3，hook 命令必须用 sys.executable。"""
    cmd = hook_command()
    assert cmd.startswith(f'"{sys.executable}"')
    assert not cmd.startswith("python3")  # 不依赖 PATH 里的 python3 存在


def test_pid_alive_posix():
    """POSIX：kill(pid, 0) 探测，活进程 True，死 pid False。"""
    from evo_harness.cli import _pid_alive

    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(2**22) is False  # 不存在的 pid


def test_pid_alive_windows_branch_never_kills(monkeypatch):
    """Windows 分支（codex r2 must-fix）：os.kill(pid, 0) 在 Windows 上是
    TerminateProcess 杀伤调用，分支必须走 OpenProcess，且绝不调 os.kill。"""
    import ctypes

    from evo_harness.cli import _pid_alive

    monkeypatch.setattr(os, "name", "nt")

    def _boom(pid, sig):
        raise AssertionError("Windows 分支不许调 os.kill（杀伤性）")

    monkeypatch.setattr(os, "kill", _boom)

    class _K32:
        @staticmethod
        def OpenProcess(access, inherit, pid):
            return 1 if pid == 4242 else 0

        @staticmethod
        def CloseHandle(handle):
            return None

    monkeypatch.setattr(
        ctypes, "windll", type("W", (), {"kernel32": _K32}), raising=False
    )
    assert _pid_alive(4242) is True
    assert _pid_alive(9999) is False
