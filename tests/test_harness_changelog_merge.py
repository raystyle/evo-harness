"""CHANGELOG-only 合并冲突自动解回归（p8-close-r1 实证）。"""

import subprocess as sp
from pathlib import Path

from evo_harness.worktrees import WorktreeRegistry


def _mk_repo(tmp_path) -> tuple[Path, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    def g(*a):
        sp.run(["git", *a], cwd=repo, check=True, capture_output=True)
    g("init", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- 基线条目\n", encoding="utf-8")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-m", "base")

    class _Bus:
        root = tmp_path / "shared" / "r1"
        run_id = "r1"
    _Bus.root.mkdir(parents=True)
    reg = WorktreeRegistry(repo, _Bus)
    # unit 分支：改 CHANGELOG（与 main 后续改动同区冲突）
    g("worktree", "add", "-b", "t/u1", str(tmp_path / "wt" / "u1"))
    wt = tmp_path / "wt" / "u1"
    (wt / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- 分支条目 A\n- 分支条目 B\n",
        encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "unit"], cwd=wt, check=True, capture_output=True)
    reg._save({"u1": {"path": str(wt), "branch": "t/u1", "base": "main"}})
    # main 侧同区改动（制造冲突）
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- 主干条目 C\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-m", "main side")
    return repo, reg


def test_changelog_only_conflict_auto_resolved(tmp_path):
    repo, reg = _mk_repo(tmp_path)
    ok, detail = reg.merge_one("u1")
    assert ok is True, detail
    assert "自动并" in detail
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "<<<<<<<" not in text
    # 两边条目都活着（追加共存）
    for needle in ("主干条目 C", "分支条目 A", "分支条目 B"):  # 基线两侧皆覆写
        assert needle in text, needle


def test_other_file_conflict_still_fails(tmp_path):
    repo, reg = _mk_repo(tmp_path)
    # unit 分支同时改 f.txt（制造非 CHANGELOG 冲突）
    wt = tmp_path / "wt" / "u1"
    (wt / "f.txt").write_text("unit\n", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "unit2"], cwd=wt, check=True, capture_output=True)
    (repo / "f.txt").write_text("main\n", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "main2"], cwd=repo, check=True, capture_output=True)
    ok, detail = reg.merge_one("u1")
    assert ok is False
    assert "CONFLICT" in detail or "conflict" in detail.lower()
    # merge 已回滚，无残留标记
    assert "<<<<<<<" not in (repo / "CHANGELOG.md").read_text(encoding="utf-8")
