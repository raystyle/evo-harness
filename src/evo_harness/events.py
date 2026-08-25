"""异步文件事件等待（watchdog.asyncio + asyncio.Queue）：事件驱动，零阻塞轮询。

编排器的 observe/wait 全部走这里，文件出现/变化触发谓词重查；
queue.get 的超时仅用于预算检查与终态兜底。协作式让位用 asyncio.sleep
（让出事件循环），阻塞式 time.sleep 在异步代码里是禁止项（lint 固化）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent


class AsyncFileWatcher:
    """递归监听目录；谓词在每次事件（及初始/超时唤醒）时评估。"""

    def __init__(self, root: Path) -> None:
        from watchdog import events as we
        from watchdog.observers import Observer

        self.root = Path(root)
        # Observer 是线程实现；事件经 thread-safe 的 call_soon_threadsafe 进 asyncio 队列
        loop = asyncio.get_running_loop()
        raw_q: asyncio.Queue[FileSystemEvent | None] = asyncio.Queue()

        class _AsyncHandler(we.FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                loop.call_soon_threadsafe(raw_q.put_nowait, event)

        self._raw_q = raw_q
        self._observer: Any = Observer()
        self._observer.schedule(_AsyncHandler(), str(self.root), recursive=True)
        self._observer.start()

    def close(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)

    async def __aenter__(self) -> "AsyncFileWatcher":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()

    async def wait_until(self, predicate, timeout: float, on_wake=None) -> bool:
        """等待谓词为真或超时。on_wake(事件|None)：每次唤醒回调（预算检查等）。"""
        if predicate():
            return True
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return predicate()
            try:
                event = await asyncio.wait_for(
                    self._raw_q.get(), timeout=min(remaining, 5.0)
                )
            except asyncio.TimeoutError:
                event = None  # 超时唤醒：兜底重查（事件可能被合并）
            if on_wake is not None:
                on_wake(event)
            if predicate():
                return True


async def wait_for_files(root: Path, predicate, timeout: float,
                         on_wake=None) -> bool:
    watcher = AsyncFileWatcher(root)
    try:
        return await watcher.wait_until(predicate, timeout, on_wake=on_wake)
    finally:
        watcher.close()
