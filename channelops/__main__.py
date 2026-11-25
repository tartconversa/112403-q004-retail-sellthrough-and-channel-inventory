"""Entry point so the app runs as ``python3 -m channelops``."""

from .web import main

if __name__ == "__main__":
    raise SystemExit(main())
