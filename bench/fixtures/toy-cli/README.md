# toy-cli

A minimal Python CLI used as a gearbox benchmark fixture.

## Commands

- `greet <name>` — print a greeting
- `add <a> <b>` — sum two integers
- `divide <a> <b>` — divide a by b (returns float)
- `append-state <text>` — append a line to `state.txt`
- `describe` — print a short description

## Running

```
python3 app.py <command> [args]
```

## Tests

```
python3 -m pytest -q
```

Some tests fail on the unmodified fixture by design — they exercise known bugs.
