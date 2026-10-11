"""Keep the computer awake while a copy is running (sleep disconnects USB disks)."""
from __future__ import annotations

import os
import subprocess
import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self):
        self._proc = None

    def __enter__(self):
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        elif sys.platform == "darwin":
            try:
                self._proc = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
            except OSError:
                self._proc = None
        return self

    def __exit__(self, *exc):
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        elif self._proc is not None:
            self._proc.terminate()
            self._proc = None
