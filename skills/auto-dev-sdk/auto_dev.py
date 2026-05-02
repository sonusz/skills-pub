#!/usr/bin/env python3
"""Executable shim — `./auto_dev.py <verb> ...` is equivalent to `auto-dev ...`."""
import sys

from auto_dev.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
