"""异步纪律 lint（AST 级）：evo_harness 源码禁止 time.sleep 调用。

只检测真实调用节点，注释/文档里的字样不误报。
允许的等待：asyncio.sleep（协作式让位）、rmux 原生 wait-pane（线程池阻塞）、
watchdog 事件（events.py）。fake_agent.py 是独立进程测试夹具（模拟延迟），不辖。
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _time_sleep_calls(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sleep"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "time"
        ):
            hits.append(node.lineno)
    return hits


def test_no_time_sleep_in_sources():
    offenders = []
    # 豁免：monitor.py 是独立渲染进程（TUI 帧率节流）；notifyd.py 是独立
    # 守护进程（自有 pane，2s 轮询决策目录）·都不在编排事件循环内
    exempt = ("__pycache__",)
    for py in sorted(SRC.rglob("*.py")):
        if (any(part in py.parts for part in exempt)
                or py.name in ("monitor.py", "notifyd.py")
                or py.parent.name == "scripts"):  # 包内 scripts=夹具（fake_agent 等）
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for line in _time_sleep_calls(tree):
            offenders.append(f"{py.relative_to(SRC)}:{line}")
    assert not offenders, (
        "evo_harness 源码出现 time.sleep 调用（阻塞事件循环，P7 禁项）："
        + "; ".join(offenders)
    )


def test_stage_and_driver_are_async():
    for mod in ("driver.py", "stages.py"):
        text = (SRC / "evo_harness" / mod).read_text(encoding="utf-8")
        assert "async def" in text, f"{mod} 应为 async/await 风格"
