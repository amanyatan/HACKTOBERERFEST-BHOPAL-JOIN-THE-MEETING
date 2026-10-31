import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.write_files import InvalidFiles, validate, write_files

OK = {"path": "index.html", "content": "<h1>hi</h1>"}


@pytest.mark.parametrize("path", ["../x.html", "/etc/a.html", "a/../b.html", "C:/a.html", "a\b.html", "a.exe", "Index.html", ".env"])
def test_bad_paths_rejected(path):
    with pytest.raises(InvalidFiles):
        validate([OK, {"path": path, "content": "x"}])


def test_requires_index_and_size_and_dupes():
    with pytest.raises(InvalidFiles):
        validate([{"path": "a.css", "content": ""}])
    with pytest.raises(InvalidFiles):
        validate([{"path": "index.html", "content": "x" * 300_000}])
    with pytest.raises(InvalidFiles):
        validate([OK, OK])


def test_writes_nested(tmp_path):
    write_files(tmp_path / "s", [OK, {"path": "css/a.css", "content": "b{}"}])
    assert (tmp_path / "s" / "css" / "a.css").read_text() == "b{}"
