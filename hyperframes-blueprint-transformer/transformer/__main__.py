"""Entrypoint for `python -m transformer`.

Just delegates to `cli.main()` — kept separate so the CLI module is
importable for testing without triggering `argparse`'s `sys.argv`
parsing.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
