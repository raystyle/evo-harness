"""states.HOOK_STATE_MAP ↔ agent_state_hook.STATE_MAP 对拍（审计 r3：
两份 map 各自漂移无测试锁定）。"""

import ast
import re
from pathlib import Path

import evo_harness.states as st


def _script_map() -> dict:
    src = (Path(st.__file__).parent / "scripts" / "agent_state_hook.py"
           ).read_text(encoding="utf-8")
    m = re.search(r"STATE_MAP[^=]*=\s*\{(.*?)\}", src, re.S)
    return ast.literal_eval("{" + m.group(1) + "}")


def test_hook_state_map_matches_script():
    script = _script_map()
    for k, v in st.HOOK_STATE_MAP.items():
        if k in ("sessionend", "done"):  # 显式分歧点（脚本无这些键）
            continue
        assert script.get(k) == v, f"{k}: states={v} script={script.get(k)}"
    # 未覆盖键回落一致
    assert st.HOOK_STATE_MAP.get("__nope__", "unknown") == "unknown"


def test_states_docstrings_no_false_wiring_claims():
    """三 Protocol 不得再声称「实现方：driver.py」（审计 r3 失实项）。"""
    src = Path(st.__file__).read_text(encoding="utf-8")
    assert "实现方：driver.py" not in src
    assert "实现方：cli.py" not in src
