"""Validate and write model-produced files. Model output is untrusted."""
import re
import shutil
from pathlib import Path

ALLOWED_EXT = {".html", ".css", ".js", ".json", ".svg", ".txt", ".md"}
MAX_FILES = 20
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 600_000
_PATH = re.compile(r"^[a-z0-9][a-z0-9_\-./]*$")


class InvalidFiles(ValueError):
    pass


def validate(files: list[dict]) -> list[dict]:
    if not files or len(files) > MAX_FILES:
        raise InvalidFiles(f"need 1-{MAX_FILES} files")
    seen, total = set(), 0
    for f in files:
        path, content = f.get("path", ""), f.get("content", "")
        if not isinstance(path, str) or not isinstance(content, str):
            raise InvalidFiles("path and content must be strings")
        if not _PATH.match(path) or ".." in path or "//" in path or path.endswith("/") or len(path) > 100:
            raise InvalidFiles(f"bad path: {path[:60]!r}")
        if Path(path).suffix.lower() not in ALLOWED_EXT:
            raise InvalidFiles(f"file type not allowed: {path}")
        if path in seen:
            raise InvalidFiles(f"duplicate path: {path}")
        size = len(content.encode())
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise InvalidFiles("files too large")
        seen.add(path)
    if "index.html" not in seen:
        raise InvalidFiles("index.html is required")
    return files


def write_files(root: Path, files: list[dict]) -> None:
    validate(files)
    root = root.resolve()
    if root.exists():
        shutil.rmtree(root)
    for f in files:
        dest = (root / f["path"]).resolve()
        if root not in dest.parents:
            raise InvalidFiles("path escapes site folder")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"], encoding="utf-8")
