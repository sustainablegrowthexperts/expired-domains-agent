"""Command-line entry point for ``python -m expired_domains``."""

import sys

from expired_domains.cli import main

if __name__ == "__main__":
    sys.exit(main())
