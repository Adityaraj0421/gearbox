"""Tests for app.py.  Runnable with: python3 -m pytest -q"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app import greet, add, divide, append_state, describe


def test_greet():
    assert greet("Alice") == "Hello, Alice!"


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_describe_nonempty():
    result = describe()
    assert isinstance(result, str) and len(result) > 0


def test_divide_normal():
    assert divide(10, 2) == 5.0


def test_divide_by_zero_returns_none():
    # BUG: currently raises ZeroDivisionError — this test FAILS on unmodified fixture.
    result = divide(5, 0)
    assert result is None, f"expected None, got {result!r}"


def test_divide_by_zero_no_assert_divide_none():
    # Alias used by T1 accept-grader grep: asserts divide(...,0) is None.
    result = divide(1, 0)
    assert result is None
