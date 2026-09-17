from src.render import render


def test_simple_substitution():
    assert render("hello {name}", {"name": "world"}) == "hello world"


def test_repeated_placeholder():
    assert render("{x}+{x}", {"x": "1"}) == "1+1"


def test_multiple_values():
    assert render("{a} and {b}", {"a": "1", "b": "2"}) == "1 and 2"


def test_unknown_placeholder_kept_literal():
    assert render("hi {nobody}", {}) == "hi {nobody}"


def test_placeholder_does_not_evaluate_attributes():
    class User:
        name = "alice"

    assert render("{user.name}", {"user": User()}) == "{user.name}"


def test_secret_attribute_not_leaked():
    class Session:
        token = "s3cr3t-token"

    out = render("{session.token}", {"session": Session()})
    assert "s3cr3t-token" not in out
