# -*- coding: utf-8 -*-
"""单实例锁：同一时间只允许一个机器人在跑。

桌面上那个快捷方式很容易被双击两下，两个进程同时监听微信的后果是同一条消息
被回复两遍。所以正式启动前先抢一把锁，抢不到就说明已经有一个在跑了。

实现用 ``msvcrt.locking`` 锁住锁文件的第一个字节，PID 写在它后面 —— Windows 的
文件锁会随进程结束自动释放（哪怕是强制结束、断电重启），所以上次崩溃留下的锁文
件不会把人挡在门外，也不需要额外清理残留文件。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("dsbot")


class InstanceLock:
    """文件锁版本的单实例互斥。锁文件里只记自己的 PID，方便打印人话。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        """抢到返回 True；已经有别的进程占着返回 False。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o666)
            fh = os.fdopen(fd, "rb+")
        except OSError as exc:
            log.warning("单实例锁文件打不开（%s），本次跳过重复启动检查", exc)
            return True
        try:
            import msvcrt
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")          # 锁文件至少要有 1 个字节才能上锁
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        except Exception as exc:            # msvcrt 不可用（非 Windows）时不拦人
            log.warning("单实例锁不可用（%s），本次跳过重复启动检查", exc)
            self._fh = fh
            return True
        # 锁只压在第 1 个字节上，PID 写在它后面：这样别的进程（包括自己）要读
        # PID，只要跳过第 1 个字节就不会撞上锁。落在锁里读整个文件会直接报错。
        fh.seek(1)
        fh.truncate(1)
        fh.write(b"%d\n" % os.getpid())
        fh.flush()
        self._fh = fh
        return True

    def holder_pid(self) -> str:
        """读锁文件里记的 PID，只是为了打印一句人话。"""
        if self._fh is not None:
            try:
                self._fh.seek(1)
                return self._fh.read().decode("ascii", "replace").strip() or "未知"
            except Exception:
                pass
        try:
            with open(self.path, "rb") as fh:
                fh.seek(1)
                return fh.read().decode("ascii", "replace").strip() or "未知"
        except OSError:
            return "未知"

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate(0)        # 留个空文件，表示现在没人占着
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        self._fh = None
