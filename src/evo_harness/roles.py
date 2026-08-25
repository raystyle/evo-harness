"""L3 角色模板：role -> prompt 渲染（模板可被 skill references/harness-roles/ 覆盖）。

代码里只放**结构**（必含要素与输出契约路径）；完整提示词文本优先从
skills/evo/references/harness-roles/<role>.md 加载（存在则用），否则用内置精简版。
模型只填模板，不拥有控制权。
"""

from __future__ import annotations

from pathlib import Path

# 内置精简模板：{goal}/{title}/{criteria}/{out}/{extra} 占位
BUILTIN: dict[str, str] = {
    "explorer": (
        "你是探索 agent。目标：{goal}\n"
        "只做只读搜索（gh search repos/code/issues），不做深读、不 clone、不下结论。\n"
        "把结果（含 repo、stars、查询词来源）写成 JSON 数组到 {out}。\n"
        "**产物文件名必须逐字使用上面的路径，禁止自创文件名**；多个查询的结果合并进同一个文件。\n"
        "gh 查询报 403/限流时：等 20 秒重试一次，仍失败就写空数组 [] 到 {out} 继续，不许卡住。\n"
        "{extra}"
    ),
    "ranker": (
        "你是打分 agent。读取 {extra} 目录下的全部候选 JSON（可能部分为空数组），"
        "去重、按相关性打分，筛出 5-10 个，每条必须含 clone_url 与一句话理由，"
        "写到 {out}（文件名逐字使用，禁止自创）。"
    ),
    "researcher": (
        "你是研究 agent。目标：{goal}\n"
        "定点解剖 {extra}：clone --depth 1 到本 unit 目录、rg/读 README 与关键模块。\n"
        "「声称」与「代码实证」分开写；每条结论挂证据（文件/行号/commit）。\n"
        "产出 summary.md 到 {out}。禁止改主项目代码。"
    ),
    "synthesizer": (
        "你是综合 agent。读取 {extra} 下所有 summary，fan-in 成一份 synthesis.md 写到 {out}："
        "可复用模式清单 + 风险清单 + 证据链。"
    ),
    "planner": (
        "你是规划 agent。目标：{goal}\n"
        "读 {extra}/synthesis.md，产出三份契约（合法 JSON）：\n"
        "1) {out}/goal_spec.json：目标/非目标/可测试成功标准/验收口径\n"
        "2) {out}/plan.json：steps（含 depends_on、scope）、merge_order（接口→实现→测试→重构）\n"
        "3) {out}/allocations.json：每个并行任务的 unit_id/worktree 分支/scope/成功标准\n"
        "按文件所有权拆任务，不按模糊任务名；共享文件归一个 unit 或先做接口 unit。"
    ),
    "executor": (
        "你是执行 agent。任务：{title}\n"
        "成功标准（逐条对照）：{criteria}\n"
        "只改你的 scope：{extra}\n"
        "完成后：本分支 git commit；写 result.json 到 {out}，"
        "含 diff 摘要/测试结果/越界自检（改了 scope 外哪些文件，没有则空数组）/"
        "ready_for_review=true。失败也写 result.json（ready_for_review=false + 原因）。"
    ),
    "reviewer": (
        "你是复核 agent（与执行者独立）。审查对象：{extra}\n"
        "视角：正确性/越界/测试充分。禁止改业务代码。\n"
        "写 verdict JSON 到 {out}：verdict=pass|revise|reject，"
        "severity=local|architectural，findings 与 required_changes 列表。"
    ),
    "merger": (
        "你是合并 agent。按 {extra} 的 merge_order 逐个 merge 到主分支，"
        "冲突先尝试语义合并并说明理由；完成写 merge/report.json 到 {out}。"
    ),
}

ROLE_ORDER = ["explorer", "ranker", "researcher", "synthesizer",
              "planner", "executor", "reviewer", "merger"]


def _override_dir() -> Path | None:
    # 仓库内模板覆盖：宿主可放 references/harness-roles/<role>.md 定制提示词
    for cand in (
        Path("skills/evo/references/harness-roles"),
        Path("references/harness-roles"),
    ):
        if cand.is_dir():
            return cand
    return None


def render(role: str, **fields) -> str:
    """渲染 role prompt；skill 侧 md 模板（若存在）优先于内置精简版。"""
    if role not in BUILTIN:
        raise KeyError(f"未知 role: {role}（可用: {ROLE_ORDER}）")
    odir = _override_dir()
    template = BUILTIN[role]
    if odir is not None and (odir / f"{role}.md").exists():
        template = (odir / f"{role}.md").read_text(encoding="utf-8", errors="replace")
    # 未提供的占位替换为空串，避免 format KeyError
    class _Safe(dict):
        def __missing__(self, key: str) -> str:  # type: ignore[override]
            return ""

    return template.format_map(_Safe(fields))


def fake_directive(out_path: str, payload, sleep_s: float = 1.0,
                   extra_writes: dict | None = None,
                   text_writes: dict | None = None) -> str:
    """构造 fake agent 的机器指令行（确定性 E2E；fake_agent.py 消费）。

    write=JSON 对象/数组；write_text=原始文本（synthesis.md 这类文本产物）。
    """
    import json

    spec: dict = {"sleep": sleep_s, "write": {out_path: payload}}
    if extra_writes:
        spec["write"].update(extra_writes)
    if text_writes:
        spec["write_text"] = text_writes
    return "FAKE " + json.dumps(spec, ensure_ascii=False)
