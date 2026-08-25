#!/usr/bin/env python3
"""evo-harness agent 状态 hook（win-rmux + herdr 双源移植，Linux/Python 版）。

两种调用形态（herdr 式位置参数优先）：
  agent_state_hook.py working        # 四态直传：working/idle/blocked/unknown
  agent_state_hook.py < stdin JSON   # hook_event_name 事件映射

通道权威是 herdr 底层的四态（idle/working/blocked/unknown）；五态里的
`done` 是展示派生态，不落 state.json，由 monitor 依据 state=idle +
同目录 worker.json 的 seen=false 派生（见 docs/harness-herdr.md P8.3）。
直传参数 `done` 作为语法糖：state 落 idle、worker.json 标 seen=false。

写 EVO_STATE_FILE 指向的文件：{"state": "idle|working|blocked|unknown", ...}；
若同目录存在 worker.json，同步写回 state/seen/ts（保留 agent/pane 字段）。
身份由 spawn 注入的 EVO_RUN/EVO_UNIT 环境变量携带（herdr 的
HERDR_PANE_ID 同思路；我们按 unit 落文件而非 socket 上报）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE_MAP = {
    "session": "idle", "idle": "idle", "sessionstart": "idle",
    "stop": "idle", "interrupt": "idle",
    "userpromptsubmit": "working", "pretooluse": "working",
    "posttooluse": "working", "posttoolusefailure": "working",
    "subagentstart": "working", "subagentstop": "working",
    "precompact": "working",
    "permissionrequest": "blocked", "permissionresult": "working",
    "notification": "idle",  # claude 回合结束提示（smoke-9 实证非 blocked）
    "working": "working", "blocked": "blocked", "unknown": "unknown",
}


def _atomic_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, target)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _seen(prev_state: str | None, state: str, current_seen: bool) -> bool:
    """herdr 的 seen/done 派生：非 idle 即 seen；idle 完成沿未 seen。"""
    if state != "idle":
        return True
    if prev_state != "idle":
        return False
    return bool(current_seen)


def _write_state(state_file: Path, state: str, event: str,
                 force_unseen: bool = False) -> None:
    prev = _load_json(state_file).get("state")
    ts = round(time.time(), 3)
    record = {
        "state": state,
        "event": event,
        "unit": os.environ.get("EVO_UNIT", ""),
        "run": os.environ.get("EVO_RUN", ""),
        "ts": ts,
    }
    _atomic_json(state_file, record)

    worker_file = state_file.with_name("worker.json")
    if not worker_file.exists():
        return
    worker = _load_json(worker_file)
    worker["state"] = state
    worker["seen"] = False if force_unseen else _seen(prev, state, worker.get("seen", True))
    worker["ts"] = ts
    _atomic_json(worker_file, worker)


def main() -> int:
    state_file = os.environ.get("EVO_STATE_FILE")
    if not state_file:
        return 0  # 非 harness 起的会话，静默退出
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    event = ""
    if action:
        event = action.lower()
    else:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            return 0
        event = str(payload.get("hook_event_name", "")).lower()
    force_unseen = event == "done"
    state = "idle" if force_unseen else STATE_MAP.get(event, "unknown")
    _write_state(Path(state_file), state, event, force_unseen=force_unseen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
