"""Entry point used by PyInstaller builds."""
import sys

from homeduo_recovery import cli
from homeduo_recovery.app import main as gui_main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("list", "tree", "copy", "resume", "status", "disks"):
        sys.exit(cli.main())
    sys.exit(gui_main())
