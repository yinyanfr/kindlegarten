"""Run kindlegarten with the checkout's local account and output directories."""

from pathlib import Path

from kindlegarten.cli import entrypoint


if __name__ == "__main__":
    raise SystemExit(entrypoint(Path(__file__).resolve().parent))
