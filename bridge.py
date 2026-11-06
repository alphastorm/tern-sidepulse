#!/usr/bin/env python3
"""Entry point for the tern-sidepulse helper. Runs with ``python3 -I -S -X pycache_prefix=<data>/pycache bridge.py <command>``.

Kept parseable by old interpreters so the version guard below can answer with JSON instead of
a SyntaxError.
"""
import json
import os
import sys

MINIMUM = (3, 10)


def check_python(version_info):
    if tuple(version_info[:2]) >= MINIMUM:
        return None
    version = ".".join(str(part) for part in version_info[:3])
    return {
        "ok": False,
        "error": "python_too_old",
        "detail": "requires Python %d.%d+" % MINIMUM,
        "version": version,
        "executable": sys.executable,
    }


def main():
    problem = check_python(sys.version_info)
    if problem is not None:
        sys.stdout.write(json.dumps(problem) + "\n")
        return 3
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)  # -I leaves the script directory off sys.path
    if getattr(sys, "pycache_prefix", None) is None:
        # A plain `python3 bridge.py …` from a shell must not write __pycache__ into the plugin
        # directory, which Tern watches for reloads; the plugin's own launches cache elsewhere.
        sys.dont_write_bytecode = True
    from tern_sidepulse.cli import main as cli_main
    from pathlib import Path

    return cli_main(sys.argv[1:], Path(os.path.abspath(__file__)))


if __name__ == "__main__":
    sys.exit(main())
