"""Write the release version (e.g. v0.1.1) into homeduo_recovery/__init__.py before building."""
import pathlib
import re
import sys

version = sys.argv[1].lstrip("v")
init = pathlib.Path(__file__).resolve().parent.parent / "homeduo_recovery" / "__init__.py"
text = init.read_text(encoding="utf-8")
new = re.sub(r'__version__ = "[^"]*"', f'__version__ = "{version}"', text)
if new == text and f'"{version}"' not in text:
    sys.exit("could not stamp version")
init.write_text(new, encoding="utf-8")
print("version", version)
