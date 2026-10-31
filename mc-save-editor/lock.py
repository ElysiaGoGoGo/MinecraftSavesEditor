"""世界目录占用检测 (session.lock) 与加锁。

Minecraft 1.21.1:
  LevelStorageSource.LevelStorageAccess 构造时调用 DirectoryLock.create(levelDir),
  对世界目录下 session.lock 文件做 FileChannel.tryLock() (整个文件, 独占锁)。
  游戏运行期间持有该锁; 另一个进程 (包括本工具) 无法再获取, 即 "世界被占用"。
  正常退出/崩溃时 OS 会自动释放, session.lock 文件本身保留。

本模块用与 Java 兼容的整文件独占锁实现:
  - Windows: ctypes 调用 LockFileEx/UnlockFileEx (范围 0..2^64-1)
  - POSIX: fcntl.flock(LOCK_EX | LOCK_NB)

这样:
  1. 游戏正在运行时, 本工具加锁失败 -> 拒绝操作 (满足 "非占用状态存档" 要求)
  2. 本工具运行期间, 游戏也无法打开该世界 -> 避免并发写坏存档
"""

from __future__ import annotations

import os
import sys

LOCK_FILENAME = "session.lock"


class WorldInUseError(OSError):
    """世界正被其他进程 (通常是 Minecraft) 占用。"""


class DirectoryLock:
    def __init__(self, world_dir: str):
        self.world_dir = os.path.abspath(world_dir)
        self.path = os.path.join(self.world_dir, LOCK_FILENAME)
        self.fd: int | None = None
        self._windows_overlapped = None
        self._acquire()

    # ------------------------------------------------------------------ #
    def _acquire(self) -> None:
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            if sys.platform == "win32":
                self._lock_windows()
            else:
                self._lock_posix()
        except WorldInUseError:
            self._close_fd()
            raise
        except Exception:
            self._close_fd()
            raise

    def _lock_windows(self) -> None:
        import ctypes
        import ctypes.wintypes as wt
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD),
                ("OffsetHigh", wt.DWORD),
                ("hEvent", wt.HANDLE),
            ]

        lock_file_ex = kernel32.LockFileEx
        lock_file_ex.argtypes = [
            wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(OVERLAPPED)
        ]
        lock_file_ex.restype = wt.BOOL

        LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
        LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
        ERROR_LOCK_VIOLATION = 33
        ERROR_IO_PENDING = 997

        handle = msvcrt.get_osfhandle(self.fd)  # type: ignore[arg-type]
        ov = OVERLAPPED()
        ok = lock_file_ex(
            handle,
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(ov),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_LOCK_VIOLATION, ERROR_IO_PENDING):
                raise WorldInUseError(
                    f"世界正被占用 (session.lock 已被其他进程锁定): {self.world_dir}\n"
                    "请先完全退出 Minecraft (包括单人游戏存档界面), 再重试。"
                )
            raise OSError(err, f"无法锁定 {self.path}: WinError {err}")
        self._windows_overlapped = (kernel32, handle, ov)

    def _lock_posix(self) -> None:
        import fcntl

        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[arg-type]
        except BlockingIOError:
            raise WorldInUseError(
                f"世界正被占用 (session.lock 已被其他进程锁定): {self.world_dir}\n"
                "请先完全退出 Minecraft, 再重试。"
            ) from None

    # ------------------------------------------------------------------ #
    def _close_fd(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if sys.platform == "win32" and self._windows_overlapped is not None:
                import ctypes
                import ctypes.wintypes as wt

                kernel32, handle, ov = self._windows_overlapped
                unlock = kernel32.UnlockFileEx
                unlock.argtypes = [
                    wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(type(ov))
                ]
                unlock.restype = wt.BOOL
                unlock(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov))
        except Exception:
            pass
        finally:
            self._windows_overlapped = None
            self._close_fd()

    def __enter__(self) -> "DirectoryLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass
