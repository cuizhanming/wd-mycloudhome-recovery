import sys

if len(sys.argv) > 1 and sys.argv[1] in ("list", "tree", "copy", "resume", "status", "disks"):
    from .cli import main
else:
    from .app import main

sys.exit(main())
