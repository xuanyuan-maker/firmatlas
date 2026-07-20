"""同一数据目录的进程锁。

恢复遗留状态前必须确认没有另一个 FirmAtlas 进程仍在工作，否则会把正常的
``running`` / ``downloading`` 任务误判为上次崩溃遗留。flock 由操作系统持有，
进程正常退出或崩溃时都会自动释放；锁文件本身无需删除。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO

from firmatlas.domain.errors import ProcessLockError


class DataDirectoryLock:
    """对一个 FirmAtlas 数据目录持有非阻塞独占锁。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._path = data_dir / ".firmatlas.lock"
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ProcessLockError(
                f"数据目录 {self._data_dir} 正被另一个 FirmAtlas 进程使用，请稍后重试。"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> DataDirectoryLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()
