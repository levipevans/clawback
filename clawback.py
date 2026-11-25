"""Launcher for the Clawback assistant.

Run it with:

    python clawback.py

Everything else lives in the `clawback` package so it can be tested and
imported without side effects.
"""

from clawback.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
