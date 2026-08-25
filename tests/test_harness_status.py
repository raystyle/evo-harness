"""evo_harness status 前置校验回归（claude r1 must-fix #2）。

旧实现：status 对不存在的 run-id 没有守卫，FileBus 构造副作用会 mkdir +
写全新 run.json，输出伪造的 stage=IDLE status=running，轮询方永远等不到终态。
"""

import json
import time
from pathlib import Path

from evo_harness.cli import main


def _make_run(shared: Path, run_id: str) -> Path:
    run_dir = shared / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({
            "run_id": run_id, "stage": "EXECUTE", "status": "running",
            "created_at": time.time(), "history": [],
        }),
        encoding="utf-8",
    )
    return run_dir


def test_status_unknown_run_id_fails_without_creating_dir(tmp_path, monkeypatch):
    """不存在的 run-id：报错返回 1，且不能空建目录/伪造 run.json。"""
    monkeypatch.chdir(tmp_path)
    shared = tmp_path / "shared"
    _make_run(shared, "run-real")
    rc = main(["status", "--shared", str(shared), "--run-id", "run-ghost"])
    assert rc == 1
    assert not (shared / "run-ghost").exists()
    # 真 run 毫发无损
    assert json.loads(
        (shared / "run-real" / "run.json").read_text(encoding="utf-8")
    )["status"] == "running"


def test_status_unknown_run_id_json_mode_also_guarded(tmp_path, monkeypatch):
    """--json 轮询姿势同样被守卫（agent 轮询主路径）。"""
    monkeypatch.chdir(tmp_path)
    shared = tmp_path / "shared"
    rc = main(["status", "--shared", str(shared), "--run-id", "run-ghost",
               "--json"])
    assert rc == 1
    assert not (shared / "run-ghost").exists()


def test_status_existing_run_id_ok(tmp_path, monkeypatch, capsys):
    """真实 run：正常输出快照，返回 0。"""
    monkeypatch.chdir(tmp_path)
    shared = tmp_path / "shared"
    _make_run(shared, "run-real")
    rc = main(["status", "--shared", str(shared), "--run-id", "run-real"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run=run-real" in out and "status=running" in out
