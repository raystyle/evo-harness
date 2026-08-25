#!/usr/bin/env python3
"""evo-harness fake agent：确定性 E2E 的假 agent。

跑在 pane 里，从 stdin 逐行读指令；支持两种行：
  FAKE {"sleep": 2, "write": {"<路径>": <json 对象>}, "mkdir": ["<目录>"]}
      -> 睡 sleep 秒后把每个对象写成 JSON 文件（目录自动建）
  其它文本 -> 忽略（echo 回显便于 capture 观察）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def _write_state(state: str) -> None:
    """fake agent 没有 hook，自己把生命周期写回状态通道（monitor 一致率）。"""
    state_file = os.environ.get("EVO_STATE_FILE")
    if not state_file:
        return
    target = Path(state_file)
    prev = _load_json(target).get("state")
    ts = round(time.time(), 3)
    record = {
        "state": state,
        "event": "fake-agent",
        "unit": os.environ.get("EVO_UNIT", ""),
        "run": os.environ.get("EVO_RUN", ""),
        "ts": ts,
    }
    _atomic_json(target, record)
    worker_file = target.with_name("worker.json")
    if worker_file.exists():
        worker = _load_json(worker_file)
        seen = True
        if state == "idle":
            seen = False if prev != "idle" else bool(worker.get("seen", True))
        worker["state"] = state
        worker["seen"] = seen
        worker["ts"] = ts
        _atomic_json(worker_file, worker)


def main() -> int:
    print("fake-agent ready", flush=True)
    for line in sys.stdin:
        text = line.rstrip("\n")
        if " FILE " in text[:80]:  # 兼容 [task-id] FILE <path> 前缀
            text = text[text.index(" FILE ") + 1:]
        if text.startswith("FILE "):
            # 任务文件模式：从文件读 FAKE 指令（提示词走文件，drive 只发短行）
            try:
                content = Path(text[5:]).read_text(encoding="utf-8").strip()
            except OSError as exc:
                print(f"[fake-error] read task file: {exc}", flush=True)
                continue
            text = content if content.startswith("FAKE ") else "FAKE " + content
        if not text.startswith("FAKE "):
            print(f"[fake-echo] {text[:80]}", flush=True)
            continue
        try:
            spec = json.loads(text[5:])
        except json.JSONDecodeError as exc:
            print(f"[fake-error] bad directive: {exc}", flush=True)
            continue
        _write_state("working")
        time.sleep(float(spec.get("sleep", 1.0)))
        for d in spec.get("mkdir", []):
            Path(d).mkdir(parents=True, exist_ok=True)
        for path, payload in spec.get("write", {}).items():
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[fake-wrote] {path}", flush=True)
        for path, text in spec.get("write_text", {}).items():
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(text), encoding="utf-8")
            print(f"[fake-wrote-text] {path}", flush=True)
        _write_state("idle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
