"""P8.4 monitor 自动落位回归：HERDR_ENV=1 自动 split right + run monitor。

herdr 0.8.2 实测契约（r10 本机 herdr pane 内验证）：
- pane split --current --direction right --no-focus --cwd → stdout JSON，
  新 pane 在 result.pane.pane_id；
- pane wait-output --regex '<提示符>' <pane_id> 等 shell 就绪；
- pane run <pane_id> <cmd...> 原子投命令。
全部 fake（monkeypatch shutil.which / subprocess.run / Popen），零真分屏。
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_harness import cli

SPLIT_JSON = json.dumps({
    "id": "cli:pane:split",
    "result": {"pane": {"pane_id": "w4:pZ"}, "type": "pane_info"},
})


@pytest.fixture(autouse=True)
def _herdr_env_clean(monkeypatch):
    """门控变量不吃外环境（rc-r1 must-fix）：worker pane 会继承
    EVO_HERDR_MONITOR_PLACED=1（run --detach 注入 → rmux server → pane），
    fixer 在池内跑 pytest 当门禁时 5 例假败、空烧一轮修复。逐例清理，
    需要「已落位」语义的用例（防双开）在测试体内自行 setenv。"""
    monkeypatch.delenv("EVO_HERDR_MONITOR_PLACED", raising=False)
    monkeypatch.delenv("HERDR_ENV", raising=False)


@pytest.fixture(autouse=True)
def _herdr_fake_ok(monkeypatch):
    """mock 路径放行（真 herdr 护栏的测试侧开关）。"""
    monkeypatch.setenv("EVO_HERDR_TEST_FAKE", "1")


def _mk_run(calls, split_rc=0, split_out=SPLIT_JSON, run_rc=0, wait_exc=None):
    """subprocess.run 替身：按子命令分发脚本化结果，全量记录 argv。"""

    def fake(argv, **kw):
        calls.append(tuple(argv))
        sub = argv[2] if argv[1] == "pane" else argv[0]
        if sub == "split":
            return SimpleNamespace(returncode=split_rc,
                                   stdout=split_out, stderr="boom")
        if sub == "wait-output":
            if wait_exc:
                raise wait_exc
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if sub == "run":
            return SimpleNamespace(returncode=run_rc, stdout="", stderr="boom")
        raise AssertionError(f"unexpected argv: {argv}")

    return fake


# ---------------------------------------------------------- _place 单元 ----

def test_place_happy_path_sequence(monkeypatch, capsys, tmp_path):
    """三步次序：split(--no-focus --cwd) → wait-output(提示符) → run(带 pane_id)。"""
    calls = []
    monkeypatch.setattr("shutil.which", lambda n: "/fake/herdr")
    monkeypatch.setattr(cli.subprocess, "run", _mk_run(calls))
    assert cli._place_herdr_monitor("run-x", tmp_path) == "w4:pZ"
    split, wait, run = calls
    assert split[1:5] == ("pane", "split", "--current", "--direction")
    assert "right" in split and "--no-focus" in split and "--cwd" in split
    assert wait[1:3] == ("pane", "wait-output") and wait[3] == "--regex"
    assert wait[-1] == "w4:pZ"  # split 输出的新 pane id
    assert run[1:4] == ("pane", "run", "w4:pZ")
    tail = run[4:]
    # monitor 命令：本进程解释器直跑，不依赖新 pane 的 PATH/venv
    assert tail[0] == shlex.quote(sys.executable)
    assert tail[1:4] == ("-m", "evo_harness.cli", "monitor")
    assert tail[tail.index("--shared") + 1] == str(tmp_path)
    assert tail[-2:] == ("--run-id", "run-x")
    assert "monitor 已落位右窗格 w4:pZ" in capsys.readouterr().err
    inv = json.loads(
        (tmp_path / "run-x" / "console.json").read_text(encoding="utf-8"))
    assert inv == {"monitor": {"plane": "herdr", "pane": "w4:pZ"}}


def test_place_skips_when_already_placed(monkeypatch):
    """--detach 自守护子进程（EVO_HERDR_MONITOR_PLACED=1）：跳过防双开。"""
    calls = []
    monkeypatch.setenv("EVO_HERDR_MONITOR_PLACED", "1")
    monkeypatch.setattr("shutil.which", lambda n: "/fake/herdr")
    monkeypatch.setattr(cli.subprocess, "run", _mk_run(calls))
    assert cli._place_herdr_monitor("run-x", Path(".")) is None
    assert calls == []


def test_place_herdr_cli_missing(monkeypatch, capsys):
    """HERDR_ENV=1 但无 herdr 二进制：告警跳过，零 subprocess。"""
    calls = []
    monkeypatch.setattr("shutil.which", lambda n: None)
    monkeypatch.setattr(cli.subprocess, "run", _mk_run(calls))
    assert cli._place_herdr_monitor("run-x", Path(".")) is None
    assert calls == []
    assert "跳过" in capsys.readouterr().err


def test_place_split_fails(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("shutil.which", lambda n: "/fake/herdr")
    monkeypatch.setattr(cli.subprocess, "run", _mk_run(calls, split_rc=1))
    assert cli._place_herdr_monitor("run-x", Path(".")) is None
    assert len(calls) == 1  # split 失败即止，不 wait 不 run
    assert "pane split 失败" in capsys.readouterr().err


def test_place_run_fails(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("shutil.which", lambda n: "/fake/herdr")
    monkeypatch.setattr(cli.subprocess, "run", _mk_run(calls, run_rc=1))
    assert cli._place_herdr_monitor("run-x", Path(".")) is None
    assert len(calls) == 3
    assert "pane run 失败" in capsys.readouterr().err


def test_place_wait_timeout_is_nonfatal(monkeypatch, tmp_path):
    """wait-output 超时（异壳/慢启动）不阻断：照投 monitor。"""
    calls = []
    monkeypatch.setattr("shutil.which", lambda n: "/fake/herdr")
    monkeypatch.setattr(
        cli.subprocess, "run",
        _mk_run(calls, wait_exc=subprocess.TimeoutExpired(cmd="x", timeout=15)),
    )
    assert cli._place_herdr_monitor("run-x", tmp_path) == "w4:pZ"
    assert len(calls) == 3


# ------------------------------------------------------------ main 集成 ----

@pytest.fixture
def placed(monkeypatch):
    """记录 _place_herdr_monitor 调用；_run_flow 换成空转。"""
    seen = []

    async def _noop_flow(args):
        return 0

    monkeypatch.setattr("shutil.which", lambda n: "/fake/bin/" + n)
    monkeypatch.setattr(cli, "_place_herdr_monitor",
                        lambda rid, shared: seen.append((rid, shared)) or "w4:pM")
    monkeypatch.setattr(cli, "_spawn_notifyd", lambda rid, shared: True)
    monkeypatch.setattr(cli, "_spawn_flow_in_rmux_session",
                           lambda args, rid: False)
    monkeypatch.setattr(cli, "_run_flow", _noop_flow)
    return seen


def test_main_auto_places_inside_herdr_env(placed, monkeypatch):
    """HERDR_ENV=1（herdr 起 pane 自动注入）：auto 缺省即落位，run_id 先锚定。"""
    monkeypatch.setenv("HERDR_ENV", "1")
    assert cli.main(["run", "目标", "--shared", "s"]) == 0
    [(rid, shared)] = placed
    assert rid and rid.startswith("run-")  # 时间戳 id 只生成一次并锚定
    assert str(shared) == "s"


def test_main_silent_skip_outside_herdr(placed, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    assert cli.main(["run", "目标", "--shared", "s"]) == 0
    assert placed == []


def test_main_pane_right_forces_without_env(placed, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    assert cli.main(["run", "目标", "--shared", "s", "--pane", "right"]) == 0
    assert len(placed) == 1


def test_main_pane_none_disables_even_in_herdr(placed, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    assert cli.main(["run", "目标", "--shared", "s", "--pane", "none"]) == 0
    assert placed == []


def test_main_anchors_explicit_run_id(placed, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    assert cli.main(["review-cycle", "目标", "--shared", "s",
                     "--run-id", "xyz"]) == 0
    assert placed == [("xyz", Path("s"))]


def test_main_detach_child_env_guard(placed, monkeypatch, tmp_path):
    """--detach：父进程先落位，自守护子进程注入 PLACED=1 防双开。"""
    monkeypatch.setenv("HERDR_ENV", "1")
    captured = {}

    class _P:
        pid = 4242

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env")
        return _P()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    assert cli.main(["run", "目标", "--detach", "--shared",
                     str(tmp_path)]) == 0
    assert len(placed) == 1  # 父进程已落位
    assert captured["env"]["EVO_HERDR_MONITOR_PLACED"] == "1"  # 子进程防双开
