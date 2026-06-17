#!/usr/bin/env python3
"""Toy CLI for gearbox benchmark fixture.

Commands:
  greet <name>           Print a greeting.
  add <a> <b>            Print the sum of two integers.
  divide <a> <b>         Divide a by b (uncaught ZeroDivisionError on b=0).
  append-state <text>    Append a line to state.txt (no locking — latent race).
  describe               Print a short description of this CLI.
"""
import argparse
import os


def greet(name: str) -> str:
    """Return a greeting string."""
    return f"Hello, {name}!"


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b


def divide(a: float, b: float) -> float:
    """Return a / b.

    BUG: raises ZeroDivisionError when b == 0 instead of returning None.
    """
    return a / b


def append_state(text: str, path: str = "state.txt") -> None:
    """Append a line to a state file.

    BUG: no file locking — concurrent invocations can interleave writes and
    corrupt the file (lost updates / partial lines).
    """
    with open(path, "a") as f:
        f.write(text + "\n")


def describe() -> str:
    """Return a short description of this CLI."""
    return (
        "toy-cli: a minimal Python CLI for benchmark tasks. "
        "Functions: greet, add, divide, append_state, describe."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_greet = sub.add_parser("greet")
    p_greet.add_argument("name")

    p_add = sub.add_parser("add")
    p_add.add_argument("a", type=int)
    p_add.add_argument("b", type=int)

    p_div = sub.add_parser("divide")
    p_div.add_argument("a", type=float)
    p_div.add_argument("b", type=float)

    p_app = sub.add_parser("append-state")
    p_app.add_argument("text")
    p_app.add_argument("--path", default="state.txt")

    sub.add_parser("describe")

    args = parser.parse_args()

    if args.cmd == "greet":
        print(greet(args.name))
    elif args.cmd == "add":
        print(add(args.a, args.b))
    elif args.cmd == "divide":
        print(divide(args.a, args.b))
    elif args.cmd == "append-state":
        append_state(args.text, args.path)
    elif args.cmd == "describe":
        print(describe())


if __name__ == "__main__":
    main()
