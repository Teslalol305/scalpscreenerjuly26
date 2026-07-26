"""CLI entrypoint: `python -m tapescreen [--record N | --replay FILE] ...`."""

from tapescreen.main import cli

if __name__ == "__main__":
    raise SystemExit(cli())
