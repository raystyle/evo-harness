"""WorktreeRegistry：编程路径的 worktree 生命周期 + 按序 merge。

规则（harness.md §3 Stage 3/5）：
- 1 Execution Unit = 1 pane = 1 git worktree = 1 专用分支
- 禁止两个 unit 共享同一工作目录或同一分支
- merge 按 plan.merge_order（接口→实现→测试→重构）逐个合，主仓集成测试过关才算完
- 过则 worktree remove + 删分支；败则反馈对应 unit 或回规划
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} 失败: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


class WorktreeRegistry:
    def __init__(self, repo_root: Path, bus) -> None:
        self.repo = Path(repo_root).resolve()
        self.bus = bus
        self.index_file = bus.root / "worktrees.json"
        if not self.index_file.exists():
            self.index_file.write_text("{}", encoding="utf-8")

    # ------------------------------------------------------------ 登记表 ----

    def _load(self) -> dict:
        return json.loads(self.index_file.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.index_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def path_for(self, unit_id: str) -> Path:
        """worktree 落在仓库外（兄弟目录），避免污染主仓未跟踪区。"""
        data = self._load()
        if unit_id in data:
            return Path(data[unit_id]["path"])
        wt = self.repo.parent / f"evo-wt-{self.bus.run_id}" / unit_id
        return wt

    def ensure(self, unit_id: str, branch: str) -> Path:
        """幂等创建：同 unit 复用已登记 worktree（revise 重试场景）。"""
        data = self._load()
        if unit_id in data and Path(data[unit_id]["path"], ).exists():
            return Path(data[unit_id]["path"])
        wt = self.path_for(unit_id)
        wt.parent.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            raise WorktreeError(f"worktree 目录已存在但未登记: {wt}")
        base = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        _git(self.repo, "worktree", "add", "-b", branch, str(wt), base)
        data[unit_id] = {
            "path": str(wt), "branch": branch,
            "base": base, "owner": unit_id,
        }
        self._save(data)
        return wt

    # ------------------------------------------------------------- merge ----

    def merge_one(self, unit_id: str, main_branch: str | None = None) -> tuple[bool, str]:
        """把 unit 分支合回主仓当前分支。返回 (成功, 输出/冲突说明)。

        CHANGELOG-only 冲突自动解（p8-close-r1 实证）：并行单元都往
        [Unreleased] 追加，内容冲突但语义同向——每块两边都保留（追加共存），
        程序化替代手工解；其它文件冲突照旧 abort 报告。
        """
        data = self._load()
        entry = data.get(unit_id)
        if entry is None:
            return False, f"{unit_id} 无 worktree 登记"
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--no-edit", entry["branch"]],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            conflicted = _git(
                self.repo, "diff", "--name-only", "--diff-filter=U",
                check=False,
            ).split()
            if conflicted == ["CHANGELOG.md"]:
                resolved = self._resolve_changelog_conflict()
                if resolved:
                    _git(self.repo, "add", "CHANGELOG.md", check=False)
                    c2 = subprocess.run(
                        ["git", "-C", str(self.repo), "commit",
                         "--no-edit"],
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace",
                    )
                    if c2.returncode == 0:
                        return True, ("CHANGELOG 冲突已自动并"
                                      f"（{resolved} 块追加共存）")
            _git(self.repo, "merge", "--abort", check=False)
            return False, proc.stderr.strip() or proc.stdout.strip()
        return True, proc.stdout.strip()

    def _resolve_changelog_conflict(self) -> int:
        """就地解 CHANGELOG.md 冲突标记：每块 HEAD 段 + 分支段都保留。

        返回解掉的块数（0 = 无标记/解析失败）。"""
        p = self.repo / "CHANGELOG.md"
        try:
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return 0
        out: list[str] = []
        blocks = 0
        i, n = 0, len(lines)
        while i < n:
            if lines[i].startswith("<<<<<<<"):
                j = next((k for k in range(i + 1, n)
                          if lines[k].startswith("=======")), None)
                m = next((k for k in range(j + 1 if j else 0, n)
                          if lines[k].startswith(">>>>>>>")), None)
                if j is None or m is None:
                    return 0  # 标记残缺：不硬解
                out.extend(lines[i + 1:j])
                out.extend(lines[j + 1:m])
                blocks += 1
                i = m + 1
            else:
                out.append(lines[i])
                i += 1
        if blocks:
            p.write_text("".join(out), encoding="utf-8")
        return blocks

    def merge_in_order(self, unit_ids: list[str], order: list[str]) -> list[dict]:
        """按 plan.merge_order 逐个 merge；返回逐条报告（unit/ok/detail）。"""
        report = []
        ordered = [u for u in order if u in unit_ids] + [
            u for u in unit_ids if u not in order
        ]
        for uid in ordered:
            ok, detail = self.merge_one(uid)
            report.append({"unit_id": uid, "merged": ok, "detail": detail[:500]})
        return report

    def integration_test(self, command: list[str] | None = None) -> tuple[bool, str]:
        """主仓集成测试（默认 uv run pytest -q；可配任意命令）。"""
        cmd = command or ["uv", "run", "pytest", "-q"]
        proc = subprocess.run(
            cmd, cwd=str(self.repo),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
        return proc.returncode == 0, tail

    # ------------------------------------------------------------ 清理 ----

    def cleanup(self, unit_id: str) -> None:
        data = self._load()
        entry = data.pop(unit_id, None)
        if entry is None:
            return
        wt = Path(entry["path"])
        if wt.exists():
            _git(self.repo, "worktree", "remove", "--force", str(wt), check=False)
        _git(self.repo, "branch", "-D", entry["branch"], check=False)
        self._save(data)

    def cleanup_all(self) -> None:
        for uid in list(self._load()):
            self.cleanup(uid)


def clean_run_worktrees(repo_root: Path, run_id: str) -> list[str]:
    """清指定 run 的全部 worktree 与分支（hard stop 保留现场后的一键清场；
    abort 路径自动调用）。返回清理的 unit 列表。"""
    import subprocess as sp

    marker = f"evo-wt-{run_id}"
    try:
        out = sp.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout
    except sp.CalledProcessError:
        return []  # 非 git 仓（测试/误用）：无可清
    cleaned: list[str] = []
    blocks = out.strip().split("\n\n")
    for blk in blocks:
        lines = blk.splitlines()
        wt_path = next((l.split(" ", 1)[1] for l in lines
                        if l.startswith("worktree ")), "")
        if marker not in wt_path or wt_path == str(repo_root):
            continue
        branch = next((l.split(" ", 1)[1] for l in lines
                       if l.startswith("branch ")), "")
        if branch.startswith("refs/heads/"):  # branch -D 要名字不要全 ref
            branch = branch[len("refs/heads/"):]
        unit = Path(wt_path).name
        sp.run(["git", "worktree", "remove", "--force", wt_path],
               cwd=repo_root, capture_output=True)
        if branch:
            sp.run(["git", "branch", "-D", branch],
                   cwd=repo_root, capture_output=True)
        cleaned.append(unit)
    # 目录壳（可能残留）
    import shutil
    for cand in repo_root.parent.glob(f"evo-wt-{run_id}"):
        if cand.is_dir() and not any(cand.iterdir()):
            shutil.rmtree(cand, ignore_errors=True)
    return cleaned
