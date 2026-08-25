"""ExecutionUnit：L4 执行单元的数据模型（1 pane [+ 1 worktree] = 1 unit）。

对应 references/harness.md §2 核心对象模型里的最小 JSON 结构；
(反)序列化走 to_dict/from_dict，落盘在 shared/units/<unit_id>/unit.json。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# unit 生命周期状态（控制面唯一有权改写）
STATUS = ("pending", "running", "done", "failed", "retry", "aborted")


@dataclass
class TaskSpec:
    title: str
    scope: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "scope": self.scope,
            "success_criteria": self.success_criteria,
            "depends_on": self.depends_on,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSpec":
        return cls(
            title=data["title"],
            scope=list(data.get("scope", [])),
            success_criteria=list(data.get("success_criteria", [])),
            depends_on=list(data.get("depends_on", [])),
        )


@dataclass
class Isolation:
    """unit 的物理隔离：pane 寻址 + 可选 worktree。"""

    session: str = ""
    window: str = ""
    pane: str = ""                # 如 "%12"（pane id，定位用）
    worktree_path: str = ""       # 编程任务才有
    worktree_branch: str = ""

    def to_dict(self) -> dict:
        return {
            "tmux": {
                "session": self.session,
                "window": self.window,
                "pane": self.pane,
            },
            "worktree": {
                "path": self.worktree_path,
                "branch": self.worktree_branch,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Isolation":
        tm = data.get("tmux", {})
        wt = data.get("worktree", {})
        return cls(
            session=tm.get("session", ""),
            window=tm.get("window", ""),
            pane=tm.get("pane", ""),
            worktree_path=wt.get("path", ""),
            worktree_branch=wt.get("branch", ""),
        )


@dataclass
class ExecutionUnit:
    unit_id: str
    run_id: str
    stage: str                    # explore/research/plan/execute/review/merge
    role: str                     # explorer/researcher/planner/executor/reviewer/...
    agent: str                    # codex/kimi/claude/fake
    task_spec: TaskSpec
    isolation: Isolation = field(default_factory=Isolation)
    context_slice: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    status: str = "pending"
    retries: int = 0
    max_retries: int = 3
    artifact_dir: str = ""        # shared/units/<unit_id>/
    result_path: str = ""         # 完成信号：units/<id>/result.json（review 为 verdict.json）

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "role": self.role,
            "agent": self.agent,
            "task_spec": self.task_spec.to_dict(),
            "isolation": self.isolation.to_dict(),
            "context_slice": self.context_slice,
            "tools": self.tools,
            "status": self.status,
            "retries": self.retries,
            "budget": {"max_retries": self.max_retries},
            "artifact_path": self.artifact_dir,
            "result_path": self.result_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionUnit":
        return cls(
            unit_id=data["unit_id"],
            run_id=data["run_id"],
            stage=data["stage"],
            role=data["role"],
            agent=data.get("agent", "claude"),
            task_spec=TaskSpec.from_dict(data.get("task_spec", {"title": data["unit_id"]})),
            isolation=Isolation.from_dict(data.get("isolation", {})),
            context_slice=list(data.get("context_slice", [])),
            tools=list(data.get("tools", [])),
            status=data.get("status", "pending"),
            retries=int(data.get("retries", 0)),
            max_retries=int(data.get("budget", {}).get("max_retries", 3)),
            artifact_dir=data.get("artifact_path", ""),
            result_path=data.get("result_path", ""),
        )
