#!/usr/bin/env python
"""
Run tests for Alert Dashboard V2.

Usage:
    python run_tests.py              # Run all tests
    python run_tests.py -v           # Verbose output
    python run_tests.py --cov        # With coverage report
"""

import subprocess
import sys


def main():
    """Run pytest with provided arguments."""
    args = ["pytest"] + sys.argv[1:]

    # Add default verbosity if not specified
    if "-v" not in args and "--verbose" not in args:
        args.append("-v")

    # Run pytest
    result = subprocess.run(args)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
