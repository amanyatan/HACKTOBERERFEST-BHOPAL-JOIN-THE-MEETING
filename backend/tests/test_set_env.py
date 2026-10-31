import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("set_env", Path(__file__).parent.parent.parent / "scripts" / "set_env.py")
set_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(set_env)


def test_replaces_only_the_named_line_and_keeps_everything_else(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# keep me\nA=1\nVERCEL_TOKEN=old\nB=2\n")
    assert set_env.set_var("VERCEL_TOKEN", "  new-value ", f) == "replaced"
    assert f.read_text() == "# keep me\nA=1\nVERCEL_TOKEN=new-value\nB=2\n"


def test_adds_the_variable_even_when_the_file_has_no_trailing_newline_or_does_not_exist(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1")  # no newline: a naive append would glue the two settings together
    assert set_env.set_var("VERCEL_TOKEN", "t", f) == "added"
    assert f.read_text() == "A=1\nVERCEL_TOKEN=t\n"
    g = tmp_path / "new.env"
    assert set_env.set_var("X", "y", g) == "added" and g.read_text() == "X=y\n"


def test_a_commented_out_line_is_not_mistaken_for_the_variable(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# VERCEL_TOKEN=example\n")
    assert set_env.set_var("VERCEL_TOKEN", "real", f) == "added"
    assert f.read_text() == "# VERCEL_TOKEN=example\nVERCEL_TOKEN=real\n"


@pytest.mark.parametrize("name,value", [("lower", "x"), ("BAD NAME", "x"), ("OK", ""), ("OK", "a\nB=injected")])
def test_rejects_bad_names_and_values_without_touching_the_file(tmp_path, name, value):
    f = tmp_path / ".env"
    f.write_text("A=1\n")
    with pytest.raises(ValueError):
        set_env.set_var(name, value, f)
    assert f.read_text() == "A=1\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]  # no temp file left behind
