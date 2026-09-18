from src.escapes import escape_html
from src.template import render


def test_none_renders_empty():
    assert render("Hi {{name}}!", {"name": None}) == "Hi !"


def test_ampersand_escaped():
    assert render("{{x}}", {"x": "<b>&"}) == "&lt;b&gt;&amp;"


def test_plain_replacement():
    assert render("Hi {{name}}", {"name": "Ann"}) == "Hi Ann"


def test_autoescape_off_keeps_raw():
    assert render("{{x}}", {"x": "<b>"}, autoescape=False) == "<b>"


def test_escape_html_basics():
    assert escape_html("a<b") == "a&lt;b"
    assert escape_html("a&b") == "a&amp;b"
