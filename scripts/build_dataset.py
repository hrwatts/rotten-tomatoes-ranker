#!/usr/bin/env python3
"""
Canonical entrypoint for the recommended Rotten Tomatoes dataset build.

This wrapper keeps the public quickstart stable even if the implementation
module changes later.
"""

from rt_database_builder_playwright import main


if __name__ == "__main__":
    main()

