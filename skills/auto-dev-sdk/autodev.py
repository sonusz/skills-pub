#!/usr/bin/env python3
"""autodev CLI shim — `./autodev.py <verb> ...` == `autodev ...`."""
import sys

from autodev.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
