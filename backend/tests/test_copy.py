from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
HTML = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
PERSONAS = (ROOT / "backend" / "personas.py").read_text(encoding="utf-8")


def test_the_harsh_wording_is_gone_from_what_visitors_see_and_hear():
    for text in (HTML, PERSONAS):
        assert "means losing access" not in text and "anonymous ID kept in" not in text


def test_the_limit_is_still_disclosed_honestly_in_the_small_print_and_in_cutus_facts():
    assert "linked to this browser" in HTML and "starts a new empty workspace" in HTML  # one click away, not hidden
    assert "<details" in HTML and "How is this saved?" in HTML
    assert "starts a new empty workspace" in PERSONAS  # Cutu answers truthfully if asked
