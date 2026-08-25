"""evo_harness abort 跨进程回归（round1 review must-fix #1）。

旧实现三宗罪：
- abort 未传 --run-id 时生成新时间戳 id（FileBus 构造还给它空建一套目录）
- kill_session 只杀 self.session（abort 是独立进程，session 必为 None → 完全不生效）
- run --detach 的守护 pid 无人停，终态会被活进程覆盖回 running
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from evo_harness.cli import main


def _make_run(shared: Path, run_id: str, pid: int | None = None) -> Path:
    run_dir = shared / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({
            "run_id": run_id, "stage": "EXECUTE", "status": "running",
            "created_at": time.time(), "history": [],
        }),
        encoding="utf-8",
    )
    if pid is not None:
        (run_dir / "run.pid").write_text(str(pid), encoding="utf-8")
    return run_dir


def _status(run_dir: Path) -> str:
    return json.loads(
        (run_dir / "run.json").read_text(encoding="utf-8")
    )["status"]


def test_abort_kills_detached_pid_and_sets_terminal(tmp_path, monkeypatch):
    """run.pid 里的守护进程（独立会话）必须被整组停掉，run.json 置终态。"""
    monkeypatch.chdir(tmp_path)
    shared = tmp_path / "shared"
    proc = subprocess.Popen(  # 与 run --detach 同款：start_new_session
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    try:
        run_dir = _make_run(shared, "run-x", pid=proc.pid)
        assert main(["abort", "--shared", str(shared), "--run-id", "run-x"]) == 0
        proc.wait(timeout=10)  # SIGTERM 生效则退出；不生效 -> TimeoutExpired 红
        assert _status(run_dir) == "aborted"
    finally:
        proc.kill()
        proc.wait()


def test_abort_without_run_id_targets_latest_run(tmp_path, monkeypatch):
    """缺省 --run-id 锚定最新 run，而不是新生成一个时间戳 id。"""
    monkeypatch.chdir(tmp_path)
    shared = tmp_path / "shared"
    old = _make_run(shared, "run-20000101-000000")
    new = _make_run(shared, "run-20000102-000000")
    os.utime(old / "run.json", (1_000_000_000, 1_000_000_000))
    os.utime(new / "run.json", (1_000_000_100, 1_000_000_100))

    assert main(["abort", "--shared", str(shared)]) == 0
    assert _status(new) == "aborted"
    assert _status(old) == "running"  # 不误伤旧 run
    # 旧实现的标志性垃圾：给新时间戳 id 空建一套 run 目录
    assert {p.name for p in shared.iterdir()} == {
        "run-20000101-000000", "run-20000102-000000",
    }


def test_abort_unknown_run_id_fails_without_creating_dir(tmp_path, monkeypatch):
    """不存在的 run-id：报错退出，且不能因 FileBus 构造空建目录。"""
    monkeypatch.chdir(tmp_path)
    shared = tmp_path / "shared"
    _make_run(shared, "run-real")
    rc = main(["abort", "--shared", str(shared), "--run-id", "run-ghost"])
    assert rc == 1
    assert not (shared / "run-ghost").exists()
    assert _status(shared / "run-real") == "running"  # 真 run 毫发无损


def test_abort_no_runs_at_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["abort", "--shared", str(tmp_path / "shared")]) == 1
