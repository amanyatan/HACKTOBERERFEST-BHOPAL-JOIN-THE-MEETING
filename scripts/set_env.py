"""Set or replace ONE variable in backend/.env without an editor. The value is typed hidden and never printed.

    backend/.venv/Scripts/python scripts/set_env.py VERCEL_TOKEN
"""
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / "backend" / ".env"
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def set_var(name: str, value: str, path: Path = ENV) -> str:
    """Replace the NAME= line (or append one), keep every other line exactly as it is. Returns 'replaced' or 'added'."""
    if not NAME_RE.match(name):
        raise ValueError("name must look like VERCEL_TOKEN (capital letters, digits, underscores)")
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("value must be one non-empty line")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new, found = f"{name}={value}", False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name and "=" in line and not line.lstrip().startswith("#"):
            lines[i], found = new, True
            break
    if not found:
        lines.append(new)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")  # write beside the target, then swap in one step
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return "replaced" if found else "added"


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    try:
        value = getpass.getpass(f"Paste the value for {args[0]} (hidden, then Enter): ")
        action = set_var(args[0], value)
    except ValueError as e:
        print("Not saved:", e)
        return 1
    print(f"{args[0]} {action} in {ENV}. ({len(value.strip())} characters; the value itself was not shown.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
